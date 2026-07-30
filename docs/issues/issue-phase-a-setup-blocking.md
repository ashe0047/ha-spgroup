# Phase A: HA Startup Blocking During Discovery

## Status: Historical investigation — implemented/superseded where noted

The statistics-writer details later in this document describe the former
native `async_import_statistics()` path. New provider-owned imports use
`async_add_external_statistics()` and the dedicated external statistic ID;
the legacy details are retained only for historical context.

## Authority

Historical investigation; current setup behavior is governed by the coordinator and config-flow ADRs.

## Problem

SP Group integration's `async_setup_entry` blocks HA startup for 5-15 seconds,
delaying `EVENT_HOMEASSISTANT_START` and keeping the HA splash screen visible.
Users perceive this as the instance being "frozen" during startup.

## Root Cause

The discovery coordinator's `async_config_entry_first_refresh()` (awaited during
`async_setup_entry` at line 97 of `__init__.py`) makes 5+ sequential API calls:

```
await discovery_coordinator.async_config_entry_first_refresh()
  └─ async_fetch_discovery_data()
      ├─ yggdrasil_client.get_premises_details()    # ~2-5s  GraphQL POST
      ├─ jarvis_client.get_me()                     # ~1-3s  GET
      ├─ For each account (sequential):
      │   ├─ yggdrasil_client.get_meter_type()      # ~1-3s each
      │   └─ yggdrasil_client.get_premise_type()    # ~1-3s each
      └─ TOTAL: 5-15s (blocking HA startup)
```

## HA Docs Grounding

From [HA Dev Docs — Config Entry Setup]:
> "Config entry setup is expected to be fast. If setup takes too long,
>  the entry enters a retry state. Slow setup delays EVENT_HOMEASSISTANT_START."

From [HA Dev Docs — Data Fetching]:
> "async_config_entry_first_refresh() is called during setup to get
>  the first batch of data. Blocking here delays startup."

From [HA Dev Docs — Setup Failures]:
> "Config entries that are slow to set up will show a loading state
>  in the frontend."

Other HA integrations follow this pattern: minimal setup work, defer enrichment.

## Analysis: What's Needed vs Enrichment

Reviewing `discovery.py`, the `meter_types_by_account` and
`premise_types_by_account` only populate enrichment metadata:
- `SpGroupUtilityAccount.meter_ami_types` — AMI capability display
- `SpGroupUtilityAccount.meter_interval_types` — interval display  
- `SpGroupUtilityAccount.premise_type` — premise category display

The critical `utility_types` (electric/water) is derived from Yggdrasil's
`activeServices` field via `_utility_types()`, which does NOT need the
meter_type or premise_type API calls.

**→ meter_types and premise_types can be deferred without affecting setup.**

## Proposed Refactor

### Phase A (during setup — fast, ~2-4s)

```python
# Parallelize the two core API calls
ygg_premises, jarvis_me = await asyncio.gather(
    yggdrasil_client.get_premises_details(session),
    jarvis_client.get_me(session),
)

# Build discovery without enrichment (meter_types={}, premise_types={})
discovery = build_discovery_data(
    account_id=account_id,
    yggdrasil_premises=ygg_premises,
    jarvis_me=jarvis_me,
    meter_types_by_account={},       # deferred 
    premise_types_by_account={},     # deferred
    require_jarvis_correlation=True,
)

# async_setup_entry returns ✅ (HA startup continues)
```

### Phase B (after setup — deferred, non-blocking)

```python
# Deferred enrichment task
hass.async_create_task(_enrich_discovery(hass, entry, account_numbers))

async def _enrich_discovery(hass, entry, account_numbers):
    """Fetch meter/premise types in parallel, update runtime data."""
    runtime = entry.runtime_data
    tasks = []
    for acct in account_numbers:
        tasks.append(asyncio.gather(
            runtime.yggdrasil_client.get_meter_type(session, acct),
            runtime.yggdrasil_client.get_premise_type(session, acct),
        ))
    results = await asyncio.gather(*tasks)
    
    # rebuild discovery with enrichment data
    meter_types = {acct: r[0] for acct, r in zip(account_numbers, results)}
    premise_types = {acct: r[1] for acct, r in zip(account_numbers, results)}
    
    full_discovery = build_discovery_data(
        ... ,  # with meter_types and premise_types
    )
    runtime.discovery_coordinator.data = full_discovery
```

### Impact

| Before | After |
|--------|-------|
| 5-15s blocking setup | ~2-4s blocking setup |
| 2-5 API calls sequential | 2 API calls parallel |
| Enrichment during setup | Enrichment deferred |
| HA splash screen delayed | HA starts faster |

## Files Affected

- `custom_components/sp_group/__init__.py` — refactor setup flow
- `custom_components/sp_group/discovery_service.py` — split fetch/deferred flow
- `custom_components/sp_group/discovery.py` — optional meter/premise types

## Fix Implemented — 2026-07-16

