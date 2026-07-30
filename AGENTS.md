# Repository Guidelines — SP Group Home Assistant Integration

## Project Structure & Module Organization

This is a Home Assistant custom integration for SP Group utility data.
Active integration code lives in `custom_components/sp_group/`.
Home Assistant tests live under `tests/components/sp_group/`.
Integration docs live in `docs/`.

The reusable async SDK (`spgroup`) lives in its own repo at
`../../../python-packages/spgroup/`. For SDK development, discussions, or
documentation, switch to that checkout.

## Build, Test, and Development Commands

Use `mise` for the main workflow; this repo pins Python `3.14.2`.

- `mise run python:install` creates/refreshes `.venv/` (Python 3.14.2) and installs the SDK, Home Assistant `2026.7.2`, test/lint tooling, and optional component deps.
- `mise run python:test` runs the HA integration test suite.
- `mise run python:lint` runs Ruff and mypy against HA integration code.
- `mise run ha:check` compile-checks the Python scaffold.
- `mise run ha:dev` / `ha:dev:reset` start a local HA dev instance (HA 2026.7.2, Python 3.14.2) via `.venv/bin/python`. The script auto-detects `.venv/` and runs HA from it.
- `mise run ha:dev:reset` resets `.runtime/ha-config` before starting HA.

Use `python`, not `python3`, when invoking Python directly.

## Coding Style & Naming Conventions

Python code targets Ruff line length `88` and strict mypy. Prefer typed async APIs aligned to HA domain conventions. Name tests `test_*.py`; name platform files by HA conventions (`sensor.py`, `water.py`, `electricity.py`). Keep generated caches, build outputs, and runtime state out of source changes.

## Testing Guidelines and Known Limitations

**pytest-homeassistant-custom-component** is installed with `--no-deps` because the latest release (0.13.316) pins `homeassistant==2026.2.3`, which is behind our target `2026.7.2`. The workaround is safe — the test framework provides fixtures that work with newer HA versions.

Default tests should be hermetic and must not call live SP Group or Auth0 services. Live tests are opt-in via the `spgroup_ha_live_soak` marker; document live credential/session assumptions in the PR.

## Branching and CI workflow

`main` is the release-ready default branch. `development` is the shared
integration branch for work intended for the next release. Create a short-lived
feature or fix branch from `development`, keep commits focused, and open a pull
request back to `development`. Merge only after the required CI checks pass.
Promote `development` to `main` through a reviewed pull request when the release
is ready; do not develop directly on `main`.

GitHub Actions runs `Integration CI` on pushes and pull requests targeting
`development` or `main`. It installs the pinned Python/SDK environment and
runs the hermetic tests, Ruff/mypy, documentation checks, and HA compile check.
`HACS Validation` separately checks the HACS layout and runs daily. Live
provider tests remain opt-in and are not required CI checks.

CI uses the vendored SDK snapshot under
`custom_components/sp_group/vendor/` and sets `PYTHONPATH`/`SPGROUP_SDK_PATH`,
while local development continues using the sibling editable SDK checkout.
This keeps CI self-contained and avoids requiring access to the private SDK
repository. Update the vendored snapshot and its packaging boundary deliberately
when the SDK contract changes.

## Commit & Pull Request Guidelines

Use short imperative commit subjects such as `Add SP Group meter parsing tests`
or conventional prefixes like `docs:`. PRs should summarize scope, list
verification commands, note live-test status, and link relevant docs or issues.
Include screenshots only for HA frontend or UI-visible changes.

### Dev HA instance known repair issues

When starting a local HA dev instance (`mise run ha:dev`), the HA UI shows two repair warnings that are expected for non-Docker development:
- `deprecated_method` — Core installations are deprecated per HA 2025.12 policy. To suppress this, use Docker-based development (devcontainer).

## Contributing

See `CONTRIBUTING.md` for development setup, testing, and SDK dependency
instructions.

## HACS Distribution & Repository Conventions

This repo is set up for HACS distribution as a custom integration. Two docs
define the alignment and conventions:

- `docs/guides/hacs-integration-checklist.md` — HACS alignment requirements
- `docs/guides/repo-conventions.md` — Repo naming, domain, versioning, and GitHub
  conventions

## Agent-Specific Instructions

For implementation, refactoring, planning, or architecture discussion, append discoveries and locked decisions to `docs/context-tracker.md`; do not create a second tracker.

The old `docs/api/` directory has been moved to the SDK repo at
`../../../python-packages/spgroup/docs/api/`. Pre-split SDK context remains in this
repo's `docs/context-tracker.md`.
