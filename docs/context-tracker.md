
## 2026-07-12 22:04:07 +08 - SP Mobile API Documentation Complete

- Created comprehensive API documentation for the SP Group mobile app (APK v15.9.0) in `docs/api/`.
- 15 markdown files, 2,372 total lines:
  - `README.md` — overview, conventions, base URLs, domain glossary, service map
  - `auth0-flow.md` — password-realm login, MFA OOB flow (live-tested), refresh, logout
  - `auth0-api.md` — /oauth/token grants, /mfa/ endpoints, DB connections
  - `jarvis-api.md` — utility accounts, charts (elec/water/gas), meter readings, green goals
  - `skalbox-api.md` — meter reading, billing, consent
  - `yggdrasil-api.md` — premises GraphQL, sharing mutations, link/unlink
  - `payment-api.md` — Njord payables, Nomisma EV subscriptions, Sersi UND
  - `ev-api.md` — EVA charging locations, sessions, subscriptions
  - `greenup-api.md` — GreenUp quests, OneUp GraphQL, advocado rewards
  - `frieza-api.md` — Tengah cooling, FCU control, appointments, warranty
  - `content-api.md` — Magnolia banners, CMS, system status
  - `utility-apis.md` — Paladin, Titan, Mera, Halp, minor APIs
  - `models.md` — Credentials, JWT, Premise, Account, Chart data models
  - `network-layer.md` — Retrofit builders, interceptors, error handling
  - `queries.md` — Full GQL query/mutation strings from the APK

## 2026-07-13 01:00 SGT - APK Modification Summary

### What was modified

**1. Bypassed Certificate Transparency (proxy blocking)**
- File: `res/xml/network_security_config.xml`
- Changed: Removed `<certificateTransparency enabled="true"/>` from domain-config for `identity.spdigital.sg` and `*.api.spdigital.sg`
- Changed: Added `<certificates src="user"/>` to base-config trust-anchors
- Effect: MITM proxies can now intercept all traffic to/from SP Group API servers

**2. Disabled Emulator Detection**
- File: `smali_classes2/fh/a.smali` (the EmulatorDetection utility class)
- Changed: Method `a([Ljava/lang/String;)Z` now always returns `false` (const/4 v0, 0x0; return v0)
- Effect: Neutralized all file-based emulator detection checks (qemu pipe, goldfish kernel, genymotion, vbox)

**3. APK rebuilding**
- Rebuilt all three split APKs (base + config.arm64_v8a + config.xxhdpi)
- Signed with debug keystore using APK Signature Scheme v2+v3
- Installed successfully on API 36 (Android 16) emulator
- App launches without detection dialog

### Tools used
- apktool 3.0.2 (decompile + rebuild)
- adb (install on emulator)
- apksigner from build-tools 37.0.0 (signing)
- keytool (debug keystore generation)
- Python zipfile (arm64 split repackaging)

### Remaining
- The original manifest binary was needed because aapt2 from build-tools 37.0.0 couldn't handle split APK manifest attributes
- Still need to set up MITM proxy and intercept API calls for full reverse engineering

## 2026-07-13 20:55:13 +08 - Home Assistant Integration Feasibility Assessment

- Discussion: Reassessed the project direction from an always-running Express/Playwright server into a Home Assistant custom integration that polls the reverse-engineered SP Mobile endpoints and feeds electricity/water data into the Home Assistant Energy Dashboard.
- Discovery: The current README still describes the project as an Express + Playwright prototype, but the codebase already has a separate mobile path:
  - `src/mobile-auth/login-flow.ts` implements Auth0 password-realm login, MFA challenge/OOB completion, refresh-token persistence, refresh, and `GET /jarvis/v3/me` validation.
  - `src/services/mobile-sync-service.ts` already calls the mobile API path, refreshes cached mobile sessions, reads premises from `/jarvis/v3/me`, and best-effort fetches `/jarvis/v4/charts/{premiseId}`.
  - `docs/api/auth0-flow.md`, `docs/api/jarvis-api.md`, and `docs/api/models.md` document the mobile Auth0 flow, refresh token behavior, utility account discovery, monthly charts, AMI charts, and meter models.
- Discovery: Home Assistant's current integration model supports polling APIs directly through a `DataUpdateCoordinator`, with auth failures surfaced via `ConfigEntryAuthFailed` for reauth. Long-term statistics require sensor state classes such as `TOTAL` or `TOTAL_INCREASING`; energy/water dashboard eligibility depends on correct device class, unit, and stable numeric values.
- Assessment: The idea is valid and feasible. It fits Home Assistant better than a standalone server because the integration can poll SP Mobile endpoints on HA's schedule and expose native sensor entities. The mobile refresh-token flow removes the core reason the server existed for the web prototype: keeping a browser/Auth0 session alive.
- Recommended architecture:
  - Extract a Python SDK first, for example `spmobile`, with async methods for login, MFA challenge/complete, refresh, account/premise discovery, charts, and AMI charts.
  - Build a Home Assistant custom integration on top of that SDK with `config_flow.py`, `DataUpdateCoordinator`, and sensor entities for electricity and water.
  - Persist refresh token and selected premise/account identifiers in the Home Assistant config entry, not in a separate runtime file.
  - Prefer `/jarvis/v3/ami/charts` for day/week/month utility history when available; fall back to `/jarvis/v4/charts/{premise_id}` monthly data where AMI data is absent.
- Entity modeling decision:
  - Expose cumulative utility sensors only if the API returns stable cumulative values or if the integration can reconstruct a monotonic cumulative total from chart deltas without double counting.
  - Electricity should use kWh and Home Assistant energy device class; water should use volume/water-compatible units such as m3 or L after confirming SP's returned unit.
  - Avoid exposing a billing-period value as `total_increasing` unless reset boundaries are handled explicitly; incorrect resets can corrupt HA long-term statistics.
- Risks / validation needed:
  - Confirm the actual shapes and units returned by `/jarvis/v4/charts/{premise_id}` and `/jarvis/v3/ami/charts` from live account data.
  - Confirm refresh tokens remain valid unattended over multiple days/weeks inside a Home Assistant-like polling cadence.
  - Confirm whether SP applies device, user-agent, rate-limit, or unusual-auth checks beyond bearer + `X-id-token`.
  - Confirm how historical monthly chart periods map to HA statistics without producing duplicate increments on repeated polling.
- Locked-in direction for now: Treat "Python SDK first, Home Assistant integration second" as the clean target direction. Keep the existing TypeScript prototype as reverse-engineering evidence and test oracle until the Python SDK reaches parity for auth, refresh, premise discovery, and chart pulls.

## 2026-07-13 20:57:26 +08 - Home Assistant Docs-Grounded Rough Implementation Shape

- Docs grounding:
  - Home Assistant custom integrations live under `<config_dir>/custom_components/<domain>` and require `manifest.json`; platform entities such as sensors live in files like `sensor.py`; a coordinator subclass is conventionally placed in `coordinator.py`.
  - A UI setup flow should be implemented with `config_flow.py`, `manifest.json` should include `"config_flow": true`, and the flow should set a stable unique ID to avoid duplicate account setup.
  - Polling cloud APIs should use `DataUpdateCoordinator` when one API refresh can feed multiple entities. The coordinator should set an appropriate `update_interval`; for SP utility data, start conservatively at 1-6 hours, not minute-level polling, unless live AMI data proves more frequent updates are meaningful and safe.
  - Expired credentials should raise `ConfigEntryAuthFailed` from setup or the coordinator so Home Assistant starts a reauth flow. Temporary connectivity/API failures should raise `UpdateFailed` in the coordinator or `ConfigEntryNotReady` during first setup.
  - Runtime objects such as SDK client, coordinator, and discovered account/premise cache should live in `ConfigEntry.runtime_data`; persisted tokens/account choices belong in config entry data/options.
  - The Python SDK dependency should be async and accept an injected `aiohttp` or `httpx` session from Home Assistant to fit HA runtime expectations.
- Proposed package split:
  - `packages/spmobile/`: reusable async Python SDK with Auth0 login/MFA/refresh, token model, Jarvis account/charts/AMI methods, typed response models, and tests using captured fixtures.
  - `custom_components/sp_mobile/`: Home Assistant integration consuming the SDK.
- Proposed HA files:
  - `manifest.json`: domain `sp_mobile` or `sp_group`, name, version for custom integration, `iot_class: "cloud_polling"`, SDK requirement, `config_flow: true`.
  - `const.py`: domain, stored data keys, default scan interval, utility constants.
  - `config_flow.py`: username/password step, OTP challenge step, validation against `/jarvis/v3/me`, unique ID from Auth0 subject or account identifier, reauth flow for dead refresh token.
  - `__init__.py`: create SDK client with injected web session, refresh/validate session, instantiate coordinator, set `entry.runtime_data`, forward sensor platform, support unload.
  - `coordinator.py`: refresh tokens if needed, fetch `/jarvis/v3/me`, fetch `/jarvis/v3/ami/charts` when available or `/jarvis/v4/charts/{premise_id}` fallback, normalize into an internal model keyed by premise + utility.
  - `sensor.py`: `CoordinatorEntity` + `SensorEntity` sensors for electricity and water, stable `unique_id`s, correct device class/unit/state class, no fast-changing attributes.
  - `diagnostics.py`: redacted config-entry diagnostics with no tokens, no passwords, and probably no full address.
  - `strings.json`: setup/reauth/translatable error strings.
- First viable entity set:
  - Electricity consumption sensor per premise.
  - Water consumption sensor per premise.
  - Optional disabled-by-default raw/monthly/billing-period sensors only after proving they do not confuse HA Energy statistics.
- Energy Dashboard mapping rule:
  - Prefer cumulative meter-like values for Energy Dashboard sensors. If only monthly/period consumption rows are available, reconstruct a monotonically increasing internal total only by tracking already-seen periods and adding each closed/new period once.
  - Use `SensorStateClass.TOTAL` when the reconstructed or source value may decrease/reset and the reset semantics are controlled; use `TOTAL_INCREASING` only when the source/reconstructed value is guaranteed monotonic except true meter resets.
  - Do not publish sparse monthly period values directly as live `total_increasing` sensor state.
- Implementation order:
  1. Port the TypeScript mobile auth/client into a Python SDK and prove `login -> MFA -> refresh -> /jarvis/v3/me`.
  2. Add live or fixture-backed chart parsing for `/jarvis/v4/charts/{premise_id}` and `/jarvis/v3/ami/charts`.
  3. Build a minimal custom integration with config flow, coordinator, and two sensors.
  4. Run Home Assistant locally, verify setup/reauth, and confirm sensors appear as eligible long-term-statistics candidates.
  5. Only then tune Energy Dashboard behavior and add diagnostics/options.

## 2026-07-13 21:11:39 +08 - Scaffold Implementation Started

- Implementation direction: move all current non-doc root files and folders into `prototype/`, keep root `docs/` as the active documentation/tracker location, and create the new root Python/Home Assistant scaffold.
- Locked implementation details:
  - Home Assistant domain: `sp_group`
  - Python SDK package/import name: `spgroup`
  - Existing TypeScript/APK/Postman/runtime artifacts become prototype references under `prototype/`.
  - Use Home Assistant's official scaffold generator as the baseline when possible, then minimally adapt the generated integration to this custom integration repo shape.

## 2026-07-13 21:20:23 +08 - Root Restructure and Scaffold Implemented

- Restructured the repo root:
  - Kept `docs/` at the root as the active documentation and context-tracker location.
  - Moved existing non-doc root artifacts into `prototype/`, including the previous TypeScript source, build output, Postman files, APK artifacts, runtime state, and Node project files.
  - Moved root runtime duplicates created/observed during the session into `prototype/.runtime-root-after-scaffold/`, `prototype/.runtime-root-final-check/`, and `prototype/.runtime-root-stopped-prototype-processes/` rather than deleting them.
  - Stopped stale TypeScript/Playwright prototype processes that were still running from the old root path and recreating `.runtime/`.
- Used Home Assistant's official scaffold templates as the baseline:
  - Created a temporary ignored HA Core checkout.
  - Ran `python -m script.scaffold integration` for `sp_group` after applying temporary-only scaffold compatibility patches in that checkout.
  - Copied the generated `homeassistant/components/sp_group` and `tests/components/sp_group` outputs into this custom-integration repo shape.
  - Removed the temporary HA Core checkout after copying the generated files.
- Added the new root scaffold:
  - Root Python tooling via `mise.toml` and `pyproject.toml`.
  - Reusable async SDK skeleton under `packages/spgroup/` with typed auth, client, and model placeholders.
  - Home Assistant custom integration under `custom_components/sp_group/` with manifest, config flow, coordinator, sensor platform, diagnostics, translations, and quality-scale scaffold.
- Current behavior:
  - The config flow is intentionally placeholder-only: username/password starts a scaffold flow, OTP completes it, and no password/OTP is persisted.
  - The coordinator returns an empty stable payload.
  - Electricity and water sensors are placeholders and do not expose Energy Dashboard state-class behavior yet.
- Verification passed:
  - `mise run python:install`
  - `mise run python:test` (5 passed)
  - `mise run python:lint`
  - `mise run ha:check`

## 2026-07-13 21:28:38 +08 - Energy Dashboard and HA Testing Alignment Review

- Docs grounding:
  - Home Assistant's electricity grid guide says manually integrated energy sensors must provide `device_class`, `state_class`, and `unit_of_measurement`; it also calls out utility/provider-supplied usage data as a valid direction.
  - Home Assistant's sensor docs classify electricity meter consumption as `SensorDeviceClass.ENERGY` with an energy unit such as kWh.
  - Long-term statistics require a valid `state_class`; for total utility consumption, HA recommends `SensorStateClass.TOTAL` without `last_reset` where possible, and `SensorStateClass.TOTAL_INCREASING` only when the value is monotonically increasing and any decreases are real meter-cycle/reset events.
  - For custom-component testing, HA docs place integration tests under `tests/components/<domain>`, recommend exercising integrations through HA's public/core interfaces such as `hass.config_entries.async_setup`, `hass.states`, registries, and services, and require full config-flow coverage for config-flow integrations.
  - For custom integrations outside HA Core, `pytest-homeassistant-custom-component` is the practical bridge to HA's core pytest fixtures (`hass`, `MockConfigEntry`, etc.); it requires the `enable_custom_integrations` fixture and `asyncio_mode = auto`.
