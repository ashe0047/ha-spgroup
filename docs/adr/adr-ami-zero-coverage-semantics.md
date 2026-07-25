# ADR: AMI Zero-Value Coverage Semantics

## Status

Locked — 2026-07-22

## Context

The SP Group AMI chart endpoint returns 30-minute rows with a numeric
`consumption` value. A value of zero is not self-describing: it can represent
the period before the smart meter was installed, a provider data-lag suffix, or
a period that has not occurred yet. Treating every zero row as a real
consumption bucket can make the ledger appear caught up while silently filling
missing provider data with zeros.

The existing June transition decision already establishes that a leading AMI
zero prefix represents pre-smart-meter coverage and that the monthly history
source is authoritative for that transition month. Live validation also showed
the provider returning non-zero readings through one point in the day followed
by zero rows for later periods that had already elapsed locally.

## Decision

Interpret AMI zero rows by their position in the returned ordered series and
the current time:

1. **Leading zero prefix before AMI coverage begins**
   - Represents periods before the smart meter was installed or before AMI
     coverage exists.
   - It is not usable AMI consumption.
   - During historical hybrid backfill, use the applicable history bucket and
     discard the partial AMI month as defined by
     `adr-hybrid-backfill-partial-month.md`.

2. **Trailing zero suffix after the last non-zero AMI bucket**
   - Represents provider lag or unavailable data once the corresponding
     30-minute period has elapsed locally.
   - It is not an accepted zero-consumption bucket.
   - The integration must retain the last trusted total and leave the suffix
     eligible for a later poll rather than advancing the source cursor through
     it.

3. **Zero rows whose period has not started or is still in progress**
   - Represent future or not-yet-closed periods.
   - They are not observations for the current poll and must be excluded until
     the period is closed and provider data is available.

4. **Any zero row after AMI coverage begins**
   - Means that the provider has not supplied usable data for that period yet,
     including an isolated zero between populated rows.
   - It is not accepted as a real zero-usage bucket unless a future provider
     contract explicitly introduces a separate coverage signal.

The source freshness model remains variable and provider-dependent. There is
no fixed delay that converts a zero into valid data. A zero suffix is resolved
only when a later provider response supplies a non-zero value or an explicit
source-coverage signal for that bucket.

## Consequences

- The AMI parser/ledger acceptance path must identify and exclude leading and
  trailing coverage zeros instead of accepting every non-negative value.
- A trailing lag suffix must not advance accepted-bucket or statistics
  progress metadata.
- The sensor continues to expose the last trusted cumulative total while the
  source is lagging; lack of a new provider bucket alone does not make it
  unavailable.
- Test fixtures must cover leading zeros, trailing elapsed zeros, future zeros,
  internal post-coverage zeros, and later replacement of unavailable rows with
  real provider values.

## Scope

This ADR defines the meaning of AMI zero rows. It does not yet implement the
parser, cursor, source-freshness metadata, or historical hybrid-backfill
changes required to enforce the decision.
