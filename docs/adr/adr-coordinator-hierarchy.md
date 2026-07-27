# ADR: Coordinator Hierarchy and Polling Architecture

## Status

Accepted - 2026-07-25

## Context

The integration uses Home Assistant's `DataUpdateCoordinator` pattern to
manage polling, state management, and failure handling. Three coordinators
handle different responsibilities: discovery, electricity polling, and water
polling. Their hierarchy defines how data flows from the provider API to
the sensor entities.

## Design

### Coordinator Hierarchy

```
SpGroupDiscoveryCoordinator (update_interval=None, one-shot)
  │  Fetches accounts, premises, utility types at setup
  │  Data: SpGroupCoordinatorData
  │
  ├── SpGroupElectricityCoordinator (update_interval=~60min)
  │     Polls AMI charts API for 30-min bucket data
  │     require_two_sightings=False (direct accept)
  │
  └── SpGroupWaterCoordinator (update_interval=None, timer-based)
        Polls history charts API for monthly bucket data
        Timer-triggered via async_track_point_in_time
```

### Discovery Coordinator

`SpGroupDiscoveryCoordinator` runs once during setup. It calls the Yggdrasil
and Jarvis APIs to discover accounts and premises, then normalizes the
results into `SpGroupCoordinatorData`. The data includes premises, accounts,
and `SpGroupFutureSensorKey` objects that represent potential sensor entities.

The discovery coordinator is configured with `update_interval=None` and is
refreshed manually via `async_request_refresh()` during setup. After the
initial refresh, it is not polled again (utility changes would require
re-configuration).

### Electricity Coordinator

`SpGroupElectricityCoordinator` polls the AMI charts API on a fixed interval
(~60 minutes, configurable through options). Each poll:

1. Refreshes the session if needed
2. Calls `_async_poll_sources(backfill=False)`
3. For each source, calls `_request_start()` to get the cursor
4. Calls `get_ami_charts()` with the cursor date and current date
5. Parses the response through the electricity normalization/composition path
   (the legacy `_electricity_ami_source_poll()` adapter must not create
   internal unresolved ranges)
6. Applies observations to the ledger via `async_apply_observations()`
7. If `accepted_delta > 0`, schedules a statistics retry

The coordinator's `_async_update_data()` catches:
- `ConfigEntryAuthFailed` → reauth flow
- `TimeoutError` → `UpdateFailed`
- `LedgerError`, `OSError` → `UpdateFailed`
- `SpGroupMobileError` → `UpdateFailed` (with API error logged)

Failure causes the coordinator to retry on the next scheduled interval.
There is no immediate retry or backoff — the standard HA coordinator
behavior applies.

### Water Coordinator

`SpGroupWaterCoordinator` does not use a fixed poll interval. Instead, it
calculates the next run time via `next_water_run()` which selects the 1st
of the next month at a configured hour. A timer is set via
`async_track_point_in_time`.

Each water poll:
1. Calls `get_history_charts()` (no date parameters — returns all history)
2. Parses through `_water_history_source_poll()`
3. Applies monthly bucket observations to the ledger
4. Schedules next poll for the 1st of the following month

Water also has a startup catch-up: if HA starts after the configured monthly
poll time, it schedules one immediate `async_request_refresh()` to capture
the most recent closed month.

### Statistics Retry Scheduling

When a poll accepts new buckets (`accepted_delta > 0`), the coordinator
schedules a statistics-only retry via `_schedule_statistics_retry()`. This
calls `async_schedule_statistics_backfill()` with `force=True`.

The HA Recorder also fires `EVENT_RECORDER_HOURLY_STATISTICS_GENERATED`
every hour, which triggers a statistics retry regardless of `accepted_delta`.
This provides a safety net for missed retries.

### Backfill (Source History)

The initial backfill runs as a tracked background task. It is not a
coordinator refresh — it calls `async_poll_now(backfill=True)` which
directly invokes `_async_poll_sources()` without HA's coordinator
debounce or failure handling. Backfill is serialized per-utility:
electricity runs first (with two-sighting poll count derived from
`require_two_sightings`), then water.

## Consequences

### Positive

- Standard HA coordinator pattern — works with HA's retry, state, and
  availability machinery
- Electric and water have independent poll schedules
- Statistics retry is decoupled from source polling
- Backfill is one-shot and does not re-run on restart after success

### Negative

- No immediate retry on transient API failures — the coordinator waits
  for the next scheduled interval. This is acceptable for a ~60 minute
  cadence but means a single HTTP 500 causes a ~1 hour data gap.
- Water polling is timer-based, which is less resilient than the
  coordinator's fixed interval (timer is set once and not re-evaluated
  on failure).

## Related

- `coordinator.py` — coordinator classes
- `backfill.py` — initial backfill orchestration
- `__init__.py` — setup and retry listener registration
