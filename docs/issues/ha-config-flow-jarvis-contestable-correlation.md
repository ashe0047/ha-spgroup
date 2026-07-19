# HA Config Flow Jarvis Contestable Correlation — Closed

## Status

Closed on 2026-07-15.

## Observation

During Home Assistant frontend setup, the SP Group config flow accepted the
username/password step and completed OTP, but then failed while setting up the
integration.

The failure was not an Auth0 or MFA problem. Authentication completed far enough
for the integration to start live discovery, then strict discovery validation
raised:

```text
DiscoveryDataError: Jarvis discovery did not match all Yggdrasil utility accounts
```

## Root Cause

The discovery mapper compared Yggdrasil account numbers and Jarvis account
numbers literally, and only indexed Jarvis accounts from:

```text
premises[].accounts[]
```

Live payloads showed two additional shapes that the mapper did not support:

- Jarvis pads numeric account identifiers with leading zeroes while Yggdrasil
  exposes the same account without those leading zeroes.
- Contestable retailer electricity is exposed under premise-level
  `contestable_details`, not under the normal Jarvis `accounts` list.

This meant a valid electricity account supplied by a third-party retailer was
misclassified as an unmatched account. Because config flow requires strict
Jarvis correlation before seed setup, the whole setup failed after OTP.

## Fix

Implemented in:

- `custom_components/sp_group/discovery.py`
- `tests/components/sp_group/test_discovery.py`

The fix does three things:

1. Account-number correlation now indexes both raw and normalized all-digit
   account numbers. Normalization strips leading zeroes for matching only.
2. Jarvis premise-level `contestable_details` is indexed as a supported
   electricity correlation source.
3. Utility labels are canonicalized at the discovery boundary so live
   `electricity` and `electric` both map to the integration's existing internal
   `ELECTRIC` / `electric` convention.

Opaque HA entity/source keys remain derived from Yggdrasil identifiers, so this
matching normalization does not change stable unique IDs.

## Regression Coverage

Added tests for:

- padded Jarvis account numbers matching unpadded Yggdrasil account numbers;
- contestable retailer electricity matching through `contestable_details`;
- strict `require_jarvis_correlation=True` still rejecting genuinely unmatched
  Jarvis/Yggdrasil account data.

## Verification

Hermetic checks:

```bash
mise exec -- python -m pytest tests/components/sp_group -q
mise run python:test
mise run python:lint
mise run ha:check
```

Results at closure:

- `135 passed, 1 skipped` for `tests/components/sp_group`
- `200 passed, 10 skipped` for `mise run python:test`
- lint passed
- HA compile/check passed

Live validation with the cached mobile session confirmed strict discovery now
builds successfully with:

- one discovered premise;
- two utility accounts;
- both accounts correlated as `matched`;
- future sensor keys for water and electricity.

The user also confirmed the frontend setup progressed past the original error.

## Operational Notes

If this class of failure appears again, inspect live Yggdrasil and Jarvis
payloads for account-shape drift before weakening strict correlation. Retailer
or contestable electricity should be treated as a supported classification, not
as an unsupported utility type, as long as the Jarvis payload still provides a
correlatable account and premise identifier.