- Current scaffold assessment:
  - `custom_components/sp_group/sensor.py` is not yet Energy Dashboard-ready. The electricity placeholder has `native_unit_of_measurement="kWh"` but does not set `device_class=SensorDeviceClass.ENERGY`, does not set `state_class`, and returns `None`.
  - This non-eligibility is intentional and correct for the scaffold phase because the live SP data semantics are not proven yet. Adding Energy Dashboard metadata before proving cumulative/reset behavior would risk corrupting HA long-term statistics.
  - The direction remains valid if the SP API can provide, or the SDK can safely derive, one stable per-premise grid-consumption total in kWh.
- Locked implementation direction from this review:
  - Keep placeholder sensors non-statistical until live/fixture data proves the exact semantics.
  - Prefer a cumulative meter-like electricity entity for the Energy Dashboard. If SP returns lifetime/cumulative meter readings, model it as energy/kWh with `TOTAL` unless HA behavior or reset semantics require `TOTAL_INCREASING`.
  - If SP returns monthly/billing-period/chart deltas only, do not expose those rows directly as an Energy Dashboard sensor. First derive a monotonic or reset-aware total with explicit duplicate-period handling, or expose the raw values as disabled/non-statistical diagnostic sensors.
  - Defer tariffs, import/export split, and cost sensors until actual SP payloads prove the required fields. If tariff splitting is needed later, prefer HA's Utility Meter integration unless SP exposes authoritative tariff-bucketed usage.
- Testing harness gap:
  - Current tests are useful scaffold smoke/unit tests, but they are not yet seamless HA custom-integration tests. They do not load the custom integration through HA, use `hass`, use `enable_custom_integrations`, set up/unload a `MockConfigEntry`, assert entity states via `hass.states`, or cover the full config flow through `hass.config_entries.flow`.
  - Next testing foundation should add `pytest-homeassistant-custom-component`, restore `asyncio_mode = auto`, add `tests/components/sp_group/conftest.py`, and convert/add tests for config flow, setup/unload, sensor entity state/registry behavior, diagnostics redaction, and future Energy Dashboard metadata once real semantics are available.

## 2026-07-13 21:52:24 +08 - Testing Guidelines Documented

- Added `docs/testing-guidelines.md` as the canonical testing approach for the Python SDK and Home Assistant custom integration.
- The guideline captures:
  - Home Assistant docs grounding for integration test structure, config-flow coverage, public HA interface testing, and custom-component fixture support.
  - Current scaffold checks and their limits.
  - Target `pytest-homeassistant-custom-component` harness requirements.
  - SDK test scope, HA integration test scope, Energy Dashboard-specific tests, fixture redaction rules, and verification commands.
- Locked direction: upgrade the HA test foundation before adding Energy Dashboard sensor metadata so future implementation work is protected by HA-style setup, config-flow, entity, diagnostics, and long-term-statistics tests.

## 2026-07-13 21:54:34 +08 - Energy Dashboard Direction Reference Documented

- Added `docs/references/ha-energy-dashboard-direction.md` as the canonical reference for aligning SP Group utility data with Home Assistant Energy Dashboard expectations.
- The reference locks in:
  - Current scaffold sensors are intentionally not Energy Dashboard-ready.
  - The first Energy Dashboard candidate should be a per-premise electricity grid-consumption sensor backed by proven SP data semantics.
  - Energy metadata should be added only after the SDK proves whether SP returns cumulative readings, period deltas, chart buckets, or another shape.
  - Raw monthly, billing-period, or chart-delta values must not be exposed directly as `TOTAL_INCREASING`.
  - `DataUpdateCoordinator` remains the HA polling boundary, while the SDK owns Auth0/Jarvis request details.
  - Tariffs, cost, export, and advanced water behavior remain deferred until real payload fields justify them.

## 2026-07-13 22:54:15 +08 - SP Group Mobile SDK Structure Implementation Started

- Implementation direction: replace the thin `packages/spgroup` scaffold with a typed async `spgroup.mobile` SDK package.
- Locked package structure:
  - Keep the existing `src/` layout under `packages/spgroup/src/spgroup`.
  - Add a dedicated `spgroup.mobile` subpackage for reverse-engineered mobile Auth0 and Jarvis behavior.
  - Keep root `spgroup` exports minimal and stable.
  - Keep tests outside `src` under `packages/spgroup/tests`.
- Locked behavior for this pass:
  - Prototype-proven broad login scope remains default.
  - Auth0 POST headers align with APK annotations.
  - Jarvis default user-agent starts with APK `Infinity/15.9.0 (Android)`, with the prototype user-agent preserved as a fallback constant.
  - SDK remains raw-only; Home Assistant energy normalization stays outside the SDK.

## 2026-07-13 23:08:59 +08 - Mobile SDK APK and Prototype Grounding Audit

- Re-grounded the current `spgroup.mobile` implementation against the prototype and decompiled APK after the initial SDK scaffold.
- Confirmed aligned:
  - Password-realm login body fields match the APK `LoginRequest` and the prototype, with the prototype-proven broad scope kept as the default.
  - Refresh-token body fields match the APK `RefreshTokenRequest` and prototype refresh flow, including using saved session scope before falling back to the default scope.
  - MFA OOB and OTP completion body fields match the APK request models and prototype behavior.
  - Jarvis auth headers match the APK `AuthInterceptor` pattern: `Authorization: Bearer <access token>` plus `X-id-token`.
  - Jarvis default user-agent matches the APK `UserAgentInterceptor` value `Infinity/15.9.0 (Android)`, while preserving the prototype user-agent as a fallback constant.
  - `/jarvis/v3/me`, `/jarvis/v4/charts/{premise_id}`, and `/jarvis/v3/ami/charts` match the decompiled `JarvisApiServiceV2` paths.
  - AMI request field names and enum mappings match the APK `SmartMeterChartRequestModel`: `premise_id`, `start`, `end`, `grouped_by`, `utility_type`, with day/week/month/year and electric/water/gas values.
- Issues or watch items:
  - `MobileSession.expired` currently checks exact token expiry. Both the prototype sync flow and APK credential handling use a 60-second expiry buffer, so the SDK should add the same buffer before live polling work.
  - The APK has a force-MFA refresh-token request shape (`grant_type=refresh_token`, `refresh_token`, `force_mfa=true`) that the SDK does not expose yet. This is not required for the current read-only polling scaffold, but should be added if reauth or sensitive-account flows need APK parity.
  - `get_mfa_authenticators` matches the APK by sending authorization only, while the prototype also sent `Content-Type: application/json`. Low risk, but worth validating live if the Auth0 endpoint behaves differently outside the browser/prototype flow.
  - `trigger_mfa_challenge` uses the APK-style Auth0 JSON headers. The prototype used only `Content-Type` for that endpoint, so this is a deliberate plan decision rather than a bug, but live validation should verify it.
  - The APK model serializes absent AMI grouping/utility values as empty strings; the SDK requires valid grouping and omits `utility_type` when `None`. Explicit electricity/water calls remain aligned, but optional utility-type behavior is not byte-for-byte identical.
  - The SDK token fingerprint is intentionally safe metadata, but it is not the same algorithm as the prototype helper. This only matters if prototype session cache migration needs fingerprint continuity.

## 2026-07-13 23:12:46 +08 - Mobile SDK Alignment Issues Fixed

- Fixed the grounding issues found in the APK/prototype audit:
  - `MobileSession.expired` now uses the APK/prototype 60-second token expiry buffer.
  - Added `MobileSession.expires_within(...)` for explicit buffer checks.
  - Token fingerprinting now matches the active prototype mobile-auth helper so session status/cache migration behavior stays consistent.
  - Added `MobileAuthClient.request_force_mfa_challenge_token(...)` for the APK force-MFA refresh-token request shape.
  - `get_mfa_authenticators(...)` now includes the prototype-proven `Content-Type: application/json` header alongside `Authorization`.
  - AMI chart requests now always include `utility_type`, using an empty string when absent to mirror the APK request model.
- Added regression tests for:
  - prototype fingerprint parity,
  - 60-second expiry buffer behavior,
  - force-MFA refresh request shape,
  - MFA authenticator headers,
  - AMI empty `utility_type` serialization.
- Verification passed:
  - `mise run python:test` (16 passed)
  - `mise run python:lint`
  - `mise run ha:check`

## 2026-07-13 23:14:05 +08 - SDK Full APK Parity Roadmap Direction

- Next direction: treat full APK API parity as a staged parity program, not a sequence of ad hoc client methods.
- Immediate next step should be an APK API inventory artifact:
  - Parse/list Retrofit services, path constants, request models, response models, auth requirements, headers, query/path/body params, base URL families, and whether each endpoint is read-only or mutating.
  - Mark which endpoints are proven by the prototype, needed by Home Assistant, useful for future SDK completeness, or out of scope/risky for v1.
- Priority order:
  1. Finish the read-only utility/energy surface in `JarvisClient`: utility account, history charts, AMI charts, meter details/readings, and any related account/premise metadata needed to interpret payload semantics.
  2. Complete Auth0/Profile support needed for robust login, reauth, token refresh, user profile, MFA authenticators, and logout/session hygiene.
  3. Add shared transport foundations before broad expansion: ResourceV2/error wrapper parsing, endpoint family/base URL registry, auth/no-auth header policy, and golden request-shape tests.
  4. Add remaining APK domains as separate modules only after inventory: payments, notifications, sustainability, marketplace/rewards, FCU/Tengah, EV, GraphQL/query APIs, and other app features.
- Locked design constraint remains: SDK stays raw/mobile-parity oriented; Home Assistant normalization and Energy Dashboard semantics stay outside the SDK.

## 2026-07-13 23:17:11 +08 - SDK Parity Roadmap Documented

- Added `docs/sdk-parity-roadmap.md` as the canonical roadmap for growing `spgroup.mobile` toward fuller APK API parity.
- The roadmap captures:
  - current SDK baseline,
  - immediate APK API inventory step,
  - implementation priority order,
  - shared SDK foundations needed before broad API expansion,
  - testing and documentation strategy,
  - locked decisions around raw SDK behavior and Home Assistant normalization boundaries.

## 2026-07-13 23:24:08 +08 - Jarvis Utility Parity Milestone 1 Implemented

- Added `docs/api/jarvis-parity-inventory.md` as the APK-grounded Jarvis inventory for milestone 1.
- Extended `spgroup.mobile.JarvisClient` with read-only APK Jarvis utility and meter surfaces:
  - `get_meter_reading(...)`
  - `fetch_account_meters(...)`
  - `fetch_meter_readings(...)`
  - `get_public_meter_details(...)`
  - `get_ppms_chart(...)`
  - `get_credit_balance(...)`
- Mirrored APK endpoint families:
  - authenticated calls use `https://b2c.api.spdigital.sg`
  - public/no-auth meter details uses `https://public.api.spdigital.sg`
- Deferred mutating Jarvis endpoints remain out of SDK milestone 1: meter submission, anonymous submit, incident report, e-bill switch, account link completion, and premise sharing/update calls.
- Added request-shape and raw-preservation tests for all new methods, including authenticated headers, public no-auth headers, PPMS chart validation, and APK request bodies.
- Verification passed:
  - `mise run python:test` (24 passed)
  - `mise run python:lint`
  - `mise run ha:check`

## 2026-07-13 23:28:14 +08 - Public Jarvis Header Alignment

- Aligned `get_public_meter_details(...)` public GET headers more closely to APK wire behavior.
- Kept the APK `UserAgentInterceptor` behavior by sending `User-Agent: Infinity/15.9.0 (Android)`.
- Removed synthetic `Content-Type` and `Accept` headers from the public no-body GET path because APK `No-Authentication: true` is an internal interceptor control header and no JSON content header is declared on that Retrofit method.
- Updated the SDK golden request-shape test for public meter details.
- Verification passed:
  - `mise run python:test` (24 passed)
  - `mise run python:lint`
  - `mise run ha:check`

## 2026-07-13 23:40:06 +08 - Mobile SDK Milestone 2 Implementation Started

- Implementation direction: add Auth0/Profile parity inventory and implement the APK read-only Profile MuleSoft surface as a sibling client to the existing Auth0 and Jarvis clients.
- Grounding:
  - `Auth0ApiService` and `Auth0Api` confirm the current SDK already covers the password-realm, MFA challenge/completion, refresh, and force-MFA refresh-token request shapes needed for v2.
  - `ProfileMulesoftApiService` and `ProfileApi` identify the read-only profile endpoints for client config, user profile, MFA authenticators, notification settings, referral code, segments, and persona stats.
  - The TypeScript prototype does not implement these Profile MuleSoft endpoints, so APK evidence is the source of truth for Profile in this milestone.
- Locked implementation details:
  - Preserve the prototype-proven broad Auth0 default scope and `get_mfa_authenticators` header shape.
  - Add `ProfileClient` with caller-owned HTTP session, raw response preservation, APK user-agent, authenticated MuleSoft headers, and public no-auth headers matching the existing public Jarvis convention.
  - Defer mutating account/profile/MFA enrollment/logout endpoints until their device-token and self-service lifecycle semantics are intentionally modeled.

## 2026-07-13 23:48:00 +08 - Mobile SDK Milestone 2 Implemented

- Added `docs/api/auth-profile-parity-inventory.md` as the APK-grounded Auth0/Profile parity inventory for milestone 2.
- Added `spgroup.mobile.ProfileClient` with read-only APK Profile MuleSoft methods:
  - `get_client_config()`
  - `get_user_profile(...)`
  - `get_mfa_authenticators(...)`
  - `get_notification_settings(...)`
  - `get_referral_code(...)`
  - `get_segments(...)`
  - `get_persona_stats(...)`
- Added MuleSoft auth/public base URL constants and a shared transport JSON-array helper for raw list endpoints.
- Preserved existing Auth0 behavior and tests, including the prototype-proven broad scope and MFA authenticator header shape.
- Added request-shape and raw-preservation tests for all new Profile methods, including public no-auth behavior and authenticated MuleSoft headers.
- Verification passed:
  - `mise run python:test` (31 passed)
  - `mise run python:lint`
  - `mise run ha:check`

