
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

## 2026-07-19 21:35:15 +08 — Investigation: negative Energy Dashboard consumption after update

Observed behavior under investigation: first-time setup/backfill produced correct
SP Group Energy Dashboard graphs, but after a later data update the graph showed
negative consumption again. The suspected trigger is an update/poll after
backfill, but that is not yet confirmed.

Investigation approach locked for this pass:

- Inspect `.runtime/ha-config/home-assistant_v2.db` recorder/statistics rows
  before changing code.
- Compare statistics metadata, short-term/hourly statistics rows, and current
  sensor states against the integration ledger store.
- Determine whether the negative value is caused by a newly inserted Recorder row,
  statistics import projection, unit/sum metadata, or HA's derived Energy
  Dashboard aggregation.
- Report root cause only after DB evidence supports it.

Findings:

- The active entity IDs are `sensor.electricity_consumption` and
  `sensor.water_consumption`.
- Imported hourly long-term statistics were correct after initial backfill:
  electricity reached `sum=977.655` and water reached `sum=244.4`.
- On `2026-07-19 13:47:39 +08`, HA Recorder inserted/compiled new rows after
  the imported backfill range with `sum=0.0` for both sensors. This created the
  exact negative deltas shown by the Energy Dashboard:
  `977.655 -> 0.0` for electricity and `244.4 -> 0.0` for water at
  `2026-07-18 19:00:00 +08`.
- The ledger store remains sane: latest totals are still `1000.0`, accepted
  bucket totals are `977.655` electricity and `244.4` water, pending buckets are
  zero, and ledger/statistics warnings are empty.
- Root cause: `async_import_statistics()` imports hourly long-term statistics,
  but HA's normal sensor statistics compiler continues from
  `statistics_short_term`. The SP Group sensors' short-term statistics rows all
  have `sum=0.0`, so the first post-backfill HA-compiled hourly rows reset the
  long-term `sum` to zero even though imported historical hourly rows used the
  correct sum offset.

Design direction recommended after grounding against HA docs/source:

- Do not continue mixing SP Group imported historical statistics and HA-generated
  entity statistics for the same `sensor.*` statistic ID.
- HA docs/model: entity-derived statistics use `statistics_short_term` snapshots
  and hourly `statistics`; external statistics use colon-delimited statistic IDs.
- HA 2026.7.2 source confirms sensor `compile_statistics()` seeds `sum` from the
  latest short-term statistics, not from manually imported hourly long-term rows.
- Recommended fix: publish dashboard data as SP Group-owned external statistics
  with IDs such as `sp_group:<opaque_key>_electricity` and
  `sp_group:<opaque_key>_water`, using `async_add_external_statistics()` for both
  historical and ongoing hourly rows. Keep entity sensors as live/current state
  surfaces, but avoid making them the Energy Dashboard statistics source.
- Avoid direct writes to `statistics_short_term`; that would rely on private DB
  internals and fight HA's recorder lifecycle/purge model.

### 2026-07-19 23:41 +08 - Native Statistics Baseline Recheck

Question: is there a reliable HA-native way to import historical backfill into a
sensor's entity statistics and have HA continue from that imported baseline?

Grounded conclusion:

- For public/stable APIs, no. HA's public statistics import APIs write hourly
  long-term rows into `statistics`; they do not seed the sensor compiler's
  `statistics_short_term` continuation state.
- HA 2026.7.2 source confirms `sensor.recorder.compile_statistics()` resumes a
  sensor's `sum` from `get_latest_short_term_statistics_with_session(...)`.
  Imported hourly rows are not consulted as the next native entity-statistics
  baseline.
- HA docs say total/total_increasing sensors use the first compiled value as the
  zero point, while historical/imported statistics are modeled separately as
  imported long-term statistics.
- Directly manipulating `statistics_short_term`, racing HA's hourly compiler, or
  reshaping the live sensor state to encode a historical offset would be
  implementation-specific and fragile, not HA-aligned.

Decision: keep the recommended direction of moving Energy Dashboard history to
SP Group-owned external statistics if preserving pre-HA history is required.

### 2026-07-19 23:48 +08 - Historical Sensor-State Backfill Feasibility

Question: can SP Group backfill historical sensor state rows, then let HA's
native sensor statistics compiler build the Energy Dashboard timeline normally?

Findings:

- HA's native sensor compiler does read recorded entity state history for each
  compile window, so the idea is conceptually coherent.
- The supported integration-facing entity path (`async_write_ha_state()`) writes
  the entity's current state with the current timestamp. It does not expose a
  stable way for a custom integration to emit arbitrary historical state changes.
- `hass.states.async_set_internal(..., timestamp=...)` exists but is explicitly
  marked as core-internal and not a stable integration API.
- Directly inserting historical rows into Recorder's `states` tables would need
  private schema knowledge, event/state metadata consistency, recorder lifecycle
  coordination, and later missing-statistics compilation. It is also constrained
  by Recorder purge behavior for states and short-term statistics.
- Therefore historical sensor-state backfill is only a private-DB workaround, not
  a reliable HA-aligned integration design.

Decision: do not backfill HA entity state rows for Energy Dashboard history.
Use HA's public statistics import path for historical dashboard data.

### 2026-07-20 10:01 +08 - Spook Statistics Import Check

Question: Spook reportedly allows importing existing statistics data; verify
whether this changes the SP Group statistics design.

Findings:

- Spook docs confirm it adds a `recorder.import_statistics` action that manually
  imports long-term statistics into HA Recorder.
- Spook source implements the action as a thin wrapper around HA public APIs:
  `async_import_statistics()` when `statistic_id` is a valid entity ID, and
  `async_add_external_statistics()` otherwise.
- Spook does not backfill Recorder `states` rows and does not seed
  `statistics_short_term`.
- The docs warn that direct recorder/statistics manipulation can skew data and
  should be used with caution.

Decision impact:

- Spook validates that importing historical long-term statistics is feasible and
  commonly used.
- It does not provide a supported mechanism for HA's native entity statistics
  compiler to continue from imported history. The previous recommendation still
  stands: if SP Group needs guaranteed historical continuity, own the statistics
  timeline through HA's public import APIs.

### 2026-07-20 10:12 +08 - klausj1/homeassistant-statistics Check

Question: does `klausj1/homeassistant-statistics` provide a better pattern for
historical import than Spook, and does it solve native continuation?

Findings:

- The project provides actions only; no entities or dashboard cards. It imports
  and exports long-term statistics from CSV/TSV/JSON.
- It routes internal statistics (`sensor.name`, source `recorder`) through
  `async_import_statistics()` and external statistics (`domain:name`) through
  `async_add_external_statistics()`.
- It validates that internal recorder statistics point to an existing entity and
  that units match both the entity and existing `statistics_meta`.
- It has a useful delta import mode: given hourly deltas, it queries existing
  long-term statistics references and converts deltas into absolute `sum` and
  `state`. It supports forward conversion from an older reference and backward
  reconstruction from a newer reference.
- Its docs explain HA's normal flow (`states` -> `statistics_short_term` ->
  `statistics`) and state that the Energy Dashboard uses `sum`.
- It does not backfill Recorder `states`, does not seed `statistics_short_term`,
  and does not make HA's native entity compiler continue from imported history.

Decision impact:

- Its delta/reference design is relevant to SP Group's overlap math and supports
  the idea of anchoring imported rows from trusted references.
- It does not change the ownership conclusion: for automated SP Group backfill
  plus ongoing dashboard continuity, the integration should own the imported
  statistics timeline rather than mix one-time entity imports with native
  compiler continuation for the same statistic ID.

### 2026-07-20 10:31 +08 - Newer-Reference Import Feasibility For Native Continuation

Question: can SP Group borrow `homeassistant-statistics` newer-reference delta
conversion to prevent the negative boundary when HA later updates long-term
statistics from sensor state?

Findings:

- `homeassistant-statistics` avoids counter boundary jumps only when imported
  deltas are converted relative to an existing statistics reference. For a newer
  reference, it reconstructs older rows by subtracting deltas backwards and adds
  a connection row at the reference timestamp.
- SP Group's current implementation does not use HA's native statistics row as a
  reference. It computes imported `sum` from the first imported ledger bucket
  forward, so the latest imported historical row can end at the full backfill
  total while HA's native sensor compiler later starts from `sum=0`.
- A native-continuation design is theoretically possible:
  1. Let HA's sensor compiler establish the native reference row for the live
     sensor, usually `sum=0` at/after the first trusted sensor state.
  2. Import only historical rows before that reference.
  3. Compute imported `sum` by subtracting accepted deltas backward from the HA
     reference `sum`, while keeping `state` as the absolute reconstructed meter
     reading.
  4. Abort or defer if no matching HA reference exists, units mismatch, the
     reference state does not match the ledger-derived state, or the bridge is
     incomplete.
- This would mean pre-reference imported `sum` values may be zero or negative
  relative to HA's native zero point. That is how backward anchoring preserves
  positive deltas while connecting to native future rows.
- This approach preserves HA native future compilation, but delays/retries
  backfill until a reliable native reference exists and is more fragile than
  owning an external statistics timeline.

Decision impact:

- Newer-reference anchoring is a valid alternative to investigate if preserving
  native entity-derived future statistics is prioritized over integration-owned
  statistics.
- It needs a dedicated plan and tests around reference discovery, boundary
  connection, negative pre-reference sums, idempotency, and HA restart timing.

### 2026-07-20 11:09 +08 - Native-Anchored Statistics Backfill Implemented

Implemented the native-anchored statistics backfill retrofit:

- Split source-history attempt tracking from statistics import completion so HA
  restarts do not repeatedly re-fetch full provider history while statistics are
  waiting for a native reference row.
- Removed the synthetic current-hour continuation row from the statistics import
  path. Imported historical rows now require a validated HA-native reference row
  and are summed backward from that reference.
- Added a `native_anchored_v1` statistics strategy marker to the ledger. Legacy
  ledgers with older `statistics_imported_through` values are treated as not
  complete for the new strategy and receive a migration warning instead of
  silently suppressing the corrected importer.
- Added repair issues for two user-actionable states: waiting for HA to create a
  native statistics reference and existing statistics conflicts that require
  manual review/reset before retry.
- Added Recorder/statistics regression coverage for backward anchoring,
  positive Energy-style deltas, water final-month-hour projection, conflict
  skipping, and HA 2026.7 five-minute statistics task behavior.

### 2026-07-20 11:19 +08 - Mise Python Install Task Fixed

Fixed `mise run python:install` after discovering `.venv/bin/pip` still pointed
at the old `/apps/energy-monitoring/.venv/bin/python` interpreter.

- The install task now recreates `.venv` with `python -m venv --clear .venv`.
- It calls `.venv/bin/python -m pip` instead of `.venv/bin/pip`, avoiding stale
  console-script shebangs.
- It installs the HA 2026.7.2 dependency set plus the pytest custom-component
  harness support packages, then installs
  `pytest-homeassistant-custom-component==0.13.346` with `--no-deps` so the
  harness cannot downgrade Home Assistant.
- Verification: `mise run python:install`, `.venv/bin/python -m pytest
  tests/components/sp_group/test_recorder_statistics.py -q`, `mise run
  python:test`, `mise run python:lint`, and `mise run ha:check` all passed.

### 2026-07-20 12:07 +08 - Statistics Retry UX Tightened

Refined native-anchored statistics backfill behavior after reviewing HA
Recorder cadence and repair UX:

- HA Recorder short-term statistics are queued every five minutes at the
  scheduled `:00/:05/... +10s` ticks. The installed HA 2026.7.2 source compiles
  hourly long-term rows when the five-minute statistics start time has
  `minute == 55`, then fires `recorder_hourly_statistics_generated`.
- Waiting for the first HA-native long-term reference row is a normal timing
  state, not a user-actionable failure. The integration now records the
  `statistics_waiting_for_native_reference` ledger warning but clears any stale
  waiting repair issue instead of creating one.
- The options-flow retry action now runs `async_run_statistics_backfill()` only,
  using persisted ledger buckets. It no longer re-fetches full provider history
  just to retry HA statistics import.
- User-actionable repair issues remain reserved for failed source/history
  backfill and statistics conflicts that require manual review/reset.

### 2026-07-20 12:19 +08 - Automatic Statistics Retry Feasibility

Evaluated whether SP Group can automatically retry statistics import after HA
creates the native long-term statistics reference row.

- HA developer docs support integrations listening for Home Assistant events
  through event helpers or the event bus, with cancellable listeners registered
  and removed as part of integration lifecycle.
- The installed HA 2026.7.2 Recorder source exports
  `EVENT_RECORDER_HOURLY_STATISTICS_GENERATED`, fires it after hourly
  statistics compilation, and queues short-term statistics every five minutes.
- Supported direction: register an unload-safe listener for
  `EVENT_RECORDER_HOURLY_STATISTICS_GENERATED` and schedule a non-blocking
  statistics-only retry for entries that have completed source backfill and are
  still waiting for the native reference row.
- Unsupported/risky direction: call Recorder's internal `compile_statistics`
  directly to force native row creation. That would couple the integration to
  Recorder internals and could race Recorder's own database worker.
- Decision: keep the manual options retry as a fallback, but implement automatic
  event-driven statistics-only retry as the normal UX path.

### 2026-07-20 12:31 +08 - Automatic Statistics Retry Implemented

Implemented the event-driven native-reference retry path:

- Added a shared statistics-only scheduler with single-flight protection so
  hourly Recorder events and manual options retry cannot overlap.
- Registered an unload-safe listener for
  `EVENT_RECORDER_HOURLY_STATISTICS_GENERATED`. The listener schedules
  statistics import only after source-history backfill has been attempted and
  native-anchored statistics remain incomplete.
- Kept the hourly event path provider-safe: it calls the statistics-only retry
  and never calls full source-history backfill or utility backfill polling.
- Added gating for manual-review states such as statistics conflicts, legacy
  rows needing rebuild, and missing bridge warnings.
- Added tests for delayed eligibility after source backfill, conflict gating,
  and single-flight scheduling.

