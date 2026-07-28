# ADR: Electricity AMI Gap and No-Data Semantics

## Status

Accepted — implementation aligned 2026-07-27

## Context

The historical electricity backfill combines 30-minute AMI data with monthly
History data. The existing hybrid ADR defines source precedence and History
remainder allocation for a valid partial AMI month. This ADR records the
additional decisions for provider gaps, missing History evidence, terminal
polling windows, and cursor behavior.

Electricity has two intentionally separate processing policies:

- **Historical backfill** is the configured backfill lifecycle. It composes
  closed calendar months from AMI and History and may span timestamps both
  before and after the configured seed boundary.
- **Normal post-seed polling** starts from the persisted ledger cursor and is
  AMI-only. It does not fetch History or invoke month-level composition. Any
  historical correction requiring History belongs to an explicit rebuild or
  reconciliation workflow.

The key distinction is between:

- an explicit provider zero-valued AMI bucket;
- an AMI interval omitted by the provider and intentionally treated as zero;
- a leading AMI zero/omitted prefix that represents unavailable coverage;
- a month for which neither source provides data;
- a source failure that must be retried.

The first two have the same consumption consequence (`0 kWh`) but must remain
distinguishable in persisted provenance for diagnostics and future repair. A
leading unavailable prefix has no consumption bucket and is represented as
known-unavailable coverage instead.

## Decision

### Source precedence

For each calendar month in the historical backfill window:

1. Full usable AMI coverage takes precedence over History. History is ignored.
   Leading zero/placeholder rows do not establish usable AMI coverage.
2. No usable AMI plus valid History produces one `history_monthly` bucket. A
   zero-only or placeholder-only AMI month is no usable AMI. History rows with
   provider status `Actual` or `Estimated` are both valid backfill evidence;
   the status must be retained as provenance for diagnostics.
3. Partial AMI plus valid History retains AMI and allocates only the calculated
   History-minus-AMI remainder across the leading gap.
4. Partial AMI without History is accepted as AMI-only. No remainder is
   allocated; the leading unavailable prefix is persisted as known-unavailable
   coverage, while middle and permitted trailing gaps are handled as
   zero-valued gaps.
5. No AMI and no History follows the no-source rule below.

### AMI gap handling

AMI gaps are classified by position relative to the polling window:

- **Leading unavailable prefix**: explicit zero rows or omitted intervals before
  the first positive AMI bucket. These rows do not establish AMI coverage and
  are not accepted as zero-consumption observations.
- **Middle gap**: an interval between positive AMI buckets.
- **Trailing gap**: the interval after the last positive AMI bucket.

The **terminal month** is the calendar month containing the polling window
end. The **leading month** is the calendar month containing the polling window
start. These definitions are request-scoped and are not global calendar
properties.

For a non-terminal month, the leading unavailable prefix is represented as
known-unavailable coverage; middle and trailing gaps are accepted as explicit
zero-valued gap buckets.

For the terminal month:

- the leading unavailable prefix is represented as known-unavailable coverage;
- middle gaps are accepted as zero-valued gap buckets;
- a trailing gap remains a provider-lag range and is retried by normal
  polling from the last accepted bucket end. This includes explicit zero
  placeholder rows after the last positive completed AMI bucket; those rows
  must not be persisted as consumption.

An explicit zero-valued provider row before or between positive AMI buckets and
an inferred middle gap treated as zero have the same statistical effect, but
use different provenance. Explicit or omitted terminal trailing placeholders
are provider lag, not consumption. Leading zero rows are unavailable and are
not persisted as consumption buckets:

| Provenance | Meaning |
| --- | --- |
| `ami_observed` | Positive AMI consumption observed from the provider. |
| `ami_observed_zero` | Explicit zero-valued AMI bucket returned between positive AMI buckets. |
| `ami_gap_zero` | Missing AMI interval treated as zero within an AMI month. |
| `no_source_gap_zero` | Missing interval treated as zero because neither source supplied data. |
| `history_allocated` | Synthetic leading-prefix allocation from a History remainder. |
| `history_monthly` | History-only calendar-month bucket. |

History status is provider-supplied metadata, not a value inferred by the
integration. Both `Actual` and `Estimated` rows are eligible for backfill
selection and History remainder allocation. The selected status must be
persisted alongside the source decision, either on the accepted bucket or in
the associated source-coverage diagnostics. An empty, unknown, or unsupported
status is not valid History evidence unless a later provider contract adds it.

Zero-valued buckets are persisted explicitly so the source timeline, cursor,
and Recorder projection can represent the decision durably. They contribute
zero consumption and must not be confused with observed positive AMI.

### Normal post-seed AMI polling