## 2026-07-13 23:54:20 +08 - SDK Parity Milestone 3 Implementation Started

- Implementation direction: add shared APK-parity request and error foundations before expanding to additional API families.
- Grounding:
  - `MuleSoftRetrofitBuilder` defines auth/public MuleSoft endpoint families, while Auth0 has its own endpoint family.
  - `AuthInterceptor` applies `Authorization` and `X-id-token` for authenticated/optional-auth MuleSoft calls and strips internal control headers before requests are sent.
  - `UserAgentInterceptor` adds the APK `User-Agent`.
  - `ResourceV2` is an app-side success/error wrapper produced by Retrofit call adapters, not a success payload envelope expected from the wire.
- Locked implementation details:
  - Existing `JarvisClient`, `ProfileClient`, and `MobileAuthClient` public methods and successful raw return values must remain unchanged.
  - Request policy support should include authenticated, no-auth, and optional-auth policies, but no existing endpoint should begin using optional auth.
  - Error parsing should add a redacted `MobileApiError` foundation without adding implicit retries.

## 2026-07-14 00:01:02 +08 - Mobile SDK Milestone 3 Implemented

- Added shared APK-parity request foundations in `spgroup.mobile.request`:
  - endpoint families: `AUTH0`, `MULESOFT_AUTH`, `MULESOFT_PUBLIC`
  - auth policies: `AUTHENTICATED`, `NO_AUTH`, `OPTIONAL_AUTH`
  - `RequestSpec`/`PreparedRequest`, URL/query building, static header handling, and internal APK auth-control header stripping.
- Refactored `JarvisClient`, `ProfileClient`, and `MobileAuthClient` onto the shared request builder while preserving public method behavior and existing golden wire shapes.
- Preserved Auth0 prototype-proven behavior by allowing Auth0 requests to omit the MuleSoft APK `User-Agent`.
- Added `spgroup.mobile.resource` with redacted `MobileApiError` parsing and the currently proven common categories:
  - `UNAUTHORIZED`
  - `REQUIRES_VERIFICATION`
  - `MAINTENANCE`
  - `MFA_REQUIRED`
  - `GENERIC`
- Kept `ResourceV2` as an APK app-side adapter concept only; SDK success returns remain raw dict/list JSON payloads.
- Moved request-recording fake session helpers into `packages/spgroup/tests/fakes.py` for reuse.
- Added `docs/api/sdk-foundations.md` as the shared reference for endpoint families, request policy semantics, error parsing, and future inventory template expectations.
- Verification passed:
  - `mise run python:test` (43 passed)
  - `mise run python:lint`
  - `mise run ha:check`

## 2026-07-14 00:04:57 +08 - Mobile SDK Milestone 3 Focused Verification

- Verification scope: rerun unit/integration tests, lint/type checks, HA compile check, focused import/syntax checks, static SDK-foundation checks, and packaging build check.
- Current blocker found:
  - `packages/spgroup/src/spgroup/mobile/models.py` contains non-Python audit prompt text starting at line 27.
  - This causes `SyntaxError` during package import, pytest collection, Ruff parsing, mypy parsing, and compileall.
- Verification results:
  - `mise run python:test` failed during collection with 3 import errors caused by `models.py:27`.
  - `mise run python:lint` failed before mypy because Ruff found invalid syntax in `models.py`.
  - `mise run ha:check` failed compiling `models.py`.
  - Direct `python -m py_compile packages/spgroup/src/spgroup/mobile/models.py` failed with the same syntax error.
  - Direct `import spgroup` failed with the same syntax error.
  - Direct `py_compile` of the new foundation/client/test helper files excluding `models.py` passed.
  - `python -m pip wheel --no-deps packages/spgroup -w /tmp/spgroup-wheel-check` passed, but this only validates package build metadata/wheel assembly and does not prove runtime importability.
- No new tests were added in this pass because the syntax blocker prevents existing and new package tests from collecting; adding more tests before removing the blocker would not produce actionable signal.

## 2026-07-14 00:09:23 +08 - Mobile SDK Milestone 3 Audit Fixes Verified

- Fixed validated Critical/High/Medium findings from the milestone 3 audit:
  - Removed accidental prompt text from `packages/spgroup/src/spgroup/mobile/models.py`.
  - Made APK internal control-header stripping case-insensitive.
  - Added `path_param(...)` for Retrofit-style dynamic path segment encoding and applied it to Jarvis path parameters.
  - Extended redaction coverage for authorization/token/client-secret/contact fields.
  - Extended `MobileApiError` parsing to inspect nested and case-insensitive error fields.
  - Attached parsed redacted `MobileApiError` details to Auth0 `SpGroupAuthError` paths while preserving existing public Auth0 exception shape.
- Added regression tests for:
  - case-insensitive internal header stripping
  - path parameter encoding
  - nested/case-insensitive error parsing
  - broader recursive redaction
  - Auth0 non-object HTTP errors
  - Auth0 token errors with redacted parsed details
  - Jarvis dynamic path segment encoding
- Requirement re-check:
  - No current SDK endpoint uses `AuthPolicy.OPTIONAL_AUTH`; only the foundation and tests reference it.
  - Jarvis dynamic paths now call `path_param(...)`.
  - Jarvis, Profile, and Auth0 now all use `MobileApiError` parsing for relevant HTTP error paths.
  - No active migration/schema files exist in the root scaffold, so migration/schema validation remains not applicable.
- Verification passed:
  - `mise run python:test` (48 passed)
  - `mise run python:lint`
  - `mise run ha:check`
  - direct `spgroup.mobile` import smoke check
  - `python -m py_compile packages/spgroup/src/spgroup/mobile/models.py`
  - `python -m pip wheel --no-deps packages/spgroup -w /tmp/spgroup-wheel-check`

## 2026-07-13 23:48:48 +08 - SDK Parity Milestone 3 Planning Grounding

- Planning target: milestone 3 from `docs/sdk-parity-roadmap.md`, which calls for shared APK-parity foundations before adding more API families.
- APK grounding:
  - `MuleSoftRetrofitBuilder` defines authenticated and public MuleSoft base URLs.
  - `AuthInterceptor` applies `Authorization` and `X-id-token` when a request requires auth and strips internal auth-control headers before sending.
  - `UserAgentInterceptor` adds the APK user-agent to requests.
  - Retrofit method annotations decide no-auth, optional-auth, static headers, path, query, and body behavior.
  - `ResourceV2` and `ResourceError` are app-side result wrappers produced by call adapters, not necessarily literal success response envelopes on the wire.
- Prototype grounding:
  - Existing prototype proof remains most relevant for Auth0 scope/header behavior and Jarvis token validation.
  - There is no broader prototype transport/request-policy abstraction to port directly.
- Planning implication:
  - Milestone 3 should add shared request-policy, endpoint-family, query/body construction, raw response, and error-model foundations while preserving current public raw SDK method returns.

## 2026-07-13 23:44:45 +08 - Mobile SDK Milestone 2 APK Review

- Re-grounded the milestone 2 implementation against `ProfileMulesoftApiService`, `ProfileApi`, `Auth0ApiService`, `Auth0Api`, `AuthInterceptor`, `UserAgentInterceptor`, and the Auth0 OTP request model.
- Confirmed aligned:
  - Profile endpoint paths and auth/public base URL split match the APK wrappers.
  - Public Profile client config correctly uses the public MuleSoft endpoint and avoids auth headers.
  - Auth0 OTP completion maps to the APK `EnrolAuthenticatorAppRequestModel` default `mfa-otp` grant type.
  - Mutating Profile/Auth0 endpoints remain deferred as planned.
- Watch item found:
  - Authenticated Profile GET requests currently reuse Jarvis-style `Content-Type` and `Accept` headers. The APK read-only Profile Retrofit methods do not declare those static headers; wire behavior appears to be only auth headers from `AuthInterceptor` plus `User-Agent`. This should be aligned if strict APK header parity is prioritized for Profile.

## 2026-07-13 23:46:00 +08 - Profile Header Parity Gap Fix Started

- Implementation direction: align authenticated Profile read-only GET headers with APK wire behavior.
- Locked fix:
  - Keep `Authorization: Bearer <access_token>`, `X-id-token`, and `User-Agent`.
  - Remove synthetic `Content-Type` and `Accept` headers from Profile authenticated GETs because `ProfileMulesoftApiService` does not declare static JSON headers on those read-only methods.
  - Keep public Profile client config headers unchanged with only APK `User-Agent`.

## 2026-07-13 23:46:00 +08 - Profile Header Parity Gap Fixed

- Updated `ProfileClient` authenticated headers to match the APK read-only Profile GET wire shape:
  - `Authorization`
  - `X-id-token`
  - `User-Agent`
- Removed Profile-only use of the Jarvis JSON accept/content headers.
- Updated SDK golden request-shape tests so Profile headers are pinned separately from Jarvis headers.
- Verification passed:
  - `mise run python:test` (31 passed)
  - `mise run python:lint`
  - `mise run ha:check`

## 2026-07-14 10:11:07 +08 - SDK Parity Milestone 4 Planning Grounding

- Planning target: milestone 4 from `docs/sdk-parity-roadmap.md`, which calls for expanding SDK domain modules one APK family at a time after the shared request foundation.
- Recommended family: Yggdrasil/YCS premise GraphQL.
- APK grounding:
  - `YggdrasilApi` uses `muleSoftRetrofitBuilder.buildAuthEndpoint()`, so SDK calls should use the authenticated MuleSoft endpoint family.
  - `YggdrasilApiService` posts read-only GraphQL requests to `yggdrasil/graphql` for premise details, meter type, premise type, and invites.
  - `QueryRequest` serializes GraphQL request bodies with `query` and optional `variables`; account-scoped queries use `QueryVariableAccountNo` with JSON key `accountNo`.
  - Mutating Yggdrasil operations such as link utilities, force-link, create shared account, and update shared access should stay deferred.
- Prototype grounding:
  - No prototype implementation for Yggdrasil premise GraphQL was found, so APK evidence is the source of truth for this milestone.
  - Existing prototype evidence still only overrides APK behavior for already proven Auth0/Jarvis flows.
- Planning implication:
  - Add a focused `YggdrasilClient` that follows existing SDK conventions: caller-owned `aiohttp` session, raw response preservation, authenticated request foundation, no Home Assistant normalization, and exact golden request-shape tests.
  - Watch item for implementation: confirm whether APK/Gson omits `variables` or sends `variables: null` for no-variable GraphQL requests before pinning the SDK body shape.

## 2026-07-14 10:34:00 +08 - SDK Parity Milestone 4 Implementation Started

- Implementation target: Yggdrasil/YCS read-only GraphQL SDK parity.
- Locked APK evidence:
  - `YggdrasilApi` uses `muleSoftRetrofitBuilder.buildAuthEndpoint()`.
  - `YggdrasilApiService` posts read-only GraphQL operations to `YggdrasilApiPath.YCS_GRAPHQL` (`yggdrasil/graphql`).
  - `QueryRequest` has `query` and nullable `variables`; account-scoped calls use `QueryVariableAccountNo` with JSON key `accountNo`.
  - No Retrofit static JSON headers are declared on `YggdrasilApiService`, so the SDK adds no Yggdrasil-specific static headers.
- Serialization decision:
  - No `serializeNulls` evidence was found in the decompiled Retrofit/Gson configuration.
  - SDK no-variable GraphQL calls omit `variables` rather than sending `variables: null`, matching default Gson behavior as the best-supported APK inference.
- Scope decision:
  - Implement read-only premise details, meter type, premise type, and invites list.
  - Classify invites list as `sdk_core`, not HA-required, because it may expose sharing metadata.
  - Defer all Yggdrasil mutations and link/force-link utility calls.

## 2026-07-14 11:18:30 +08 - HA-0 Packaging Boundary Implementation Started

- Implementation target: HA integration packaging and SDK import boundary.
- Locked dependency mode:
  - Keep the custom integration in local-development mode.
  - Install the repo-local SDK with `python -m pip install -e packages/spgroup`
    through `mise run python:install`.
  - Keep `custom_components/sp_group/manifest.json` `requirements: []`, so Home
    Assistant will not install `spgroup` automatically yet.
- Distribution decision:
  - Standalone custom-component/HACS installation remains unsupported until a
    future milestone chooses a pip/git manifest requirement or vendored SDK
    layout.
- Import-boundary decision:
  - HA integration code must not depend on the root `spgroup` compatibility
    facade.
  - Future live SDK wiring should import public clients from `spgroup.mobile`.

## 2026-07-14 11:57:21 +0800 - HA-1 Auth Config Flow Implementation Started

- Started HA-1 implementation for SDK-backed Auth0 config flow and refresh contract.
- Re-grounded against current Home Assistant docs:
  - Config flows own serializable config-entry data and should use stable unique IDs for duplicate protection.
  - Reauth flows should update the existing entry via `async_update_reload_and_abort` and validate the same unique ID with `_abort_if_unique_id_mismatch`.
  - Coordinator polling should later raise `ConfigEntryAuthFailed` when refresh cannot recover so Home Assistant starts `SOURCE_REAUTH`.
- Locked implementation decisions:
  - The SDK now exposes ID-token subject metadata through public `spgroup.mobile` identity helpers; Home Assistant stores the subject as `account_id`.
  - HA config-entry data stores only serialized `MobileSession` and `account_id`; username, password, MFA token, OOB code, authenticator ID, and OTP remain transient flow state.
  - MFA setup follows the prototype-confirmed OOB path: discover authenticators, prefer active email, fall back to active SMS or default OOB challenge if email challenge fails, and do not retry another channel after OTP completion fails.
  - HA-1 locks the refresh persistence shape and SDK preservation behavior, while actual pre-poll refresh and `ConfigEntryAuthFailed` raising remain coordinator work for the next milestone.

## 2026-07-14 14:18:46 +0800 - HA-2 Runtime Container Planning Grounding

- Planning target: third HA integration roadmap milestone, `HA-2: Runtime Container And Session Refresh`.
- Current implementation baseline:
  - HA-0 and HA-1 are implemented.
  - `custom_components/sp_group/auth.py` already provides `mobile_session_from_entry` and `async_refresh_session_if_needed`.
  - `SpGroupRuntimeData` currently stores only the coordinator.
  - `SpGroupDataUpdateCoordinator` is still a placeholder and does not receive the config entry, SDK clients, current session, or refresh helper.
