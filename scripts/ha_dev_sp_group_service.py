"""Call a development-only SP Group service in a running HA instance."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ha_dev_force_statistics import (
    DEFAULT_CONFIG_DIR,
    _access_token,
    _server_port,
)

DOMAIN = "sp_group"
SERVICES = {
    "prepare_poll_gap",
    "poll_now",
}
UTILITIES = {"electric", "water"}


def main() -> int:
    """Call one of the controlled SP Group development services."""
    import sys

    if len(sys.argv) not in (3, 4):
        raise SystemExit(
            "Usage: python scripts/ha_dev_sp_group_service.py "
            "<prepare_poll_gap|poll_now> <electric|water> [poll_end]"
        )
    action, utility = sys.argv[1:3]
    if action not in SERVICES or utility not in UTILITIES:
        raise SystemExit(f"Unsupported SP Group dev action: {action} {utility}")

    repo = Path(__file__).resolve().parents[1]
    config_dir = repo / DEFAULT_CONFIG_DIR
    port = _server_port(config_dir)
    token = _access_token(config_dir, port)
    service = f"dev_{action}"
    print(f"Calling {DOMAIN}.{service} with utility={utility}")
    data: dict[str, Any] = {"utility": utility}
    poll_end = sys.argv[3] if len(sys.argv) == 4 else os.environ.get(
        "SPGROUP_HA_DEV_POLL_END"
    )
    if action == "poll_now" and poll_end:
        data["poll_end"] = poll_end
    _call_service(port, token, service, data)
    print("Service completed successfully.")
    return 0


def _call_service(
    port: int,
    token: str,
    service: str,
    data: dict[str, Any],
) -> None:
    """Call one HA service and report the response body on failure."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/services/{DOMAIN}/{service}",
        data=json.dumps(data).encode(),
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
            body = response.read().decode(errors="replace")
            if body:
                print(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(
            f"Service call failed with HTTP {exc.code}: {detail}\n"
            "Start HA with the poll-gap mise task and complete the SP Group "
            "config flow before calling this helper."
        ) from exc


if __name__ == "__main__":
    raise SystemExit(main())