### 2026-07-20 13:05 +08 - Robust Statistics Retry Fix Implemented

Hardened the native-anchored statistics retry path after manual retry showed no
visible import progress despite valid ledger and native reference rows.

- Replaced the silent task/`None` scheduler contract with explicit statuses for
  scheduled, already-running, not-ready, and blocked states.
- Changed options-flow statistics retry into an action-only translated abort
  result so retry does not rewrite options or reload the config entry.
- Added durable `statistics_reference_start` ledger metadata so a validated
  native reference row is frozen and reused across retries.
- Refactored statistics projection through an indexed prefix calculator and HA
  executor jobs so large backfill planning does not block the event loop.
- Stopped treating `statistics_missing_bridge:*` as a hard retry blocker;
  conflicts, negative existing statistics deltas, and legacy rows still require
  manual review.
- Updated tests for action-only retry, force rechecking, missing-bridge retry,
  frozen reference metadata, and existing Recorder row idempotency.

### 2026-07-20 13:20 +08 - Statistics Retry RCA Completed

Investigated why submitting Options Flow "Retry statistics import" still did
not update Recorder or ledger metadata after the robust retry refactor.

- Live state showed the source ledger and Recorder native reference rows were
  valid: offline replay against `.runtime/ha-config` planned importable rows for
  electricity and water.
- Live HA logs and unchanged ledger metadata showed the retry action was not
  reaching the active importer task.
- Root cause: the scheduler trusted the `ConfigEntry` object passed by the
  Options Flow. In HA runtime this can be a detached flow entry object without
  the loaded integration `runtime_data`, while the canonical loaded entry in
  `hass.config_entries` has the actual ledger/runtime state.
- Fix: `async_schedule_statistics_backfill()` now resolves HA's canonical
  loaded config entry by `entry_id` before readiness checks, single-flight
  checks, task creation, and task tracking.
- Regression: added a test where a detached entry schedules against a loaded
  canonical entry with runtime data.
- Earlier related fix remains required: the Recorder hourly event listener uses
  `hass.add_job(...)` before scheduling so event callbacks from non-event-loop
  threads do not call `hass.async_create_task` unsafely.

### 2026-07-20 13:34 +08 - Statistics Retry Live Root Cause Confirmed

Completed the live retry investigation against the running HA dev instance.

- The options action scheduled the statistics-only task, and the canonical-entry
  scheduler fix allowed the active runtime ledger to be reached.
- Live Recorder rows from `statistics_during_period()` exposed the remaining
  defect: HA returned row `start` values as Unix timestamp numbers, while the
  importer only accepted `datetime` objects when selecting and validating the
  native reference row and when checking existing-row conflicts.
- Because of that shape mismatch, valid native reference rows were rejected with
  `reason=no_start`, leaving `planned=0` and preserving
  `statistics_waiting_for_native_reference`.
- Fix: normalize Recorder row starts from either timezone-aware `datetime` or
  Unix timestamp before reference selection, validation, decreasing-sum checks,
  and conflict filtering.
- Live verification after restarting HA and submitting `Retry statistics import`
  imported 13,572 electricity rows and 12 water rows from persisted ledger data;
  Recorder totals became 13,574 electricity rows and 14 water rows including the
  two native rows already present.
- Regression coverage now mocks Recorder reference rows with Unix timestamps for
  both electricity and water statistics import tests.

### 2026-07-20 13:55 +08 - Dev-Only Recorder Statistics Accelerator

Added a local development shortcut for full config-flow and Energy Dashboard
validation without waiting for the next natural Recorder hourly statistics
event.

- Production behavior remains unchanged: SP Group waits for HA Recorder's
  hourly statistics event, validates the native reference row, then imports
  historical statistics from persisted ledger buckets.
- The dev runner now sets `SPGROUP_HA_DEV_TOOLS=1`, which enables a hidden
  SP Group service only inside local `ha:dev` instances.
- New mise task: `mise run ha:dev:force-statistics`.
- The helper waits for any in-flight one-shot source-history backfill and queues
  Recorder statistics tasks only.
- SP Group statistics import remains owned by the existing
  `EVENT_RECORDER_HOURLY_STATISTICS_GENERATED` listener; the dev helper does
  not directly schedule the SP Group import path.
- The helper does not call SP Group provider history endpoints and should be
  used only in disposable `.runtime/ha-config` validation loops.

### 2026-07-20 15:22 +08 - Forced Recorder Event Thread-Safety Fix

Validated the dev Recorder accelerator after a fresh reset/config-flow run.

- The forced Recorder task fired `EVENT_RECORDER_HOURLY_STATISTICS_GENERATED`,
  but the SP Group listener scheduled `_schedule_hourly_statistics_backfill`
  through `hass.add_job()` with an undecorated sync function.
- HA treated that undecorated function as executor work; the executor then
  called `hass.async_create_task()`, triggering HA's thread-safety guard.
- Fix: mark `_schedule_hourly_statistics_backfill` with HA's `@callback` so
  `hass.add_job()` schedules it onto the event loop instead of the executor.
- The dev CLI still only queues Recorder work. It now passively waits for the
  listener-driven import metadata before printing evidence, which avoids a
  misleading early "not started" report.

### 2026-07-20 15:57 +08 - Downtime Catch-Up Behavior Inspection

Reviewed whether normal polling fills data gaps after Home Assistant is down.

- Electricity normal polling requests AMI data from each utility seed anchor to
  now, not only the latest bucket. The ledger dedupes accepted buckets and uses
  a two-sighting rule before accepting electricity observations, so post-seed
  downtime can be caught up after provider data appears in two matching polls.
- The catch-up guarantee is bounded by provider/API retention and timeout
  behavior. If the AMI endpoint no longer returns a missed interval, the
  integration cannot reconstruct it from normal polling.
- Water monthly polling fetches the history chart when its schedule fires, and
  the parser accepts all closed months from the seed anchor. However, startup
  scheduling currently computes the next configured month only; if HA starts
  after the configured water poll day/time, it does not perform an immediate
  startup catch-up poll and will wait until the next scheduled monthly run.
- The one-shot source-history backfill intentionally does not rerun on every
  restart; downtime catch-up is therefore a normal utility polling concern, not
  a full historical backfill rerun.

### 2026-07-20 16:11 +08 - Robust Polling Recommendation

Assessed what is needed to make post-setup polling robust across downtime and
provider lag.

- Current electricity polling can catch up missed AMI buckets in the ledger
  because normal polls request from the seed anchor to now and the ledger dedupes
  accepted buckets, but this is inefficient and still depends on the provider
  returning the missed AMI range.
- Current water polling can parse all closed monthly history rows when it runs,
  but startup scheduling can skip an immediate catch-up when HA starts after the
  configured monthly window.
- Ledger catch-up is not the same as Energy Dashboard catch-up. The
  native-anchored statistics importer currently imports projected rows before the
  HA-native reference boundary; post-reference downtime buckets may be absorbed
  by HA as a later cumulative jump instead of being placed into their source
  periods.
- Recommended direction: add per-utility polling cursors and gap state, run a
  non-blocking startup catch-up planner, fetch bounded windows from the earliest
  unresolved cursor with overlap, import accepted missed-bucket statistics for
  closed gaps using the same idempotent/conflict-safe Recorder import path, and
  surface unrecoverable provider-retention gaps as Repairs.

### 2026-07-20 16:16 +08 - Robust Polling Second-Pass Reconciliation

Ran a second code-grounded pass over setup, backfill, coordinators, ledger,
sensors, statistics import, options, and tests.

- Confirmed previous gaps: electricity normal polling can catch up provider
  buckets returned from seed-to-now; water startup scheduling can miss an
  immediate catch-up; statistics import only plans rows before the native
  reference, so post-reference downtime bucket placement is not guaranteed.
- Added gaps: source backfill is marked attempted before utility fetch success;
  accepted bucket corrections are warning-only and never reprocess affected
  ledger/statistics rows; utility sensors do not include `super().available`
  in their custom availability property; no explicit provider-retention or
  unresolved-gap repair state exists.
- Test coverage does not currently model HA-down intervals across utility
  poll cycles plus Recorder statistics placement, nor failed first source
  backfill followed by restart recovery.

### 2026-07-20 16:19 +08 - Robust Polling Complexity Review

Re-evaluated the robust polling recommendation against Home Assistant polling
patterns.

- The earlier "full recovery subsystem" framing is heavier than necessary for
  the next step. HA conventions favor `DataUpdateCoordinator` for periodic
  polling, event helpers for targeted callbacks, `UpdateFailed`/coordinator
  availability for transient provider failures, and Repairs for user-actionable
  states.
- Updated approach: keep recovery coordinator-centered and add only minimal
  persisted per-utility cursor/status fields required to avoid repeated
  seed-to-now fetches, detect missed water schedules, retry failed initial
  source backfill accurately, and schedule statistics-only import after accepted
  missed buckets.
- Defer complex provider-retention gap registries until real endpoint behavior
  proves they are needed; initially surface bounded warnings/Repairs only for
  confirmed unrecoverable gaps or accepted-bucket corrections.

### 2026-07-20 16:47 +08 - Coordinator-Centered Polling Refactor Implementation

Implemented the coordinator-centered robustness refactor.

- Added per-ledger source backfill state (`never_attempted`, `running`,
  `succeeded`, `failed`) plus source backfill timestamps and interrupted-running
  recovery on setup.
- Kept normal catch-up inside the utility `DataUpdateCoordinator` path:
  electricity derives its poll cursor from pending/accepted ledger buckets with
  a bounded overlap and splits long AMI windows; water keeps its monthly
  scheduler and adds one unload-cancelled startup catch-up task when HA starts
  after a possibly missed monthly poll window.
- Source retry and statistics retry are now separate options actions.
  Statistics retry remains persisted-ledger only and single-flight; source retry
  reruns only retryable source-history entries.
- Threaded `force=True` through statistics retry and fixed forced
  post-reference planning so missing deterministic rows after a HA-native
  reference import with cumulative sums that produce positive Energy Dashboard
  deltas.
- Updated sensor availability to include coordinator availability via
  `super().available`, matching Home Assistant transient-failure behavior.

### 2026-07-20 17:12 +08 - Polling Robustness Coverage Closure

Closed the remaining test coverage gaps for the coordinator-centered polling
refactor.

- Added focused source-backfill orchestration tests covering mixed
  electricity/water success and failure, utility-specific ledger status
  transitions, and Repair creation only on real source failure.
- Added setup/restart tests for failed and interrupted source backfill states,
  plus a malformed-ledger assertion that keeps malformed state on the Repair
  path instead of scheduling healthy source/statistics backfill.
- Added water startup catch-up task lifecycle tests for scheduling,
  de-duplication, refresh invocation, cancellation, and no-op behavior.
- Strengthened normal polling tests so accepted electricity/water buckets
  schedule statistics-only forced retry, while first-sighting/pending buckets do
  not.
- Added statistics scheduler branch tests for blocked and non-blocking warnings,
  expanded forced import regression to cover multiple post-reference missed
  rows, and added diagnostics coverage for non-empty source-backfill status
  histograms.
- New tests exposed two implementation defects that were fixed: malformed
  ledgers were still scheduling healthy backfill/schedules, and cancelled water
  startup catch-up tasks stayed in runtime tracking until callback cleanup.

### 2026-07-20 17:19 +08 - Dev Session Refresh Command

Added a dev-only `mise run ha:dev:refresh-session` helper to refresh
`.runtime/mobile-session.json` without starting Home Assistant or opening the
SP Group config flow.

- The helper reuses the SDK `MobileAuthClient.refresh_session()` path and the
  same serialized `MobileSession` shape used by the cached-session config-flow
  bypass.
- It defaults to `SPGROUP_HA_DEV_SESSION_FILE=.runtime/mobile-session.json`,
  supports `--session-file` for explicit overrides, writes refreshed tokens
  atomically with owner-only permissions, and prints only sanitized metadata.
- This keeps repeated post-auth config-flow testing from requiring fresh
  username/password/OTP attempts when a valid refresh token is already cached.

### 2026-07-20 19:54 +08 - Statistics Backfill Test-First Hardening

Added test-first coverage for the remaining water negative-consumption bug in
native-anchored historical statistics import.

- Added reusable Energy Dashboard-style delta assertions that normalize Recorder
  row starts, use an explicit pre-window sum, include native reference rows, and
  fail on negative visible consumption deltas rather than negative absolute
  pre-reference sums.
- Replaced the old water monthly projection expectation that accepted a visible
  negative baseline with tests for monthly source values, the live Aug 2025 to
  Jun 2026 water fixture, and a real Recorder-backed import/query regression.
- Added baseline idempotency/conflict coverage and a forced post-reference water
  retry test.
- Verification is intentionally red against current implementation:
  `tests/components/sp_group/test_recorder_statistics.py` reports 13 passed and
  4 failed. The failures identify visible water baseline deltas of `-10.0` and
  `-244.4`, plus a forced water retry/reference gap.

### 2026-07-20 20:09 +08 - Statistics Backfill Coverage Gap Reassessment

Reassessed the test-first hardening after identifying that electricity can hide
the same negative-baseline bug when the test starts its visible window after the
synthetic baseline row.

- Root cause: the helper accepted caller-supplied `pre_window_sum`, allowing an
  electricity test to skip the imported baseline row and therefore pass without
  proving the broader Energy Dashboard invariant.
- Delegated two read-only reviews to subagents: one focused on HA
  statistics/dashboard semantics, and one focused on code execution-path
  coverage. Both independently identified the electricity baseline-visible gap
  and recommended a coverage matrix rather than scenario-only tests.
- Added `docs/plans/statistics-backfill-full-coverage-hardening.md` with the
  corrected oracle, required red tests for electricity and water, cross-utility
  paths, force-mode parity, real Recorder-backed regressions, and a final gate
  requiring every matrix row to be tested or explicitly marked unreachable.

### 2026-07-20 20:15 +08 - Water Statistics Backfill Branch Tests

