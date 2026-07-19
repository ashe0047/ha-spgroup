"""APK ResourceV2-inspired error helpers for SP Group mobile clients."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .transport import redact_json


class MobileApiErrorType(StrEnum):
    """Small SDK subset of APK ResourceError categories."""

    GENERIC = "GENERIC"
    MAINTENANCE = "MAINTENANCE"
    MFA_REQUIRED = "MFA_REQUIRED"
    REQUIRES_VERIFICATION = "REQUIRES_VERIFICATION"
    UNAUTHORIZED = "UNAUTHORIZED"


@dataclass(frozen=True, slots=True)
class MobileApiError:
    """Redacted API error detail parsed from an HTTP response."""

    status: int
    type: MobileApiErrorType
    error: str | None
    error_description: str | None
    redacted_body: object | None


def parse_mobile_api_error(status: int, body: object) -> MobileApiError:
    """Parse a redacted SDK error detail from an API response body."""
    error, description = _extract_error_fields(body)
    return MobileApiError(
        status=status,
        type=_error_type(status, error, description),
        error=error,
        error_description=description,
        redacted_body=redact_json(body),
    )


def _extract_error_fields(body: object) -> tuple[str | None, str | None]:
    if not isinstance(body, dict):
        return None, str(body) if isinstance(body, str) and body else None

    error = _string_field(body, "error") or _string_field(body, "code")
    description = (
        _string_field(body, "error_description")
        or _string_field(body, "message")
        or _string_field(body, "description")
    )
    if error is None or description is None:
        nested_error, nested_description = _extract_nested_error_fields(body)
        error = error or nested_error
        description = description or nested_description
    return error, description


def _string_field(body: dict[str, Any], key: str) -> str | None:
    value = next(
        (item for item_key, item in body.items() if item_key.lower() == key),
        None,
    )
    return value if isinstance(value, str) and value else None


def _extract_nested_error_fields(value: object) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        error = _string_field(value, "error") or _string_field(value, "code")
        description = (
            _string_field(value, "error_description")
            or _string_field(value, "message")
            or _string_field(value, "description")
        )
        if error is not None or description is not None:
            return error, description
        for item in value.values():
            error, description = _extract_nested_error_fields(item)
            if error is not None or description is not None:
                return error, description
    elif isinstance(value, list):
        for item in value:
            error, description = _extract_nested_error_fields(item)
            if error is not None or description is not None:
                return error, description
    return None, None


def _error_type(
    status: int,
    error: str | None,
    description: str | None,
) -> MobileApiErrorType:
    normalized_error = error.lower() if error is not None else None
    normalized_description = description.lower() if description is not None else None
    if normalized_error == "mfa_required":
        return MobileApiErrorType.MFA_REQUIRED
    if status == 503 or normalized_error == "maintenance":
        return MobileApiErrorType.MAINTENANCE
    if status == 401:
        if normalized_error == "requires_verification":
            return MobileApiErrorType.REQUIRES_VERIFICATION
        return MobileApiErrorType.UNAUTHORIZED
    if normalized_description == "multi-factor authentication required":
        return MobileApiErrorType.MFA_REQUIRED
    return MobileApiErrorType.GENERIC


__all__ = [
    "MobileApiError",
    "MobileApiErrorType",
    "parse_mobile_api_error",
]
