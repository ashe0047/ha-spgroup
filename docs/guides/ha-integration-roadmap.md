# Home Assistant Integration Roadmap

This roadmap starts after the SDK parity milestones needed for electricity and
water sensor work are complete. The SDK remains raw and APK-parity oriented.
The Home Assistant integration owns packaging, session persistence, coordinator
polling, payload interpretation, entity design, diagnostics, and Energy
Dashboard safety.

## Source Grounding

Home Assistant references:

- Fetching data and `DataUpdateCoordinator`:
  `https://developers.home-assistant.io/docs/integration_fetching_data/`
- Sensor entity model:
  `https://developers.home-assistant.io/docs/core/entity/sensor/`
- Energy Dashboard FAQ:
  `https://www.home-assistant.io/docs/energy/faq/`
- Integration manifest:
  `https://developers.home-assistant.io/docs/creating_integration_manifest/`
- Integration file structure:
  `https://developers.home-assistant.io/docs/creating_integration_file_structure/`
- Config entries and lifecycle:
  `https://developers.home-assistant.io/docs/config_entries_index/`
- Reauthentication flow:
  `https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/reauthentication-flow/`
- Config entry unloading:
  `https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/config-entry-unloading/`
- Repairs:
  `https://developers.home-assistant.io/docs/core/platform/repairs/`
- Existing project energy direction:
  `docs/references/ha-energy-dashboard-direction.md`
- Existing project testing guidelines:
  `docs/guides/testing.md`

SDK surfaces available for the integration:

- `MobileAuthClient`: password login, MFA challenge/completion, session refresh,
  force-MFA request shape.
- `JarvisClient`: `/jarvis/v3/me`, history charts, AMI charts, meter reading,
  electric meter metadata/readings, public meter details, PPMS chart, credit
  balance.
- `YggdrasilClient`: premise details, meter type, premise type, invites list.
- `ProfileClient`: read-only profile/session interpretation endpoints.

## Home Assistant Conventions To Preserve

- Keep the custom integration under `custom_components/sp_group`.
- Keep HA integration responsibilities in HA files:
  - `manifest.json` for metadata, dependencies, `iot_class`, version, and
    requirements;
  - `__init__.py` for config-entry setup/unload and platform forwarding;
  - `config_flow.py` for UI setup, MFA, reauth, and future reconfigure/options
    flows;
  - `coordinator.py` for coordinated polling and API error mapping;
  - `sensor.py` for memory-only sensor entities;
  - `diagnostics.py` and optional `repairs.py` for support surfaces.
- Store runtime-only objects in `ConfigEntry.runtime_data`, not in
  `hass.data`.
- Never mutate `ConfigEntry.data` or `ConfigEntry.options` directly; use Home
  Assistant's config entry update APIs.
- Implement and maintain `async_unload_entry`; register cleanup callbacks with
  `entry.async_on_unload` when runtime resources need cleanup.
- Add `async_migrate_entry` before changing config-entry data schema versions.
- Use `DataUpdateCoordinator` for the cloud-polling API. Entity properties must
  read coordinator memory only and must not perform network I/O.
- Use `CoordinatorEntity` for sensors so availability and update lifecycle are
  tied to the coordinator.
- Use `SensorEntityDescription`, `native_value`, and
  `native_unit_of_measurement`; do not implement legacy `state` or
  `unit_of_measurement` properties.
- Add device classes, state classes, and long-term statistics metadata only
  after HA's sensor and Energy Dashboard requirements are proven by tests.
- Keep user-facing errors, flow labels, entity names, repairs, and diagnostics
  translatable through the integration's translation files.

## Target Data Flow

The integration should use one config entry and one `DataUpdateCoordinator` per
SP Group account.

1. Config flow authenticates through the SDK, including MFA when required.
2. Config entry stores only serializable token/session material and a stable
   account identity. It must not store username/password/OTP/MFA transaction
   secrets after setup.
3. Runtime data owns the shared Home Assistant aiohttp client session, SDK
   clients, current `MobileSession`, refresh lock, and coordinator.
