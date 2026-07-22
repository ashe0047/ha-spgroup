# ADR: Provider-Owned External Statistics for Delayed Utility History

## Status

Accepted - 2026-07-22

## Context

SP Group's provider does not guarantee that a poll returns only the most recent
hour. Electricity responses contain 30-minute buckets and may arrive after the
hour they represent. Water responses contain historical monthly buckets and
may also arrive later than the coordinator refresh that discovers them.

The integration therefore has two different time concepts:

1. The current sensor value is the latest trusted cumulative ledger total.
2. The provider buckets are historical usage observations with their own
   timestamps.

The current sensor implementation exposes `latest_total` and
`TOTAL_INCREASING` through `custom_components/sp_group/sensor.py`. A normal
entity state write is timestamped when HA receives the update. If one poll
accepts many older buckets, HA's native sensor compiler sees one large state
transition at the poll time and cannot infer the timestamps of the individual
provider buckets.

The current statistics importer writes to the entity statistic ID using
`async_import_statistics()` and uses `source=recorder`. This makes the
imported series share ownership with HA's native sensor statistics compiler.
The native-reference and native-offset logic in
`custom_components/sp_group/statistics_backfill.py` exists to connect those two
writers, but it creates unnecessary ordering, reference, conflict, and
negative-delta failure modes when provider data is late.

Home Assistant Core integrations that receive delayed utility history use
provider-owned external statistics instead. Examples include:

- [Opower delayed usage handling](https://github.com/home-assistant/core/blob/dev/homeassistant/components/opower/coordinator.py)
- [Elvia hourly meter history](https://github.com/home-assistant/core/blob/dev/homeassistant/components/elvia/importer.py)
- [Suez Water daily history](https://github.com/home-assistant/core/blob/dev/homeassistant/components/suez_water/coordinator.py)
- [Anglian Water timestamped meter readings](https://github.com/home-assistant/core/blob/dev/homeassistant/components/anglian_water/coordinator.py)

HA's Recorder statistics API distinguishes entity-backed imports from external
statistics, and the Energy integration stores a `statistic_id` as its source.
The public API and current API changes are documented in the [Recorder
statistics API guidance](https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes/).

## Decision

SP Group will own the provider-timestamped consumption timeline through
dedicated external statistic IDs.

### Statistics ownership

- Accepted ledger buckets are the source of truth for historical usage.
- Each accepted bucket is projected to its HA-compatible timestamp and imported
  through `async_add_external_statistics()`.
- The external series starts its `sum` at zero for the first imported usage row
  and advances by accepted bucket deltas. It does not require an HA-native
  reference row, synthetic continuation row, or native sum offset.
- The sensor continues to publish the latest trusted cumulative total through
  the normal entity path. Its state history remains a record of observations
  made by HA, not a retroactively reconstructed provider time series.
- The Energy Dashboard must use the external statistic ID as its consumption
  source. Native entity statistics may still exist for the sensor, but they are
  not the authoritative SP Group dashboard series.

### Statistic identity

Each utility sensor receives one deterministic external statistic ID derived
from its stable discovery key, for example:

```text
sp_group:<stable_sensor_key>_consumption
```

The implementation must normalize the name to HA's statistic ID rules, detect
collisions, and avoid tokens, account credentials, or other secret material.
The derived ID must remain stable if the entity display name or entity ID
changes. The resolved ID and strategy version are persisted in ledger metadata
once established.

### Migration

- Existing entity-backed `native_anchored_v1` and `native_offset_v2` rows are
  legacy data and are never silently deleted or rewritten.
- On upgrade, the integration rebuilds the new external series from accepted
  ledger buckets, independently of the old entity statistic watermark.
- Existing Energy Dashboard configuration is not mutated behind the user's
  back. The integration provides a repair/action with the old and new statistic
  identifiers and documents the one-time source switch.
- New setups use the external statistic ID from the first import.
- The migration is complete only after the external rows and metadata have been
  verified. The old legacy series remains available for inspection until the
  user removes or replaces it.

### Retry and polling

- Statistics retry is statistics-only and reads persisted ledger buckets; it
  never refetches provider history.
- Accepted buckets discovered by normal polling schedule an external-statistics
  retry immediately through the existing single-flight scheduler.
- Recorder hourly events are no longer required as a native-reference trigger.
- Polling cadence controls freshness and API load, not historical correctness.
  Late buckets are handled by the ledger cursor, overlap/finality rules,
  deduplication, and repeatable external-statistics reconciliation.

## Consequences

### Positive

- Provider timestamps remain authoritative even when API responses are late.
- Imported history cannot race HA's native entity compiler for the same statistic
  ID.
- No native reference wait, native sum adjustment, or synthetic baseline is
  needed for the provider-owned series.
- Energy Dashboard consumption deltas are derived from a single deterministic
  `sum` sequence.
- The current sensor remains useful for live state, availability, and ordinary
  HA entity history.

### Negative

- Existing users must switch the Energy Dashboard source to the new statistic
  ID. The integration cannot safely change unrelated Energy preferences without
  an explicit supported migration API.
- The Recorder database may contain both the legacy entity series and the new
  external series during migration.
- The sensor History panel will still show state updates at observation/write
  time; this ADR does not attempt unsupported historical state replay.
- The integration must maintain a second identity and migration state in its
  ledger metadata.

## Alternatives rejected

### Native entity statistics only

Rejected because provider lag and multi-bucket responses make native state
transitions temporally inaccurate. No polling interval can remove that
dependency.

### Continue using the entity statistic ID with native offsets

Rejected as the long-term design because it requires two writers to coordinate
on one Recorder series. It remains legacy compatibility data, but new imports
will not extend it.

### Backdate sensor states

Rejected because the normal `SensorEntity` API does not provide a supported
historical replay operation. HA's lower-level timestamped state setter is
explicitly an unstable core-internal API.

### Direct Recorder database writes

Rejected because they bypass the public Recorder queue, metadata validation,
purge behavior, and integration compatibility guarantees.
