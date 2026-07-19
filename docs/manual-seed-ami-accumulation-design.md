# Manual Seed Plus AMI Delta Accumulation Design

## Purpose

This document records the locked solution direction for operating the SP Group
Home Assistant integration when an automatic cumulative meter-reading API is not
available or not authorized for the current account.

The goal is to keep the integration useful while preserving Home Assistant
Energy/Water Dashboard correctness. SP chart and AMI payloads must not be
misrepresented as cumulative meter readings.

## Problem

Home Assistant dashboard-ready utility sensors need a source with safe total
semantics. The ideal source is an API-provided cumulative meter reading with a
timestamp, unit, and stable meter identity.

Current live validation shows:

- Discovery and history-chart surfaces are available.
- History and AMI/PPMS chart payloads are chart or period evidence, not proven
  cumulative totals.
- The likely cumulative electricity source,
  `fetch_account_meters` plus `fetch_meter_readings`, is currently blocked by
  account-surface authorization for the tested live session.
- Water does not yet have a proven cumulative source.

Without a cumulative source, publishing chart values as `TOTAL` or
`TOTAL_INCREASING` would risk corrupting Home Assistant long-term statistics.

## Track 2 Closure Decision

Numeric sensor work is no longer blocked on resolving the current SP cumulative
API authorization gap.

The first production numeric source will be `manual_seed_plus_bucket_deltas`.
The integration will use a user-provided physical meter reading as the absolute
seed and API usage buckets as post-seed deltas.

This first numeric source is intended to be dashboard-ready as soon as the
ledger, finalization, correction, and monotonicity rules in this document are
implemented and tested. There is no separate non-dashboard numeric preview phase.

Automatic SP cumulative APIs remain desirable, but they are no longer required
for the first full-utility implementation. They are future swappable sources.

Candidate future API cumulative sources:

- `sp_api_cumulative_skalbox_meter_history`
- `sp_api_cumulative_jarvis_meter_readings`
- `sp_api_latest_meter_reading`

The blocked or unproven API sources should continue to be investigated in
parallel, but their unresolved status must not block implementation of the
manual seed plus bucket-delta path.

## Locked Direction

Use a swappable meter-reading source architecture.

Initial supported source:

- `manual_seed_plus_bucket_deltas`

Concrete utility modes:

- electricity: `manual_seed_plus_ami_deltas`
- water: `manual_seed_plus_monthly_deltas` until the water meter is upgraded to
  a finer-grained smart-meter source

Future source:

- `sp_api_cumulative`

Candidate future `sp_api_cumulative` implementations:

- Skalbox meter-reading history, if live authorization and payload semantics are
  proven.
- Jarvis electric meter readings, if account-surface authorization and
  `cumulativeRead` semantics are proven.
- Latest meter-reading payloads, if `lastActualValue` / `lastActualAt` or
  equivalent fields prove sufficient for continuity.

Supporting evidence source:

- `chart_period_evidence`

The entity identity should remain stable across source changes. The integration
should be able to replace the manual/AMI source with a future SP cumulative API
source without changing entity unique IDs, as long as the premise, utility, unit,
and continuity checks pass.

## Manual Seed Plus Bucket Deltas

The user provides an absolute physical meter reading:

- seed reading value, for example `12345.6 kWh`;
- seed observed timestamp;
- utility type;
- premise/entity association.

API chart payloads provide usage buckets after that seed. The derived total is:

```text
derived_total = seed_reading + sum(accepted usage buckets after effective_seed_boundary)
```

The seed remains the absolute base. API bucket values only advance the total
after the seed boundary.

For electricity, the bucket source is AMI chart data. For water, the initial
bucket source is monthly usage data because the current water meter is not smart.
Both use the same ledger, deduplication, correction, and source-switching model.

## AMI Boundary Alignment

SP AMI chart data can be grouped by day, but the underlying data points are
30-minute buckets. A physical meter reading taken at an arbitrary time cannot be
perfectly aligned with those buckets.

Locked alignment rule:

- Store `seed_observed_at`.
- Derive `seed_effective_from` as the next 30-minute AMI bucket boundary, unless
  `seed_observed_at` is already exactly on a boundary.
