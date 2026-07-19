"""The SP Group integration."""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

# Dev mode: vendor directory provides the SDK directly.
# Production: vendor is not shipped; manifest.json requirements install spgroup from pip.
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

from .auth import mobile_session_from_entry
from .backfill import async_start_initial_backfill
from .const import CONF_ACCOUNT_ID, DOMAIN
from .coordinator import (
    SpGroupDataUpdateCoordinator,
    SpGroupDiscoveryCoordinator,
    SpGroupElectricityCoordinator,
    SpGroupWaterCoordinator,
)
from .ledger import SpGroupLedgerStore
from .polling_options import normalize_options
from .sources import source_configs_for_data

_PLATFORMS: list[Platform] = [Platform.SENSOR]

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
    session = mobile_session_from_entry(entry)
    aiohttp_session = async_get_clientsession(hass)
    ledger_store = SpGroupLedgerStore(hass, entry.entry_id)
    await ledger_store.async_load()
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
    )
    entry.runtime_data.backfill_tasks = set()
    discovery_coordinator = SpGroupDataUpdateCoordinator(hass, entry)
    electricity_coordinator = SpGroupElectricityCoordinator(hass, entry)
    water_coordinator = SpGroupWaterCoordinator(hass, entry)
    entry.runtime_data.discovery_coordinator = discovery_coordinator
    entry.runtime_data.electricity_coordinator = electricity_coordinator
    entry.runtime_data.water_coordinator = water_coordinator
    await discovery_coordinator.async_config_entry_first_refresh()
    discovery_data = getattr(discovery_coordinator, "data", None)
    if ledger_store.snapshot.malformed is False and discovery_data is not None:
        await ledger_store.async_initialize_sources(
            source_configs_for_data(discovery_data, entry.options)
        )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    if hasattr(hass, "loop"):
        water_coordinator.async_start_schedule()
        entry.async_on_unload(water_coordinator.async_cancel_schedule)
    # Skip backfill if all utilities have completed statistics import.
    # The initial backfill is a one-time import that fetches historical API
    # data.  On restart, regular polling handles incremental updates.
    try:
        _backfill_needed = _any_entry_missing_watermark(entry.runtime_data)
    except (AttributeError, KeyError, TypeError):
        LOGGER.warning(
            "SP Group ledger state check failed, defaulting to backfill-needed",
            exc_info=True,
        )
        _backfill_needed = True
    if _backfill_needed and hasattr(hass, "loop"):
        async_start_initial_backfill(hass, entry)

    return True



def _any_entry_missing_watermark(runtime: SpGroupRuntimeData) -> bool:
    """Return True if any seeded ledger entry has no completed statistics import."""
    if runtime.ledger_store is None or runtime.ledger_store.snapshot.malformed:
        return True
    for _ledger_entry in runtime.ledger_store.snapshot.entries.values():
        if _ledger_entry.statistics_imported_through is None:
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
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        water_coordinator = getattr(runtime, "water_coordinator", None)
        if water_coordinator is not None:
            water_coordinator.async_cancel_schedule()
        for task in set(getattr(runtime, "backfill_tasks", set()) or set()):
            task.cancel()
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)


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
