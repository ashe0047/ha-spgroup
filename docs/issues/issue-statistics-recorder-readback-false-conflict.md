# External Statistics Read-Back Produces False Conflicts

## Status

Root cause identified; fix implemented and test-verified. The current fix uses
event-driven exact reconciliation: one queue attempt, one post-queue readback,
watchdog-bounded pending state, and Recorder-event retry without time-based
sleep loops. A live HA restart was not performed in the original investigation.

## Authority

Implemented issue record; current behavior is governed by the provider-owned statistics ADR.

## Problem

During a fresh SP Group setup, the external statistics importer can report a
conflict for every water row even though Recorder eventually contains the
correct rows.

Observed runtime sequence on 2026-07-22:

```text
water planned=11 imported=11 verified=0 warnings=11
```

The ledger then persisted 11 warnings in the form:

```text
statistics_conflict:<month-end>
```

Recorder contained all 11 water rows with positive, monotonic cumulative sums,
ending at `244.4`. The active HA issue registry did not contain an SP Group
statistics conflict repair at the time of inspection.

## Root Cause

The importer queues rows using HA's public
`async_add_external_statistics()` API. That API is asynchronous and inserts an
`import_statistics` job into Recorder's queue; it does not return a completion
future.

The current implementation in
`custom_components/sp_group/statistics_backfill.py` then:

1. calls `get_instance(hass).async_block_till_done()` once;
2. immediately queries `statistics_during_period()`;
3. classifies rows that are not visible yet as `statistics_conflict:*`.

Recorder's `async_block_till_done()` only waits when it can obtain a pending
commit future. It can return without one after the worker has dequeued the
import job while the transaction is still becoming visible to a subsequent
read. The later SQLite inspection showed the rows present and correct, which
is consistent with a Recorder queue/commit visibility race rather than an
actual value mismatch.

The current metadata update also marks the external series complete and may
clear its dirty state whenever only `bridge_warnings` are absent. This allows a
false verification result to leave conflict warnings without a clean,
retryable state.

## Required Invariants

- A submitted row is not an imported row until Recorder read-back confirms it.
- `statistics_imported_through` advances only through the latest contiguous
  verified row.
- A missing row is a temporary Recorder visibility/acknowledgement condition,
  not a value conflict.
- A visible row with a different `state` or `sum` remains a hard conflict and
  must not be overwritten silently.
- Incomplete verification keeps the ledger dirty/pending and retryable.
- Exact rows observed by a later retry clear the pending warning and advance
  the watermark idempotently.

## Proposed Fix

### 1. Use event-driven Recorder read-back verification

After queuing an external statistics batch, retain the existing Recorder
acknowledgement wait under a shielded watchdog, then query the expected suffix
once through `statistics_during_period()` on the Recorder executor. Missing
rows remain pending for the existing Recorder-event statistics retry. The same
batch is not requeued during readback.

### 2. Separate pending rows from conflicts

Change verification to return distinct outcomes:

- `verified`: row exists and `state`/`sum` match exactly;
- `pending`: row is not visible by the current read-back attempt;
- `conflict`: row is visible but its values differ.

Only `conflict` produces `statistics_conflict:<start>` and the statistics
conflict Repair. Exhausted `pending` verification should persist a bounded,
retryable warning such as `statistics_recorder_rows_pending` and must not create
a conflict Repair.

### 3. Preserve progress semantics

Update external metadata only after verification:

- advance `statistics_imported_through` to the latest verified contiguous row;
- leave the prior watermark unchanged for submitted-but-unverified rows;
- retain `statistics_dirty_from` and a pending/incomplete status when any
  expected rows remain unverified;
- set the external series complete and clear dirty state only after all planned
  rows are verified.

### 4. Make retries self-healing

The existing statistics-only retry path should be able to observe the rows on a
later run, classify them as exact/idempotent, clear the pending warning, and
complete the watermark without refetching provider history.

## Tests Required

- Recorder rows become visible after the first read-back attempt: no conflict
  warning or Repair is created, and the watermark advances.
- Rows remain temporarily invisible: a retryable pending warning is persisted;
  no sleep/backoff loop or second queue submission occurs in the same attempt.
- A visible row with a mismatched `state` or `sum` still creates the conflict
  warning/Repair and does not advance progress past the conflict.
- A later statistics-only retry observes exact existing rows, clears pending
  warnings, and completes idempotently.
- Partial verification advances only through a contiguous verified prefix.
- Water monthly rows and electricity hourly rows both use the same verification
  classification rules.
- The real temporary SQLite Recorder path reproduces delayed visibility and
  confirms that the false-positive repair cannot be produced.

## Scope and Safety

This fix keeps provider-owned external statistics, HA's public Recorder import
API, stable statistic IDs, and source-history behavior unchanged. It does not
delete or rewrite visible conflicting Recorder rows. It only improves the
acknowledgement boundary and the persisted progress state.

## Implementation Status

Implemented in the external statistics path:

- Recorder read-back performs one watchdog-bounded verification attempt;
- pending visibility is retried by the Recorder-event statistics scheduler;
- missing rows produce retryable pending warnings rather than conflicts;
- visible mismatches still create the conflict Repair;
- the watermark advances only through the verified contiguous prefix;
- pending imports retain dirty state and `external_migration_status="pending"`;
- complete imports clear warnings/dirty state and mark the series complete;
- verification warnings are bounded so a large electricity outage cannot create
  unbounded ledger metadata.

Focused and full integration verification pass: `294 passed, 1 skipped`.
