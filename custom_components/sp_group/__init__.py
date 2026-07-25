"""The SP Group integration."""
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_RECORDER_HOURLY_STATISTICS_GENERATED, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_utils

# Dev mode: vendor directory provides the SDK directly.
# Production: vendor is not shipped; manifest.json requirements install spgroup.
_VENDOR_PATH = str(Path(__file__).resolve().parent / "vendor")
if _VENDOR_PATH not in sys.path:
    sys.path.insert(0, _VENDOR_PATH)

from spgroup.mobile import (
    JarvisClient,
    MobileAuthClient,
    MobileSession,
    ProfileClient,
    YggdrasilClient,
)

from .auth import async_load_session
from .backfill import async_schedule_statistics_backfill, async_start_initial_backfill
from .const import CONF_ACCOUNT_ID, DOMAIN
from .coordinator import (
    SpGroupDataUpdateCoordinator,
    SpGroupDiscoveryCoordinator,
    SpGroupElectricityCoordinator,
    SpGroupWaterCoordinator,
)
from .dev import async_register_dev_services, initial_backfill_skipped_for_dev
from .external_statistics import EXTERNAL_STATISTICS_STRATEGY
from .ledger import STATISTICS_STRATEGY_NATIVE_OFFSET_V2, SpGroupLedgerStore
from .polling_options import normalize_options
from .sources import source_configs_for_data

_PLATFORMS: list[Platform] = [Platform.SENSOR]
_STATISTICS_RETRY_EVENT_DELAY_SECONDS = 1

LOGGER = logging.getLogger(__name__)
MALFORMED_LEDGER_REPAIR_ISSUE = "malformed_ledger"


@dataclass(slots=True)
class SpGroupRuntimeData:
    """Runtime data for the SP Group integration."""

    auth_client: MobileAuthClient
    jarvis_client: JarvisClient
    yggdrasil_client: YggdrasilClient
    profile_client: ProfileClient
    session: MobileSession
    account_id: str | None
    refresh_lock: asyncio.Lock
    ledger_store: SpGroupLedgerStore | None = None
    discovery_coordinator: SpGroupDiscoveryCoordinator | None = None
    electricity_coordinator: SpGroupElectricityCoordinator | None = None
    water_coordinator: SpGroupWaterCoordinator | None = None
    backfill_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    session_store: Store[Any] | None = None

    @property
    def coordinator(self) -> SpGroupDiscoveryCoordinator | None:
        """Return discovery coordinator for legacy callers."""
        return self.discovery_coordinator

    @coordinator.setter
    def coordinator(self, value: SpGroupDiscoveryCoordinator | None) -> None:
        """Set discovery coordinator for legacy callers."""
        self.discovery_coordinator = value


