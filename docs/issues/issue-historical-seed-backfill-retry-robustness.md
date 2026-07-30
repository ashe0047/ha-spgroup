# Historical Seed Backfill Retry Robustness

## Status

Implemented and superseded by the provider-failure boundary. Utility provider
outages now produce a no-op coordinator result, while the source-backfill
orchestrator records the attempt as failed and retains the existing repair/
retry flow. The original investigation below is retained as historical
context.

## Authority

Historical/implemented issue record; current behavior is governed by the electricity gap ADR.

## Issue

The source-history backfill historically had no retry mechanism for transient
API failures. The implemented provider-failure boundary now leaves the
utility coordinator available without ledger mutation, while the source
backfill records the attempt as failed and retains the existing
`backfill_failed` repair/retry flow.

## Background

The SP Group AMI charts endpoint (`/jarvis/v3/ami/charts`) has exhibited
intermittent HTTP 500 errors during the initial backfill. These errors are
transient — retrying the same request seconds later succeeds. The water
backfill uses a different endpoint (`/jarvis/v4/charts/{premise_id}`) and
has not exhibited the same issue.

The electricity backfill uses `poll_count=2` to satisfy the two-sighting
rule. The name `_run_backfill_with_retry` is misleading — the "retry" refers
to the two confirmation polls, not to recovering from transient failures.

## Current Flow

```
_run_backfill_with_retry()
  └─ _run_utility_source_backfill("electric", poll_count=2)
      └─ mark source_backfill_status = "running"
      └─ for attempt in 1..2:
           └─ await poll(backfill=True)
               └─ API call → HTTP 500
               └─ SpGroupMobileError → wrapped as UpdateFailed
           └─ caught at line 430:
               mark source_backfill_status = "failed"
               return False
      └─ (attempt 2/2 is NEVER reached because the first attempt failed)
  └─ if any_utility_failed:
       create "backfill_failed" repair issue
       return
  └─ (statistics import is NOT reached)
```

Recovery requires the user to discover the repair issue and submit
`retry_source_backfill` from the options flow manually.

## Current State Machine

```
never_attempted  ──→  running  ──→  succeeded
                       │  │
                       │  └──→  failed  (transient error)
                       │
                       └──→  failed  (interrupted via restart migration)
```

Retryable statuses: `{never_attempted, failed}`

## Proposed Design

### 1. Distinguish transient vs permanent errors

Not all failures should be retried. The backfill should classify errors:

| Error Type | Examples | Retry? |
|------------|----------|--------|
| Transient | HTTP 5xx, timeout, connection reset | Yes |
| Auth | HTTP 401, session expired | No — create repair |
| Invalid | HTTP 400, malformed response | No — create repair |

The `SpGroupMobileError` from the jarvis SDK can carry the HTTP status
code via `SpGroupApiError`. The transport layer raises `SpGroupTransportError`
for connection failures. Both inherit from `SpGroupMobileError`.

### 2. Add exponential backoff retry for transient errors

Inside `_run_utility_source_backfill`, wrap the poll loop with a retry
wrapper for transient failures:

```python
_MAX_BACKFILL_RETRIES = 3
_BACKFILL_RETRY_DELAY_SECONDS = [5, 15, 45]  # exponential backoff

for poll_index in range(poll_count):
    last_error = None
    for retry in range(_MAX_BACKFILL_RETRIES + 1):
        try:
            await poll(backfill=True)
            break  # success, move to next poll
        except UpdateFailed as err:
            if _is_transient(err) and retry < _MAX_BACKFILL_RETRIES:
                last_error = err
                LOGGER.warning(
                    "SP Group %s source backfill poll %d/%d transient error "
                    "(attempt %d/%d), retrying in %ds: %s",
                    utility_type, poll_index + 1, poll_count,
                    retry + 1, _MAX_BACKFILL_RETRIES + 1,
                    _BACKFILL_RETRY_DELAY_SECONDS[retry], err,
                )
                await asyncio.sleep(_BACKFILL_RETRY_DELAY_SECONDS[retry])
                continue
            raise  # exhausted retries → let the outer handler mark failed
```

### 3. Transient error detection

```python
def _is_transient(err: UpdateFailed) -> bool:
    """Return True if the underlying error is likely transient."""
    # Unwrap the UpdateFailed to find the original SpGroupMobileError
    cause = err.__cause__
    if isinstance(cause, SpGroupApiError):
        return 500 <= cause.status < 600  # server errors
    if isinstance(cause, SpGroupTransportError):
        return True  # connection errors are transient
    if isinstance(cause, TimeoutError):
        return True
    return False
```

Note: `SpGroupApiError` currently stores the status code in its message
string rather than a structured field. The jarvis SDK at
`vendor/spgroup/mobile/jarvis.py` constructs it as:

```python
raise SpGroupApiError(f"{context} failed: HTTP {response.status}")
```

The transport raises `SpGroupTransportError` for connection-level failures.

### 4. Idempotency guard

The retry loop must not create duplicate accepted buckets. The existing
two-sighting rule already provides idempotency: if a bucket was accepted
in a previous (partially successful) attempt, the retry will find it in
`accepted` (via `accepted_by_key`) and skip it. If it was in `pending`,
the retry will match it and promote to `accepted`.

### 5. Retry boundary

Retries should be PER-POLL, not per-utility. If poll 1/2 succeeds but poll
2/2 fails, the retry should attempt poll 2/2 again without refetching poll
1/2's data. The `poll_count` loop naturally handles this.

### 6. Survivability across restart

If HA restarts during a backfill with active retries:
- The `source_backfill_status = "running"` is persisted in the ledger
- On setup, `running` is migrated to `failed` (existing logic)
- `_retryable_source_sensor_keys` picks up `failed` entries
- The retryable backfill runs on restart

This means retries don't need to survive restarts — the normal restart
recovery handles it.

### 7. Water backfill

Water uses `poll_count=1` (no two-sighting rule) and a different API
endpoint. The same retry wrapper applies to water as well, providing
consistent behavior across utilities.

### 8. Repair issue timing

The `backfill_failed` repair issue should ONLY be created AFTER all retry
attempts are exhausted for all utilities. A transient error that resolves
on the first retry should not produce a repair issue at all.

## Implementation Summary

| Change | File | Description |
|--------|------|-------------|
| Add `_is_transient()` | `backfill.py` | Classify UpdateFailed cause as transient or permanent |
| Add retry wrapper | `backfill.py` | Per-poll retry loop with exponential backoff |
| Update logging | `backfill.py` | Log retry attempts and delays |
| No change needed | `ledger.py` | Existing status machine supports retry via `failed → running` |
| No change needed | `config_flow.py` | Existing `retry_source_backfill` action remains as fallback |

## Out of Scope

- Changing the jarvis SDK to expose structured status codes
- Adding a dedicated retry background task separate from the backfill flow
- Persisting retry state across restarts (handled by the existing
  `running → failed` migration)

## Status

The original exponential-backoff proposal is superseded and is not part of
the current implementation.
