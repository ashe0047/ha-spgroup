"""Initial backfill orchestration for SP Group."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_HOMEASSISTANT_START
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_utils

from .const import DOMAIN
from .external_statistics import EXTERNAL_STATISTICS_STRATEGY
from .statistics_backfill import async_backfill_statistics

if TYPE_CHECKING:
    from . import SpGroupConfigEntry

LOGGER = logging.getLogger(__name__)
_STATISTICS_BACKFILL_TASK_NAMES = {
    "sp_group_full_backfill",
    "sp_group_dev_statistics_backfill",
    "sp_group_hourly_statistics_backfill",
    "sp_group_options_statistics_backfill",
    "sp_group_poll_statistics_backfill",
}
_SOURCE_RETRYABLE_STATUSES = {"never_attempted", "failed"}
_HARD_STATISTICS_WARNING_PREFIXES = (
    "statistics_existing_negative_delta:",
    "statistics_conflict:",
)
_HARD_STATISTICS_WARNINGS = {
    "statistics_legacy_rows_need_manual_rebuild",
}


class StatisticsBackfillScheduleStatus(StrEnum):
    """Result of scheduling a statistics-only backfill."""

    SCHEDULED = "scheduled"
    ALREADY_RUNNING = "already_running"
    NOT_READY = "not_ready"
    BLOCKED = "blocked"


def _track_backfill_task(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
    task: asyncio.Task[None],
    *,
    trigger: str | None = None,
) -> None:
    """Track a backfill task for single-flight checks and unload cleanup."""
    if hasattr(entry, "runtime_data") and entry.runtime_data is not None:
        entry.runtime_data.backfill_tasks.add(task)
        task.add_done_callback(entry.runtime_data.backfill_tasks.discard)
        task.add_done_callback(
            lambda completed: _log_backfill_task_result(
                hass,
                entry,
                completed,
                trigger=trigger,
            )
        )


def _has_running_statistics_backfill(entry: SpGroupConfigEntry) -> bool:
    """Return whether a source/statistics backfill task is already running."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return False
    for task in getattr(runtime, "backfill_tasks", set()) or set():
        if task.done():
            continue
        if task.get_name() in _STATISTICS_BACKFILL_TASK_NAMES:
            return True
    return False


def async_schedule_statistics_backfill(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
    *,
    name: str = "sp_group_hourly_statistics_backfill",
    force: bool = False,
) -> StatisticsBackfillScheduleStatus:
    """Schedule a statistics-only backfill retry if one is not already running."""
    runtime_entry = _runtime_config_entry(hass, entry)
    if _statistics_backfill_blocked(runtime_entry):
        LOGGER.info(
            "SP Group statistics backfill skipped: trigger=%s force=%s reason=blocked",
            name,
            force,
        )
        return StatisticsBackfillScheduleStatus.BLOCKED
    if not _statistics_backfill_ready(runtime_entry, force=force):
        LOGGER.info(
            "SP Group statistics backfill skipped: trigger=%s force=%s "
            "reason=not_ready",
            name,
            force,
        )
        return StatisticsBackfillScheduleStatus.NOT_READY
    if _has_running_statistics_backfill(runtime_entry):
        LOGGER.info(
            "SP Group statistics backfill skipped: trigger=%s force=%s "
            "reason=already_running",
            name,
            force,
        )
        return StatisticsBackfillScheduleStatus.ALREADY_RUNNING
    task = hass.async_create_task(
        async_run_statistics_backfill(
            hass,
            runtime_entry,
            trigger=name,
            force=force,
        ),
        name=name,
    )
    _track_backfill_task(hass, runtime_entry, task, trigger=name)
    LOGGER.info(
        "SP Group statistics backfill scheduled: trigger=%s force=%s",
        name,
        force,
    )
    return StatisticsBackfillScheduleStatus.SCHEDULED


