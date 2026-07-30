# ADR: Backfill Runs Once, Then Repair on Failure

## Status

Locked — 2026-07-17

## Authority

Normative for source-history backfill lifecycle and repair/retry handling.

## Implementation

`custom_components/sp_group/backfill.py`, `__init__.py`, and backfill lifecycle tests.

## Context

The initial historical backfill is a one-time data import that fetches years of
SP Group API data and populates both the integration ledger and HA's long-term
statistics. Once complete, regular coordinator polling handles incremental
updates (electricity hourly via AMI, water monthly via history charts).

Currently, `async_start_initial_backfill` is called on **every** HA restart,
regardless of whether the backfill has already completed. This has two problems:

1. **Wasteful**: The backfill re-fetches and re-processes the same historical
   API data on every startup, triggering heavy AMI API calls (10-30s each)
   that serve no purpose once data is already loaded.

2. **Violates HA convention**: Config entry setup should be fast and
   predictable. Running a long-running data import on every startup is not
   aligned with how other HA integrations work. The [HA Dev Docs state]:
   > "Config entry setup is expected to be fast. Heavy work should be
   > deferred or triggered explicitly."

3. **Poor UX**: The user sees "Loading" or waits for data every time they
   restart, even when no new data needs to be imported.

## Decision

**The backfill runs only once — after the config flow creates a new entry.**
On subsequent restarts, if backfill has already completed (detected via the
`statistics_imported_through` watermark), it is skipped entirely. Regular
coordinator polling handles incremental data going forward.

If the backfill fails, a **HA repair issue** is created so the user knows.
The user can retry the backfill through the options flow (which already has
the retry action).

### Implementation rules

1. **Backfill triggers only on new config entries.** The config flow calls
   `async_start_initial_backfill` after entry creation. On subsequent restarts
   (when the entry is loaded from storage), the setup code skips the backfill
   if `statistics_imported_through` is set on any ledger entry.

2. **Detection is watermark-based.** If ALL seeded utilities have a
   `statistics_imported_through` value, the backfill is considered complete.
   If ANY utility is missing the watermark (e.g., interrupted mid-backfill),
   the backfill is re-triggered.

3. **Failure creates a repair issue.** If the backfill fails (API errors,
   timeout, etc.), a HA `repairs.issue_registry` issue is created with
   domain `sp_group` and issue ID `backfill_failed`. The issue text directs
   the user to the options flow to retry.

4. **Retry is through the existing options flow.** The options flow already
   has a "retry backfill" action. No new UI is needed. Retrying clears the
   failed repair issue.

### Non-goals

- This ADR does not change how regular polling works (electricity AMI hourly,
  water history monthly). Those remain unchanged.
- This ADR does not change the hybrid backfill design (history endpoint for
  pre-AMI electricity). That is covered in a separate ADR.

## Consequences

- HA restarts are fast — no heavy API calls on startup after initial setup.
- User gets a clear notification if backfill fails (repair issue).
- The `statistics_imported_through` watermark serves double duty: tracking
  statistics import progress AND indicating backfill completion.
- Existing options flow retry action is reused — no new UI.
- If the user clears the ledger storage, the watermark resets, and backfill
  will trigger again on next restart.

## Alternatives Considered

### Always run backfill on restart (current behavior)

Rejected because it makes every restart slow and violates HA convention.

### Run backfill only if ledger is empty

Rejected because the ledger might have data but no statistics (interrupted
import). The watermark check is more precise — it tracks actual completion.

### Always skip backfill on restart, require manual trigger

Rejected because the first startup after config flow needs to run it
automatically. Manual-only would mean the user sees an empty Energy Dashboard
until they discover the options flow action.

### Use a separate "backfill completed" flag

Rejected because the watermark already serves this purpose. Adding a separate
flag would duplicate state and add migration complexity.
