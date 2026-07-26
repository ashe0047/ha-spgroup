# ADR: Electricity AMI Gap and No-Data Semantics

## Status

Accepted decision — 2026-07-26; implementation pending

## Context

The hybrid electricity backfill combines 30-minute AMI data with monthly
History data. The existing hybrid ADR defines source precedence and History
remainder allocation for a valid partial AMI month. This ADR records the
additional decisions for provider gaps, missing History evidence, terminal
polling windows, and cursor behavior.

The key distinction is between:

- an explicit provider zero-valued AMI bucket;
- an AMI interval omitted by the provider and intentionally treated as zero;
- a month for which neither source provides data;
- a source failure that must be retried.

The first two have the same consumption consequence (`0 kWh`) but must remain
distinguishable in persisted provenance for diagnostics and future repair.

## Decision

### Source precedence

For each calendar month in the polling window:

1. Full AMI coverage takes precedence over History. History is ignored.
2. No usable AMI plus valid History produces one `history_monthly` bucket.
3. Partial AMI plus valid History retains AMI and allocates only the calculated
   History-minus-AMI remainder across the leading gap.
4. Partial AMI without History is accepted as AMI-only. No remainder is
   allocated; every gap is handled as a zero-valued gap.
5. No AMI and no History follows the no-source rule below.

### AMI gap handling

AMI gaps are classified by position relative to the polling window:

- **Leading gap**: the interval before the first positive AMI bucket.
- **Middle gap**: an interval between positive AMI buckets.
- **Trailing gap**: the interval after the last positive AMI bucket.

The **terminal month** is the calendar month containing the polling window
end. The **leading month** is the calendar month containing the polling window
start. These definitions are request-scoped and are not global calendar
properties.

For a non-terminal month, leading, middle, and trailing gaps are accepted as
zero-valued gap buckets.

For the terminal month:

- leading and middle gaps are accepted as zero-valued gap buckets;
- a trailing gap remains a provider-lag range and is retried by normal
  polling from the last accepted bucket end.

An explicit zero-valued provider row and an inferred gap treated as zero have
the same statistical effect, but use different provenance:

| Provenance | Meaning |
| --- | --- |
| `ami_observed` | Positive AMI consumption observed from the provider. |
| `ami_observed_zero` | Explicit zero-valued AMI bucket returned by the provider. |
| `ami_gap_zero` | Missing AMI interval treated as zero within an AMI month. |
| `no_source_gap_zero` | Missing interval treated as zero because neither source supplied data. |
| `history_allocated` | Synthetic leading-prefix allocation from a History remainder. |
| `history_monthly` | History-only calendar-month bucket. |

Zero-valued buckets are persisted explicitly so the source timeline, cursor,
and Recorder projection can represent the decision durably. They contribute
zero consumption and must not be confused with observed positive AMI.

### Partial AMI without History

When AMI has valid positive coverage for a month but no valid History total is
available, accept the positive AMI buckets. Do not create allocated prefix
values. Leading, middle, and permitted trailing gaps are persisted as zero
gap buckets according to the terminal-month rule.

### No AMI and no History

No AMI and no History is not zero consumption by itself. It means no available
backfill evidence.

- If the month is the leading month of the historical polling window, persist
  a known-unavailable/no-data range, create no consumption bucket, and allow
  the cursor to move past it.
- If the month is not the leading month, persist expected half-hour
  `no_source_gap_zero` buckets so the source timeline and cursor can advance.

This rule applies only after both source requests complete successfully and
are validly interpreted as containing no data. Authentication, transport,
timeout, malformed-response, and provider API failures remain retryable
failures and must not be converted into no-data zeros.

History validation or arithmetic failures are also poll failures. In
particular, a negative History-minus-AMI remainder is a data-integrity error:
the poll must fail closed, create no synthetic allocation, and leave the
cursor unchanged. It must not be represented as a normal unresolved coverage
gap.

### Cursor and retry behavior

For electricity, the normal result of a missing interval is either an
accepted explicit zero bucket or a persisted known-unavailable range. Neither
creates a blocking unresolved range. Consequently, a clean ledger produced by
this strategy has no earlier electricity unresolved ranges.

Terminal trailing provider lag is the only electricity unresolved state. It is
diagnostic/retryable, but non-blocking: the normal cursor continues from the
last accepted bucket end, which is guaranteed to exist immediately before the
terminal trailing gap. The cursor must not take the minimum unresolved start
for this state. If the implementation retains the generic minimum-based
ledger cursor, it must first prove that the only persisted electricity
unresolved range is this terminal range and that its start equals the last
accepted end.

Provider/API failures are not coverage ranges. They fail the poll and leave
the cursor unchanged for the next retry. Malformed or duplicate AMI rows are
source failures; an omitted interval in an otherwise usable response is a
coverage gap and follows the positional zero/terminal-lag rules above.

Known-unavailable leading ranges and accepted zero-gap buckets are treated as
proven source coverage and allow the cursor to advance.

## Consequences

- The ledger must persist explicit zero-valued observations and their
  provenance.
- The ledger must persist known-unavailable ranges separately from blocking
  unresolved ranges and terminal provider-lag ranges.
- Composition must receive the polling window start and end so leading and
  terminal classification is deterministic.
- Explicit zero and no-source zero buckets are clipped to the requested
  polling window; they must not create buckets outside that window merely to
  complete a calendar month.
- Recorder statistics must include zero buckets for continuity while
  preserving non-negative cumulative sums.
- Missing timestamps cannot be classified from the AMI payload alone; the
  composer must receive the expected calendar-month slots for the request.
- Existing persisted ledgers created before these provenance and range states
  require the explicit rebuild flow. No automatic migration or silent rewrite
  is permitted.
- The rebuild is also required for any persisted electricity ledger containing
  pre-decision `missing_internal_bucket` or hybrid-month unresolved ranges;
  those ranges must never be silently reinterpreted as terminal lag.

### Coverage classification contract

The composer classifies each requested closed calendar month using the
expected 30-minute grid and row presence:

- `full_ami`: every expected slot is represented, including a complete
  all-zero grid;
- `no_ami`: no usable AMI rows (including sparse/placeholder-only rows);
- `partial_ami`: usable positive AMI evidence whose missing intervals can be
  positioned as leading, middle, or trailing gaps for the polling-window
  rules;
- `invalid_ami`: evidence that cannot be safely placed on the grid, such as
  duplicate timestamps, malformed values, unsupported units, or an untrusted
  payload shape.

An omitted slot is not automatically a provider failure. Once the response is
otherwise valid, it is a zero gap bucket except when it is the terminal
month's trailing provider-lag interval. Duplicate timestamps, malformed
values, unsupported units, invalid payload structure, and malformed History
are failures and do not produce synthetic zero coverage.

## Required verification

Tests must prove:

- partial AMI with and without History;
- leading, middle, and trailing gaps in terminal and non-terminal months;
- explicit provider zeros versus inferred gap zeros;
- no AMI and no History for leading and non-leading months;
- malformed/transient source failures remain retryable;
- terminal trailing retry starts from the last accepted end;
- no electricity gap other than terminal trailing lag creates an unresolved
  range;
- legacy blocking electricity unresolved ranges force explicit rebuild rather
  than changing cursor meaning in place;
- known-unavailable ranges do not block the cursor;
- zero buckets preserve contiguous Recorder projection and non-negative sums;
- rerunning the same composition is idempotent and cannot overlap accepted
  spans.