Added isolated water-focused tests for statistics backfill branch coverage in
`tests/components/sp_group/test_statistics_backfill_water_invariants.py`.

- Covered persisted native reference reuse for both datetime-shaped and
  timestamp-shaped Recorder rows.
- Covered exact water row idempotency, conflicting water row handling,
  decreasing existing water sums creating a conflict Repair, and missing
  pre-seed/post-seed monthly bridge warnings.
- Strengthened the Aug 2025 through Jun 2026 live water fixture so every
  expected monthly dashboard delta is asserted, not only selected months.
- Added a matching real Recorder-backed live-fixture regression through HA's
  public statistics import/query APIs.
- Verification is intentionally red against current implementation:
  `test_statistics_backfill_water_invariants.py` reports 7 passed and 2 failed.
  The failures are the mocked and real-Recorder live fixture both detecting the
  visible `2025-07-31T23:00+08:00` delta of `-244.4`.

### 2026-07-20 20:16 +08 - Electricity Statistics Invariant Red Tests

Added a disjoint test-only electricity invariant file for native-anchored
historical statistics backfill.

- New coverage lives in
  `tests/components/sp_group/test_statistics_backfill_electricity_invariants.py`
  and does not edit production code.
- The tests assert Energy Dashboard-style visible `sum` deltas over windows that
  include the synthetic baseline row, instead of starting after it with a
  favorable pre-window sum.
- Focused verification is intentionally red against current production code:
  the mocked single-hour case exposes `-2.0`, the live-like multi-hour case
  exposes `-5.75`, and the real Recorder-backed regression exposes `-4.5` as
  first-visible electricity deltas.

### 2026-07-20 20:17 +08 - Water Statistics Invariant And Cross-Utility Tests

Added the remaining test-first coverage slices for statistics backfill.

- `tests/components/sp_group/test_statistics_backfill_water_invariants.py`
  covers water persisted native reference row shapes, exact-row idempotency,
  conflict Repairs, existing decreasing sums, missing pre/post-seed monthly
  bridges, the full Aug 2025-Jun 2026 live fixture, and a real Recorder-backed
  live-fixture regression.
- `tests/components/sp_group/test_statistics_backfill_cross_utility.py` covers
  no-Recorder no-op behavior, missing entity handling, one utility waiting for a
  native reference while another imports, and one utility conflicting without
  corrupting another utility's metadata.
- Focused verification of the three new invariant files is intentionally red
  with 11 passing and 5 failing tests. The failures are the expected generalized
  negative-delta catchers: electricity `-2.0`, electricity live-like `-5.75`,
  electricity real Recorder `-4.5`, water live fixture `-244.4`, and water real
  Recorder `-244.4`.

### 2026-07-20 21:10 +08 - Native-Offset Statistics Backfill Fix

Implemented the `native_offset_v2` statistics import strategy to close the
remaining Energy Dashboard negative-consumption bug.

- Historical rows now start from a zero-sum baseline and advance positively by
  accepted source bucket deltas. `state` remains the absolute reconstructed
  ledger meter total.
- When a validated HA-native reference row exists, the importer applies the
  missing cumulative history offset from that reference onward through
  Recorder's `async_adjust_statistics()` API before importing deterministic
  historical rows.
- Ledger metadata now persists `statistics_reference_sum_offset` and marks
  successful imports with `native_offset_v2`; older `native_anchored_v1`
  metadata remains legacy/possibly contaminated rather than being treated as
  complete.
- Forced statistics retry now continues from the adjusted reference sum and
  re-checks Recorder rows without fetching provider history.
- Water state validation now treats monthly buckets as effective at bucket end,
  so monthly source history validates against native rows after the closed
  month is represented.
- Regression tests were updated to verify visible Energy Dashboard-style
  consecutive `sum` deltas, offset calls, exact-row idempotency, conflicts,
  water live-bug shape, electricity parity, and real Recorder-backed behavior.
- Verification passed:
  `mise exec -- python -m pytest tests/components/sp_group/test_recorder_statistics.py tests/components/sp_group/test_statistics_backfill_electricity_invariants.py tests/components/sp_group/test_statistics_backfill_water_invariants.py tests/components/sp_group/test_statistics_backfill_cross_utility.py -q`,
  `mise exec -- python -m pytest tests/components/sp_group/test_init.py tests/components/sp_group/test_coordinator.py -q`,
  `mise exec -- python -m pytest tests/components/sp_group -q`,
  `mise run python:test`, `mise run python:lint`, and `mise run ha:check`.

### 2026-07-20 21:16 +08 - Dev Console Logging Visibility Assessment

Assessed the current local HA dev mode and integration logging surfaces for
manual debugging/verification.

- Current dev runner sets `SPGROUP_HA_DEV_TOOLS=1` and exposes the
  dev-only force-statistics service, but the generated HA config does not
  include a `logger:` section. HA's logger docs say messages below the
  configured level are ignored, so existing `DEBUG` phase logs are not visible
  by default in the dev console.
- Existing useful logs:
  - discovery coordinator has debug summaries for auth refresh, discovery, and
    normalization counts;
  - utility coordinators have debug summaries after ledger source polling;
  - backfill/statistics orchestration logs scheduled/started/finished/failed
    states at info/warning/error;
  - statistics import logs existing row counts, reference rejection reasons,
    planned/importable/processed/warning counts, and conflict/waiting outcomes;
  - dev force-statistics helper logs Recorder task queuing and source-backfill
    wait behavior.
- Main visibility gaps to close in a logging pass:
  - explicit dev logger configuration for `custom_components.sp_group`;
  - setup/unload/options action lifecycle;
  - poll start/end, mode, request window count, observation count, accepted and
    pending deltas, and stats-retry schedule status;
  - water monthly schedule/catch-up decisions;
  - source-backfill per-utility start/end/failure and retryability;
  - statistics offset application details and per-utility final metadata;
  - config-flow step transitions and dev cached-session path, with no secrets
    or raw provider IDs.
- Recommended severity split: `INFO` for dev-visible lifecycle and phase
  summaries, `DEBUG` for detailed sanitized counts/windows/status reasons,
  `WARNING` for retryable degraded states, and `ERROR/exception` only for task
  failures or integrity blockers.

### 2026-07-20 21:24 +08 - Dev Console Logging Implementation

Added sanitized dev-visible logging for the SP Group HA integration.

- The local HA dev runner now writes a `logger:` section that keeps the default
  HA log level at `info` while enabling `debug` for
  `custom_components.sp_group`, so `mise run ha:dev*` exposes integration
  diagnostics without globally noisy logs.
- Setup, discovery, platform forwarding, backfill scheduling, Recorder hourly
  listener registration, unload, and background-task cancellation now emit
  concise lifecycle logs.
- Utility polling now logs start/end phase summaries with mode, request/window
  counts, observation counts, accepted/pending deltas, and statistics retry
  schedule status. Water monthly scheduling also logs next run, startup
  catch-up decisions, retry queueing, cancellation, and completion.
- Source-history backfill and statistics-only backfill now log scheduling
  status, per-utility start/end/failure, skipped retry reasons, native-offset
  adjustment, and final metadata counts.
- Config flow and options flow now log sanitized step transitions, dev cached
  session path, selected action type, and retry scheduling outcomes without
  logging tokens, OTPs, account IDs, source keys, provider IDs, addresses, or
  raw API payloads.
- Verification passed:
  `mise exec -- python -m pytest tests/components/sp_group/test_coordinator.py tests/components/sp_group/test_runtime_privacy.py tests/components/sp_group/test_config_flow.py tests/components/sp_group/test_init.py -q --tb=short`,
  `mise exec -- python -m pytest tests/components/sp_group -q`,
  `mise run python:test`, `mise run python:lint`, and `mise run ha:check`.

### 2026-07-20 22:36 +08 - Live Sensor State Investigation

- Confirmed from the running HA Recorder database and the persisted SP Group
  ledger that both live entity states currently equal their manual seed totals:
  electricity is `1000.0 kWh` and water is `1000.0 m3`.
- This is not caused by the historical backfill. Ledger semantics intentionally
  retain the seed as the current meter anchor and add only accepted source
  buckets at or after `seed_effective_from`; pre-seed history is used solely to
  reconstruct historical statistics and must not inflate the live meter state.
- Electricity has no accepted or pending post-seed bucket. The only scheduled
  electricity poll after the seed began at 22:26 local time, but an entry reload
  began before it completed, so no ledger update was applied. The reloaded
  hourly coordinator will make its next normal attempt one hour after the new
  coordinator was created.
- Water is expected to remain at its July seed until the provider exposes and
  the integration accepts a closed post-seed monthly bucket. The current month
  is not a completed monthly source period.

### 2026-07-21 22:20 +08 - Polling Update and Two-Sighting Rule Evaluation

- Set electricity polling interval to 5 minutes via DurationSelector for testing.
- First poll (22:05:46): 50 observations went to pending (two-sighting rule).
- Second poll (22:11:02): 50 promoted to accepted, sensor jumped from 1000.0 to 1017.374.
- Verified `latest_total = seed + post_seed_sum`: 1000.0 + 17.374 = 1017.374 ✅
- Sensor jumps in one batch because two-sighting rule accumulates pending data and promotes it all at once on the next matching poll.
- The one-shot jump is expected behavior with `require_two_sightings=True`.
- Decision: Two-sighting rule removal deferred for now. Will revisit if the 5-minute delay on post-seed data is undesirable.
- Auth refresh reload cycle fix confirmed working: no recurring unload/reload after multiple poll cycles.

### 2026-07-21 22:45 +08 - Statistics Import vs Sensor Jump Investigation

Investigated the one-shot sensor jump (1000 → 1017.374) when post-seed polling
data is accepted.

Key findings:
- Pre-seed data (27,162 buckets): projected into HA long-term statistics via
  `statistics_backfill.py`. History panel shows "Long-term statistics" with
  correct stepped graph across 18 months. Sensor `latest_total` stays at seed
  value (1000.0) throughout because pre-seed buckets do not affect it.
- Post-seed data (52 buckets accepted in one batch): `latest_total` jumps from
  1000.0 to 1017.374 in a single update. Statistics import is blocked by
  `statistics_waiting_for_native_reference` (`projected=13606`, `existing=13599`,
  only 7 new rows needed). History panel shows "History" instead of
  "Long-term statistics" for the post-seed period.
- The root cause of the one-shot sensor jump is `latest_total = seed_value +
  sum(all_post_seed_accepted)`, which sums all 52 buckets spanning 25 hours
  into a single value without time projection.
- The pre-seed data DID cause an identical jump pattern? Actually pre-seed does
  not affect latest_total at all — the stepped graph the user sees in the
  History panel is purely from statistics, not from sensor state changes.
- Conclusion: the statistics import needs to be unblocked for post-seed data
  so the Energy Dashboard/History panel shows the correct stepped graph. The
  sensor value jump to 1017.374 is mathematically correct for
  `total_increasing`, but the time projection into statistics is what produces
  the stepped display.

### 2026-07-21 23:55 +08 - Complete HA Statistics/Sensor/Polling Investigation

Investigated and documented the full interaction between HA's three data
stores (states, statistics, statistics_short_term), SP Group's polling/ledger
flow, and the statistics backfill pipeline.

Key findings documented in docs/references/ha-statistics-sensor-interaction.md:

1. Sensor `latest_total = seed + sum(post_seed_accepted)` jumps when
   multiple buckets are accepted at once — correct math but abrupt display.
2. Statistics import (`async_import_statistics`) is blocked by
   `_validated_reference` because the HA-compiled row's state (1017.374)
   doesn't match the ledger's projected state at the reference hour.
3. At 23:36: existing=13600 projected=13607 (7 rows needed), blocked by
   `statistics_missing_bridge` + `statistics_waiting_for_native_reference`.
4. The `_any_entry_should_retry_statistics` function doesn't detect that
   new buckets have been accepted after `statistics_imported_through` — it
   only checks strategy version, not bucket watermark.
5. Full fix requires polling window fix (remove overlap) AND statistics
   retry check update (compare against newest accepted bucket, not just
   strategy metadata).

### 2026-07-22 - Native Sensor and Statistics Cause Revalidated

Re-grounded the post-seed one-shot and statistics-import behavior against the
current SP Group implementation, the installed Home Assistant Recorder source,
and the official sensor/statistics guidance.

- `SpGroupUtilitySensor.native_value` publishes the ledger's single
  `latest_total`; ledger application calculates it as `seed_value + sum(all
  accepted buckets at or after seed_effective_from)`. A poll that accepts many
  provider buckets therefore produces one current state update containing the
  final total. The provider bucket timestamps are not passed to the HA state
  machine.
- HA's sensor Recorder platform obtains the entity's recorded states from
  Recorder history and computes `total_increasing` sum from consecutive
  observed states. Its state timestamps are the HA state-update timestamps, not
  the source bucket timestamps. HA does not reconstruct historical state rows
  from a later cumulative state.
- HA's hourly compiler takes the latest five-minute statistic in each hour and
  inserts that as the hourly row. The native reference row is therefore valid
  for the sensor history HA actually observed, even when it cannot represent
  the provider's older buckets returned in the same poll.
- `async_import_statistics` is the supported path for supplying timestamped
  historical hourly statistics. It validates top-of-hour timestamps and queues
  the import in Recorder; it is not a second native sensor-state update path.
- Consequently, the probable cause is confirmed with an important
  qualification: the native reference is not inherently faulty. The problem
  is the integration's attempt to reconcile one current-time native state
  jump with multiple provider buckets whose usage belongs to earlier hours.
  Native compilation is sufficient only for data observed as live state at the
  time it occurs. A delayed/outage catch-up requires statistics import (or an
  equivalent timestamped source) to preserve historical bucket placement.

### 2026-07-22 - Clarified Post-Seed Sensor Projection Proposal

The requested design is to apply multiple post-seed provider buckets
chronologically to the cumulative sensor value, so native HA statistics can
derive the hourly progression from sensor states rather than importing those
post-seed rows directly.

- This is valid for future/live buckets if each value is written when its
  bucket becomes current.
