# Source Timeline Recovery and Water Parity

## Status

Implemented for the cadence-aware recovery scope on 2026-07-22. Accepted-bucket
correction remediation remains intentionally deferred and is still an explicit
Repair/rebuild workflow.

## Summary

The implementation now persists a cadence-aware recovery record for both
utilities. Electricity polling derives an exact cursor from unresolved ranges,
pending buckets, accepted bucket ends, and finally the seed boundary. Water
polling uses the same ledger apply/recovery path while retaining its full-history
endpoint and monthly closure rules.

The integration should have one source-timeline model for both utilities. The
provider adapters should differ only in endpoint and bucket cadence:

- electricity: 30-minute source buckets projected to hourly statistics;
- water: monthly source buckets projected to the final hour of each month.

The Home Assistant entity remains a current cumulative total updated when a
poll completes. Historical time projection belongs in the provider-owned
external statistics series, which is written through Home Assistant's public
Recorder statistics API.

## Evidence

### Electricity recovery

`SpGroupLedgerEntry.source_poll_cursor_start()` uses the earliest unresolved
range, then the earliest pending bucket, the latest accepted bucket end, or the
seed boundary. The former two-hour overlap has been removed, so an accepted
cursor advances at an exact half-open boundary:
`custom_components/sp_group/ledger.py`.

The electricity coordinator splits a large request into bounded windows and
deduplicates observations by bucket key in the ledger. Internal gaps are
recorded only between completed positive observations and remain persisted until
their exact span is accepted:
`custom_components/sp_group/coordinator.py` and
`custom_components/sp_group/ledger.py`.

### Incomplete-hour handling is safe but depends on source semantics

The statistics planner refuses an electricity hour unless both 30-minute
buckets are present and refuses water rows unless the monthly bridge from the
seed is contiguous. Missing bridges produce retryable warnings and safe-prefix
projection: `custom_components/sp_group/statistics_backfill.py`.

The AMI adapter now ignores leading, trailing, future, and in-progress zero
rows; an internal zero is represented as an unresolved range between completed
positive observations. Zero is never accepted as usage without explicit
provider coverage evidence: `custom_components/sp_group/coordinator.py`.
See [ADR: AMI zero coverage semantics](../adr/adr-ami-zero-coverage-semantics.md).

### Corrections are detected but not reconciled

If a later provider response changes an accepted bucket, the ledger preserves
the old value and records `corrected_bucket:<key>`. External statistics import
then stops and creates a Repair. This is safe against silent mutation, but it
does not converge to the provider's corrected data without an explicit
remediation path: `custom_components/sp_group/ledger.py` and
`custom_components/sp_group/statistics_backfill.py`.

### Restart recovery and lifecycle

Persisted source backfills in `running` state are converted to `failed` during
setup and become retryable. Deferred initial-backfill waiters, source tasks,
statistics tasks, and water startup catch-up tasks are tracked and cancelled on
unload. A transient water startup failure remains a coordinator failure and is
re-queued within the configured grace window:
`custom_components/sp_group/backfill.py`, `custom_components/sp_group/__init__.py`,
and `custom_components/sp_group/coordinator.py`.

### Water parity

Water rereads one full history response per premise, filters to closed months,
records missing closed months between confirmed observations, and scans every
expected closed month from the seed boundary through the latest ready month.
Repeated responses remain idempotent and each accepted monthly row is retained
for provider-owned statistics projection at the represented month's final hour.

The design requires water to use the same seed-plus-delta architecture as
electricity with a larger bucket interval. See
[manual seed accumulation design](../manual-seed-ami-accumulation-design.md)
and [provider-owned external statistics ADR](../adr/adr-provider-owned-external-statistics.md).

## Implemented Design

### 1. Introduce one cadence-aware source timeline contract

The shared `SpGroupBucketObservation`, `SpGroupSourceCoverage`, and
`SpGroupUnresolvedRange` contract is used by both utility coordinators and the
ledger. It exposes:

- bucket start and end;
- bucket cadence or a source-specific next-boundary function;
- normalized observations;
- the provider coverage boundary known for this response;
- unavailable or unresolved bucket boundaries that must be retried.

The electricity adapter supplies 30-minute AMI observations. The water adapter
supplies closed monthly observations. The ledger, cursor, pending handling,
accepted handling, source status, and statistics planner operate on the common
contract while endpoint-specific parsing remains at the coordinator boundary.

The water endpoint may continue to return the full history because its SDK
method has no range parameters. The adapter must still apply the same derived
cursor and unresolved-gap filters before applying observations to the ledger.

### 2. Persist unresolved source gaps and derive polling from them

Ledger state contains bounded, serializable unresolved range records. Each
record contains cadence, start/end, first/last observation times, and a
sanitized reason; it does not contain raw provider payloads or credentials.

On every successful source response:

1. merge accepted and pending observations by bucket key;
2. mark a previously unresolved bucket resolved when a valid observation is
   accepted;
3. discover missing expected boundaries only when the provider coverage
   boundary proves that the interval should exist;
