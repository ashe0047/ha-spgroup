# HA Config Flow Stale Already In Progress — Closed

## Status

Closed on 2026-07-15.

## Observation

During Home Assistant frontend setup, the SP Group config flow could complete
username/password and OTP, then end on Home Assistant's raw:

```text
already_in_progress
```

This was confusing because the integration was not registered yet in the
Integrations dashboard, and the user had no reliable way to resume the hidden
in-progress setup dialog that was blocking the current visible setup flow.

## Root Cause

After authentication succeeds, the integration sets the Home Assistant config
flow unique ID from the Auth0 account subject:

```text
async_set_unique_id(account_id)
```

Home Assistant aborts this call with `already_in_progress` when another
in-progress flow for the same integration already has the same unique ID. This
can happen when a previous setup reached a post-auth step such as utility
selection or seed entry, then the frontend dialog was closed or abandoned before
the config entry was created.

Home Assistant keeps that old flow in memory, but it does not provide a reliable
frontend path for the user to resume it once the dialog is no longer visible and
no config entry exists yet.

## UX Decision

Any other same-account SP Group setup flow before config-entry creation is
treated as stale and reset-required.

There is no resume boundary for hidden setup flows. The current visible flow is
the only flow the user can reliably act on.

Locked behavior:

- do not auto-abort stale flows without user confirmation;
- show a dedicated reset confirmation step in the current visible flow;
- if the user confirms, abort the stale flow and continue the current setup;
- if the user declines, leave the stale flow untouched and abort only the
  current flow with a friendly reason;
- keep existing configured-entry and reauth behavior unchanged.

## Fix

Implemented in:

- `custom_components/sp_group/config_flow.py`
- `custom_components/sp_group/const.py`
- `custom_components/sp_group/strings.json`
- `tests/components/sp_group/test_config_flow.py`

The fix adds a new setup step:

```text
stale_flow_reset_confirm
```

When `async_set_unique_id(account_id)` raises `already_in_progress` during an
initial setup flow, the integration now:

1. Finds matching same-account SP Group setup flows in Home Assistant's flow
   manager.
2. Stores only transient post-auth state on the current flow.
3. Shows `stale_flow_reset_confirm`.
4. On confirmation, aborts the stale flow IDs and re-runs the normal setup
   continuation path.
5. On decline, aborts the current flow with `stale_flow_not_reset`.

No username, password, OTP, MFA token, OOB code, source config, or ledger state
is persisted during the stale-flow confirmation step.

## Regression Coverage

Added tests for:

- password-login duplicate in-progress setup routing to
  `stale_flow_reset_confirm`;
- OTP duplicate in-progress setup routing to `stale_flow_reset_confirm`;
- confirmed reset aborting the stale flow and continuing current setup to source
  selection;
- declined reset preserving the stale flow and aborting only the current flow;
- already configured entries still aborting as `already_configured`;
- existing reauth success and wrong-account behavior remaining unchanged.

## Verification

Hermetic checks:

```bash
mise exec -- python -m pytest tests/components/sp_group/test_config_flow.py -q
mise exec -- python -m pytest tests/components/sp_group -q
mise run python:test
mise run python:lint
mise run ha:check
```

Results at closure:

- `26 passed` for `test_config_flow.py`;
- `147 passed, 1 skipped` for `tests/components/sp_group`;
- `212 passed, 10 skipped` for `mise run python:test`;
- lint and type checks passed;
- HA compile/check passed.

## Operational Notes

If `already_in_progress` appears again in the frontend, first check whether it
is coming from this same-account setup branch or from a different Home Assistant
flow source. The intended user-facing behavior for initial setup is the SP
Group-owned `stale_flow_reset_confirm` prompt, not Home Assistant's raw abort
screen.
