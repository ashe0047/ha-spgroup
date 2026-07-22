"""Authentication helpers for SP Group runtime code."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from spgroup.mobile import MobileAuthClient, MobileSession, SpGroupAuthError

from .const import CONF_ACCOUNT_ID, CONF_SESSION, STORAGE_VERSION

if TYPE_CHECKING:
    from . import SpGroupConfigEntry

LOGGER = logging.getLogger(__name__)


def mobile_session_from_entry(entry: ConfigEntry[Any]) -> MobileSession:
    """Deserialize the stored mobile session from a config entry."""
    data = entry.data.get(CONF_SESSION)
    if not isinstance(data, dict):
        raise ConfigEntryAuthFailed("SP Group config entry is missing session data")
    try:
        return MobileSession.from_dict(data)
    except (KeyError, TypeError, ValueError) as err:
        raise ConfigEntryAuthFailed(
            "SP Group config entry has invalid session data"
        ) from err


async def async_refresh_session_if_needed(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    *,
    session: MobileSession | None = None,
    client: MobileAuthClient | None = None,
) -> MobileSession:
    """Refresh and persist an expired mobile session before API polling."""
    current_session = session or mobile_session_from_entry(entry)
    auth_client = client or MobileAuthClient(async_get_clientsession(hass))

    return await _async_refresh_session_core(
        hass,
        entry,
        session=current_session,
        client=auth_client,
        persist=True,
    )


async def async_load_session(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
) -> tuple[MobileSession, Store[dict[str, Any]]]:
    """Load the runtime session from Store, migrating legacy entry data."""
    store: Store[dict[str, Any]] = Store(
        hass,
        STORAGE_VERSION,
        f"sp_group.session.{entry.entry_id}",
    )
    store_data = await store.async_load()
    if isinstance(store_data, dict):
        try:
            return MobileSession.from_dict(store_data), store
        except (KeyError, TypeError, ValueError):
            LOGGER.warning(
                "SP Group session Store is invalid; falling back to config entry data"
            )

    session = mobile_session_from_entry(entry)
    if store_data is None:
        await store.async_save(session.to_dict())
        LOGGER.info("SP Group session migrated from config entry to HA Store")
    return session, store


async def async_refresh_runtime_session_if_needed(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> MobileSession:
    """Refresh and persist the runtime mobile session before API polling."""
    async with entry.runtime_data.refresh_lock:
        original_token = entry.runtime_data.session.access_token
        refreshed = await _async_refresh_session_core(
            hass,
            entry,
            session=entry.runtime_data.session,
            client=entry.runtime_data.auth_client,
            on_refreshed=lambda new_session: _update_runtime_session(
                entry,
                new_session,
            ),
        )
        entry.runtime_data.session = refreshed
    # Persist outside the refresh lock — only if session was actually refreshed
    if refreshed.access_token != original_token:
        await async_persist_session(hass, entry, refreshed)
    return refreshed


async def _async_refresh_session_core(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    *,
    session: MobileSession,
    client: MobileAuthClient,
    on_refreshed: Callable[[MobileSession], Awaitable[None] | None] | None = None,
    persist: bool = False,
) -> MobileSession:
    """Shared refresh path."""
    if not session.expired:
        return session

    if not session.refresh_token:
        raise ConfigEntryAuthFailed("SP Group session is missing a refresh token")

    try:
        refreshed = await client.refresh_session(session)
    except SpGroupAuthError as err:
        raise ConfigEntryAuthFailed("SP Group session refresh failed") from err

    if not _is_usable_refreshed_session(refreshed):
        raise ConfigEntryAuthFailed("SP Group session refresh returned unusable tokens")

    if persist:
        account_id = entry.data.get(CONF_ACCOUNT_ID)
        if refreshed.identity_metadata is not None:
            account_id = refreshed.identity_metadata.subject
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCOUNT_ID: account_id,
                CONF_SESSION: refreshed.to_dict(),
            },
        )

    if on_refreshed is not None:
        result = on_refreshed(refreshed)
        if result is not None:
            await result
    return refreshed

async def async_persist_session(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
    session: MobileSession,
) -> None:
    """Persist a refreshed session to the HA Store for restart durability."""
    store = getattr(entry.runtime_data, "session_store", None)
    if store is None:
        try:
            store = Store(hass, STORAGE_VERSION, f"sp_group.session.{entry.entry_id}")
            entry.runtime_data.session_store = store
        except (AttributeError, KeyError):
            # Store not available (e.g., test context without full HA infra)
            return
    await store.async_save(session.to_dict())


def _is_usable_refreshed_session(session: MobileSession) -> bool:
    """Return whether a refreshed session has the tokens runtime code needs."""
    return bool(session.access_token and session.id_token and session.refresh_token)


def _update_runtime_session(
    entry: SpGroupConfigEntry,
    session: MobileSession,
) -> None:
    """Update runtime data after a persisted refresh."""
    entry.runtime_data.session = session
    if session.identity_metadata is not None:
        entry.runtime_data.account_id = session.identity_metadata.subject