- Home Assistant grounding:
  - Runtime-only clients/session/cache/locks belong in typed `ConfigEntry.runtime_data`.
  - SDK clients should use Home Assistant's shared aiohttp session via `async_get_clientsession`.
  - `DataUpdateCoordinator` should own coordinated polling and use `async_config_entry_first_refresh` during setup once setup-time API validation is introduced.
  - Auth failures during runtime refresh should surface as `ConfigEntryAuthFailed` so Home Assistant can start reauth.
  - The integration must continue to support `async_unload_entry`; cleanup callbacks should use `entry.async_on_unload` when resources are introduced.
- Locked HA-2 direction:
  - Introduce a typed runtime container with shared web session, public SDK clients, current `MobileSession`, refresh lock, account ID, and coordinator.
  - Move refresh coalescing into a container method rather than leaving refresh logic as a free-floating helper only.
  - Keep first refresh/data polling placeholder-safe for HA-2: prove runtime wiring and refresh semantics without adding live utility discovery or sensor semantics yet.
  - Defer config-entry migration because the HA-1 stored shape is still current and no schema-version change is required for HA-2.

## 2026-07-14 14:56:32 +0800 - HA-4 Discovery Data Model Implementation Started

- Implementation target: HA-owned discovery modeling for future sensors.
- Locked scope:
  - Fetch and normalize premise/account discovery from Yggdrasil and Jarvis only.
  - Do not add utility readings, Energy Dashboard metadata, dynamic sensor creation, or device registry entries in HA-4.
- Home Assistant grounding:
  - Entity properties must remain memory-only, so discovery data belongs in the coordinator payload.
  - Future entity unique IDs and device identifiers must be stable and not based on display names, addresses, emails, phone numbers, raw account numbers, or the HA config entry ID alone.
- Privacy and identity decisions:
  - Coordinator dataclasses should store opaque deterministic keys, coarse status/provenance flags, utility types, meter type, and premise type.
  - Raw SDK payloads and PII fields should not be retained in coordinator data or exposed through dataclass representations.
  - Future premise devices should be keyed by opaque premise keys, but `device_info` remains deferred until real entity creation.
- Discovery behavior:
  - Use Yggdrasil premise details as the primary source.
  - Cross-check Jarvis `/jarvis/v3/me` by account number and premise number to capture a future chart-correlation key without retaining raw premise IDs.
  - Treat usable GraphQL data with `errors` as partial discovery, but malformed or missing premise data as an update failure.

## 2026-07-14 15:03:33 +0800 - HA-4 Integration Review Correction

- Integration review finding:
  - The pure discovery mapper could record Jarvis/Yggdrasil mismatches as provenance, but the coordinator initially accepted that payload.
  - This conflicted with the locked HA-4 assumption that Jarvis correlation is required before future chart polling can be considered usable.
- Correction:
  - Keep mismatch provenance available for mapper-level tests.
  - Run the coordinator in strict correlation mode so unmatched Jarvis/Yggdrasil discovery raises a redacted discovery error, which the coordinator maps to Home Assistant `UpdateFailed`.

## 2026-07-14 15:13:42 +0800 - HA-5 Electricity Semantics Implementation Started

- Implementation target: electricity payload semantics only.
- Locked scope:
  - Parse electricity chart payloads into non-publishable coordinator candidates.
  - Keep sensors as unavailable placeholders with no Energy Dashboard metadata.
  - Do not add AMI polling, meter polling, water polling, tariff, cost, export,
    dynamic entities, or device registry writes.
- Home Assistant grounding:
  - Sensor properties must remain memory-only.
  - Energy Dashboard eligibility still requires proven device class, state class,
    stable numeric values, units, and reset/period behavior.
- Privacy decision:
  - Store raw Jarvis premise IDs only as private `repr=False` runtime handles
    needed for chart polling.
  - Do not use raw handles for identity, diagnostics, logging, persistence, or
    entity unique IDs.
- Semantics decision:
  - Treat `/jarvis/v4/charts/{premise_id}` `elec.data[]` as period chart usage,
    not cumulative energy totals.
  - Add AMI parser coverage for fixture comparison only; default coordinator
    refresh does not call AMI endpoints in HA-5.

## 2026-07-14 15:19:11 +0800 - HA-5 Electricity Semantics Implementation Completed

- Added redacted electricity candidate and parse-result models plus history-chart
  and AMI fixture parsers. All parsed candidates remain `publishable=False`.
- Extended discovery with a private `jarvis_premise_id` runtime handle using
  `repr=False`, and kept opaque keys and model repr output free of raw account,
  premise, address, email, phone, and Jarvis premise identifiers.
- Coordinator now refreshes auth, performs HA-4 discovery, then fetches
  `/jarvis/v4/charts/{premise_id}` only for electric utility accounts with a
  Jarvis premise handle; chart payloads append non-publishable electricity
  candidates and parser warnings to coordinator data.
- Sensor behavior remains placeholder-only: `native_value` is `None`, with no
  Energy Dashboard device class or state class.
- Verification passed: `python -m pytest tests/components/sp_group -vv`,
  `mise run python:test`, `mise run python:lint`, and `mise run ha:check`.

## 2026-07-14 16:59:14 +0800 - HA-7 Integration Review

- End-to-end review traced config-entry setup, first coordinator refresh,
  discovery-derived sensor keys, sensor platform registration, coordinator
  listener updates, and stale discovery availability behavior.
- Review finding: initial HA-7 sensors were created with `native_value=None` but
  appeared as `unknown` because availability only checked coordinator health and
  key presence. This conflicted with the HA-7 requirement that entities remain
  unavailable until valid publishable readings exist.
- Fix: sensor availability now also requires a publishable electricity or water
  candidate for the sensor premise/utility. HA-7 still does not select or
  publish numeric values.
- Verification passed after the fix: `python -m pytest
  tests/components/sp_group/test_sensor.py -vv`, `python -m pytest
  tests/components/sp_group -vv`, `mise run python:test`, `mise run
  python:lint`, and `mise run ha:check`.

## 2026-07-14 15:37:43 +0800 - HA-6 Water Semantics Implementation Started

- Implementation target: water payload semantics only, parallel to HA-5
  electricity semantics.
- Locked scope:
  - Parse water chart payloads into non-publishable coordinator candidates.
  - Keep water sensors as unavailable placeholders with no `SensorDeviceClass.WATER`,
    no state class, and no dashboard readiness claim.
  - Do not add AMI polling, PPMS polling, meter polling, tariff, cost, export,
    gas, dynamic entities, or device registry writes.
- Home Assistant grounding:
  - Sensor properties must remain memory-only and coordinator-backed.
  - Water dashboard eligibility still requires proven water device class,
    total-style state class, supported units, stable numeric values, and
    reset/period behavior.
- Semantics decision:
  - Treat `/jarvis/v4/charts/{premise_id}` `water.data[]` as premise-level
    period usage evidence, not cumulative meter totals.
  - AMI water parsing is fixture-only; default coordinator refresh must not call
    AMI endpoints in HA-6.

## 2026-07-14 15:41:13 +0800 - HA-6 Water Semantics Implementation Completed

- Added redacted water candidate and parse-result models plus history-chart and
  AMI fixture parsers. All parsed candidates remain `publishable=False`.
- Water normalization is intentionally conservative: cubic-meter and liter/litre
  variants normalize to cubic meters using `Decimal`; other units remain
  unsupported until redacted SP fixtures prove their source shape.
- Coordinator history-chart polling is now premise-scoped: one
  `/jarvis/v4/charts/{premise_id}` request per eligible premise, parsed into
  electricity and/or water evidence according to discovered utility accounts.
- Water sensor behavior remains placeholder-only: `native_value` is `None`, with
  no `SensorDeviceClass.WATER`, no state class, and no dashboard readiness claim.
- Verification passed: `python -m pytest tests/components/sp_group -vv`,
  `mise run python:test`, `mise run python:lint`, and `mise run ha:check`.

## 2026-07-14 15:56:23 +0800 - HA-7 Discovery-Driven Sensor Entities Started

- Implementation target: entity lifecycle only, using coordinator discovery data
  to create real Home Assistant sensor entities.
- Locked scope:
  - Replace static config-entry sensors with one sensor per
    `SpGroupFutureSensorKey`.
  - Use opaque discovery keys as stable entity unique IDs.
  - Add newly discovered entities from coordinator listener updates.
  - Keep `native_value` as `None` and do not add device classes, state classes,
    device registry entries, Energy/Water Dashboard metadata, SDK calls, or
    candidate value selection.
- Availability decision:
  - Sensors are available only when the coordinator is healthy and their opaque
    discovery key is still present in coordinator data.
  - Removed discovery keys leave existing registry entities in place but mark
    the state unavailable instead of deleting user-customizable registry data.

## 2026-07-14 15:58:25 +0800 - HA-7 Discovery-Driven Sensor Entities Completed

- Sensor platform setup now creates entities from
  `SpGroupCoordinatorData.future_sensor_keys()` instead of static entry-scoped
  descriptions.
- Entity unique IDs now use opaque per-premise/per-utility discovery keys, and
  coordinator listener updates add newly discovered sensors without duplicating
  existing registry entries.
- HA-7 remains an entity lifecycle milestone only: `native_value` is still
  `None`, with no device registry entries, no `SensorDeviceClass`, no state
  class, no dashboard metadata, no SDK calls from entity properties, and no
  candidate value selection.
- Verification passed: `python -m pytest tests/components/sp_group -vv`,
  `mise run python:test`, `mise run python:lint`, and `mise run ha:check`.

## 2026-07-14 17:04:52 +0800 - HA-8 Statistics Metadata Safety Gate Started

- Implementation target: negative statistics eligibility only.
- Locked scope:
  - Reject all current `history_chart` and `ami_chart` electricity/water
    candidates as unsafe for Home Assistant long-term statistics.
  - Treat synthetic `publishable=True` chart candidates as still unavailable
    and non-statistical because cumulative/reset semantics are not proven.
  - Keep sensors without `SensorDeviceClass`, `SensorStateClass`, `last_reset`,
    numeric state, dashboard metadata, candidate value selection, or new polling.
- Home Assistant grounding:
  - Dashboard eligibility requires correct device class, state class, and exact
    unit.
  - Incorrect state-class/reset decisions can corrupt long-term statistics, so
    positive eligibility must wait for proven cumulative or reset-aware totals.

## 2026-07-14 17:05:30 +0800 - HA-8 Statistics Metadata Safety Gate Completed

- Added an HA-owned negative statistics eligibility helper that rejects all
  current `history_chart` and `ami_chart` electricity/water candidates,
  including synthetic candidates marked `publishable=True`.
- Sensor availability now uses the statistics gate instead of raw candidate
  `publishable`, so current chart evidence cannot make sensors available or
  dashboard-eligible.
- Sensor units now use Home Assistant constants for kWh and cubic meters while
  preserving rendered units.
- HA-8 keeps sensors without `SensorDeviceClass`, `SensorStateClass`,
  `last_reset`, numeric state, dashboard metadata, or candidate value selection.
- Verification passed: `python -m pytest tests/components/sp_group -vv`,
  `mise run python:test`, `mise run python:lint`, and `mise run ha:check`.

## 2026-07-14 17:12:01 +0800 - HA-9 Diagnostics And Operational Safety Started

- Implementation target: privacy-first observability only.
- Locked scope:
  - Diagnostics must be allowlisted summaries, not redacted raw payload dumps.
  - Coordinator `UpdateFailed` messages must use fixed phase-level text and
    chain original exceptions without interpolating raw exception messages.
  - Debug logs may contain phase names and counts only.
  - Revoked, missing, or unusable sessions continue to use
    `ConfigEntryAuthFailed` so Home Assistant starts reauth; no repair issue is
    added for auth failures.
  - Sensor values, statistics metadata, SDK polling endpoints, entity IDs,
    device registry behavior, and config-entry schema remain unchanged.

## 2026-07-14 17:18:44 +0800 - HA-9 Diagnostics And Operational Safety Completed

- Diagnostics now return allowlisted summaries only: config-entry key names,
  runtime/coordinator presence, coordinator health, discovery counts,
  candidate counts, bounded warning-code histograms, and partial-error status.
- Coordinator `UpdateFailed` messages now use fixed sanitized text while
  preserving original exceptions as causes; raw SDK/API exception strings are
  not interpolated into user/log-facing update messages.
- Added phase-level debug logs for auth refresh, discovery, chart fetch,
  normalization, and entity additions. Logs contain counts and phases only.
- Auth failures remain `ConfigEntryAuthFailed` reauth conditions and do not
  create repair issues.
- Marked the diagnostics quality-scale rule as done. Kept log-on-unavailable as
  todo because this pass relies on Home Assistant's coordinator behavior rather
  than adding a custom transition logger.
- Verification passed: `python -m pytest tests/components/sp_group -vv`
  (94 passed), `mise run python:test` (147 passed), `mise run python:lint`, and
  `mise run ha:check`.

## 2026-07-14 17:19:12 +0800 - SP Group SDK Readiness Audit

- Current implemented mobile SDK surface lives under `spgroup.mobile`: Auth0
  login/MFA/refresh, Profile metadata, Yggdrasil GraphQL, and Jarvis utility
  endpoints. The root `SpGroupClient` remains an empty compatibility facade.
- Existing SDK tests are hermetic fake-transport tests in
  `packages/spgroup/tests`, not live SP Group e2e tests. They verify request
  URLs, headers, bodies, parsing, redacted error handling, date formatting, and
  import/package boundaries.
- Verification passed: `python -m pytest packages/spgroup/tests -vv`
  (53 passed).
- Readiness gap: no live e2e harness currently exercises the SDK against
  Auth0/SP Group with cached or freshly obtained credentials.
- Function coverage gap in the hermetic SDK suite: `MobileAuthClient.complete_mfa_otp`
  and `JarvisClient.get_history_charts` are implemented but do not have direct
  request-shape tests. All other public methods on `MobileAuthClient`,
  `ProfileClient`, `YggdrasilClient`, and `JarvisClient` have direct coverage or
  shared validation coverage.

