"""Prepare and run a local Home Assistant dev instance for SP Group."""

from __future__ import annotations

import os
import json
import shutil
import signal
import socket
import secrets
import subprocess
import sys
import uuid
from contextlib import suppress
from pathlib import Path
from types import FrameType
from datetime import datetime, timezone

SHUTDOWN_TIMEOUT = 60
TERMINATE_TIMEOUT = 10

def _resolve_python(repo: Path) -> str:
    """Return the Python executable to use for HA.

    Prefer .venv/bin/python if it exists (allows HA to detect the virtual
    environment and suppress the unsupported_local_deps warning).
    Falls back to sys.executable otherwise.
    """
    venv_python = repo / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable



REQUIRED_PYTHON = (3, 14)
REQUIRED_HA_VERSION = (2026, 3)


def _check_environment() -> str | None:
    """Return an error message if Python or HA version is too old, else None."""
    if sys.version_info[:2] < REQUIRED_PYTHON:
        py_str = ".".join(map(str, REQUIRED_PYTHON))
        return (
            f"SP Group dev requires Python {py_str}+, got {sys.version}. "
            f"Run via `mise run ha:dev` (or `mise run ha:dev:reset`) "
            f"to use the correct Python environment."
        )

    try:
        import homeassistant.const as hac  # noqa: PLC0415
        ha_version = (hac.MAJOR_VERSION, hac.MINOR_VERSION)
    except ImportError:
        return (
            "Home Assistant is not installed in this Python environment "
            f"({sys.executable}). Run `mise run python:install` first."
        )
    except AttributeError:
        return (
            "Could not determine Home Assistant version. "
            f"The homeassistant package at {Path(hac.__file__).resolve().parent} "
            "may be damaged. Run `mise run python:install`."
        )

    if ha_version < REQUIRED_HA_VERSION:
        ha_str = ".".join(map(str, REQUIRED_HA_VERSION))
        current_str = ".".join(map(str, ha_version))
        return (
            f"SP Group dev requires Home Assistant {ha_str}+, got {current_str}. "
            f"Installed at: {Path(hac.__file__).resolve().parent}. "
            f"Run `mise run python:install` to upgrade."
        )

    return None