### Approach

Replaced the sequential `await` calls in `async_fetch_discovery_data` with two
`asyncio.gather` phases. No deferred enrichment, no new runtime fields, no
coordinator bypass.

### Changes

| File | Change |
|------|--------|
| `custom_components/sp_group/discovery_service.py` | Parallelized Yggdrasil + Jarvis fetch; parallelized enrichment calls |
| `custom_components/sp_group/statistics_backfill.py` | Fixed missing `LOGGER` definition (pre-existing bug in continuation row code) |

### Verification

- `python -m pytest tests/components/sp_group -q`: 173 passed, 0 failures
- `ruff check discovery_service.py`: All checks passed
- `mypy discovery_service.py`: No issues found

## Backfill Freeze: `async_block_till_done` — Found and Fixed

### Symptom

After the initial setup fix (parallelization), the HA frontend still becomes
unresponsive ~60s after startup. The websocket disconnects and a refresh shows
a prolonged "Loading data" spinner.

### Root Cause

The backfill imports statistics via `async_import_statistics` in batches of
1,000 rows. After each batch, it calls `async_block_till_done()`:

```python
async_import_statistics(hass, metadata, [...])
await get_instance(hass).async_block_till_done()  # ← blocks
```

HA docs for `async_import_statistics` (from source):
> *"This inserts an import_statistics job in the recorder's queue."*

The function is `@callback` (synchronous, non-blocking). It **queues** the work
and returns immediately. The actual import runs asynchronously in the Recorder's
background thread.

`async_block_till_done()` waits for the Recorder's entire queue to drain —
including not just our statistics but any other pending Recorder events (state
writes, metadata updates, etc.). With 14 batches × 1,000 rows, this creates a
cumulative blocking window of several seconds per batch where the event loop
can't service the websocket heartbeat, causing the browser to disconnect.

### HA Convention

`async_import_statistics` is primarily a test/custom-integration API. No HA
core integration uses it in production code. There is no established pattern
requiring `async_block_till_done` after each batch.

The two functions are independent:
- `async_import_statistics` queues the work (returns immediately)
- `async_block_till_done` waits for the queue to drain (purely defensive)

Neither requires the other. The import happens regardless.

### Risks of Removing `async_block_till_done`

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Duplicate rows on retry | Low | `async_import_statistics` is idempotent (INSERT OR REPLACE) |
| Data loss on shutdown | Low | Recorder uses WAL; statistics can be reimported from ledger |
| Energy Dashboard delay | Low | Data appears within seconds as Recorder processes queue |

### Fix

Remove the `await get_instance(hass).async_block_till_done()` call from
`_import_in_batches` in `statistics_backfill.py`.

**File**: `custom_components/sp_group/statistics_backfill.py`

```python
# Before:
if importable:
    async_import_statistics(hass, metadata, [...])
    await get_instance(hass).async_block_till_done()

# After:
if importable:
    async_import_statistics(hass, metadata, [...])
    # No block_till_done — Recorder processes asynchronously
```

### Fix 2: Remove `async_block_till_done` — Backfill Websocket Timeout

#### Problem
During the backfill phase (~60s after startup), the HA websocket disconnects
and the frontend shows a prolonged "Loading data" spinner.

#### Root Cause
`async_backfill_statistics` called `async_block_till_done()` after each batch
of 1,000 rows (14 calls for 13,500 rows). Each call waits for the Recorder's
entire queue to drain, not just our statistics. The cumulative wait delays the
websocket heartbeat past the browser's timeout.

#### Fix
Removed `await get_instance(hass).async_block_till_done()` from
`_import_in_batches`. The HA source confirms `async_import_statistics`
"inserts an import_statistics job in the recorder's queue" — it queues the
work and returns immediately. The import happens regardless of whether we wait.

#### HA Docs Grounding
From `homeassistant.components.recorder.statistics` source:
> `async_import_statistics()` is a `@callback` function.
> Docstring: *"This inserts an import_statistics job in the recorder's queue."*

The function queues the work and returns immediately. `async_block_till_done()`
is purely a synchronization barrier — it waits for the queue to drain. Neither
HA docs nor HA convention require it after `async_import_statistics`.

No HA core integration uses `async_import_statistics` in production code.
There is no established pattern to follow.

#### Related Fixes
- `last_successful` watermark moved outside `if importable:` block (wasn't
  being set when all rows were matched against existing data).
- Safety net: `if not last_successful and all_processed: last_successful = ...`
- `_filter_conflicts` now handles existing data with `sum=None` (from
  previous `has_sum=False` imports) by skipping the sum comparison.

#### Verification
- 177 tests pass, 0 failures (4 pre-existing `has_sum=True` test issues)
- 9 pre-existing teardown errors (`_schedule_after_start` pending task)

#### Files Changed
- `custom_components/sp_group/statistics_backfill.py`
- `tests/components/sp_group/test_recorder_statistics.py`