def _runtime_config_entry(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> SpGroupConfigEntry:
    """Return HA's loaded config entry when the caller has a detached entry."""
    current = hass.config_entries.async_get_entry(entry.entry_id)
    if current is not None:
        return current
    return entry


def _statistics_backfill_ready(entry: SpGroupConfigEntry, *, force: bool) -> bool:
    """Return whether persisted ledger state can attempt statistics import."""
    runtime = getattr(entry, "runtime_data", None)
    ledger_store = getattr(runtime, "ledger_store", None)
    snapshot = getattr(ledger_store, "snapshot", None)
    if snapshot is None or getattr(snapshot, "malformed", False):
        return False
    for ledger_entry in getattr(snapshot, "entries", {}).values():
        status = getattr(ledger_entry, "source_backfill_status", None)
        if (
            status in {"never_attempted", "running", "failed"}
            or (
                status is None
                and getattr(ledger_entry, "source_backfill_attempted_at", None) is None
            )
        ):
            continue
        if force:
            return True
        if getattr(ledger_entry, "external_statistic_id", None) is not None:
            if (
                not getattr(ledger_entry, "accepted", ())
                and getattr(ledger_entry, "external_migration_status", None)
                == "complete"
            ):
                continue
            if (
                getattr(ledger_entry, "statistics_imported_through", None) is None
                or getattr(ledger_entry, "statistics_dirty_from", None) is not None
                or getattr(ledger_entry, "statistics_strategy_version", None)
                != EXTERNAL_STATISTICS_STRATEGY
            ):
                return True
            continue
        if (
            getattr(ledger_entry, "statistics_imported_through", None) is None
            or getattr(ledger_entry, "statistics_strategy_version", None)
            != "native_offset_v2"
        ):
            return True
    return False


def _statistics_backfill_blocked(entry: SpGroupConfigEntry) -> bool:
    """Return whether statistics retry should wait for manual review."""
    runtime = getattr(entry, "runtime_data", None)
    ledger_store = getattr(runtime, "ledger_store", None)
    snapshot = getattr(ledger_store, "snapshot", None)
    if snapshot is None or getattr(snapshot, "malformed", False):
        return False
    for ledger_entry in getattr(snapshot, "entries", {}).values():
        statistics_warnings = getattr(ledger_entry, "statistics_warnings", ()) or ()
        entry_warnings = getattr(ledger_entry, "warnings", ()) or ()
        for warning in (*statistics_warnings, *entry_warnings):
            if warning in _HARD_STATISTICS_WARNINGS or any(
                warning.startswith(prefix)
                for prefix in _HARD_STATISTICS_WARNING_PREFIXES
            ) or warning.startswith("corrected_bucket:"):
                return True
    return False


def _log_backfill_task_result(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
    task: asyncio.Task[None],
    *,
    trigger: str | None,
) -> None:
    """Consume task exceptions so background failures are logged and surfaced."""
    if task.cancelled():
        LOGGER.debug("SP Group backfill task cancelled")
        return
    try:
        task.result()
    except Exception:  # noqa: BLE001
        LOGGER.exception("SP Group backfill task failed")
        ir.async_create_issue(
            hass,
            DOMAIN,
            "backfill_failed",
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="backfill_failed",
            translation_placeholders={
                "error": "Statistics import failed, check logs for details",
            },
        )
        return
    if trigger in _STATISTICS_BACKFILL_TASK_NAMES:
        LOGGER.info("SP Group statistics backfill task completed")


def async_start_initial_backfill(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> None:
    """Start backfill immediately if HA is already running, or defer if still starting.

    When the config entry is loaded during HA startup, the backfill is deferred
    until EVENT_HOMEASSISTANT_START fires.  When the entry is created later
    (e.g. after a config flow), HA is already running and the event will never
    fire again, so the backfill starts immediately.
    """
    if hass.state == CoreState.running:
        LOGGER.info("SP Group source-history backfill scheduling immediately")
        task = hass.async_create_task(
            _run_backfill_with_retry(hass, entry),
            name="sp_group_full_backfill",
        )
        _track_backfill_task(hass, entry, task, trigger="sp_group_full_backfill")
        return
    LOGGER.info("SP Group source-history backfill deferred until HA start")
    task = hass.async_create_task(
        _schedule_after_start(hass, entry),
        name="sp_group_backfill_start_waiter",
    )
    _track_backfill_task(hass, entry, task, trigger="sp_group_backfill_start_waiter")


async def _schedule_after_start(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> None:
    """Wait for EVENT_HOMEASSISTANT_START, then launch the tracked backfill.

    The waiter itself is tracked so unload can cancel it.  Once HA is running,
    it hands off the real work to a separately tracked task without blocking
    the bootstrap framework.
    """
    started = asyncio.Event()
    _unsub = hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_START,
        lambda _: started.set(),
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=300)
    except TimeoutError:
        LOGGER.warning(
            "SP Group backfill deferred: EVENT_HOMEASSISTANT_START not received "
            "within 300s. Check that the onboarding component has completed "
            "or that .storage/onboarding exists."
        )
        return
    finally:
        if not started.is_set():
            _unsub()
    # Bootstrap is done; hand off to a tracked task
    LOGGER.info("SP Group source-history backfill scheduling after HA start")
    task = hass.async_create_task(
        _run_backfill_with_retry(hass, entry),
        name="sp_group_full_backfill",
    )
    _track_backfill_task(hass, entry, task, trigger="sp_group_full_backfill")


async def _run_backfill_with_retry(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> None:
    """Run utility backfill and statistics import with retry."""
    runtime = entry.runtime_data
    LOGGER.info("SP Group source-history backfill started")
    # Clear any stale repair issue before starting
    ir.async_delete_issue(hass, DOMAIN, "backfill_failed")
    any_utility_failed = False
    if runtime.electricity_coordinator is not None:
        any_utility_failed = (
            not await _run_utility_source_backfill(
                entry,
                "electric",
                runtime.electricity_coordinator.async_poll_now,
                poll_count=2 if runtime.electricity_coordinator.require_two_sightings else 1,
            )
            or any_utility_failed
        )
    if runtime.water_coordinator is not None:
        any_utility_failed = (
            not await _run_utility_source_backfill(
                entry,
                "water",
                runtime.water_coordinator.async_poll_now,
                poll_count=1,
            )
            or any_utility_failed
        )
    if any_utility_failed:
        ir.async_create_issue(
            hass,
            DOMAIN,
            "backfill_failed",
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="backfill_failed",
            translation_placeholders={
                "error": "Utility data fetch failed",
            },
        )
        LOGGER.debug("SP Group utility backfill deferred")
        return

    # Statistics import is a single non-blocking attempt.  Recorder events and
    # the coordinator's normal refresh path provide the retry mechanisms; this
    # one-shot source job must not create a second retry loop.
    LOGGER.info(
        "SP Group source-history backfill completed; starting statistics import"
    )
    try:
        result = await async_backfill_statistics(hass, entry)
        LOGGER.info(
            "SP Group initial statistics import finished: planned=%d "
            "importable=%d processed=%d warnings=%d",
            result.planned_count,
            result.importable_count,
            result.processed_count,
            result.warning_count,
        )
    except UpdateFailed:
        LOGGER.warning(
            "SP Group initial statistics import deferred: Recorder retry remains "
            "available"
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        ir.async_create_issue(
            hass,
            DOMAIN,
            "backfill_failed",
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="backfill_failed",
            translation_placeholders={
                "error": "Statistics import failed; check logs for details",
            },
        )
        LOGGER.exception("SP Group initial statistics import failed")


async def _run_utility_source_backfill(
    entry: SpGroupConfigEntry,
    utility_type: str,
    poll: Callable[..., Awaitable[object]],
    *,
    poll_count: int,
) -> bool:
    """Run source backfill for one utility and update ledger status."""
    runtime = entry.runtime_data
    ledger_store = runtime.ledger_store
    if ledger_store is None:
        LOGGER.debug(
            "SP Group %s source backfill skipped: reason=no_ledger_store",
            utility_type,
        )
        return True
    sensor_keys = _retryable_source_sensor_keys(entry, utility_type)
    if not sensor_keys:
        LOGGER.debug(
            "SP Group %s source backfill skipped: reason=no_retryable_sources",
            utility_type,
        )
        return True
    now = dt_utils.utcnow()
    LOGGER.info(
        "SP Group %s source backfill started: source_count=%d poll_count=%d",
        utility_type,
        len(sensor_keys),
        poll_count,
    )
    await ledger_store.async_mark_source_backfill_running(
        sensor_keys=sensor_keys,
        started_at=now,
    )
    try:
        for poll_index in range(poll_count):
            LOGGER.debug(
                "SP Group %s source backfill poll started: attempt=%d/%d",
                utility_type,
                poll_index + 1,
                poll_count,
            )
            await poll(backfill=True)
    except asyncio.CancelledError:
        raise
    except UpdateFailed:
        await ledger_store.async_mark_source_backfill_failed(
            sensor_keys=sensor_keys,
            failed_at=dt_utils.utcnow(),
        )
        LOGGER.warning(
            "SP Group %s source backfill failed: error=UpdateFailed",
            utility_type,
        )
        return False
    except Exception:  # noqa: BLE001
        await ledger_store.async_mark_source_backfill_failed(
            sensor_keys=sensor_keys,
            failed_at=dt_utils.utcnow(),
        )
        LOGGER.exception("SP Group %s source backfill failed", utility_type)
        return False
    await ledger_store.async_mark_source_backfill_succeeded(
        sensor_keys=sensor_keys,
        succeeded_at=dt_utils.utcnow(),
    )
    LOGGER.info(
        "SP Group %s source backfill succeeded: source_count=%d",
        utility_type,
        len(sensor_keys),
    )
    return True


def _retryable_source_sensor_keys(
    entry: SpGroupConfigEntry,
    utility_type: str,
) -> set[str]:
    """Return source keys requiring initial/retry backfill for a utility."""
    ledger_store = entry.runtime_data.ledger_store
    if ledger_store is None or ledger_store.snapshot.malformed:
        return set()
    return {
        sensor_key
        for sensor_key, ledger_entry in ledger_store.snapshot.entries.items()
        if ledger_entry.utility_type == utility_type
        and ledger_entry.source_backfill_status in _SOURCE_RETRYABLE_STATUSES
    }


async def async_run_backfill(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> None:
    """Run the full backfill inline, with retry (used by options flow)."""
    await _run_backfill_with_retry(hass, entry)


async def async_run_statistics_backfill(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
    *,
    trigger: str = "manual",
    force: bool = False,
) -> None:
    """Retry statistics import from persisted ledger data only."""
    LOGGER.info("SP Group statistics backfill started: trigger=%s", trigger)
    result = await async_backfill_statistics(hass, entry, force=force)
    LOGGER.info(
        "SP Group statistics backfill finished: trigger=%s planned=%d importable=%d "
        "processed=%d warnings=%d",
        trigger,
        result.planned_count,
        result.importable_count,
        result.processed_count,
        result.warning_count,
    )


__all__ = [
    "async_run_backfill",
    "async_run_statistics_backfill",
    "async_schedule_statistics_backfill",
    "async_start_initial_backfill",
    "StatisticsBackfillScheduleStatus",
]