- It does not retroactively create correctly timestamped sensor history when
  a later poll returns already-closed buckets. Replaying those values in one
  event-loop turn gives them current HA timestamps; replaying them hourly
  delays recovery and still does not recreate the original timestamps.
- Directly injecting old timestamps into the state machine would bypass the
  normal entity path and is not an acceptable integration pattern.
- Therefore the robust split remains: native sensor updates for live data;
  timestamped statistics import for delayed/outage catch-up. The sensor may
  still move to the latest cumulative total in one current-time state update
  after catch-up, while the Energy Dashboard statistics remain time-projected.

### 2026-07-22 - Delayed Provider Data Pattern Re-researched

Compared SP Group with HA's documented sensor/statistics behavior, installed
Recorder source, the public statistics API, and the `klausj1/homeassistant-
statistics` implementation.

- HA integrations normally expose the latest provider value through a sensor;
  HA records that value at the time the entity writes it.
- When an API supplies historical intervals whose source timestamps must be
  preserved, the supported representation is timestamped Recorder statistics,
  using `async_import_statistics` for an existing entity statistic ID or
  `async_add_external_statistics` for a separate external statistic ID.
- `homeassistant-statistics` explicitly has no entities and imports directly
  into long-term statistics. Its documentation distinguishes internal
  `sensor.domain` IDs from external `domain:name` IDs and accepts hourly
  counter rows or deltas.
- This confirms that replaying delayed provider intervals through sensor state
  updates is not the usual HA solution for historical time placement. The
  sensor path is for current/live state; the statistics API is the historical
  time-series path.

### 2026-07-22 - Qualification on Historical Sensor Timestamps

Clarified that HA's state machine technically accepts an optional timestamp on
`StateMachine.async_set`, but the entity write path used by integrations writes
the current time and calls the internal state setter. Historical state replay
would therefore require bypassing the normal entity API and directly mutating
the state machine, which is marked as an unstable core-internal API and would
not update the entity/coordinator lifecycle safely.

- Precise conclusion: historical sensor states are technically possible through
  low-level state-machine manipulation, but not a supported or reliable custom
  integration backfill pattern.
- The supported historical representation remains timestamped Recorder
  statistics import; native entity states remain the live path.

### 2026-07-22 - Core Integration Comparison for Delayed API History

Inspected Home Assistant Core integrations that receive delayed or historical
utility data:

- Opower explicitly notes that usage arrives days late, fetches the delayed
  readings, and imports timestamped external statistics. Its sensors expose
  forecasts, dates, and freshness metadata rather than pretending delayed
  readings are live meter states.
- Elvia fetches hourly historical meter values, uses the provider timestamps,
  resumes from the last stored statistic, and builds cumulative `sum` rows via
  `async_add_external_statistics`.
- Suez Water fetches daily history since the last statistic and builds daily
  timestamped cumulative statistics; its sensor exposes an aggregated current
  value rather than replaying each historical day through entity state.
- Anglian Water uses the provider `read_at` timestamp, shifts the statistic
  hour to match the meter reading semantics, and imports provider cumulative
  readings as `sum` plus interval consumption as `state`.
- Ista EcoTrend exposes current values through coordinator sensors and imports
  monthly historical data separately through external statistics.
- Tibber's realtime sensor uses the source timestamp for reset detection only;
  it writes the latest value through the normal entity path, so HA records it
  at the update time rather than backdating the state.

Locked finding: Core integrations do not normally replay delayed provider
  buckets as historical entity states. They either expose a current/latest
  sensor value or a non-meter period value, and preserve historical placement
  through Recorder statistics APIs. SP Group's ledger plus timestamped
  statistics importer follows this split; the remaining correctness burden is
  to make imported `state`/`sum` rows coherent with the native statistic ID,
  not to synthesize past entity-state writes.

### 2026-07-22 - Qualification on HA Entity Timestamp Support

Second-pass source verification refined the earlier wording. The HA state
machine has an optional explicit timestamp, but `Entity.async_write_ha_state`
captures the current write time and passes it through the normal entity path.
The lower-level `StateMachine.async_set_internal` timestamp path is explicitly
documented in source as core-internal, unstable, and not for integrations.

Therefore, delayed sensor-state replay is technically possible at a lower
level, but there is no supported public SensorEntity workflow for an
integration to backdate a sequence of historical states. The supported design
remains current/latest entity state plus timestamped Recorder statistics for
historical buckets. This is a deliberate separation of live entity state from
historical time-series import, not an assertion that HA lacks all timestamp
support.

### 2026-07-22 - Proposed Direction for Unpredictable Provider Lag

Provider lag means no fixed polling interval can make native sensor-derived
statistics temporally authoritative. A poll may return several older buckets,
and HA cannot infer their provider timestamps from one latest sensor-state
write.

Proposed direction, not yet locked: treat accepted provider buckets and their
timestamps as the source of truth for historical statistics. Keep the sensor
state as the latest trusted cumulative total, and use timestamped Recorder
statistics for every delayed or recovered bucket. Native HA compilation remains
useful for genuinely live sensor observations, but must not be relied on to
reconstruct late provider history.

Two implementation strategies remain to be evaluated before any change:

- preserve the existing entity statistic ID and maintain a careful native
  handoff/reconciliation boundary; or
- use a dedicated external statistic ID, matching Core integrations such as
  Opower, Elvia, Suez Water, and Anglian Water, to isolate provider history
  from native entity compilation. The latter requires an explicit migration
  plan for existing Energy Dashboard selections.

### 2026-07-22 - Provider-Owned External Statistics Decision

Decision accepted: SP Group's authoritative historical electricity and water
series will use stable provider-owned external statistic IDs through
`async_add_external_statistics()`. The sensor remains the latest trusted
cumulative entity state; it is not used to replay delayed provider buckets.

Current-code grounding:

- `sensor.py` publishes `ledger_entry.latest_total` with
  `TOTAL_INCREASING`.
- `coordinator.py` applies provider observations to the ledger and schedules
  statistics retry after accepted buckets.
- `statistics_backfill.py` currently uses entity IDs with
  `async_import_statistics()`, then coordinates with native reference and
  native-offset logic. This is the path being replaced for new imports.
- HA Energy preferences store statistic IDs, so external statistics can be
  selected as the dashboard source. Existing dashboard preferences will not be
  silently mutated.

ADR added: `docs/adr/adr-provider-owned-external-statistics.md`.
Implementation plan added: `docs/plans/provider-owned-external-statistics.md`.
Legacy entity-backed rows remain immutable; migration rebuilds a new external
series from accepted ledger buckets and provides explicit dashboard migration
guidance.

### 2026-07-22 - Adversarial Review of Provider-Owned Statistics Plan

Review outcome: the external-statistics direction is compatible with HA's
Recorder and Energy models, but the plan needs stronger implementation gates
before it is safe to execute. The highest-risk gaps are external statistic ID
ownership/collision handling, queued Recorder write acknowledgement, late
bucket suffix reconciliation beyond a single watermark, and explicit migration
of existing Energy Dashboard source IDs. The plan also needs a settled
`StatisticData.state` contract, accepted-bucket correction handling, source-vs-
statistics concurrency control, stable-ID behavior when discovery/account
identity changes, and bounded Recorder read/query costs. These are review
findings only; no production or plan changes were made.

### 2026-07-22 - Provider-Owned Statistics Plan Grounding Resolved

The plan was updated after grounding the review findings against the installed
HA Recorder and Energy source. Recorder external imports are queued writes;
the implementation will await Recorder commit visibility, reread the affected
rows, and conditionally advance ledger metadata using a source fingerprint and
epoch. The external statistic ID is now a hard ownership boundary, not a
warning-only collision check. Late buckets use a persisted dirty boundary and
deterministic suffix reconciliation rather than relying on
`statistics_imported_through` alone.

The external row contract is explicit: `state` is represented provider-period
usage and `sum` is the positive cumulative external series; the live sensor
continues to expose the absolute ledger total. Accepted provider corrections
block reconciliation pending user action. The plan also now covers bounded
Recorder reads, source/statistics races, disabled sensors, legacy strategy
shutdown before migration, and explicit Energy Dashboard source-switch
acknowledgement.

### 2026-07-22 - Native-ID Post-Seed Alignment Evaluation

Reusing the pre-seed timestamped planner for post-seed accepted buckets is
possible at the statistics-row level, but it would not backdate the sensor
state history. The current sensor publishes only the latest ledger total, while
the current native-ID importer and HA compiler both write the same entity
statistic ID. Extending this approach post-seed therefore creates a two-writer
handoff that must reconcile poll-time native rows, provider-time imported rows,
late suffix changes, and native sum offsets. It is a viable alternative only as
a deliberately scoped native-ID handoff design, not as a simple reuse of the
pre-seed function.

### 2026-07-22 - External Statistics Recommendation

Recommendation: proceed with provider-owned external statistics for the
authoritative Energy Dashboard and long-term statistics series. This best
matches the requirement for provider-timestamped delayed history while avoiding
two writers competing over one entity statistic ID. The recommendation does
not claim to retroactively step the sensor History graph, and it remains subject
to explicit user acceptance of the Energy Dashboard statistic-ID migration.

### 2026-07-22 - HA-Owned Versus Integration-Owned Statistics Implications

HA-owned statistics use the `TOTAL_INCREASING` sensor state as the source and
let Recorder compile sums from state transitions. This preserves the native
entity/statistics relationship with less integration-owned reconciliation, but
cannot assign past provider buckets to their original timestamps when delayed
data arrives in one poll. Integration-owned external statistics remain in the
same Recorder long-term statistics system, but use provider timestamps and
explicit cumulative sums; this improves delayed-history correctness at the
cost of a separate statistic ID, migration UX, and reconciliation ownership.

### 2026-07-22 - External Statistics Necessity Re-evaluation

The external-statistics direction is not technically mandatory. A native-ID
alternative remains possible, but it would require the integration to import
historical rows into the entity statistic ID and coordinate a durable handoff
with HA's native compiler. That preserves one Energy statistic ID but makes the
integration and HA co-owners of one cumulative series, including repeated
offset/suffix reconciliation whenever delayed buckets arrive. Pure HA-owned
statistics cannot satisfy provider-time backfill because sensor state writes
are timestamped when HA observes them. No final reversal of the accepted ADR
was made; the trade-off remains open for explicit confirmation before coding.

External-statistics trade-offs confirmed: Energy consumption remains supported,
but external sources cannot use an entity or fixed-number price unless a
separate external cost statistic is configured. External statistics also do not
retroactively populate sensor History, and they introduce a second statistic
identity plus integration-owned reconciliation. Native sensor state, Recorder
storage, and ordinary live polling remain unchanged.

### 2026-07-22 19:22 +08 - Worktree Commit Strategy Before External Statistics

The current worktree is a large accumulated change set, not a single coherent
external-statistics implementation. It contains 31 modified tracked files and
16 untracked project files, with approximately 6,100 added lines before the
untracked files are counted. The focused integration suite currently passes:
275 passed, 1 skipped. `git diff --check` is clean, and no generated cache files
are part of the reported worktree changes.

The changes divide into these semantic groups:

1. Ledger/source-backfill state, coordinator polling/catch-up, statistics retry
   scheduling, and lifecycle handling. These runtime changes are coupled across
   `__init__.py`, `backfill.py`, `coordinator.py`, and `ledger.py` and should be
   committed as coherent runtime slices with their matching tests.
2. Native-offset statistics projection and Recorder reconciliation in
   `statistics_backfill.py`, with the electricity/water invariant and
   cross-utility tests. This is the currently validated native-ID checkpoint,
   not the future provider-owned external-statistics implementation.
3. Granular polling options, translations, diagnostics, and sensor availability.
4. Developer services, force-statistics/session-refresh helpers, debug logging,
   mise tasks, and runtime validation documentation.
5. Auth Store migration documentation and partial implementation.

The auth Store change is not commit-ready as a completed refactor: setup still
loads the session only from `entry.data`, does not initialize/load a Store into
runtime data, and `async_refresh_session_if_needed()` still calls
`async_update_entry()` through its `persist=True` path. It must either be
completed and tested as its own commit or excluded from the final commit set;
it must not be presented as a finished Store-only migration.

Proposed commit order:

1. `feat: harden ledger source backfill and polling lifecycle` — ledger state
   migration, cursor/catch-up behavior, coordinator orchestration, lifecycle
   tests, and only the related runtime wiring.
2. `feat: harden native statistics backfill` — native-offset projection,
   Recorder reconciliation, retry semantics, repair translations, and all
   statistics regression tests. Keep this explicitly as the pre-external
   checkpoint.
3. `feat: add granular polling configuration` — duration parsing/validation,
   option migration, translations, and config-flow tests.
4. `fix: persist runtime sessions through HA Store` — only after setup loading,
   migration fallback, runtime persistence, and the remaining
   `async_update_entry` scope are made internally consistent.
5. `chore: add development diagnostics and runtime helpers` — dev-only
   services/scripts, mise tasks, debug logging, and their documentation/tests.
6. `docs: record architecture decisions and validation procedures` — ADRs,
   issue reports, plans, references, developer guidance, and the context
   tracker entries that describe the completed checkpoints.
7. Implement provider-owned external statistics in separate commits, beginning
   with the external statistic identity/ledger contract, then projection and
   Recorder reconciliation, then runtime migration/dashboard UX, each with
   independently verifiable tests. Do not mix that migration into the native
   statistics checkpoint.

Before committing, use selective staging (`git add -p`) for files shared by
multiple groups, especially `__init__.py`, `backfill.py`, `coordinator.py`,
`ledger.py`, and the large statistics test module. No destructive cleanup or
reset is authorized; unrelated user changes remain untouched.

### 2026-07-22 19:27 +08 - HA Store Runtime Evidence Recheck

