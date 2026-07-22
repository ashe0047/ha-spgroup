# Statistics Backfill Full Coverage Hardening Plan

## Summary

The current statistics backfill tests are not complete enough for the core
feature. They catch the observed water negative-consumption bug, but the
electricity tests can still hide the same baseline problem by starting the
visible assertion window after the synthetic baseline row.

The corrected target is feature-level coverage:

- every imported statistics sequence must produce non-negative Energy
  Dashboard-visible consumption deltas;
- accepted source bucket deltas must appear in the correct period;
- negative absolute `sum` values remain allowed only when consecutive visible
  deltas remain correct;
- electricity hourly, water monthly, forced post-reference retry, and real
  Recorder-backed paths must all be tested.

## Grounding

- Home Assistant long-term statistics store hourly rows in `statistics`; for
  metered sensors, `state` is the sensor state at the end of the hour and `sum`
  is the accumulated total since statistics were first compiled.
- HA `total_increasing` sensors represent monotonically increasing usage;
  decreases are special/reset behavior and must not be introduced accidentally.
- Recorder-backed history and dashboard surfaces read from Recorder data, so at
  least one regression must use HA's public statistics import/query APIs rather
  than only mocked planner rows.

## Root Cause Of The Test Gap

The prior helper accepted a caller-provided `pre_window_sum`. That is useful for
some narrow assertions, but it lets tests choose a safe baseline and therefore
hide the exact failure users see in the Energy Dashboard.

The electricity test did this:

- imported a synthetic baseline row at `2026-07-14T00:00+08:00` with
  `sum=-2`;
- started the visible assertion at `2026-07-14T01:00+08:00`;
- passed `pre_window_sum=-2`;
- therefore only proved the next row had a positive delta.

It did not prove that the visible synthetic baseline would not show negative
consumption if the dashboard range included it.

## Correct Test Oracle

Add stricter helpers:

- `assert_dashboard_deltas_from_zero(rows, visible_start, visible_end, expected)`
  for a dashboard window with no earlier Recorder row.
- `assert_dashboard_deltas_from_rows(rows, visible_start, visible_end, expected)`
  where the pre-window baseline is derived from actual rows before
  `visible_start`.
- `assert_no_negative_dashboard_deltas_across_windows(rows, candidate_windows)`
  to run broad windows that include:
  - before the first imported row;
  - exactly at the synthetic baseline;
  - the first projected source row;
  - the native reference row;
  - after the native reference.

The default correctness assertion must derive deltas from the row sequence, not
from hand-picked favorable values.

## Coverage Matrix

Every row below must have an explicit test or a documented unreachable reason.

| Area | Required coverage |
| --- | --- |
| No Recorder / malformed ledger | no import, no metadata advancement, no healthy retry scheduling |
| Missing entity registry entry | skips that utility without affecting another utility |
| No accepted buckets | no projected rows, no import, bounded warning/no-op |
| Missing bridge | no affected rows imported; warning does not become a hard blocker |
| No native reference | waits, imports nothing, metadata records waiting state |
| Valid native reference | imports deterministic rows and freezes reference metadata |
| Persisted reference | timestamp and datetime row shapes work for electricity and water |
| Exact existing rows | idempotent, no duplicate import, metadata advances only through verified rows |
| Conflicting rows | import stops, conflict warning/Repair, metadata does not falsely advance |
| Existing decreasing sums | blocks import for electricity and water, creates conflict Repair |
| Electricity pre-reference | hourly rows from 30-minute buckets; broad visible windows never go negative |
| Water pre-reference | monthly rows; broad visible windows never go negative |
| Post-reference forced retry | imports missed deterministic rows for electricity and water without provider calls |
| Mixed utility entry | one utility waiting/conflicting/succeeding does not corrupt the other utility |
| Real Recorder API | water live bug and electricity baseline-visible bug fail through public HA APIs |
| Metadata | warnings are replaced per run; reference freezes; watermark is progress, not proof |

## Required Red Tests Before Production Fix

Add these tests before changing `statistics_backfill.py`.

1. `test_statistics_backfill_electricity_visible_baseline_not_negative`
   - Use accepted 30-minute electricity buckets before a native reference.
   - Start the visible window before the synthetic baseline.
   - Derive pre-window sum from rows or zero.
   - Assert no negative visible delta.
   - This should fail against the current implementation.

2. `test_statistics_backfill_electricity_live_shape_first_hour_not_negative`
   - Use multiple hourly electricity projections.
   - Include native reference after the imported history.
   - Assert first visible hour is not negative and later hourly deltas match
     accepted bucket sums.

3. `test_statistics_backfill_electricity_deltas_from_real_recorder_rows`
   - Use temporary SQLite Recorder.
   - Import the native reference through HA's public statistics API.
   - Run SP Group statistics import.
   - Query `statistics_during_period`.
   - Assert broad-window deltas are non-negative and match source values.

4. Strengthen existing water tests
   - Assert every month from the first visible month through native reference.
   - Months without accepted source buckets must have zero or no consumption,
     never negative.
   - Keep the live `2025-08` through `2026-06` fixture totaling `244.4`.

5. Add water branch coverage
   - Persisted reference timestamp and datetime variants.
   - Exact existing rows are idempotent.
   - Conflicting rows stop import.
   - Existing decreasing sums create conflict Repair.
   - Missing bridge cases before and after the seed anchor.

6. Add cross-utility coverage
   - One utility imports while the other waits for native reference.
   - One utility conflicts while the other has importable rows.
   - Metadata updates remain per sensor key.

7. Add forced retry parity
   - `force=True` imports missed deterministic rows after the frozen reference
     for electricity and water.
   - `force=False` does not import post-reference rows.
   - Neither path calls provider/source-history backfill.

## Implementation Discipline

1. Add/adjust tests first and run
   `mise exec -- python -m pytest tests/components/sp_group/test_recorder_statistics.py -vv`.
2. Confirm at least water and electricity baseline-visible regressions fail.
3. Only then change production statistics planning.
4. Re-run the focused suite until green.
5. Run:
   - `mise exec -- python -m pytest tests/components/sp_group/test_init.py tests/components/sp_group/test_coordinator.py -q`
   - `mise exec -- python -m pytest tests/components/sp_group -q`
   - `mise run python:test`
   - `mise run python:lint`
   - `mise run ha:check`

## Delegation Model

Future implementation should be split into separate review lanes:

- HA semantics lane: validates dashboard delta invariants and official HA
  statistics assumptions.
- Utility matrix lane: validates electricity 30-minute, water monthly, seed
  overlap, and future hybrid electricity-history cases.
- Execution path lane: validates native reference, force retry, conflict,
  idempotency, metadata, and real Recorder paths.

The final gate is not simply "tests pass". The final gate is that every row in
the coverage matrix has an explicit passing test or an explicit documented
reason that the path is unreachable.
