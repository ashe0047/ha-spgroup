# ADR: Auth Session Management and HA Store Migration

## Status

Accepted - 2026-07-25

## Authority

Normative for session storage, refresh, authentication failure, and reauth behavior.

## Implementation

`custom_components/sp_group/auth.py`, `__init__.py`, and config-flow/session tests.

## Context

The SP Group integration authenticates with the SP Group mobile API through
OAuth2 tokens managed by Auth0. The mobile SDK provides a `MobileSession`
object containing an access token, refresh token, and identity token. These
tokens have a finite lifetime (~24 hours for access tokens, longer for refresh
tokens).

The integration must persist the session across HA restarts, refresh tokens
proactively before they expire, and handle expired tokens gracefully. The
session is shared across all runtime components (discovery, polling, backfill).

## Design

### Session Storage

The session is stored in two places with different responsibilities:

1. **HA Store** (`sp_group.session.<entry_id>`) — durable persistence layer.
   The session is serialized to HA's JSON storage via `async_persist_session()`
   in `auth.py`. This survives HA restarts and is the source of truth.

2. **Runtime memory** (`entry.runtime_data.session`) — the active session
   object used by the jarvis SDK for API calls. This is loaded from the HA
   Store at startup and refreshed in memory during operation.

```
Startup:
  async_load_session() → reads session from HA Store
  → validates expiry → if expired, refreshes
  → stores in runtime_data.session

Runtime:
  async_refresh_runtime_session_if_needed() → checks expiry
  → if expiring soon, refreshes
  → updates runtime_data.session AND HA Store
```

### Token Refresh Cycle

The `on_refreshed` callback updates the in-memory session automatically when
the SDK refreshes tokens. This callback is registered during session
initialization.

```
async_refresh_runtime_session_if_needed(hass, entry)
  → checks session expiry in runtime_data.session
  → if expires within threshold (~5 min), calls refresh
  → on_refreshed callback fires → updates runtime_data.session
  → async_persist_session() saves to HA Store
```

The refresh is proactive — it fires before the token actually expires, so API
calls never encounter a 401. If a refresh fails, `ConfigEntryAuthFailed` is
raised, triggering HA's reauth flow.

### HA Store Migration

Initially, the session was stored in `entry.data` (config entry data). This
caused a reload cycle: writing to `entry.data` via `async_update_entry()`
would trigger `async_reload_entry()`, tearing down the integration and
restarting it. This was a destructive cycle for in-progress polling and
backfill.

The migration moved session storage to the HA Store API (`Store` class),
which is a side-effect-free key-value store that does not cause reload. The
migration runs once on setup:

```
async_setup_entry(hass, entry)
  → async_load_session(hass, entry)
  → if session found in Store, use it
  → if session not found, check entry.data for legacy session
  → migrate to Store, clear from entry.data
```

This is implemented in `__init__.py` setup and `auth.py`.

## Consequences

### Positive

- Token refreshes no longer cause config entry reloads
- In-progress polls and backfills are not interrupted by a refresh
- HA Store is the standard persistence mechanism for custom integrations

### Negative

- Session data is stored outside the config entry, so it's not visible in
  the HA UI's config entry editor
- HA Store files are not automatically cleaned up on integration uninstall
- Two storage paths to check during session lookup (Store then legacy)

## Related

- `auth.py` — session load, refresh, persist
- `__init__.py` — setup migration
- `config_flow.py` — developer auth flow
- Vendor SDK: `vendor/spgroup/mobile/jarvis.py`, `vendor/spgroup/mobile/transport.py`
