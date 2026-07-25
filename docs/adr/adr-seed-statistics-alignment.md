# ADR: Seed-Statistics Alignment for Dual-Source Discrepancies

## Status

Accepted - 2026-07-24

## Context

The SP Group integration has two independent sources of truth for consumption
data:

1. **The sensor seed value** — the actual meter reading entered by the user at
   setup time (`seed_effective_from`). This is the authoritative record of the
   current cumulative consumption as measured by the physical utility meter.
   It is not subject to provider data lag or API availability.

2. **The provider API data** — historical consumption records returned by the
   SP Group API, used for source backfill and external statistics. These are
   subject to provider data lag (the API may not return data up to the present
   moment), API availability (transient 5xx errors, timeouts), and provider
   retention limits (data may only extend back to a certain date).

At the `seed_effective_from` boundary, the sensor value (`seed`) and the
total cumulative sum from provider data will inevitably differ because they
measure different realities. This ADR documents the two distinct discrepancy
scenarios and how the `state_at()` method in
`custom_components/sp_group/statistics_backfill.py` handles them.

## Discrepancy Scenarios

### Scenario A: Baseline Consumption Gap (Pre-Provider Data)

Occurs when the provider's historical data coverage does not extend back to
the account's inception. Consumption occurred before the provider's earliest
available data point but is reflected in the meter reading (seed).

```
Meter timeline:
[─── invisible consumption ───] [─── provider data window ───] [─── post-seed ───]
                                ↑                              ↑
                            first data point              seed_effective_from
                                                           (meter reads seed)
```

**Example (realistic):**
- Provider data starts: June 3, 2026
- Provider cumulative at seed_time: 1085.69 kWh
- Seed (meter reading) at seed_time: 2000.00 kWh
- Baseline gap: 2000.00 - 1085.69 = 914.31 kWh (consumption before June 3)

This gap is not recoverable from the provider API — it represents real
consumption that predates available records. The `state_at` calculation
subtracts only what the provider returned, leaving the remainder as the
baseline:

```
state_at(June 3) = 2000 (seed) - 1085.69 (provider data) = 914.31
                                                             ↑
                                              invisible baseline
```

### Scenario B: Provider Lag Gap (Within the Historical Window)

Occurs when the provider API has not yet returned data for the most recent
period within the backfill window (e.g., the last few hours before
seed_effective_from). The meter at seed_time has already accumulated this
consumption, but the provider has not processed it yet.

```
Provider data: [─── June 3 to 00:00 Jul 23 ──] [LAG] [seed(01:00)]
                                                ↑      ↑
                                            last data  meter reads seed
                                            returned
```

**Example:**
- Last provider data point: July 23 00:00
- seed_effective_from: July 23 01:00
- The 00:00-01:00 consumption (e.g., 2.13 kWh) exists at the meter but has
  not been returned by the provider at backfill time.

This gap is temporary — it resolves when the provider processes and returns
the missing buckets on a subsequent poll.

## Resolution: state_at() Anchoring

Both scenarios are handled by the same `state_at()` method in
`LedgerStatisticsIndex`:

```python
def state_at(self, at: datetime) -> Decimal:
    point = at.astimezone(SINGAPORE_TZ)
    seed = self.entry.seed_effective_from
    if point <= seed:
        return self.entry.seed_value - self.sum_between(point, seed)
    return self.entry.seed_value + self.sum_between(seed, point)
```

### How it handles Scenario A (Baseline Gap)

For any point before `seed_effective_from` (including the first provider data
point), the formula is:

```
state_at(point) = seed - sum_between(point, seed)
```

`sum_between(point, seed)` is the sum of ALL accepted buckets between `point`
and `seed_time`. This includes only provider-recorded buckets. The subtraction
naturally leaves `seed - provider_cumulative` as the remainder — which IS
the invisible baseline consumption.

This works for any seed value >= provider cumulative: the subtraction
implicitly captures whatever baseline gap exists without needing to know
it explicitly. If the seed is smaller than provider cumulative (test
artifact), the result is negative, but the functional behavior (delta-based
Energy Dashboard calculations) remains correct.

