# Complete Data Flow: Sensor, Statistics, and Polling

## Current implementation note — 2026-07-30

The provider-owned external statistics path is authoritative for new SP Group
setups. The native-reference, native-offset, and two-sighting material below
was superseded by the external reconciliation design and is retained only as
historical context. Current external rows use
`sp_group:<stable_sensor_key>_consumption`, `source=sp_group`, and are written
with `async_add_external_statistics()`.

## HA has three separate data stores

### 1. States Table (entity history)
```
Table: states
Columns: entity_id, state, last_changed, last_updated
Populated by: Every call to async_set_state() (entity platform)
```

Every time `sensor.electricity_consumption` changes from 1000.0 to 1017.374,
a new row is written to `states`. This is the **"History"** source in the
History panel.

### 2. Statistics Table (long-term hourly)
```
Table: statistics
Columns: metadata_id, start_ts, state, sum
Populated by: provider-owned external statistics through
`async_add_external_statistics()` or by HA's native compiler for entity-backed
series.
```

Hourly rows with `state` (meter reading at that hour) and `sum` (cumulative).
This is the **"Long-term statistics"** source in the History panel.
The Energy Dashboard consumes this table.

### 3. Statistics Short-Term Table (5-minute)
```
Table: statistics_short_term
Columns: metadata_id, start_ts, state, sum
Populated by: compile_statistics() (runs hourly)
```

HA compiles 5-minute statistics from the sensor's state changes. This fills
gaps where no imported statistics exist. The Energy Dashboard merges this
with the hourly `statistics` table.

## How SP Group's data flows

```
SP Group API (30-min buckets)
     ↓
Coordinator polling (normal/backfill)
     ↓
Ledger (accepted/pending buckets)
     ↓
  ┌─→ Sensor: latest_total = seed + sum(post_seed_accepted)
  │      ↓
  │  HA States table → "History" in panel
  │      ↓
  │  HA compile_statistics() → statistics_short_term
  │
  └─→ Statistics backfill: projects buckets into hourly rows
         ↓
     async_add_external_statistics() → provider-owned statistics table
         ↓
     "Long-term statistics" in panel → Energy Dashboard
```

## The sensor value

The SP Group sensor returns:
```python
native_value = str(entry.latest_total)
```
Where:
```python
latest_total = Decimal(seed_value) + sum(post_seed_accepted_bucket_values)
```

This is the **correct cumulative meter reading** at this moment. For a
`total_increasing` sensor, this value MUST be monotonic.

## The statistics import

The active statistics backfill does:

1. **Project** all accepted ledger buckets into hourly rows with correct
   timestamps, using `LedgerStatisticsIndex` to reconstruct the cumulative
   meter state at each hour.

2. Build a deterministic external plan with provider timestamps and
   cumulative non-negative `sum` values.
3. Preflight existing external rows. Exact rows are idempotent; missing rows
   are importable; visible mismatches or duplicate starts are hard conflicts.
4. Queue only missing rows through `async_add_external_statistics()`.
5. Verify visibility and exact state/sum values through the Recorder readback
   path. Delayed visibility remains pending and is retried from the
   Recorder-event statistics retry without refetching provider data.
6. Persist progress only after verification, with monotonic watermark and
   source-fingerprint/epoch guards.

## Legacy native path

The reference validation (`_validated_reference`) at statistics_backfill.py:740:
```python
expected_state = index.state_at(start + 1h)
if not _float_equal(row.state, expected_state):
    return None
```

This verifies that the HA-native row's `state` (compiled from sensor state
changes) matches the ledger's reconstructed state at the same hour. If they
don't match, the import refuses to proceed -- avoiding silent data corruption.

The following mismatch analysis applies only to the legacy native path and is
not a blocker for the provider-owned external series:
- The **sensor state** jumped from 1000.0 → 1017.374 in one snapshot
  (latest_total was recalculated)
- HA's compile_statistics sees this snapshot and creates a row with
  state=1017.374
- But the **ledger** has 52 buckets spanning 25 hours -- the reconstructed
  state should show intermediate values (1000.5, 1001.2, etc.) at each hour
- The HA row's state (1017.374) doesn't match the ledger's expected state
  at the REFERENCE hour -- only at the CURRENT hour

## Historical two-sighting context

The two-sighting rule requires two matching polls before data is accepted.
This means:
- All 52 post-seed buckets were accepted in ONE BATCH on the second poll
- latest_total jumped from 1000.0 to 1017.374 in one snapshot
- HA compiled that single snapshot into statistics
- The statistics import sees a mismatch between the HA-compiled row and the
  ledger's projected distribution

## Historical summary of issues

| Issue | Root Cause | Effect |
|---|---|---|
| Polling overlap | source_poll_cursor_start -2h | Re-fetches accepted data |
| Sensor one-shot jump | 52 buckets accepted at once | latest_total jumps 1000→1017 |
| Statistics blocked | Reference validation fails in the legacy path | Legacy rows not imported |
| No auto-retry | Legacy metadata showed "complete" | Legacy listener did not re-trigger |

## What the current implementation considers working correctly

1. Polling window = [last_accepted_end, now] (no overlap)
2. Accepted ledger buckets are projected into the external series.
3. Exact existing rows are reused; only missing rows are queued once.
4. Recorder visibility is verified before the ledger watermark advances.
5. Pending visibility is resolved by event-driven retry; conflicts are never
   overwritten.
6. The Energy Dashboard uses the provider-owned external statistic ID.
