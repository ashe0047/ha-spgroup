# Seed-Boundary Hour Gap in External Statistics

## Status

Needs implementation audit — verify the current Recorder projection and seed
boundary tests before classifying the reported gap as resolved or active.

## Authority

Informational issue record; active statistics behavior is governed by `adr/adr-provider-owned-external-statistics.md`.

## Issue

The external statistics table has a permanent 60-minute gap in the hourly
statistics for the hour that contains the `seed_effective_from` boundary. When
a user provides a manual meter reading at, e.g., 12:30 SGT, the histogram row
for 12:00-13:00 is never generated despite both 30-minute AMI buckets existing
(12:00-12:30 and 12:30-13:00). This causes a visible discontinuity in the
Energy dashboard and any feature that queries `recorder/statistics` over an
interval spanning that hour.

## Background

The integration accepts 30-minute AMI buckets into a persistent accumulation
ledger. For `latest_total` on the sensor entity, the boundary is correct:
buckets with `start < seed_effective_from` are excluded from the sum, and
buckets with `start >= seed_effective_from` are included. The sensor value
transitions cleanly across the seed boundary.

The external statistics code in `_electricity_statistics` groups 30-minute
buckets by hour and generates one cumulative `ProjectedStatistic` row per
complete hour. This row has:

- `start`: the hour start (e.g., 13:00)
- `period_start`: the hour start (13:00)
- `period_end`: the hour start + 1 hour (14:00)
- `state`: the cumulative meter state at the end of the hour, computed via
  `LedgerStatisticsIndex.state_at()` which correctly handles the boundary
  (`seed_value - sum_between(point, seed)` for pre-seed,
   `seed_value + sum_between(seed, point)` for post-seed)
- `delta`: the sum of all 30-minute bucket values in that hour

## Current Flow

```
_electricity_statistics()
  └─ group accepted buckets by hour_start (rounded to hour)
  └─ for each hour_start in sorted(by_hour):
       ├─ hour_end = hour_start + 1h
       ├─ if hour_start < seed_effective_from < hour_end:    ← LINE 564
       │     continue   ← SKIP the seed-spanning hour
       ├─ if not has_contiguous_fixed_range(start=hour_start, end=hour_end):
       │     continue   ← skip incomplete hours
       ├─ if not _has_seed_bridge(...):
       │     continue   ← LINE 941: also skips seed-spanning hour
       └─ generate ProjectedStatistic for this hour
```

The hour containing `seed_effective_from` is blocked at two independent
gates, each checking `hour_start < seed < hour_end` or `row_start < seed < row_end`.

## Impact

The statistics table has a permanent gap at the seed-boundary hour. For a
realistic seed of 2000 kWh at 12:30 SGT with ~0.6 kWh per 30-minute bucket,
a query from 10:00 to 16:00 returns:

| start | state | sum | period |
|-------|-------|-----|--------|
| 10:00 | 1998.461 | 0.000 | anchor |
| 11:00 | 1999.345 | 0.884 | 11-12 |
| (missing) | | | **12-13** |
| 13:00 | 2001.180 | 2.719 | 13-14 |
| 14:00 | 2001.730 | 3.269 | 14-15 |
| 15:00 | 2002.176 | 3.715 | 15-16 |

A downstream consumer (Energy dashboard, utility meter, or derived sensor)
sees `sum` jump from 0.884 directly to 2.719, losing 1.238 kWh of
consumption. The `state` column also jumps from 1999.345 to 2001.180,
creating a "teleported" increase of 1.835 kWh in one hour that does not
correspond to any real consumption pattern.

The gap is permanent — it can never be retroactively filled because the
skip is unconditional on every statistics backfill run.

## Root Cause

The skip was added as a conservative guard with the assumption that
cumulative statistics spanning the seed boundary would produce inconsistent
or non-monotonic state/sum values. This assumption is incorrect.

`LedgerStatisticsIndex.state_at()` already handles the boundary correctly:

```python
def state_at(self, at: datetime) -> Decimal:
    point = at.astimezone(SINGAPORE_TZ)
    seed = self.entry.seed_effective_from
    if point <= seed:
        return self.entry.seed_value - self.sum_between(point, seed)
    return self.entry.seed_value + self.sum_between(seed, point)
```