4. derive the next request start from the earliest unresolved bucket, then the
   earliest pending bucket, then the latest accepted bucket end, then the seed
   boundary;
5. keep all transitions atomic with the existing ledger lock and epoch checks.

For electricity, trailing zero AMI rows must not establish coverage. Leading
zero prefixes remain outside AMI coverage. An internal zero between confirmed
covered buckets becomes an unresolved slot rather than a zero-usage bucket.
For water, closed months are the coverage boundary and missing months between
the seed boundary and the latest ready month become unresolved monthly slots.

This closes the older-internal-gap failure without repeatedly fetching from the
seed boundary on every normal poll.

### 3. Make water use the same seed/bridge/statistics flow

Water normal polling uses the same source cursor and ledger apply entry point as
electricity. Only these values remain utility-specific:

- endpoint and response parser;
- bucket boundary function;
- cadence (`30 minutes` versus `1 calendar month`);
- stability rule (electricity requires two unchanged sightings; water requires
  a closed month plus its existing grace period).

The statistics planner continues to use the same external-statistics
reconciliation contract. Water rows remain at the final top-of-hour represented
by each monthly period, and each row's delta equals exactly one accepted monthly
bucket. Missing monthly bridges remain retryable and never produce a fabricated
zero or negative row.

### 4. Correct AMI zero handling according to the locked ADR

Zero interpretation is applied in the electricity source adapter, where ordered
provider rows and coverage context are available:

- leading zeros before the first confirmed AMI bucket are ignored as
  pre-installation coverage;
- trailing zeros after the last confirmed bucket are ignored as provider lag;
- internal zeros after coverage begins create unresolved slots;
- a zero is accepted as usage only if a future provider contract explicitly
  identifies it as a real zero-usage bucket.

The parser must not silently turn unavailable provider data into accepted ledger
buckets or complete statistics bridges.

### 5. Make retry and restart recovery lifecycle-safe

- The deferred initial-backfill waiter, source tasks, statistics tasks, and
  water startup catch-up task are tracked and unload-safe.
- A failed startup catch-up preserves coordinator failure/availability behavior
  and re-queues a bounded retry during the water grace window.
- Startup scans all expected closed water months from the seed boundary through
  the latest ready month.
- Statistics-only retries never call source APIs.

These changes follow Home Assistant's coordinator model for polling and
`UpdateFailed` availability, and its lifecycle requirement that listeners and
background work are cleaned up when a config entry unloads.

### 6. Correction policy retained for a separate remediation design

Correction detection remains conservative in this refactor:

- persist the old and candidate bucket values in a bounded correction record;
- keep the accepted ledger value unchanged until remediation is confirmed;
- route the correction to the existing explicit Repair/options rebuild flow when
  the user chooses it;
- when approved, recompute cumulative external rows from the correction boundary
  onward and submit them through `async_add_external_statistics`;
- verify Recorder read-back before clearing the correction and advancing
  metadata;
- do not modify native entity statistics or delete arbitrary statistic IDs.

Home Assistant's Recorder import implementation updates an existing row when
the same statistic ID and start timestamp are submitted, so the explicit
provider-owned rebuild can remain within the public API boundary. The rebuild
must still be guarded by statistic ownership, source fingerprint, epoch, and
read-back verification.

## Required Tests Before Implementation Is Accepted

- Electricity tail outage spanning multiple bounded API windows.
- Electricity internal gap older than the normal overlap, including later
  buckets already accepted.
- AMI leading zeros, trailing lag zeros, internal zeros, and later replacement.
- Complete hours, missing half-hour bridges, and recovery after the missing
  bucket arrives.
- Water parity tests for seed bridge, missing older month, late closed month,
  monthly projection, and repeated full-history responses.
- Water startup catch-up failure followed by bounded retry and final Repair.
- Restart while source backfill is running, while statistics import is pending,
  and while the deferred HA-start waiter exists.
- Provider correction detection, explicit rebuild, Recorder read-back, and
  non-correction of unrelated statistic IDs.
- Idempotency and no-provider-call assertions for statistics-only retries.
- Diagnostics and persistence tests proving unresolved/correction records are
  bounded and secret-safe.

## Home Assistant Alignment

Home Assistant recommends `DataUpdateCoordinator` for polling, raising
`UpdateFailed` for unavailable services, and using config-entry unload cleanup
for listeners and resources. The proposed design keeps those responsibilities
in the coordinators and uses the public Recorder external-statistics API for
timestamped provider history. It does not write Recorder tables directly or
make sensor state history pretend that old observations arrived at their
original timestamps.

References:

- [Home Assistant: Fetching data](https://developers.home-assistant.io/docs/integration_fetching_data/)
- [Home Assistant: Handling setup failures](https://developers.home-assistant.io/docs/integration_setup_failures/)
- [Home Assistant: Listening for events](https://developers.home-assistant.io/docs/integration_listen_events/)
- [Home Assistant: Config entries and unload cleanup](https://developers.home-assistant.io/docs/config_entries_index/)
- [Home Assistant: Recorder statistics API changes](https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes/)
