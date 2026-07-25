"""Force local HA Recorder statistics for SP Group validation."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DOMAIN = "sp_group"
SERVICE = "dev_force_statistics_compile"
EXTERNAL_STATISTICS_STRATEGY = "external_provider_v3"
LEGACY_STATISTICS_STRATEGY = "native_offset_v2"
DEFAULT_CONFIG_DIR = Path(".runtime/ha-config")
WAIT_TIMEOUT_SECONDS = 30
WAIT_INTERVAL_SECONDS = 1


def main() -> int:
    """Call the SP Group dev Recorder helper in a running HA dev instance."""
    repo = Path(__file__).resolve().parents[1]
    config_dir = repo / DEFAULT_CONFIG_DIR
    port = _server_port(config_dir)
    token = _access_token(config_dir, port)
    print(f"Calling {DOMAIN}.{SERVICE} on http://127.0.0.1:{port}")
    print("This queues HA Recorder statistics; SP Group import is listener-driven.")
    _call_service(port, token)
    _wait_for_listener_import(config_dir)
    _print_evidence(config_dir)
    return 0


def _server_port(config_dir: Path) -> int:
    config = config_dir / "configuration.yaml"
    if not config.exists():
        return 8123
    match = re.search(r"(?m)^  server_port: (\\d+)\\s*$", config.read_text())
    return int(match.group(1)) if match else 8123


def _access_token(config_dir: Path, port: int) -> str:
    refresh = _refresh_token(config_dir)
    for host in ("127.0.0.1", "localhost"):
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": f"http://{host}:{port}/",
            }
        ).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/auth/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            return str(payload["access_token"])
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
        except urllib.error.URLError as exc:
            raise SystemExit(
                "Could not reach the HA dev instance. Start it with "
                "`mise run ha:dev:cached-session` first."
            ) from exc
    raise SystemExit(
        "Could not exchange the Test user's HA refresh token. "
        "Sign in again after a dev reset."
    )


def _refresh_token(config_dir: Path) -> str:
    auth_path = config_dir / ".storage" / "auth"
    if not auth_path.exists():
        raise SystemExit(
            "No HA auth storage found. Complete HA onboarding and sign in as "
            "the Test user first."
        )
    data = json.loads(auth_path.read_text())["data"]
    users = {user["id"]: user for user in data["users"]}
    for token in data["refresh_tokens"]:
        user = users.get(token["user_id"], {})
        if user.get("name") == "Test" and token.get("token_type") == "normal":
            return str(token["token"])
    raise SystemExit(
        "No refresh token found for HA user `Test`. Sign in to the HA frontend "
        "with test/test123, then retry this command."
    )


def _call_service(port: int, token: str) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/services/{DOMAIN}/{SERVICE}",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            if response.status != 200:
                raise SystemExit(f"Service returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(
            f"Service call failed with HTTP {exc.code}: {detail}\n"
            "Make sure HA was started through `mise run ha:dev` or "
            "`mise run ha:dev:cached-session`, then complete the SP Group "
            "config flow before running this command."
        ) from exc


def _print_evidence(config_dir: Path) -> None:
    _print_statistics_rows(config_dir)
    _print_ledger_status(config_dir)
    log_path = config_dir / "home-assistant.log"
    if log_path.exists():
        print("\nRecent SP Group statistics logs:")
        lines = [
            line.rstrip()
            for line in log_path.read_text(errors="replace").splitlines()
            if "SP Group statistics" in line or "SP Group dev statistics" in line
        ]
        for line in lines[-20:]:
            print(line)


def _wait_for_listener_import(config_dir: Path) -> None:
    print(
        "Waiting for listener-driven SP Group statistics import "
        f"(up to {WAIT_TIMEOUT_SECONDS}s)..."
    )
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = _ledger_import_status(config_dir)
        if status == "complete":
            return
        if status == "not_configured":
            return
        time.sleep(WAIT_INTERVAL_SECONDS)
    print("Timed out waiting; printing current Recorder and ledger evidence.")


def _ledger_import_status(config_dir: Path) -> str:
    ledger_paths = sorted((config_dir / ".storage").glob("sp_group_*_ledger"))
    if not ledger_paths:
        return "not_configured"
    saw_source_attempt = False
    for ledger_path in ledger_paths:
        payload: dict[str, Any] = json.loads(ledger_path.read_text())
        entries = payload.get("data", payload).get("entries", {})
        for entry in entries.values():
            if entry.get("source_backfill_attempted_at") is None:
                continue
            saw_source_attempt = True
            strategy = entry.get("statistics_strategy_version")
            if entry.get("statistics_dirty_from") is not None:
                return "pending"
            if strategy == EXTERNAL_STATISTICS_STRATEGY:
                if (
                    entry.get("external_migration_status") == "complete"
                    and not entry.get("accepted")
                ):
                    continue
                if entry.get("statistics_imported_through") is None:
                    return "pending"
                continue
            if entry.get("statistics_imported_through") is None:
                return "pending"
            if (
                strategy != LEGACY_STATISTICS_STRATEGY
                or entry.get("statistics_reference_start") is None
            ):
                return "pending"
    return "complete" if saw_source_attempt else "pending"


def _print_statistics_rows(config_dir: Path) -> None:
    db_path = config_dir / "home-assistant_v2.db"
    if not db_path.exists():
        print("Recorder database not found yet.")
        return
    print("\nRecorder long-term statistics rows:")
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            SELECT
                m.statistic_id,
                COUNT(*),
                MIN(datetime(s.start_ts, 'unixepoch')),
                MAX(datetime(s.start_ts, 'unixepoch'))
            FROM statistics s
            JOIN statistics_meta m ON m.id = s.metadata_id
            WHERE m.statistic_id IN (
                'sensor.electricity_consumption',
                'sensor.water_consumption'
            ) OR m.statistic_id LIKE 'sp_group:%'
            GROUP BY m.statistic_id
            ORDER BY m.statistic_id
            """
        ).fetchall()
    if not rows:
        print("No SP Group long-term statistics rows found yet.")
        return
    for statistic_id, count, first, last in rows:
        print(f"- {statistic_id}: rows={count} first={first} last={last}")


def _print_ledger_status(config_dir: Path) -> None:
    print("\nSP Group ledger statistics status:")
    ledger_paths = sorted((config_dir / ".storage").glob("sp_group_*_ledger"))
    if not ledger_paths:
        print("No SP Group ledger file found yet.")
        return
    for ledger_path in ledger_paths:
        payload: dict[str, Any] = json.loads(ledger_path.read_text())
        entries = payload.get("data", payload).get("entries", {})
        for sensor_key, entry in entries.items():
            print(
                "- "
                f"{sensor_key}: "
                f"imported_through={entry.get('statistics_imported_through')} "
                f"reference={entry.get('statistics_reference_start')} "
                f"strategy={entry.get('statistics_strategy_version')} "
                f"warnings={entry.get('statistics_warnings')}"
            )


if __name__ == "__main__":
    raise SystemExit(main())