type SpGroupConfigEntry = ConfigEntry[SpGroupRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: SpGroupConfigEntry) -> bool:
    """Set up SP Group from a config entry."""
    LOGGER.info("SP Group setup started")
    async_register_dev_services(hass)
    session, session_store = await async_load_session(hass, entry)
    aiohttp_session = async_get_clientsession(hass)
    ledger_store = SpGroupLedgerStore(hass, entry.entry_id)
    await ledger_store.async_load()
    if hasattr(ledger_store, "async_mark_interrupted_source_backfills_failed"):
        await ledger_store.async_mark_interrupted_source_backfills_failed(
            failed_at=dt_utils.utcnow()
        )
    if ledger_store.snapshot.malformed:
        _async_create_malformed_ledger_issue(hass, entry)
    else:
        _async_delete_malformed_ledger_issue(hass, entry)
    entry.runtime_data = SpGroupRuntimeData(
        auth_client=MobileAuthClient(aiohttp_session),
        jarvis_client=JarvisClient(aiohttp_session),
        yggdrasil_client=YggdrasilClient(aiohttp_session),
        profile_client=ProfileClient(aiohttp_session),
        session=session,
        account_id=entry.data.get(CONF_ACCOUNT_ID),
        refresh_lock=asyncio.Lock(),
        ledger_store=ledger_store,
        session_store=session_store,
    )
    entry.runtime_data.backfill_tasks = set()
    discovery_coordinator = SpGroupDataUpdateCoordinator(hass, entry)
    electricity_coordinator = SpGroupElectricityCoordinator(hass, entry)
    water_coordinator = SpGroupWaterCoordinator(hass, entry)
    entry.runtime_data.discovery_coordinator = discovery_coordinator
    entry.runtime_data.electricity_coordinator = electricity_coordinator
    entry.runtime_data.water_coordinator = water_coordinator
    await discovery_coordinator.async_config_entry_first_refresh()
    LOGGER.info("SP Group setup discovery first refresh completed")
    discovery_data = getattr(discovery_coordinator, "data", None)
    if ledger_store.snapshot.malformed is False and discovery_data is not None:
        source_configs = source_configs_for_data(discovery_data, entry.options)
        await ledger_store.async_initialize_sources(
            source_configs
        )
        LOGGER.debug(
            "SP Group setup ledger sources initialized: source_count=%d",
            len(source_configs),
        )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    LOGGER.info(
        "SP Group setup platforms forwarded: platform_count=%d",
        len(_PLATFORMS),
    )
    if not ledger_store.snapshot.malformed and hasattr(hass, "loop"):
        water_coordinator.async_start_schedule()
        entry.async_on_unload(water_coordinator.async_cancel_schedule)
    # Source-history backfill is a one-shot provider fetch.  Statistics import
    # may remain pending until HA creates a native reference row, but that must
    # not cause repeated full-history API calls on restart.
    if initial_backfill_skipped_for_dev():
        LOGGER.warning(
            "SP Group setup source-history backfill skipped: "
            "reason=dev_controlled_poll_gap"
        )
        _backfill_needed = False
    elif ledger_store.snapshot.malformed:
        _backfill_needed = False
    else:
        try:
            _backfill_needed = _any_entry_missing_source_backfill_attempt(
                entry.runtime_data
            )
        except (AttributeError, KeyError, TypeError):
            LOGGER.warning(
                "SP Group ledger state check failed, defaulting to backfill-needed",
                exc_info=True,
            )
            _backfill_needed = True
    if _backfill_needed and hasattr(hass, "loop"):
        LOGGER.info("SP Group setup scheduling source-history backfill")
        async_start_initial_backfill(hass, entry)
    else:
        LOGGER.debug(
            "SP Group setup source-history backfill not scheduled: "
            "malformed=%s needed=%s",
            ledger_store.snapshot.malformed,
            _backfill_needed,
        )
    _async_register_statistics_retry_listener(hass, entry)

    LOGGER.info("SP Group setup completed")
    return True


