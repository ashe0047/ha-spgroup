"""Developer-only helpers for local SP Group HA validation."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_utils

from .const import DOMAIN
from .sources import SINGAPORE_TZ

if TYPE_CHECKING:
    from . import SpGroupConfigEntry

LOGGER = logging.getLogger(__name__)

ENV_DEV_TOOLS = "SPGROUP_HA_DEV_TOOLS"
ENV_SKIP_INITIAL_BACKFILL = "SPGROUP_HA_DEV_SKIP_INITIAL_BACKFILL"
SERVICE_FORCE_STATISTICS_COMPILE = "dev_force_statistics_compile"
SERVICE_PREPARE_POLL_GAP = "dev_prepare_poll_gap"
SERVICE_POLL_NOW = "dev_poll_now"
_RECORDER_DOMAIN = "recorder"
_FULL_BACKFILL_TASK_NAME = "sp_group_full_backfill"
_UTILITY_ELECTRIC = "electric"
_UTILITY_WATER = "water"


class _RecorderInstance(Protocol):
    """Minimal Recorder methods used by the dev helper."""

    def queue_task(self, task: Any) -> None:
        """Queue a Recorder task."""

    def async_add_executor_job(
        self,
        target: Callable[..., Any],
        *args: Any,
    ) -> Awaitable[Any]:
        """Run a Recorder database job."""

    async def async_block_till_done(self) -> None:
        """Wait for Recorder to process queued work."""

    def get_session(self) -> Any:
        """Return Recorder's SQLAlchemy session maker."""


def async_register_dev_services(hass: HomeAssistant) -> None:
    """Register local development services when explicitly env-gated."""
    if os.environ.get(ENV_DEV_TOOLS) != "1":
        return
    if hass.services.has_service(DOMAIN, SERVICE_FORCE_STATISTICS_COMPILE):
        return

    async def _async_force_statistics_compile(_call: ServiceCall) -> None:
        await async_force_statistics_compile(hass)

    async def _async_prepare_poll_gap(call: ServiceCall) -> None:
        await async_prepare_poll_gap(hass, call)

    async def _async_poll_now(call: ServiceCall) -> None:
        await async_poll_now(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_STATISTICS_COMPILE,
        _async_force_statistics_compile,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PREPARE_POLL_GAP,
        _async_prepare_poll_gap,
        schema=vol.Schema(
            {vol.Required("utility"): vol.In({_UTILITY_ELECTRIC, _UTILITY_WATER})}
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_POLL_NOW,
        _async_poll_now,
        schema=vol.Schema(
            {
                vol.Required("utility"): vol.In(
                    {_UTILITY_ELECTRIC, _UTILITY_WATER}
                ),
                vol.Optional("poll_end"): str,
            }
        ),
    )


def initial_backfill_skipped_for_dev() -> bool:
    """Return whether the disposable controlled-gap mode is enabled."""
    return (
        os.environ.get(ENV_DEV_TOOLS) == "1"
        and os.environ.get(ENV_SKIP_INITIAL_BACKFILL) == "1"
    )


async def async_prepare_poll_gap(hass: HomeAssistant, call: ServiceCall) -> None:
    """Arm one selected utility for a controlled post-seed poll gap."""
    utility = str(call.data["utility"])
    entries = _loaded_entries(hass)
    if not entries:
        raise HomeAssistantError("no_loaded_sp_group_entries")

    prepared = 0
    for entry in entries:
        runtime = entry.runtime_data
        coordinator = getattr(runtime, _coordinator_attribute(utility))
        if coordinator is None:
            continue
        ledger_store = runtime.ledger_store
        if ledger_store is None or ledger_store.snapshot.malformed:
            raise HomeAssistantError("malformed_sp_group_ledger")
        sensor_keys = {
            key
            for key, ledger_entry in ledger_store.snapshot.entries.items()
            if ledger_entry.utility_type == utility
        }
        if not sensor_keys:
            continue
        if any(
            ledger_store.snapshot.entries[key].accepted
            or ledger_store.snapshot.entries[key].pending
            for key in sensor_keys
        ):
            raise HomeAssistantError("poll_gap_requires_empty_ledger")
        if any(
            not task.done()
            and task.get_name() == _FULL_BACKFILL_TASK_NAME
            for task in getattr(runtime, "backfill_tasks", set()) or set()
        ):
            raise HomeAssistantError("source_backfill_still_running")
        await ledger_store.async_mark_source_backfill_succeeded(
            sensor_keys=sensor_keys,
            succeeded_at=dt_utils.utcnow(),
        )
        prepared += len(sensor_keys)

    if not prepared:
        raise HomeAssistantError(f"no_{utility}_source_available")
    LOGGER.info(
        "SP Group dev poll gap prepared: utility=%s source_count=%d",
        utility,
        prepared,
    )


async def async_poll_now(hass: HomeAssistant, call: ServiceCall) -> None:
    """Run one normal, non-backfill poll for a selected utility."""
    utility = str(call.data["utility"])
    poll_end = _parse_poll_end(call.data.get("poll_end"))
    entries = _loaded_entries(hass)
    if not entries:
        raise HomeAssistantError("no_loaded_sp_group_entries")
    polled = 0
    for entry in entries:
        coordinator = getattr(entry.runtime_data, _coordinator_attribute(utility))
        if coordinator is None:
            continue
        poll_kwargs: dict[str, Any] = {"backfill": False}
        if poll_end is not None:
            poll_kwargs["poll_end"] = poll_end
        await coordinator.async_poll_now(**poll_kwargs)
        polled += 1
    if not polled:
        raise HomeAssistantError(f"no_{utility}_coordinator_available")
    LOGGER.info(
        "SP Group dev normal poll completed: utility=%s entry_count=%d poll_end=%s",
        utility,
        polled,
        poll_end.isoformat() if poll_end is not None else "wall_clock",
    )


def _parse_poll_end(value: object) -> datetime | None:
    """Parse the optional dev-only poll end timestamp."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HomeAssistantError("invalid_poll_end")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise HomeAssistantError("invalid_poll_end") from err
    if parsed.tzinfo is None:
        raise HomeAssistantError("poll_end_requires_timezone")
    poll_end = parsed.astimezone(SINGAPORE_TZ)
    if poll_end > dt_utils.utcnow().astimezone(SINGAPORE_TZ):
        raise HomeAssistantError("poll_end_cannot_be_in_future")
    return poll_end


def _loaded_entries(hass: HomeAssistant) -> list[SpGroupConfigEntry]:
    """Return loaded SP Group entries for dev-only actions."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if getattr(entry, "runtime_data", None) is not None
    ]


