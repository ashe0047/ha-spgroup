"""Test SP Group runtime auth helpers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock, patch

import pytest
from custom_components.sp_group import SpGroupRuntimeData
from custom_components.sp_group.auth import (
    async_refresh_runtime_session_if_needed,
    async_refresh_session_if_needed,
)
from custom_components.sp_group.const import CONF_ACCOUNT_ID, CONF_SESSION, DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from spgroup.mobile import MobileSession, SpGroupAuthError, TokenMetadata

from .helpers import make_mobile_session


class FakeRefreshClient:
    """Fake SDK auth client for refresh contract tests."""

    def __init__(
        self,
        *,
        session: MobileSession | None = None,
        error: Exception | None = None,
    ) -> None:
        """Initialize the fake client."""
        self.session = session
        self.error = error
        self.refresh_calls: list[MobileSession] = []

    async def refresh_session(self, session: MobileSession) -> MobileSession:
        """Fake refresh behavior."""
        self.refresh_calls.append(session)
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        if self.session is None:
            raise AssertionError("FakeRefreshClient requires session or error")
        return self.session


def expired_session(
    *,
    refresh_token: str = "refresh-token",
) -> MobileSession:
    """Create a session inside the SDK expiry buffer."""
    now = datetime.now(UTC)
    identity_metadata = make_mobile_session("auth0|account-1").identity_metadata
    return MobileSession(
        access_token="old-access-token",
        id_token="old-id-token",
        refresh_token=refresh_token,
        identity_metadata=identity_metadata,
        token_metadata=TokenMetadata(
            issued_at_epoch=int((now - timedelta(hours=1)).timestamp()),
            expires_at_epoch=int((now - timedelta(minutes=1)).timestamp()),
            issuer=None,
            audience=(),
            scope=None,
            fingerprint="old-fingerprint",
        ),
    )


def active_session() -> MobileSession:
    """Create a session that does not require refresh."""
    now = datetime.now(UTC)
    identity_metadata = make_mobile_session("auth0|account-1").identity_metadata
    return MobileSession(
        access_token="active-access-token",
        id_token="active-id-token",
        refresh_token="refresh-token",
        identity_metadata=identity_metadata,
        token_metadata=TokenMetadata(
            issued_at_epoch=int((now - timedelta(minutes=1)).timestamp()),
            expires_at_epoch=int((now + timedelta(hours=1)).timestamp()),
            issuer=None,
            audience=(),
            scope=None,
            fingerprint="active-fingerprint",
        ),
    )


def make_entry(session: MobileSession) -> MockConfigEntry:
    """Create a config entry containing a serialized mobile session."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={
            CONF_ACCOUNT_ID: "auth0|account-1",
            CONF_SESSION: session.to_dict(),
        },
        state=ConfigEntryState.LOADED,
    )


def attach_runtime(
    entry: MockConfigEntry,
    *,
    session: MobileSession,
    client: FakeRefreshClient,
    account_id: str = "auth0|account-1",
) -> None:
    """Attach runtime data for refresh tests."""
    entry.runtime_data = SpGroupRuntimeData(
        auth_client=client,  # type: ignore[arg-type]
        jarvis_client=object(),  # type: ignore[arg-type]
        yggdrasil_client=object(),  # type: ignore[arg-type]
        profile_client=object(),  # type: ignore[arg-type]
        session=session,
        account_id=account_id,
        refresh_lock=asyncio.Lock(),
    )


async def test_refresh_helper_skips_active_session(hass: HomeAssistant) -> None:
    """Active sessions are returned without SDK refresh or config-entry updates."""
    session = active_session()
    entry = make_entry(session)
    entry.add_to_hass(hass)
    client = FakeRefreshClient(session=make_mobile_session("auth0|account-1"))
    hass.config_entries.async_update_entry = Mock()  # type: ignore[method-assign]

    result = await async_refresh_session_if_needed(
        hass,
        entry,
        client=client,  # type: ignore[arg-type]
    )

    assert result == session
    assert client.refresh_calls == []
    hass.config_entries.async_update_entry.assert_not_called()


async def test_runtime_refresh_skips_active_session(hass: HomeAssistant) -> None:
    """Active runtime sessions do not refresh or update config entries."""
    session = active_session()
    entry = make_entry(session)
    entry.add_to_hass(hass)
    client = FakeRefreshClient(session=make_mobile_session("auth0|account-1"))
    attach_runtime(entry, session=session, client=client)
    hass.config_entries.async_update_entry = Mock()  # type: ignore[method-assign]

    result = await async_refresh_runtime_session_if_needed(hass, entry)  # type: ignore[arg-type]

    assert result == session
    assert entry.runtime_data.session == session
    assert client.refresh_calls == []
    hass.config_entries.async_update_entry.assert_not_called()


async def test_refresh_helper_persists_refreshed_session(
    hass: HomeAssistant,
) -> None:
    """Expired sessions are refreshed and persisted back to the config entry."""
    old_session = expired_session()
    refreshed = make_mobile_session(
        "auth0|account-1",
        access_token="new-access-token",
    )
    entry = make_entry(old_session)
    entry.add_to_hass(hass)
    client = FakeRefreshClient(session=refreshed)
    hass.config_entries.async_update_entry = Mock()  # type: ignore[method-assign]

    result = await async_refresh_session_if_needed(
        hass,
        entry,
        client=client,  # type: ignore[arg-type]
    )

    assert result == refreshed
    assert client.refresh_calls == [old_session]
    hass.config_entries.async_update_entry.assert_called_once()
    _, kwargs = hass.config_entries.async_update_entry.call_args
    assert kwargs["data"][CONF_SESSION]["accessToken"] == "new-access-token"
    assert kwargs["data"][CONF_ACCOUNT_ID] == "auth0|account-1"