def _write_auto_onboarding(config_dir: Path, port: int) -> None:
    """Write storage files so HA starts fully onboarded with a pre-configured owner.

    Creates the auth system, core config, person, areas, defaults, and HTTP
    setup that the interactive onboarding flow would normally produce.  Called
    only when the config directory is freshly created (first run or after reset).
    """
    import base64

    import bcrypt

    storage_dir = config_dir / ".storage"
    storage_dir.mkdir(parents=True, exist_ok=True)

    user_id = uuid.uuid4().hex[:32]
    content_user_id = uuid.uuid4().hex[:32]
    credential_id = uuid.uuid4().hex[:32]
    now = datetime.now(timezone.utc).isoformat()

    # --- Onboarding: all 4 steps done
    (storage_dir / "onboarding").write_text(
        json.dumps(
            {
                "version": 4,
                "minor_version": 1,
                "key": "onboarding",
                "data": {
                    "done": ["user", "core_config", "analytics", "integration"]
                },
            }
        )
    )

    # --- Auth provider: bcrypt-hashed password for test / test123
    password_hash: bytes = bcrypt.hashpw(
        b"test123", bcrypt.gensalt(rounds=12)
    )
    stored_password: str = base64.b64encode(password_hash).decode()
    (storage_dir / "auth_provider.homeassistant").write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 1,
                "key": "auth_provider.homeassistant",
                "data": {
                    "users": [{"username": "test", "password": stored_password}]
                },
            }
        )
    )

    # --- Auth: users, groups, credentials
    system_token = secrets.token_hex(64)
    system_jwt_key = secrets.token_hex(64)
    (storage_dir / "auth").write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 1,
                "key": "auth",
                "data": {
                    "users": [
                        {
                            "id": content_user_id,
                            "group_ids": ["system-read-only"],
                            "is_owner": False,
                            "is_active": True,
                            "name": "Home Assistant Content",
                            "system_generated": True,
                            "local_only": False,
                        },
                        {
                            "id": user_id,
                            "group_ids": ["system-admin"],
                            "is_owner": True,
                            "is_active": True,
                            "name": "Test",
                            "system_generated": False,
                            "local_only": False,
                        },
                    ],
                    "groups": [
                        {"id": "system-admin", "name": "Administrators"},
                        {"id": "system-users", "name": "Users"},
                        {"id": "system-read-only", "name": "Read Only"},
                    ],
                    "credentials": [
                        {
                            "id": credential_id,
                            "user_id": user_id,
                            "auth_provider_type": "homeassistant",
                            "auth_provider_id": None,
                            "data": {"username": "test"},
                        },
                    ],
                    "refresh_tokens": [
                        {
                            "id": content_user_id,
                            "user_id": content_user_id,
                            "client_id": None,
                            "client_name": None,
                            "client_icon": None,
                            "token_type": "system",
                            "created_at": now,
                            "access_token_expiration": 1800.0,
                            "token": system_token,
                            "jwt_key": system_jwt_key,
                            "last_used_at": None,
                            "last_used_ip": None,
                            "expire_at": None,
                            "credential_id": None,
                            "version": "2026.7.2",
                        },
                    ],
                },
            }
        )
    )

    # --- Person
    (storage_dir / "person").write_text(
        json.dumps(
            {
                "version": 2,
                "minor_version": 1,
                "key": "person",
                "data": {
                    "items": [
                        {
                            "id": "test",
                            "name": "Test",
                            "user_id": user_id,
                            "device_trackers": [],
                        }
                    ]
                },
            }
        )
    )

    # --- Core config: Singapore location
    (storage_dir / "core.config").write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 4,
                "key": "core.config",
                "data": {
                    "latitude": 1.357107,
                    "longitude": 103.8194992,
                    "elevation": 0,
                    "unit_system_v2": "metric",
                    "location_name": "Home",
                    "time_zone": "Asia/Singapore",
                    "external_url": None,
                    "internal_url": None,
                    "currency": "SGD",
                    "country": "SG",
                    "language": "en",
                    "radius": 100,
                },
            }
        )
    )

    # --- HTTP
    (storage_dir / "http").write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 1,
                "key": "http",
                "data": {
                    "server_port": port,
                    "ssl_profile": "modern",
                    "use_x_frame_options": True,
                    "cors_allowed_origins": [
                        "https://cast.home-assistant.io"
                    ],
                    "ip_ban_enabled": True,
                    "login_attempts_threshold": -1,
                },
            }
        )
    )

    (storage_dir / "http.auth").write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 1,
                "key": "http.auth",
                "data": {"content_user": content_user_id},
            }
        )
    )

    # --- Area registry: default named areas
    (storage_dir / "core.area_registry").write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 9,
                "key": "core.area_registry",
                "data": {
                    "areas": [
                        {
                            "aliases": [],
                            "floor_id": None,
                            "humidity_entity_id": None,
                            "icon": "mdi:sofa",
                            "id": "living_room",
                            "labels": [],
                            "name": "Living Room",
                            "picture": None,
                            "temperature_entity_id": None,
                            "created_at": now,
                            "modified_at": now,
                        },
                        {
                            "aliases": [],
                            "floor_id": None,
                            "humidity_entity_id": None,
                            "icon": "mdi:stove",
                            "id": "kitchen",
                            "labels": [],
                            "name": "Kitchen",
                            "picture": None,
                            "temperature_entity_id": None,
                            "created_at": now,
                            "modified_at": now,
                        },
                        {
                            "aliases": [],
                            "floor_id": None,
                            "humidity_entity_id": None,
                            "icon": "mdi:bed",
                            "id": "bedroom",
                            "labels": [],
                            "name": "Bedroom",
                            "picture": None,
                            "temperature_entity_id": None,
                            "created_at": now,
                            "modified_at": now,
                        },
                    ]
                },
            }
        )
    )

    # --- Lovelace: default dashboard
    (storage_dir / "lovelace_dashboards").write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 1,
                "key": "lovelace_dashboards",
                "data": {
                    "items": [
                        {
                            "id": "map",
                            "icon": "mdi:map",
                            "title": "Map",
                            "url_path": "map",
                            "require_admin": False,
                            "mode": "storage",
                            "show_in_sidebar": True,
                        }
                    ]
                },
            }
        )
    )

    # --- Dev seed defaults for config flow
    _write_dev_seed_defaults(config_dir)

    # --- HA version marker
    (config_dir / ".HA_VERSION").write_text("2026.7.2")