- Accumulate only AMI buckets with `bucket_start >= seed_effective_from`.
- Mark the source as boundary-aligned only when the user-provided timestamp is
  exactly on a 30-minute boundary.
- Mark the source as approximate when the timestamp had to be rounded forward.

Example:

```text
seed_reading_kwh = 12345.6
seed_observed_at = 2026-07-14 13:17
seed_effective_from = 2026-07-14 13:30
```

The integration avoids double counting by ignoring the partially overlapping
13:00-13:30 bucket. This may miss usage between 13:17 and 13:30, so it is
approximate.

## Water Monthly Bucket Alignment

Water should use the same seed-plus-delta architecture as electricity, but with
monthly buckets while the current water meter is not smart.

Locked water rule:

- Treat monthly water usage rows as bucket deltas, not cumulative readings.
- Poll water on a monthly cadence until a finer-grained smart-meter water source
  is available.
- Store the same seed fields as electricity.
- Derive `seed_effective_from` from the next non-overlapping monthly bucket
  boundary unless the seed was taken exactly at a monthly boundary.
- Accumulate only closed monthly buckets that start at or after
  `seed_effective_from`.
- Mark water freshness using source lag rather than treating monthly cadence as
  an integration failure.

Example:

```text
seed_reading_m3 = 456.7
seed_observed_at = 2026-07-15 10:00
seed_effective_from = 2026-08-01 00:00
```

In this example, the July monthly bucket overlaps the seed and must not be
added. The first safe automatic water delta is the closed August bucket, once SP
publishes it.

When the water meter is upgraded and SP exposes finer-grained water data, the
same source can switch from monthly buckets to the new bucket granularity after
continuity checks pass.

## User Guidance

The config flow should authenticate first, perform discovery, then ask for seed
readings for each discovered utility that the user wants to enable. Seed entry
belongs in config flow because the initial integration should be usable without
requiring a second options step after setup.

Setup uses Home Assistant selectors for the critical fields:

- email/password selectors for authentication;
- one-time-code friendly OTP entry for MFA;
- a multi-select utility picker keyed by opaque sensor keys;
- numeric seed entry with the utility unit shown;
- a Home Assistant date-time selector for the seed observation time.

Discovered utilities may use user-friendly premise labels, such as the SP app
display label or address, because this integration is intended for personal use.
Those labels are UI copy only. Source configuration, entity unique IDs, ledger
storage, diagnostics, and logs must continue to use opaque keys and must not
store raw account numbers, premise IDs, meter IDs, IAM IDs, emails, phone
numbers, tokens, or Jarvis handles.

The options flow should support later seed changes or source switching. Reseeding
must be explicit and should create a new accumulation epoch.

Options uses the same opaque-key utility multi-select and a single maintenance
action selector. One reseed or ledger rebuild is handled per options-flow run;
the user can run options again for additional maintenance.

For electricity, the flow should strongly guide the user to take the physical
meter reading exactly at a 30-minute boundary:

- `HH:00`
- `HH:30`

This gives the cleanest relationship between the manual seed and AMI buckets.

If the user provides a non-boundary timestamp, the integration may allow it only
as an explicitly approximate source. The config/options flow must show a
separate confirmation step before storing the seed. Confirmation means the user
accepts that accumulation starts at the next safe bucket boundary and may skip
the partial interval between the physical reading time and that boundary.

For water, the flow should explain that the current source uses monthly buckets.
The cleanest water seed is a reading taken at a monthly boundary. A mid-month
water seed can be accepted, but accumulation begins from the next closed
non-overlapping month.

All bucket boundaries and seed timestamps use `Asia/Singapore`.

## Persistence And State Boundaries

Seed configuration should live in the Home Assistant config entry:

- initial seeds are collected during config flow after discovery;
- later seed changes are handled by options flow;
- stored seed data must use opaque premise/utility keys, not raw account,
  premise, meter, address, or user identifiers.

Mutable accumulation state should live in Home Assistant storage using a
versioned store:

- accepted bucket ledger;
- current accumulation epoch;
- latest derived total;
- latest accepted bucket timestamp;
- source freshness metadata;
- warning/correction metadata.

The ledger must be restorable across Home Assistant restarts before any
dashboard-ready sensor state is published.

## Accumulation Safety Rules