And `_electricity_statistics` uses `state_at(hour_end)` for the row's state,
not a naive sum — so the cumulative value at 13:00 is
`2000 + 0.583 = 2000.583`, which is monotonic with the 11:00 state of
`2000 - (0.655 + ...) = 1999.345`.

## Proposed Fix

### Changes

Two locations in `statistics_backfill.py`:

**1. `_electricity_statistics` (line 564)**

Replace the blanket `continue` with a targeted contiguity check across the
seed boundary:

```python
# Before:
if hour_start < entry.seed_effective_from < hour_end:
    continue

# After:
if hour_start < entry.seed_effective_from < hour_end:
    # The seed bisects this hour.  Only generate a row when both
    # pre-seed and post-seed halves of the hour have contiguous buckets,
    # so the cumulative chain (seed - pre_sum) → (seed + post_sum) is
    # continuous.
    if not _has_indexed_contiguous_range(
        index,
        start=hour_start,
        end=entry.seed_effective_from,
        step=_HALF_HOUR,
    ):
        continue
    if not _has_indexed_contiguous_range(
        index,
        start=entry.seed_effective_from,
        end=hour_end,
        step=_HALF_HOUR,
    ):
        continue
```

**2. `_has_seed_bridge` (line 941)**

The same two-sided check replaces the blanket `return False`:

```python
# Before:
if row_start < seed < row_end:
    return False

# After:
if row_start < seed < row_end:
    if not _has_indexed_contiguous_range(
        index, start=row_start, end=seed, step=step
    ):
        return False
    if not _has_indexed_contiguous_range(
        index, start=seed, end=row_end, step=step
    ):
        return False
    return True
```

### What changes

- One additional `PlannedStatistic` row is generated per statistics run for
  the seed-boundary hour (if both halves are available)
- The `imported_through` cursor advances past this hour, preventing
  re-imports
- Statistics queries across the seed boundary return a continuous monotonic
  chain with no gaps

### What does NOT change

- `latest_total` computation on `SpGroupLedgerEntry` — unchanged, already
  correct
- `state_at()` — unchanged, already handles the boundary
- Sensor `native_value` — unchanged, reads from `latest_total`
- Water statistics — separate code path, no hour grouping involved
- External statistics `_planned_statistics` — already uses
  `_positive_projected_rows` which handles any number of sorted projected
  rows correctly
- Anchor row generation — unchanged; `first` is still the earliest projected
  row
- The 13:00-14:00 and later hours — their contiguity checks still start from
  `seed`, unchanged behavior
- Any hour that does NOT have data on both sides of the seed — still skipped,
  no partial-row generation

### What should be true after the fix

Given the same data (2000 seed at 12:30, 0.655 pre-seed + 0.583 post-seed in
the 12-13 hour):

| start | state | sum | period |
|-------|-------|-----|--------|
| 10:00 | 1998.461 | 0.000 | anchor |
| 11:00 | 1999.345 | 0.884 | 11-12 |
| **12:00** | **2000.583** | **2.122** | **12-13** |
| 13:00 | 2001.180 | 2.719 | 13-14 |
| 14:00 | 2001.730 | 3.269 | 14-15 |
| 15:00 | 2002.176 | 3.715 | 15-16 |

Both `state` and `sum` increase monotonically. The `state` at 12:00
(2000.583) is continuous with 11:00 (1999.345 → +1.238 hour delta = 2000.583
→ +0.597 13:00 bucket = 2001.180 at 13:00). The `sum` goes 0.000 → 0.884 →
2.122 → 2.719, each step reflecting real consumption.

### Risk assessment

**Low.** The fix only removes a guard that was more conservative than
necessary. The underlying `state_at()` computation already handles the
boundary correctly; the guard was preventing it from running on the one hour
where it would produce a useful result. The two-sided contiguity check
ensures no partial-row artifacts. Re-importing statistics after the fix
handles the previously-missing row naturally — `_filter_conflicts` sees no
existing row at start=12:00, imports it, and the chain is complete.