def _write_dev_seed_defaults(config_dir: Path) -> None:
    """Write developer seed defaults so the config flow pre-fills seed fields.

    Timestamps are computed dynamically relative to 'now' so a fresh reset
    always produces sensible defaults without manual entry.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    sgt = ZoneInfo("Asia/Singapore")
    now_sgt = datetime.now(sgt)

    # Water: 1st of current month at 00:00 SGT (naive)
    water_seen = datetime(
        year=now_sgt.year, month=now_sgt.month,
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )
    # Frontend DateTimeSelector expects YYYY-MM-DDT00:00:00 HH:MM format
    water_seen_frontend = water_seen.strftime("%Y-%m-%dT00:00:00 00:00")

    # Electricity: round down to nearest 30-minute boundary (naive)
    elec_minutes = (now_sgt.minute // 30) * 30
    elec_seen = datetime(
        year=now_sgt.year, month=now_sgt.month,
        day=now_sgt.day, hour=now_sgt.hour,
        minute=elec_minutes, second=0, microsecond=0,
    )
    # Frontend DateTimeSelector expects YYYY-MM-DDT00:00:00 HH:MM format
    elec_seen_frontend = f"{elec_seen.strftime('%Y-%m-%d')}T00:00:00 {elec_seen.strftime('%H:%M')}"

    defaults = {
        "water": {
            "seed_value": 500,
            "seed_observed_at": water_seen_frontend,
            "backfill_from": "2025-01-01T00:00:00 00:00",
        },
        "electric": {
            "seed_value": 2000,
            "seed_observed_at": elec_seen_frontend,
            "backfill_from": "2025-01-01T00:00:00 00:00",
        },
    }

    target = config_dir.parent / "dev-seed-defaults.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(defaults, indent=2))
    _LOGGER = __import__("logging").getLogger(__name__)
    _LOGGER.info(
        "SP Group dev seed defaults written: %s (water=%s, elec=%s)",
        target,
        defaults["water"]["seed_observed_at"],
        defaults["electric"]["seed_observed_at"],
    )



def main() -> int:
    """Prepare a minimal HA config directory and start Home Assistant."""
    repo = Path(__file__).resolve().parents[1]
    config_dir = repo / ".runtime" / "ha-config"
    custom_components = config_dir / "custom_components"
    sp_group_link = custom_components / "sp_group"
    port = _select_port(int(os.environ.get("HA_DEV_PORT", "8123")))

    # Version guards — prevent accidental launch with wrong Python or HA version
    error = _check_environment()
    if error is not None:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1



    if os.environ.get("HA_DEV_RESET") == "1" and config_dir.exists():
        shutil.rmtree(config_dir)

    custom_components.mkdir(parents=True, exist_ok=True)
    if sp_group_link.exists() or sp_group_link.is_symlink():
        sp_group_link.unlink()
    sp_group_link.symlink_to(repo / "custom_components" / "sp_group")
    _write_minimal_config(config_dir, port)
    if not (config_dir / ".storage" / "onboarding").exists():
        _write_auto_onboarding(config_dir, port)
        print(
            "Onboarding auto-configured -- log in with test / test123",
            flush=True,
        )
    print(f"Starting Home Assistant at http://localhost:{port}", flush=True)
    process = subprocess.Popen(
        [
            _resolve_python(repo),
            "-m",
            "homeassistant",
            "--config",
            str(config_dir),
        ],
        cwd=repo,
        env=_ha_environment(),
        start_new_session=True,
    )
    _install_signal_handlers(process)
    try:
        return _wait_for_home_assistant(process)
    except KeyboardInterrupt:
        _stop_home_assistant(process)
        return 0


def _wait_for_home_assistant(process: subprocess.Popen[bytes]) -> int:
    """Wait for Home Assistant while leaving signal handling responsive."""
    while True:
        try:
            return process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            continue


def _install_signal_handlers(process: subprocess.Popen[bytes]) -> None:
    """Install parent signal handlers that clean up the HA process group."""

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        _stop_home_assistant(process)
        raise SystemExit(0)

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGTSTP, signal.SIGQUIT):
        signal.signal(signum, _handle_signal)


def _stop_home_assistant(process: subprocess.Popen[bytes]) -> None:
    """Stop Home Assistant and every child in its process group."""
    if process.poll() is not None:
        return

    _signal_process_group(process, signal.SIGCONT)
    _signal_process_group(process, signal.SIGINT)
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        pass

    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=TERMINATE_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        pass

    _signal_process_group(process, signal.SIGKILL)
    process.wait()


def _signal_process_group(
    process: subprocess.Popen[bytes],
    signum: signal.Signals,
) -> None:
    """Send a signal to the HA process group if it still exists."""
    with suppress(PermissionError, ProcessLookupError):
        os.killpg(process.pid, signum)


def _select_port(preferred: int) -> int:
    """Return the preferred port or the next available port."""
    for port in range(preferred, preferred + 20):
        if not _port_available(port):
            continue
        return port
    raise RuntimeError(f"No free Home Assistant port found near {preferred}")


def _port_available(port: int) -> bool:
    """Return whether Home Assistant can bind the port on its default hosts."""
    probes = (
        (socket.AF_INET6, ("::", port)),
        (socket.AF_INET, ("127.0.0.1", port)),
    )
    for family, address in probes:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(address)
            except OSError:
                return False
    return True


def _ha_environment() -> dict[str, str]:
    """Return an environment with common local HA runtime binaries available."""
    env = os.environ.copy()
    env["SPGROUP_HA_DEV_TOOLS"] = "1"
    if shutil.which("ffmpeg", path=env.get("PATH")):
        return env

    for path in (
        Path("/opt/homebrew/opt/ffmpeg/bin"),
        Path("/usr/local/opt/ffmpeg/bin"),
    ):
        if (path / "ffmpeg").exists():
            env["PATH"] = f"{path}{os.pathsep}{env.get('PATH', '')}"
            break
    return env





def _write_minimal_config(config_dir: Path, port: int) -> None:
    """Write a minimal config that avoids default_config optional dependencies."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "configuration.yaml").write_text(
        "\n".join(
            [
                "homeassistant:",
                "  name: SP Group Dev",
                "  latitude: 1.3521",
                "  longitude: 103.8198",
                "  elevation: 0",
                "  unit_system: metric",
                "  time_zone: Asia/Singapore",
                "",
                "config:",
                "",
                "frontend:",
                "",
                "http:",
                f"  server_port: {port}",
                "",
                "api:",
                "",
                "onboarding:",
                "",
                "recorder:",
                "",
                "logger:",
                "  default: info",
                "  logs:",
                "    custom_components.sp_group: debug",
                "",
                "history:",
                "",
                "energy:",
                "",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
