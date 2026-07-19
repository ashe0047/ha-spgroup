# Production Readiness Checklist

**Generated:** 2026-07-17

Checklist of identified gaps between the current SP Group HA integration and
production-readiness.  Items are ordered by severity within each category.

---

## Critical

- [x] **BUG: undefined `runtime` variable in `__init__.py:113`**
  `_any_entry_missing_watermark(runtime)` references a variable that is never
  defined, causing a `NameError` wrapped in a bare `except Exception` so the
  backfill fires unconditionally on every HA restart.
  **File:** [__init__.py](/custom_components/sp_group/__init__.py#L113)

- [x] **Backfill fires unconditionally on every restart (optimisation dead code)**
  The `try/except Exception` around `_any_entry_missing_watermark(runtime)` on
  line 113 swallows the `NameError`, so `_backfill_needed` always defaults to
  `True`.  Even when all utilities have completed statistics import, the backfill
  fires on every HA restart.  Requires two fixes: (a) correct the `runtime`
  variable binding, and (b) narrow the except clause so genuine failures surface
  instead of being silently absorbed.
  **File:** [__init__.py](/custom_components/sp_group/__init__.py#L112-L115)

- [x] **SDK distribution strategy unresolved**
  `manifest.json` has an empty `requirements` array.  The SDK is currently
  installed via `pip install -e ../../../../../python-packages/spgroup`.  Must choose one of:
  (a) add SDK as a pip/git manifest requirement, or (b) vendor the SDK into
  the custom component.  HACS users cannot install the integration today.

---

## Security
## Security

- [ ] **Add `async_will_remove_from_hass` for session revocation**
  When a config entry is removed, the SP Group refresh token should be
  revoked (or at minimum the stored session cleared).  No handler exists
  today.

- [ ] **Strip dev auth bypass from production `config_flow.py`**
  `SPGROUP_HA_DEV_AUTH_BYPASS` and `SPGROUP_HA_DEV_SESSION_FILE` env-var
  paths and the associated dev auth steps live in the production config
  flow.  Gate behind a build flag or strip before distribution.

- [ ] **Review diagnostics for session-data leakage**
  `data_keys` includes `"session"` in the diagnostics output, revealing
  that a session is stored.  Confirm no serialized token data leaks into
  any diagnostics payload.

---

## HA Quality Scale Compliance

### Bronze (all todo)

- [ ] `action-setup` -- Define HA service/action definitions
- [ ] `appropriate-polling` -- Confirm poll intervals are sensible
- [ ] `brands` -- Add brand icons/images if applicable
- [ ] `common-modules` -- Use HA shared modules where applicable
- [ ] `config-flow-test-coverage` -- Measure and improve config flow test coverage
- [ ] `config-flow` -- Validate config flow follows HA best practices
- [ ] `dependency-transparency` -- Document all runtime dependencies
- [ ] `docs-actions` -- Document all actions/services
- [ ] `docs-conditions` -- Document all conditions (none exist but rule must be addressed)
- [ ] `docs-high-level-description` -- Write high-level integration description
- [ ] `docs-installation-instructions` -- Write end-user installation guide
- [ ] `docs-removal-instructions` -- Write removal instructions
- [ ] `docs-triggers` -- Document all triggers (none exist but rule must be addressed)
- [ ] `entity-event-setup` -- Ensure entity lifecycle events are handled
- [ ] `entity-unique-id` -- Verify all entities have stable unique IDs
- [ ] `has-entity-name` -- Verify all entities use `has_entity_name`
- [ ] `runtime-data` -- Verify runtime data pattern is correct
- [ ] `test-before-configure` -- Add pre-configure validation tests
- [ ] `test-before-setup` -- Add pre-setup validation tests
- [ ] `unique-config-entry` -- Prevent duplicate config entries for the same account

### Silver (all todo)

- [ ] `action-exceptions` -- Map HA exception types for actions
- [ ] `config-entry-unloading` -- Verify clean unload path
- [ ] `docs-configuration-parameters` -- Document all config entry options
- [ ] `docs-installation-parameters` -- Document installation parameters
- [ ] `entity-unavailable` -- Handle and communicate unavailable entity states
- [ ] `integration-owner` -- Document integration owner/maintainer
- [ ] `log-when-unavailable` -- Add logging when entities become unavailable
- [ ] `parallel-updates` -- Set `parallel_updates` on coordinators
- [ ] `reauthentication-flow` -- Verify reauth flow end-to-end
- [ ] `test-coverage` -- Establish and meet test coverage targets

### Gold (all todo except diagnostics)

- [ ] `devices` -- Register premises/accounts as HA devices in device registry
- [ ] `discovery-update-info` -- Support for discovery metadata updates
- [ ] `discovery` -- Support for re-discovery of utilities
- [ ] `docs-data-update` -- Document data update intervals and behavior
- [ ] `docs-examples` -- Add usage examples
- [ ] `docs-known-limitations` -- Document known limitations
- [ ] `docs-supported-devices` -- Document supported premises/meters
- [ ] `docs-supported-functions` -- Document supported functions
- [ ] `docs-troubleshooting` -- Write troubleshooting guide
- [ ] `docs-use-cases` -- Document primary use cases
- [ ] `dynamic-devices` -- Handle premises added/removed in SP Group
- [ ] `entity-category` -- Set appropriate entity categories
- [ ] `entity-device-class` -- Verify device classes are correct
- [ ] `entity-disabled-by-default` -- Mark diagnostic entities as disabled by default
- [ ] `entity-translations` -- Verify all entity translations
- [ ] `exception-translations` -- Add translatable exception messages
- [ ] `icon-translations` -- Add icon translations if applicable
- [ ] `reconfiguration-flow` -- Add `async_step_reconfigure` for credential changes
- [ ] `repair-issues` -- Add repair flows for recoverable issues
- [ ] `stale-devices` -- Handle stale/removed premises

### Platinum (all todo)

- [ ] `async-dependency` -- Verify async-safe dependency handling
- [ ] `inject-websession` -- Properly inject HA websession into SDK clients
- [ ] `strict-typing` -- Achieve full strict mypy compliance

---

## Code Quality & Architecture

- [ ] **Split `config_flow.py`** -- Currently 1,424 lines / 54 KB.  Extract
  into separate modules: config flow, options flow, dev auth, form builders.

- [ ] **Split `coordinator.py`** -- Currently 773 lines / 26 KB.  Extract
  observation parsing helpers into `electricity.py` and `water.py`.

- [ ] **Consolidate duplicate helpers** -- `_parse_decimal`, `_as_string`,
  `_is_water_volume_unit` duplicated across `coordinator.py`,
  `electricity.py`, and `water.py`.

- [ ] **Review bare `except Exception` blocks** -- 11 instances across the
  codebase.  Narrow exception scope where possible.

- [ ] **Add retry strategy for transient SP API failures** -- Utility
  coordinators raise `UpdateFailed` on any error.  Add configurable retry
  with backoff for 5xx and timeout errors.

- [ ] **Deduplicate `strings.json` vs `translations/en.json`** -- Both files
  contain the same translation payload.  Remove or minimize `strings.json`.

---

## Test Coverage

- [ ] **Add tests for `backfill.py`** -- Backfill orchestration, retry logic,
  HA start event scheduling, and error/repair issue creation are untested.

- [ ] **Add tests for `response_schema.py`** -- Schema validation guards are
  untested.

- [ ] **Add tests for `polling_options.py`** -- Input normalization and
  validation are untested.

- [ ] **Add tests for `discovery_service.py`** -- Multi-API fetch orchestration
  is only indirectly covered through `test_discovery.py`.

- [ ] **Review `config_flow.py` edge-case coverage** -- Seed workflow, MFA
  paths, OOB, stale-flow detection, approximate seed confirmation paths
  need confidence verification.

- [ ] **Add `ledger.py` edge-case tests** -- Rebuild, malformed snapshot
  recovery, lower-value replacement blocking, legacy epoch-id migration.

- [ ] **Establish coverage targets** -- Set minimum coverage thresholds for
  SDK and HA integration code, integrate into CI.

---

## Distribution & Packaging

- [ ] **Add `hacs.json`** -- Minimal HACS manifest for HACS installation.

- [ ] **Resolve SDK vendoring or manifest requirements** -- Choose and
  implement one approach:
  - Option A: List SDK in manifest `requirements` as pip/git package
  - Option B: Vendor SDK directly into `custom_components/sp_group/`

- [ ] **Set up CI pipeline** -- GitHub Actions workflow for lint, type-check,
  tests, and packaging validation.

- [ ] **Define versioning strategy** -- Semantic versioning policy, changelog
  format, release process documentation.

---

## Missing Production Features

- [ ] **Add device registry entries** -- Register each discovered premise as
  a device in the HA device registry.

- [ ] **Set entity categories** -- Mark diagnostic/secondary sensors with
  `entity_category=EntityCategory.DIAGNOSTIC` where appropriate.

- [ ] **Add disabled-by-default entities** -- Future utility-specific sensors
  should be disabled by default.

- [ ] **Add `async_step_reconfigure`** -- Config flow step for changing
  credentials without re-adding the integration.

- [ ] **Handle stale devices** -- Implement stale-device detection for
  premises/meters that no longer appear in discovery data.

- [ ] **Handle dynamic device changes** -- Detect new premises/accounts
  added to the SP Group account and prompt for seed setup.

- [ ] **Define HA service/actions** -- Add action definitions for refresh,
  backfill, and reseed operations.

---

## Monitoring & Observability

- [ ] **Add logging when sensors become unavailable** -- Log at WARNING level
  when the coordinator fails and sensors transition to unavailable.

- [ ] **Set `parallel_updates`** -- Configure `parallel_updates` on
  `DataUpdateCoordinator` subclasses to prevent thundering-herd updates.

- [ ] **Audit diagnostics for session metadata** -- Remove or flag
  `has_session` and `data_keys` entries in diagnostics output.

---

## Documentation

- [ ] **Write installation instructions** -- HA/HACS installation guide for
  end users.
- [ ] **Write removal instructions** -- How to fully remove the integration
  and its stored data.
- [ ] **Write troubleshooting guide** -- Common issues, log locations,
  diagnostic steps.
- [ ] **Write configuration parameter docs** -- Document each config entry
  option and its effects.
- [ ] **Write installation parameter docs** -- Document any install-time
  parameters.
- [ ] **Write high-level description** -- What the integration does and how
  it works.
- [ ] **Document supported devices** -- Which SP Group meters/premises are
  supported.
- [ ] **Document known limitations** -- Monthly-only water, smart meter
  requirements for electricity, etc.
- [ ] **Document data update behaviour** -- Poll intervals, scheduling,
  backfill expectations.
- [ ] **Document use cases** -- Energy Dashboard integration, manual seed
  workflow, replacement meter handling.

---

## Planning Notes

Suggested order of operations:

1. **Fix the critical `__init__.py` bug first** -- the `runtime` NameError
   is a runtime crash (silently swallowed, but blocks the backfill-skip
   optimisation).
2. **Resolve SDK distribution** before any public release -- without this,
   no user can install the integration.
3. **Split `config_flow.py` and `coordinator.py`** before adding significant
   new code -- the current monolithic files will only grow.
4. **Tackle Bronze quality-scale rules** before Silver/Gold -- HA's quality
   model is designed to be climbed incrementally.
5. **Add HACS manifest and CI** early -- makes every subsequent change easier
   to validate.
6. **Session revocation and security hardening** before any public
   announcement -- users who remove the integration should not leave live
   tokens on the SP Group side.