Before `manual_seed_plus_bucket_deltas` can publish a dashboard-ready total, the
integration must implement and test:

- persistent bucket ledger/checkpointing across restarts;
- deduplication of AMI buckets across repeated polling;
- exclusion or clear handling of incomplete/current buckets;
- utility-specific source freshness rules, including unpredictable AMI source
  lag and monthly water lag;
- unit normalization to kWh for electricity and cubic meters for water;
- detection of missing buckets;
- correction policy for changed historical buckets;
- monotonic/reset behavior compatible with Home Assistant statistics;
- privacy-safe diagnostics that do not expose raw account, premise, meter, or
  address identifiers.

Corrections are the main risk. If SP changes a historical bucket after Home
Assistant statistics have already consumed the derived total, the integration
must not silently rewrite history in a way that corrupts long-term statistics.

Locked correction policy:

- once a bucket is accepted into the ledger, do not mutate its contribution
  automatically;
- if SP later returns a different value for an accepted bucket, keep the
  published total stable and record a correction warning;
- manual repair or ledger rebuild may be added later as an explicit advanced
  flow, not as silent background behavior.

Locked state-class policy:

- publish `SensorStateClass.TOTAL_INCREASING` for manual seed plus bucket
  deltas only after the ledger guarantees the derived total will not decrease;
- do not back-apply negative corrections to the published total;
- if a future source cannot preserve monotonicity, it must not replace the
  dashboard-ready source without a separate design decision.

Locked source freshness policy:

- electricity AMI lag is not fixed and depends on the upstream source provider;
- do not use a fixed freshness delay as the only acceptance rule;
- accept only buckets that are non-overlapping, not current/in-progress, and
  stable according to the implemented ledger policy;
- lack of a new bucket does not make the sensor unavailable;
- keep the last derived total and expose source freshness metadata instead;
- water monthly buckets are accepted only after the month is closed, with a
  default three-day grace period after month end;
- bucket boundaries use `Asia/Singapore`.

Unavailable should mean the integration cannot produce a trustworthy total:

- no valid seed;
- invalid or unreadable ledger;
- source identity no longer matches discovery;
- unrecoverable auth/config-entry failure.

It should not mean merely that the upstream data is delayed.

## Source Switching

When a reliable SP cumulative API source becomes available, switching from
`manual_seed_plus_ami_deltas` to `sp_api_cumulative` should require:

- same premise and utility identity;
- compatible unit;
- compatible meter/source identity when available;
- continuity check between the current derived total and new API total;
- user confirmation if the delta is suspicious;
- no entity unique ID change;
- no dashboard metadata change unless the replacement source preserves safe
  total semantics.

## Manual Reset And Replacement Flow

Lower-value reseeding is supported only as an explicit reset/replacement flow.
This is different from an ordinary seed correction because the published sensor
uses Home Assistant `SensorStateClass.TOTAL_INCREASING`, where a lower state is
treated as a meter reset/discontinuity by long-term statistics.

Locked reset/replacement rules:

- ordinary reseeding creates a new accumulation epoch;
- if the new seed is greater than or equal to the currently published ledger
  total, it may replace the old epoch without an extra reset confirmation;
- if the new seed is lower than the currently published ledger total, the
  options flow must show a separate confirmation step explaining that this is a
  meter replacement or intentional cumulative baseline reset;
- the persisted source config must record `reset_confirmed=true` for a confirmed
  lower replacement;
- the ledger must refuse an unconfirmed lower replacement even if options are
  manually edited outside the UI;
- when a replacement epoch is accepted, prior pending and accepted buckets are
  discarded for that sensor and the new seed becomes the first trusted total for
  the new epoch;
- the entity unique ID must not change during replacement, so Home Assistant can
  apply its normal long-term-statistics reset semantics to the same sensor;
- diagnostics may expose only summary warning codes such as
  `user_confirmed_lower_replacement` or
  `blocked_unconfirmed_lower_replacement`, never seed values or raw identifiers.

Guardrails:

- replacement is available only from the options flow after the integration has
  discovery and a trusted ledger total;
- no automatic source correction or backend payload change can trigger a lower
  replacement;
- historical accepted bucket values from the old epoch are not rewritten;
- future raw SP cumulative sources must use the same continuity and explicit
  lower-reset confirmation policy before replacing the manual seed source.

