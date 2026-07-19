"""Prepare and run a local Home Assistant dev instance for SP Group."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from types import FrameType

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
