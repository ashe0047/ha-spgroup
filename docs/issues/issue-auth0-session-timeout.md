# Auth0 Session Timeout — Unverified Deduction

## Status

Historical, unverified observation; not a runtime contract.

## Authority

Informational only. Confirm behavior against the auth ADR and provider SDK before acting on it.

## Observation

When logged into the SP Group web portal (`services.spservices.sg`) via a regular
browser, the app displays a "you will be logged out roughly one hour later"
message. After approximately one hour, the portal shows a login screen, and the
user **must re-enter credentials + OTP** to log back in — there is no silent
re-authentication.

## Cookie data from persisted Playwright profile

| Cookie | Domain | Days remaining (at time of check) |
|---|---|---|
| `auth0` | `identity.spdigital.auth0.com` | 3 days |
| `auth0_compat` | `identity.spdigital.auth0.com` | 3 days |
| `is.authenticated` | `services.spservices.sg` | 1 day |
| `_csrf` | `identity.spdigital.auth0.com` | 10 days |
| `did` / `did_compat` | `identity.spdigital.auth0.com` | 365 days |

## Current assumption (unverified)

The Auth0 **server-side session** has a **1-hour inactivity timeout**, even
though the `auth0` cookie on disk has a 3-day `Max-Age`. When the portal logs
the user out (or when the access token expires), it likely calls Auth0's
`/v2/logout` endpoint, which destroys the server-side session. After that,
`prompt=none` silent auth returns `login_required` because Auth0's server has
no record of the session, regardless of whether the cookie file is still on
disk.

If this assumption is correct:

- The 3-day cookie `Max-Age` is irrelevant — the server-side session expires
  after 1 hour of inactivity regardless.
- The keepalive **must** refresh within the 1-hour window or the session is
  permanently lost, requiring a full `pnpm setup:login`.
- The current 55-minute refresh interval (`access token expiry − 5 min skew`)
  is the correct default, because it stays within the presumed inactivity
  window.

## Alternative explanation (also unverified)

The 1-hour "logged out" could be based purely on the access token lifetime
(3600s). The portal's Auth0 SDK might check the access token, find it expired,
and show a login screen. But Auth0's server-side session could still be valid,
and clicking "login" should trigger silent auth (no credentials needed). The
fact that credentials + OTP are required suggests the server-side session is
also being killed.

## How to verify

1. Start the keepalive with a fixed 55-minute interval.
2. Let it run for 3+ days without intervention.
3. Check whether the web session survives beyond 3 days.
   - If it does: the 3-day cookie is the real limit, not 1 hour.
   - If it fails at the same time every cycle and requires `setup:login`:
     the 1-hour server-side session timeout is correct.
4. Alternatively, manually call Auth0's `/authorize?prompt=none` via the
   Playwright profile at various times after login (1h, 2h, 6h, 12h, 24h, 3d)
   and check when it switches from returning a new token to returning
   `login_required`.

## Impact on keepalive configuration

- **`keepaliveRefreshSkewSeconds`** (default 300 / 5 min before expiry):
  Produces ~55 min cycles for a 1-hour token. If the 1-hour server-side
  timeout is real, this is the correct interval. If it's actually 3 days, this
  is overly aggressive and could be relaxed to e.g. 6 hours.

- **`keepaliveFallbackIntervalSeconds`** (default 900 / 15 min):
  Determines how quickly a failed auth is retried. Unrelated to this issue.

- **`keepaliveMinIntervalSeconds`** (default 60):
  Safety floor. Unrelated.

## Cross-reference

- `src/services/keepalive-service.ts` — the `nextDelayMs()` function that
  determines the refresh schedule.
- `src/browser/browser-session.ts` — `acquireTokenWithBrowser()` which opens
  Playwright and captures the token.
- `docs/context-tracker.md` — general implementation history.

## Decision (based on this unverified deduction)

**Chosen renewal interval:** Access token expiry − 5 minutes (~55 minutes).

**Justification:**

Until the assumption is verified, we choose the **conservative** option:
refresh within the 1-hour window. If the 1-hour server-side session timeout
is real, a longer interval (e.g. 6 hours) would cause the session to expire
after the first cycle, requiring a full `pnpm setup:login` every cycle.
That would effectively break the keepalive.

If the 1-hour timeout turns out to be wrong and the 3-day cookie is the real
limit, the consequence of staying at 55-minute cycles is only moderate:
- ~1 Playwright launch per hour instead of once per 6 hours
- Marginal extra load on the SP API (~24 calls/day instead of ~4)
- No functional degradation

The risk/reward balance favours the shorter interval until verified.

**Decision: KEEP the current defaults.**

| Config | Default | Effective cycle |
|---|---|---|
| `SP_KEEPALIVE_REFRESH_SKEW_SECONDS` | 300 (5 min) | token expiry − 5 min ≈ 55 min |
| `SP_KEEPALIVE_FALLBACK_INTERVAL_SECONDS` | 900 (15 min) | 15 min after failure |
| `SP_KEEPALIVE_MIN_INTERVAL_SECONDS` | 60 (1 min) | Hard floor |

**Verification plan:**

Run the keepalive for 3+ consecutive days with the 55-minute interval. If it
never requires a `pnpm setup:login` during that period, the 1-hour server-side
timeout is incorrect and the interval can be relaxed. If it does fail, the
1-hour timeout is confirmed and the current interval is correct.

To verify more quickly, run with a longer interval (e.g. 6h) and observe
whether silent auth fails after the first cycle:
```bash
SP_KEEPALIVE_REFRESH_SKEW_SECONDS=3600 SP_KEEPALIVE_MIN_INTERVAL_SECONDS=3600 pnpm dev
```

If the session dies within the first 6 hours → 1-hour timeout confirmed.
If it survives multiple 6-hour cycles → 3-day cookie is the real limit.