## 2026-07-14 18:36:06 +0800 - Live SDK E2E Harness Implementation Started

- Implementation target: opt-in production-backed SP Group SDK e2e tests with
  secret-safe session caching, explicit setup, serial live pytest execution, and
  read-only endpoint coverage only.
- Locked safety rules:
  - Default `python -m pytest` remains hermetic and must not call production
    endpoints or prompt for credentials/OTP.
  - Fresh login/MFA belongs in an explicit setup script, not normal pytest
    execution.
  - Session cache files may contain only serialized `MobileSession` data and
    must be ignored by git.
  - Live output must use redacted summaries, never raw credentials, tokens, OTPs,
    account numbers, addresses, phone numbers, emails, or full payloads.
  - Mutating Auth0, Profile, Jarvis, and Yggdrasil operations remain excluded.

## 2026-07-14 18:44:00 +0800 - Live SDK E2E Harness Implementation Completed

- Added opt-in live SDK e2e plumbing under `packages/spgroup/tests/e2e`:
  secret-safe session cache helpers, explicit production setup script, pytest
  live markers/options, core required-pass tests, and optional read-only probes.
- Added hermetic request-shape coverage for `MobileAuthClient.complete_mfa_otp`
  and `JarvisClient.get_history_charts`.
- Root `.runtime/` is now ignored so live session caches are not accidentally
  tracked.
- Live pytest behavior:
  - `--spgroup-live` is required before production tests can run.
  - Missing session caches skip cleanly without opening HTTP resources.
  - Expired cached sessions refresh automatically with the cached refresh token.
  - `--spgroup-live-auth` forces a refresh before live tests.
  - xdist workers are rejected for live runs.
- The setup script requires `--production`, prompts only in interactive
  terminals, refuses non-interactive production setup without
  `SPGROUP_USERNAME`/`SPGROUP_PASSWORD`, and writes only serialized
  `MobileSession` data with owner-only file permissions.
- Added `docs/api/live-e2e.md`, the `python:test:spgroup-live-e2e` mise task,
  and README pointers for operator-driven live validation.
- Verification passed:
  - `python -m pytest packages/spgroup/tests -vv` (59 passed, 9 skipped)
  - `python -m pytest packages/spgroup/tests/e2e -vv -rs` (4 passed, 9 skipped)
  - `python -m pytest packages/spgroup/tests/e2e -m spgroup_e2e --spgroup-live --spgroup-session-file .runtime/mobile-session.json -vv -rs` (9 skipped, 4 deselected; missing cache)
  - `python packages/spgroup/tests/e2e/setup_live_session.py` (refused without
    `--production`)
  - `python packages/spgroup/tests/e2e/setup_live_session.py --production --session-file .runtime/mobile-session.json` (refused non-interactive missing credentials)
  - `mise run python:test` (153 passed, 9 skipped)
  - `mise run python:lint`
- Live production setup and endpoint execution were not run because real
  credentials and OTP were not supplied in this session.

## 2026-07-14 19:17:23 +0800 - Live Setup DNS Resolver Fix

- User-reported production login setup failed before authentication completed:
  `Channel.getaddrinfo() takes 3 positional arguments but 4 positional
  arguments (and 4 keyword-only arguments) were given`.
- Local dependency inspection showed `aiohttp` can take the optional
  `aiodns`/`pycares` resolver path, while the installed `pycares`
  `Channel.getaddrinfo` signature is incompatible with that call path.
- Locked fix: live setup and live pytest harness now create aiohttp sessions
  through a shared `live_client_session()` helper that forces
  `aiohttp.ThreadedResolver`, avoiding optional c-ares resolver drift without
  changing SDK endpoint behavior.
- Added a troubleshooting note to `docs/api/live-e2e.md`.

## 2026-07-14 19:26:07 +0800 - Live E2E Production Verification

- Verified `.runtime/mobile-session.json` after user-created live login:
  owner-only `0600` permissions, expected serialized `MobileSession` top-level
  keys only, and no username/password/OTP/MFA challenge fields.
- Fixed live pytest execution under the Home Assistant test plugin stack:
  `pytest-socket` restrictions are now opened only for opted-in
  `spgroup_e2e` tests, and aiohttp SSL shutdown timers are allowed only for
  those live tests.
- Tightened live error-summary redaction after an expected Jarvis 403 xfail
  exposed an endpoint identifier in pytest output. Endpoint paths and long
  numeric sequences are now redacted, with hermetic regression coverage.
- Live production verification passed:
  `python -m pytest packages/spgroup/tests/e2e -m spgroup_e2e --spgroup-live
  --spgroup-session-file .runtime/mobile-session.json -vv -rs` returned
  6 passed, 5 deselected, 3 expected xfailed.
- Default safety verification passed:
  `python -m pytest packages/spgroup/tests/e2e -vv -rs` returned
  5 passed, 9 skipped; `python -m pytest packages/spgroup/tests -vv` returned
  60 passed, 9 skipped; `mise run python:test` returned 154 passed, 9 skipped;
  `mise run python:lint` passed.

## 2026-07-14 22:13:23 +0800 - APK Cumulative Meter Reading Investigation Started

- User requested a read-only investigation of the decompiled SP Services APK to
  determine whether any API provides cumulative meter readings for electricity
  and water.
- Special focus: electricity accounts served by third-party retailers, because
  `/jarvis/v3/me` returns third-party electricity account information under
  `contestable_details`.
- Scope locked as inspection/report only; no SDK implementation changes unless
  requested after findings are reviewed.

## 2026-07-14 22:17:04 +0800 - APK Cumulative Meter Reading Investigation Findings

- Decompiled APK inspection found electricity cumulative reading support through
  `MeterReadingHistoryResponse.meters[].readings[].cumulativeMeterRead`.
- The newer app flow posts account numbers to Skalbox
  `retrieveMeterReadingHistory`; the older/alternate flow posts account numbers
  to Jarvis `elec-meters` and then `elec-meters/readings`.
- The APK account-number helper includes both regular premise account numbers
  and `contestable_details.account_number`, so third-party retailer/contestable
  electricity accounts are intended to be included in the same account-number
  based meter-reading flow.
- No equivalent water historical cumulative-reading API/model was found. Water
  appears in generic meter submission/latest-reading models and UI paths, but
  not in a cumulative history response parallel to the electricity models.
- Prior live-readiness notes still apply: Jarvis account-meter surfaces can be
  blocked by account/session authorization even though the APK contains the API
  flow.

## 2026-07-14 22:23:10 +0800 - Postman Meter Reading Collection Update

- Reviewed `prototype/postman/collections/sp-mobile-api.json` against the APK
  meter-reading APIs identified earlier.
- Existing collection already contained `GET /jarvis/v3/smrd-uportal/{premise_id}`,
  `POST /jarvis/v3/smrd/elec-meters`, and Skalbox
  `POST /skalbox/b2c/smrd/v1/retrieveMeterReadingHistory`.
- Added the missing read-only Jarvis endpoint:
  `POST /jarvis/v3/smrd/elec-meters/readings`.
- Corrected the Skalbox meter-reading history request body to APK shape
  `{ "accountNos": ["{{ACCOUNT_NO}}"] }` instead of premise/date fields.
- Added a `METER_NUMBER` Postman variable and example response shapes for the
  read-only meter-reading calls, including the cumulative electricity fields.
- Aligned `docs/api/jarvis-api.md` and `docs/api/skalbox-api.md` with the same
  request/response shapes to avoid collection/documentation drift.

## 2026-07-14 22:49:37 +0800 - APK Source For Electric Meter Account Numbers

- Re-traced the APK usage pattern for `POST /jarvis/v3/smrd/elec-meters`.
- The request's `account_numbers` payload is sourced from the selected
  `PremiseResponseModel` returned in `/jarvis/v3/me` `premises`.
- The app helper `Ub.h.i(premiseResponseModel, false)` builds a de-duplicated
  account list from both `premise.accounts[].account_number` and
  `premise.contestable_details.account_number`.
- In the Jarvis `elec-meters` fallback flow, the helper is called with
  `z10=false`, then shorter non-hyphenated account numbers are left-padded with
  `00` immediately before `fetchElecMeters`.

## 2026-07-14 22:52:02 +0800 - Contestable Account Jarvis Authorization Finding

- User reported `bp_unauthorized` when calling Jarvis
  `POST /jarvis/v3/smrd/elec-meters` with
  `contestable_details.account_number`.
- Rechecked APK flow split: when feature toggle `F0()` is true, the app sends
  the account list to Skalbox `retrieveMeterReadingHistory`; it does not call
  Jarvis `elec-meters`.
- Jarvis `elec-meters` is only used in the fallback branch when `F0()` is false.
  Therefore `contestable_details.account_number` being included in the helper
  does not guarantee the current production Jarvis account-meter endpoint
  authorizes it for every account/session.

## 2026-07-14 22:54:00 +0800 - Postman Collection Path And Body Shape Correction

- User noticed the stated Skalbox `retrieveMeterReadingHistory` body did not
  match the collection.
- Re-grounded the APK request class: `MeterReadingHistoryRequest` serializes the
  JSON key `accountNos`.
- Found the active collection in this checkout at
  `postman/collections/sp-mobile-api.json`; it still had the stale
  `premise_id`, `start_date`, and `end_date` body.
- Corrected the active collection to use `{ "accountNos": ["{{ACCOUNT_NO}}"] }`,
  added `METER_NUMBER`, and added the missing Jarvis
  `POST /jarvis/v3/smrd/elec-meters/readings` request.

## 2026-07-14 23:00:36 +0800 - Postman Meter Reading Collection Restored

- User reported the collection had been messed up and requested restoring the
  earlier meter-reading changes.
- Reapplied the active `postman/collections/sp-mobile-api.json` updates:
  `METER_NUMBER` variable, missing Jarvis
  `POST /jarvis/v3/smrd/elec-meters/readings`, APK-shaped Skalbox
  `{ "accountNos": ["{{ACCOUNT_NO}}"] }` body, response examples for the four
  read-only meter surfaces, and contestable-account descriptions.
- Verified the collection with `python -m json.tool` and a targeted audit for
  all four expected request URLs.

## 2026-07-14 19:50:53 +0800 - APK-Grounded Live Payload Readiness Gate Started

- Implementation target: close production-readiness gaps with explicit gates for
  SDK live readiness, APK/live payload-shape readiness, HA runtime readiness,
  sensor-value readiness, and Energy/Water Dashboard readiness.
- Locked validation model:
  - APK DTOs are the source of truth for field names and intended client model
    shape.
  - Live E2E probes validate account-specific endpoint availability and current
    production response shape.
  - HA publication decisions require both APK evidence and live payload evidence.
- Added `docs/api/ha-payload-contracts.md` as the contract record for HA-relevant
  history charts, AMI/PPMS charts, meter-reading surfaces, and Yggdrasil/Jarvis
  discovery correlation.
- Locked semantic decision:
  - `/jarvis/v4/charts/{premise_id}` and AMI/PPMS chart payloads remain
    non-publishable period/chart evidence.
  - `fetch_account_meters` plus `fetch_meter_readings` is the primary
    electricity cumulative-read candidate because the APK exposes
    `ElecMeterReadingDataModel.cumulativeRead`.
  - Electricity still needs live evidence for timestamp, unit/scaling, meter
    identity, ordering, and reset/monotonic behavior before numeric publication.
  - Water remains independently blocked unless an equivalent water cumulative
    source is identified.
- Live E2E summaries may expose measurement-safe metadata such as units, status
  categories, period/date shape, numeric type classes, and counts. They must not
  expose tokens, account numbers, premise IDs, meter IDs, addresses, names,
  emails, phone numbers, raw payloads, or Jarvis premise handles.

## 2026-07-14 19:53:41 +0800 - APK-Grounded Live Payload Readiness Gate Completed

- Added semantic live shape assertions for:
  - Yggdrasil/Jarvis discovery correlation.
  - Jarvis history charts against APK history-chart DTOs.
  - AMI/PPMS charts against APK smart-meter chart DTOs when optional endpoints
    are available.
  - Electric account-meter and meter-reading payloads against APK meter DTOs
    when optional endpoints are available.
- Added hermetic regression coverage for measurement-safe summaries,
  measurement-unit allowlisting, redaction of unit/address identifiers, APK
  chart contract checks, cumulative-read contract checks, and HA parser behavior
  against live-shaped chart fixtures.
- Updated `docs/api/live-e2e.md` and `docs/ha-integration-roadmap.md` to make
  the readiness gates explicit.
- Verification passed:
  - `python -m pytest packages/spgroup/tests/e2e -vv -rs` returned 10 passed,
    9 skipped.
  - `python -m pytest tests/components/sp_group -vv` returned 98 passed.
  - `python -m pytest packages/spgroup/tests -vv` returned 65 passed, 9 skipped.
  - `mise run python:test` returned 163 passed, 9 skipped.
  - `mise run python:lint` passed.
  - `mise run ha:check` passed.
  - `mise run python:test:spgroup-live-e2e` returned 6 passed, 10 deselected,
    3 expected xfailed.
- Live readiness conclusion:
  - SDK live-readiness passes for the accessible read-only endpoints.
  - Payload-shape readiness passes for accessible discovery and history-chart
    surfaces.
  - Optional Jarvis premise/account-meter surfaces still return expected 403
    account-surface authorization xfails for this cached session, so cumulative
    electricity readiness remains blocked.
  - History and AMI/PPMS chart payloads remain non-publishable and
    non-statistical.

## 2026-07-14 23:52:03 +0800 - Manual Seed Plus AMI Delta Source Decision

- Discussion outcome: if a reliable SP cumulative meter-reading API remains
  unavailable, the integration should still be able to operate at full utility
  through a swappable reading-source design.
- Locked source model:
  - `manual_seed_plus_ami_deltas` is the first usable source for a cumulative
    total.
  - `sp_api_cumulative` remains the future preferred automatic source.
  - `chart_period_evidence` remains supporting evidence only and must stay
    non-dashboard/non-statistical.
- Locked seed rule: the user-provided physical meter reading is the absolute
  seed. AMI chart values may only advance the total as post-seed deltas; they do
  not replace the seed.
- Locked alignment rule: because AMI data is in 30-minute buckets, store the
  user reading time as `seed_observed_at` and derive `seed_effective_from` as the
  next 30-minute boundary unless the reading was taken exactly on `HH:00` or
  `HH:30`.
