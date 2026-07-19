"""Authentication helpers for SP Group runtime code."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from spgroup.mobile import MobileAuthClient, MobileSession, SpGroupAuthError

from .const import CONF_ACCOUNT_ID, CONF_SESSION

if TYPE_CHECKING:
    from . import SpGroupConfigEntry


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
    )


async def async_refresh_runtime_session_if_needed(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> MobileSession:
    """Refresh and persist the runtime mobile session before API polling."""
    async with entry.runtime_data.refresh_lock:
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
        return refreshed


async def _async_refresh_session_core(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    *,
    session: MobileSession,
    client: MobileAuthClient,
    on_refreshed: Callable[[MobileSession], Awaitable[None] | None] | None = None,
) -> MobileSession:
    """Shared refresh and persistence path."""
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
