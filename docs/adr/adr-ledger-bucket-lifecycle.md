# ADR: Ledger Bucket Accumulation Lifecycle

## Status

Accepted - 2026-07-25

## Context

The SP Group integration tracks utility consumption as a sequence of
time-bucketed observations. These observations come from provider API
responses and must be accumulated into a trusted total (the sensor value)
while handling duplicates, corrections, coverage gaps, and persistence.

The ledger (`ledger.py`, 1395 lines) is the accumulation state machine.
It stores accepted buckets, tracks provider coverage boundaries, and
derives the cursor for polling windows.

## Design

### Bucket Lifecycle

```
API response → parser extracts SpGroupBucketObservation
  │
  ├──→ Already accepted? (accepted_by_key match)
  │     └──→ Same value → skip (idempotent)
  │     └──→ Different value → record correction, keep existing
  │
  └──→ New observation
        └──→ Direct accept (require_two_sightings=False, current behavior)
              → appended to accepted list
              → latest_total recalculated
              → statistics_dirty_from set
```

Previously (with `require_two_sightings=True`), observations passed through
a pending state requiring a second matching observation. This was removed
because the API returns immutable data per time slot — a second poll always
returns the same value, making the check redundant.

### Ledger Entry Structure (`SpGroupLedgerEntry`)

```
SpGroupLedgerEntry:
  sensor_key          — unique identifier for this sensor
  utility_type        — "electric" or "water"
  seed_value          — meter reading at setup time
  seed_effective_from — when the seed was observed
  backfill_from       — start of the backfill window
  accepted            — tuple of SpGroupAcceptedBucket
  pending             — (deprecated) legacy pending buckets
  latest_total        — seed + sum of post-seed accepted bucket values
  latest_accepted_at  — end time of the most recently accepted bucket
  unresolved_ranges   — periods where provider coverage is known missing
  source_coverage_status / start / end — provider data boundaries
  warnings            — non-fatal conditions (corrections, duplicates)
  statistics_*        — statistics import tracking metadata
```

### Accepted Bucket

Each `SpGroupAcceptedBucket` stores:
- `start`, `end` — the 30-min (electric) or month (water) time window
- `value` — the consumption delta for this bucket
- `first_seen_at`, `accepted_at` — timestamps for auditing

### Cursor Derivation (`source_poll_cursor_start()`)

The cursor determines where the next poll starts. It uses the first
available source in this order:

1. Earliest `unresolved_ranges.start` — retry provider coverage gaps
2. Earliest `pending.start` — (legacy, now empty)
3. Latest `accepted.end` — continue from the last proven data point
4. `seed_effective_from` — initial fallback before any data

### Latest Total Calculation

```
latest_total = seed_value + sum(accepted.value for buckets
               where bucket.start >= seed_effective_from)
```

Pre-seed buckets (before the seed observation time) are excluded because
their consumption is already reflected in the seed value (the meter reading
at setup). Only post-seed consumption contributes to the sensor value.

### Coverage Tracking

When the API returns data, the parser reports a `SpGroupSourceCoverage`
object with:
- `status` — `"covered"`, `"lagging"`, or `"malformed"`
- `start`, `end` — the date range of returned data
- `unresolved_ranges` — gaps within the returned data

This coverage is persisted and used by the cursor to prioritize unresolved
ranges on the next poll.

### Persistence

The ledger is persisted to HA Store via `SpGroupLedgerStore` which wraps
HA's `Store` class. Operations are serialized through an `asyncio.Lock()`
to prevent concurrent modifications. The snapshot is held in memory
(`self._snapshot`) and persisted on every mutation.

## Consequences

### Positive

- Buckets are immutable once accepted — corrections are recorded separately
- Cursor always points to the last proven data boundary
- Pre-seed/post-seed separation keeps sensor value anchored to the seed
- HA Store persistence survives restarts

### Negative

- `latest_total` skips pre-seed buckets, meaning the sensor value is only
  post-seed consumption plus the seed (intentional, but surprising)
- Legacy pending buckets from the two-sighting era are never cleaned up
  (cleared on startup via migration)

## Related

- `ledger.py` — `SpGroupLedgerStore`, `_apply_observations_to_entry()`
- `coordinator.py` — `_request_start()`, `source_poll_cursor_start()`
- `sensor.py` — `native_value` derivation
