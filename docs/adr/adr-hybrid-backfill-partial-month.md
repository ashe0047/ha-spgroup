# ADR: Hybrid Electricity Backfill — Partial AMI Month Handling

## Status

Locked — revised 2026-07-26

## Amendment History

This ADR replaces the 2026-07-16 decision to use one monthly history bucket
and discard AMI for a partial transition month. That approach is not compatible
with the current electricity statistics projection, which requires a
contiguous 30-minute source timeline to create hourly Recorder statistics.

## Context

The SP Group integration backfills historical electricity data from two API
sources:

1. **AMI charts endpoint** (`POST /jarvis/v3/ami/charts`) — 30-minute smart
   meter consumption buckets.
2. **History charts endpoint** (`GET /jarvis/v4/charts/{premise_id}`) —
   monthly billed-consumption evidence. This endpoint has no date query
   parameters.

For an overlapping transition month, AMI can begin partway through the month
when a smart meter is installed. The history endpoint provides the full-month
total, while AMI provides the observed post-installation half-hours.

Live validation for June 2026 established the following for the same Jarvis
premise:

| Evidence | Value |
| --- | --- |
| AMI timestamps | 1,440 complete June half-hour slots; no missing timestamps |
| AMI prefix | 124 zero slots / 62 hours, from 1 June 00:00 until AMI begins at 3 June 14:00 +08:00 |
| Observed AMI suffix | 1,316 positive buckets totaling 588.8723 kWh |
| History total | `Actual`, `kWh`, `consumption.previous = 685.07` for the June row |
| Historical remainder | 685.07 − 588.8723 = 96.1977 kWh |

The history response uses a timestamp-formatted period in this case
(`2026-06-01T00:00:00+08:00`) and has `consumption.current = 0`. The
historical-total field must therefore be selected through a validated
response-shape rule; the existing current-only electricity history parser is
not sufficient for this backfill path.

## Decision

**Select one source strategy per calendar month, with AMI taking precedence
whenever it has usable coverage.**

| AMI evidence for a closed month | History evidence | Selected result |
| --- | --- | --- |
| Full AMI coverage | Present or absent | Use AMI only; do not add or reconcile a history bucket. |
| No usable AMI coverage | Valid `Actual` history total | Use history only as one `history_monthly` bucket. AMI absence includes no AMI rows or only zero/placeholder rows; a complete zero AMI grid is not required. |
| Partial AMI coverage | Valid `Actual` history total | Retain AMI and backfill only its missing leading prefix from the calculated history remainder, as specified below. |
| Partial AMI coverage | Missing or invalid history total | Accept AMI-only and represent permitted gaps as explicit zero buckets; terminal trailing provider lag is the only unresolved state. |

AMI is therefore authoritative whenever it has observed monthly coverage. The
history endpoint is used alone only when AMI has no usable coverage for that
month; it is never added on top of full AMI data. For a partial month, history
is not an alternative source: it is evidence used solely to calculate the
amount allocated into the missing AMI prefix.

For a validated partial month:

1. Fetch and normalize both AMI and history evidence for the same direct
   Jarvis premise mapping.
2. Validate that the history row is closed, `Actual`, in a supported unit, and
   exposes a provider-validated full-month total.
3. Validate that AMI rows are structurally usable and place any omitted
   intervals on the expected 30-minute grid as leading, middle, or trailing
   gaps. Only malformed values, duplicate timestamps, unsupported units, or an
   otherwise untrusted response are invalid source data.
4. Compute:

   ```text
   historical_remainder = historical_month_total − observed_ami_month_total
   ```

5. Retain every observed positive AMI bucket unchanged. Evenly distribute the
   non-negative historical remainder across the leading zero slots, using a
   deterministic fixed-precision Decimal remainder policy so the allocated
   buckets sum exactly to the historical remainder.
6. Persist the resulting complete, non-overlapping 30-minute month timeline
   once, with provenance that distinguishes observed AMI buckets from
   history-derived allocated buckets.

For June 2026, this means retaining the 1,316 observed AMI buckets and
allocating 96.1977 kWh over the 124 leading slots. At ten decimal places, one
deterministic exact split is 92 slots of `0.7757879032` kWh and 32 slots of
`0.7757879033` kWh, distributed deterministically through the prefix.

The month is accepted only when the guards above hold and the remainder is
non-negative. A malformed response, unvalidated history field, or negative
remainder fails closed. Missing AMI slots in an otherwise usable response are
explicit zero buckets, except for a terminal trailing provider-lag suffix.
Only that terminal suffix is persisted as an unresolved backfill condition.

## Rationale

This approach ensures that:

1. **There is no data gap.** Every 30-minute interval in the transition month
   has a ledger contribution, so the current electricity statistics projection
   can produce a continuous hourly Recorder timeline.
2. **The historical total is correct leading up to the observed seed value.**
   The composed month totals exactly the validated billed/history amount rather
   than undercounting the pre-smart-meter consumption.
3. **Observed AMI is preserved.** Real post-installation half-hour usage is not
   discarded merely because the beginning of the month predates AMI coverage.
4. **The ledger has one unambiguous timeline.** Source arbitration and
   allocation occur before persistence, preventing a monthly history bucket
   from being added on top of AMI buckets and double-counting the month.

## Trade-off

The allocated leading buckets are **not observed time-resolved consumption**.
They evenly project an aggregate historical remainder over time and therefore
misrepresent the actual consumption pattern during the pre-AMI interval. Their
provenance must remain visible to diagnostics and future repair/migration work;
they must never be presented as provider-observed AMI readings.

This trade-off is accepted because the provider supplies only an aggregate
historical total for the missing interval, while a continuous 30-minute
timeline is required to avoid a data gap and to align the cumulative history
correctly with the observed seed value.

## Alternatives Considered

### Whole Monthly History Bucket (Rejected)

Discard all AMI for the transition month and store one monthly history bucket.
This preserves the monthly total but loses the real AMI suffix and cannot feed
the current electricity statistics projection, which requires contiguous
30-minute buckets.

### Use AMI As-Is (Rejected)

Keep only the observed AMI suffix. This undercounts the transition month by the
historical remainder and leaves a permanent cumulative/history gap before the
observed seed value.

### One Daily Gap Bucket Plus AMI (Rejected)

Represent the missing prefix as one or more daily buckets while retaining AMI.
This creates mixed-duration electricity input and still does not satisfy the
current fixed 30-minute statistics projection.

## Consequences

- Electricity source ingestion needs a month-level composition adapter before
  ledger persistence; the existing AMI-only coordinator and current-only
  history parser cannot be used unchanged.
- Accepted buckets need durable provenance for observed AMI versus
  history-derived allocation.
- Existing persisted partial-AMI months require a deliberate strategy/version
  migration and rebuild so old AMI rows are not combined with new allocations.
- Tests must prove the response shape, partial-month classification, exact
  remainder allocation, exact month total, 30-minute continuity, idempotency,
  no double counting, migration behavior, and positive Recorder-visible
  cumulative deltas across the transition.
