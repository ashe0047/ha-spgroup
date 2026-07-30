# ADR: Two-Sighting Rule Removal

## Status

Accepted - 2026-07-25

## Authority

Normative for direct acceptance; two-sighting details below are historical context.

## Implementation

`custom_components/sp_group/coordinator.py`, `backfill.py`, and acceptance tests.

The `True`/`False` values in the historical context below describe the former
configuration. The accepted decision and current implementation use
`require_two_sightings=False` for both utilities with direct bucket
acceptance; legacy pending observations are retained only for migration and
diagnostics.

## Context

The integration originally required two matching API responses before
accepting a bucket observation into the ledger. This "two-sighting" rule
was designed to prevent a single erroneous API response from corrupting
the accumulated total.

The rule was implemented via `require_two_sightings` on the coordinator:
- Electricity: `True` (historical configuration; no longer active)
- Water: `False` (always direct accept)

## Rationale for Removal

### The rule does not protect against consistent erroneous data

If the API returns a wrong value for a specific time slot, and that value
is consistent across two polls, the rule accepts it. The only scenario it
protects against is a single-cycle transient glitch — and even then, the
data is delayed by one poll cycle (~1 hour) without meaningful protection.

### The API returns immutable data per time slot

SP Group's AMI API returns finalized consumption data for 30-minute time
slots. A bucket at `14:00-14:30` has the same value on every poll — it
represents the same physical consumption interval. The parser already
validates for positive values, valid dates, and malformed responses.

### The rule added unnecessary latency

Every observation was delayed by ~1 hour (one poll cycle) while waiting
for confirmation. For a utility meter reading, this latency is invisible
at the daily consumption level, but it complicates debugging and
verification.

## Implementation

### Changes

1. `SpGroupElectricityCoordinator.require_two_sightings = False`
   — enables direct accept for electric observations.

2. Backfill `poll_count` derived from `require_two_sightings` flag
   — when `False`, the backfill runs a single poll instead of two.

3. Startup migration clears legacy pending buckets for electric entries
   — prevents the cursor from stalling on pending data that will never
   be matched.

### What stayed

- The `pending` tuple in `SpGroupLedgerEntry` — kept for backward
  compatibility with existing persisted data
- The pending logic in `_apply_observations_to_entry()` — dead code
  path, kept for minimal diff
- `source_poll_cursor_start()` still checks `pending_start` before
  `accepted_end` — legacy pending would have stalled the cursor, which
  is why the startup migration exists

## Consequences

### Positive

- Observations are accepted on the first poll — no ~1 hour delay
- Backfill runs a single poll per utility instead of two
- Simpler data flow: API → observation → accepted (no pending state)
- No behavioral change for water (was already `False`)

### Negative

- Legacy pending buckets from before the change require the startup
  migration to prevent cursor stalls
- A single-cycle transient API error could in theory introduce a wrong
  value (no more or less likely than before — consistent wrong values
  passed through regardless)

## Related

- `coordinator.py` — `require_two_sightings` field and classes
- `backfill.py` — `poll_count` derivation
- `ledger.py` — `async_load()` pending migration
- `ledger.py` — `_apply_observations_to_entry()` pending path (dead code)