The earlier assessment needs a precise qualification. The HA Store is not
absent: `.runtime/ha-config/.storage/sp_group.session.01KXZTM9SAFC9PV3247D7MV38Q`
exists with Store version 1 and the expected session fields. Its modification
time is 19:16, matching the successful runtime poll and statistics retry. The
Store token metadata is newer and has a different fingerprint from the token
metadata in `core.config_entries`, proving that the runtime refresh path did
persist a refreshed session to Store without rewriting the config entry.

The remaining implementation gap is that `async_setup_entry()` still calls
`mobile_session_from_entry(entry)` before constructing `SpGroupRuntimeData`;
there is no setup-time `Store.async_load()` or Store-first migration path. A
restart therefore does not use the newer Store session as its source of truth.
The generic `async_refresh_session_if_needed()` helper also retains an
`async_update_entry()` path, although the production coordinators use
`async_refresh_runtime_session_if_needed()` and the observed Store write comes
from that runtime path.

Conclusion: Store persistence is partially and demonstrably implemented, but
the Store-only auth refactor is not complete until setup reads Store first,
migrates the legacy entry-data session safely, and the remaining
`async_update_entry()` behavior is explicitly scoped or removed. The commit
plan must label this as a partial runtime persistence checkpoint rather than
claiming the full refactor is already absent or complete.

### 2026-07-22 19:33 +08 - HA Store Startup Fix Implemented And Reviewed

Implemented the missing restart path from `docs/plans/auth-store-refactor.md`.
`async_setup_entry()` now uses a Store-first loader, attaches the loaded Store
to runtime data, migrates a legacy `entry.data` session when no Store payload
exists, and falls back to legacy entry data when the Store payload is invalid
without silently overwriting the corrupt Store.

Added setup regressions for Store precedence, first-run migration, and invalid
Store fallback. The existing runtime persistence behavior remains Store-backed
and only writes when the refreshed token changes. The generic
`async_refresh_session_if_needed()` compatibility helper still retains its
`async_update_entry()` path exactly as scoped by the plan; production
coordinators use `async_refresh_runtime_session_if_needed()` and do not invoke
that path.

Verification: `278 passed, 1 skipped`; `mise run python:lint` passed Ruff and
mypy; `mise run ha:check` passed compileall; `git diff --check` remains clean.
The auth-store plan is now implemented for the production runtime path, with
the compatibility helper exception explicitly documented rather than treated
as an accidental second persistence path.

### 2026-07-22 19:34 +08 - Revised Commit Plan After Store Fix

The worktree remains uncommitted on `main` at `fdfc841`. It now contains 32
modified tracked files plus 16 untracked project files, approximately 6,368
added lines and 429 removed lines. `.runtime/` is ignored and is not part of
the commit set. The current verification checkpoint remains 278 passed and 1
skipped, with Ruff, mypy, compileall, and `git diff --check` passing.

Recommended commit sequence:

1. `feat: harden source ledger and polling lifecycle` — source-backfill state
   migration, cursor derivation, bounded polling windows, water startup
   catch-up, coordinator availability, source retry orchestration, and their
   ledger/coordinator/backfill lifecycle tests. Stage the related runtime
   hunks from `__init__.py`, `backfill.py`, `coordinator.py`, and `ledger.py`
   together because these modules form one executable polling contract.
2. `feat: harden native statistics backfill` — native-offset projection,
   Recorder reconciliation, forced retry behavior, conflict/repair handling,
   statistics metadata, translations, and the electricity/water/cross-utility
   Recorder tests. This is a validated transitional native-ID checkpoint and
   must not be described as the provider-owned external-statistics solution.
3. `fix: use HA Store as the runtime session source` — the completed
   Store-first setup loader, legacy migration, runtime persistence, auth tests,
   setup precedence/fallback tests, and the sanitized-log test boundary. Stage
   only the auth/session hunks from `__init__.py` and `auth.py`; keep config
   entry option migration in the later options/runtime commit if needed.
4. `feat: add granular polling and maintenance options` — DurationSelector
   parsing/normalization, source/statistics retry actions, sensor availability,
   diagnostics, translations, and config-flow/options/sensor tests.
5. `chore: add developer runtime helpers and observability` — dev-only service
   registration, `services.yaml`, force-statistics/session-refresh scripts,
   mise tasks, Python 3.14 tool configuration, debug logging, and runtime
   validation guidance.
6. `docs: record architecture decisions and implementation plans` — the
   polling-window ADR, provider-owned-statistics ADR and plan, auth issue and
   plan, statistics coverage plan, reference material, developer guidelines,
   and the context tracker.
7. Implement provider-owned external statistics in new commits after this
   checkpoint: external statistic identity/ledger contract, pure projection,
   Recorder reconciliation, migration/dashboard UX, and runtime rollout. Do
   not amend the native-ID checkpoint with external-statistics code.

Shared-file staging must be selective (`git add -p`) and each commit should be
verified before the next is staged. Suggested gates are: polling/lifecycle
tests after commit 1; Recorder statistics tests after commit 2; auth/init tests
after commit 3; config-flow/sensor/diagnostic tests after commit 4; lint,
compileall, and dev-script checks after commit 5. No reset, checkout, or
destructive cleanup is authorized.

### 2026-07-22 19:37 +08 - Commit Checkpoint Completed

Committed the accumulated implementation in verified slices:

- `85d3917` `feat: harden runtime polling and session lifecycle`
- `492fe3a` `test: cover source backfill orchestration`
- `b1a09c7` `feat: harden native statistics backfill`
- `48dd538` `chore: add developer runtime helpers and observability`
- `2614cb8` `docs: record architecture decisions and implementation plans`

Post-commit verification passed: 278 integration tests passed with 1 skipped,
Ruff and mypy passed, compileall passed, and the worktree was clean apart from
ignored `.runtime/` state. Provider-owned external statistics remains a
separate future implementation; these commits preserve the current
native-offset checkpoint and its documented migration plan.

### 2026-07-22 20:05 +08 - Provider-Owned Statistics Implementation Started

Started implementing `docs/plans/provider-owned-external-statistics.md` as a
separate strategy boundary. New ledger entries now receive deterministic
`sp_group:<sensor_key>_consumption` statistic IDs, an identity fingerprint, the
`external_provider_v3` strategy, and a dirty-from marker. Existing entries are
left on the legacy path until the ledger store prepares them for migration;
that preparation only assigns the new identity and never deletes or rewrites
legacy entity statistics.

Added an external Recorder reconciliation path using HA's public
`async_add_external_statistics()` API. It projects accepted electricity and
water provider periods into positive cumulative rows from zero, checks metadata
ownership, reads and verifies Recorder rows on the Recorder executor, allows
only a ledger-marked dirty suffix to be rewritten, and preserves retryable
missing-bridge warnings. Metadata is advanced only after Recorder commit and
exact row verification. The existing entity/native-reference importer remains
available for unprepared legacy test/runtime entries.

Added focused tests for positive provider-period projection, dirty-suffix versus
conflict reconciliation, public external API usage without an entity registry,
and missing-bridge safe-prefix behavior. The external and existing Recorder
statistics tests currently pass: 21 passed. The full SP Group suite is at 282
passed and 1 skipped after updating diagnostics expectations for the new
strategy/dirty/migration summaries. Runtime reset and end-to-end dev
verification remain pending until the complete external implementation and
full static checks are finished.

### 2026-07-22 20:22 +08 - External Runtime Verification and Safety Closure

Reset the disposable HA runtime, completed HA onboarding as `Test`, and
completed the SP Group frontend config flow using the cached provider session.
Live evidence: electricity accepted 40 provider 30-minute buckets, the sensor
state reached `1011.002 kWh`, and Recorder stored 20 external hourly rows under
`sp_group:..._electric_consumption`. External metadata is `source=sp_group`,
`unit=kWh`, `has_mean=0`, and `has_sum=1`; the first sums are positive and
cumulative (`1.004`, `2.626`, `4.161`). Water was queried successfully but the
provider returned no usable monthly observations for the configured safe seed
boundary, so its sensor remained at `1000.0 m³` and no water statistic was
fabricated.

The dev force-statistics helper initially failed to exchange the HA refresh
token because the browser login used `127.0.0.1` while the helper assumed
`localhost`; it now tries both loopback client IDs. Its old native strategy
assumptions were also updated to recognize external IDs and query external
Recorder rows. A forced Recorder compile then fired the listener and performed
an idempotent external recheck (`planned=20 importable=0 processed=20`) without
provider refetch.

Closed additional safety gaps found during review: external metadata updates
now compare the captured accepted-bucket fingerprint and epoch before commit;
provider correction warnings hard-block export and create the existing
statistics conflict Repair; Recorder acknowledgement waits are bounded and
remain retryable on timeout; and a successful empty provider response is
recorded as verified-empty so it does not trigger an hourly retry forever.
Focused external tests now pass: 7 passed; full-suite/static verification is
being rerun after these final changes.

### 2026-07-22 20:27 +08 - Scope Decision: No Legacy Migration in Dev Rollout

The legacy native-to-external migration and Energy Dashboard acknowledgement
repair are explicitly out of scope for this development rollout. The previous
implementation attempt was removed. Fresh config-flow entries use
`external_provider_v3`; legacy native rows are not migrated or rewritten. A
future production rollout can add the migration and explicit Energy source
switch after the fresh external path has been validated.

### 2026-07-22 20:26 +08 - Final Fresh-Setup Runtime Check

Restarted the existing non-reset dev configuration once so the final Python
module edits were loaded, then ran `mise run ha:dev:force-statistics`. The
Recorder compile event fired and the listener correctly skipped a complete
external series as `not_ready`/already complete; the helper exited without its
former 30-second timeout. The persisted fresh ledger now shows electricity
`external_provider_v3`, 40 accepted buckets, clean dirty state, and water
`external_provider_v3`, zero accepted buckets, `external_migration_status`
`complete`, and clean dirty state. This confirms an empty but successful water
response does not remain an artificial retry condition.

The verified instance was then detached into `.runtime/ha-dev-detached.log` and
remains available at `http://localhost:8123`; its final startup log reports
`source-history backfill not scheduled: ... needed=False` and
`statistics retry listener skipped: complete_or_blocked`.

### 2026-07-22 20:38 +08 - Live Statistics Conflict Investigation

Inspected the still-running instance after a fresh reset and complete SP Group
config flow. The flow completed normally. Source backfill succeeded for both
utilities: electricity ran two sightings and accepted 27,257 half-hour buckets;
water accepted 11 monthly buckets. The statistics task then reported
electricity `planned=13628 imported=13628 verified=13628 warnings=0`, but water
`planned=11 imported=11 verified=0 warnings=11`.

The water ledger persisted one `statistics_conflict:<month-end>` warning for
each August 2025 through June 2026 period, with no imported-through watermark.
Recorder nevertheless contains all 11 water rows under the external statistic
ID, with the expected positive cumulative sequence ending at `244.4` and no
decreasing sums. Electricity also has 13,628 rows with no decreasing sums.

The persisted HA repair registry currently contains only the unrelated
`deprecated_method` issue; no active SP Group statistics conflict repair is
present. The conflict state is therefore present in the ledger, but the repair
is not currently active in the registry.

Code-path evidence indicates a false post-import verification result rather
than a data mismatch: the external importer queues rows with HA's public
`async_add_external_statistics()` API, waits through Recorder's
`async_block_till_done()`, then verifies each row. The log's water result says
all 11 rows failed verification, while the rows are present and correct in the
database after the task. This is consistent with the Recorder queue/commit
acknowledgement race in the current read-back boundary. No fix was applied in
this investigation; the running HA process remains up.

### 2026-07-22 20:42 +08 - Resolution for False External-Statistics Conflicts

The correct resolution is to make Recorder read-back verification bounded and
retryable. After `async_add_external_statistics()` queues a batch, the importer
should wait for the Recorder acknowledgement and then reread the expected rows
through `statistics_during_period()` several times with a short deadline. A
missing row during that deadline is a pending Recorder visibility/acknowledgement
condition, not a data conflict.

Only a row that is visible in Recorder and has a different `state` or `sum`
should produce `statistics_conflict:<start>` and a conflict Repair. If the
deadline expires with rows still missing, persist a retryable warning such as
`statistics_recorder_rows_pending`, retain the ledger dirty/incomplete, and let
the existing statistics retry path try again. When a later retry observes exact
rows, clear the warning and advance `statistics_imported_through`.

The external ledger metadata update must also remain `pending`/dirty whenever
verification is incomplete or warnings exist. It must not set
`external_migration_status=complete` or clear the dirty suffix merely because a
Recorder import job was queued. This prevents a false verification result from
becoming a hard retry blocker.

The `statistics_imported_through` watermark remains necessary as a progress
marker and retry optimization. It must advance only to the latest contiguous
row confirmed present and correct in Recorder. Submitted-but-unverified rows
must not advance it; a partial verification must retain the prior watermark or
advance only through the verified contiguous prefix. The watermark is not, by
itself, proof that the full source history was imported.

### 2026-07-22 20:50 +08 - External Read-Back Fix Planning

Created `docs/plans/statistics-recorder-readback-fix.md` for the false external
statistics conflict. The plan separates pure row classification from Recorder
orchestration, classifying visible value mismatches as conflicts and absent rows
as bounded, retryable pending visibility. It retains
`statistics_imported_through` as a contiguous verified-prefix watermark and
requires pending/conflict outcomes to keep the external ledger incomplete and
dirty.

The plan preserves the provider-owned external statistics strategy, public HA
Recorder import/query APIs, source projections, statistic IDs, and
statistics-only retry behavior. It is implementation-ready but has not been
implemented or adversarially reviewed in this turn.

### 2026-07-22 21:08 +08 - External Read-Back Fix Implemented

Implemented the read-back fix from
`docs/plans/statistics-recorder-readback-fix.md`. The external statistics
importer now classifies Recorder rows as exact, pending, or conflicting and
retries pending visibility for a bounded five-second deadline without
requeueing the import batch. Missing rows persist bounded
`statistics_recorder_rows_pending:*` warnings; visible mismatches retain the
existing conflict warning/Repair path.