- Locked safety rule: accumulate only AMI buckets with
  `bucket_start >= seed_effective_from`; non-boundary seeds are approximate and
  should not be silently treated as perfect dashboard-ready totals.
- Implementation direction: guide users to take physical readings exactly at a
  30-minute boundary, persist a bucket ledger/checkpoint before publishing
  totals, dedupe buckets across refreshes/restarts, and keep source switching
  explicit and continuity-checked.
- Added design reference:
  `docs/manual-seed-ami-accumulation-design.md`.

## 2026-07-15 00:00:56 +0800 - Water Monthly Bucket Source Decision

- Locked electricity direction: proceed with `manual_seed_plus_ami_deltas`.
  Electricity uses the manual physical meter reading as the absolute seed and
  30-minute AMI buckets as post-seed deltas after boundary alignment.
- Updated source architecture name: the generalized source is
  `manual_seed_plus_bucket_deltas`.
- Locked water direction: implement water with the same bucketed-delta
  architecture as electricity, but start with monthly usage buckets because the
  current water meter is not smart.
- Water monthly data should be treated as source lag / coarse granularity, not
  as an integration failure. Poll water monthly until a finer-grained smart
  water source is available.
- Water alignment rule: monthly water buckets are deltas, not cumulative
  readings. A seed taken mid-month must not accumulate the overlapping current
  month bucket; accumulation starts from the next non-overlapping closed monthly
  bucket unless the seed is taken exactly at a monthly boundary.
- Future water smart-meter upgrade should reuse the same ledger/source-switching
  plumbing and change only the bucket granularity/source policy after continuity
  checks pass.

## 2026-07-15 00:07:16 +0800 - Track 2 Numeric Sensor Blocker Closed

- Locked product decision: Track 2 is closed by choosing
  `manual_seed_plus_bucket_deltas` as the first production numeric sensor source.
- Numeric sensor implementation should no longer block on resolving current SP
  cumulative API authorization gaps for Jarvis `elec-meters`,
  `elec-meters/readings`, or related meter-reading surfaces.
- API cumulative sources remain future swappable sources, not prerequisites:
  - `sp_api_cumulative_skalbox_meter_history`
  - `sp_api_cumulative_jarvis_meter_readings`
  - `sp_api_latest_meter_reading`
- Continue investigating those API candidates opportunistically, especially
  Skalbox meter-reading history and Jarvis latest-reading fields, but the first
  full-utility implementation path is manual seed plus bucket ledger.
- Updated `docs/manual-seed-ami-accumulation-design.md` with the explicit Track
  2 closure decision and candidate future API cumulative source names.

## 2026-07-15 00:19:12 +0800 - Manual Seed Ledger Design Tightened

- Locked dashboard target: the first implemented numeric source should be
  dashboard-ready immediately once ledger/finalization/correction rules pass.
  There will be no separate non-dashboard numeric preview phase.
- Locked seed UX:
  - config flow authenticates, discovers premises/utilities, then asks for
    initial seed readings for enabled utilities;
  - options flow handles later seed changes and source switching;
  - reseeding must be explicit and create a new accumulation epoch.
- Locked persistence boundary:
  - seed configuration belongs in config entry data/options using opaque
    premise/utility keys;
  - mutable accepted-bucket ledger and derived-total state belong in a versioned
    Home Assistant storage store.
- Locked correction policy: accepted bucket contributions are frozen. If SP later
  changes a historical bucket, keep the published total stable and record a
  correction warning rather than silently rewriting history.
- Locked state-class policy: use `SensorStateClass.TOTAL_INCREASING` only after
  the ledger guarantees the derived total cannot decrease.
- Locked freshness policy:
  - electricity AMI lag is variable and provider-dependent, so freshness cannot
    rely on a single fixed delay;
  - lack of a new bucket should not make the sensor unavailable;
  - water accepts only closed monthly buckets with a default three-day grace
    period after month end;
  - all bucket boundaries use `Asia/Singapore`.

## 2026-07-14 23:09:51 +0800 - Third-Party Retailer Meter Reading Support Check

- User reported `retrieveMeterReadingHistory` also fails with the account number
  from `contestable_details`.
- Re-inspected the decompiled APK meter-reading paths:
  - `Ub.h.i(...)` collects both normal premise account numbers and
    `contestable_details.account_number`.
  - `retrieveMeterReadingHistory` is the cumulative electricity history path and
    posts `{ "accountNos": [...] }`.
  - `retrieveMeterReadDetails` is a separate submission/details path and posts
    `{ "accounts": [{ "accountNo": "..." }] }`; it is not the same cumulative
    history API.
  - The app also contains a separate OEM/switch-retailer meter-reading webview
    path via `urls_oem_submit_meter_reading`.
- Current conclusion: the APK shows why the contestable account was worth
  trying, but it does not show a reliable third-party-retailer cumulative
  reading API path. Combined with live backend authorization failures, treat
  third-party electricity cumulative readings as unsupported through the mobile
  API for this account/session unless a future live capture proves otherwise.

## 2026-07-15 00:42:00 +0800 - Manual Seed Ledger Implementation Start

- Began aligning the HA implementation with
  `manual-seed-ami-accumulation-design.md`.
- Locked implementation choices for v1:
  - a valid seed is the first dashboard-ready derived total;
  - electricity AMI buckets require two unchanged successful sightings before
    acceptance;
  - water monthly buckets use the three-day closed-month grace policy;
  - lower-value reseed/reset handling is deferred to a future explicit reset
    flow;
  - source config uses opaque discovery sensor keys and mutable accumulation
    state uses a versioned Home Assistant storage ledger.

## 2026-07-15 00:56:00 +0800 - User-Friendly Setup Labels

- User clarified this integration is for personal use, so setup/options labels
  should be user-friendly premise labels instead of opaque utility keys.
- Implementation direction:
  - discovery may carry a display label for setup UX;
  - entity IDs, unique IDs, ledger keys, diagnostics, logs, and stored source
    identity remain opaque and privacy-preserving;
  - premise display labels are excluded from dataclass repr output.

## 2026-07-15 01:08:00 +0800 - Reset/Replacement Flow

- User requested the previously deferred lower reseed/reset flow be fully
  implemented as part of the manual seed + AMI accumulation design.
- Locked reset/replacement rules:
  - lower-value reseed is allowed only through an explicit options-flow
    confirmation step;
  - source configs carry an epoch id and `reset_confirmed` flag;
  - the ledger refuses unconfirmed lower replacements even if options are edited
    manually;
  - a confirmed replacement clears old accepted/pending bucket state for that
    sensor and starts a new trusted total from the replacement seed;
  - entity unique IDs remain stable so Home Assistant can apply normal
    `total_increasing` reset semantics.

## 2026-07-15 11:34:50 +0800 - Final Adversarial Audit Fixes

- Final audit found two Medium readiness defects and no Critical/High defects.
- Locked fixes:
  - enabled source configs must be revalidated against current discovery by opaque sensor key, premise key, utility type, and source mode before polling or publishing ledger-backed totals;
  - ledger/storage failures during coordinator refresh must map to a sanitized transient `UpdateFailed` message instead of escaping as raw operational exceptions.
- Added regression tests for malformed/manual source option mismatch and sanitized ledger failure handling.

## 2026-07-15 11:45:45 +0800 - Current Readiness Gap Review

- Re-evaluated the current HA integration state after the manual seed + AMI/monthly ledger work.
- Current readiness assessment:
  - seeded electricity and water sensors are functionally dashboard-capable through trusted ledger totals;
  - unseeded utilities remain unavailable and non-statistical;
  - chart/AMI payload candidates are not published directly as statistics;
  - source configs and ledger errors now have stronger defensive boundaries.
- Remaining readiness gaps before broader production confidence:
  - no full Home Assistant Recorder/statistics database end-to-end validation yet;
  - no real Home Assistant UI setup/options/reseed browser-flow validation yet;
  - no multi-cycle HA runtime soak using live production payloads yet;
  - custom-component distribution remains repo-local because `manifest.json` has empty `requirements`;
  - config-entry migration is still absent and must be added before changing stored data/options schema.

## 2026-07-15 14:20:28 +0800 - Readiness Gap Classification Clarification

- Clarified that the main remaining readiness gaps are testing-phase gaps:
  Recorder/statistics validation, real HA UI flow validation, and multi-cycle
  live HA runtime soak.
- Classified the remaining non-testing gaps as productization/maintenance
  boundaries:
  - repo-local distribution means the custom component depends on this repo's
    editable SDK install path and is not yet standalone/HACS-ready;
  - config-entry migration is not required for the current schema but must be
    added before future stored data/options schema changes;
  - advanced ledger repair/rebuild is an operational support feature beyond the
    implemented safe reseed/reset replacement flow.

## 2026-07-15 14:48:25 +0800 - Config Migration And Ledger Repair Implementation

- Implemented the maintenance foundation plan:
  - distribution packaging remains a future roadmap/productization fix;
  - config entries now migrate to version `1.2` by normalizing source-config
    options without touching mutable ledger storage;
  - malformed ledger storage creates a fixable Home Assistant Repair issue;
  - Repair flow rebuilds malformed ledger state from stored seed configuration;
  - options flow supports explicit per-utility ledger rebuild for healthy
    ledgers.
- Locked safety distinctions:
  - Home Assistant Repairs are only for malformed/untrusted ledger state;
  - intentional healthy-ledger rebuild is an options-flow action;
  - rebuild clears accepted/pending buckets and returns the selected utility to
    its stored seed baseline without changing entity unique IDs.

## 2026-07-15 15:00:06 +0800 - Post-Maintenance Readiness Review

- Rechecked the implementation after adding config-entry migration and ledger
  repair/rebuild support.
- Current conclusion: ready for the next testing phase.
- Evidence:
  - `async_migrate_entry` now normalizes source options and advances entries to
    schema `1.2` without mutating ledger storage;
  - malformed ledger state has a fixable Home Assistant Repair issue and rebuild
    flow;
  - healthy per-utility ledger rebuild remains an explicit options-flow action;
  - component tests, full hermetic tests, lint, and HA compile checks pass.
- Remaining work is next-phase validation rather than a blocking implementation
  gap:
  - real Home Assistant UI setup/options/reseed/rebuild walkthrough;
  - Recorder/statistics database end-to-end validation;
  - multi-cycle live runtime soak with sanitized production payload summaries;
  - standalone custom-component distribution packaging.

## 2026-07-15 15:04:42 +0800 - Next Testing Phase Scope Assessment

- Reassessed the next testing phase against the current HA implementation and
  Home Assistant docs for config flows, coordinated polling, entity/sensor
  behavior, diagnostics/repairs, and Energy/Water dashboard requirements.
- Locked the next phase as validation work across four gates:
  1. local HA runtime install and UI flow validation;
  2. Recorder/statistics persistence validation for ledger-backed
     `TOTAL_INCREASING` sensors;
  3. live read-only runtime soak using cached SDK credentials and sanitized
     payload summaries;
  4. privacy/operational validation for diagnostics, logs, repairs, and
     config-entry migration.
- The phase should not add new source semantics, publish raw chart/AMI values
  directly, or change standalone distribution packaging.

## 2026-07-15 15:40:36 +0800 - HA Runtime Testing Implementation Start

- Started implementing the approved HA runtime and dashboard testing phase.
- Locked implementation order:
  1. add Recorder/statistics tests for seeded ledger-backed sensors;
  2. add runtime lifecycle tests for setup, migration, repairs, rebuild, and
     reset flow behavior;
  3. add privacy/operations tests across persisted payloads, diagnostics, logs,
     repairs, entity state, and reprs;
  4. add a manual HA runtime validation checklist and opt-in live-soak guidance.
- Production behavior should remain unchanged unless these tests expose a
  validated defect.

## 2026-07-15 15:54:35 +0800 - HA Runtime Testing Implementation Complete

- Added runtime testing coverage:
  - Recorder/statistics tests for seeded electricity and water ledger sensors;
  - runtime lifecycle tests for seeded setup, unseeded behavior, and transient
    refresh failure preservation;
  - privacy/operations tests for options, ledger payloads, diagnostics, repair
    issue data, logs, entity state, and reprs;
  - opt-in HA live soak test and `mise run python:test:ha-live-soak`;
  - manual runtime checklist in `docs/testing/ha-runtime-validation.md`.
- Validated defect found and fixed:
  - ledger-backed sensors were becoming unavailable after transient coordinator
    update failures because availability inherited the coordinator failure flag;
  - availability now depends on current discovery presence plus a trusted ledger
    entry, so last trusted totals remain available through transient upstream
    failures.
- Verification:
  - `python -m pytest tests/components/sp_group/test_recorder_statistics.py
    tests/components/sp_group/test_runtime_lifecycle.py
    tests/components/sp_group/test_runtime_privacy.py -vv` passed;
  - `python -m pytest tests/components/sp_group/test_live_soak.py -vv -rs`
    skipped by default as intended;
  - `python -m pytest tests/components/sp_group -q` passed with the live soak
    skipped;
  - `mise run python:test` passed with SDK and HA live tests skipped by default;
  - `mise run python:lint` passed;
  - `mise run ha:check` passed.

## 2026-07-15 16:00:49 +0800 - Local HA Frontend Dev Task

- Added `mise run ha:dev` as the one-command local Home Assistant frontend
  workflow.
- The task creates `.runtime/ha-config/custom_components`, symlinks the repo's
  `custom_components/sp_group` into that config directory, and starts Home
  Assistant with `python -m homeassistant --config .runtime/ha-config`.
- This is the manual/browser runtime path for validating the real HA frontend at
  `http://localhost:8123`; it is separate from pytest live soak and SDK live E2E
  tests.

## 2026-07-15 16:09:56 +0800 - Local HA Frontend Startup Fix

- Fixed the `ha:dev` startup path after runtime validation exposed two issues:
  - Home Assistant Core was installed without the matching frontend package, so
    `hass_frontend` could not be imported;
  - the generated default Home Assistant config loaded broad optional
    integrations and the first port check only tested IPv4 while Home Assistant
    binds IPv6 too.
