"""Diagnostics support for the SP Group integration."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DOMAIN

_MAX_WARNING_CODES = 10


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    runtime = getattr(entry, "runtime_data", None)
    discovery_coordinator = getattr(runtime, "discovery_coordinator", None)
    if discovery_coordinator is None:
        discovery_coordinator = getattr(runtime, "coordinator", None)
    electricity_coordinator = getattr(runtime, "electricity_coordinator", None)
    water_coordinator = getattr(runtime, "water_coordinator", None)
    data = getattr(discovery_coordinator, "data", None)

    return {
        "domain": DOMAIN,
        "entry": {
            "title": entry.title,
            "data_keys": sorted(entry.data),
            "options_keys": sorted(entry.options),
        },
        "runtime": {
            "has_runtime_data": runtime is not None,
            "has_coordinator": discovery_coordinator is not None,
            "has_discovery_coordinator": discovery_coordinator is not None,
            "has_electricity_coordinator": electricity_coordinator is not None,
            "has_water_coordinator": water_coordinator is not None,
            "has_session": getattr(runtime, "session", None) is not None,
            "ledger": _ledger_summary(runtime),
        },
        "coordinators": {
            "discovery": _coordinator_summary(discovery_coordinator, data=data),
            "electricity": _coordinator_summary(electricity_coordinator),
            "water": _coordinator_summary(water_coordinator),
        },
        "coordinator": _coordinator_summary(discovery_coordinator, data=data),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a device."""
    return await async_get_config_entry_diagnostics(hass, entry)


def _coordinator_data_summary(data: Any) -> dict[str, Any]:
    """Return an allowlisted summary of coordinator data."""
    if data is None:
        return {"available": False}

    electricity_warnings = tuple(getattr(data, "electricity_warnings", ()))
    water_warnings = tuple(getattr(data, "water_warnings", ()))

    return {
        "available": True,
        "premise_count": len(getattr(data, "premises", ())),
        "account_count": len(getattr(data, "accounts", ())),
        "future_sensor_count": len(data.future_sensor_keys()),
        "electricity_candidate_count": len(
            getattr(data, "electricity_candidates", ())
        ),
        "electricity_warning_count": len(electricity_warnings),
        "electricity_warning_codes": _warning_code_histogram(electricity_warnings),
        "water_candidate_count": len(getattr(data, "water_candidates", ())),
        "water_warning_count": len(water_warnings),
        "water_warning_codes": _warning_code_histogram(water_warnings),
        "has_partial_errors": bool(getattr(data, "has_partial_errors", False)),
    }


def _ledger_summary(runtime: Any) -> dict[str, Any]:
    ledger_store = getattr(runtime, "ledger_store", None)
    snapshot = getattr(ledger_store, "snapshot", None)
    entries = getattr(snapshot, "entries", {})
    if not isinstance(entries, dict):
        entries = {}
    return {
        "has_ledger_store": ledger_store is not None,
        "malformed": bool(getattr(snapshot, "malformed", False)),
        "entry_count": len(entries),
        "accepted_bucket_count": sum(
            len(getattr(entry, "accepted", ())) for entry in entries.values()
        ),
        "pending_bucket_count": sum(
            len(getattr(entry, "pending", ())) for entry in entries.values()
        ),
        "unresolved_range_count": sum(
            len(getattr(entry, "unresolved_ranges", ())) for entry in entries.values()
        ),
        "source_coverage_statuses": dict(
            Counter(
                str(getattr(entry, "source_coverage_status", "unknown"))
                for entry in entries.values()
            )
        ),
        "correction_record_count": sum(
            len(getattr(entry, "corrections", ())) for entry in entries.values()
        ),
        "warning_count": sum(
            len(getattr(entry, "warnings", ())) for entry in entries.values()
        ),
        "statistics_warning_count": sum(
            len(getattr(entry, "statistics_warnings", ()))
            for entry in entries.values()
        ),
        "statistics_strategy_counts": dict(
            Counter(
                str(getattr(entry, "statistics_strategy_version", "unknown"))
                for entry in entries.values()
            )
        ),
        "statistics_dirty_entry_count": sum(
            getattr(entry, "statistics_dirty_from", None) is not None
            for entry in entries.values()
        ),
        "external_migration_statuses": dict(
            Counter(
                str(getattr(entry, "external_migration_status", "none"))
                for entry in entries.values()
            )
        ),
        "source_backfill_statuses": dict(
            Counter(
                str(getattr(entry, "source_backfill_status", "unknown"))
                for entry in entries.values()
            )
        ),
    }


def _coordinator_summary(coordinator: Any, *, data: Any = None) -> dict[str, Any]:
    """Return an allowlisted coordinator summary."""
    return {
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "update_interval_seconds": _update_interval_seconds(coordinator),
        "data": _coordinator_data_summary(data) if data is not None else None,
    }


def _warning_code_histogram(warnings: tuple[str, ...]) -> dict[str, int]:
    """Return a bounded histogram of warning codes."""
    return dict(Counter(warnings).most_common(_MAX_WARNING_CODES))


def _update_interval_seconds(coordinator: Any) -> float | None:
    """Return the configured update interval in seconds."""
    update_interval = getattr(coordinator, "update_interval", None)
    if update_interval is None:
        return None
    return float(update_interval.total_seconds())
