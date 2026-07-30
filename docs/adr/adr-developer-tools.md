# ADR: Developer Tools and Testing Helpers

## Status

Accepted - 2026-07-25

## Authority

Normative for development-only services and runtime validation helpers.

## Implementation

`custom_components/sp_group/dev.py`, service registration, and developer-tool tests.

## Context

The integration includes developer-only tools (`dev.py`) and registration
services that are not available in production. These tools provide testing
shortcuts for integration validation without waiting for natural schedules
(statistics compilation, polling cycles, data gaps).

## Design

### Gating

All dev tools are gated behind an environment variable `ENV_DEV_TOOLS`.
Services are only registered when this variable is set to `"1"`:

```python
async_register_dev_services(hass):
    if not os.environ.get(ENV_DEV_TOOLS):
        return  # production — no dev services
    hass.services.async_register(...)
```

### Registered Services

1. **`force_statistics`** — triggers the HA Recorder's statistics
   compilation for the electric and water sensor entities. This forces
   the creation of the native reference row that the external statistics
   path may be waiting for. Runs the coordinator's `_async_update_data()`
   to ensure current data is available before compilation.

2. **`poll_now`** — calls `coordinator.async_poll_now()` for the
   specified utility. Accepts a `utility` parameter (`"electric"` or
   `"water"`) and an optional `extra_mode` parameter. `extra_mode="backfill"`
   runs a backfill poll instead of a normal poll.

3. **`prepare_poll_gap`** — arms the ledger for a controlled poll gap
   test. Sets `source_backfill_status` to `succeeded` for all sensors
   of the specified utility, creating the condition where a normal poll
   will find data that the backfill didn't. Intended to be followed by
   `poll_now` (normal mode) to test pre-seed lag recovery.

4. **`refresh_session`** — loads and refreshes the mobile session from
   `.runtime/mobile-session.json`. Updates the HA Store with the refreshed
   session. Used when the session file changes outside HA (e.g., manual
   token refresh).

### Constraint: prepare_poll_gap Requires Empty Ledger

`prepare_poll_gap` enforces that all accepted and pending buckets are
empty. If the ledger has data, it raises `HomeAssistantError` with
`poll_gap_requires_empty_ledger`. This prevents accidentally clobbering
a populated ledger during testing.

## Consequences

### Positive

- Integration tests can simulate scenarios that would otherwise require
  waiting hours or days
- `force_statistics` is essential for testing the external statistics
  import path (native reference needs the Recorder compile to create it)
- `prepare_poll_gap` enables testing pre-seed lag recovery end-to-end
- Environment variable gating prevents exposure in production

### Negative

- Dev tools are not discoverable through HA UI — they are service calls
  only
- `force_statistics` uses HA's `sensor.recorder.compile_statistics`
  service which is a private/internal API, not a public contract

## Related

- `dev.py` — all service handlers
- `scripts/ha_dev_force_statistics.py` — CLI wrapper for force_statistics
- `scripts/ha_dev.py` — HA dev runner with auto-onboarding
- `services.yaml` — service definitions
