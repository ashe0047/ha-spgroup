# Home Assistant Energy Dashboard Direction

This reference captures the implementation direction for turning SP Group mobile
utility data into Home Assistant entities that can eventually feed the Energy
Dashboard.

It is intentionally conservative. Incorrect energy metadata can pollute Home
Assistant long-term statistics, so Energy Dashboard compatibility should only be
enabled after the SP payload semantics are proven.

## Source Documentation

Use these Home Assistant docs as the governing references:

- Home Assistant electricity grid guide:
  `https://www.home-assistant.io/docs/energy/electricity-grid/`
- Home Assistant sensor entity docs:
  `https://developers.home-assistant.io/docs/core/entity/sensor/`
- Home Assistant integration testing docs:
  `https://developers.home-assistant.io/docs/development_testing/`
- Project testing guideline:
  `docs/testing-guidelines.md`

## Current Scaffold Status

The current `sp_group` sensor scaffold is not Energy Dashboard-ready yet.

Current placeholder behavior:

- Electricity sensor declares `native_unit_of_measurement="kWh"`.
- Water sensor declares `native_unit_of_measurement="m³"`.
- Both sensors return `None`.
- The electricity sensor does not yet declare
  `SensorDeviceClass.ENERGY`.
- The electricity sensor does not yet declare a long-term statistics
  `state_class`.

This is the right scaffold posture. The integration should not expose Energy
Dashboard metadata until the SDK proves whether SP returns cumulative readings,
period deltas, chart buckets, or another data shape.

## Target Entity Shape

The first Energy Dashboard candidate should be a grid-consumption electricity
sensor per SP premise.

Target properties after semantics are proven:

- Platform: `sensor`
- Device class: `SensorDeviceClass.ENERGY`
- Unit: `UnitOfEnergy.KILO_WATT_HOUR`
- Native value: numeric electricity consumption in kWh
- State class: chosen based on proven source semantics
- Unique ID: stable across restarts and independent of display names
- Availability: unavailable when the coordinator has no valid current data
- Polling: through `DataUpdateCoordinator`, not per-entity API calls

The sensor should represent household grid consumption, not instantaneous power.
Do not use `SensorDeviceClass.POWER` unless a future endpoint exposes current
power in W or kW.

## State Class Decision

Choose the state class only after SP data semantics are proven.

Use `SensorStateClass.TOTAL` when:

- The source value is a controlled total that may decrease or reset with known
  semantics.
- The integration owns reset handling or can clearly distinguish reset
  boundaries.
- The value represents accumulated energy rather than a point-in-time
  measurement.

Use `SensorStateClass.TOTAL_INCREASING` only when:

- The source value or derived value is monotonically increasing.
- Any decrease is a real meter reset, rollover, replacement, or known reset
  boundary.
- Repeated polling cannot double count already-seen chart or billing periods.

Do not use either state class for raw monthly, billing-period, or chart-delta
rows until they have been converted into a safe total.

## SP Payload Handling Rules

### If SP Exposes Cumulative Meter Readings

If the API returns a cumulative electricity reading:

- Normalize the value to kWh.
- Preserve numeric precision with `Decimal` internally where practical.
- Publish one per-premise grid-consumption entity.
- Add Energy Dashboard metadata after tests prove stable units, values, and
  reset behavior.

### If SP Exposes Period Deltas Or Chart Buckets

If the API returns monthly, billing-period, daily, weekly, or chart-bucketed
usage:

- Do not expose a bucket value directly as the Energy Dashboard sensor state.
- Track seen period identifiers to avoid double counting.
- Convert deltas into a monotonic or reset-aware total only after the period
  boundary behavior is understood.
- Prefer closed periods over in-progress periods unless the API clearly marks
  partial values.
- Store enough state to survive restarts before deriving a cumulative value.
- Consider exposing raw bucket data only as disabled-by-default diagnostic
  sensors or attributes if useful.

### If SP Exposes Mixed AMI And Non-AMI Data

If smart-meter and non-smart-meter accounts have different endpoint behavior:

- Normalize them into the same SDK model only when semantics match.
- Keep different source types explicit in internal models.
- Avoid making one source type Energy Dashboard-ready just because the other
  source type is safe.
- Add tests for each source type separately.

## Coordinator Direction

The Home Assistant integration should fetch SP data through a coordinator:

- Use one `DataUpdateCoordinator` per config entry.
- Fetch account, premise, and utility readings in coordinator refreshes.
- Keep entity classes thin and data-driven.
- Do not issue API requests directly from sensor properties.
- Start with a conservative update interval, such as six hours, until live API
  behavior proves a better cadence.
- Treat temporary network/API failures as coordinator update failures.
- Treat expired or revoked credentials as Home Assistant reauth triggers.

The SDK should own Auth0/Jarvis request details. The HA integration should own
config entries, coordinators, entity setup, diagnostics, and Home Assistant error
mapping.

## Tariffs, Cost, Export, And Water

Defer tariff, cost, export, and advanced water behavior until real SP payloads
prove the required fields.

Tariffs:

- Prefer Home Assistant's Utility Meter integration for tariff splitting unless
  SP exposes authoritative tariff-bucketed usage.
- Do not invent tariff entities from display-only billing data.

Cost:

- Add cost support only if SP exposes a reliable current price, billing charge,
  or tariff model.
- Cost sensors should be separate from energy consumption sensors.

Export:

- Add grid export only if SP exposes export-to-grid data separately from
  consumption.
- Do not infer export from net consumption without clear source semantics.

Water:

- Water can be exposed as a utility sensor, but it should follow water-specific
  Home Assistant unit and device-class guidance.
- Do not conflate water dashboard support with electricity Energy Dashboard
  eligibility.

## Required Tests Before Energy Metadata

Before adding `device_class` and `state_class` to the electricity sensor, tests
must prove:

- The SDK fixture or live-capture parser identifies the electricity unit.
- The HA sensor value is numeric and normalized to kWh.
- Repeated coordinator refreshes do not double count the same source period.
- Reset or period-boundary behavior is handled intentionally.
- The entity is created through the HA setup path and visible through
  `hass.states`.
- The entity registry has a stable unique ID.
- Placeholder/no-data states do not publish misleading numeric values.

See `docs/testing-guidelines.md` for the broader HA custom-integration test
harness.

## Non-Goals For The First Energy Pass

The first implementation pass should not:

- Claim Energy Dashboard compatibility before source semantics are proven.
- Expose raw monthly or billing-period usage as `TOTAL_INCREASING`.
- Implement tariffs unless SP exposes reliable tariff-specific usage.
- Implement cost unless SP exposes reliable price or charge data.
- Implement export unless SP exposes export separately.
- Store passwords or OTP values in Home Assistant config entries.
- Put tokens, premise IDs, addresses, or account identifiers in diagnostics.

## Locked Direction

Build the SDK and integration so the first Energy Dashboard-ready entity is a
conservative, per-premise electricity grid-consumption sensor backed by proven
SP data semantics.

Until then, keep placeholder utility sensors non-statistical, prove the API data
shape with redacted fixtures, and add the Home Assistant test harness before
enabling Energy Dashboard metadata.
