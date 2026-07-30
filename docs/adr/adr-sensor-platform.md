# ADR: Sensor Platform Architecture

## Status

Accepted - 2026-07-25

## Authority

Normative for sensor entity identity, availability, state, device class, and sensor-to-ledger behavior.

## Implementation

`custom_components/sp_group/sensor.py` and sensor/entity tests.

## Context

The SP Group integration exposes utility consumption as HA sensor entities.
Each sensor represents a single utility (electricity or water) for a
discovered premise. The sensor value is the latest trusted cumulative total
from the ledger, combining the manually-entered seed with post-seed provider
consumption.

The sensor platform (`sensor.py`, 197 lines) is intentionally minimal —
it delegates all state management to the coordinator and ledger.

## Design

### Entity Creation

Sensors are created dynamically as discovery data becomes available. The
`async_setup_entry()` function registers a listener on the discovery
coordinator. When discovery data is updated, `add_discovered_entities()`
creates an `SpGroupUtilitySensor` for each `SpGroupFutureSensorKey` that
has a matching utility coordinator.

```
async_setup_entry() → register listener on discovery coordinator
  → on discovery update:
    for each future_sensor_key in data.future_sensor_keys():
      if key not already added:
        create SpGroupUtilitySensor(coordinator, discovery_coordinator, sensor_key)
```

The sensor is initialized with a description based on its utility type:
- Electric: `SensorDeviceClass.ENERGY`, `UnitOfEnergy.KILO_WATT_HOUR`
- Water: `SensorDeviceClass.WATER`, `UnitOfVolume.CUBIC_METERS`

### State Derivation

The sensor's `native_value` comes from the ledger entry:

```
native_value:
  self._ledger_entry()  →  SpGroupLedgerEntry | None
    → None: return None (no trusted total available yet)
    → entry: return entry.latest_total
```

`_ledger_entry()` returns `None` when:
- `ledger_store` is None
- The snapshot is malformed
- The sensor's ledger entry doesn't exist
- `entry.has_trusted_total` is False (no accepted buckets)

When `_ledger_entry()` returns `None`, the sensor also returns `None` for
`device_class` and `state_class`, causing HA to treat it as unavailable.

### Device and State Class Gating

`device_class` and `state_class` are gated on the presence of a trusted
ledger entry:

```python
@property
def device_class(self):
    if self._ledger_entry() is None:
        return None  # not ready
    return SensorDeviceClass.ENERGY / WATER

@property
def state_class(self):
    if self._ledger_entry() is None:
        return None
    return SensorStateClass.TOTAL_INCREASING
```

Before the backfill completes and accepted buckets exist, the sensor is
available (entity exists) but has `native_value=None`, `device_class=None`,
and `state_class=None`. After the first accepted bucket, it transitions
to the full state.

### External Statistics Separation

Sensor entities track the cumulative meter reading (`seed + post-seed
consumption`). Historical time-series data for the Energy Dashboard is
stored in provider-owned external statistics via
`async_add_external_statistics()` using a separate colon-form statistic ID
(`sp_group:<stable_sensor_key>_consumption`) with `source=sp_group`. These are
not tied to the sensor entity — they have their own metadata and lifecycle.

## Consequences

### Positive

- Minimal sensor code — delegates to coordinator and ledger
- Dynamic entity creation scales with discovered premises
- Device/state class gating prevents HA from recording statistics before
  data is ready
- External statistics keep the Energy Dashboard data separate from the
  sensor state

### Negative

- Sensor is unavailable until backfill creates accepted buckets
- `native_value` only includes post-seed consumption, which may be much
  smaller than the full historical cumulative value
- No sensor entity for the external statistics series — they exist only
  as Recorder statistics (the "without state" label in Energy Dashboard)

## Related

- `sensor.py` — `SpGroupUtilitySensor`, `async_setup_entry()`
- `ledger.py` — `SpGroupLedgerEntry.latest_total`, `has_trusted_total`
- `statistics_backfill.py` — external statistics import