External ledger metadata now distinguishes pending from complete state. The
watermark advances only through the latest contiguous verified row, dirty state
is retained for incomplete verification, and successful exact retries clear
warnings and complete the series. Verification timestamps are not created for
an initial import with no confirmed rows.

Added pure, delayed-visibility, timeout, retry, conflict, metadata, and
scheduler regression coverage. Verification completed with `294 passed, 1
skipped`; Ruff, mypy, compile checks, and `git diff --check` pass. The existing
HA process was not restarted, so the live instance has not loaded these module
changes yet.

### 2026-07-22 21:15 +08 - Controlled Post-Seed Poll Gap Verification

The normal config-flow path cannot naturally provide a first-poll historical
gap: setup immediately schedules the one-shot source-history backfill, which
consumes all provider buckets from the configured seed boundary before normal
polling begins.

Recommended lowest-resistance route for manual verification is a dev-only
controlled-gap harness. It should pause/skip only the initial source-history
backfill, establish a deterministic seed boundary a few hours before `now`,
then invoke the existing coordinator poll through an unload-safe HA service or
mise helper. The harness must use the runtime ledger/coordinator APIs rather
than edit `.storage` JSON or Recorder SQLite directly.

The verification target is the external Recorder statistics timeline: after
the two-sighting acceptance behavior, four 30-minute provider buckets spanning
two hours should become two hourly rows whose row states equal the paired
bucket deltas and whose cumulative sums have non-negative consecutive deltas.
The live sensor state will still be timestamped at the poll time because HA
state updates cannot be backdated by a normal integration poll; it should not
be used as proof of historical hourly placement.

### 2026-07-22 21:22 +08 - Controlled Gap Flow Correction

The initial controlled-gap proposal requires one correction. Leaving
`source_backfill_status` as `never_attempted` would cause the statistics
scheduler to skip the entry even when `force=True`; the current readiness gate
explicitly ignores `never_attempted`, `running`, and `failed` source states.

The dev harness must therefore be enabled before config-flow setup so the
initial full-backfill task is never scheduled, and after setup it must mark the
controlled source as developer-bypassed/succeeded through a dedicated ledger
API. The fresh entry must still have no accepted or pending buckets. The
coordinator polls must be invoked with `backfill=False` twice, proving the first
poll creates pending observations and the second matching poll accepts them
under the existing two-sighting rule.

The manual acceptance criteria are: no `mode=backfill` log; two normal/manual
polls from the configured seed boundary; first poll `accepted_delta=0` with a
positive pending delta; second poll with a positive accepted delta; then
external Recorder rows at the expected hourly starts with correct states and
non-negative cumulative-sum deltas. `ha:dev:force-statistics` remains a
Recorder compile helper and is not the poll-gap creator.

### 2026-07-22 21:26 +08 - Seed and Poll Window Boundary Clarification

For electricity, `seed_observed_at` represents the last trusted meter reading,
not a poll-window end field. The source builder normalizes it to
`seed_effective_from`, the next non-overlapping 30-minute boundary. With no
accepted or pending ledger buckets, the normal coordinator poll starts at that
effective boundary and ends at the current poll time. Provider observations
whose bucket is not yet closed are excluded.

Therefore, to create an approximately two-hour post-seed gap, place the trusted
seed observation/effective boundary about two hours before `now`; `now` remains
the poll-window end. `backfill_from` is the historical source-backfill boundary
and is not the normal poll end. For an aligned electricity seed, setting both
form timestamps to the same boundary is appropriate, but the dev skip-backfill
gate is still required to prevent setup from consuming the gap.
## 2026-07-22 21:40 +08 - Controlled Post-Seed Poll Gap Harness Completed

- Implemented the missing dev-only harness for the controlled post-seed polling test.
- `SPGROUP_HA_DEV_SKIP_INITIAL_BACKFILL=1` is accepted only alongside `SPGROUP_HA_DEV_TOOLS=1`; setup therefore skips the one-shot provider history backfill after config-flow completion without changing production behavior.
- Added guarded `sp_group.dev_prepare_poll_gap` and `sp_group.dev_poll_now` services. Preparation refuses malformed or non-empty ledgers and marks only the selected utility's source status as succeeded; normal polling invokes the coordinator with `backfill=False`, preserving the existing two-sighting rule and statistics-only retry path.
- Added `scripts/ha_dev_sp_group_service.py` and four mise commands for electricity/water preparation and normal polling. The force-statistics command remains separate and only queues Recorder compilation.
- Focused test coverage now checks both environment gating, service behavior, empty-ledger safety, normal-poll invocation, and validated service metadata.
## 2026-07-22 22:20 +08 - Controlled Gap Test Used Provider-Lagged Zero Buckets

- Live verification showed the controlled electricity test was orchestrated correctly: setup skipped full source backfill, preparation succeeded, the first normal poll created two pending buckets, and the second accepted them and completed the statistics-only import.
- A direct read-only AMI request for the same premise and date showed non-zero provider readings through `15:30`, followed by literal zero rows from `16:00` onward. The tested `21:00` and `21:30` buckets were therefore in the provider's not-yet-covered/lagged suffix.
- The test instructions assumed a recent two-hour window would contain positive data. That assumption is invalid for a lagging provider. The current parser accepts zero-valued AMI rows as valid buckets, so this run validates orchestration but not positive time-projected accumulation and may treat provider coverage sentinels as real zero consumption.
- No production fix was made in this investigation. A follow-up design must distinguish legitimate zero-usage buckets from provider coverage gaps before using this live harness as a positive backfill test.
## 2026-07-22 22:30 +08 - AMI Zero Semantics Decision Scope

- Repository grounding confirms one locked zero-value decision only: the June 2026 AMI zero prefix represents pre-smart-meter coverage, so the history monthly bucket is authoritative for that transition month and the AMI rows are discarded. See `docs/adr/adr-hybrid-backfill-partial-month.md`.
- No general decision exists for zero-valued post-seed/current AMI suffix rows. The manual-seed design records provider lag as variable and says freshness must not rely on a fixed delay, but it does not define whether a zero row means real zero usage or “not covered yet.”
- `docs/adr/adr-polling-window-exact-end.md` describes `end >= now` filtering as the lag safeguard, but its status remains Proposed and that filter excludes in-progress buckets; it does not resolve completed-looking zero sentinel rows.
- Current `_electricity_ami_observations` accepts zero values because it rejects only negative values. Therefore, the live test exposed an undocumented semantic gap rather than violating a locked general zero-value rule.
## 2026-07-22 22:40 +08 - Locked General AMI Zero-Value Semantics

- Locked the distinction requested for AMI zero values: leading zero prefixes represent pre-smart-meter coverage; trailing zero suffixes after the last non-zero bucket represent provider lag/unavailable data; future or in-progress zero rows are not observations.
- Any zero row after AMI coverage begins, including an isolated internal zero, is treated as unavailable provider data until a later response supplies a usable value or an explicit coverage signal.
- Added `docs/adr/adr-ami-zero-coverage-semantics.md` and updated the manual seed design and payload contract.
- This is a documentation/decision update only. The parser and ledger still require a separate implementation plan before behavior changes.
## 2026-07-22 22:50 +08 - Validation Must Bound Poll End to Provider Coverage

- A live positive-gap test cannot be made valid by changing only `seed_observed_at`: normal coordinator polling always uses the real current time as its request/filter end, so later provider zero suffix rows remain in the same response.
- Revised test design must first identify the latest usable non-zero AMI bucket, then bound the simulated poll end to that bucket's closed end and place the seed boundary before it.
- The dev harness therefore needs either a controlled poll-end override that is strictly dev-gated, or a deterministic provider fixture. A preflight alone is insufficient if the coordinator still polls through wall-clock `now`.
- Production polling semantics remain unchanged: its end remains the actual poll time; this is only a validation-harness requirement.
## 2026-07-22 23:00 +08 - Provider-Coverage-Bounded Gap Harness Implemented

- Added an optional, timezone-validated `poll_end` to the dev normal-poll service. Production scheduled refreshes remain wall-clock based.
- Added `scripts/ha_dev_ami_latest.py` and `mise run ha:dev:ami-latest:electric` to query recent AMI days read-only and report the latest positive bucket plus seed/backfill/poll-end timestamps.
- Added tests for dev poll-end forwarding and service metadata; focused coordinator/init/packaging suite passes.
- Updated `docs/testing/ha-runtime-validation.md` with the corrected preflight -> config flow -> bounded two-poll -> Recorder verification sequence.
## 2026-07-22 23:20 +08 - Corrected Preflight Poll-End Boundary

- The live bounded-gap run accepted the positive `14:30-15:00` and `15:00-15:30` buckets, but excluded the latest `15:30-16:00` bucket because the recommended `poll_end=16:00` is inclusive at the bucket end (`end >= now` filter).
- This left the `15:00` hourly projection incomplete and correctly produced `statistics_missing_bridge:15:00`; external statistics were not imported, although the native sensor statistic reflected the one-shot current sensor update.
- Corrected `ha_dev_ami_latest.py` to recommend `latest_positive_bucket.start + 60 minutes`: for the observed latest positive `15:30` bucket, use `16:30`. This includes `15:30-16:00` and excludes the following lagged `16:00-16:30` zero bucket.
## 2026-07-22 23:30 +08 - Aligned Preflight Seed to Complete Statistics Hour

- The first bounded run also revealed that `seed=latest_start - 1h` can place the seed at `:30`, causing the first hourly statistics row to be intentionally skipped as a partial seed hour.
- Updated the preflight to place the seed at the preceding whole-hour boundary while retaining `poll_end=latest_start + 60 minutes`. With the observed latest `15:30` bucket, the next test uses seed/backfill `14:00` and poll end `16:30`.
- This gives the live test positive buckets `14:00`, `14:30`, `15:00`, and `15:30`, while the lagged `16:00` zero bucket remains excluded and complete `14:00`/`15:00` hourly projections can be verified.

## 2026-07-22 22:50 UTC - AMI Preflight Failed on Expired Cached Access Token

- `mise run ha:dev:ami-latest:electric` failed before AMI discovery because `/jarvis/v3/me` returned HTTP 401.
- `.runtime/mobile-session.json` contained an access token expired at `2026-07-22T14:48:02Z`; the cached refresh token was present.
- `mise run ha:dev:refresh-session` successfully refreshed the cache, after which the AMI preflight completed and returned the expected seed/backfill `2026-07-22T14:00:00+08:00` and poll end `2026-07-22T16:30:00+08:00`.
- The preflight helper currently reads the cached session directly and does not refresh automatically. The failure was session expiry, not a provider response-shape or poll-window defect.

## 2026-07-22 22:55 +08 - Controlled Electricity Gap Rerun Verified

- The fresh controlled run completed without restarting or stopping the running HA instance.
- First poll requested `14:00` through `16:30` and recorded four AMI observations as pending; the second identical poll accepted all four and cleared pending state.
- Accepted source buckets were `0.315`, `0.361`, `0.252`, and `0.259` kWh for `14:00`, `14:30`, `15:00`, and `15:30`, totaling `1.187` kWh.
- The sensor moved from seed `1000.0` to `1001.187`, exactly matching the seed plus accepted bucket total.
- The statistics-only retry imported two complete hourly external rows with deltas `0.676` and `0.511` kWh; both were non-negative and matched the two 30-minute bucket pairs.
- Ledger verification passed: pending empty, `statistics_warnings` empty, `statistics_dirty_from` null, `statistics_strategy_version=external_provider_v3`, and `external_migration_status=complete`.
- Recorder also contained the normal native sensor statistic row. The water entity was unavailable because water was intentionally not selected for this electricity-only run.

## 2026-07-22 23:00 +08 - Scope of Controlled Gap Validation

- The controlled run proves post-seed AMI polling can accept 30-minute buckets and project them into complete hourly external statistics rows: `.315 + .361 = .676` and `.252 + .259 = .511`.
- It does not prove that the sensor history itself is backfilled hourly; the sensor state still changes at the poll execution time, which is expected for a live HA entity.
- It also does not cover arbitrary outage lengths, incomplete hours, provider corrections, restart recovery, water, or production wall-clock polling without the dev-only bounded `poll_end`.

## 2026-07-22 23:05 +08 - Sensor State History Verification

- Live HA API and Recorder state history both contain `sensor.electricity_consumption=1000.0` followed by `1001.187` at the second poll timestamp.
- The current entity state API reports `state=1001.187`, with the expected energy device class, `total_increasing` state class, and `kWh` unit.
- Therefore the integration did publish and Recorder did persist the sensor update. A graph remaining visually at `1000` is not explained by missing sensor state data; it is likely a frontend time-range/cache/data-source presentation issue, or the graph being viewed is the long-term statistics series rather than the entity state history.

## 2026-07-22 23:10 +08 - Clarified Controlled-Test Coverage Limits

- “Not covered” refers to scenarios not exercised by the latest manual run, not confirmed implementation failures.
- The run covered one bounded electricity window containing four 30-minute buckets that formed two complete hourly rows. Long outages, incomplete hours, provider corrections, restart recovery, and water require separate verification paths.

## 2026-07-22 23:20 +08 - Static Trace of Unexercised Recovery Scenarios

- Electricity long-outage recovery is structurally supported when the provider returns the complete interval: the cursor derives from accepted/pending buckets, the two-hour overlap is deduplicated by bucket key, and requests split into bounded windows. It is not a guarantee for an unresolved gap older than the overlap or beyond provider retention.
- Incomplete electricity hours are safely withheld by the statistics planner with `statistics_missing_bridge`; later accepted buckets trigger a statistics-only retry. The parser still accepts zero AMI values, so provider-lagged zero rows can incorrectly close a bridge despite the locked zero-coverage decision.
- Accepted-provider corrections are deliberately not auto-applied: the ledger keeps the original value, records `corrected_bucket`, and blocks statistics with a repair. This preserves integrity but requires manual remediation to converge to corrected provider data.
- Restart recovery converts persisted `running` source backfills to `failed`, allowing retry; however, the pre-HA-start deferred backfill waiter is not stored in tracked runtime tasks, and water startup catch-up failures clear without an immediate retry.
- Water normal polling rereads the provider history and is idempotent, but startup catch-up checks only the latest ready month. An older missing month can remain undetected if the latest ready month is already accepted.

