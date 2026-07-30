# HA Payload Contracts And Readiness Gates

This document records the Home Assistant-relevant payload contracts that must be
validated before SP Group utility data can be published as sensor values or long
term statistics.

The decompiled APK is the schema source of truth for expected field names and
client-side model intent. Live E2E tests validate whether those contracts are
available for the current account and still match production payloads.

## Readiness Gates

| Gate | Meaning | Required evidence |
| --- | --- | --- |
| SDK live-ready | Cached mobile sessions refresh and read-only SDK methods work against production. | Opt-in live E2E pass or documented optional 401/403 account-surface xfails. |
| Payload-shape ready | Live payloads match APK-grounded DTO expectations. | Semantic live shape census for discovery, charts, and meter payloads. |
| HA runtime-ready | Config entry setup, refresh, coordinator polling, diagnostics, and dynamic sensors remain safe. | Hermetic HA integration tests with redacted fixtures. |
| Sensor-value ready | Numeric utility states are safe to expose. | Proven unit, timestamp, source, duplicate, and reset/monotonic behavior. |
| Dashboard-ready | Energy/Water Dashboard metadata is safe. | Home Assistant `device_class`, `state_class`, and unit requirements are satisfied by proven cumulative/reset-safe semantics. |

## History Charts

Endpoint: `GET /jarvis/v4/charts/{premise_id}`

APK evidence:

- `HistoryChartResponseModel`: `serverTime`, `premiseType`, `elec`, `gas`,
  `water`.
- `UtilityModel`: `comparisonType`, `comparisonMessageType`,
  `comparisonMessage`, `averageConsumption`, `data`.
- `UtilityHistoryResponseModel`: `period`, `status`, `unit`, `consumption`,
  `peerComparison`.
- `UtilityConsumptionModel`: `current`, `previous`.

HA interpretation:

- This is premise-level chart or period evidence.
- `consumption.current` can be parsed as usage for the row period when the unit
  is supported.
- It is not proven to be a cumulative meter total.
- It must remain non-publishable and non-statistical until a later milestone
  explicitly designs safe period accumulation and restart behavior.

Live validation:

- At least one utility section must exist.
- Utility `data`, when present, must be an array.
- Rows must be objects.
- `period`, `status`, and `unit`, when present, must be strings.
- `consumption.current` and `consumption.previous`, when present, must be
  numeric-like.

## AMI And PPMS Charts

Endpoints:

- `POST /jarvis/v3/ami/charts`
- `POST /jarvis/v3/ppms/charts`

APK evidence:

- `SmartMeterChartRequestModel`: `premise_id`, `start`, `end`, `grouped_by`,
  `utility_type`.
- `SmartMeterChartResponseModel`: `dailyLimit`, `unit`, `history`.
- `SmartMeterChartHistoryResponseModel`: `next`, `previous`, `start`, `end`,
  `data`.
- `SmartMeterChartHistoryDataResponseModel`: `date`, `amount`, `consumption`,
  `metadata`.

HA interpretation:

- This is date-windowed chart bucket evidence.
- `consumption` is preferred over `amount` for fixture parsing because it is
  typed numerically in the APK DTO.
- It is not proven to be a cumulative meter total.
- A zero `consumption` value is positional coverage evidence, not universally
  valid zero usage: leading zeros indicate pre-AMI coverage, while any zero
  after AMI coverage begins indicates provider lag/unavailable data, including
  trailing, internal, and future/in-progress rows.
- It must remain non-publishable and non-statistical.

Live validation:

- `history` must be an array.
- `unit`, `dailyLimit`, group `start`, group `end`, and row `date`, when
  present, must be strings.
- Row `amount` and `consumption`, when present, must be numeric-like.

## Meter Reading Surfaces

Endpoints:

- `GET /jarvis/v3/smrd-uportal/{premise_id}`
- `POST /jarvis/v3/smrd/elec-meters`
- `POST /jarvis/v3/smrd/elec-meters/readings`

APK evidence:

- `MeterReadingModel`: `submissionToken`, `message`,
  `latestSubmissionPeriod`, `meters`.
- `MeterModel`: `accountNumber`, `meterId`, `utilityType`, `lastActualAt`,
  `lastActualValue`, `lastSubmittedAt`, `lastSubmittedValue`, `preDecPlace`,
  `decPlace`, `min`, `max`, `source`, `nextMnrFromDate`, `nextMnrToDate`.
- `ElectricityMeters`: `accountMeters`.
- `ElecMeter`: `meterNumber`, `meterType`, `measurementId`, `currentStatus`,
  `meterDateFrom`, `meterDateTo`, `readings`.
- `ElecMeterReadingDataModel`: `readDate`, `cumulativeRead`, `selfRead`,
  `remarks`, `readType`, `smrd`, `asmrd`, `transEbsCa`.

HA interpretation:

- `cumulativeRead` is the strongest currently identified electricity candidate
  for future Energy Dashboard support.
- It still needs live proof of unit/scaling, timestamp meaning, meter identity,
  ordering, and reset/monotonic behavior before any numeric state is published.
- Water remains independently blocked unless an equivalent cumulative water
  source is identified.

Live validation:

- `account_meters` / `accountMeters`, when present, must be arrays.
- Meter collections and `readings`, when present, must be arrays.
- Reading rows must be objects.
- `read_date` / `readDate`, when present, must be strings.
- `cumulative_read` / `cumulativeRead`, when present, must be numeric-like.

## Discovery Correlation

Sources:

- `YggdrasilClient.get_premises_details()`
- `JarvisClient.get_me()`

APK evidence:

- Yggdrasil premise details expose `premiseDetails.premises[].accountNo` and
  `accounts[].premiseNo`.
- Jarvis `/me` exposes premise/account metadata used by the mobile app for
  utility chart and meter surfaces.

HA interpretation:

- Yggdrasil remains the primary discovery source.
- Jarvis correlation is required for private premise handles used by chart
  polling.
- Raw account numbers, premise numbers, meter IDs, addresses, names, emails,
  phone numbers, and Jarvis premise IDs must not be used in HA entity IDs,
  diagnostics, or logs.

Live validation:

- Yggdrasil payload must expose at least one `accountNo`.
- Jarvis `/me` must expose a non-empty `premises` array.

## Current Readiness Decision

History and AMI/PPMS chart payloads remain unsafe as direct cumulative sensor
values. They may contribute only as post-seed deltas inside the HA-owned
`manual_seed_plus_bucket_deltas` ledger, after boundary, stability,
deduplication, correction, and monotonicity rules pass.

The first positive electricity/water publication path is therefore the persisted
manual-seed ledger, not raw chart values. A future raw SP cumulative source can
replace it only after continuity and reset/monotonic semantics are proven.
