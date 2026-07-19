# Contributing

## Development Setup

This repo pins Python `3.14.2` via `mise.toml`. All development commands use
`mise` — no manual venv management needed.

```bash
# Install dependencies (creates .venv/, installs SDK + HA + tooling)
mise run python:install

# Run tests
mise run python:test

# Run lint and type checks
mise run python:lint

# Lightweight HA scaffold compile check
mise run ha:check
```

### SDK Dependency

The integration depends on the [spgroup SDK](https://github.com/ashe0047/spgroup),
which is installed as an editable pip package from a sibling checkout:

```bash
python -m pip install -e ../../../python-packages/spgroup
```

`mise run python:install` runs this command automatically along with test and
lint tooling.

The repo also includes a vendored SDK fallback at
`custom_components/sp_group/vendor/spgroup/` for environments where the SDK
is not pip-installed. This is a frozen snapshot — SDK updates require
re-vendoring.

### Running a Local HA Instance

```bash
# Start HA with SP Group integration loaded
mise run ha:dev

# Reset config and start fresh
mise run ha:dev:reset
```

---

## Project Structure

```
custom_components/sp_group/   ← Integration code
  __init__.py                 ← Entry point, async_setup_entry
  config_flow.py              ← Setup wizard (username → OTP → discovery → seed)
  coordinator.py              ← Data polling coordinator
  sensor.py                   ← Sensor platform (electricity, water)
  electricity.py              ← Electricity AMI bucket processing
  water.py                    ← Water monthly bucket processing
  ledger.py                   ← Accumulation ledger (persistent state)
  backfill.py                 ← Historical data backfill
  statistics.py               ← HA recorder statistics integration
  auth.py                     ← Auth0 OTP authentication flow
  diagnostics.py              ← HA diagnostics support
  discovery.py                ← Utility account discovery
  vendor/spgroup/             ← Vendored SDK (frozen fallback)
  brand/                      ← Brand assets (logo, icon)
tests/                        ← Test suite
docs/                         ← Documentation
```

---

## Testing

```bash
# All tests (hermetic, no live calls)
mise run python:test

# Opt-in live soak test (requires valid SP Group credentials)
mise run python:test:ha-live-soak
```

Default tests are hermetic and use mock API responses. Live tests require the
`spgroup_ha_live_soak` marker and are opt-in.

---

## Coding Style

- Python: Ruff (line length 88), strict mypy
- Tests: pytest with async support, `pytest-homeassistant-custom-component`
- Prefer typed async APIs aligned to Home Assistant domain conventions
- Test files: `test_*.py` naming
- Platform files: follow HA naming (`sensor.py`, `electricity.py`, `water.py`)

---

## Pull Requests

1. Ensure tests and lint pass
2. Update `manifest.json` `version` if the change affects runtime behavior
3. Update docs if the change affects the user-facing integration
4. Summarize what was verified in the PR description

---

## HACS Distribution

Before committing changes that affect the repository structure, review:

- `docs/hacs-integration-checklist.md` — HACS alignment requirements
- `docs/repo-conventions.md` — Repo naming, domain, versioning conventions

---

## License

This project is MIT licensed. See [LICENSE](LICENSE).
