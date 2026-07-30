# External Statistics Recorder Read-Back Fix Plan

## Status

Implemented and superseded by the event-driven reconciliation implementation.
The historical plan below records the original bounded-readback design; the
current path uses exact preflight, one queue attempt, one post-queue readback,
watchdog-bounded pending state, and Recorder-event retry without sleep loops.

## Authority

Historical implementation plan; current external reconciliation is governed by `adr/adr-provider-owned-external-statistics.md`.

## Summary

Fix the false `statistics_conflict:*` warnings produced when SP Group queues
provider-owned external statistics and verifies them before Recorder exposes the
new rows. Preserve HA's public `async_add_external_statistics()` API, the
provider-owned statistics strategy, deterministic projections, idempotency, and
the existing statistics-only retry path.

The fix has two inseparable parts:

1. classify Recorder read-back outcomes as `verified`, `pending`, or
   `conflict`, with event-driven retry for pending visibility;
2. persist the watermark and completion state only after the expected rows are
   verified.

## Evidence and Grounding

- `custom_components/sp_group/statistics_backfill.py` queues external rows with
  `async_add_external_statistics()`, waits once with
  `Recorder.async_block_till_done()`, and immediately calls
  `statistics_during_period()`.
- HA's external statistics API inserts an `import_statistics` job into the
  Recorder queue and returns without a completion result.
- HA's `async_block_till_done()` waits for a commit future only when it detects
  queued work or pending event-session writes. It is not an import-job result
  tied to the submitted batch.
- The observed run reported water `planned=11 imported=11 verified=0
  warnings=11`, while the subsequent Recorder database contained all 11 exact
  rows with monotonic sums ending at `244.4`.
- `SpGroupLedgerStore.async_update_external_statistics_metadata()` currently
  always writes `external_migration_status="complete"`. The importer also uses
  `clear_dirty=not bridge_warnings`, which can clear dirty state despite
  verification warnings.

This plan does not require direct Recorder database writes, private statistics
compilation, sensor replay, provider refetching, or changes to source
projection rules.

## Design Contract

### Verification outcomes

Introduce a small immutable verification result used by both the pure planner
and async Recorder orchestration:

- `verified`: expected row exists and its `start`, `state`, and `sum` match;
- `pending`: expected row is not visible yet;
- `conflict`: expected row is visible but has a different `state` or `sum`.

The comparison must be ordered by normalized start time and must return the
latest contiguous verified prefix separately from rows that are verified after
a gap. A later exact row must not advance the watermark past an earlier pending
row.

### Warning and Repair semantics

- Missing rows after the bounded deadline produce a retryable warning such as
  `statistics_recorder_rows_pending:<start>`.
- Visible value mismatches produce the existing
  `statistics_conflict:<start>` warning and the existing actionable conflict
  Repair.
- Pending warnings never create a conflict Repair and never block the existing
  statistics retry scheduler.
- A successful later retry clears pending warnings and removes any stale
  conflict Repair for that entry.

### Watermark and completion semantics

`statistics_imported_through` remains required, but means only the latest
contiguous row confirmed correct in Recorder.

- Submitted-but-unverified rows do not advance it.
- Partial verification advances only through the verified contiguous prefix.
- Any pending or conflict result keeps the statistics state incomplete/dirty.
- Completion and dirty-state clearing happen only when all planned rows are
  verified and no blocking warning remains.
- The captured source fingerprint and ledger epoch remain conditional guards
  against committing progress from stale planning.

## Implementation Steps

### 1. Add pure verification model

In `statistics_backfill.py`:

- add a typed result for verified rows, pending starts, conflict starts, and
  contiguous `verified_through`;
- normalize Recorder row starts through the existing timestamp helper;
- distinguish a missing row from a visible mismatched row;
- retain exact-row idempotency and dirty-suffix behavior;
- ensure conflict detection stops at the first unexplained visible mismatch,
  while pending rows remain retryable.

Keep the existing `_reconcile_external_rows()` contract focused on deciding
which rows to submit. Add a separate pure read-back classifier instead of
overloading reconciliation with Recorder timing behavior.

### 2. Add state-based Recorder read-back helper

Add an async helper that:

1. queues the already planned import batch through
   `async_add_external_statistics()`;
2. waits using the existing Recorder synchronization method under an overall
   timeout;
3. reads the affected range through `_existing_external_statistics()`;
4. classifies the result;
5. performs one exact suffix readback after the Recorder barrier;
6. leaves missing visibility pending for the Recorder-generated statistics
   event; it does not sleep or requeue within the same attempt;
7. returns the final verification result without raising a conflict for missing
   rows.

The barrier and readback are wrapped in a shielded watchdog. A watchdog
timeout is a pending result and does not cancel the underlying Recorder task.
Recorder reads remain on `async_add_executor_job`.

