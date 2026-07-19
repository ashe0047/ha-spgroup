"""HTTP transport helpers for SP Group mobile clients."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .errors import SpGroupTransportError
from .models import JsonValue, RawResponse

if TYPE_CHECKING:
    from aiohttp import ClientSession

SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "id_token",
        "refresh_token",
        "mfa_token",
        "password",
        "binding_code",
        "authorization",
        "client_secret",
        "email",
        "mobile",
        "mobile_number",
        "otp",
        "phone",
        "token",
    }
)


class MobileTransport:
    """Thin JSON transport using a caller-owned aiohttp session."""

    def __init__(self, session: ClientSession) -> None:
        """Initialize the transport with a caller-owned session."""
        self._session = session

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> RawResponse:
        """Perform a JSON request and return parsed plus raw response data."""
        try:
            async with self._session.request(
                method,
                url,
                headers=dict(headers or {}),
                json=dict(json_body) if json_body is not None else None,
            ) as response:
                raw_text = await response.text()
                status = response.status
        except Exception as err:  # noqa: BLE001 - keep transport boundary narrow.
            raise SpGroupTransportError(f"HTTP request failed: {err}") from err

        try:
            data: JsonValue = json.loads(raw_text) if raw_text else None
        except json.JSONDecodeError:
            data = raw_text[:200]

        return RawResponse(status=status, data=data, raw_text=raw_text)


def redact_json(value: object) -> object:
    """Return a best-effort redacted copy of JSON-like data."""
    if isinstance(value, dict):
        return {
            key: "***REDACTED***"
            if key.lower() in SENSITIVE_KEYS
            else redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    return value


def as_object(value: JsonValue, *, context: str) -> dict[str, Any]:
    """Return a parsed JSON object or raise a transport error."""
    if not isinstance(value, dict):
        raise SpGroupTransportError(f"{context} response was not a JSON object")
    return value


def as_array(value: JsonValue, *, context: str) -> list[Any]:
    """Return a parsed JSON array or raise a transport error."""
    if not isinstance(value, list):
        raise SpGroupTransportError(f"{context} response was not a JSON array")
    return value
