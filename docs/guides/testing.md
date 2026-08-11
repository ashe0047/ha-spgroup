# Testing Guidelines

This is a Home Assistant custom integration that consumes the spgroup SDK.

- `custom_components/sp_group`: the Home Assistant custom integration that consumes the SDK.

The testing strategy should keep those surfaces separate while still proving that
the integration works through Home Assistant's own setup, config-entry, entity,
and state-machine paths.

## Documentation Grounding

Use these Home Assistant references as the baseline for testing decisions:

- Home Assistant developer testing docs: run related pytest tests locally, and
  for an integration use `pytest ./tests/components/<domain>/ --cov=...` when
  coverage matters.
- Home Assistant integration test file structure: tests for an integration live
  under `tests/components/<domain>`, with shared fixtures in `conftest.py` and
  file names matching the integration area, such as `test_init.py`,
  `test_config_flow.py`, and `test_sensor.py`.
- Home Assistant integration testing guidance: test through public/core HA
  interfaces where possible, such as `hass.config_entries.async_setup`,
  `hass.config_entries.flow`, `hass.states`, entity registry, device registry,
  and service calls.
- Home Assistant config-flow docs: integrations with a config flow need full
  test coverage for `config_flow.py`.
- `pytest-homeassistant-custom-component`: custom integrations should use this
  package to access HA-style pytest fixtures outside the Home Assistant Core
  repository. Tests must use the `enable_custom_integrations` fixture, and
  pytest should run with `asyncio_mode = auto`.

  **Note**: The package is installed with `--no-deps` because the latest
  release (0.13.316) pins `homeassistant==2026.2.3`, which is behind our
  target `2026.7.2`. The workaround is safe — the test framework provides
  fixtures that work with newer HA versions.

## Current Harness Checks

The maintained harness exercises config flow, coordinators, sensors, ledgers,
statistics, Recorder reconciliation, and provider-failure availability
through Home Assistant fixtures. The focused SP Group suite is the primary
verification gate.

Run them with:

```bash
mise run python:test
mise run python:lint
mise run ha:check
```

These checks are the repository's current scaffold and integration gate. The
HA custom-component harness is already established; do not treat it as future
work or add a second test loader. New tests should extend the existing fixtures
and keep provider calls hermetic by default.

## Home Assistant Integration Tests

HA integration tests belong under `tests/components/sp_group`.

The minimum test set should be:

- `test_config_flow.py`
  - Initial user step returns a form.
  - Username/password submission starts the OTP step.
  - OTP submission creates a config entry.
  - Invalid credentials and invalid OTP return form errors.
  - Duplicate account setup aborts using a stable unique ID.
  - Reauth starts and completes without persisting password or OTP.
- `test_init.py`
  - `MockConfigEntry` can be added and set up through
    `hass.config_entries.async_setup`.
  - Runtime data is created on the config entry.
  - Sensor platform forwarding is requested.
  - Unload removes runtime state and unloads platforms cleanly.
  - Auth failures map to Home Assistant reauth behavior.
  - Utility transport/API failures are successful no-op polls that preserve
    the last trusted value; discovery/authentication failures retain their
    setup/reauth paths, and malformed/data-integrity failures map to
    `UpdateFailed`.
- `test_sensor.py`
  - Sensors are created through the HA setup path.
  - State is asserted through `hass.states`.
  - Entity unique IDs and translation keys are stable.
  - Placeholder sensors remain non-statistical until real SP data semantics are
    proven.
- `test_diagnostics.py`
  - Diagnostics are available.
  - Tokens, passwords, OTP values, and sensitive address/account identifiers are
    redacted.

Prefer assertions through Home Assistant state, config-entry state, entity
registry, and device registry. Directly instantiating integration classes should
be reserved for narrow pure-unit behavior.

## Energy Dashboard Tests

Energy Dashboard compatibility must be tested only after the SP payload semantics
are proven by SDK fixtures or live captures.

Do not mark a placeholder sensor as Energy Dashboard-ready.

When the electricity implementation is ready, add tests proving:

- The electricity grid-consumption entity uses
  `SensorDeviceClass.ENERGY`.
- The unit is Home Assistant's kWh unit, not a visually similar custom string.
- The state class is intentionally chosen:
  - `SensorStateClass.TOTAL` for controlled total values that may reset or
    decrease with known semantics.
  - `SensorStateClass.TOTAL_INCREASING` only when the source or derived value is
    monotonic except for true meter resets.
- Monthly, billing-period, or chart-delta data is not exposed directly as a
  long-term-statistics sensor unless duplicate periods and reset behavior are
  explicitly handled.
- The sensor value is numeric, stable across repeated polls, and does not
  double count already-seen chart periods.

These tests should protect Home Assistant long-term statistics from bad source
semantics. A wrong energy state class can produce misleading or corrupted
statistics, so the default should be conservative.

## Fixtures And Redaction

Captured SP API payloads should be stored as redacted fixtures.

Fixture rules:

- Remove or replace tokens, IDs, names, addresses, phone numbers, and emails.
- Preserve payload shape, field names, date formats, utility types, and units.
- Keep one fixture per endpoint and scenario.
- Include edge cases for missing water/electricity data, empty chart responses,
  API errors, and reset/billing-boundary behavior.

## Verification Gates

For scaffold work:

```bash
mise run python:install
mise run python:test
mise run python:lint
mise run ha:check
```

For integration behavior after the HA harness is added:

```bash
python -m pytest tests/components/sp_group -vv
python -m pytest tests/components/sp_group --cov=custom_components.sp_group --cov-report term-missing -vv
```


Use `python`, not `python3`, on this machine.

## Locked Direction

The Home Assistant test harness is established. New behavior should be tested
through public HA setup, entity, Recorder, and statistics interfaces where
possible, with pure tests reserved for deterministic parser/composer and
reconciliation contracts.
