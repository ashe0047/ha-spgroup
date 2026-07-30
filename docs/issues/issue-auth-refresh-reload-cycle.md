# Auth Refresh Triggers Infinite Entry Reload Cycle

## Status

Needs implementation audit — verify the remaining `async_update_entry` session
persistence path before classifying this issue as resolved.

## Authority

Historical investigation; runtime auth behavior is governed by `adr/adr-auth-session-management.md`.

## Problem

Every ~1 hour, the SP Group integration enters an infinite reload loop that
prevents normal polling and leaves sensor states permanently stuck at their
seed values. The cycle never resolves on its own.

## Root Cause

A destructive interaction between the session refresh mechanism and the config
entry update listener:

1. **Session expiry** (`auth.py:81`): The SP Group mobile session access token
   has an approximate 1-hour TTL. When the electric coordinator's scheduled
   poll starts, `_async_poll_sources` calls `async_refresh_runtime_session_if_needed`
   at `coordinator.py:215`.

2. **Session refresh persists via `async_update_entry`** (`auth.py:99`): The
   shared core function `_async_refresh_session_core` refreshes the expired
   session via the SDK, then calls `hass.config_entries.async_update_entry(entry,
   data={..., session: refreshed.to_dict(), ...})` to persist the new tokens.

3. **Update listener fires `async_reload`**: HA's `async_update_entry` fires
   registered update listeners asynchronously. The listener registered at
   `__init__.py:131` -- `entry.add_update_listener(_async_update_listener)` --
   calls `await hass.config_entries.async_reload(entry.entry_id)` at
   `__init__.py:349`.

4. **Unload happens mid-poll**: The reload calls `async_unload_entry`, which
   cancels the water schedule and backfill tasks. The electric coordinator's
   in-flight `_async_update_data` task continues running but the integration
   is being torn down around it. The poll returns `success: False`.

5. **Setup runs again**: The reload calls `async_setup_entry`, creating new
   coordinators. The new electric coordinator schedules its next poll in 1
   hour.

6. **Repeat**: 1 hour later, the session has expired again, and the cycle
   repeats indefinitely.

### Timeline from Production Logs

```
16:18:14  - Setup completes, session valid
17:18:14  - Electric poll starts  (1 hour later)
17:18:14  - Session refresh triggers async_update_entry
17:18:14  - Update listener calls async_reload
17:18:14  - Unload starts mid-poll
17:18:14  - Poll returns success: False
17:18:14  - Setup runs again
18:31:04  - Electric poll starts  (1 hour later, repeat)
18:31:04  - Cycle repeats
```

## Why This Passes Initial Setup

The update listener is registered at `__init__.py:131`, **after** the discovery
coordinator's first refresh at line 119:

```python
await discovery_coordinator.async_config_entry_first_refresh()  # line 119
# (session refresh here does NOT trigger reload)
entry.async_on_unload(entry.add_update_listener(_async_update_listener))  # line 131
```

So the initial session refresh during setup is safe. But every subsequent
refresh during runtime polling triggers the cycle.

## Two Separate Refresh Paths

There are two independent refresh mechanisms:

### Path A: Config flow dev-only refresh (`config_flow.py:733`)

```python
async def _async_refresh_dev_session_if_needed(self, session):
    refreshed = await client.refresh_session(session)
    await self.hass.async_add_executor_job(
        _write_dev_cached_session, _dev_session_file(), refreshed
    )
    return refreshed
```

- Writes to a local cache file, **not** to `entry.data`
- Does NOT call `_async_refresh_session_core`
- Called only during the config flow's cached-session step
- Does NOT trigger the reload cycle

### Path B: Runtime refresh (`auth.py:52`)

```python
# Called from coordinators before every poll:
#   coordinator.py:73   (discovery)
#   coordinator.py:215  (electric poll)
#   coordinator.py:516  (water poll)
await async_refresh_runtime_session_if_needed(hass, entry)
    └─ _async_refresh_session_core(hass, entry, ...)
        ├─ checks session.expired
        ├─ refreshes via SDK
        ├─ async_update_entry(...)  # ← triggers reload
        └─ on_refreshed → updates runtime_data
```

- Called before every poll in both dev and production
- Persists to `entry.data` via `async_update_entry` -- THIS is the problem
- Triggers the reload cycle

### How the refresh mechanism works

Both paths are **proactive** (not reactive):

1. Called before making API calls (not triggered by 401 errors)
2. Checks the JWT access token's `exp` claim against current time
3. Has a 60-second buffer (`TOKEN_EXPIRY_BUFFER_SECONDS = 60`) -- treats token as
   expired 60s before actual `exp`
4. If expired, calls `client.refresh_session(session)` using the refresh token
5. Returns immediately with the existing session if still valid -- zero API calls

The refresh is NOT:
- On a timer/interval
- Triggered by HTTP 401/403 errors

The refresh correlates with the 1-hour electric poll interval simply because
the Auth0 access token has a ~1-hour `exp`, so by the time the next poll
starts, the token is always expired.

## HA Convention Grounding

The auth flow individually follows HA patterns:

- **Storing auth tokens in `entry.data`**: Used by Blink, Canary, Tedee, and
  many other integrations. Standard practice.
- **`add_update_listener` for options changes**: Used by Canary, AirVisual,
  and others. Standard practice.
- **`async_update_entry` to persist refreshed tokens**: Used by Blink (line
  99) and others. Standard practice.

