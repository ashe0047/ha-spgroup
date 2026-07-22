# ADR: Polling Window Starts at Last Accepted Bucket End

## Status

Proposed — 2026-07-21

## Context

The electric coordinator's polling window determines which time range of AMI
data is fetched on each poll. The current implementation uses `source_poll_cursor_start`
which applies a 2-hour overlap:

```python
# Current (simplified):
pending_start - 2h    # when pending buckets exist
accepted_end - 2h     # when accepted buckets exist
seed_effective_from   # when no buckets exist
```

This 2-hour overlap was originally introduced to cover two concerns:

1. **Boundary safety**: Re-fetch data at the edge of the accepted range to
   catch any 30-minute buckets that might have been partially written or missed
   on the previous poll.
2. **Two-sighting rule support**: The overlap ensures the second poll's window
   includes the same data as the first poll's pending data, allowing the
   two-sighting rule to promote pending to accepted.

### The overlap is unnecessary

After thorough evaluation, the 2-hour overlap was found to be unnecessary for
both concerns:

**Concern 1 (Boundary safety):**

- The AMI API is queried by **date** (`_date_for_api` at `coordinator.py:795`),
  not by precise time window. Two polls 5 minutes apart return the same
  date-range data. The `_observations` filter `start < backfill_from` already
  excludes already-accepted data.
- The `end >= now` filter independently handles provisional data near the
  current time. This is the true data-lag safeguard, not the overlap.
- SP Group AMI data for completed 30-minute periods is authoritative and
  stable (regulated smart meter utility). No evidence of late corrections.

**Concern 2 (Two-sighting rule):**

- During catch-up, both polls start from the same cursor (`pending_start` or
  `accepted_end`). They fetch the same date-range data because the API is
  date-based. Pending observations are correctly matched and promoted on the
  second poll regardless of the overlap.
- After catch-up, the cursor naturally advances to `pending_start` (checked
  first) or `accepted_end`. No re-fetching of accepted data is needed.
- The two-sighting rule's matching is based on `bucket_key` equality, not on
  the fetch window range.

### The overlap causes problems

The 2-hour overlap introduces a subtle cursor advancement issue. During
catch-up, after pending is promoted to accepted:

```python
# With overlap:
accepted_end = seed + 30min
cursor = max(seed, accepted_end - 2h) = max(seed, seed - 1.5h) = seed
# Cursor is STUCK at seed. Next poll re-fetches the same data.

# Without overlap:
cursor = accepted_end = seed + 30min
# Cursor advances. Next poll fetches new data.
```

The overlap pulls the cursor backward by 2 hours from `accepted_end`,
effectively canceling forward progress. This means the first 2 polls after
any catch-up event re-fetch the same large window before the cursor finally
advances to steady-state.

Additionally, in steady-state polling, the overlap causes a constant
re-fetching of 2 hours of already-accepted data on every poll — wasteful API
calls that serve no purpose.

### Trace of the proposal

**Proposed cursor** (no overlap):

```python
pending_start = min((bucket.start for bucket in self.pending), default=None)
if pending_start is not None:
    return max(self.seed_effective_from, pending_start)
accepted_end = max((bucket.end for bucket in self.accepted), default=None)
if accepted_end is not None:
    return max(self.seed_effective_from, accepted_end)
return self.seed_effective_from
```

**Trace with two-sighting rule:**

| Poll | State | Cursor | Window | Result |
|---|---|---|---|---|
| 1st | No buckets | `seed` | `[seed, now]` | → pending |
| 2nd | Pending at `seed` | `seed` | `[seed, now]` | → promoted + new pending |
| 3rd | New pending at `T1` | `T1` | `[T1, now]` | → promoted + new pending |
| 4th+ | Incremental | advances | `[cursor, now]` | → steady-state |

The cursor naturally advances forward with each poll, never re-fetching
already-accepted data. In steady-state, `accepted_end ≈ now`, so the window
is `[now, now]` (empty) until new data matures past the `end >= now` filter.

**Trace without two-sighting rule** (for reference):

| Poll | State | Cursor | Window | Result |
|---|---|---|---|---|
| 1st | No buckets | `seed` | `[seed, now]` | → accepted immediately |
| 2nd | Accepted to `T1` | `T1` | `[T1, now]` | → accepted immediately |
| 3rd+ | Incremental | advances | `[cursor, now]` | → steady-state |

### Edge cases verified

All edge cases were traced and verified to have no bugs, no data loss, and no
inconsistencies:

| Scenario | Result |
|---|---|
| Two-sighting rule interaction | ✅ Works — API is date-based, same date data returned |
| Cursor advancement during catch-up | ✅ Advances by `pending_start`, not pulled back |
| 24h downtime catch-up | ✅ Catch-up completes in 2 polls |
| Steady-state incremental | ✅ Empty window when caught up, no re-fetching |
| Boundary bucket at `accepted_end` | ✅ Clean — `start < backfill_from` excludes already-accepted |
| Water coordinator | ✅ Unaffected — uses base `_request_start`, not cursor |
| API data lag | ✅ Handled by `end >= now` filter independently |
| Instance restart with persisted ledger | ✅ Loads from persistence, cursor derives from accepted |

## Decision

Remove the 2-hour overlap from `source_poll_cursor_start`. The cursor should
return the exact boundary — `pending_start`, `accepted_end`, or
`seed_effective_from` — without subtracting the overlap.

### Changes required

1. **`custom_components/sp_group/ledger.py`**: Remove the `overlap` parameter
   from `source_poll_cursor_start` and the subtraction in all branches.

2. **`custom_components/sp_group/coordinator.py`**: Update the electricity
   coordinator's `_request_start` to pass `overlap=timedelta()` (no overlap).
   Alternatively, remove the overlap entirely from the method signature.

### What stays the same

- The two-sighting rule (`require_two_sightings=True`) is unchanged.
- The `end >= now` filter in `_observations` is unchanged.
- The polling interval (DurationSelector) is unchanged.
- The water coordinator is unchanged.
- The data backfill into statistics is unchanged.

## Consequences

### Positive

- No re-fetching of already-accepted data on any poll.
- Cursor advances naturally forward during catch-up.
- Simpler, more predictable logic.
- Fewer API calls (no overlap re-fetching).

### Negative

- Theoretical loss of boundary safety if the API ever returns incomplete data
  for a completed time period. Mitigated by:
  - API is authoritative (regulated utility).
  - Two-sighting rule provides independent protection.
  - If a bucket is missed, it will be included in the next date-range query
    (because the API is date-based, not window-based).

### Neutral

- The `overlap` parameter is removed from `source_poll_cursor_start`. Callers
  that pass an overlap value will need to be updated.
- The `_ELECTRICITY_NORMAL_POLL_OVERLAP` constant becomes unused and can be
  removed in a cleanup pass.

## Compliance

- HA convention: The `DataUpdateCoordinator` model is preserved. The cursor
  derivation remains inside the ledger store, not in the coordinator.
- No SDK request shapes, entity IDs, or dashboard metadata change.
- No config entry migration required.
- No statistics metadata schema change.