def _async_register_statistics_retry_listener(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> None:
    """Register an hourly Recorder listener for pending statistics import."""
    if not _any_entry_can_eventually_retry_statistics(entry.runtime_data):
        LOGGER.debug("SP Group statistics retry listener skipped: complete_or_blocked")
        return
    LOGGER.info("SP Group statistics retry listener registered")

    @callback
    def _async_retry_statistics(_event: Event) -> None:
        LOGGER.debug("SP Group Recorder hourly statistics event received")
        if _any_entry_should_retry_statistics(entry.runtime_data):

            @callback
            def _async_delayed_retry(_now: object) -> None:
                _schedule_hourly_statistics_backfill(hass, entry)

            entry.async_on_unload(
                async_call_later(
                    hass,
                    _STATISTICS_RETRY_EVENT_DELAY_SECONDS,
                    _async_delayed_retry,
                )
            )
        else:
            LOGGER.debug("SP Group statistics retry skipped after event: not_ready")

    entry.async_on_unload(
        hass.bus.async_listen(
            EVENT_RECORDER_HOURLY_STATISTICS_GENERATED,
            _async_retry_statistics,
        )
    )


@callback
def _schedule_hourly_statistics_backfill(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> None:
    """Schedule statistics backfill from HA's event loop."""
    async_schedule_statistics_backfill(hass, entry)


def _any_entry_missing_watermark(runtime: SpGroupRuntimeData) -> bool:
    """Return True if any ledger entry has incomplete statistics import."""
    if runtime.ledger_store is None or runtime.ledger_store.snapshot.malformed:
        return True
    for _ledger_entry in runtime.ledger_store.snapshot.entries.values():
        if not _statistics_entry_complete(_ledger_entry):
            return True
    return False


def _any_entry_can_eventually_retry_statistics(runtime: SpGroupRuntimeData) -> bool:
    """Return True when any entry may later be retried by Recorder event."""
    if runtime.ledger_store is None or runtime.ledger_store.snapshot.malformed:
        return False
    entries = getattr(runtime.ledger_store.snapshot, "entries", None)
    if entries is None:
        return False
    for _ledger_entry in entries.values():
        if _statistics_retry_blocked_by_warning(
            (
                *getattr(_ledger_entry, "statistics_warnings", ()),
                *getattr(_ledger_entry, "warnings", ()),
            )
        ):
            continue
        if not _statistics_entry_complete(_ledger_entry):
            return True
    return False


def _any_entry_should_retry_statistics(runtime: SpGroupRuntimeData) -> bool:
    """Return True when hourly Recorder event should schedule stats import."""
    if runtime.ledger_store is None or runtime.ledger_store.snapshot.malformed:
        return False
    entries = getattr(runtime.ledger_store.snapshot, "entries", None)
    if entries is None:
        return False
    for _ledger_entry in entries.values():
        if _ledger_entry.source_backfill_attempted_at is None:
            continue
        if _statistics_retry_blocked_by_warning(
            (
                *getattr(_ledger_entry, "statistics_warnings", ()),
                *getattr(_ledger_entry, "warnings", ()),
            )
        ):
            continue
        if not _statistics_entry_complete(_ledger_entry):
            return True
    return False


def _statistics_entry_complete(ledger_entry: Any) -> bool:
    """Return whether the current statistics strategy has no dirty suffix."""
    if ledger_entry.statistics_imported_through is None:
        if (
            getattr(ledger_entry, "external_statistic_id", None) is not None
            and not getattr(ledger_entry, "accepted", ())
            and getattr(ledger_entry, "external_migration_status", None)
            == "complete"
        ):
            return True
        return False
    if getattr(ledger_entry, "statistics_dirty_from", None) is not None:
        return False
    strategy = ledger_entry.statistics_strategy_version
    if getattr(ledger_entry, "external_statistic_id", None) is not None:
        return bool(strategy == EXTERNAL_STATISTICS_STRATEGY)
    return bool(strategy == STATISTICS_STRATEGY_NATIVE_OFFSET_V2)


def _statistics_retry_blocked_by_warning(warnings: tuple[str, ...]) -> bool:
    """Return True when warnings describe state requiring manual action."""
    return any(
        warning.startswith("statistics_existing_negative_delta:")
        or warning.startswith("statistics_conflict:")
        or warning.startswith("corrected_bucket:")
        or warning == "statistics_legacy_rows_need_manual_rebuild"
        for warning in warnings
    )


def _any_entry_missing_source_backfill_attempt(
    runtime: SpGroupRuntimeData,
) -> bool:
    """Return True when any seeded utility has not attempted source backfill."""
    if runtime.ledger_store is None or runtime.ledger_store.snapshot.malformed:
        return True
    for _ledger_entry in runtime.ledger_store.snapshot.entries.values():
        status = getattr(_ledger_entry, "source_backfill_status", None)
        if status in {"never_attempted", "failed"} or (
            status is None
            and getattr(_ledger_entry, "source_backfill_attempted_at", None) is None
        ):
            return True
    return False




async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries."""
    if entry.version > 1:
        return False
    if entry.version == 1 and entry.minor_version >= 3:
        return True

    options = normalize_options(entry.options)
    hass.config_entries.async_update_entry(
        entry,
        data=entry.data,
        options=options,
        version=1,
        minor_version=3,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SpGroupConfigEntry) -> bool:
    """Unload a config entry."""
    LOGGER.info("SP Group unload started")
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        tasks = set(getattr(runtime, "backfill_tasks", set()) or set())
        water_coordinator = getattr(runtime, "water_coordinator", None)
        if water_coordinator is not None:
            water_coordinator.async_cancel_schedule()
            tasks.update(getattr(runtime, "backfill_tasks", set()) or set())
        for task in tasks:
            task.cancel()
        if tasks:
            LOGGER.info(
                "SP Group unload cancelling background tasks: task_count=%d",
                len(tasks),
            )
            await asyncio.gather(*tasks, return_exceptions=True)
    unloaded = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    LOGGER.info("SP Group unload completed: unloaded=%s", unloaded)
    return unloaded


async def _async_update_listener(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> None:
    """Reload the entry after source options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _ledger_issue_id(entry: ConfigEntry) -> str:
    return f"{MALFORMED_LEDGER_REPAIR_ISSUE}_{entry.entry_id}"


def _async_create_malformed_ledger_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        _ledger_issue_id(entry),
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=MALFORMED_LEDGER_REPAIR_ISSUE,
    )


def _async_delete_malformed_ledger_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    ir.async_delete_issue(hass, DOMAIN, _ledger_issue_id(entry))
