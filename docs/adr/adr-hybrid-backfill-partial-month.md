# ADR: Hybrid Electricity Backfill — Partial AMI Month Handling

## Status

Locked — 2026-07-16

## Context

The SP Group integration backfills historical electricity data from two API sources:

1. **AMI charts endpoint** (`/jarvis/v3/ami/charts`) — 30-minute granularity, but
   only returns smart-meter data from June 2026 onwards (when the smart meter was
   installed at the premise).

2. **History charts endpoint** (`/jarvis/v4/charts/{premise_id}`) — monthly
   granularity, returns billed consumption data covering the last 12 months
   (Jul 2025 - Jun 2026 in this case).

For the transition month (June 2026), the AMI endpoint covers 28 days of data
(Jun 3-30, 1,316 non-zero buckets, 588.87 kWh) with a 2-day gap (Jun 1-2, 124
zero-value buckets). The history endpoint covers the full month (685.07 kWh).

The gap of 96.20 kWh is **pure consumption recorded by the traditional meter
before the smart meter was installed on June 3**, not a measurement discrepancy.

## Decision

**Use the history monthly bucket for the overlapping partial AMI month.**
The AMI 30-min data for that month is discarded.

### Implementation rule

For each month during backfill:

```
if month has NO AMI data:
    use history monthly bucket
elif month has AMI data but with a zero-value prefix (partial AMI):
    use history monthly bucket (discard AMI for this month)
elif month has annual history data AND month has full AMI data:
    use AMI (discard history for this month)
else:
    use whatever source has data (current month)
```

For the current backfill data, this means:

| Period | AMI coverage | Source | Buckets |
|--------|-------------|--------|---------|
| Jul 2025 - May 2026 | None | History | 11 monthly |
| Jun 2026 | **Partial** (2-day zero prefix) | **History** | 1 monthly (685.07 kWh) |
| Jul 2026+ | Full | AMI | 30-min live |

## Rationale

1. **One transition month**: June is a single transition month between 11
   months of monthly granularity and ongoing AMI 30-min granularity. The
   cost of losing 30-min granularity for 28 of June's 30 days is acceptable.

2. **No artificial data**: Alternative approaches require creating synthetic
   30-min buckets (pro-rata gap-fill) or mixing daily buckets with 30-min
   buckets within the same month, both introducing complexity without
   meaningful benefit.

3. **Simpler implementation**: The statistics projection already handles
   monthly buckets (for water and pre-AMI electricity). No special handling
   is needed for mixed bucket durations within a single month.

4. **Consistency**: June is treated the same as the 11 pre-AMI months,
   avoiding special-case logic for the transition.

## Alternatives Considered

### Pro-rata Gap-Fill (Rejected)

Create 124 synthetic 30-min buckets for Jun 1-2 by distributing the 96.20 kWh
gap evenly (0.776 kWh per bucket). Keeps 30-min granularity for 93% of June,
but the flat-valued synthetic buckets create an unnatural straight-line pattern
in the sensor graph, followed by real varying data from Jun 3.

### Single Gap Bucket + AMI (Rejected)

Create one daily bucket covering Jun 1-2 (96.20 kWh) while keeping AMI 30-min
buckets for Jun 3-30. Granularity preserved for the AMI period, but the
statistics projection must handle mixed bucket durations within the same month
(a daily bucket interspersed with 30-min buckets). Added complexity and testing
surface outweighs the benefit of 28 days of 30-min data.

### Use AMI As-Is (Rejected)

Use AMI data for Jun 3-30 as-is (588.87 kWh), leaving Jun 1-2 as zero values.
June total would undercount by 96.20 kWh (14%), creating a data gap. Rejected
because the requirement is no data gaps.

## Consequences

- The user loses 30-min granularity for June 2026 (one transition month).
- The statistics projection handles monthly-to-30-min transition at the
  July 1 boundary (same as water already does).
- No changes needed to the statistics projection for mixed bucket durations.
- Future smart meter retrofits at other premises will follow the same pattern:
  the installation month uses the history bucket; subsequent months use AMI.

