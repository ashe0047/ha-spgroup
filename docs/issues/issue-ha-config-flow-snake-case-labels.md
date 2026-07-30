# HA Config Flow Snake Case Labels — Closed

## Status

Closed on 2026-07-15.

## Authority

Historical incident record; config-flow behavior is governed by `adr/adr-config-flow-wizard.md`.

## Observation

During Home Assistant frontend setup/options flows, some field labels appeared
as raw snake_case keys such as:

```text
seed_value
seed_observed_at
confirm_stale_flow_reset
maintenance_action
```

This was not acceptable UX because these are internal schema keys, not
user-facing labels.

## Root Cause

The integration only provided:

```text
custom_components/sp_group/strings.json
```

and did not provide the custom integration runtime translation file:

```text
custom_components/sp_group/translations/en.json
```

Home Assistant custom integrations use the `translations/<language>.json`
directory for runtime frontend translations. `strings.json` and placeholder
syntax such as:

```text
[%key:common::config_flow::data::username%]
```

are Home Assistant Core build-time conventions and are not reliable runtime
translation sources for this custom component. As a result, the frontend could
fall back to raw schema keys.

## Fix

Implemented in:

- `custom_components/sp_group/translations/en.json`
- `custom_components/sp_group/strings.json`
- `tests/components/sp_group/test_translations.py`
- `docs/testing/ha-runtime-validation.md`
- `docs/context-tracker.md`

The fix adds a custom integration translation file with literal English copy
for:

- config-flow steps, field labels, field descriptions, errors, and aborts;
- options-flow steps, field labels, field descriptions, errors, and aborts;
- sensor entity translation keys;
- repair issue and repair flow copy.

The legacy `strings.json` file now mirrors `translations/en.json` exactly to
avoid developer confusion, but runtime frontend behavior should be validated
against `translations/en.json`.

All Core-only placeholder references were removed from the custom integration
translation payload.

## Regression Coverage

Added tests for:

- `custom_components/sp_group/translations/en.json` existing and being valid
  JSON;
- absence of `[%key:...%]` placeholder syntax;
- config-flow field translation coverage for setup, OTP, sources, stale-flow
  reset, no-source confirmation, seed, and approximate seed confirmation;
- options-flow field translation coverage for source selection, maintenance,
  reseed, approximate confirmation, reset, and rebuild;
- error and abort reason translation coverage;
- entity translation key coverage;
- repair issue translation coverage;
- `strings.json` matching `translations/en.json`.

## Verification

Hermetic checks:

```bash
mise exec -- python -m pytest tests/components/sp_group/test_translations.py -vv
mise exec -- python -m pytest tests/components/sp_group/test_config_flow.py -vv
mise exec -- python -m pytest tests/components/sp_group -q
mise run python:test
mise run python:lint
mise run ha:check
```

Results at closure:

- `5 passed` for `test_translations.py`;
- `26 passed` for `test_config_flow.py`;
- `152 passed, 1 skipped` for `tests/components/sp_group`;
- `217 passed, 10 skipped` for `mise run python:test`;
- lint and type checks passed;
- HA compile/check passed.

## Operational Notes

After translation changes, restart the local Home Assistant dev server and
hard-refresh the browser before judging frontend labels. Home Assistant and the
frontend may cache translation payloads, so a stale browser session can make a
fixed translation look broken.