### How it handles Scenario B (Provider Lag Gap)

At backfill time, the provider may not have returned data for the period
immediately before `seed_effective_from`. `state_at()` uses only the
currently accepted buckets in `sum_between()`. The missing buckets don't
exist yet, so they are not subtracted:

```
# At initial backfill (00:00-01:00 buckets missing due to lag):
state_at(seed_time - 1h) = seed - sum_between(..., seed)
                          = 2000 - sum(only buckets that exist)
                          = slightly inflated (missing buckets not subtracted)
```

This is a stale projection, but it is **self-correcting**. When the lagged
buckets are accepted by a subsequent poll:

1. The poll's accepted delta > 0 triggers a statistics retry
   (`_schedule_statistics_retry()` in `SpGroupUtilityCoordinator`)
2. The retry calls `async_run_statistics_backfill()` which re-projects ALL
   statistics from the current ledger snapshot
3. The new projection includes the previously missing buckets
4. `sum_between()` now includes them, so `state_at()` produces a corrected,
   lower value for pre-seed points
5. `async_import_statistics()` replaces the stale rows at the same `start`
   timestamps with corrected values

The retry always re-projects from the current ledger state — it never
accumulates stale offsets. This is what provides **eventual consistency**.

### Baseline and Provider Lag Can Coexist

A common edge case: both a baseline gap and provider lag exist simultaneously
at backfill time.

```
At backfill time (5 kWh lagging):
  sum_between(first_point, seed) = 1080 (actual=1085)
  state_at(first_point) = 2000 - 1080 = 920
  ↓
  async_import_statistics writes sum=920 at June 3 14:00
  ↓
  This 920 is a BLEND of:
    - True baseline: 2000 - 1085 = 915
    - Lag inflation: +5 (missing data not yet available)
```

When the lagged 5 kWh bucket arrives:
```
  Retry re-projects from updated ledger:
  sum_between(first_point, seed) = 1085 (now complete)
  state_at(first_point) = 2000 - 1085 = 915
  ↓
  async_import_statistics OVERWRITES June 3 14:00 row: sum=915
  ↓
  BASELINE CORRECTED — the old 920 is replaced
```

The temporary inflation never contaminates the baseline permanently. The
retry corrects it in one pass because `async_import_statistics` replaces
existing rows at matching `start` timestamps.

## Eventual Consistency: How It Self-Heals

The retry mechanism provides practical eventual consistency through this
chain:

```
accepted_delta > 0 during poll
  ↓
_schedule_statistics_retry() fires
  ↓
async_run_statistics_backfill() called
  ↓
_projections_statistics_with_warnings() re-projects from current ledger
  ↓
state_at() uses sum_between() with the now-complete bucket data
  ↓
_async_backfill_external() calls async_import_statistics() with corrected rows
  ↓
async_import_statistics() REPLACES existing rows at matching start timestamps
```

No sequential dependency required — the retry handles mixed pre-seed/post-seed
data regardless of arrival order. The sensor (`seed + post_seed`) and
statistics (`seed-anchored cumulative`) converge at the latest point after
each retry.

### Weak Points — When Eventual Consistency Can Break

The self-healing is not formally guaranteed. These are the gaps:

1. **Zero consumption lagged data never triggers retry**: The AMI parser
   filters buckets with value=0 (`value > 0` in `_electricity_ami_source_poll`).
   If the lagged bucket has zero consumption, it never becomes an accepted
   bucket, no `accepted_delta > 0` fires, and no retry is triggered. The
   statistics remain in the temporarily inflated state indefinitely.

2. **Provider never returns the data**: If the API persistently errors for
   a specific time range, the lagged data never arrives. The stats stay
   inflated with no TTL or timeout that marks the period as "assumed complete."

3. **Hourly Recorder event may skip retries**: The Recorder hourly event
   logs `statistics retry skipped after event: not_ready` when stats are
   waiting for a native reference. With external statistics, this check may
   suppress the automatic retry even when data is ready to import.

