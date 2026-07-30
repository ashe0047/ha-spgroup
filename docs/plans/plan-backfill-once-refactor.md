# Refactor Plan: Backfill Runs Once, Then Repair on Failure

## Status

Implemented pending final audit — source-backfill gating and repair handling
are present in the runtime; verification references should be kept current.

## Authority

Implementation plan; backfill behavior is governed by `adr/adr-backfill-once-then-repair.md`.

## Summary

Stop running the historical backfill on every HA restart. Run it only once
after config flow completion. If it fails, create a HA repair issue. The user
retries through the existing options flow action.

## Current State

`async_setup_entry` always calls `async_start_initial_backfill(hass, entry)`
on every config entry load (initial setup AND every restart). The backfill
makes heavy API calls (10-30s × 2 for AMI) even when the ledger and
statistics already have complete data.

## Key Changes

### 1. `__init__.py` — Gate backfill on completion watermark

Add a check before `async_start_initial_backfill`: if ALL seeded ledger
entries have a non-None `statistics_imported_through`, skip the backfill.

```python
# In async_setup_entry, before the backfill call:
_backfill_needed = False
if runtime.ledger_store is not None:
    for entry in runtime.ledger_store.snapshot.entries.values():
        if entry.statistics_imported_through is None:
            _backfill_needed = True
            break
if _backfill_needed:
    async_start_initial_backfill(hass, entry)
```

Also add `_async_create_backfill_failed_issue` and
`_async_delete_backfill_failed_issue` following the same pattern as the
existing `_async_create_malformed_ledger_issue`.

### 2. `backfill.py` — Create repair issue on failure

In `_run_backfill_with_retry`, if the backfill fails after all retries,
create a repair issue via `hass.repairs.async_create_issue()`.

```python
# After all retries exhausted:
hass.repairs.async_create_issue(
    DOMAIN,
    "backfill_failed",
    is_fixable=True,
    severity=RepairIssueSeverity.WARNING,
    translation_key="backfill_failed",
    translation_placeholders={
        "error": str(err) if err else "Backfill failed after 3 attempts",
    },
)
```

On successful completion, delete any existing `backfill_failed` repair issue.

### 3. `config_flow.py` — Retry action clears repair

The existing retry backfill action (in options flow) should also delete the
`backfill_failed` repair issue on successful completion. This might already
work if the repair is cleared by the backfill function itself.

### 4. `strings.json` / `translations/en.json` — Repair copy

Add translation entries for the repair issue:

```json
"repair": {
    "backfill_failed": {
        "title": "SP Group historical data backfill failed",
        "description": "The initial backfill of historical SP Group data failed.\n\nError: {error}\n\nGo to the SP Group integration options to retry."
    }
}
```

## Files Affected

| File | Change |
|------|--------|
| `custom_components/sp_group/__init__.py` | Gate backfill on watermark; add repair issue functions |
| `custom_components/sp_group/backfill.py` | Create/delete repair issue on failure/success |
| `custom_components/sp_group/strings.json` | Repair issue copy |
| `custom_components/sp_group/translations/en.json` | Repair issue copy |

## Test Plan

- New entry (no watermark): backfill runs
- Existing entry with watermark: backfill skipped
- Backfill success: no repair issue
- Backfill failure after retries: repair issue created
- Options flow retry after failure: repair issue deleted on success
- Regular polling still works after backfill is skipped

## Edge Cases

- **Partial watermark**: Some utilities have watermark, others don't →
  backfill runs (handled by per-entry check)
- **Mid-backfill restart**: Watermark partially updated → backfill resumes
  from last completed row (existing behavior, unchanged)
- **Ledger malformed**: Already handled by existing malformed-ledger repair
  issue (unchanged)
- **No ledger store**: Guard against None (existing pattern)
- **Options flow retry before initial backfill completes**: Race condition
  guarded by existing `backfill_tasks` set and coordinator state

## Verification

- `python -m pytest tests/components/sp_group -q`
- `mise run ha:dev:cached-session:reset`, complete config flow → backfill runs
- Restart HA → backfill skipped, data still visible
- Simulate backfill failure → repair issue appears in Settings → Repairs
- Retry through options → repair issue cleared, data appears
