"""Polling option helpers for the SP Group integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from typing import Any

from .const import (
    CONF_ELECTRICITY_POLL_INTERVAL_HOURS,
    CONF_SOURCE_CONFIG,
    CONF_WATER_POLL_DAY,
    CONF_WATER_POLL_TIME,
    DEFAULT_ELECTRICITY_POLL_INTERVAL_HOURS,
    DEFAULT_WATER_POLL_DAY,
    DEFAULT_WATER_POLL_TIME,
)
from .sources import normalize_source_options


@dataclass(frozen=True, slots=True)
class SpGroupPollingOptions:
    """Normalized polling options."""

    electricity_poll_interval_hours: int = DEFAULT_ELECTRICITY_POLL_INTERVAL_HOURS
    water_poll_day: int = DEFAULT_WATER_POLL_DAY
    water_poll_time: time = time(0, 0)


def polling_options(options: Mapping[str, Any]) -> SpGroupPollingOptions:
    """Return normalized polling options with defensive defaults."""
    return SpGroupPollingOptions(
        electricity_poll_interval_hours=_int_in_range(
            options.get(CONF_ELECTRICITY_POLL_INTERVAL_HOURS),
            default=DEFAULT_ELECTRICITY_POLL_INTERVAL_HOURS,
            minimum=1,
            maximum=24,
        ),
        water_poll_day=_int_in_range(
            options.get(CONF_WATER_POLL_DAY),
            default=DEFAULT_WATER_POLL_DAY,
            minimum=1,
            maximum=28,
        ),
        water_poll_time=_parse_time(options.get(CONF_WATER_POLL_TIME)),
    )


def normalize_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return normalized config-entry options."""
    normalized = dict(options)
    normalized[CONF_SOURCE_CONFIG] = normalize_source_options(options)
    normalized_polling = polling_options(normalized)
    normalized[CONF_ELECTRICITY_POLL_INTERVAL_HOURS] = (
        normalized_polling.electricity_poll_interval_hours
    )
    normalized[CONF_WATER_POLL_DAY] = normalized_polling.water_poll_day
    normalized[CONF_WATER_POLL_TIME] = normalized_polling.water_poll_time.isoformat()
    return normalized


def _int_in_range(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        return default
    if not isinstance(value, (str, bytes, int, float)):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < minimum or parsed > maximum:
        return default
    return parsed


def _parse_time(value: object) -> time:
    if not isinstance(value, str) or not value:
        value = DEFAULT_WATER_POLL_TIME
    try:
        return time.fromisoformat(value)
    except ValueError:
        return time.fromisoformat(DEFAULT_WATER_POLL_TIME)


__all__ = [
    "SpGroupPollingOptions",
    "normalize_options",
    "polling_options",
]
