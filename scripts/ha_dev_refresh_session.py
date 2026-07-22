"""Refresh the local SP Group developer mobile-session cache.

This helper is intentionally dev-only. It refreshes the token cache used by the
Home Assistant config-flow cached-session path without starting Home Assistant.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import NoReturn

import aiohttp

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_PATH = REPO_ROOT / "custom_components" / "sp_group" / "vendor"
DEFAULT_SESSION_FILE = REPO_ROOT / ".runtime" / "mobile-session.json"
SESSION_FILE_ENV = "SPGROUP_HA_DEV_SESSION_FILE"

sys.path.insert(0, str(VENDOR_PATH))

from spgroup.mobile import (  # noqa: E402
    MobileAuthClient,
    MobileSession,
    SpGroupAuthError,
    SpGroupMobileError,
)


class SessionRefreshError(Exception):
    """Raised when the developer session cache cannot be refreshed."""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh .runtime/mobile-session.json using its refresh token.",
    )
    parser.add_argument(
        "--session-file",
        default=os.environ.get(SESSION_FILE_ENV, str(DEFAULT_SESSION_FILE)),
        help=(
            "Path to the cached mobile session JSON. Defaults to "
            f"${SESSION_FILE_ENV} or .runtime/mobile-session.json."
        ),
    )
    return parser.parse_args()


def _resolve_session_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def _load_session(path: Path) -> MobileSession:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise SessionRefreshError(f"Session cache not found: {path}") from err
    except (OSError, json.JSONDecodeError) as err:
        raise SessionRefreshError(
            f"Session cache is unreadable or malformed: {path}"
        ) from err

    if not isinstance(data, dict):
        raise SessionRefreshError("Session cache must contain a JSON object")

    try:
        session = MobileSession.from_dict(data)
    except (KeyError, TypeError, ValueError) as err:
        raise SessionRefreshError(
            "Session cache does not match MobileSession shape"
        ) from err

    _validate_session(session)
    return session


def _validate_session(session: MobileSession) -> None:
    if not session.access_token or not session.id_token:
        raise SessionRefreshError("Session cache is missing access or ID token")
    if not session.refresh_token:
        raise SessionRefreshError("Session cache is missing refresh token")
    if session.identity_metadata is None or not session.identity_metadata.subject:
        raise SessionRefreshError("Session cache is missing identity metadata")


def _write_session(path: Path, session: MobileSession) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(session.to_dict(), indent=2, sort_keys=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            os.chmod(temp_path, 0o600)
            temp.write(payload)
            temp.write("\n")
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _describe_session(session: MobileSession) -> str:
    identity = session.identity_metadata
    token = session.token_metadata
    identity_fragment = (
        identity.fingerprint if identity is not None else "unknown-identity"
    )
    expiry = token.expires_at.isoformat() if token is not None else "unknown-expiry"
    token_fragment = token.fingerprint if token is not None else "unknown-token"
    return (
        f"identity_fingerprint={identity_fragment} "
        f"access_token_fingerprint={token_fragment} expires_at={expiry}"
    )


async def _async_refresh(path: Path) -> MobileSession:
    session = _load_session(path)
    async with aiohttp.ClientSession() as http_session:
        client = MobileAuthClient(http_session)
        try:
            refreshed = await client.refresh_session(session)
        except (SpGroupAuthError, SpGroupMobileError) as err:
            raise SessionRefreshError("SP Group refresh-token request failed") from err
    _validate_session(refreshed)
    _write_session(path, refreshed)
    return refreshed


def _fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    args = _parse_args()
    path = _resolve_session_path(args.session_file)
    try:
        refreshed = asyncio.run(_async_refresh(path))
    except SessionRefreshError as err:
        _fail(str(err))
    except OSError as err:
        _fail(f"Could not write refreshed session cache: {err}")

    print(f"Refreshed SP Group mobile session cache: {path}")
    print(_describe_session(refreshed))


if __name__ == "__main__":
    main()
