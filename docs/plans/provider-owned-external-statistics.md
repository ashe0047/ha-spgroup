# Provider-Owned External Statistics Refactor Plan

## Summary

Move SP Group's authoritative historical electricity and water statistics from
the entity statistic IDs to stable provider-owned external statistic IDs. Keep
the existing sensor entities and ledger, but remove the native-reference and
native-offset dependency from the new import path.

The design is aligned with Home Assistant's delayed-history integrations. Core
integrations such as Opower, Elvia, Suez Water, and Anglian Water use
`async_add_external_statistics()` for provider-timestamped usage. Recorder
requires an external statistic ID in `<domain>:<slug>` form, with the metadata
source matching the ID domain. Energy stores configured sources as statistic
IDs and calculates usage changes from cumulative `sum` values.

Grounding:

- [Recorder statistics API guidance](https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes/)
- [Recorder external statistics implementation](https://github.com/home-assistant/core/blob/dev/homeassistant/components/recorder/statistics.py)
- [Energy statistic source model](https://github.com/home-assistant/core/blob/dev/homeassistant/components/energy/data.py)
- [Energy change calculation](https://github.com/home-assistant/core/blob/dev/homeassistant/components/energy/websocket_api.py)
- [Opower delayed statistics](https://github.com/home-assistant/core/blob/dev/homeassistant/components/opower/coordinator.py)
- [HA data fetching and polling guidance](https://developers.home-assistant.io/docs/integration_fetching_data/)

## Resolved Design Constraints

### 1. Statistic ownership and collisions

The external ID is derived from the already opaque, deterministic ledger key:

```text
sp_group:<canonical_sensor_key>_consumption
```

The canonical key is lower-case, slug-compatible, and digest-based. It must
not be produced by lossy normalization of arbitrary provider identifiers.
`valid_statistic_id()` must pass before the ID is persisted.

The ID is a global Recorder namespace. Before the first write, the integration
must inspect Recorder metadata for the ID on the Recorder executor:

- no metadata: claim the ID in the ledger and continue;
- matching SP Group metadata and matching persisted identity: continue;
- incompatible source, unit class, unit, mean type, or sum contract: hard
  conflict, create a Repair, and perform no writes;
- an ID already claimed by a different in-memory SP Group runtime entry: hard
  conflict unless both entries resolve to the same persisted provider identity.

The collision path is never warning-only. Recorder updates an existing row
when statistic ID and start timestamp match, so a collision could otherwise
overwrite another series.

### 2. External row contract

Each projected provider period produces:

- `start`: timezone-aware top-of-hour timestamp, normalized by Recorder to UTC;
- `state`: the usage for that represented provider period, matching Core's
  delayed-history external integrations;
- `sum`: cumulative positive usage for this external series.

The sensor continues to expose the absolute ledger total. The external
statistics series is a usage timeline; its `state` is not a replacement for
the live sensor state. Energy consumption is derived from consecutive
`sum` changes.

Metadata must use:

- `source="sp_group"`;
- the persisted external statistic ID;
- `mean_type=StatisticMeanType.NONE`;
- `has_sum=True`;
- `EnergyConverter.UNIT_CLASS` and kWh for electricity;
- `VolumeConverter.UNIT_CLASS` and cubic metres for water;
- a stable, non-secret human-readable name.

Do not depend on deprecated `has_mean` input behavior. Preserve existing
entity metadata and entity IDs for the live sensors.

### 3. Reconciliation and late data

`statistics_imported_through` remains a progress hint only. Add explicit
metadata for:

- `external_statistic_id`;
- `external_identity_fingerprint`;
- `statistics_dirty_from`;
- `statistics_source_fingerprint`;
- `external_migration_status`;
- `statistics_last_verified_at`.

The ledger update path marks `statistics_dirty_from` when a newly accepted
bucket or a provider correction can affect an already-imported period. A
successful reconciliation clears it only after Recorder verification.

Every reconciliation captures the current ledger fingerprint and entry epoch.
Before metadata is advanced, it compares the captured fingerprint with the
current ledger. If accepted data changed during planning, the job must discard
its progress metadata and retry from the new dirty boundary.

### 4. Recorder acknowledgement

`async_add_external_statistics()` queues a Recorder job and returns before the
database write is necessarily visible. The importer must:

1. validate ownership and existing rows;
2. queue one bounded batch through the public API;
3. await the Recorder commit future through the existing asynchronous Recorder
   synchronization method, with a timeout;
4. reread the affected external rows on the Recorder executor;
5. compare every expected `start`, `state`, and `sum` value;
6. only then persist progress, fingerprint, and `last_verified_at`.

Timeouts, missing rows, and database failures remain retryable and never mark
the series complete. The adapter must isolate Recorder synchronization so the
rest of the integration does not call Recorder internals or write its DB.

## Non-Goals

- Do not replay historical values through the sensor state machine.
- Do not refetch provider history from the statistics-only retry path.
- Do not delete, rewrite, or silently repair legacy entity-backed statistics.
- Do not change provider request shapes, seed semantics, entity IDs, units, or
  the approved hybrid electricity source decision.
- Do not automatically mutate Home Assistant Energy preferences.
- Do not use private Recorder compilation or direct database writes.

## Phase 1: Ledger and Identity Contract

### 1. Add external metadata fields

Extend `SpGroupLedgerEntry` serialization, deserialization, diagnostics, and
test fakes with the metadata listed above. Legacy entries must load without
losing accepted buckets, pending buckets, seed values, or latest totals.

Add explicit strategy constants:

- `external_provider_v3` for the new path;
- existing native strategies retained only for migration detection.

Add atomic ledger methods for:

- claiming and persisting an external statistic ID;
- marking `statistics_dirty_from`;
- conditional metadata update using captured source fingerprint and epoch;
- clearing verified dirty state;
- recording migration status.

The conditional update must refuse to advance metadata if the accepted ledger
changed during the import job.

### 2. Define canonical identity

Implement and test one helper that uses the existing opaque sensor key without
including credentials, tokens, raw provider payloads, or display labels. Test:

- restart and reload stability;
- entity display-name and entity-ID changes;
- discovery order changes;
- account/config replacement behavior;
- electricity/water separation;
- invalid IDs and normalization collisions;
- duplicate runtime entries.

If a persisted ID exists, it is authoritative. The integration must not
silently regenerate it from changed discovery data.

### 3. Define collision ownership

Implement a Recorder metadata ownership check on the Recorder executor and a
per-Home-Assistant in-memory claim registry. Treat incompatible metadata or a
different persisted identity as a hard conflict. Add Repair creation and
cleanup tests, including a foreign statistic with the same candidate ID.

## Phase 2: Pure External Projection and Reconciliation Planner

### 1. Preserve source projection

Retain the validated projection rules:

- electricity: complete accepted 30-minute buckets projected into hourly rows;
- water: accepted monthly buckets represented at the final hour of the month;
- missing bridges omit unsafe rows and remain retryable warnings;
- provider periods are normalized to Singapore local boundaries and passed to
  Recorder as timezone-aware timestamps;
- duplicate buckets do not double count.

Water month-end, February, leap-year, year-boundary, and UTC conversion cases
must be explicit tests.

### 2. Build external rows

For a complete planned sequence:

```text
external_state[row] = row.delta
external_sum[row] = previous_external_sum + row.delta
```

For a first sequence, `previous_external_sum` is zero. For an existing
sequence, begin at the verified predecessor row immediately before
`statistics_dirty_from`, or zero if no predecessor exists.

If there is a missing bridge, do not import a row whose provider period depends
on that bridge. Once the bridge arrives, mark the affected suffix dirty and
reconcile it from the predecessor.

### 3. Determine the affected suffix

The planner must calculate the earliest affected row from:

- `statistics_dirty_from`, if present;
- the earliest accepted row after the verified fingerprint differs;
- the first missing external row;
- the first existing row whose expected state or sum differs.

It must then plan the complete deterministic suffix, not only rows newer than
`statistics_imported_through`. Existing exact rows before the suffix are
read-only and skipped. Existing mismatches in the owned suffix may be updated
only when the mismatch is explained by a ledger change; unexplained mismatches
are hard conflicts.

Accepted provider corrections currently remain immutable in the ledger. A
`corrected_bucket:*` warning therefore blocks external reconciliation for that
utility and creates an actionable Repair rather than silently exporting stale
or newly changed data.

### Verification

Pure tests must prove:

- positive cumulative sums for electricity and water;
- `state` equals represented period usage;
- late insertion rewrites exactly the affected suffix;
- missing-bridge completion rewrites the dependent suffix;
- duplicate and idempotent retries produce no additional changes;
- unexplained mismatches stop safely;
- captured ledger changes prevent stale metadata advancement.

## Phase 3: Recorder Adapter

### 1. Replace entity import

Replace the new path's `async_import_statistics()` calls with
`async_add_external_statistics()`. Remove the entity registry lookup as a
prerequisite for the external series. A disabled or unavailable sensor must
not prevent provider statistics import.

Keep all Recorder reads through `get_instance(hass).async_add_executor_job`.
Use bounded read windows based on the affected suffix and one predecessor row.
Use `get_metadata()` and `get_last_statistics()` where they avoid loading the
entire series.

### 2. Implement queue-and-verify batches

Add a small adapter that owns:

- metadata validation;
- batch-size limits;
- `async_add_external_statistics()` queueing;
- asynchronous Recorder commit waiting with timeout;
- affected-row reread and exact comparison;
- retryable versus hard failure classification.

Never advance ledger metadata after only queueing a batch. Cancelled jobs do
not create Repairs; confirmed Recorder or data-integrity failures do.

### 3. Bound resource usage

Plan and verify at most one utility suffix at a time. Keep pure planning in an
executor job for large ledgers, cap batch sizes, and log counts/durations only.
Do not reread the full multi-year series on every normal poll when a dirty
boundary permits a bounded suffix query.

## Phase 4: Runtime Orchestration

### 1. Fresh setup and normal polling

- source backfill persists accepted buckets first;
- external statistics import starts from persisted ledger data without waiting
  for a native Recorder reference;
- accepted buckets from normal polling mark the affected suffix dirty and
  schedule the existing statistics-only single-flight job;
- source polling and statistics reconciliation use the ledger conditional
  update so a concurrent source update causes a retry rather than stale
  completion.

### 2. Retry eligibility

Replace all native `native_offset_v2` readiness predicates in setup, backfill,
options, listeners, and diagnostics with external-series predicates:

- healthy ledger;
- source history attempted where required;
- external ID claimed;
- projected rows or dirty/incomplete state exists;
- no hard ownership, correction, or statistics conflict.

Retry remains statistics-only and never calls provider history APIs.

### 3. Recorder listener

The hourly Recorder event is not a prerequisite. It may remain only as a
bounded retry trigger for a pending external Recorder write or retryable
verification failure. It must use the same scheduler, remain unload-safe, and
never create a native-reference Repair.

### 4. Background lifecycle

Initial source backfill, statistics reconciliation, and retries remain tracked
HA tasks. Unload cancels them. Task exceptions are consumed, classified, and
reported without blocking HA startup or the event loop.

## Phase 5: Legacy Migration and Energy UX

### 1. Detect legacy data

Recognize `native_anchored_v1`, `native_offset_v2`, old entity statistic IDs,
legacy water negative-delta warnings, and old statistics watermarks. Do not
treat legacy progress as external-series completion.

Disable the old importer for an entry before starting external migration, so
the two strategies cannot write concurrently.

### 2. Rebuild external history

For each utility independently:

1. claim and persist the external ID;
2. initialize dirty state at the first safe projected row;
3. reconcile all safe accepted buckets chronologically;
4. wait for Recorder visibility and verify every imported suffix row;
5. conditionally persist `external_provider_v3`, verified fingerprint, and
   progress metadata.

Interrupted migration resumes idempotently. Legacy rows remain unchanged.
Any conflict or correction blocks only the affected utility and does not erase
healthy data from the other utility.

### 3. Dashboard migration

Create a persistent Repair or options action that displays:

- the legacy entity statistic ID;
- the verified external statistic ID;
- migration status;
- the exact Energy configuration action required.

The user explicitly acknowledges the source switch after configuring Energy.
Only then is the migration Repair cleared. Add tests for old configuration,
new configuration, restart persistence, and acknowledgement failure.

## Phase 6: Test Matrix

### Identity and metadata

- valid external IDs and source/domain alignment;
- unit converter classes and units;
- deterministic identity across reload/config changes;
- incompatible metadata and foreign-owner collisions;
- duplicate runtime claim handling;
- secret-safe diagnostics and Repairs.

### Pure projection

- electricity hourly aggregation from 30-minute buckets;
- water month-end projection across all calendar month shapes;
- period `state` and cumulative `sum` equations;
- zero usage, missing bridge, duplicate, late, and corrected buckets;
- timezone-aware top-of-hour timestamps.

### Recorder integration

- no native reference required;
- disabled sensor does not block import;
- queue visibility waits before metadata advancement;
- timeout/database failure is retryable;
- exact rows are idempotent;
- late bucket rewrites only the deterministic suffix;
- unexplained mismatch creates a Repair and does not overwrite;
- ledger change during planning prevents stale completion;
- forced retry imports missing rows without provider calls.

### Migration and runtime

- v1/v2 legacy detection and old-row preservation;
- interrupted migration and restart recovery;
- fresh setup without entity-registry dependency;
- source backfill followed by external import is non-blocking;
- normal polling catch-up marks and imports all accepted delayed buckets;
- options and Recorder triggers share one statistics-only single flight;
- unload cancels outstanding work;
- Energy source switch acknowledgement is durable.

## Verification Commands

```text
mise exec -- python -m pytest tests/components/sp_group/test_recorder_statistics.py -q
mise exec -- python -m pytest tests/components/sp_group/test_statistics_backfill_electricity_invariants.py tests/components/sp_group/test_statistics_backfill_water_invariants.py tests/components/sp_group/test_statistics_backfill_cross_utility.py -q
mise exec -- python -m pytest tests/components/sp_group/test_init.py tests/components/sp_group/test_coordinator.py tests/components/sp_group/test_sensor.py -q
mise exec -- python -m pytest tests/components/sp_group -q
mise run python:test
mise run python:lint
mise run ha:check
```

Manual verification must confirm both statistic IDs:

1. Complete a fresh setup and verify external metadata and rows exist.
2. Confirm external rows use provider bucket timestamps and non-negative
   cumulative-sum deltas.
3. Confirm the sensor remains the latest trusted cumulative total.
4. Configure Energy to use the external statistic ID and acknowledge migration
   when applicable.
5. Simulate delayed multi-bucket polling and verify only the affected suffix is
   reconciled without provider refetch or native-reference waiting.
6. Restart during and after migration and confirm safe resume, no duplicate
   rows, no old-strategy writes, and no source refetch after success.

## Rollout Order

1. Add identity, ownership, metadata, and ledger conditional-update tests.
2. Add pure projection and suffix-reconciliation tests, initially red against
   the current entity-backed implementation.
3. Add the Recorder queue-and-verify adapter tests.
4. Implement external statistics for fresh setups behind
   `external_provider_v3`.
5. Implement legacy migration and explicit Energy source-switch UX.
6. Run migration against development data and verify both utility paths.
7. Remove obsolete native-offset code only after migration, conflict, retry,
   and runtime tests are green.

## Assumptions

- The provider ledger remains the source of truth for accepted usage buckets.
- The live sensor remains a normal HA entity and is not historically replayed.
- External statistics own provider-time history; native entity statistics remain
  legacy/observational data.
- Existing conflicting or corrected legacy data is not silently rewritten.
- Energy preferences are changed only through explicit user action.
