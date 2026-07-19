"""Initial backfill orchestration for SP Group."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_HOMEASSISTANT_START
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN
from .statistics_backfill import async_backfill_statistics

if TYPE_CHECKING:
    from . import SpGroupConfigEntry

LOGGER = logging.getLogger(__name__)


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
        task = hass.async_create_task(
            _run_backfill_with_retry(hass, entry),
            name="sp_group_full_backfill",
        )
        if hasattr(entry, "runtime_data") and entry.runtime_data is not None:
            entry.runtime_data.backfill_tasks.add(task)
            task.add_done_callback(entry.runtime_data.backfill_tasks.discard)
        return
    hass.loop.create_task(_schedule_after_start(hass, entry))


async def _schedule_after_start(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> None:
    """Wait for EVENT_HOMEASSISTANT_START, then launch the tracked backfill.
    
    This function runs as an untracked task so the bootstrap framework
    does not wait for it.  Once HA is running, it hands off the real
    work to a tracked task that HA can monitor.
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
    task = hass.async_create_task(
        _run_backfill_with_retry(hass, entry),
        name="sp_group_full_backfill",
    )
    if hasattr(entry, "runtime_data") and entry.runtime_data is not None:
        entry.runtime_data.backfill_tasks.add(task)
        task.add_done_callback(entry.runtime_data.backfill_tasks.discard)


async def _run_backfill_with_retry(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> None:
    """Run utility backfill and statistics import with retry."""
    runtime = entry.runtime_data
    # Clear any stale repair issue before starting
    ir.async_delete_issue(hass, DOMAIN, "backfill_failed")
    try:
        if runtime.electricity_coordinator is not None:
            await runtime.electricity_coordinator.async_poll_now(backfill=True)
            await runtime.electricity_coordinator.async_poll_now(backfill=True)
        if runtime.water_coordinator is not None:
            await runtime.water_coordinator.async_poll_now(backfill=True)

        # Statistics import with retry
        for attempt in range(3):
            try:
                await async_backfill_statistics(hass, entry)
                break
            except UpdateFailed:
                if attempt < 2:
                    LOGGER.warning(
                        "SP Group stats backfill transient failure (attempt %d/3)",
                        attempt + 1,
                    )
                    await asyncio.sleep(30 * (attempt + 1))
                else:
                    LOGGER.warning(
                        "SP Group stats backfill deferred after 3 attempts"
                    )
            except Exception:  # noqa: BLE001
                if attempt < 2:
                    LOGGER.exception(
                        "SP Group stats backfill error (attempt %d/3)",
                        attempt + 1,
                    )
                    await asyncio.sleep(30 * (attempt + 1))
                else:
                    ir.async_create_issue(
                        hass,
                        DOMAIN,
                        "backfill_failed",
                        is_fixable=True,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key="backfill_failed",
                        translation_placeholders={
                            "error": "Statistics import failed after 3 attempts",
                        },
                    )
                    LOGGER.exception(
                        "SP Group stats backfill failed after 3 attempts"
                    )
    except UpdateFailed:
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
                "error": "Backfill failed, check logs for details",
            },
        )
        LOGGER.exception("SP Group backfill failed")


async def async_run_backfill(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> None:
    """Run the full backfill inline, with retry (used by options flow)."""
    await _run_backfill_with_retry(hass, entry)


__all__ = ["async_run_backfill", "async_start_initial_backfill"]
