"""Find the latest usable electricity AMI bucket for controlled dev testing."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from spgroup.mobile import JarvisClient, MobileSession

SINGAPORE_TZ = ZoneInfo("Asia/Singapore")
SESSION_FILE = Path(".runtime/mobile-session.json")
LOOKBACK_DAYS = 7


def main() -> int:
    """Print provider-backed seed and poll-end timestamps."""
    repo = Path(__file__).resolve().parents[1]
    session_path = repo / SESSION_FILE
    if not session_path.exists():
        raise SystemExit(
            f"Missing cached session: {session_path}. "
            "Refresh the dev mobile session first."
        )
    session = MobileSession.from_dict(json.loads(session_path.read_text()))
    results = asyncio.run(_find_latest(session))
    if not results:
        raise SystemExit("No positive electricity AMI bucket found in the lookback.")

    print("Latest usable electricity AMI buckets:")
    for premise_id, bucket in sorted(results.items()):
        # The coordinator excludes rows where bucket.end >= poll_end. Add one
        # extra bucket so the latest positive row is included while the next
        # provider-lagged zero row remains excluded.
        poll_end = bucket["start"] + timedelta(minutes=60)
        # Start at the preceding whole-hour boundary so the projection contains
        # complete hourly rows rather than only a partial seed hour.
        seed = bucket["start"].replace(
            minute=0,
            second=0,
            microsecond=0,
        ) - timedelta(hours=1)
        print(f"- premise={premise_id}")
        print(f"  bucket_start={bucket['start'].isoformat()}")
        print(f"  value_kwh={bucket['value']}")
        print(f"  recommended_seed_observed_at={seed.isoformat()}")
        print(f"  recommended_backfill_from={seed.isoformat()}")
        print(f"  recommended_poll_end={poll_end.isoformat()}")
    return 0


async def _find_latest(session: MobileSession) -> dict[str, dict[str, Any]]:
    """Find the latest positive bucket for every candidate Jarvis premise."""
    async with aiohttp.ClientSession() as http:
        client = JarvisClient(http)
        me = await client.get_me(session)
        candidates = _numeric_id_candidates(me)
        results: dict[str, dict[str, Any]] = {}
        today = datetime.now(SINGAPORE_TZ).date()
        for premise_id in candidates:
            latest: dict[str, Any] | None = None
            for offset in range(LOOKBACK_DAYS):
                day = today - timedelta(days=offset)
                try:
                    payload = await client.get_ami_charts(
                        session,
                        premise_id,
                        day,
                        day,
                        "day",
                        "electric",
                    )
                except Exception as err:  # noqa: BLE001
                    if offset == LOOKBACK_DAYS - 1:
                        print(
                            f"Skipping premise {premise_id}: "
                            f"{type(err).__name__}",
                            file=sys.stderr,
                        )
                    continue
                for row in _ami_rows(payload):
                    bucket = _positive_bucket(row)
                    if bucket is None:
                        continue
                    if latest is None or bucket["start"] > latest["start"]:
                        latest = bucket
            if latest is not None:
                results[premise_id] = latest
        return results


def _numeric_id_candidates(payload: Any) -> tuple[str, ...]:
    """Return likely numeric Jarvis premise identifiers without exposing payloads."""
    candidates: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    isinstance(child, str)
                    and child.isdigit()
                    and key.lower() in {"id", "premiseid", "premise_id"}
                ):
                    candidates.add(child)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return tuple(sorted(candidates))


def _ami_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract AMI rows from the public chart response shape."""
    rows: list[dict[str, Any]] = []
    history = payload.get("history")
    if not isinstance(history, list):
        return rows
    for group in history:
        if not isinstance(group, dict) or not isinstance(group.get("data"), list):
            continue
        rows.extend(row for row in group["data"] if isinstance(row, dict))
    return rows


def _positive_bucket(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return a positive, timestamped AMI bucket."""
    value = row.get("consumption", row.get("amount"))
    try:
        value_decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if value_decimal <= 0 or not isinstance(row.get("date"), str):
        return None
    try:
        start = datetime.fromisoformat(row["date"])
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=SINGAPORE_TZ)
    return {"start": start.astimezone(SINGAPORE_TZ), "value": value_decimal}


if __name__ == "__main__":
    raise SystemExit(main())
