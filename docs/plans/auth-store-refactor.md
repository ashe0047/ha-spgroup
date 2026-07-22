# HA Store Session Persistence Refactor Plan

## Summary

Replace session persistence via `async_update_entry` (to `entry.data`) with
persistence via HA's `Store` API. The `add_update_listener` stays in place
for options changes. Auth refresh no longer touches `entry.data`, so it never
triggers a reload.

The in-memory `entry.runtime_data.session` continues to serve as the runtime
cache. The HA Store provides restart durability. `entry.data[CONF_SESSION]`
becomes a dormant migration fallback that is read on first setup after upgrade
but never written afterward.

## Key Changes

### 1. `const.py`

Add a storage version constant:

```python
STORAGE_VERSION = 1
```

### 2. `__init__.py`

- Import `Store` from `homeassistant.helpers.storage`
- In `async_setup_entry`, after loading the ledger, create a session Store:

```python
from homeassistant.helpers.storage import Store

session_store: Store[dict[str, Any]] = Store(
    hass, STORAGE_VERSION, f"sp_group.session.{entry.entry_id}"
)
```

- Load session from Store; fall back to `entry.data` for migration:

```python
session_data = await session_store.async_load()
if session_data is not None:
    session = MobileSession.from_dict(session_data)
else:
    # Migration: existing entry still has session in entry.data
    session = mobile_session_from_entry(entry)
```

- Add `session_store` to `SpGroupRuntimeData`:

```python
@dataclass(slots=True)
class SpGroupRuntimeData:
    ...
    session_store: Store[dict[str, Any]] | None = None
```

- Pass the Store instance to runtime data:

```python
entry.runtime_data = SpGroupRuntimeData(
    ...,
    session_store=session_store,
)
```

- Remove `CONF_SESSION` from `mobile_session_from_entry` calls (or keep it
  as a fallback).

### 3. `auth.py`

- Remove `CONF_SESSION` and `CONF_ACCOUNT_ID` imports (no longer needed for
  `async_update_entry` data dict)
- Remove the `async_update_entry` call from `_async_refresh_session_core`
- Add an `async_persist_session` helper:

```python
async def async_persist_session(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
    session: MobileSession,
) -> None:
    """Persist a refreshed session to the HA Store."""
    store = getattr(entry.runtime_data, "session_store", None)
    if store is not None:
        await store.async_save(session.to_dict())
```

- Update `async_refresh_runtime_session_if_needed` to persist after refresh:

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
    # Persist outside the lock
    await async_persist_session(hass, entry, refreshed)
    return refreshed
```

- `_async_refresh_session_core` no longer calls `async_update_entry`.
  The `account_id` update line remains (via entry.data.get):
  ```python
  account_id = entry.data.get(CONF_ACCOUNT_ID)  # keep reading account_id from entry.data
  if refreshed.identity_metadata is not None:
      account_id = refreshed.identity_metadata.subject
  ```
  But `CONF_SESSION` and `async_update_entry` are removed.

- `async_refresh_session_if_needed` (external path, not used in production):
  Keep the `async_update_entry` call for backward compatibility of this
  public helper. It needs to be refactored separately if needed.

### 4. `config_flow.py`

No changes. `CONF_SESSION` continues to be stored in `entry.data` during
config flow creation. This is harmless — the setup code reads from the Store
first and falls back to `entry.data` for migration.

### 5. `coordinator.py`

No changes needed. Coordinators call `async_refresh_runtime_session_if_needed`
which handles Store persistence internally.

## Test Changes

### `test_auth.py`

- The `make_entry` helper still stores `CONF_SESSION` in entry.data (tests
  existing entries). No change needed.

- The `attach_runtime` helper must be updated to include a `session_store`
  field (set to `None` or a mock). Since `session_store` is optional
  (`Store | None`), setting it to `None` is sufficient for tests that
  don't test persistence.

- **`test_refresh_helper_persists_refreshed_session`**: Keep as-is — this
  tests `async_refresh_session_if_needed` (external path) which still uses
  `async_update_entry`.

- **`test_runtime_refresh_persists_and_updates_runtime_session`**: Update:
  - Remove `hass.config_entries.async_update_entry` mock and assertions
  - Add assertion that `entry.runtime_data.session == refreshed`
  - Runtime session update and account_id are verified — persistence to
    Store is tested in a separate test

- **`test_runtime_refresh_coalesces_concurrent_calls`**: Update:
  - Remove `async_update_entry.assert_called_once()` assertion
  - Keep the rest (coalescing behavior is unchanged)

- Add a new test **`test_runtime_refresh_persists_store`**:
  - Mock `async_persist_session` or verify Store write behavior
  - Assert that persistence is called once per refresh with the correct data

### `test_init.py`

- Add setup test that verifies session Store is created and loaded correctly
- Add migration test: entry with `CONF_SESSION` in data but no Store file
  → falls back to `mobile_session_from_entry`
- Add migration test: entry with both Store data and `entry.data` session
  → prefers Store data

## Verification

```bash
mise exec -- python -m pytest tests/components/sp_group/test_auth.py -vv
mise exec -- python -m pytest tests/components/sp_group/test_init.py -vv
mise exec -- python -m pytest tests/components/sp_group -q
mise run python:test
mise run python:lint
mise run ha:check
```

## Manual Smoke Test

1. Start HA: `mise run ha:dev:cached-session`
2. Complete config flow
3. Confirm sensors are created and display seed values
4. Wait 1+ hour (or manually trigger electric poll)
5. Confirm no reload cycle occurs:
   - No recurring `SP Group unload started` / `SP Group setup started` pattern
   - Sensor state updates beyond seed value
   - Energy Dashboard shows post-seed data
6. Restart HA and confirm session is still valid (no re-auth prompt)

## Edge Cases

| Case | Behavior |
|---|---|
| First run after upgrade (entry.data has session) | Session migrated to Store on first setup |
| Fresh config flow (entry.data has new session) | Entry.data written as before; Store populated on first refresh |
| Reauth | Entry.data updated; Store will be used on next setup |
| Entry removed and recreated | New Store created; old Store file is orphaned (harmless) |
| Store file corrupted | Falls back to entry.data; refresh populates Store with fresh session |
| Concurrent refreshes | Lock protects in-memory update; Store write happens outside lock |
