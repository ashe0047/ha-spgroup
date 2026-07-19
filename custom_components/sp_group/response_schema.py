"""Response schema validation for SP Group API endpoints.

Each endpoint's response structure is validated before business-logic parsing.
- Discovery paths (setup/startup): raise DiscoveryDataError on structural failure.
- Poll paths (ongoing operation): log warning + return empty on structural failure.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structural type guards — raise DiscoveryDataError for discovery contexts,
# log + return default for poll contexts.
# ---------------------------------------------------------------------------


class SchemaError(ValueError):
    """Raised when an API response structure does not match expectations."""


def assert_is_dict(
    value: Any,
    *,
    path: str,
) -> dict[str, Any]:
    """Assert value is a dict or raise a descriptive DiscoveryDataError."""
    if isinstance(value, dict):
        return value
    raise SchemaError(
        f"Expected dict at {path}, got {type(value).__name__}: {repr(value)[:120]}"
    )


def assert_is_list(
    value: Any,
    *,
    path: str,
) -> list[Any]:
    """Assert value is a list or raise a descriptive DiscoveryDataError."""
    if isinstance(value, list):
        return value
    raise SchemaError(
        f"Expected list at {path}, got {type(value).__name__}: {repr(value)[:120]}"
    )


def assert_is_str(
    value: Any,
    *,
    path: str,
    allow_empty: bool = False,
) -> str:
    """Assert value is a non-empty string or raise."""
    if isinstance(value, str) and (allow_empty or value):
        return value
    raise SchemaError(
        f"Expected non-empty string at {path}, got {type(value).__name__}: "
        f"{repr(value)[:120]}"
    )


def assert_is_bool(
    value: Any,
    *,
    path: str,
) -> bool:
    """Assert value is a bool or raise."""
    if isinstance(value, bool):
        return value
    raise SchemaError(
        f"Expected bool at {path}, got {type(value).__name__}: {repr(value)[:120]}"
    )


def optional_dict(
    value: Any,
    *,
    path: str,
) -> dict[str, Any] | None:
    """Return value if it is a dict or None, otherwise raise."""
    if value is None or isinstance(value, dict):
        return value
    _LOGGER.warning(
        "SP Group schema warning: expected dict or None at %s, got %s: %s",
        path,
        type(value).__name__,
        repr(value)[:120],
    )
    return None


def optional_list(
    value: Any,
    *,
    path: str,
) -> list[Any] | None:
    """Return value if it is a list or None, otherwise raise."""
    if value is None or isinstance(value, list):
        return value
    _LOGGER.warning(
        "SP Group schema warning: expected list or None at %s, got %s: %s",
        path,
        type(value).__name__,
        repr(value)[:120],
    )
    return None


def optional_str(
    value: Any,
    *,
    path: str,
) -> str | None:
    """Return value if it is a (possibly empty) string or None."""
    if value is None or isinstance(value, str):
        return value
    _LOGGER.warning(
        "SP Group schema warning: expected str or None at %s, got %s: %s",
        path,
        type(value).__name__,
        repr(value)[:120],
    )
    return None


# ---------------------------------------------------------------------------
# Poll-context validators — log warnings and return safe defaults.
# ---------------------------------------------------------------------------


def poll_validate_is_dict(
    value: Any,
    *,
    path: str,
) -> dict[str, Any] | None:
    """Return value if dict or None; log warning otherwise."""
    if value is None or isinstance(value, dict):
        return value
    _LOGGER.warning(
        "SP Group %s: expected dict, got %s — treating as absent",
        path,
        type(value).__name__,
    )
    return None


def poll_validate_is_list(
    value: Any,
    *,
    path: str,
) -> list[Any] | None:
    """Return value if list or None; log warning otherwise."""
    if value is None or isinstance(value, list):
        return value
    _LOGGER.warning(
        "SP Group %s: expected list, got %s — treating as absent",
        path,
        type(value).__name__,
    )
    return None


def poll_validate_is_str(
    value: Any,
    *,
    path: str,
) -> str | None:
    """Return value if non-empty string; log warning otherwise."""
    if isinstance(value, str) and value:
        return value
    _LOGGER.warning(
        "SP Group %s: expected non-empty string, got %s — treating as absent",
        path,
        repr(value)[:80],
    )
    return None


__all__ = [
    "SchemaError",
    "SchemaError",
    "assert_is_dict",
    "assert_is_list",
    "assert_is_str",
    "assert_is_bool",
    "optional_dict",
    "optional_list",
    "optional_str",
    "poll_validate_is_dict",
    "poll_validate_is_list",
    "poll_validate_is_str",
]