4. Coordinator refresh:
   - refreshes the mobile session when needed;
   - discovers premises/accounts using Yggdrasil and Jarvis;
   - fetches raw electricity/water utility payloads;
   - maps raw SDK payloads into an HA-owned typed coordinator model;
   - raises Home Assistant auth/update exceptions for reauth and transient API
     failures.
5. Sensor entities read only from coordinator memory. Sensor properties must not
   perform network I/O.
6. Diagnostics expose redacted integration state only.

## HA-0: Packaging And Integration Boundary

Goal: decide how the custom integration will load the SDK outside the monorepo
development environment.

- Current HA-0 decision: keep local-development editable install mode. The SDK
  is installed with `python -m pip install -e ../../../../python-packages/spgroup` via
  `mise run python:install`, and `manifest.json` keeps `requirements: []`.
- This means standalone custom-component installation is not supported yet.
  Before HACS/manual distribution, choose a pip/git manifest requirement or a
  vendored SDK layout.
- Future productization fix: do not block the current HA testing phase on
  distribution packaging. Before standalone/HACS use, pick one supported
  packaging mode, update `manifest.json` requirements accordingly, and verify
  install in a clean Home Assistant environment.
- Choose one packaging strategy:
  - bundle the SDK package with the custom integration;
  - publish/install `spgroup` as a manifest requirement;
  - or keep development-only editable install with explicit docs.
- Update `manifest.json`, local install docs, and test setup to match the chosen
  strategy. Custom integration `manifest.json` must continue to include a valid
  `version`, `config_flow: true`, `integration_type: "hub"`, and
  `iot_class: "cloud_polling"`.
- Keep the import boundary explicit: HA integration code should import public
  SDK symbols from `spgroup.mobile`, not private SDK modules.
- Declare SDK/runtime dependency loggers in `manifest.json` if SDK logging is
  introduced.

Verification:

- A Home Assistant test environment can import `spgroup.mobile` the same way a
  user install will.
- Manifest requirements and local test/development install instructions do not
  contradict each other.
- Package/wheel checks include the SDK modules needed by the integration.

## HA-1: Real Auth Config Flow

Goal: replace placeholder auth with SDK-backed login while keeping secrets out
of stored config entries.

- Replace `PlaceholderAuth` with `MobileAuthClient`.
- Model the Auth0 flow explicitly:
  - username/password step;
  - MFA required branch;
  - authenticator/challenge handling where needed;
  - OTP completion step.
- Carry transient MFA state only inside the config-flow instance.
- Store only serialized `MobileSession` data and a stable account identity in
  the config entry.
- Do not persist username, password, OTP, MFA token, OOB code, or authenticator
  challenge secrets after setup.
- Replace username-based unique IDs with a stable identity such as Auth0 subject
  or Profile IAM ID. If the SDK does not expose a safe stable identity yet, add
  that SDK metadata before completing this milestone.
- Use Home Assistant config-flow APIs for duplicate prevention and reauth:
  `_abort_if_unique_id_configured`, `_abort_if_unique_id_mismatch`, and
  `async_update_reload_and_abort` where applicable.
- Keep config-flow errors mapped to translated keys such as `cannot_connect`,
  `invalid_auth`, `mfa_required`, and `unknown`.

Verification:

- Tests cover successful password login, MFA required, OTP completion, invalid
  auth, cannot connect, duplicate entry, and unknown error handling.
- Tests assert stored entry data excludes password, OTP, MFA token, and OOB code.
- Tests assert the config entry unique ID is stable and not derived from mutable
  display text.
- Reauth tests verify wrong-account prevention and entry data updates without
  creating a second config entry.

## HA-2: Runtime Container And Session Refresh

Goal: create the runtime SDK container and reliable session refresh behavior
before polling utility data.

- Use Home Assistant's shared aiohttp client session via the HA helper rather
  than creating per-request or per-client HTTP sessions.
- Runtime data should contain:
  - SDK clients;
  - current `MobileSession`;
  - a refresh lock;
  - coordinator;
  - minimal non-secret account identity.
- Refresh the mobile session before polling when `MobileSession.expired` is
  true.
- Persist refreshed session data back to the config entry using Home Assistant's
  config entry update API.