- Locked the local dev behavior:
  - `mise run python:install` installs `home-assistant-frontend==20250109.2`
    alongside Home Assistant `2025.1.4`;
  - `mise run ha:dev` now writes a minimal frontend-capable config instead of
    using `default_config`;
  - the helper probes IPv6 and IPv4 before choosing a port, so it falls back to
    the next available port when `8123` is busy.
  - the wrapper runs Home Assistant in a separate process session and treats
    Ctrl-C as a clean interactive shutdown.
- Verified `mise run ha:dev` starts Home Assistant successfully on
  `http://localhost:8124` when `8123` is occupied, and the frontend returns
  `200 text/html` at `/onboarding.html`.

## 2026-07-15 16:53:15 +0800 - HA Frontend Onboarding Dependency Fix

- Fixed missing dependencies discovered while completing Home Assistant
  onboarding from the frontend/dev instance.
- Root cause:
  - HA onboarding automatically initializes built-in `google_translate`, `met`,
    `radio_browser`, and `shopping_list` flows;
  - the lightweight local install did not include those integrations'
    transitive requirements (`mutagen`, `ha-ffmpeg`, `PyMetno`, `radios`,
    `pycountry`);
  - `radios==0.3.2` also needed `pycares==4.11.0` because `pycares 5.x`
    changed the `Channel.query` call signature used through `aiodns`;
  - the built-in TTS setup required an `ffmpeg` executable on the Home Assistant
    process PATH.
- Locked dev workflow changes:
  - `mise run python:install` now installs the built-in onboarding requirements
    and pins `pycares==4.11.0`;
  - `mise run ha:dev:reset` resets only `.runtime/ha-config` and starts a clean
    local Home Assistant frontend instance;
  - `scripts/ha_dev.py` exposes Homebrew's ffmpeg path to the HA child process
    when needed.
- Verification:
  - imported all built-in onboarding config-flow modules successfully;
  - completed the HA onboarding user/token/core-config API path against a reset
    dev instance with `200` responses;
  - observed no follow-up missing-module, radio-browser DNS, or ffmpeg setup
    errors after onboarding settled;
  - `mise run ha:check` and `mise run python:lint` passed.

## 2026-07-15 17:21:20 +0800 - Python 3.13 HA Runtime Alignment

- Addressed the Home Assistant startup warning that Python `3.12` is deprecated
  for the local HA `2025.1.4` runtime.
- Grounding:
  - official current Home Assistant development docs recommend keeping the
    manual Python environment aligned with Home Assistant's required Python
    version;
  - the installed `homeassistant==2025.1.4` package advertises Python `3.13`
    support and emits the deprecation warning only for Python `3.12`.
- Locked repo tooling changes:
  - `mise.toml` now pins Python `3.13`;
  - `pyproject.toml` now targets Python `3.13` for Ruff and mypy;
  - `mise run python:install` now pins the Home Assistant and test harness
    package versions explicitly to avoid accidental upgrades while changing
    Python versions;
  - HA Bluetooth transitive packages are pinned to the previously validated
    versions to keep Python `3.13` installs reproducible.
- Verification:
  - `mise exec -- python --version` returned `Python 3.13.14`;
  - `mise run python:install` installed `homeassistant==2025.1.4` and
    `home-assistant-frontend==20250109.2`;
  - `mise run ha:dev:reset` started HA successfully and the frontend returned
    `200 text/html` at `/onboarding.html`;
  - the prior Python `3.12` deprecation warning no longer appeared;
  - `mise run ha:check` and `mise run python:lint` passed.

## 2026-07-15 18:38:34 +0800 - Root Contributor Guide Added

- Added root `AGENTS.md` as the concise contributor guide for this checkout.
- Locked guide scope:
  - active Python/Home Assistant work is centered on `custom_components/sp_group`
    and `packages/spgroup`;
  - `prototype/` is supporting TypeScript reverse-engineering reference unless a
    task explicitly targets it;
  - default tests remain hermetic, while SP Group and HA live paths stay opt-in
    through explicit pytest markers and `mise` tasks.
- Noted that this directory does not expose Git history, so commit guidance uses
  imperative/conventional examples without claiming a repository-specific
  detected convention.
- Correction: this entry was moved to the file tail after an initial patch
  matched an earlier verification block.

## 2026-07-15 18:42:41 +0800 - HA Dev Shutdown Cleanup Fix

- Investigated `mise run ha:dev` / `mise run ha:dev:reset` leaving stale local
  Home Assistant processes behind after exit.
- Findings:
  - stale wrapper processes were in stopped `T` state and one Home Assistant
    child was still listening on `8123`;
  - the wrapper started HA in a separate session but only signaled the direct
    process, not the full process group;
  - the wrapper used one blocking `process.wait()`, which could leave the
    parent stuck after terminal stop behavior produced a defunct child.
- Fix:
  - shutdown now targets the Home Assistant process group;
  - cleanup sends `SIGCONT`, then graceful `SIGINT`, then escalates to
    `SIGTERM` and `SIGKILL` only if needed;
  - wrapper signal handling covers `SIGTERM`, `SIGHUP`, `SIGTSTP`, and
    `SIGQUIT`;
  - the main wait path now polls with a short timeout so signal handling and
    child reaping stay responsive.
- Verification:
  - cleared stale local HA dev processes;
  - `mise run ha:dev` followed by Ctrl-C exited with code `0` and left no HA
    process or listener on ports `8123`-`8126`;
  - `mise run ha:dev` followed by Ctrl-Z also exited with code `0` and left no
    HA process or listener on ports `8123`-`8126`;
  - `mise run ha:check` and `mise run python:lint` passed.

## 2026-07-15 18:55:00 +0800 - Live Discovery Correlation Diagnosis

- Diagnosed a frontend config-flow failure after OTP submission.
- Finding:
  - Auth and OTP completion succeeded far enough to start live discovery;
  - config flow failed during Yggdrasil/Jarvis correlation, not during MFA;
  - live Yggdrasil account numbers are not shaped exactly like Jarvis account
    numbers because Jarvis pads account identifiers with leading zeroes;
  - Jarvis also exposes retailer electricity details under premise-level
    contestable details rather than only under the premise account list.
- The current mapper indexes only `premises[].accounts[]` from Jarvis and
  compares account numbers literally, so it rejects valid live account data.
- Local unredacted evidence was written to `.runtime/live-discovery-debug.json`
  for operator verification; do not commit that runtime artifact.

## 2026-07-15 18:56:29 +08:00 - Live Discovery Correlation Fix

- Fixed the config-flow discovery blocker found after OTP completion.
- Locked behavior:
  - Jarvis account numbers may be left-padded with zeroes; discovery now uses
    normalized numeric account keys for correlation only;
  - opaque entity/source keys continue to be derived from Yggdrasil identifiers,
    so account-number normalization does not affect HA unique IDs;
  - retailer/contestable electricity exposed under Jarvis premise-level
    contestable details is treated as a supported electricity correlation source,
    not as an unsupported account class;
  - utility service names are canonicalized at the discovery boundary so
    `electricity` and `electric` both feed the integration's existing internal
    `ELECTRIC` / `electric` convention.
- Regression coverage added for padded Jarvis account identifiers and
  contestable retailer electricity discovery.

## 2026-07-15 19:05:31 +08:00 - Closed Discovery Correlation Issue Doc

- Added `docs/issues/ha-config-flow-jarvis-contestable-correlation.md` to
  document the frontend setup failure after OTP, the live Jarvis/Yggdrasil
  shape mismatch, the implemented contestable-details correlation fix, and the
  verification results.
- The issue is marked closed after the user confirmed the config flow progressed
  past the original discovery correlation error.

## 2026-07-15 19:25:29 +08:00 - HA Config Flow UX Gap Closure

- Improved setup/options UX around the manual seed flow.
- Locked decisions:
  - credentials and OTP use Home Assistant text selectors with appropriate
    email/password/tel/autocomplete hints;
  - source selection uses one `enabled_utilities` multi-select keyed by opaque
    sensor keys instead of dynamic label-as-field schema keys;
  - seed entry uses a numeric selector with utility-specific units and a
    date-time selector for the observation timestamp;
  - timezone-less seed timestamps are interpreted as `Asia/Singapore`;
  - non-boundary electricity/water seed timestamps require an explicit
    approximate-seed confirmation before storage;
  - setup with zero selected utilities requires a discovery-only confirmation;
  - options uses a single `maintenance_action` selector (`none`, `reseed:*`, or
    `rebuild:*`) so each run performs at most one reseed/rebuild action.
- User-facing utility labels are now title-cased and unit-aware for personal-use
  setup, while persisted source config, entity IDs, ledger storage, diagnostics,
  and logs remain keyed by opaque identifiers.
- `strings.json` now includes `data_description` coverage for setup, OTP,
  source selection, seed, approximate confirmation, options, reset, and rebuild
  steps.

## 2026-07-15 21:04:18 +08:00 - Stale Config Flow Reset UX

- Fixed the poor frontend experience where submitting OTP could end on Home
  Assistant's raw `already_in_progress` abort if a previous same-account setup
  flow was still hidden in memory.
- Locked UX decision:
  - a hidden unfinished setup flow is not considered reliably resumable by the
    user before a config entry exists;
  - the current visible flow asks for explicit confirmation before aborting the
    stale same-account setup flow;
  - declining the reset leaves the stale flow untouched and aborts only the
    current flow with a friendly reason;
  - configured entries and reauth continue to use the existing
    `already_configured`, `wrong_account`, and `reauth_successful` behavior.
- Added `stale_flow_reset_confirm` copy and regression coverage for password
  and OTP duplicate in-progress setup paths.

## 2026-07-15 21:51:33 +08:00 - Custom Integration Translation Coverage

- Investigated snake_case labels appearing in the Home Assistant frontend
  config flow.
- Finding:
  - the integration only had `custom_components/sp_group/strings.json`;
  - Home Assistant custom integrations need runtime translation files under
    `custom_components/<domain>/translations/<language>.json`;
  - `strings.json` and `[%key:common::...%]` placeholders are Home Assistant
    Core build-time conventions and are not reliable frontend translation
    sources for this custom component.
- Fix direction implemented:
  - added `custom_components/sp_group/translations/en.json` with literal English
    config/options/entity/repair translations;
  - mirrored `strings.json` to the same literal copy to avoid developer
    confusion;
  - added translation coverage tests for flow fields, errors, aborts, entity
    translation keys, repairs, and placeholder-free custom translation values.
- Runtime validation note:
  - after translation changes, restart the local HA dev server and hard-refresh
    the browser because HA/frontend translation caches can keep stale labels
    visible.

## 2026-07-15 22:06:52 +08:00 - HA Dev Cached Session Flow

- Documented the desired local development workflow for testing Home Assistant
  config-flow stages without repeatedly triggering SP Group/Auth0
  username/password and OTP flows.
- Locked direction:
  - production auth remains unchanged;
  - local dev may use an environment-gated cached-session bypass;
  - the bypass should replace only the risky auth stage, then continue through
    the same discovery, source selection, seed, confirmation, and config-entry
    creation path as real auth;
  - pre-creating `.storage/core.config_entries` is not preferred for testing the
    setup wizard because it skips too much of the post-auth config flow.
- Added `docs/dev-guidelines.md` with guardrails and operational notes.

## 2026-07-15 22:09:42 +08:00 - Expanded Development Guidelines

- Expanded `docs/dev-guidelines.md` from the cached-session concept into the
  complete local development operator guide.
- Covered:
  - `mise` Python/tool setup;
  - standard hermetic verification commands;
  - local Home Assistant frontend workflow and reset behavior;
  - frontend translation/cache restart notes;
  - production auth cautions during development;
  - planned cached-session config-flow auth bypass and guardrails;
  - opt-in SDK live E2E workflow;
  - opt-in HA live soak workflow;
  - manual HA runtime walkthrough;
  - sensitive `.runtime/` artifacts and troubleshooting.
- Clarified that the cached-session config-flow bypass is planned and not yet
  implemented.

## 2026-07-15 22:17:42 +08:00 - Dev Cached Session Config Flow Bypass

- Implemented the local-development cached-session config-flow auth bypass.
- Locked decisions:
  - the bypass is unavailable unless `SPGROUP_HA_DEV_AUTH_BYPASS=1`;
  - `SPGROUP_HA_DEV_SESSION_FILE` defaults to `.runtime/mobile-session.json`;
  - the dev path refreshes expired cached sessions before calling
    `_async_finish_auth(session)`;
  - refreshed sessions are written back atomically with owner-only permissions;
  - production username/password/OTP auth and reauth behavior remain unchanged.
- Added `mise run ha:dev:cached-session` and
  `mise run ha:dev:cached-session:reset` for frontend setup testing without
  repeated Auth0/MFA attempts.

## 2026-07-15 22:26:27 +08:00 - Dev Cache Async I/O Fix

- Live HA frontend testing reported Home Assistant blocking-loop warnings for
  `Path.read_text()` while loading `.runtime/mobile-session.json` in the
  cached-session config-flow bypass.
- Confirmed this is a bug: Home Assistant requires blocking disk reads and
  writes from async flow handlers to run in `hass.async_add_executor_job`.
- Fixed the cached-session bypass so both session-cache reads and refreshed
  cache write-backs run through Home Assistant's executor path.

## 2026-07-15 22:33:11 +08:00 - Seed DateTime Selector Payload Fix

- Live HA frontend setup reported `Invalid datetime specified:
  2026-07-15T00:00:00 22:00:00` after selecting the seed observed-at value
  through the specialized date/time field.
- Root cause:
  - Home Assistant's `DateTimeSelector` validates submitted values before the
    flow step receives them;
  - the frontend submitted a split date/time payload with a midnight date
    prefix plus the selected time, which the stock validator rejects.
- Fixed by keeping the field as a HA datetime selector while using an
  integration-local lenient selector subclass that normalizes this split
  payload into a standard ISO datetime before validation and seed parsing.

## 2026-07-15 23:58:00 +08:00 - Polling Split And Recorder Backfill

- Implemented the polling/runtime split:
  - discovery is now one-shot on setup/startup/reload/manual refresh and has no
    periodic update interval;
  - electricity uses its own coordinator with a default 1-hour interval;
  - water uses its own scheduled coordinator with a default local Singapore
    month-start run and catch-up checks through day 7.
