# Refactor Plan: Phase A — Fast Discovery Setup

## Status

Implemented. `discovery_service.py` now parallelizes the independent discovery
and enrichment requests. The remaining text is retained as the original plan;
its API-failure statement applies to discovery, whose failures remain distinct
from utility polling provider no-op failures.

## Authority

Historical implementation plan; current discovery behavior is governed by the coordinator hierarchy ADR.

## Summary

Parallelize the API calls in `async_fetch_discovery_data` using `asyncio.gather`.
No other changes needed. Setup time drops from 5-15s to 3-8s.

## Current State

During `async_setup_entry`, line 97 awaits the discovery coordinator's
first refresh, which calls `_async_update_data` → `async_fetch_discovery_data`.
The current function makes 5+ sequential API calls:

```python
ygg_premises = await yggdrasil_client.get_premises_details(session)   # ~2-5s
jarvis_me = await jarvis_client.get_me(session)                        # ~1-3s
for acct in account_numbers:                                           # sequential
    meter_types[a] = await yggdrasil_client.get_meter_type(...)        # ~1-3s each
    premise_types[a] = await yggdrasil_client.get_premise_type(...)    # ~1-3s each
```

**Total: 5-15s blocking HA startup.**

## Changed Approach

The original plan proposed splitting discovery into core + deferred enrichment.
The adversarial review found that approach introduced:
- Race conditions between enrichment task and explicit refresh
- Coordinator state bypass (last_update_success, ConfigEntryNotReady)
- Silent failure on enrichment errors
- Disproportionate complexity for 1-3s marginal gain

**Revised approach**: Just parallelize the existing function. No deferred tasks,
no new runtime fields, no coordinator bypass. The improvement (5-15s → 3-8s)
comes from running independent API calls concurrently.

## Implementation (one file)

**File**: `custom_components/sp_group/discovery_service.py`

Replace sequential `await` calls with two `asyncio.gather` phases:

```python
async def async_fetch_discovery_data(...):
    # Phase 1: core data parallel
    ygg_premises, jarvis_me = await asyncio.gather(
        yggdrasil_client.get_premises_details(session),
        jarvis_client.get_me(session),
    )
    account_numbers = account_numbers_from_yggdrasil(ygg_premises)

    # Phase 2: enrichment parallel
    tasks = [
        asyncio.gather(
            yggdrasil_client.get_meter_type(session, acct),
            yggdrasil_client.get_premise_type(session, acct),
        )
        for acct in account_numbers
    ]
    results = await asyncio.gather(*tasks)

    meter_types = {}
    premise_types = {}
    for i, (mt, pt) in enumerate(results):
        acct = account_numbers[i]
        meter_types[acct] = mt
        premise_types[acct] = pt

    return build_discovery_data(
        account_id=account_id,
        yggdrasil_premises=ygg_premises,
        jarvis_me=jarvis_me,
        meter_types_by_account=meter_types,
        premise_types_by_account=premise_types,
        require_jarvis_correlation=require_jarvis_correlation,
    )
```

**No other files changed.** `__init__.py`, `coordinator.py`, and all other
files remain untouched. The setup flow still calls
`await discovery_coordinator.async_config_entry_first_refresh()` exactly as
before.

## Files Affected

| File | Change |
|------|--------|
| `custom_components/sp_group/discovery_service.py` | Replace sequential awaits with `asyncio.gather` |

## Test Plan

- All existing tests pass (no behavioral change, only parallelism)
- `python -m pytest tests/components/sp_group -q`
- `python -m pytest tests/components/sp_group/test_config_flow.py -vv`
- `mise run ha:dev:cached-session` and observe setup time

## Edge Cases

- **Zero accounts**: `asyncio.gather()` with empty list returns empty list,
  no enrichment calls made → correct
- **One account**: Both phases work with single task → correct  
- **API failures**: Propagate normally through same error handling as before
- **Multiple accounts**: Parallelized enrichment eliminates O(n) scaling → correct
