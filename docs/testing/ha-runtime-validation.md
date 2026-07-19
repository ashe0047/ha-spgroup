# Home Assistant Runtime Validation

This checklist covers the post-hermetic validation phase for the SP Group custom
integration. The goal is to prove the integration works inside a real Home
Assistant runtime without leaking secrets or corrupting long-term statistics.

## Prerequisites

- Run `mise run python:install`.
- Run hermetic checks first:
  - `python -m pytest tests/components/sp_group -vv`
  - `mise run python:test`
  - `mise run python:lint`
  - `mise run ha:check`
- For live checks, create or refresh `.runtime/mobile-session.json` using the SDK
  live setup flow in `the SDK repo's docs/api/live-e2e.md`.
- Live/runtime validation must remain read-only against SP Group endpoints.

## Manual HA UI Walkthrough

Use a disposable Home Assistant config directory and install the repo-local SDK
editable package before starting HA.

After translation changes, restart Home Assistant and hard-refresh the browser
before judging setup/options labels. Home Assistant and the frontend may cache
translation payloads across reloads.

Validate:

1. Add the SP Group integration through the Home Assistant UI.
2. Complete username/password and MFA setup. Confirm the email/password and OTP
   fields render as purpose-built inputs rather than generic text boxes.
3. Confirm discovered utility labels are user-friendly and do not expose raw
   account, premise, meter, IAM, email, phone, or Jarvis identifiers.
4. Select utilities from the multi-select list. If no utilities are selected,
   confirm setup asks for explicit discovery-only confirmation before creating
   the entry.
5. Enter seed values and timestamps for electricity and water. Confirm the seed
   reading uses a numeric field with the correct unit (`kWh` or `m³`) and the
   timestamp uses the Home Assistant date-time selector.
6. Try a non-boundary seed timestamp in a disposable entry. Confirm setup shows
   the approximate-seed confirmation and explains that accumulation starts at
   the next safe bucket boundary.
7. Confirm seeded sensors become available immediately with:
   - electricity: `kWh`, `energy`, `total_increasing`;
   - water: `m³`, `water`, `total_increasing`.
8. Open integration options and validate:
   - utility enablement uses the same stable multi-select model;
   - enabling a previously skipped utility;
   - safe reseed;
   - lower reset/replacement confirmation;
   - per-utility ledger rebuild confirmation;
   - only one maintenance action is selectable per options-flow run.
9. Reload the config entry and restart Home Assistant. Confirm sensor unique IDs
   and entity IDs remain stable, and trusted totals restore from storage.
10. Corrupt or replace the ledger storage only in a disposable test instance.
   Confirm a Repair issue appears and the repair flow rebuilds from stored seed
   config.
11. Download diagnostics and scan for secrets or raw identifiers.

## Recorder And Statistics Checks

The automated gate is:

```bash
python -m pytest tests/components/sp_group/test_recorder_statistics.py -vv
```

For a real HA runtime, additionally verify:

- seeded electricity and water sensors are visible as long-term-statistics
  sources;
- Energy/Water dashboard selection shows only seeded ledger-backed sensors;
- unseeded or malformed-ledger utilities are unavailable and not selectable;
- pending AMI buckets do not advance statistics;
- accepted bucket deltas advance totals monotonically;
- confirmed lower replacement is visible as an intentional reset/replacement
  event, not a silent historical rewrite.

## Opt-In Live HA Soak

Run only after hermetic tests pass:

```bash
mise run python:test:ha-live-soak
```

Optional environment:

- `SP_MOBILE_SESSION_FILE=.runtime/mobile-session.json`
- `SPGROUP_HA_LIVE_SOAK_CYCLES=2`

The live soak:

- uses the cached mobile session;
- sets up the HA integration runtime with live SDK clients;
- performs serial read-only coordinator refreshes;
- validates discovery, chart polling, entity creation, diagnostics, and
  sanitized runtime summaries;
- does not seed utilities, mutate SP Group account data, or duplicate the full
  SDK endpoint matrix.

## Evidence Format

Record only sanitized summaries:

```text
date:
HA version:
integration entry id:
cycle count:
premise count:
future sensor count:
electricity candidate count:
water candidate count:
ledger entry count:
warning code histogram:
auth refresh outcome:
coordinator outcome:
recorder/statistics outcome:
```

Never record or paste:

- access, ID, or refresh tokens;
- raw account, premise, meter, IAM, email, phone, address, owner, or Jarvis IDs;
- raw SDK payloads;
- screenshots that reveal addresses or account details.

## Pass Criteria

- Hermetic runtime, Recorder/statistics, privacy, lint, and compile checks pass.
- Manual UI setup/options/reseed/reset/rebuild flows behave as described.
- Recorder/statistics accepts only ledger-backed seeded utility sensors.
- Transient upstream failures keep the last trusted ledger total available.
- Diagnostics, logs, repairs, config-entry options, and ledger storage remain
  privacy-safe.
- Live HA soak either passes or records a sanitized, classified upstream/auth
  blocker.
