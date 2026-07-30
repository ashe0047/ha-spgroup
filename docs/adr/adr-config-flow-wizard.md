# ADR: Config Flow Wizard Architecture

## Status

Accepted - 2026-07-25

## Authority

Normative for setup, options, seed, retry, and reauthentication flows.

## Implementation

`custom_components/sp_group/config_flow.py`, strings/translations, and config-flow tests.

## Context

The SP Group config flow (`config_flow.py`, 1627 lines) guides the user
through integration setup. It must handle authentication with SP Group's
Auth0-based API, discover accounts and premises, configure sensors with seed
values, and support ongoing options like retries and reseeding.

The flow has two entry points: initial setup (adding the integration) and
options (modifying an existing configuration).

## Design

### Step Sequence (Initial Setup)

```
user (auth selection)
  │
  ├── developer_auth (optional dev shortcut)
  │     └── dev_session_confirm
  │
  └── password_auth (standard login)
        │
        └── otp (multi-factor if required)
              │
              └── reauth (if token expires during setup)

After auth → sources (select which utilities to enable)
           │
           └── seed (enter meter reading for each utility)
                 │
                 └── approximate_seed_confirm (if time not on boundary)
```

### Step Sequence (Options Flow)

```
options (maintenance menu)
  │
  ├── retry_source_backfill → triggers async backfill, no form
  ├── retry_statistics_backfill → triggers stats import, no form
  ├── reseed:seed → allows re-entering seed value for a utility
  └── rebuild:seed → resets ledger to stored seed config
```

### Auth Steps

1. **`async_step_user`** — presents two auth methods:
   - Developer auth (uses a pre-configured cached session file)
   - Standard password login

2. **`async_step_developer_auth`** — loads a cached session from
   `.runtime/mobile-session.json` (dev-only, not available in production).

3. **`async_step_password_login`** — calls the Auth0 password grant with
   the username and password. Returns MFA challenge if required.

4. **`async_step_otp`** — handles MFA one-time password challenge.
   Re-polls the MFA endpoint if the code is invalid.

5. **`async_step_reauth`** — triggered by `ConfigEntryAuthFailed` during
   runtime. Updates the existing entry's auth data without recreating it.

### Discovery Step

`async_step_sources` calls the Yggdrasil and Jarvis APIs to discover
accounts, premises, and utility types. The results are normalized into
`SpGroupFutureSensorKey` objects. Users select which discovered utilities
to enable. If no utilities are discoverable, the flow aborts with
`no_sources_found`.

### Seed Step

`async_step_seed` collects:
- **Seed value** — the current meter reading (Decimal)
- **Seed observed at** — when the reading was taken (datetime, must be on
  a 30-min boundary for electric or month boundary for water)
- **Backfill from** — how far back to request provider history (date)

On submission, `build_source_config()` validates the inputs. If the seed
time is not on a clean boundary, `approximate_seed_confirm` step is shown.
The seed is applied per-utility; if multiple utilities are selected, the
seed step repeats for each.

### Options Flow

`async_step_options` provides maintenance actions:
- `retry_source_backfill` — re-runs the full source backfill for failed/
  never-attempted sources
- `retry_statistics_backfill` — re-imports statistics from existing ledger
- `reseed:<key>` — enter a new seed (e.g., after meter replacement)
- `rebuild:<key>` — reset the ledger entry to its stored seed config

Options changes that modify source configs (reseed, rebuild) are applied
via `async_update_entry()` which triggers a reload.

### Validation

Input validation is split between the config flow layer and the
`build_source_config()` function in `sources.py`:

- Config flow validates form-level constraints (required fields, number
  format, timestamp parsing)
- `sources.py` validates semantic constraints (seed not in future, backfill
  not after seed, seed not too old, seed on 30-min boundary)

## Consequences

### Positive

- Auth, discovery, and seeding are cleanly separated into steps
- Developer auth shortcut speeds up dev workflow
- MFA is handled transparently
- Options flow doesn't require full re-setup

### Negative

- The config flow file is large (1627 lines) and contains complex branching
- Error messages are split between strings.json, translations/en.json, and
  inline placeholders
- Options flow maintenance actions are fire-and-forget (no progress feedback)

## Related

- `config_flow.py` — all step handlers
- `strings.json`, `translations/en.json` — user-facing text
- `sources.py` — `build_source_config()`, `_validate_seed_timestamp()`
- `auth.py` — session management
- `discovery.py` — API discovery logic