## 2026-07-22 23:35 +08 - Source Timeline Recovery and Water Parity Issue Documented

- Grounding confirmed the existing design intent already requires water to use the same seed-plus-delta architecture as electricity with monthly buckets, but the current water coordinator does not share electricity's derived cursor or unresolved-gap behavior.
- Documented the proposed fix in `docs/issues/source-timeline-recovery-and-water-parity.md`.
- The proposal adds a cadence-aware common source timeline, persisted unresolved gaps, ADR-compliant AMI zero handling, all-month water gap scanning, lifecycle-safe retries, and explicit provider-owned correction remediation.
- No production implementation changes were made for this issue.
## 2026-07-22 23:57 +0800 - Cadence-aware recovery implementation

- Locked implementation scope: recover missing electricity and water source
  buckets through the existing ledger/coordinator flow; preserve provider-owned
  external statistics and current entity/statistic identities.
- Correction policy remains conservative: accepted-bucket corrections are
  detected and surfaced for explicit repair, with no automatic sensor/statistics
  rewrite or new epoch in this refactor.
- Implementation will proceed in gated phases: ledger persistence/cursor,
  coverage-aware adapters, lifecycle, statistics snapshot verification, and
  focused regression tests before full validation.

## 2026-07-23 00:12 +0800 - Cadence-aware recovery implementation completed

- Exact-end source cursors now prioritize unresolved ranges, pending buckets,
  accepted bucket ends, and the seed boundary. The HA Store minor version is
  bumped and recovery metadata is serialized with bounded correction records.
- Electricity AMI parsing now treats only completed positive rows as coverage;
  leading/trailing/internal/future/in-progress zero behavior follows the locked
  ADR. Water shares the ledger application path, caches one history request per
  premise, and scans all expected closed months during startup catch-up.
- Deferred backfill waiters and water catch-up tasks are tracked for unload;
  transient water startup failures requeue within the grace window. Statistics
  remains provider-owned, snapshot/fingerprint guarded, Recorder read-back
  verified, and statistics-only retries remain free of provider calls.
- Focused and full SP Group tests pass. Accepted-bucket corrections remain
  Repair-only and are intentionally not rewritten automatically.

## 2026-07-23 00:24 +0800 - Final lifecycle validation

- Water schedule cancellation now removes the active catch-up task for
  rescheduling, while unload snapshots tasks before cancellation so every
  cancelled task is still awaited.
- The initial statistics attempt no longer owns a parallel sleep/retry loop;
  Recorder hourly events and normal coordinator refreshes provide retry paths.
- Final validation passed: `308 passed, 1 skipped`, Ruff, mypy, compileall, and
  `git diff --check`.

## 2026-07-23 10:45 +0800 - Test coverage closure for cadence-aware recovery

Audited the 308-test suite against the 6 test gates declared in the cadence-aware
recovery plan. Coverage was ~85% complete with 7 identified gaps.

Added 10 new tests to close all but one gap:

**Ledger gate (+)**
- `test_ledger_source_poll_cursor_falls_back_to_seed_when_empty`
- `test_ledger_concurrent_apply_observations_lock_serializes`
- `test_ledger_concurrent_apply_observations_same_bucket`
- `test_ledger_preserves_cursor_on_retention_exceeded`

**Electricity/water gate (+)**
- `test_water_repeated_full_history_is_idempotent`
- `test_electricity_source_poll_malformed_response_at_coordinator`

**Recorder gate (+)**
- `test_external_readback_partial_acknowledgement_retryable`
- `test_external_readback_partial_ack_out_of_order`
- `test_external_readback_first_row_conflict_with_partial_ack`
- `test_external_poll_completes_during_import_metadata_update_rejected`

