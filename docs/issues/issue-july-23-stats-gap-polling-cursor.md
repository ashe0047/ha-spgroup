# July 23 Statistics Gap: 04:00-13:00 SGT

## Status

Historical incident — resolved by exact cursor semantics; retained for evidence.

## Authority

Informational only; current cursor behavior is governed by `adr/adr-polling-window-exact-end.md`.

## Summary

A 9-hour gap exists in electricity statistics on July 23, 2026 (04:00-13:00 SGT). The root cause is the old `max(seed_observation_time, accepted_end)` cursor code which set the polling window start to 12:30PM instead of 04:00AM. Although the API returns the full day's 48 data rows regardless of the polling window, the downstream parser filter `start >= backfill_from` discarded observations before 12:30PM. By the time the cursor code was fixed, the cursor had already advanced past the gap, making it unrecoverable by normal polling.

## Investigation Analysis

### Step 1: Observing the gap

The user noticed that electricity statistics on July 23 showed no data between 04:00 and 13:00 SGT despite non-zero consumption being expected for those hours.

### Step 2: Initial hypothesis -- cursor max() was wrong

The old `source_poll_cursor_start()` contained `max(seed_observation_time, accepted_end)`, which compared the seed observation time (12:30PM) against the last accepted bucket end (04:00AM from backfill). Since 12:30PM > 04:00AM, the function returned 12:30PM as the polling window start.

Evidence from logs (old code running):

```
Every normal poll: start=2026-07-23T12:30:00+08:00
```

If the cursor had been 04:00AM, the parser would have included observations from 04:00 onwards. But 12:30PM excluded everything before it.

### Step 3: Counter-evidence -- API returns full day data

The API call uses `_date_for_api(request_start)` which converts to a date (July 23), not a time. So even with `request_start=12:30PM`, the API is called for the full date 2026-07-23 to 2026-07-23, returning all 48 thirty-minute rows for the day.

This meant the API DID return data for 04:00-12:30 in every poll. The initial theory that the cursor being at 12:30PM prevented the API from returning earlier data was disproven.

### Step 4: Tracing the actual filter -- parser start >= backfill_from

The upstream chain was traced:

```
_request_start() returns cursor (12:30PM)
    |
    v
request_start = 12:30PM
    |
    +-- _date_for_api(12:30PM) = date(July 23)
    |     API returns FULL 48 rows for July 23
    |     (data from 04:00-12:30 IS in the API response)
    |
    +-- backfill_from=request_start=12:30PM
          |
          v
        _electricity_ami_source_poll(payload, backfill_from=12:30PM)
          |
          v
        PARSER FILTER [coordinator.py:1014-1019]:
          for start, value in rows         <- ALL 48 rows from API
          if value > 0
          and start >= backfill_from        <- start >= 12:30PM EXCLUDES rows before 12:30PM
          and start + 30min <= now
```

The critical filter is `start >= backfill_from`. Since `backfill_from = 12:30PM` (from the old cursor), all rows from 04:00-12:30 were discarded even though they existed in the API response.

### Step 5: Verifying the gap in persisted ledger

From the HA Store ledger file:

```
Backfill buckets on July 23 (8 buckets, up to 04:00):
  00:00-00:30, 00:30-01:00, 01:00-01:30, 01:30-02:00
  02:00-02:30, 02:30-03:00, 03:00-03:30, 03:30-04:00

Normal-poll buckets on July 23 (7 buckets, from 12:30):
  12:30-13:00, 13:00-13:30, 13:30-14:00, 14:00-14:30
  14:30-15:00, 15:00-15:30, 15:30-16:00

Gap: 04:00-12:30 -- NO accepted buckets
```

Backfill max bucket end: `2026-07-23T04:00:00+08:00`
Normal-poll max bucket end: `2026-07-23T16:00:00+08:00`

### Step 6: Verifying the gap in Recorder statistics

From the HA Recorder SQLite database (statistics table):

```
July 23 SGT electric statistics:
  00:00 SGT  sum=1085.6894
  01:00 SGT  sum=1087.8214
  02:00 SGT  sum=1089.2864
  03:00 SGT  sum=1090.6434
  ---- GAP: 04:00 to 12:00 SGT ----
  13:00 SGT  sum=1091.0544
  14:00 SGT  sum=1091.4604
  15:00 SGT  sum=1091.8894
```

A clean 9-hour gap in the statistics, matching the ledger gap.

### Step 7: Why the gap cannot be filled by the cursor fix alone

After the cursor code was fixed (removing `max(seed_observation_time, accepted_end)`), the current `source_poll_cursor_start()` correctly returns `max(bucket.end)`.

The current ledger state after normal polls ran under the old code:

```
Total accepted buckets: 2387
  Backfill: 2380 (max end: 04:00 SGT)
  Normal polls: 7 (max end: 16:00 SGT)

Global max accepted bucket end: 16:00 SGT (4PM)
```

So even with the fixed cursor code, `source_poll_cursor_start()` returns 16:00 (4PM) -- the last accepted bucket end from normal polls. The gap from 04:00-12:30 is entirely past the cursor.

The cursor only moves forward. It never re-examines data before it. Subsequent polls filter observations with `start >= 16:00`, so the gap data will never be seen.

## Data Flow Diagram

```
Provider API (returns full day data)
    |
    v
_ami_rows() extracts all 48 rows
    |
    v
PARSER FILTER: start >= backfill_from (12:30PM old / 16:00 current)
    |
    +-- rows before cursor -> DISCARDED
    |     (04:00-12:30 gap under old code)
    |
    +-- rows after cursor -> observations -> pending -> accepted -> stats import
```

## Resolution

The cursor fix (`source_poll_cursor_start()` returning `accepted_end` without the `max(seed_observation_time, ...)` comparison) prevents this issue from recurring in future polls. After restart with the fix:

- The cursor correctly starts from the last accepted bucket end
- Data lag gaps will be naturally filled in subsequent polls because the cursor now points to the actual last data boundary, not the seed time
- Future provider data with timestamps between the last accepted bucket and the poll time will be captured through normal polling

However, the **existing 04:00-13:00 SGT gap on July 23 is permanent** under normal polling. The cursor has advanced past it (to 16:00), and normal polling never moves backwards.

### Recovery options (not implemented)

- Explicit reseed/rebuild via options flow
- Targeted recovery action that temporarily rolls back the cursor and re-imports the window
- Repair-driven backfill after detecting the gap through statistics read-back

## Key Files Referenced

- coordinator.py:1014-1019 -- parser filter `start >= backfill_from`
- coordinator.py:408-424 -- electricity `_request_start` (fixed cursor logic)
- ledger.py:563-575 -- `source_poll_cursor_start()` (fixed max() logic)
- HA Store: `.runtime/ha-config/.storage/sp_group_*_ledger`
- HA Recorder: `.runtime/ha-config/home-assistant_v2.db`