The issue is that SP Group is the only integration that **combines all three**
in a way that creates conflict. Survey of HA core integrations that have both
`add_update_listener` and `async_update_entry`:

| Integration | `add_update_listener` | `async_update_entry` runtime (auth) | Conflict? |
|---|---|---|---|
| **Blink** | None | Yes (auth refresh) | No -- no listener to trigger |
| **Canary** | `async_reload` | Only in setup/migration | No -- entry.data never changes at runtime |
| **AirVisual** | `async_reload` | Only in setup/migration | No -- entry.data never changes at runtime |
| **Axis** | Specific callback | Only in migration | No -- entry.data never changes at runtime |
| **SP Group** | `async_reload` | Yes (auth refresh ~1hr) | **Yes** -- every refresh triggers reload |

The HA `async_update_entry` API fires the same listeners for both `data`
changes and `options` changes. There is no built-in mechanism to distinguish
between the two. This means any integration that both (a) updates `entry.data`
during runtime and (b) registers an update listener that reloads the entry,
will trigger an unwanted reload on every data update.

## Current Triple-Redundancy in Session State Management

The runtime already has the session updated in three places before
`async_update_entry` is called:

| Source | Location | Purpose | Needed? |
|---|---|---|---|
| `on_refreshed` callback | `auth.py:107-110` | Updates `entry.runtime_data.session` | Yes |
| `entry.runtime_data.session = refreshed` | `auth.py:68` | Redundant with callback | No (but harmless) |
| `async_update_entry` to `entry.data` | `auth.py:99` | Restart durability only | Causes reload |

Lines 68 and 107 handle the runtime fully. Line 99 is purely for restart
durability -- and it is the one that triggers the destructive reload.

## Proposed Fix: Use HA Store for Session Persistence

### Approach

Replace session persistence via `async_update_entry` (to `entry.data`) with
persistence via HA's `Store API` (`homeassistant.helpers.storage.Store`).

### Why HA Store instead of removing persistence entirely

- HA Store is explicitly designed for **operational state** that needs restart
  durability but is not user configuration -- the OAuth session fits perfectly
- `entry.data` stores user-facing configuration; the session is runtime token
  state, not a config setting
- Core HA integrations use Store for: cloud credentials, energy config, entity
  registry data
- `Store.async_save()` does not touch `entry.data`, so it does NOT fire the
  update listener -- no reload

### Design

**On setup** (`__init__.py`):

```python
from homeassistant.helpers.storage import Store

session_store = Store[hass, dict[str, Any]](
    hass, STORAGE_VERSION, f"sp_group.session.{entry.entry_id}"
)
session_data = await session_store.async_load()
if session_data is not None:
    session = MobileSession.from_dict(session_data)
else:
    # Fallback: migrate from existing entry.data
    session = mobile_session_from_entry(entry)
```

**On refresh** (`auth.py`):

```python
async def async_refresh_runtime_session_if_needed(hass, entry):
    async with entry.runtime_data.refresh_lock:
        refreshed = await _async_refresh_session_core(
            hass, entry,
            session=entry.runtime_data.session,
            client=entry.runtime_data.auth_client,
            on_refreshed=lambda ns: _update_runtime_session(entry, ns),
        )
        entry.runtime_data.session = refreshed
    # Persist outside the lock -- auth is already valid in memory
    await hass.async_create_task(_async_persist_session(
        hass, entry, refreshed.to_dict()
    ))
    return refreshed
```

The `_async_refresh_session_core` function no longer calls
`async_update_entry`. The `async_update_entry` call in the core is removed.

**Migration from old entries**:

If no Store data exists, fall back to `mobile_session_from_entry(entry)` for
existing config entries that have the session in `entry.data`. The session is
then migrated to the Store on first refresh.

### Effect

| Factor | Before | After |
|---|---|---|
| Auth persists across restart | Yes (`entry.data`) | Yes (HA Store) |
| Options changes reload immediately | No (reload kills poll) | **Yes** (listener stays, no conflict) |
| Session persistence triggers reload | **Yes** (always) | **No** (never touches entry.data) |
| Number of session storage locations | 3 (`entry.data`, `on_refreshed`, `line 68`) | 2 (HA Store, `runtime_data`) |
| Polling interrupted by reload | **Yes** (every hour) | **No** |

### Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Runtime breaks without persistence | None | -- | `entry.runtime_data.session` still updated in memory |
| Restart with stale HA Store | Low | ~250ms refresh during setup | Fallback to `mobile_session_from_entry` |
| HA Store file corruption | Very low | User re-authenticates | `ConfigEntryAuthFailed` handles it |
| Write race during concurrent refreshes | None | -- | Refresh lock; persistence outside lock |

## Files Affected

- `custom_components/sp_group/__init__.py` -- Load session from HA Store on setup
- `custom_components/sp_group/auth.py` -- Remove `async_update_entry`;
  add `async_persist_session` helper
- `tests/components/sp_group/test_auth.py` -- Update tests to assert Store
  persistence instead of `async_update_entry`
- `tests/components/sp_group/test_init.py` -- Add setup/load Store coverage

## Verification

- `mise exec -- python -m pytest tests/components/sp_group/test_auth.py -vv`
- `mise exec -- python -m pytest tests/components/sp_group -q`
- `mise run python:test`
- `mise run python:lint`
- `mise run ha:check`
- Manual smoke: start HA with `mise run ha:dev:cached-session`, confirm
  after 1+ hour the sensor state updates and no reload cycle occurs