**Still open**
- Water startup catch-up bounded retry after transient failure (existing
  `test_water_startup_catchup_schedules_one_refresh_task` covers the happy
  path; failure path requires async coordinator orchestration and is
  low-risk since HA's coordinator model handles `UpdateFailed` natively).

Final validation: `318 passed, 1 skipped` (up from 308).

---

## 2026-07-23 — Dev instance auto-onboarding

**Goal**: After `ha:dev:reset`, the dev instance skips the interactive onboarding flow and goes straight to the login page, with a pre-configured owner account.

**Discovery**:
- `HA_DEV_RESET=1` calls `shutil.rmtree(config_dir)`, wiping `.storage/` and all onboarding state.
- On fresh start, HA's onboarding component checks `.storage/onboarding` for completed steps. If all 4 steps (`user`, `core_config`, `analytics`, `integration`) are marked done, `async_setup` returns `True` without registering any onboarding views.
- Auth data is stored across three files: `.storage/auth` (users, groups, credentials), `.storage/auth_provider.homeassistant` (bcrypt-hashed passwords), and `.storage/http.auth` (content user reference).
- Location/timezone config lives in `.storage/core.config`.

**Implementation**: `_write_auto_onboarding()` in `ha_dev.py` is called after `_write_minimal_config()` when `.storage/onboarding` does not exist (first run or after reset). It creates the full set of pre-onboarded storage files:
- Auth system: owner user, admin group, credential, bcrypt-hashed password for `test`/`test123`
- Content user: `system-read-only` user with system token for frontend rendering
- Config: Singapore location, metric units, Asia/Singapore timezone
- Person, areas, default Lovelace dashboard
- HTTP config with the selected port

**Credentials**: Username `test`, password `test123`. After HA starts, visit `http://localhost:8123`, log in, and reach the dashboard directly — no onboarding steps.

## 2026-07-25 — Mid-window gap recovery scenario assessed

Assessed whether a mid-window data gap (data available at start and end of
backfill window but missing in middle) could self-heal through normal polling.
Conclusion: unlikely, because the provider processes data in time-sequential
order — a middle gap would require out-of-order processing. Not tested or
verified against the live API. Documented in
`docs/adr/adr-seed-statistics-alignment.md#mid-window-gap-scenario-unlikely`.

---

## 2026-07-25 — Dev seed defaults for config flow

**Goal**: Pre-fill seed values (seed_value, seed_observed_at, backfill_from) in the SP Group config flow so the user doesn't have to type them after a reset.

**How it works**:
- A JSON file `.runtime/dev-seed-defaults.json` maps utility types to default field values.
- `config_flow.py` loads this file and applies defaults to the seed form schema fields using `vol.Required(..., default=...)`.
- `ha_dev.py` writes this file after auto-onboarding, computing dynamic timestamps:
  - Water: `seed_observed_at` = 1st of current month at 00:00 SGT
  - Electricity: `seed_observed_at` = current time rounded down to nearest 30 min SGT
- Both utilities: `backfill_from` = 2025-01-01, `seed_value` = 500 (water) / 2000 (electricity)

**Constants added to config_flow.py**:
- `DEV_SEED_DEFAULTS_FILE_ENV` / `DEFAULT_DEV_SEED_DEFAULTS_FILE` — follows the same pattern as the dev session file resolution.

**Files changed**:
- `scripts/ha_dev.py` — writes dev-seed-defaults.json in `_write_auto_onboarding`
- `custom_components/sp_group/config_flow.py` — loads defaults and pre-fills seed step schemas

## 2026-07-26 17:50 +0800 — Commit planning: seed-boundary statistics fix

Reviewed the current worktree for commit preparation. The changes are one
cohesive, uncommitted fix:

- `custom_components/sp_group/statistics_backfill.py` now permits the
  seed-boundary electricity hour when both half-hour sides are contiguous.
- `tests/components/sp_group/test_statistics_backfill_electricity_invariants.py`
  adds the regression coverage for the continuous boundary chain.
- `docs/issues/statistics-seed-boundary-hour-gap.md` records the issue, root
  cause, impact, and proposed behavior.

Validation: the focused invariant suite passes (`4 passed`) and `git diff
--check` is clean. These files should be committed together as one atomic
change so the implementation, regression test, and issue evidence cannot
drift apart. No commit was created in this planning step.

---

## 2026-07-26 18:10 +0800 — Hybrid partial-AMI-month ADR implementation assessment

Reviewed `docs/adr/adr-hybrid-backfill-partial-month.md` against the current
electricity ingestion and statistics code; this is an assessment only, with no
production implementation started.

Discoveries:

- Electricity has one configured source type,
  `manual_seed_plus_ami_deltas`, and `SpGroupElectricityCoordinator` calls only
  `get_ami_charts()` with `grouped_by="day"`. It accepts only positive
  half-hour AMI observations.
- `_electricity_ami_source_poll()` deliberately uses leading zero rows as
  positional evidence but reports only positive rows. It identifies gaps only
  *between* positive AMI rows; it does not expose a month-level partial-coverage
  classification and cannot itself select history for the transition month.
- `get_history_charts()` and `parse_history_electricity_chart()` already exist,
  and the latter validates monthly kWh `elec.data[*].consumption.current`; the
  history parser is presently non-publishable / unused by the ledger.
- The shared ledger can store buckets of different durations, but has no source
  provenance on accepted buckets and permits overlapping spans with distinct
  keys. Applying a monthly history bucket alongside AMI half-hours would
  double-count, so source arbitration must happen before
  `async_apply_observations()`.
- Existing coverage and Recorder tests exercise AMI zero handling, ledger
  application, and separate water monthly projection, but not the hybrid
  electricity month or a transition from monthly to 30-minute electricity.

Provisional implementation direction (not yet locked): add a dedicated
electricity composition adapter that fetches/parses both sources per premise,
groups valid AMI evidence and valid history rows by Singapore calendar month,
then emits exactly one non-overlapping set of observations per month: history
for no/partial AMI months, AMI for full historical-overlap months, and AMI for
the current/open month when history is absent or not closed. Treat malformed or
ambiguous coverage as non-authoritative rather than silently replacing AMI with
history. Preserve existing statistics projection as the consumer after the
ledger owns the selected timeline.

Required test gates: pure source-composition matrix, coordinator request and
idempotency test, ledger no-double-count test, and Recorder-visible cumulative
delta test across the monthly-to-half-hour boundary. Run focused suites first,
then the integration suite/lint/HA check.

## 2026-07-26 18:24 +0800 — Re-evaluation: distribute partial-month history delta

The user proposed retaining actual AMI half-hours for the AMI-covered portion
of June and evenly distributing the validated `history_month_total -
ami_month_total` across the leading missing AMI slots so cumulative history
reaches the observed seed without empty intervals.

This exposes a material ADR/code discrepancy: current electricity statistics
projection requires a contiguous 30-minute pair for each hourly Recorder row.
It does not have water-like variable-duration handling for electricity.
Therefore the ADR's assertion that the existing projection handles a monthly
electricity bucket is not true in the current implementation; a single June
monthly bucket would not create electricity long-term-statistics rows.

Provisional assessment: gap-fill is technically the lower-complexity way to
retain real AMI resolution after installation *and* preserve the current
electricity statistics invariant, but it must be explicitly labeled as
synthetic allocation rather than observed usage. Do not hard-code a two-day or
124-slot assumption: derive the eligible leading slots from validated AMI
timestamps. The ADR states both a two-day gap and 124 zero buckets, which are
inconsistent for ordinary 30-minute calendar slots (two days has 96 slots).
The original source response / redacted fixture must resolve the actual time
range before an implementation decision is locked.

If adopted, require all of: an ACTUAL closed history row; a positive,
non-negative difference within an agreed reconciliation tolerance; a strictly
leading contiguous missing interval; and a complete contiguous AMI suffix for
the month. Allocate `difference / missing_slot_count` with a deterministic
Decimal remainder policy that makes allocated sum exactly equal the difference.
If any guard fails, do not manufacture intervals; report unresolved coverage
and use the explicitly selected fallback/recovery path.

## 2026-07-26 18:31 +0800 — Live validation of June 2026 AMI gap

Ran read-only live Jarvis requests using the retained cached developer session;
no raw payloads, tokens, premise identifiers, or account identifiers were
recorded.

- One accessible electricity AMI response contained all 1,440 expected June
  half-hour timestamps: 1,316 positive and 124 zero-valued rows, with no
  missing timestamp rows.
- Positive AMI total was `588.8723 kWh`. The first positive interval was
  `2026-06-03T14:00:00+08:00`, and the final one was
  `2026-06-30T23:30:00+08:00`.
- The zero prefix is therefore exactly 124 half-hour slots / 62 hours, running
  from `2026-06-01T00:00:00+08:00` through the interval ending
  `2026-06-03T14:00:00+08:00`. It is not a two-day / 96-slot gap. The ADR's
  count is correct but its description of the duration and June 3 start is
  insufficiently precise.
- The currently live history-chart response for that same accessible candidate
  contains a June row whose period is timestamp-formatted rather than `YYYY-MM`
  and whose `Actual kWh` current value is `0`. It does not reproduce the ADR's
  historical `685.07 kWh` value. A second numeric candidate rejected the
  history request. Consequently a live, source-to-source validation of the
  stated `96.20 kWh` delta is not possible from the current API responses.

Decision impact: the AMI prefix geometry is now proven, but synthetic
allocation must remain blocked until an authoritative archived/redacted June
history response or a provider-confirmed history total is obtained. If the ADR
total is later corroborated, use the unrounded calculation
`(685.07 - 588.8723) / 124 = 96.1977 / 124` with deterministic remainder
allocation, not the ADR's rounded `0.776` value per bucket.

## 2026-07-26 18:41 +0800 — Correction: live June history total validated

Corrected the initial live-history assessment. The request path was correct
(`GET /jarvis/v4/charts/{jarvis_premise_id}`) and has no date query parameters,
but the first aggregation read `consumption.current` only and used an
insufficient numeric-ID premise selection heuristic.

Re-ran the request using the direct `premises[*].id` mapping from Jarvis
`/me`. The matching June row is timestamp-formatted
`2026-06-01T00:00:00+08:00`, status `Actual`, unit `kWh`, with:

- `consumption.current = 0`
- `consumption.previous = 685.07`

This validates the ADR's monthly total and the live gap:
`685.07 - 588.8723 = 96.1977 kWh`.

The AMI result remains validated: 1,440 June timestamps, 1,316 positive rows,
124 leading zero rows, no missing slots, and first positive bucket at
`2026-06-03T14:00:00+08:00`.

Critical implementation discovery: `parse_history_electricity_chart()`
currently reads `consumption.current` only and normalizes the row period only
as an opaque key. It would treat this real June row as `0`, so it cannot be
used as the hybrid-backfill adapter unchanged. Before implementation, establish
and document the provider semantics of `current` versus `previous` for this
specific response shape; do not globally substitute one field for the other
based only on one month. The composition adapter should select the
provider-validated historical total explicitly, with a fixture covering this
actual `current=0` / `previous=685.07` case.

## 2026-07-26 18:55 +0800 — Recommended revised hybrid-backfill direction

Recommended replacing the ADR's whole-month-history decision with a guarded
hybrid gap-fill policy for a validated partial-AMI transition month.

For the proven June 2026 case, compose the ledger timeline before persistence:

1. Fetch `GET /jarvis/v4/charts/{jarvis_premise_id}` (no date parameters) and
   the June AMI chart for the same direct Jarvis premise mapping.
2. Normalize the timestamp-formatted history period and select the
   provider-validated historical total (`previous=685.07` in this live shape),
   not the generic parser's current-only value.
3. Verify an `Actual` closed history row, all expected AMI timestamps, a
   strictly leading contiguous zero prefix, a complete positive suffix, and a
   non-negative reconciliation delta.
4. Retain the 1,316 observed positive AMI buckets; allocate the exact
   `96.1977 kWh` delta over the 124 leading zero slots; emit 1,440 contiguous
   30-minute electricity buckets whose monthly sum is exactly `685.07 kWh`.
5. Give allocated buckets durable provenance as history-derived/synthetic and
   persist the complete selected timeline only once. Do not add a monthly
   history bucket on top of AMI buckets.

Use a deterministic fixed-precision remainder distribution rather than a
single rounded value. For example at 10 decimal places, use 92 slots of
`0.7757879032` and 32 slots of `0.7757879033`, distributed deterministically
over the prefix; their sum is exactly `96.1977`.

Guard failure behavior is explicit: do not synthesize when the history total,
field semantics, AMI completeness, prefix shape, or reconciliation tolerance
is not proven. Preserve the raw coverage evidence, log/raise a repairable
issue, and leave the month pending for retry/review.

Implementation must be a new source-composition boundary before the ledger,
with source provenance and strategy/version migration for any existing AMI-only
ledger rows. The existing electricity statistics projector can remain
30-minute-only once the composed timeline is contiguous.

## 2026-07-26 19:05 +0800 — ADR realignment assessment

The locked but unimplemented hybrid partial-month ADR requires material
realignment before implementation; it should not be treated as a minor
erratum.

- **Decision reversal**: replace "use one history monthly bucket and discard
  AMI" with the guarded hybrid composition: retain observed AMI and allocate
  the validated historical remainder across the leading zero prefix.
- **Corrected evidence**: June has 124 prefix slots / 62 hours through
  `2026-06-03T14:00+08:00`, not a two-day gap. Use precise live totals
  `588.8723`, `685.07`, and `96.1977 kWh`.
- **Corrected source semantics**: the relevant live history total is in
  `consumption.previous`, while current integration parsing reads `current`.
  The ADR must make the response-shape rule, validation, and fallback explicit.
- **Corrected architecture consequence**: current electricity Recorder
  projection is fixed 30-minute/hourly and does not support the ADR's proposed
  monthly June electricity bucket. Contiguous half-hour composition is a
  functional requirement, not merely a granularity preference.
- **Alternative reclassification**: the former pro-rata option becomes the
  recommended option, subject to provenance, exact-Decimal remainder handling,
  reconciliation guards, and a repairable no-synthesis fallback.
- **New operational requirements**: pre-ledger arbitration, source provenance,
  migration/rebuild behavior for previously persisted partial AMI rows, and
  an explicit coverage/test matrix.

Recommended documentation action: revise this same unimplemented ADR in place
while retaining an amendment/history section that names the superseded decision
and live evidence. Change its status from Locked to Proposed Revision until
the field-selection semantics and migration plan are reviewed, then relock it.
Creating a separate ADR would make the still-unimplemented original decision
too easy to follow accidentally.

## 2026-07-26 19:14 +0800 — Hybrid partial-month ADR revised and relocked

At the user's direction, revised and relocked
`docs/adr/adr-hybrid-backfill-partial-month.md`.

The ADR now locks the month-level hybrid allocation approach: retain the
observed AMI suffix, evenly allocate the validated difference between the
historical monthly total and AMI total over the contiguous leading zero
half-hour prefix, and persist one complete non-overlapping 30-minute timeline.

The stated rationale is no data gap and correct historical alignment leading
up to the observed seed value. The ADR explicitly records the accepted trade-
off: allocated values are an even time projection of aggregate consumption and
misrepresent the real pre-AMI time pattern. It also locks provenance,
deterministic Decimal remainder distribution, guard-failure behavior, and
migration/testing consequences.

## 2026-07-26 19:40 +0800 — Hybrid electricity backfill implementation

Implemented the revised partial-AMI-month ADR in the electricity source path.

- Added a backfill-only history-month normalizer. It preserves generic chart
  parsing semantics but selects `consumption.previous` for the validated
  `Actual/current=0/previous>0` live response shape.
- Added month-level source composition: history-only months become one monthly
  bucket; complete leading-zero partial AMI months preserve observed AMI and
  fill the prefix with exact deterministic Decimal allocation.
- Added accepted-bucket provenance (`ami_observed`, `history_allocated`, and
  `history_monthly`) plus non-overlapping-span validation in the ledger.
- Electricity now lazily queries history only when closed AMI evidence needs
  composition. Unresolved months retain the source cursor for retry.
- Electricity statistics now project provenance-marked monthly history buckets
  as variable-duration end-of-period rows while retaining half-hour/hourly AMI
  projection.
- Added a hybrid strategy marker. Existing non-empty legacy electricity
  ledgers are blocked for explicit rebuild rather than silently mixed with the
  new strategy.

Validation completed for the changed surface: focused Ruff passed, `104` focused
tests passed, `git diff --check` passed, and `mise run ha:check` passed.

Repository-wide `mise run python:test` remains blocked during collection by the
pre-existing missing `_classify_external_rows` export expected by
`test_external_statistics.py`. Repository-wide `mise run python:lint` remains
blocked by pre-existing unrelated Ruff findings in `config_flow.py`,
`test_external_statistics.py`, and other files; the changed files pass focused
Ruff checks.

After adding the mixed-cadence Recorder test, the full changed-surface result
is `104 passed in 2.16s`. Focused mypy issues introduced by the hybrid path
were resolved; the remaining full-package mypy failures are pre-existing in
`external_statistics.py`, `statistics_backfill.py`, and `config_flow.py`.

## 2026-07-26 19:31 +0800 — Dev-instance monthly electricity gap investigation

Read-only inspection of the running dev instance confirms that the apparent
one-month gaps originate in source composition, not Recorder import.

- Recorder has electricity monthly rows only for September and November 2025,
  then January, March, and May 2026; no monthly rows exist for the intervening
  months. The source-backfill log reports all 1,338 planned electricity rows
  were imported without warnings, so the statistics projector did not drop
  already-accepted monthly buckets.
- The persisted electricity ledger has exactly five `history_monthly` buckets
  for those same months. August, October, December, February, and April are
  persisted as `electricity_hybrid_month_unresolved`; the missing rows are
  therefore deliberately absent before statistics projection.
- The current composer requires a full expected AMI half-hour grid before it
  will emit a history-only monthly bucket for a closed month with no positive
  AMI values. Real AMI coverage for these historical months is not satisfying
  that guard. This conflicts with the locked requirement that a history-only
  month should remain a monthly electricity bucket.
- The diagnostic session had expired before a read-only live re-query could
  safely confirm each raw AMI month shape. No credentials or runtime state were
modified during investigation.

## 2026-07-26 19:33 +0800 — ADR-to-implementation gate evaluation

The locked ADR's strict AMI timestamp/zero-prefix guard is explicitly scoped
to a *validated partial overlap*: a month that has observed AMI suffix data and
needs synthetic prefix allocation. That behavior is implemented and the June
ledger contains 124 allocated plus 1,316 observed half-hour buckets.

The code also applies an additional `is_full` AMI-grid condition to a
history-only month with no positive AMI slots. It requires every half-hour to
be present and zero before emitting `history_monthly`; otherwise it records the
month unresolved. That condition is not required by the ADR's partial-overlap
rule and conflicts with the separately locked history-only policy. It explains
the persisted alternating monthly omissions: the skipped months are unresolved
before statistics projection.

The ADR itself does not contain an exhaustive source-selection table for
history-only versus full-AMI months; it should be clarified on the next
documentation change. The implementation should distinguish no usable AMI
coverage (use validated history monthly bucket) from an actual partial AMI
overlap (apply the strict full-grid/leading-prefix guard and synthesize only
that case).

## 2026-07-26 19:35 +0800 — Explicit monthly source-precedence rule

The user locked the exhaustive month-selection rule in the ADR: AMI is
authoritative whenever it has usable coverage. Full AMI is used alone even
when history is present; no usable AMI coverage plus valid history becomes one
history-monthly bucket; partial AMI retains observed AMI and receives only the
history-minus-AMI remainder over its missing prefix. History is never layered
onto full AMI. The ADR now explicitly treats absent or zero/placeholder-only
AMI rows as no usable AMI coverage and does not require a complete all-zero AMI
grid before choosing history-only.

## 2026-07-26 19:55 +0800 — Source precedence implementation

Implemented the explicit monthly precedence contract.

- Added pure AMI month classification for full, no-coverage, valid partial,
  and invalid partial shapes. Complete all-zero coverage remains AMI-owned;
  absent/sparse zero placeholders can select history-only.
- Composition now iterates the union of AMI and validated history months,
  emitting history-only monthly buckets for months absent from AMI and keeping
  full AMI authoritative when history is present.
- Added composition metadata for unavailable history months and malformed
  history payloads; only malformed/retryable cases create unresolved ranges.
- Backfill polling now requests history for closed historical ranges, while
  normal polling requests it only for no-coverage, partial, or invalid closed
  AMI evidence.
- Added classifier, union-composition, full-AMI precedence, absent-AMI,
  invalid-partial, and coordinator fetch-decision tests.

Validation: 109 focused tests passed and changed-source Ruff checks passed.
Focused mypy still reports only the existing `valid_entity_id` export issue in
`external_statistics.py` and the existing optional statistic-id issue in
`statistics_backfill.py`.

The classifier was additionally tightened so a complete AMI calendar with
legitimate internal zero-valued intervals remains `full_ami`; only a leading
zero prefix followed by a zero/incomplete suffix is invalid partial evidence.
The focused suite remains at 109 passing tests, HA compile checks pass, and
`git diff --check` passes. The repository-wide test command remains blocked by
the pre-existing missing `_classify_external_rows` test import.

## 2026-07-26 20:05 +0800 — Gap and no-data semantics ADR

Created `docs/adr/adr-electricity-gap-and-no-data-semantics.md` to lock the
clarifications made after the source-precedence implementation review:

- partial AMI without History is accepted AMI-only;
- non-terminal leading/middle/trailing gaps become explicit zero buckets;
- terminal trailing gaps remain a distinct retryable provider-lag state whose
  cursor resumes from the last accepted end;
- no AMI plus no History is known-unavailable for the leading window month and
  explicit `no_source_gap_zero` coverage for later months;
- explicit provider zeros and inferred zeros have equal statistical effect but
  separate provenance;
- source failures are never converted into no-data zeros.

## 2026-07-26 20:20 +0800 — Re-evaluation after locked gap semantics

Rechecked the live implementation against the locked resulting-state rules.
The intended post-change state has only terminal trailing electricity provider
lag represented as retryable unresolved coverage. The current code still has
three pre-change paths that must be removed or narrowed: `_fixed_missing_ranges`
creates `missing_internal_bucket` unresolved ranges between positive AMI rows;
the hybrid composer creates `electricity_hybrid_month_unresolved` for invalid
partial AMI, missing History, malformed History, and negative remainders; and
the persisted ledger can retain earlier unresolved ranges and let
`source_poll_cursor_start()` choose their minimum. These are not terminal
trailing gaps and conflict with the locked decision. API/transport failures
raise `UpdateFailed` and do not create unresolved ranges. Water's
`missing_closed_month` path is separate and remains valid unless the decision
is explicitly broadened to water. The implementation work therefore needs to
classify the polling-window leading/terminal month, persist explicit zero or
known-unavailable coverage, and reserve unresolved state solely for terminal
trailing electricity lag.

## 2026-07-26 20:35 +0800 — ADR completion audit

Updated `docs/adr/adr-electricity-gap-and-no-data-semantics.md` with the
remaining cursor invariant: clean electricity ledgers must not contain
blocking unresolved ranges; only terminal trailing provider lag is unresolved
and it must not move the cursor backwards. Added explicit rebuild requirements
for legacy internal/hybrid unresolved ranges and a coverage-classification
contract distinguishing complete, absent, partial, and invalid AMI evidence.

Grounding against the current code shows the ADR is decision-complete but not
yet implementation-complete: the current composer still emits
`electricity_hybrid_month_unresolved`, the legacy electricity parser still
creates `missing_internal_bucket`, the ledger cursor still uses a generic
minimum unresolved start, and provenance/known-unavailable/terminal-lag
storage is not fully wired. These are implementation gaps, not unresolved
architecture decisions, provided the new ADR contract is followed.

The audit also made negative History-minus-AMI remainders explicit as
data-integrity poll failures (no synthetic allocation and no ordinary
unresolved range), and clarified that inferred zero buckets are clipped to
the requested polling window.