4. **Correction depends on retry firing**: If no poll produces an
   `accepted_delta > 0` for an extended period (e.g., no new data), the
   retry never fires, and the inflated values persist until the next
   Recorder hourly event or manual intervention.


### Mid-Window Gap Scenario (Unlikely)

An earlier draft of this ADR identified an additional concern: if post-seed normal polling
recovered lagged pre-seed data from a DIFFERENT DATE than the cursor, the gap could not
self-heal because the normal poll only requests the cursor's date from the API. Mid-window
gaps (data available at the start and end of the window but missing in the middle) would
also be unrecoverable.

**This scenario is unlikely in practice** because the SP Group provider processes data in
time-sequential order. If data for an earlier period has not been processed, data for a
later period on that same date has not been processed either. The available data forms a
contiguous prefix: `[available] [pending provider processing]`.
A hole where earlier data is missing but later data exists would require out-of-order
processing, which the provider is not observed to do.

**Status:** This scenario has not been tested or verified against the live provider API.
The time-ordered processing assumption is based on observed provider behavior, not on
documented guarantees. If the provider changes their processing pipeline, this assumption
may break.
These weak points mean that in edge cases, the statistics may remain
slightly inflated indefinitely. A periodic reconciliation sweep (e.g.,
checking for unresolved discrepancies and forcing a retry) would close
these gaps but is not yet implemented.

## Behavior at seed_effective_from

At the exact seed time, `state_at` returns precisely the seed value
regardless of provider data completeness:

```python
state_at(seed_effective_from) = seed
```

This guarantees the statistics cumulative sum equals the sensor value at
seed_time — the single point where both data sources have a well-defined
shared reference. Post-seed, both sensor and statistics grow by the same
accepted deltas, maintaining alignment.

## Negative vs Positive Discrepancy

The ADR assumes `seed >= pre_seed_cumulative` in production. When this
holds, `state_at(first_point) = seed - pre_seed` is positive and represents
the invisible pre-provider baseline.

If `seed < pre_seed_cumulative` (test artifact), `state_at(first_point)` is
negative. The Energy Dashboard remains unaffected because it uses `sum`
deltas, not absolute values. The raw statistics table would show negative
cumulative sums for early dates — functionally correct but cosmetically odd.

## Current Implementation Status

The `state_at()` method exists but is currently only used for the `state`
field of `PlannedStatistic`. The `sum` field is computed separately via
`running_sum` in `_positive_projected_rows()` (starting at 0), which ignores
the seed offset entirely. A pending fix will make `sum = state` so the
statistics cumulative sum uses the same seed-anchored calculation.

## Consequences

### Positive

- Both discrepancy scenarios are handled by a single, simple formula without
  branching or explicit gap detection.
- Scenario B self-recovers on the next statistics retry without user
  intervention.
- The seed remains the authoritative anchor; provider data is slotted
  relative to it.
- Energy Dashboard deltas are unaffected (seed offset cancels out in
  `sum(t2) - sum(t1)`).
- No sequential dependency required for correctness — retry handles mixed
  pre-seed/post-seed data regardless of arrival order.

### Negative

- If the seed is smaller than the provider cumulative (should not occur
  in production) the statistics sum may show negative values for early
  pre-seed dates.
- Scenario B creates a brief window where pre-seed statistics are slightly
  inflated until the next retry resolves them.
- Eventual consistency is practical, not formally guaranteed. The weak
  points (zero-value buckets, persistent API errors, skipped hourly events)
  can prevent self-healing in edge cases.

## Related

- `LedgerStatisticsIndex.state_at()` in `statistics_backfill.py`
- `SpGroupLedgerEntry.latest_total` in `ledger.py`
- `_positive_projected_rows()` in `statistics_backfill.py`
- `_electricity_statistics()` in `statistics_backfill.py`
- `_schedule_statistics_retry()` in `coordinator.py`