Do not requeue the same batch during read-back retries. Recorder import is
already idempotent, and requeueing would add unnecessary queue pressure.

### 3. Correct external metadata transitions

Extend `async_update_external_statistics_metadata()` with an explicit completion
or pending state, while preserving its epoch and source-fingerprint checks.

Update the importer so:

- verified complete result: persist the contiguous watermark, clear dirty state,
  clear warnings, and mark external status complete;
- pending result: retain the prior watermark, retain/set dirty state, persist
  the pending warning, and mark external status pending;
- conflict result: retain progress only through the safe verified prefix, keep
  dirty state, persist conflict warnings, and mark external status pending;
- stale fingerprint/epoch: discard progress and leave the entry retryable.

`clear_dirty` must be derived from the final verification result, not only from
bridge warnings. `statistics_last_verified_at` should update only when the
verification result confirms at least the relevant contiguous rows.

### 4. Preserve retry and Repair behavior

- Keep statistics retry statistics-only; do not call provider coordinators or
  source-history APIs.
- Ensure `statistics_recorder_rows_pending` is not included in hard scheduler
  blockers.
- Keep genuine conflicts, corrected-bucket warnings, and existing negative
  deltas as hard blockers.
- Clear stale conflict/waiting Repairs after a later successful exact retry.
- Add bounded sanitized logging for `pending`, `verified`, and `conflict` counts,
  plus the final watermark and completion state. Do not log provider payloads,
  credentials, or account identifiers.

### 5. Keep utility behavior unchanged

Do not change:

- electricity 30-minute to hourly projection;
- water monthly final-hour projection;
- external statistic IDs or metadata contracts;
- source polling/backfill orchestration;
- native legacy statistics behavior outside the fresh external path.

## Test Plan

### Pure classifier tests

- all expected rows exact: all verified, complete watermark;
- all expected rows missing: all pending, no conflict;
- first row pending and later rows exact: watermark does not pass the gap;
- exact prefix followed by pending suffix: watermark ends at prefix;
- visible mismatched row: conflict warning at that timestamp;
- mixed missing and mismatched rows: pending and conflict are classified
  separately, with conflict still actionable;
- timestamp-shaped Recorder rows using float timestamps and datetime values;
- water month-end and electricity hourly rows use identical comparison rules.

### Async external importer tests

- Recorder query returns empty once, then exact rows become visible on the
  Recorder-event retry: the pending warning clears and the watermark advances;
- Recorder rows remain absent after the watchdog-bounded attempt: pending warning is stored,
  no conflict Repair is created, and status remains incomplete;
- Recorder returns visible mismatched values: conflict Repair is created and
  progress does not pass the mismatch;
- a later statistics-only retry sees exact existing rows and completes without
  calling provider APIs;
- retries do not requeue the same import batch;
- ledger fingerprint/epoch changes during read-back prevent metadata advance;
- exact existing rows remain idempotent and do not duplicate imports.

### Metadata and scheduler tests

- pending verification stores `external_migration_status="pending"` and keeps
  `statistics_dirty_from`;
- complete verification stores `external_migration_status="complete"`, clears
  dirty state, and advances `statistics_imported_through`;
- pending warnings do not return `BLOCKED` from the statistics scheduler;
- genuine conflict warnings still return `BLOCKED` and create Repair;
- failed verification does not suppress a later event/options retry.

### Recorder-backed regression

Extend the existing temporary SQLite Recorder coverage with a deterministic
delayed read-back seam. The test must assert that the real external statistics
API stores the rows, that a delayed first query does not create a false conflict,
and that subsequent `statistics_during_period()` values match the planned
water rows. Keep one test for a genuinely mismatched visible row.

## Independently Verifiable Work Units

1. Add pure verification result types/classifier and tests; run the focused unit
   tests.
2. Add bounded read-back orchestration with a test double for delayed Recorder
   visibility; run external-statistics tests.
3. Update ledger metadata transitions and add pending/complete persistence
   tests; run ledger and external-statistics tests.
4. Add scheduler/Repair regression tests; run init/backfill tests.
5. Add the Recorder-backed regression and run the full SP Group suite.
6. Run static checks and inspect the live ledger/Recorder state in a disposable
   dev reset before considering the issue closed.

## Verification Commands

```text
mise exec -- python -m pytest tests/components/sp_group/test_external_statistics.py -q
mise exec -- python -m pytest tests/components/sp_group/test_recorder_statistics.py tests/components/sp_group/test_init.py -q
mise exec -- python -m pytest tests/components/sp_group -q
mise run python:test
mise run python:lint
mise run ha:check
git diff --check
```

## Non-Goals

- No provider API changes or source-history refetch logic.
- No direct Recorder database access or private statistics compilation.
- No deletion or silent rewriting of genuinely conflicting Recorder rows.
- No migration of legacy native statistics in this development rollout.