- Coalesce concurrent refresh attempts so only one refresh call runs at a time.
- Register cleanup through `entry.async_on_unload` for any listeners or owned
  resources. The shared HA aiohttp session must not be closed by this
  integration.
- Introduce config-entry migration before changing the stored session schema.

Verification:

- Tests prove one shared HA HTTP session is used for SDK clients.
- Expired access tokens trigger refresh before polling.
- Refreshed sessions are persisted.
- Concurrent refresh requests do not issue duplicate refresh calls.
- Missing/revoked/invalid refresh tokens trigger reauth.
- Unload tests prove platforms unload and runtime cleanup callbacks run.

## HA-3: Coordinator Error Boundaries

Goal: establish Home Assistant setup and polling failure semantics before adding
real entity values.

- Use `DataUpdateCoordinator` and `async_config_entry_first_refresh` during
  setup.
- Pass the config entry to the coordinator and create comparable data structures
  so `always_update=False` can be considered once equality semantics are stable.
- Perform one-time discovery/setup in coordinator `_async_setup` when the data
  only needs to be loaded before the first refresh.
- Map unrecoverable auth failures to `ConfigEntryAuthFailed` so Home Assistant
  starts reauth.
- Map utility transport/API/network outages to successful no-op poll results
  with no ledger mutation; map malformed/data-integrity failures to
  `UpdateFailed`, and expired credentials to `ConfigEntryAuthFailed`.
- Use bounded API timeouts during refresh.
- Preserve last known good coordinator data across transient refresh failures.
- Keep the update interval conservative, starting from the existing six-hour
  default unless live behavior proves a better cadence.
- Do not add per-entity polling; all network calls go through the coordinator.

Verification:

- Initial setup retry behavior is tested.
- Transient update failure is tested.
- Auth failure and reauth trigger are tested.
- Successful refresh after a prior failure is tested.
- Existing good coordinator data is not unexpectedly cleared by a transient
  failure.
- Tests assert sensor properties do not call SDK/network methods.

## HA-4: Discovery Data Model

Goal: build the integration-owned typed model that entities will consume.

Discovery sources:

- Use `YggdrasilClient.get_premises_details` as the primary premise/account
  structure source.
- Use `YggdrasilClient.get_meter_type(account_no)` and
  `get_premise_type(account_no)` to classify account behavior.
- Use `JarvisClient.get_me` as the Jarvis utility-account cross-check.

Coordinator model:

- Replace placeholder `SpGroupCoordinatorData` with immutable typed structures
  for:
  - premises;
  - accounts;
  - utility availability per premise;
  - meter/premise type;
  - safe source provenance flags;
  - optional electricity/water readings after semantics are proven.
- Store only minimal identifiers required for stable mapping.
- Do not store raw SDK payloads in runtime coordinator data unless they are
  redacted, bounded, and explicitly needed for a test/debug pathway.
- Use stable unique IDs based on safe, non-display identifiers. Do not use
  names, addresses, or config-entry IDs as the only long-term entity identity.
- Define device registry strategy before entity creation:
  - whether one device represents the SP Group account, each premise, or each
    utility account;
  - which identifiers are safe and stable enough for `device_info`;
  - how removed or no-longer-visible premises are handled.
- Keep entity names translated and rely on `has_entity_name = True`.

Verification:

- Redacted fixtures cover at least one premise with electricity and water.
- Tests cover missing utility sections, multiple premises, shared premises,
  unknown fields, and mismatched Jarvis/Yggdrasil data.
- Tests prove entity creation can be driven by coordinator discovery data, not
  static placeholder descriptions.
- Tests prove removed/missing premises do not create duplicate replacement
  entities with new unique IDs.

## HA-5: Electricity Payload Semantics

Goal: prove which electricity payload can safely feed a Home Assistant sensor.

Candidate SDK calls:

- `JarvisClient.get_history_charts(premise_id)`.
- `JarvisClient.get_ami_charts(..., utility_type="electric")`.
- Electric-specific meter calls where available:
  `fetch_account_meters` and `fetch_meter_readings`.

Questions to resolve with redacted fixtures or opt-in live validation:

- Which field contains the numeric value?
- Which field identifies the unit?
- Is the value cumulative, a billing-period delta, a chart bucket, or a latest
  meter reading?