Normal polling uses only completed half-hour slots in the requested
cursor-to-now window. It does not classify calendar-month coverage or consult
History:

- positive rows are accepted as `ami_observed`;
- explicit zeros before or between positive rows are accepted as
  `ami_observed_zero`;
- omitted non-trailing slots are accepted as `ami_gap_zero`;
- explicit or omitted slots after the last positive completed row are not
  accepted and are coalesced into one `terminal_provider_lag` range;
- a completed window with no positive row remains lagging, creates no accepted
  observations, and does not advance the cursor;
- incomplete/future slots are excluded from both observations and lag ranges.

Normal trailing classification is relative to the actual polling window, not
to a calendar month. A trailing range may cross a month boundary. The normal
parser does not call `classify_electricity_ami_month()` or
`compose_electricity_history_and_ami()`.

### Partial AMI without History

When AMI has valid positive coverage for a month but no valid History total is
available, accept the positive AMI buckets. Do not create allocated prefix
values. Persist the leading unavailable prefix as known-unavailable coverage;
middle and permitted trailing gaps are persisted as zero gap buckets according
to the terminal-month rule.

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

For historical electricity backfill, the normal result of a missing interval is either an
accepted explicit zero bucket or a persisted known-unavailable range. Neither
creates a blocking unresolved range. Consequently, a clean ledger produced by
this strategy has no earlier electricity unresolved ranges.

For normal post-seed polling, internal missing intervals follow the same
accepted-zero rule, while the trailing suffix is the sole unresolved range.
When a normal response has no positive completed value, no observations are
accepted and the cursor remains unchanged.

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
- Historical composition must receive the polling window start and end so
  leading and terminal classification is deterministic. Normal parsing
  receives the clipped cursor-to-now window and applies its independent
  trailing policy.
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
- If an existing development ledger has already imported statistics from an
  incorrect decision, rebuilding the ledger alone is insufficient because the
  external Recorder series is immutable through normal reconciliation. The
  development instance must explicitly reset that external series (or use a
  fresh Recorder database) before running the corrected backfill.
- The rebuild is also required for any persisted electricity ledger containing
  pre-decision `missing_internal_bucket` or hybrid-month unresolved ranges;
  those ranges must never be silently reinterpreted as terminal lag.

### Coverage classification contract

The historical composer classifies each requested closed calendar month using the
expected 30-minute grid and row presence:

- `full_ami`: every expected slot is represented and usable positive AMI
  coverage exists through the final completed slot; explicit internal zero
  rows before or between positive coverage remain part of the full grid. A
  terminal trailing explicit-zero suffix is not full coverage.
- `no_ami`: no usable positive AMI coverage exists, including zero-only,
  leading-zero-only, sparse, or placeholder-only rows;
- `partial_ami`: usable positive AMI evidence exists but a leading unavailable
  prefix or other positional gaps prevent full usable coverage;
- `invalid_ami`: evidence that cannot be safely placed on the grid, such as
  duplicate timestamps, malformed values, unsupported units, or an untrusted
  payload shape.

An omitted slot is not automatically a provider failure. A leading omitted or
explicit-zero slot is unavailable prefix coverage. Once AMI coverage has begun,
an omitted middle slot is a zero gap bucket except when it is the terminal
month's trailing provider-lag interval. Duplicate timestamps, malformed
values, unsupported units, invalid payload structure, and malformed History
are failures and do not produce synthetic zero coverage.

## Required verification

Tests must prove:

- historical and normal parser policies are dispatched independently;
- normal polling never fetches History or invokes month-level composition;
- normal explicit and omitted trailing slots coalesce into terminal lag,
  including across a calendar-month boundary;
- normal internal explicit and inferred zeros retain their provenance;
- a normal all-zero completed window leaves the cursor unchanged;

- partial AMI with and without History;
- zero-only AMI with valid History selecting `history_monthly`;
- `Actual` and `Estimated` History rows both selecting valid backfill output,
  with status retained for diagnostics;
- leading-zero AMI with positive suffix selecting partial precedence rather
  than discarding History;
- leading, middle, and trailing gaps in terminal and non-terminal months;
- explicit provider zeros versus inferred gap zeros;
- no AMI and no History for leading and non-leading months;
- malformed/transient source failures remain retryable;
- terminal trailing retry starts from the last accepted end;
- no electricity gap other than terminal trailing lag creates an unresolved
  range;
- development ledgers created before this contract are not migration targets;
  discard/reset them and run a fresh backfill rather than reinterpreting their
  accepted buckets or unresolved ranges;
- known-unavailable ranges do not block the cursor;
- zero buckets preserve contiguous Recorder projection and non-negative sums;
- rerunning the same composition is idempotent and cannot overlap accepted
  spans.
