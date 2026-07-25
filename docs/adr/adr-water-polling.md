# ADR: Water Polling Architecture

## Status

Accepted - 2026-07-25

## Context

Water consumption data is provided by SP Group through a different API
endpoint than electricity. The history charts API (`/jarvis/v4/charts/`)
returns monthly consumption records, not 30-minute intervals. Water buckets
are calendar months (e.g., August 1 to September 1) with a single
consumption value per month.

Water polling must account for:
- Monthly granularity (not real-time)
- Grace period for meter reading finalization (~3 days after month end)
- Seed on month boundaries (e.g., July 1)
- Startup catch-up after HA restart

## Design

### API Endpoint

Water uses `get_history_charts(premise_id)` which returns ALL available
history for the premise. Unlike electricity's `get_ami_charts()`, this
endpoint does not accept date range parameters — it always returns the
full history. Date filtering is done in the parser.

### Polling Schedule

Water does not use the coordinator's fixed `update_interval`. Instead,
a timer is set via `async_track_point_in_time` to fire at a configured
time on the 1st of the next month:

```
next_water_run(now, options) → 1st of next month at HH:MM
  (HH:MM defaults to configured poll time, e.g., 00:00)
```

The timer is reset after each poll completes.

### Observation Parsing

`_water_history_observations()` extracts monthly records from the API
response:

1. Extracts `period` (e.g., "2026-07") from each row
2. Parses to month start datetime (e.g., July 1, 2026 00:00)
3. Checks `_is_water_volume_unit()` — accepts m³, L, and variants
4. Normalizes liters to cubic meters (1000 L = 1 m³)
5. Filters by `start >= backfill_from AND closed_month_ready(end, now)`

### Grace Period

`closed_month_ready(end, now)` ensures the month has closed and the
provider has finalized the reading:

```
ready_at = end + 3 days (_WATER_GRACE_DAYS)
return now >= ready_at
```

A month ending July 1 is not ready until July 4 (July 1 + 3 days). This
grace period is applied during backfill via the clamped `now` — water
backfill uses `seed_effective_from + 3 days` as its end time to include
the grace period.

### Startup Catch-Up

If HA starts after the configured monthly poll time, one immediate
`async_request_refresh()` is scheduled to capture the most recent closed
month. The catch-up is a one-shot task tracked in runtime data and
cancellable on unload.

`_water_startup_catchup_needed()` checks:
- Current day of month < 7 (early enough that catch-up is useful)
- A closed month may be missing from the ledger

### Seed Bridge

`_has_seed_bridge` for water pre-seed buckets verifies a contiguous
monthly chain from `row_start` to `seed_effective_from`. This ensures
the state_at calculation has complete data for accurate cumulative
values. Unlike electricity (which skips the chain check entirely),
water needs this because monthly buckets don't have fixed 30-min step
intervals.

### Statistics

Water statistics are imported as external statistics (same as electricity)
with `statistic_id = "sp_group.<sensor_key>_water_consumption"`. Monthly
buckets are projected to hourly rows at the month boundary.

## Consequences

### Positive

- Monthly granularity matches provider data
- Grace period prevents importing un-finalized readings
- Startup catch-up ensures no data gaps after restart
- Same statistics path as electricity — consistent behavior

### Negative

- Timer-based polling is less resilient than coordinator fixed-interval
  polling (timer must survive restarts)
- 3-day grace period means the most recent month is always late
- Full history API call returns more data than needed (inefficient but
  necessary — API doesn't support date filtering)

## Related

- `coordinator.py` — `SpGroupWaterCoordinator`, `_water_*` functions
- `ledger.py` — `closed_month_ready()`, `_WATER_GRACE_DAYS`
- `statistics_backfill.py` — `_water_statistics()`, `_has_seed_bridge()`