async def test_runtime_refresh_persists_and_updates_runtime_session(
    hass: HomeAssistant,
) -> None:
    """Expired runtime sessions refresh, persist, and update runtime state."""
    old_session = expired_session()
    refreshed = make_mobile_session(
        "auth0|account-2",
        access_token="new-access-token",
    )
    entry = make_entry(old_session)
    entry.add_to_hass(hass)
    client = FakeRefreshClient(session=refreshed)
    attach_runtime(entry, session=old_session, client=client)
    hass.config_entries.async_update_entry = Mock()  # type: ignore[method-assign]

    result = await async_refresh_runtime_session_if_needed(hass, entry)  # type: ignore[arg-type]

    assert result == refreshed
    assert entry.runtime_data.session == refreshed
    assert entry.runtime_data.account_id == "auth0|account-2"
    assert client.refresh_calls == [old_session]
    _, kwargs = hass.config_entries.async_update_entry.call_args
    assert kwargs["data"][CONF_SESSION]["accessToken"] == "new-access-token"
    assert kwargs["data"][CONF_ACCOUNT_ID] == "auth0|account-2"


async def test_runtime_refresh_coalesces_concurrent_calls(
    hass: HomeAssistant,
) -> None:
    """Concurrent runtime refresh attempts share one SDK refresh call."""
    old_session = expired_session()
    refreshed = active_session()
    entry = make_entry(old_session)
    entry.add_to_hass(hass)
    client = FakeRefreshClient(session=refreshed)
    attach_runtime(entry, session=old_session, client=client)
    hass.config_entries.async_update_entry = Mock()  # type: ignore[method-assign]

    first, second = await asyncio.gather(
        async_refresh_runtime_session_if_needed(hass, entry),  # type: ignore[arg-type]
        async_refresh_runtime_session_if_needed(hass, entry),  # type: ignore[arg-type]
    )

    assert first == refreshed
    assert second == refreshed
    assert entry.runtime_data.session == refreshed
    assert client.refresh_calls == [old_session]
    hass.config_entries.async_update_entry.assert_called_once()


async def test_runtime_refresh_preserves_existing_account_id_without_identity(
    hass: HomeAssistant,
) -> None:
    """A refreshed session without identity metadata keeps the existing account ID."""
    old_session = expired_session()
    refreshed = MobileSession(
        access_token="new-access-token",
        id_token="new-id-token",
        refresh_token="refresh-token",
        token_metadata=active_session().token_metadata,
    )
    entry = make_entry(old_session)
    entry.add_to_hass(hass)
    client = FakeRefreshClient(session=refreshed)
    attach_runtime(entry, session=old_session, client=client)
    hass.config_entries.async_update_entry = Mock()  # type: ignore[method-assign]

    result = await async_refresh_runtime_session_if_needed(hass, entry)  # type: ignore[arg-type]

    assert result == refreshed
    assert entry.runtime_data.account_id == "auth0|account-1"
    _, kwargs = hass.config_entries.async_update_entry.call_args
    assert kwargs["data"][CONF_ACCOUNT_ID] == "auth0|account-1"


@pytest.mark.parametrize(
    ("session", "client"),
    [
        (expired_session(refresh_token=""), FakeRefreshClient()),
        (
            expired_session(),
            FakeRefreshClient(error=SpGroupAuthError("invalid refresh token")),
        ),
        (
            expired_session(),
            FakeRefreshClient(
                session=MobileSession(
                    access_token="",
                    id_token="",
                    refresh_token="",
                )
            ),
        ),
    ],
)
async def test_refresh_helper_raises_auth_failed_for_unrecoverable_refresh(
    hass: HomeAssistant,
    session: MobileSession,
    client: Any,
) -> None:
    """Missing, revoked, invalid, or unusable refresh data triggers HA reauth."""
    entry = make_entry(session)
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryAuthFailed):
        await async_refresh_session_if_needed(
            hass,
            entry,
            client=client,
        )


@pytest.mark.parametrize(
    ("session", "client"),
    [
        (expired_session(refresh_token=""), FakeRefreshClient()),
        (
            expired_session(),
            FakeRefreshClient(error=SpGroupAuthError("invalid refresh token")),
        ),
        (
            expired_session(),
            FakeRefreshClient(
                session=MobileSession(
                    access_token="",
                    id_token="",
                    refresh_token="",
                )
            ),
        ),
    ],
)
async def test_runtime_refresh_raises_auth_failed_for_unrecoverable_refresh(
    hass: HomeAssistant,
    session: MobileSession,
    client: FakeRefreshClient,
) -> None:
    """Runtime refresh failures trigger HA reauth."""
    entry = make_entry(session)
    entry.add_to_hass(hass)
    attach_runtime(entry, session=session, client=client)

    with pytest.raises(ConfigEntryAuthFailed):
        await async_refresh_runtime_session_if_needed(hass, entry)  # type: ignore[arg-type]


async def test_unrecoverable_refresh_does_not_create_repair_issue(
    hass: HomeAssistant,
) -> None:
    """Auth refresh failures use HA reauth, not duplicate repair issues."""
    session = expired_session()
    entry = make_entry(session)
    entry.add_to_hass(hass)
    client = FakeRefreshClient(error=SpGroupAuthError("invalid refresh token"))
    attach_runtime(entry, session=session, client=client)

    with patch("homeassistant.helpers.issue_registry.async_create_issue") as create:
        with pytest.raises(ConfigEntryAuthFailed):
            await async_refresh_runtime_session_if_needed(hass, entry)  # type: ignore[arg-type]

    create.assert_not_called()