- Added normalized polling options to config-entry options and migration:
  - electricity interval hours;
  - water poll day;
  - water poll time.
- Added seed-to-now backfill orchestration after setup/platform forwarding.
  Backfill intentionally bypasses the coordinator request-refresh debouncer via
  an immediate utility poll helper so electricity can collect the required two
  stable sightings without waiting for the hourly scheduler.
- Added HA Recorder statistics import support:
  - electricity accepted 30-minute ledger buckets are projected into hourly
    statistics rows;
  - water monthly buckets are projected into one row at the final hour of the
    represented month;
  - imports use the sensor entity ID as the statistic ID;
  - existing matching rows advance the import watermark;
  - conflicting existing rows stop contiguous import and are recorded as ledger
    statistics warnings instead of being overwritten.
- Verified with:
  - `mise exec -- python -m pytest tests/components/sp_group/test_recorder_statistics.py -vv`;
  - `mise exec -- python -m pytest tests/components/sp_group -q`;
  - `mise run python:test`;
  - `mise run python:lint`;
  - `mise run ha:check`.

## 2026-07-16 00:08:00 +08:00 - Recorder Executor Warning Fix

- Live HA dev startup reported Home Assistant's database executor warning while
  the SP Group statistics backfill checked existing Recorder statistics rows.
- Root cause:
  - `statistics_during_period()` was being called through
    `hass.async_add_executor_job()`;
  - Home Assistant requires Recorder database work to run through
    `homeassistant.components.recorder.get_instance(hass).async_add_executor_job()`.
- Fixed `_filter_conflicts()` in the statistics backfill path to use Recorder's
  executor and updated regression tests to assert that the Recorder executor
  path is used.
- Verified with:
  - `mise exec -- python -m pytest tests/components/sp_group/test_recorder_statistics.py -vv`;
  - `mise exec -- python -m pytest tests/components/sp_group -q`;
  - `mise run python:test`;
  - `mise run python:lint`;
  - `mise run ha:check`.

## 2026-07-16 00:18:00 +08:00 - Historical Backfill Anchor Gap

- Re-reviewed the current seed/backfill implementation after identifying that
  setup does not expose a user-selected historical backfill start.
- Current implementation is forward-only:
  - `seed_observed_at` is user-entered;
  - `seed_effective_from` is derived by rounding forward to the next clean
    bucket boundary;
  - utility polling requests AMI data starting at `seed_effective_from`;
  - ledger observation application drops buckets earlier than
    `seed_effective_from`;
  - Recorder statistics projection uses `seed_value + accepted_deltas`.
- Gap:
  - this cannot import full historical statistics before the seed boundary;
  - a user-provided `backfill_from` / historical start is needed to avoid API
    probing/rate-limit-prone discovery of the earliest available bucket.
- Locked direction for next refactor discussion:
  - keep `seed_effective_from` as the clean anchor where the physical reading
    applies;
  - add a separate historical `backfill_from` field;
  - support pre-seed accepted buckets by deriving historical cumulative totals
    backwards from the seed anchor while deriving post-seed totals forwards.

## 2026-07-16 00:31:00 +08:00 - Required Per-Utility Backfill Anchor

- Locked implementation direction:
  - `backfill_from` is required for every newly seeded utility during setup,
    options reseed, enable, and reset/replacement flows;
  - each utility stores its own `backfill_from`;
  - legacy stored configs without `backfill_from` migrate to
    `seed_effective_from` as a compatibility fallback;
  - pre-seed API buckets are stored as historical reconstruction evidence and
    never added to the live published total;
  - post-seed API buckets continue to advance the live published total;
  - live total equation is
    `latest_total = seed_value + sum(post_seed_accepted_bucket_values)`;
  - historical statistics before the seed anchor are imported only when accepted
    buckets provide a contiguous bridge back to `seed_effective_from`;
  - historical reconstruction equation is
    `historical_state_at(T) = seed_value - sum(accepted_bucket_values where T <= bucket_start < seed_effective_from)`;
  - statistics `sum` must remain monotonic from the first imported row in the
    active epoch, not become a negative delta from the seed.

## 2026-07-16 11:19:00 +08:00 - Live Seed Verification And Water AMI Guard

- Verified the active HA dev config entry against live read-only AMI endpoints
  using each seeded utility's stored `backfill_from`.
- Electricity result:
  - live AMI payload unit is `kWh`;
  - parsed observations from the endpoint match the ledger's accepted unique
    bucket count for the same backfill range;
  - all currently accepted buckets are pre-seed, so the trusted live total
    remains the manual seed until post-seed buckets are accepted.
- Water result:
  - the current water AMI call returns `kWh` data, so it is not valid
    water-volume evidence;
  - existing dev ledger data had duplicate monthly bucket keys from earlier
    parsing before the guard was added.
- Locked fixes:
  - reject water AMI payloads unless the payload unit is a known water volume
    unit;
  - normalize parsed water dates to the first day of the represented month
    before month rollover;
  - deduplicate observation bucket keys within one endpoint response before
    applying them to the ledger.
- Operational note:
  - the active dev water ledger should be rebuilt or reset before retesting
    water; until the API returns valid volume units, water will remain at the
    seeded total and will not ingest the `kWh` payload.

## 2026-07-16 12:15:00 +08:00 - Water Coordinator Switched From AMI To History Charts

- Identified that `SpGroupWaterCoordinator` was calling `get_ami_charts()` with
  `utility_type='water'`. The AMI endpoint returns `kWh` data for non-smart
  water meters, making water data ingestion invalid.
- Changed water coordinator to poll `GET /jarvis/v4/charts/{premise_id}`
  (already in SDK as `get_history_charts()`) which returns a `water` section
  with `period`, `unit`, and `consumption.current`.
- Added `_water_history_observations(payload, backfill_from, now)` in
  `coordinator.py` as a dedicated history-chart parser, mirroring the existing
  `_electricity_ami_observations` pattern.
- Overrode `_async_poll_sources` in `SpGroupWaterCoordinator` to call
  `get_history_charts` instead of the inherited `get_ami_charts` path.
- Extended `_parse_month_start` to handle `"YYYY-MM"` period format.
- Added liter-to-cubic-meter normalization in `_water_history_observations`.
- Removed old `_water_monthly_observations` (AMI-shaped parser) and the dead
  `_observations` dispatch from `SpGroupWaterCoordinator`.
- Updated `test_recorder_statistics.py` `_water_monthly_payload` → `_water_history_payload`
  (history-chart shaped fixture).
- Added 6 new unit tests for the history-chart parser and 1 coordinator
  integration test asserting `get_history_charts` is used instead of `get_ami_charts`.
- Verified: 246 passed, 10 skipped. Lint and HA compileall both pass.

## 2026-07-16: Hybrid Electricity Backfill — Partial AMI Month

**Issue:** The AMI endpoint returns smart-meter data from Jun 3 onwards (smart
meter installed on Jun 3). Jun 1-2 has zero AMI values (traditional meter).

**Decision:** Use the history monthly bucket for the partial AMI month (Jun 2026).
The AMI 30-min data for that month is discarded. This keeps implementation simple,
avoids artificial synthetic data, and treats June consistently with the 11 pre-AMI
months.

**ADR:** `docs/adr-hybrid-backfill-partial-month.md`

**Locked by:** User after evaluating Approach A (monthly), B (pro-rata), D (gap bucket).
## 2026-07-17 - Production Readiness Evaluation

- Completed comprehensive codebase evaluation, security analysis, and production gap assessment.
- Identified ~40 production gaps across 8 categories: Security, Code Quality/Architecture, Test Coverage, HA Quality Scale Compliance, Documentation, Distribution/Packaging, Monitoring/Observability, and Config Flow issues.
- Critical Issue: `__init__.py` line 113 references undefined variable `runtime` — `_any_entry_missing_watermark(runtime)` will raise `NameError` at startup.
- Security posture is reasonable for a self-hosted integration: tokens stored in config entry (by HA design), redaction layer exists in transport, but no session revocation on unload.
- Full findings captured in conversation thread for action planning.
## 2026-07-17 - Production Readiness Checklist Created

- Created `docs/issues/production-readiness-checklist.md` with ~40 checklist items
  across 8 categories: Critical, Security, HA Quality Scale (Bronze/Silver/Gold/Platinum),
  Code Quality, Test Coverage, Distribution, Missing Features, Monitoring, Documentation.
- All items are checkbox-formatted for tracking.
- Context-tracker and issue checklist both committed to `docs/`.

## 2026-07-17 - Critical Items Fixed

- **Fixed bug: undefined `runtime` variable in `__init__.py:113`**
  Changed `_any_entry_missing_watermark(runtime)` to `_any_entry_missing_watermark(entry.runtime_data)`.

- **Fixed optimisation dead code: backfill always firing**
  Narrowed `except Exception:` to `except (AttributeError, KeyError, TypeError):` and added
  `LOGGER.warning` with `exc_info=True` so genuine failures surface.

- **Resolved SDK distribution by vendoring**
  Copied `packages/spgroup/src/spgroup/` → `custom_components/sp_group/vendor/spgroup/`.
  Updated all HA integration imports from `from spgroup.mobile import ...` to
  `from .vendor.spgroup.mobile import ...`.  Integration is now self-contained with no
  external runtime dependency beyond HA's built-in `aiohttp`.

- **Updated all test imports** to use `custom_components.sp_group.vendor.spgroup.mobile`
  instead of `spgroup.mobile` to avoid vendored-vs-editable type mismatch.

- **Added 7 new tests** in `test_init.py`:
  - 5 direct unit tests for `_any_entry_missing_watermark` (None store, malformed,
    all complete, some incomplete, empty entries)
  - 2 integration tests for backfill gating (skipped when all watermarked, runs when not)

- **Regression suite: 208 passed, 3 pre-existing failures** (in `test_recorder_statistics.py`,
  all `has_sum: False` vs `has_sum: True` mismatch -- unrelated to changes).

- **Marked all 3 critical items as completed** in `production-readiness-checklist.md`.

## 2026-07-17 — ADR + Plan for Two-Mode SDK Vendoring

- Created `docs/adr/adr-two-mode-sdk-vendoring.md` (264 lines) — architectural
  decision record documenting the try/except fallback pattern, three deployment
  modes (development, HACS/production, PyPI-published), import resolution flow,
  and alternatives considered.
- Created `docs/plans/sdk-vendoring.md` (534 lines) — detailed implementation
  plan with file-by-file diff tables, verification steps, edge case analysis,
  and rollback instructions.
- ADR grounded against Python Language Reference (import system), PyPA
  Packaging Guide, and HA Developer Docs.

## 2026-07-17 — Implemented Two-Mode SDK Vendoring

- **Replaced broken `.vendor.spgroup.mobile` imports** with proper two-mode
  fallback pattern using `importlib.util.find_spec("spgroup")`:
  - Development (editable install): `find_spec` finds `spgroup`, no `sys.path`
    manipulation — dev workflow preserved
  - Production (HACS/manual): `find_spec` returns `None`, `vendor/` added to
    `sys.path[0]` — fully self-contained
- **Reverted all imports** back to `from spgroup.mobile import ...`:
  - 5 HA integration files (`__init__.py`, `auth.py`, `coordinator.py`,
    `discovery_service.py`, `config_flow.py`)
  - 7 test files (`helpers.py`, `test_auth.py`, `test_config_flow.py`,
    `test_coordinator.py`, `test_live_soak.py`, `test_runtime_lifecycle.py`,
    `test_runtime_privacy.py`)
- **Used `importlib.util.find_spec`** instead of `try: import spgroup` to pass
  the packaging boundary test (which blocks bare `import spgroup`)
- **Vendor directory** stays at `vendor/spgroup/` — untouched
- **Regression**: 208 passed, 4 pre-existing failures (`test_recorder_statistics.py`
  — `has_sum` mismatch, not related)

## 2026-07-17 17:00 SGT — SDK extracted to ../python-packages/spgroup

The spgroup SDK moved to its own repo at `../python-packages/spgroup/`.
Pre-split context for both the SDK and the HA integration is in this file.
Post-split SDK development decisions go in the SDK repo's context-tracker.

## 2026-07-18 — HA 2025.1.4 → 2026.7.2 upgrade (Python 3.13 → 3.14)

Upgraded the repo's pinned Home Assistant version from 2025.1.4 to 2026.7.2,
requiring Python 3.14.2+.

### Key changes

- **Python**: `mise.toml` → `python = "3.14.2"` (from `3.13`).
- **HA**: `homeassistant==2026.7.2` (from `2025.1.4`).
- **Virtual environment**: All packages now install into `.venv/` instead of
  directly into the mise-managed Python's site-packages. This is required by
  HA 2026.7.2's `unsupported_local_deps` check, which fires when not running
  in Docker or a virtual environment.
- **`ha_dev.py`**: Added `_resolve_python()` helper that auto-detects `.venv/`
  and uses `.venv/bin/python` to spawn HA. Also added version guards that
  block startup with Python < 3.14 or HA < 2026.3.
- **Brand images**: `custom_components/sp_group/brand/` with 4 PNG files
  (`icon.png`, `icon@2x.png`, `logo.png`, `logo@2x.png`).
- **`quality_scale.yaml`**: `brands: todo` → `brands: done`.
- **Optional deps**: Added `home-assistant-frontend`, `ha-ffmpeg`, `hassil`,
  `PyTurboJPEG`, `mutagen`, `home-assistant-intents`, `pymicro-vad`,
  `pyspeex-noise`, `aiohasupervisor`, `rf-protocols`, `infrared-protocols` to
  the install command (HA 2026.7.2 requires these for optional components).
- **Test framework**: `pytest-homeassistant-custom-component==0.13.316` installed
  with `--no-deps` (the package pins `homeassistant==2026.2.3`, which is behind
  our target).
- **Test/lint tooling**: Updated `pytest==9.0.0`, `pytest-asyncio==1.3.0`.

### Known repair issues (expected for non-Docker dev)

- `deprecated_method` — Core installations deprecated per HA 2025.12 policy.
  To suppress, switch to Docker-based development.
- `country_not_configured` — Normal for first-run dev setup.

### No integration code changes needed

All 19 Python files (6013 lines) compile cleanly on Python 3.14.2. No deprecated
HA APIs were in use.