## Advanced Ledger Repair And Rebuild

Malformed or unreadable ledger storage is a broken operational state. It should
create a Home Assistant Repair issue because the user can fix it by rebuilding
the ledger from stored seed configuration.

Healthy-ledger rebuild is different: it is an intentional administrative action
and belongs in the options flow, not in Home Assistant Repairs.

Locked repair/rebuild rules:

- config-entry migration must not migrate mutable ledger storage;
- malformed ledger repair rebuilds enabled source entries from stored seeds,
  clears accepted/pending bucket history, records
  `repair_rebuilt_malformed_ledger`, clears the Repair issue, and reloads the
  entry;
- manual per-utility rebuild from options flow requires explicit confirmation,
  clears only the selected utility's accepted/pending buckets, records
  `user_rebuilt_ledger`, and reloads or refreshes the entry even if options are
  unchanged;
- rebuild never reprocesses historical buckets or silently rewrites accepted
  values;
- repair/rebuild messages, diagnostics, and logs may expose only summary warning
  codes and counts, never seed values, bucket values, account numbers, premise
  IDs, addresses, names, emails, tokens, or Jarvis handles.

## Non-Goals

This design does not:

- treat history charts as cumulative meter readings;
- treat AMI buckets as a seed replacement;
- treat monthly water buckets as near-live telemetry;
- publish values before the ledger/finalization/correction rules are
  implemented;
- auto-create Home Assistant Utility Meter helpers;
- mutate SP Group account or meter-reading data.

Home Assistant's Utility Meter helper can remain a downstream user choice after
the integration exposes a safe cumulative source sensor.

## Current Decision

Proceed toward a source-pluggable sensor model:

1. Treat Track 2 as closed by product decision: real numeric sensors proceed
   with manual seed plus bucket deltas, not by waiting for SP cumulative API
   authorization to be solved.
2. Keep current chart-derived sensors unavailable and non-statistical until the
   source ledger is implemented.
3. Add a manual seed source as the first path toward full utility.
4. Use electricity AMI buckets only as post-seed deltas after 30-minute boundary
   alignment.
5. Use water monthly buckets as post-seed deltas after monthly boundary
   alignment, with monthly polling and explicit source-lag status.
6. Make the first implemented numeric source dashboard-ready immediately once
   the ledger guarantees monotonic derived totals.
7. Keep `sp_api_cumulative` as a future drop-in source when live authorization
   and semantics are proven.

## Implementation Alignment Status

As of 2026-07-15, the Home Assistant integration has begun implementing this
design:

- setup authenticates, performs discovery, and collects per-utility manual seed
  values for selected discovered utilities;
- source configuration is stored in config-entry options keyed by opaque
  discovery sensor keys;
- a versioned Home Assistant storage ledger stores seed-derived totals, pending
  bucket sightings, accepted immutable bucket contributions, and correction
  warnings;
- a valid seed is the first trusted dashboard-ready total;
- electricity AMI buckets require two unchanged successful sightings before
  acceptance;
- water monthly buckets use the closed-month plus three-day grace policy;
- sensors expose dashboard metadata only when a trusted ledger total exists;
- unseeded utilities remain discovery-only and unavailable/non-statistical.

Current v1 limits:

- automatic SP cumulative sources remain disabled until live authorization and
  semantics are proven.
- advanced source switching to automatic SP cumulative APIs remains future work.

Reset/replacement alignment:

- options flow supports explicit lower-value reset confirmation;
- source configs carry an epoch id and reset confirmation flag;
- the ledger blocks unconfirmed lower replacements and accepts confirmed lower
  replacements as new epochs;
- replacement preserves entity identity while clearing old pending/accepted
  bucket state for the replaced sensor.

Config-flow UX alignment:

- setup and options use stable `enabled_utilities` selections keyed by opaque
  sensor keys, not dynamic schema keys derived from labels;
- user-facing utility labels are title-cased and unit-aware;
- seed value and seed timestamp use Home Assistant number and date-time
  selectors;
- non-boundary seed timestamps require explicit approximate confirmation;
- setup with no selected utilities requires explicit discovery-only
  confirmation;
- options uses one `maintenance_action` per run instead of mixing reseed and
  rebuild booleans into a dense list.