- Does the API distinguish complete periods from in-progress periods?
- Are AMI and non-AMI payloads semantically compatible?
- Can repeated polling double count the same source period?

Rules:

- Use `Decimal` internally for numeric conversion.
- Normalize electricity to kWh before publication.
- Do not publish a numeric sensor state for uncertain payloads.
- Do not convert chart buckets into totals until duplicate-period and restart
  behavior is handled.
- Do not enable Energy Dashboard metadata until the state semantics pass the
  tests listed in `docs/references/ha-energy-dashboard-direction.md`.
- If a value must be restored across restarts for safe accumulated totals,
  decide whether the entity needs `RestoreSensor` and document the reason before
  implementing it.

Verification:

- Fixtures prove electricity unit/value extraction.
- Tests cover malformed values, missing units, unsupported units, repeated
  periods, partial periods, AMI/non-AMI differences, and API/GraphQL error
  payloads.
- Repeated coordinator refreshes do not double count the same period.

## HA-6: Water Payload Semantics

Goal: validate water separately instead of assuming it behaves like electricity.

Candidate SDK calls:

- `JarvisClient.get_history_charts(premise_id)` water section.
- `JarvisClient.get_ami_charts(..., utility_type="water")`.

Questions to resolve with redacted fixtures or opt-in live validation:

- Does water have a meter-reading endpoint equivalent, or only chart payloads?
- Which field contains the numeric value?
- Which field identifies the unit?
- Is the value cumulative, a period delta, a chart bucket, or another shape?
- Does water use a Home Assistant-supported unit without lossy conversion?

Rules:

- Treat water as a separate readiness branch from electricity.
- Do not enable water sensors merely because electricity is ready.
- Publish water only after unit/value semantics are proven.
- Add water-specific device/statistics metadata only after Home Assistant water
  requirements and payload semantics are proven.
- Keep water disabled/unavailable when only uncertain chart payloads are
  available.

Verification:

- Fixtures prove water unit/value extraction.
- Tests cover chart-only data, missing water payloads, malformed values,
  unsupported units, repeated periods, and uncertain data producing unavailable
  or `None` state.

## HA-7: Sensor Entity Implementation

Goal: expose real Home Assistant sensor entities backed by coordinator data.

- Create entities from coordinator discovery data, not static sensor
  descriptions.
- Use `CoordinatorEntity`; entity properties read memory only.
- Create stable per-premise/per-utility unique IDs.
- Add `device_info` for premises/accounts only if identifiers are safe and
  stable.
- Mark entities unavailable when coordinator data lacks a valid current reading.
- Consider disabled-by-default diagnostic entities only after the primary
  sensors are stable.
- Avoid frequently changing `extra_state_attributes`; create separate entities
  for meaningful changing values instead of bloating the recorder database.
- Specify `PARALLEL_UPDATES` only if platform-level entity update behavior is
  introduced. For coordinator-backed sensors, keep network polling centralized.

Verification:

- HA setup tests show entities in `hass.states`.
- Entity registry tests verify stable unique IDs.
- Availability tests cover failed coordinator refresh and missing utility data.
- Tests cover multiple premises, removed premises, disabled entities, and
  display-name/address changes.

## HA-8: Statistics Metadata Gate

Goal: add long-term statistics metadata only after source semantics are proven.

- Add `SensorDeviceClass.ENERGY` and `SensorStateClass.TOTAL` or
  `TOTAL_INCREASING` to electricity only after cumulative/reset semantics are
  proven.
- Prefer HA's current guidance for total sensors: use `SensorStateClass.TOTAL`
  without `last_reset` when possible, and use `TOTAL_INCREASING` or
  `TOTAL` with `last_reset` only when the source semantics require it.
- Add water-specific device class/statistics metadata only after water semantics
  are separately proven.
- Do not expose raw chart or bucket values as Energy Dashboard-ready totals.
- Do not add tariffs, costs, or export sensors in this gate.
- Use Home Assistant constants for units, such as `UnitOfEnergy.KILO_WATT_HOUR`,
  rather than string variants that could break statistics.

Verification:

- Statistics metadata tests assert device class, state class, native unit, and
  state behavior.
