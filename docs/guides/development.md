# Development Guidelines

This document is the local development operator guide for the SP Group SDK and
Home Assistant custom integration in this repository.

## Environment Setup

Use `mise` from the repository root. The repo pins Python through
`mise.toml`:

```toml
[tools]
python = "3.14.2"
```

Install the editable SDK package and local test/runtime dependencies:

```bash
mise run python:install
```

This creates or refreshes `.venv/` and installs:

- the SDK from `../../../python-packages/spgroup/` in editable mode;
- Home Assistant `2026.7.2`, `home-assistant-frontend`, and all needed optional
  component dependencies (`ha-ffmpeg`, `hassil`, `PyTurboJPEG`, `mutagen`,
  `home-assistant-intents`, `pymicro-vad`, `pyspeex-noise`, `aiohasupervisor`,
  `rf-protocols`, `infrared-protocols`);
- the Home Assistant custom-component pytest harness (via `--no-deps` — see
  [Testing Guidelines](#testing-guidelines-and-known-limitations));
- lint/type/test tooling (`ruff`, `mypy`, `pytest`);
- optional runtime packages needed by the local HA frontend config.

Use `python`, not `python3`, for direct commands in this repo.

## Branching and continuous integration

Use `main` only for release-ready changes. Develop on `development`, or create
a short-lived feature/fix branch from `development` and open a pull request
back to it. Keep the branch focused, rebase or merge the current
`development` branch before handoff, and merge only after required checks pass.
Promote `development` to `main` through a reviewed pull request; releases and
HACS consumption are based on `main`.

GitHub Actions runs the same gates on pushes and pull requests for both shared
branches. `Integration CI` installs Python, the public `spgroup` SDK, Home
Assistant, and test tooling, then runs:

```bash
mise run python:test
mise run python:lint
mise run docs:check
mise run ha:check
```

`HACS Validation` separately checks HACS integration metadata and runs daily.
The workflow is hermetic: it does not use live SP Group credentials or provider
endpoints. Live soak tests remain an explicit local operation:

```bash
mise run python:test:ha-live-soak
```

Local development keeps using the sibling SDK checkout. CI uses the vendored
SDK snapshot under `custom_components/sp_group/vendor/`, sets `PYTHONPATH` and
`SPGROUP_SDK_PATH`, and runs the same `mise run python:install` task. This keeps
the workflow self-contained because the SDK repository is private. Update the
vendored snapshot deliberately when changing the integration's SDK contract.

## Repository Areas

- `custom_components/sp_group`: Home Assistant custom integration.
- `tests/components/sp_group`: HA integration tests using
  `pytest-homeassistant-custom-component`.
- `.runtime/`: ignored local runtime state, session caches, HA config, and live
  artifacts.

Do not commit `.runtime/` contents.

## Standard Verification Commands

Run the full hermetic suite:

```bash
mise run python:test
```

Run lint and type checks:

```bash
mise run python:lint
```

Run lightweight HA compile/scaffold checks:

```bash
mise run ha:check
```

Focused useful commands:

```bash
mise exec -- python -m pytest tests/components/sp_group/test_config_flow.py -vv
mise exec -- python -m pytest tests/components/sp_group/test_translations.py -vv
mise exec -- python -m pytest tests/components/sp_group -q
```

Default tests are hermetic. They must not call production SP Group endpoints.

## Local Home Assistant Frontend

Start a local HA frontend with this integration symlinked into the config
directory:

```bash
mise run ha:dev
```

The dev script:

- uses `.runtime/ha-config` as the Home Assistant config directory;
- creates `.runtime/ha-config/custom_components/sp_group` as a symlink to the
  repo integration;
- writes a minimal `configuration.yaml`;
- enables `config`, `frontend`, `http`, `api`, `onboarding`, `recorder`,
  `history`, and `energy`;
- starts HA on `http://localhost:8123` unless that port is occupied;
- if `8123` is occupied, tries the next available port up to `8142`;
- cleans up the HA process group on Ctrl-C and terminal stop signals.

Set a preferred port if needed:

```bash
HA_DEV_PORT=8130 mise run ha:dev
```

Reset the HA dev config directory and start fresh:

```bash
mise run ha:dev:reset
```

Use reset carefully. It deletes `.runtime/ha-config`, including onboarding,
config entries, entity registry, recorder DB, integration options, and SP Group
ledger storage for that local HA instance.

Use plain `ha:dev` when you want to keep a completed config entry and avoid
going through setup again.

## Historical Statistics Backfill In Dev

After a seeded setup completes, SP Group runs source-history backfill once and
stores accepted buckets in the local ledger. Historical long-term statistics are
then imported automatically after Home Assistant Recorder fires its hourly
statistics event and the integration can validate HA's native reference row.

Expected flow:

```text
config flow complete -> source-history backfill -> HA hourly Recorder stats
event -> automatic SP Group statistics import -> Energy Dashboard history
```

The options action `Retry statistics import` remains available as a fallback,
but it uses the same statistics-only scheduler and does not re-fetch SP Group
provider history. Do not expect HA restarts to rerun full historical source
backfill; use a disposable reset or explicit maintenance action only when
intentionally rebuilding test data.

For fast full-flow validation in a disposable dev instance, force the local HA
Recorder statistics pass and SP Group statistics import:

```bash
mise run ha:dev:force-statistics
```

Use this after completing the SP Group config flow. The command calls a
developer-only service that exists only when HA is started through `ha:dev` or
`ha:dev:cached-session`. It waits for any in-flight one-shot source-history
backfill, then queues HA Recorder statistics tasks. The resulting Recorder
hourly statistics event is handled by SP Group's normal event listener, which
then starts the statistics-only import from persisted ledger data. The command
itself does not call SP Group provider history endpoints and does not directly
call SP Group's import scheduler. After calling the service, the CLI waits
briefly for the listener-driven import metadata to settle before printing
Recorder row counts and ledger status.

Recommended fast reset loop:

```bash
mise run ha:dev:cached-session:reset
# complete HA onboarding and the SP Group setup flow in the browser
mise run ha:dev:force-statistics
```

This helper intentionally accelerates Recorder timing for local verification.
Do not use it as evidence of production scheduling behavior; production waits
for Home Assistant's normal Recorder hourly statistics event.

## Brand Images (HA 2026.3+)

The SP Group brand logo and icon are shipped as PNG files in
`custom_components/sp_group/brand/`. HA auto-discovers this directory
(HA 2026.3+) and serves them via `GET /api/brands/integration/sp_group/{image}`.

| File | Size | Content |
|---|---|---|
| `icon.png` | 256×256 | Circular SP mark, cropped square |
| `icon@2x.png` | 512×512 | HiDPI icon |
| `logo.png` | 512×176 | Full logo with wordmark |
| `logo@2x.png` | 1024×352 | HiDPI logo |

No `manifest.json` changes are needed.

## Home Assistant Frontend Cache Notes

After changing config-flow UI, translations, entity names, or frontend-facing
strings:

1. Stop and restart `mise run ha:dev`.
2. Hard-refresh the browser.

Home Assistant and the frontend can cache translation payloads. A stale browser
session may keep showing old labels even after the backend files are fixed.

## Production Auth During Development

The normal Home Assistant config flow uses the real SP Group mobile auth flow:

```text
username/password -> OTP -> mobile session -> discovery -> source setup
```

Repeated username/password and OTP attempts can look suspicious to the API
provider and may trigger captcha or account-protection flows. Avoid repeating
real auth unless you are intentionally testing auth behavior.

If a config entry already exists in `.runtime/ha-config`, use `mise run ha:dev`
instead of `ha:dev:reset`.

If you remove the SP Group integration in the HA UI, or reset the HA config
directory, starting setup again normally requires real auth. Use the dev-only
cached-session command below when you need to repeatedly test the post-auth
setup wizard without triggering more Auth0/MFA attempts.

## Known Repair Issues (Expected in Dev)

The local HA dev instance reports two repair warnings that are inherent to
non-Docker development:

### `deprecated_method`

- **What**: "This system is using the Core installation type, which has been
  unsupported since Home Assistant 2025.12."
- **Why**: Running HA directly via Python (even from a venv) is detected as
  "Home Assistant Core" installation type. HA deprecated this in 2025.12 in
  favor of Container/Docker setups.
- **Affects**: All venv-based HA development, including HA's own
  `script/setup` + `hass -c config` workflow.
- **Fix**: Use Docker-based development (devcontainer).

### `country_not_configured`

- **What**: No country is configured.
- **Why**: The minimal dev config doesn't set a country. Normal for first-run.
- **Fix**: Set the country in the HA UI during onboarding.

## Dev Auth Bypass For Config Flow Testing

Status: **implemented for local development only**.

The desired local flow is:

```text
cached mobile session -> config-flow post-auth continuation -> discovery
  -> utility selection -> seed entry -> confirmations -> config entry creation
```

The purpose is to test post-auth setup stages repeatedly without hitting
Auth0/MFA every time.

Recommended command:

```bash
mise run ha:dev:cached-session
```

For a clean local HA config directory with the same bypass enabled:

```bash
mise run ha:dev:cached-session:reset
```

These commands set:

```text
SPGROUP_HA_DEV_AUTH_BYPASS=1
SPGROUP_HA_DEV_SESSION_FILE=.runtime/mobile-session.json
```

Refresh the cached mobile session without starting HA:

```bash
mise run ha:dev:refresh-session
```

This uses the refresh token already stored in `.runtime/mobile-session.json`,
writes the refreshed cache atomically with owner-only permissions, and prints
only sanitized token metadata. It is useful before starting
`ha:dev:cached-session` when the access token may have expired but you do not
want to enter username, password, or OTP again.

When enabled, the setup flow shows a developer-only authentication choice:

```text
Use cached developer session
Sign in with SP Group credentials
```

Selecting the cached-session path:

1. Read the configured session file.
2. Deserialize it into the SDK `MobileSession`.
3. Validate identity metadata.
4. Refresh the session with the SDK refresh-token path if expired.
5. Write refreshed tokens back to the cache with owner-only file permissions.
6. Continue through the same post-auth config-flow path as real auth.

Guardrails:

- disabled unless `SPGROUP_HA_DEV_AUTH_BYPASS=1`;
- default session file `.runtime/mobile-session.json`;
- never ask for, store, print, or log username/password/OTP;
- never log tokens or account identifiers;
- fail clearly if the cached session is missing, malformed, lacks identity
  metadata, lacks a refresh token, or cannot refresh;
- do not silently mutate HA storage;
- keep this as a local config-flow testing tool, not a production feature.

Do not pre-create `.storage/core.config_entries` for setup-flow testing. That
can be useful for bootstrapping an already configured integration, but it skips
too much of the setup wizard. The post-auth cached-session seam is better for
testing discovery, source selection, seed entry, confirmations, and final config
entry creation.

## Home Assistant Live Soak Workflow

The HA live soak validates the integration runtime with a cached live mobile
session. It is opt-in and read-only.

Run only after hermetic checks pass:

```bash
mise run python:test:ha-live-soak
```

Optional environment:

| Variable | Purpose |
| --- | --- |
| `SP_MOBILE_SESSION_FILE` | Session cache path, default `.runtime/mobile-session.json`. |
| `SPGROUP_HA_LIVE_SOAK_CYCLES` | Number of coordinator refresh cycles, default `2`. |

The live soak:

- uses the cached mobile session;
- sets up the HA integration runtime with live SDK clients;
- performs serial read-only coordinator refreshes;
- validates discovery, chart polling, entity creation, diagnostics, and
  sanitized runtime summaries;
- does not seed utilities or duplicate the full SDK endpoint matrix.

See `docs/testing/ha-runtime-validation.md` for manual HA UI and runtime
validation steps.

## Manual HA Runtime Walkthrough

Use `mise run ha:dev` or `mise run ha:dev:reset` depending on whether you want
to preserve existing local HA state.

Validate:

1. Add the SP Group integration from the HA UI.
2. Complete auth only when intentionally testing auth.
3. Confirm labels render as human text, not snake_case schema keys.
4. Confirm discovered utility labels are user-friendly.
5. Select utilities through the multi-select.
6. Enter seed value and observed-at timestamp.
7. Confirm approximate seed handling for non-boundary timestamps.
8. Confirm no-source setup requires explicit confirmation.
9. Confirm seeded sensors become available and dashboard eligible.
10. Open options and test enable, reseed, lower reset/replacement, and ledger
    rebuild flows.
11. Download diagnostics and scan for secret/raw identifier leaks.

For translation or frontend copy changes, restart HA and hard-refresh the
browser before judging the UI.

## Runtime Artifacts And Safety

Important `.runtime/` paths:

| Path | Purpose |
| --- | --- |
| `.runtime/mobile-session.json` | Cached SDK live mobile session with tokens. |
| `.runtime/ha-config` | Local Home Assistant dev config directory. |
| `.runtime/ha-config/.storage/core.config_entries` | Local HA config entries. |
| `.runtime/ha-config/.storage/sp_group_*_ledger` | Local SP Group ledger storage. |
| `.runtime/live-discovery-debug.json` | Optional local debug artifact; do not commit. |

Treat all `.runtime/` files as sensitive. They may include tokens, account
state, live payload fragments, local HA auth state, or derived utility data.

Do not paste raw live payloads, account numbers, meter IDs, premise IDs,
addresses, names, emails, phone numbers, or tokens into docs, tests, logs, or
issue reports. Use sanitized summaries only.

## Troubleshooting

### HA dev starts on an unexpected port

`scripts/ha_dev.py` selects the first free port from the preferred port through
the next 19 ports. Set `HA_DEV_PORT` to change the preferred port.

### HA appears to keep old labels

Restart `ha:dev` and hard-refresh the browser. Translation and frontend caches
can survive integration reloads.

### `ha:dev` leaves a port occupied

The dev script is designed to stop the HA process group on Ctrl-C and terminal
stop signals. If a stale process remains, stop it before restarting the dev
server. Prefer normal Ctrl-C shutdown over closing the terminal abruptly.

### Live auth starts requiring captcha

Stop repeated auth attempts. Use the cached `.runtime/mobile-session.json` for
SDK live tests and, once implemented, the dev auth bypass for HA config-flow
testing. Refresh the session through the refresh-token path instead of
password/OTP when possible.

### Live tests fail with optional 401/403 responses

Optional account surfaces may be unavailable for a particular account/session.
Documented optional 401/403 responses are expected diagnostic outcomes, not
necessarily SDK request-shape failures.