def _coordinator_attribute(utility: str) -> str:
    """Return the runtime coordinator attribute for a utility value."""
    return (
        "electricity_coordinator"
        if utility == _UTILITY_ELECTRIC
        else "water_coordinator"
    )


async def async_force_statistics_compile(hass: HomeAssistant) -> None:
    """Force a local Recorder statistics pass for the SP Group event listener."""
    if _RECORDER_DOMAIN not in hass.config.components:
        LOGGER.warning("SP Group dev statistics helper skipped: recorder not loaded")
        return
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if getattr(entry, "runtime_data", None) is not None
    ]
    if not entries:
        LOGGER.warning("SP Group dev statistics helper skipped: no loaded entries")
        return

    await _async_wait_for_source_backfill(entries)

    recorder = _recorder_instance(hass)
    short_term_start = _latest_completed_short_term_start()
    hourly_trigger_start = await _uncompiled_hourly_trigger_start(recorder)
    LOGGER.info(
        "SP Group dev statistics helper queueing Recorder tasks: "
        "short_term_start=%s hourly_trigger_start=%s",
        short_term_start.isoformat(),
        hourly_trigger_start.isoformat(),
    )
    recorder.queue_task(_statistics_task(short_term_start, False))
    recorder.queue_task(_statistics_task(hourly_trigger_start, True))
    await recorder.async_block_till_done()
    LOGGER.info("SP Group dev statistics helper completed Recorder queue")


async def _async_wait_for_source_backfill(entries: list[SpGroupConfigEntry]) -> None:
    """Wait for in-flight one-shot source backfill before Recorder compile."""
    tasks = [
        task
        for entry in entries
        for task in getattr(entry.runtime_data, "backfill_tasks", set()) or set()
        if not task.done() and task.get_name() == _FULL_BACKFILL_TASK_NAME
    ]
    if not tasks:
        return
    LOGGER.info(
        "SP Group dev statistics helper waiting for source backfill: tasks=%d",
        len(tasks),
    )
    for task in tasks:
        try:
            await task
        except Exception:  # noqa: BLE001
            LOGGER.exception("SP Group dev statistics helper source backfill failed")


def _latest_completed_short_term_start() -> datetime:
    """Return HA's latest completed 5-minute statistics period start."""
    now = dt_utils.utcnow()
    current_period_minutes = now.minute - now.minute % 5
    current_period = now.replace(
        minute=current_period_minutes,
        second=0,
        microsecond=0,
    )
    return current_period - timedelta(minutes=5)


async def _uncompiled_hourly_trigger_start(recorder: _RecorderInstance) -> datetime:
    """Return a :55 Recorder start that has not already been compiled."""
    start = dt_utils.utcnow().replace(minute=55, second=0, microsecond=0)
    for _ in range(24):
        exists = await recorder.async_add_executor_job(
            partial(_statistics_run_exists, recorder, start)
        )
        if not exists:
            return start
        start += timedelta(hours=1)
    return start


def _recorder_instance(hass: HomeAssistant) -> _RecorderInstance:
    """Return HA's Recorder instance through a dev-only lazy import."""
    from homeassistant.components import recorder  # noqa: PLC0415

    return cast(_RecorderInstance, recorder.get_instance(hass))  # type: ignore[attr-defined]


def _statistics_task(start: datetime, fire_events: bool) -> object:
    """Return a Recorder StatisticsTask through a dev-only lazy import."""
    from homeassistant.components.recorder.tasks import StatisticsTask  # noqa: PLC0415

    return StatisticsTask(start, fire_events)


def _statistics_run_exists(recorder: _RecorderInstance, start: datetime) -> bool:
    """Return whether Recorder has already compiled the statistics run."""
    from homeassistant.components.recorder import db_schema, util  # noqa: PLC0415

    with util.session_scope(  # type: ignore[attr-defined]
        session=recorder.get_session(),
        read_only=True,
    ) as session:
        return (
            session.query(db_schema.StatisticsRuns.run_id)
            .filter(db_schema.StatisticsRuns.start == start)
            .first()
            is not None
        )


__all__ = [
    "ENV_DEV_TOOLS",
    "ENV_SKIP_INITIAL_BACKFILL",
    "SERVICE_FORCE_STATISTICS_COMPILE",
    "SERVICE_POLL_NOW",
    "SERVICE_PREPARE_POLL_GAP",
    "async_force_statistics_compile",
    "async_poll_now",
    "async_prepare_poll_gap",
    "async_register_dev_services",
    "initial_backfill_skipped_for_dev",
]