- Tests prove monotonic/reset handling for `TOTAL_INCREASING` or reset handling
  for `TOTAL`.
- Tests prove raw bucket values cannot accidentally be marked as statistics
  totals.

## HA-9: Diagnostics And Operational Safety

Goal: make failures understandable without leaking SP account data.

- Redact tokens, account numbers, premise IDs, addresses, names, emails, phone
  numbers, IAM IDs, and raw payload identifiers.
- Add phase-level debug logging only:
  - auth refresh;
  - discovery;
  - fetch;
  - normalization;
  - entity update counts.
- Avoid logging raw API payloads by default.
- Add repairs or reauth triggers for revoked sessions and unrecoverable auth
  failures.
- Keep diagnostics bounded and avoid exposing raw payloads.
- Create repair issues only for user-actionable problems; delete or clear them
  when the condition is resolved.
- Keep repair and diagnostic messages translated.
- Log service/API outages once when the service becomes unavailable and once
  when it becomes available again, rather than on every poll.

Verification:

- Diagnostics tests assert no sensitive values are present.
- Auth-failure tests trigger reauth.
- Transient failures do not wipe last known good state.
- Debug logs contain phase summaries, not raw API data.
- Repair issue tests cover creation, persistence choice, and deletion when the
  issue is resolved.

## Minimum Path To Sensor Integration

The minimum path before real electricity/water sensors can replace placeholders
is:

1. HA-0 packaging/import boundary.
2. HA-1 real auth config flow.
3. HA-2 runtime session and refresh.
4. HA-3 coordinator error boundaries.
5. HA-4 discovery data model.
6. HA-5 electricity semantics and/or HA-6 water semantics, depending on which
   utility is being enabled first.
7. HA-7 sensors for only the utilities whose semantics are proven.

HA-8 is required before Energy Dashboard/statistics metadata is enabled. HA-9
can proceed alongside the sensor milestones, but diagnostics redaction and reauth
behavior should be complete before broad real-world use.

## Production Readiness Gates

The completed roadmap still requires an APK-grounded live validation gate before
production use:

1. **SDK live-ready:** `.runtime/mobile-session.json` refreshes successfully and
   read-only SDK endpoints pass opt-in production E2E checks, with optional
   `401`/`403` account-surface gaps documented as expected xfails.
2. **Payload-shape ready:** live responses match the APK DTO contracts recorded
   in `docs/guides/ha-payload-contracts.md`, or mismatches are documented before
   changing parser assumptions.
3. **HA runtime-ready:** config-entry setup, refresh, coordinator polling,
   diagnostics, logs, and dynamic sensor creation remain privacy-preserving and
   bounded under hermetic HA tests.
4. **Sensor-value ready:** numeric electricity or water states are exposed only
   after a source proves unit, timestamp, source identity, duplicate-period, and
   reset/monotonic behavior.
5. **Dashboard-ready:** Energy/Water Dashboard metadata is enabled only after
   Home Assistant `device_class`, `state_class`, and unit requirements are met
   by proven cumulative/reset-safe semantics.

Current status update as of 2026-07-15: history and AMI/PPMS chart payloads
remain non-publishable period/chart evidence by themselves. The first numeric
dashboard-ready source is now the HA-owned `manual_seed_plus_bucket_deltas`
ledger described in `docs/guides/manual-seed-ami-accumulation-design.md`: a user seed is
the initial trusted cumulative total, and accepted electricity AMI or water
monthly buckets advance that total only after ledger safety rules pass.
`fetch_account_meters` plus `fetch_meter_readings` remains a future raw SP
cumulative-source candidate, not a blocker for the first full-utility sensor
path.

## Explicit Non-Goals

- Do not add tariffs, cost, export, payment, premise-linking, or account-sharing
  behavior during the first sensor integration pass.
- Do not perform SP account mutations from Home Assistant.
- Do not make the SDK normalize HA entities; keep HA mapping in
  `custom_components/sp_group`.
- Do not store username/password/OTP/MFA transaction secrets in config entries.
- Do not keep unredacted raw SP payloads in diagnostics.
- Do not claim Energy Dashboard readiness before value semantics and
  long-term-statistics requirements are proven by tests.
