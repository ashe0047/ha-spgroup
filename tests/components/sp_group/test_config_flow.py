"""Test the SP Group config flow."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.sp_group.backfill import StatisticsBackfillScheduleStatus
from custom_components.sp_group.config_flow import ConfigFlow, OptionsFlowHandler
from custom_components.sp_group.const import (
    CONF_ACCOUNT_ID,
    CONF_APPROXIMATE_SEED_CONFIRM,
    CONF_BACKFILL_FROM,
    CONF_DEV_AUTH_METHOD,
    CONF_DEV_SESSION_CONFIRM,
    CONF_ENABLED_UTILITIES,
    CONF_MAINTENANCE_ACTION,
    CONF_NO_SOURCES_CONFIRM,
    CONF_REBUILD_LEDGER_CONFIRM,
    CONF_RESET_REPLACEMENT,
    CONF_SEED_OBSERVED_AT,
    CONF_SEED_VALUE,
    CONF_SESSION,
    CONF_SOURCE_CONFIG,
    CONF_STALE_FLOW_RESET,
    DOMAIN,
)
from custom_components.sp_group.discovery import (
    DiscoveryDataError,
    SpGroupCoordinatorData,
    SpGroupPremise,
    SpGroupUtilityAccount,
)
from custom_components.sp_group.ledger import SpGroupLedgerEntry, SpGroupLedgerSnapshot
from custom_components.sp_group.sources import SINGAPORE_TZ, build_source_config
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import selector
from pytest_homeassistant_custom_component.common import MockConfigEntry
from spgroup.mobile import MfaRequired, MobileSession, TokenMetadata
from spgroup.mobile.errors import SpGroupAuthError, SpGroupMobileError

from .helpers import make_mobile_session
from .test_coordinator import FakeJarvisClient, FakeYggdrasilClient

USER_INPUT = {
    CONF_USERNAME: "user@example.com",
    CONF_PASSWORD: "secret-password",
}


class FakeAuthClient:
    """SDK auth client fake for config flow tests."""

    def __init__(
        self,
        *,
        session: MobileSession | None = None,
        login_error: Exception | None = None,
        authenticators: list[dict[str, Any]] | None = None,
        challenge_errors: dict[str | None, Exception] | None = None,
        otp_session: MobileSession | None = None,
        refresh_session: MobileSession | None = None,
        refresh_error: Exception | None = None,
    ) -> None:
        """Initialize the fake client."""
        self.session = session
        self.login_error = login_error
        self.authenticators = authenticators or []
        self.challenge_errors = challenge_errors or {}
        self.otp_session = otp_session or session
        self.refresh_session_value = refresh_session or session
        self.refresh_error = refresh_error
        self.login_calls: list[tuple[str, str]] = []
        self.challenge_calls: list[str | None] = []
        self.otp_calls: list[tuple[str, str, str]] = []
        self.refresh_calls: list[MobileSession] = []

    async def attempt_password_realm_login(
        self,
        username: str,
        password: str,
    ) -> MobileSession:
        """Fake password-realm login."""
        self.login_calls.append((username, password))
        if self.login_error is not None:
            raise self.login_error
        if self.session is None:
            raise AssertionError("FakeAuthClient requires session or login_error")
        return self.session

    async def get_mfa_authenticators(
        self,
        mfa_token: str,
    ) -> list[dict[str, Any]]:
        """Return fake authenticators."""
        return self.authenticators

    async def trigger_mfa_challenge(
        self,
        mfa_token: str,
        *,
        authenticator_id: str | None = None,
    ) -> SimpleNamespace:
        """Record and fake an MFA challenge."""
        self.challenge_calls.append(authenticator_id)
        error = self.challenge_errors.get(authenticator_id)
        if error is not None:
            raise error
        return SimpleNamespace(oob_code=f"oob-{authenticator_id or 'default'}")

    async def complete_mfa_oob(
        self,
        mfa_token: str,
        oob_code: str,
        otp: str,
    ) -> MobileSession:
        """Record and fake MFA completion."""
        self.otp_calls.append((mfa_token, oob_code, otp))
        if self.otp_session is None:
            raise SpGroupAuthError("invalid otp")
        return self.otp_session

    async def refresh_session(self, session: MobileSession) -> MobileSession:
        """Record and fake refresh-token renewal."""
        self.refresh_calls.append(session)
        if self.refresh_error is not None:
            raise self.refresh_error
        if self.refresh_session_value is None:
            raise AssertionError("FakeAuthClient requires refresh_session")
        return self.refresh_session_value


@pytest.fixture
def patch_auth_client() -> Any:
    """Patch the config flow SDK client factory."""

    def _patch(client: FakeAuthClient) -> Any:
        async def _factory(hass: HomeAssistant) -> FakeAuthClient:
            return client

        return patch(
            "custom_components.sp_group.config_flow._async_get_auth_client",
            _factory,
        )

    return _patch


@pytest.fixture(autouse=True)
def patch_setup_entry() -> Any:
    """Keep config-flow tests focused on flow behavior, not runtime setup."""
    with patch(
        "custom_components.sp_group.async_setup_entry",
        AsyncMock(return_value=True),
    ):
        yield


@pytest.fixture(autouse=True)
def patch_discovery_clients() -> Any:
    """Patch discovery clients used after successful authentication."""

    async def _factory(
        hass: HomeAssistant,
    ) -> tuple[FakeYggdrasilClient, FakeJarvisClient]:
        calls: list[str] = []
        return FakeYggdrasilClient(calls), FakeJarvisClient(calls)

    with patch(
        "custom_components.sp_group.config_flow._async_get_discovery_clients",
        _factory,
    ):
        yield


async def _start_user_flow(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )


def _schema_keys(result: dict[str, Any]) -> list[str]:
    return [
        getattr(key, "schema", key)
        for key in result["data_schema"].schema
        if isinstance(getattr(key, "schema", key), str)
    ]


def _schema_validators(result: dict[str, Any]) -> dict[str, Any]:
    return {
        getattr(key, "schema", key): validator
        for key, validator in result["data_schema"].schema.items()
    }


def _select_options(result: dict[str, Any], key: str) -> list[dict[str, str]]:
    select = _schema_validators(result)[key]
    return list(select.config["options"])


def _write_session_cache(path: Any, session: MobileSession) -> None:
    path.write_text(json.dumps(session.to_dict()), encoding="utf-8")


def _expired_session(subject: str) -> MobileSession:
    return MobileSession(
        access_token="expired-access-token",
        id_token="expired-id-token",
        refresh_token="refresh-token",
        token_metadata=TokenMetadata(
            issued_at_epoch=1,
            expires_at_epoch=2,
            issuer="issuer",
            audience=("audience",),
            scope=None,
            fingerprint="expired-token",
        ),
        identity_metadata=make_mobile_session(subject).identity_metadata,
        created_at=datetime.fromtimestamp(1, tz=UTC),
    )


async def _finish_with_first_seed(
    hass: HomeAssistant,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Select one discovered utility and provide a seed."""
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"
    source_key = _select_options(result, CONF_ENABLED_UTILITIES)[0]["value"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ENABLED_UTILITIES: [source_key]},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "seed"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SEED_VALUE: "12345.6",
            CONF_SEED_OBSERVED_AT: "2026-07-15T00:00:00+08:00",
            CONF_BACKFILL_FROM: "2026-07-15T00:00:00+08:00",
        },
    )


async def test_initial_form_renders(hass: HomeAssistant) -> None:
    """The user step renders before credentials are submitted."""
    result = await _start_user_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    validators = _schema_validators(result)
    assert isinstance(validators[CONF_USERNAME], selector.TextSelector)
    assert validators[CONF_USERNAME].config["type"] == "email"
    assert isinstance(validators[CONF_PASSWORD], selector.TextSelector)
    assert validators[CONF_PASSWORD].config["type"] == "password"


async def test_dev_auth_choice_renders_only_when_enabled(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cached-session bypass is hidden unless the dev env gate is enabled."""
    monkeypatch.setenv("SPGROUP_HA_DEV_AUTH_BYPASS", "1")

    result = await _start_user_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dev_auth_choice"
    validators = _schema_validators(result)
    assert isinstance(validators[CONF_DEV_AUTH_METHOD], selector.SelectSelector)
    assert {
        option["value"] for option in _select_options(result, CONF_DEV_AUTH_METHOD)
    } == {"cached_session", "password_auth"}


async def test_dev_auth_choice_password_routes_to_normal_login(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    patch_auth_client: Any,
) -> None:
    """The dev chooser can still route through the normal credential flow."""
    monkeypatch.setenv("SPGROUP_HA_DEV_AUTH_BYPASS", "1")
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEV_AUTH_METHOD: "password_auth"},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"
    assert client.login_calls == [("user@example.com", "secret-password")]


async def test_dev_cached_session_active_reaches_sources_without_auth(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """An active cached dev session bypasses password and OTP auth."""
    session_file = tmp_path / "mobile-session.json"
    _write_session_cache(session_file, make_mobile_session("auth0|account-1"))
    monkeypatch.setenv("SPGROUP_HA_DEV_AUTH_BYPASS", "1")
    monkeypatch.setenv("SPGROUP_HA_DEV_SESSION_FILE", str(session_file))

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEV_AUTH_METHOD: "cached_session"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dev_session_confirm"
    validators = _schema_validators(result)
    assert isinstance(validators[CONF_DEV_SESSION_CONFIRM], selector.BooleanSelector)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEV_SESSION_CONFIRM: True},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"


async def test_dev_cached_session_expired_refreshes_and_writes_cache(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    patch_auth_client: Any,
) -> None:
    """Expired cached dev sessions refresh before the post-auth flow continues."""
    session_file = tmp_path / "mobile-session.json"
    expired = _expired_session("auth0|account-1")
    refreshed = make_mobile_session(
        "auth0|account-1",
        access_token="refreshed-access-token",
    )
    _write_session_cache(session_file, expired)
    monkeypatch.setenv("SPGROUP_HA_DEV_AUTH_BYPASS", "1")
    monkeypatch.setenv("SPGROUP_HA_DEV_SESSION_FILE", str(session_file))
    client = FakeAuthClient(refresh_session=refreshed)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEV_AUTH_METHOD: "cached_session"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEV_SESSION_CONFIRM: True},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"
    assert client.refresh_calls == [expired]
    cached = json.loads(session_file.read_text(encoding="utf-8"))
    assert cached["accessToken"] == "refreshed-access-token"
    assert stat.S_IMODE(session_file.stat().st_mode) == 0o600


async def test_dev_cached_session_file_io_uses_executor(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    patch_auth_client: Any,
) -> None:
    """Cached dev session file reads and writes are kept off the event loop."""
    session_file = tmp_path / "mobile-session.json"
    expired = _expired_session("auth0|account-1")
    refreshed = make_mobile_session(
        "auth0|account-1",
        access_token="refreshed-access-token",
    )
    _write_session_cache(session_file, expired)
    monkeypatch.setenv("SPGROUP_HA_DEV_AUTH_BYPASS", "1")
    monkeypatch.setenv("SPGROUP_HA_DEV_SESSION_FILE", str(session_file))
    client = FakeAuthClient(refresh_session=refreshed)
    executor_calls: list[str] = []
    original_executor = hass.async_add_executor_job

    async def _executor_spy(func: Any, *args: Any) -> Any:
        executor_calls.append(getattr(func, "__name__", repr(func)))
        return await original_executor(func, *args)

    monkeypatch.setattr(hass, "async_add_executor_job", _executor_spy)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEV_AUTH_METHOD: "cached_session"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEV_SESSION_CONFIRM: True},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"
    assert "_load_dev_cached_session" in executor_calls
    assert "_write_dev_cached_session" in executor_calls


@pytest.mark.parametrize(
    ("cache_payload", "expected_error"),
    [
        ("not-json", "dev_session_malformed"),
        ([], "dev_session_invalid"),
        (
            {
                "accessToken": "access-token",
                "idToken": "id-token",
                "refreshToken": "refresh-token",
            },
            "dev_session_missing_identity",
        ),
        (
            make_mobile_session("auth0|account-1", refresh_token="").to_dict(),
            "dev_session_missing_refresh_token",
        ),
    ],
)
async def test_dev_cached_session_validation_errors(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    cache_payload: Any,
    expected_error: str,
) -> None:
    """Cached dev session failures produce explicit dev-only form errors."""
    session_file = tmp_path / "mobile-session.json"
    if isinstance(cache_payload, str):
        session_file.write_text(cache_payload, encoding="utf-8")
    else:
        session_file.write_text(json.dumps(cache_payload), encoding="utf-8")
    monkeypatch.setenv("SPGROUP_HA_DEV_AUTH_BYPASS", "1")
    monkeypatch.setenv("SPGROUP_HA_DEV_SESSION_FILE", str(session_file))

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEV_AUTH_METHOD: "cached_session"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEV_SESSION_CONFIRM: True},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dev_session_confirm"
    assert result["errors"] == {"base": expected_error}


async def test_dev_cached_session_missing_file_error(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """A missing cached session file is reported clearly."""
    monkeypatch.setenv("SPGROUP_HA_DEV_AUTH_BYPASS", "1")
    monkeypatch.setenv(
        "SPGROUP_HA_DEV_SESSION_FILE",
        str(tmp_path / "missing-session.json"),
    )

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEV_AUTH_METHOD: "cached_session"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEV_SESSION_CONFIRM: True},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dev_session_confirm"
    assert result["errors"] == {"base": "dev_session_missing"}


async def test_dev_cached_session_refresh_failure_error(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    patch_auth_client: Any,
) -> None:
    """An expired cache that cannot refresh does not fall back to real auth."""
    session_file = tmp_path / "mobile-session.json"
    _write_session_cache(session_file, _expired_session("auth0|account-1"))
    monkeypatch.setenv("SPGROUP_HA_DEV_AUTH_BYPASS", "1")
    monkeypatch.setenv("SPGROUP_HA_DEV_SESSION_FILE", str(session_file))
    client = FakeAuthClient(refresh_error=SpGroupAuthError("revoked refresh token"))

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEV_AUTH_METHOD: "cached_session"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEV_SESSION_CONFIRM: True},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dev_session_confirm"
    assert result["errors"] == {"base": "dev_session_refresh_failed"}
    assert client.login_calls == []


async def test_dev_cached_session_create_entry_excludes_dev_fields(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """The dev bypass stores the same safe config-entry data as real auth."""
    session_file = tmp_path / "mobile-session.json"
    _write_session_cache(session_file, make_mobile_session("auth0|account-1"))
    monkeypatch.setenv("SPGROUP_HA_DEV_AUTH_BYPASS", "1")
    monkeypatch.setenv("SPGROUP_HA_DEV_SESSION_FILE", str(session_file))

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEV_AUTH_METHOD: "cached_session"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEV_SESSION_CONFIRM: True},
    )
    result = await _finish_with_first_seed(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_DEV_AUTH_METHOD not in result["data"]
    assert CONF_DEV_SESSION_CONFIRM not in result["data"]
    assert CONF_USERNAME not in result["data"]
    assert CONF_PASSWORD not in result["data"]


async def test_dev_cached_session_duplicate_configured_aborts(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Configured accounts still abort after cached-session auth."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.add_to_hass(hass)
    session_file = tmp_path / "mobile-session.json"
    _write_session_cache(session_file, make_mobile_session("auth0|account-1"))
    monkeypatch.setenv("SPGROUP_HA_DEV_AUTH_BYPASS", "1")
    monkeypatch.setenv("SPGROUP_HA_DEV_SESSION_FILE", str(session_file))

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEV_AUTH_METHOD: "cached_session"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEV_SESSION_CONFIRM: True},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_password_login_creates_entry_without_secrets(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Successful password login stores only account ID and serialized session."""
    session = make_mobile_session("auth0|account-1", refresh_token="refresh-token")
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
        result = await _finish_with_first_seed(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "SP Group"
    assert result["data"][CONF_ACCOUNT_ID] == "auth0|account-1"
    assert result["data"][CONF_SESSION]["refreshToken"] == "refresh-token"
    assert result["data"][CONF_SESSION]["identityMetadata"]["subject"] == (
        "auth0|account-1"
    )
    assert len(result["options"][CONF_SOURCE_CONFIG]) == 1
    assert CONF_USERNAME not in result["data"]
    assert CONF_PASSWORD not in result["data"]
    assert "secret-password" not in repr(result["data"])
    assert "mfa-token" not in repr(result["data"])
    assert "oob-" not in repr(result["data"])


async def test_source_selection_uses_user_friendly_premise_labels(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Source selection shows premise labels instead of opaque keys."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    validators = _schema_validators(result)
    assert isinstance(validators[CONF_ENABLED_UTILITIES], selector.SelectSelector)
    options = _select_options(result, CONF_ENABLED_UTILITIES)
    labels = [option["label"] for option in options]
    assert "123 Secret Street - Electricity (kWh)" in labels
    assert "123 Secret Street - Water (m³)" in labels
    assert not any(label.startswith("sp_group_premise_") for label in labels)
    assert not any(
        option["value"].startswith("123 Secret Street") for option in options
    )


async def test_seed_step_uses_number_and_datetime_selectors(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Seed entry uses purpose-built HA selectors."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
        source_key = _select_options(result, CONF_ENABLED_UTILITIES)[0]["value"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENABLED_UTILITIES: [source_key]},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "seed"
    validators = _schema_validators(result)
    assert isinstance(validators["seed_value"], selector.NumberSelector)
    assert validators["seed_value"].config["mode"] == "box"
    assert validators["seed_value"].config["unit_of_measurement"] == "kWh"
    assert isinstance(validators["seed_observed_at"], selector.DateTimeSelector)
    assert isinstance(validators["backfill_from"], selector.DateTimeSelector)


async def test_seed_timestamp_without_timezone_uses_singapore_time(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Date-time picker values without timezone are stored as Singapore time."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
        source_key = _select_options(result, CONF_ENABLED_UTILITIES)[0]["value"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENABLED_UTILITIES: [source_key]},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_SEED_VALUE: 12345.6,
                CONF_SEED_OBSERVED_AT: datetime(2026, 7, 15, 0, 0),
                CONF_BACKFILL_FROM: datetime(2026, 7, 15, 0, 0),
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    source_config = result["options"][CONF_SOURCE_CONFIG][source_key]
    assert source_config["seed_observed_at"] == "2026-07-15T00:00:00+08:00"


async def test_seed_datetime_selector_split_payload_is_normalized(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """HA frontend split date/time payloads are accepted as picker values."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
        source_key = _select_options(result, CONF_ENABLED_UTILITIES)[0]["value"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENABLED_UTILITIES: [source_key]},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_SEED_VALUE: 12345.6,
                CONF_SEED_OBSERVED_AT: "2026-07-15T00:00:00 22:00:00",
                CONF_BACKFILL_FROM: "2026-07-15T00:00:00+08:00",
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    source_config = result["options"][CONF_SOURCE_CONFIG][source_key]
    assert source_config["seed_observed_at"] == "2026-07-15T22:00:00+08:00"
    assert source_config["seed_effective_from"] == "2026-07-15T22:00:00+08:00"


async def test_approximate_seed_requires_confirmation(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Non-boundary seed timestamps require explicit approximate confirmation."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
        source_key = _select_options(result, CONF_ENABLED_UTILITIES)[0]["value"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENABLED_UTILITIES: [source_key]},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_SEED_VALUE: 12345.6,
                CONF_SEED_OBSERVED_AT: "2026-07-15T00:17:00+08:00",
                CONF_BACKFILL_FROM: "2026-07-15T00:00:00+08:00",
            },
        )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "approximate_seed_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_APPROXIMATE_SEED_CONFIRM: True},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    source_config = result["options"][CONF_SOURCE_CONFIG][source_key]
    assert source_config["boundary_aligned"] is False
    assert source_config["seed_effective_from"] == "2026-07-15T00:30:00+08:00"


async def test_declined_approximate_seed_returns_to_seed_form(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Declining approximate seed confirmation does not store source config."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
        source_key = _select_options(result, CONF_ENABLED_UTILITIES)[0]["value"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENABLED_UTILITIES: [source_key]},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_SEED_VALUE: 12345.6,
                CONF_SEED_OBSERVED_AT: "2026-07-15T00:17:00+08:00",
                CONF_BACKFILL_FROM: "2026-07-15T00:00:00+08:00",
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_APPROXIMATE_SEED_CONFIRM: False},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "seed"
    assert result["errors"] == {"base": "approximate_seed_not_confirmed"}


async def test_empty_source_selection_requires_confirmation(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Creating a discovery-only entry requires a confirmation step."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENABLED_UTILITIES: []},
        )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "no_sources_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NO_SOURCES_CONFIRM: True},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_SOURCE_CONFIG] == {}


async def test_seed_validation_errors_are_field_specific(
) -> None:
    """Seed validation errors attach to the field the user needs to fix."""
    data = _single_electric_discovery()
    sensor_key = data.future_sensor_keys()[0]
    flow = ConfigFlow()
    flow._selected_sensor_keys = [sensor_key]
    flow._seed_labels = {sensor_key.key: "123 Secret Street - Electricity (kWh)"}

    result = await flow.async_step_seed(
        {
            CONF_SEED_VALUE: "not-a-number",
            CONF_SEED_OBSERVED_AT: "2026-07-15T00:00:00+08:00",
            CONF_BACKFILL_FROM: "2026-07-15T00:00:00+08:00",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "seed"
    assert result["errors"] == {CONF_SEED_VALUE: "invalid_decimal"}

    result = await flow.async_step_seed(
        {
            CONF_SEED_VALUE: 100,
            CONF_SEED_OBSERVED_AT: "not-a-date",
            CONF_BACKFILL_FROM: "2026-07-15T00:00:00+08:00",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "seed"
    assert result["errors"] == {CONF_SEED_OBSERVED_AT: "invalid_timestamp"}

    result = await flow.async_step_seed(
        {
            CONF_SEED_VALUE: 100,
            CONF_SEED_OBSERVED_AT: "2026-07-15T00:00:00+08:00",
            CONF_BACKFILL_FROM: "2026-07-15T00:30:00+08:00",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "seed"
    assert result["errors"] == {CONF_BACKFILL_FROM: "backfill_after_seed"}

    result = await flow.async_step_seed(
        {
            CONF_SEED_VALUE: 100,
            CONF_SEED_OBSERVED_AT: "2026-07-15T00:00:00+08:00",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "seed"
    assert result["errors"] == {CONF_BACKFILL_FROM: "missing_backfill_from"}


async def test_discovery_failure_shows_discovery_failed(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Discovery correlation/shape failures get their own user-facing error."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    async def _raise_discovery_error(**_: Any) -> SpGroupCoordinatorData:
        raise DiscoveryDataError("could not correlate discovery data")

    with (
        patch_auth_client(client),
        patch(
            "custom_components.sp_group.config_flow.async_fetch_discovery_data",
            _raise_discovery_error,
        ),
    ):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "discovery_failed"}


async def test_no_supported_utilities_aborts(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Setup aborts cleanly when discovery has no supported utility keys."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    async def _empty_discovery(**_: Any) -> SpGroupCoordinatorData:
        return SpGroupCoordinatorData()

    with (
        patch_auth_client(client),
        patch(
            "custom_components.sp_group.config_flow.async_fetch_discovery_data",
            _empty_discovery,
        ),
    ):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_supported_utilities"


async def test_missing_identity_returns_invalid_auth(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """A token response without a stable ID-token subject is rejected."""
    session = MobileSession(
        access_token="access-token",
        id_token="id-token",
        refresh_token="refresh-token",
    )
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_duplicate_setup_aborts(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """A second setup for the same account aborts as already configured."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.add_to_hass(hass)
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_password_login_duplicate_in_progress_requires_reset(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """A hidden same-account setup flow is reset explicitly, not raw-aborted."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        stale_result = await _start_user_flow(hass)
        stale_result = await hass.config_entries.flow.async_configure(
            stale_result["flow_id"],
            USER_INPUT,
        )
        assert stale_result["type"] is FlowResultType.FORM
        assert stale_result["step_id"] == "sources"
        stale_flow_id = stale_result["flow_id"]

        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "stale_flow_reset_confirm"
        validators = _schema_validators(result)
        assert isinstance(validators[CONF_STALE_FLOW_RESET], selector.BooleanSelector)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_STALE_FLOW_RESET: True},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"
    progress_ids = {
        progress["flow_id"]
        for progress in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    }
    assert stale_flow_id not in progress_ids


async def test_declining_stale_flow_reset_leaves_old_flow(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Declining reset aborts only the current flow and preserves the stale one."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(session=session)

    with patch_auth_client(client):
        stale_result = await _start_user_flow(hass)
        stale_result = await hass.config_entries.flow.async_configure(
            stale_result["flow_id"],
            USER_INPUT,
        )
        stale_flow_id = stale_result["flow_id"]

        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_STALE_FLOW_RESET: False},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "stale_flow_not_reset"
    progress_ids = {
        progress["flow_id"]
        for progress in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    }
    assert stale_flow_id in progress_ids


async def test_otp_duplicate_in_progress_requires_reset(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """The MFA path also routes stale setup conflicts to reset confirmation."""
    session = make_mobile_session("auth0|account-1")
    stale_client = FakeAuthClient(session=session)

    with patch_auth_client(stale_client):
        stale_result = await _start_user_flow(hass)
        stale_result = await hass.config_entries.flow.async_configure(
            stale_result["flow_id"],
            USER_INPUT,
        )
    assert stale_result["type"] is FlowResultType.FORM
    assert stale_result["step_id"] == "sources"

    mfa_client = FakeAuthClient(
        login_error=MfaRequired("mfa-token"),
        authenticators=[
            {"id": "email-auth", "oob_channel": "email", "active": True},
        ],
        otp_session=session,
    )

    with patch_auth_client(mfa_client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "otp"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"otp": "123456"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "stale_flow_reset_confirm"
    assert mfa_client.otp_calls == [("mfa-token", "oob-email-auth", "123456")]


async def test_mfa_prefers_email_and_completes_otp(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """MFA selects the active email authenticator before OTP completion."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(
        login_error=MfaRequired("mfa-token"),
        authenticators=[
            {"id": "sms-auth", "oob_channel": "sms", "active": True},
            {"id": "email-auth", "oob_channel": "email", "active": True},
        ],
        otp_session=session,
    )

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "otp"
        validators = _schema_validators(result)
        assert isinstance(validators["otp"], selector.TextSelector)
        assert validators["otp"].config["type"] == "tel"
        assert client.challenge_calls == ["email-auth"]

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"otp": "123456"},
        )
        result = await _finish_with_first_seed(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCOUNT_ID] == "auth0|account-1"
    assert client.otp_calls == [("mfa-token", "oob-email-auth", "123456")]
    assert "mfa-token" not in repr(result["data"])
    assert "oob-email-auth" not in repr(result["data"])


async def test_mfa_email_challenge_error_falls_back_to_sms(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """An email challenge error, including rate limits, falls back to SMS."""
    session = make_mobile_session("auth0|account-1")
    client = FakeAuthClient(
        login_error=MfaRequired("mfa-token"),
        authenticators=[
            {"id": "email-auth", "oob_channel": "email", "active": True},
            {"id": "sms-auth", "oob_channel": "sms", "active": True},
        ],
        challenge_errors={
            "email-auth": SpGroupAuthError("rate limited", status=429),
        },
        otp_session=session,
    )

    with patch_auth_client(client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"otp": "123456"},
        )
        result = await _finish_with_first_seed(hass, result)

    assert client.challenge_calls == ["email-auth", "sms-auth"]
    assert client.otp_calls == [("mfa-token", "oob-sms-auth", "123456")]
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_invalid_auth_and_cannot_connect_errors(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Auth and transport failures map to separate form errors."""
    invalid_client = FakeAuthClient(login_error=SpGroupAuthError("bad password"))
    with patch_auth_client(invalid_client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
    assert result["errors"] == {"base": "invalid_auth"}

    connect_client = FakeAuthClient(login_error=SpGroupMobileError("timeout"))
    with patch_auth_client(connect_client):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_updates_existing_entry(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Successful reauth updates the existing entry and does not create another."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={
            CONF_ACCOUNT_ID: "auth0|account-1",
            CONF_SESSION: make_mobile_session(
                "auth0|account-1",
                access_token="old-access-token",
            ).to_dict(),
        },
    )
    entry.add_to_hass(hass)
    client = FakeAuthClient(
        session=make_mobile_session(
            "auth0|account-1",
            access_token="new-access-token",
        )
    )

    with patch_auth_client(client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data[CONF_SESSION]["accessToken"] == "new-access-token"
    assert CONF_USERNAME not in entry.data
    assert CONF_PASSWORD not in entry.data


async def test_reauth_wrong_account_aborts(
    hass: HomeAssistant,
    patch_auth_client: Any,
) -> None:
    """Reauth must not update an entry with another account's tokens."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={
            CONF_ACCOUNT_ID: "auth0|account-1",
            CONF_SESSION: make_mobile_session("auth0|account-1").to_dict(),
        },
    )
    entry.add_to_hass(hass)
    client = FakeAuthClient(session=make_mobile_session("auth0|account-2"))

    with patch_auth_client(client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_ACCOUNT_ID] == "auth0|account-1"


async def test_options_flow_confirms_lower_replacement() -> None:
    """Lower reseed requires explicit reset/replacement confirmation."""
    data = SpGroupCoordinatorData(
        premises=(
            SpGroupPremise(
                key="opaque_premise",
                active=True,
                signed_up=True,
                shared=False,
                contestable_type=None,
                accounts=(
                    SpGroupUtilityAccount(
                        key="opaque_account",
                        premise_key="opaque_premise",
                        utility_types=("ELECTRIC",),
                    ),
                ),
                display_label="123 Secret Street",
            ),
        )
    )
    sensor_key = data.future_sensor_keys()[0]
    existing_config = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-14T13:00:00+08:00",
        backfill_from="2026-07-14T13:00:00+08:00",
        now=datetime(2026, 7, 14, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        options={
            CONF_SOURCE_CONFIG: {sensor_key.key: existing_config.to_dict()},
        },
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(data=data),
        ledger_store=SimpleNamespace(
            snapshot=SpGroupLedgerSnapshot(
                entries={
                    sensor_key.key: SpGroupLedgerEntry(
                        sensor_key=sensor_key.key,
                        source=existing_config.source,
                        utility_type=existing_config.utility_type,
                        epoch_id=existing_config.epoch_id,
                        seed_value=Decimal("100.0"),
                        seed_effective_from=existing_config.seed_effective_from,
                        backfill_from=existing_config.backfill_from,
                        latest_total=Decimal("100.0"),
                    )
                }
            )
        ),
    )
    flow = OptionsFlowHandler(entry)

    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    validators = _schema_validators(result)
    assert isinstance(validators[CONF_ENABLED_UTILITIES], selector.SelectSelector)
    assert isinstance(validators[CONF_MAINTENANCE_ACTION], selector.SelectSelector)
    result = await flow.async_step_init(
        {
            CONF_ENABLED_UTILITIES: [sensor_key.key],
            CONF_MAINTENANCE_ACTION: f"reseed:{sensor_key.key}",
        }
    )
    assert result["step_id"] == "seed"
    result = await flow.async_step_seed(
        {
            CONF_SEED_VALUE: "50.0",
            CONF_SEED_OBSERVED_AT: "2026-07-14T13:30:00+08:00",
            CONF_BACKFILL_FROM: "2026-07-14T13:30:00+08:00",
        }
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reset_confirm"

    result = await flow.async_step_reset_confirm({CONF_RESET_REPLACEMENT: True})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    source_config = result["data"][CONF_SOURCE_CONFIG][sensor_key.key]
    assert source_config["seed_value"] == "50.0"
    assert source_config["reset_confirmed"] is True


async def test_options_flow_retry_statistics_import_uses_persisted_ledger_only(
    hass: HomeAssistant,
) -> None:
    """The options retry imports statistics without re-fetching provider history."""
    data = _single_electric_discovery()
    sensor_key = data.future_sensor_keys()[0]
    existing_config = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-14T13:00:00+08:00",
        backfill_from="2026-07-14T13:00:00+08:00",
        now=datetime(2026, 7, 14, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    backfill_tasks: set[Any] = set()
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        options={CONF_SOURCE_CONFIG: {sensor_key.key: existing_config.to_dict()}},
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(data=data),
        ledger_store=SimpleNamespace(snapshot=SpGroupLedgerSnapshot()),
        backfill_tasks=backfill_tasks,
    )
    flow = OptionsFlowHandler(entry)
    flow.hass = hass

    with (
        patch(
            "custom_components.sp_group.config_flow.async_schedule_statistics_backfill",
        ) as schedule_stats_backfill,
        patch(
            "custom_components.sp_group.backfill.async_run_backfill",
            new_callable=AsyncMock,
        ) as source_backfill,
    ):
        schedule_stats_backfill.return_value = (
            StatisticsBackfillScheduleStatus.SCHEDULED
        )
        result = await flow.async_step_init(
            {
                CONF_ENABLED_UTILITIES: [sensor_key.key],
                CONF_MAINTENANCE_ACTION: "retry_statistics_backfill",
            }
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "statistics_import_scheduled"
    schedule_stats_backfill.assert_called_once_with(
        hass,
        entry,
        name="sp_group_options_statistics_backfill",
        force=True,
    )
    source_backfill.assert_not_awaited()
    assert backfill_tasks == set()


async def test_options_flow_retry_source_backfill_schedules_source_path(
    hass: HomeAssistant,
) -> None:
    """The source retry action schedules provider-history backfill."""
    data = _single_electric_discovery()
    sensor_key = data.future_sensor_keys()[0]
    existing_config = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-14T13:00:00+08:00",
        backfill_from="2026-07-14T13:00:00+08:00",
        now=datetime(2026, 7, 14, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        options={CONF_SOURCE_CONFIG: {sensor_key.key: existing_config.to_dict()}},
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(data=data),
        ledger_store=SimpleNamespace(snapshot=SpGroupLedgerSnapshot()),
        backfill_tasks=set(),
    )
    flow = OptionsFlowHandler(entry)
    flow.hass = hass

    with (
        patch(
            "custom_components.sp_group.config_flow.async_start_initial_backfill",
        ) as start_source_backfill,
        patch(
            "custom_components.sp_group.config_flow.async_schedule_statistics_backfill",
        ) as schedule_stats_backfill,
    ):
        result = await flow.async_step_init(
            {
                CONF_ENABLED_UTILITIES: [sensor_key.key],
                CONF_MAINTENANCE_ACTION: "retry_source_backfill",
            }
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "source_backfill_scheduled"
    start_source_backfill.assert_called_once_with(hass, entry)
    schedule_stats_backfill.assert_not_called()


async def test_options_flow_rebuilds_seeded_ledger() -> None:
    """A confirmed rebuild resets the selected ledger entry from its seed."""
    data = _single_electric_discovery()
    sensor_key = data.future_sensor_keys()[0]
    existing_config = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-14T13:00:00+08:00",
        backfill_from="2026-07-14T13:00:00+08:00",
        now=datetime(2026, 7, 14, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    ledger_store = SimpleNamespace(
        snapshot=SpGroupLedgerSnapshot(),
        async_rebuild_sources=AsyncMock(),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        entry_id="entry-rebuild",
        options={CONF_SOURCE_CONFIG: {sensor_key.key: existing_config.to_dict()}},
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(data=data),
        ledger_store=ledger_store,
    )
    flow = OptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_reload=AsyncMock())
    )

    result = await flow.async_step_init(
        {
            CONF_ENABLED_UTILITIES: [sensor_key.key],
            CONF_MAINTENANCE_ACTION: f"rebuild:{sensor_key.key}",
        }
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "rebuild_confirm"

    result = await flow.async_step_rebuild_confirm(
        {CONF_REBUILD_LEDGER_CONFIRM: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    ledger_store.async_rebuild_sources.assert_awaited_once_with(
        {sensor_key.key: existing_config},
        sensor_keys={sensor_key.key},
    )
    flow.hass.config_entries.async_reload.assert_awaited_once_with("entry-rebuild")


async def test_options_flow_maintenance_none_updates_enabled_selection() -> None:
    """Options can update enabled utilities without a maintenance action."""
    data = _single_electric_discovery()
    sensor_key = data.future_sensor_keys()[0]
    existing_config = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-14T13:00:00+08:00",
        backfill_from="2026-07-14T13:00:00+08:00",
        now=datetime(2026, 7, 14, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        options={CONF_SOURCE_CONFIG: {sensor_key.key: existing_config.to_dict()}},
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(data=data),
        ledger_store=SimpleNamespace(snapshot=SpGroupLedgerSnapshot()),
    )
    flow = OptionsFlowHandler(entry)

    result = await flow.async_step_init(
        {
            CONF_ENABLED_UTILITIES: [],
            CONF_MAINTENANCE_ACTION: "none",
        }
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SOURCE_CONFIG] == {}


async def test_options_flow_enables_skipped_utility_with_seed() -> None:
    """A previously skipped discovered utility can be enabled from options."""
    data = _single_electric_discovery()
    sensor_key = data.future_sensor_keys()[0]
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        options={CONF_SOURCE_CONFIG: {}},
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(data=data),
        ledger_store=SimpleNamespace(snapshot=SpGroupLedgerSnapshot()),
    )
    flow = OptionsFlowHandler(entry)

    result = await flow.async_step_init(
        {
            CONF_ENABLED_UTILITIES: [sensor_key.key],
            CONF_MAINTENANCE_ACTION: "none",
        }
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "seed"
    validators = _schema_validators(result)
    assert isinstance(validators[CONF_SEED_VALUE], selector.NumberSelector)
    assert isinstance(validators[CONF_SEED_OBSERVED_AT], selector.DateTimeSelector)
    assert isinstance(validators[CONF_BACKFILL_FROM], selector.DateTimeSelector)

    result = await flow.async_step_seed(
        {
            CONF_SEED_VALUE: "42.5",
            CONF_SEED_OBSERVED_AT: "2026-07-15T00:00:00+08:00",
            CONF_BACKFILL_FROM: "2026-07-15T00:00:00+08:00",
        }
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    source_config = result["data"][CONF_SOURCE_CONFIG][sensor_key.key]
    assert source_config["seed_value"] == "42.5"


async def test_options_flow_seed_accepts_split_datetime_selector_payload() -> None:
    """Options seed entry accepts the HA frontend split date/time value too."""
    data = _single_electric_discovery()
    sensor_key = data.future_sensor_keys()[0]
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        options={CONF_SOURCE_CONFIG: {}},
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(data=data),
        ledger_store=SimpleNamespace(snapshot=SpGroupLedgerSnapshot()),
    )
    flow = OptionsFlowHandler(entry)

    result = await flow.async_step_init(
        {
            CONF_ENABLED_UTILITIES: [sensor_key.key],
            CONF_MAINTENANCE_ACTION: "none",
        }
    )
    result = await flow.async_step_seed(
        {
            CONF_SEED_VALUE: "42.5",
            CONF_SEED_OBSERVED_AT: "2026-07-15T00:00:00 22:00:00",
            CONF_BACKFILL_FROM: "2026-07-15T00:00:00+08:00",
        }
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    source_config = result["data"][CONF_SOURCE_CONFIG][sensor_key.key]
    assert source_config["seed_observed_at"] == "2026-07-15T22:00:00+08:00"


async def test_options_flow_rebuild_cancel_is_non_mutating() -> None:
    """Cancelling a rebuild leaves ledger storage untouched."""
    data = _single_electric_discovery()
    sensor_key = data.future_sensor_keys()[0]
    existing_config = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-14T13:00:00+08:00",
        backfill_from="2026-07-14T13:00:00+08:00",
        now=datetime(2026, 7, 14, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    ledger_store = SimpleNamespace(
        snapshot=SpGroupLedgerSnapshot(),
        async_rebuild_sources=AsyncMock(),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        options={CONF_SOURCE_CONFIG: {sensor_key.key: existing_config.to_dict()}},
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(data=data),
        ledger_store=ledger_store,
    )
    flow = OptionsFlowHandler(entry)

    result = await flow.async_step_init(
        {
            CONF_ENABLED_UTILITIES: [sensor_key.key],
            CONF_MAINTENANCE_ACTION: f"rebuild:{sensor_key.key}",
        }
    )
    assert result["step_id"] == "rebuild_confirm"

    result = await flow.async_step_rebuild_confirm(
        {CONF_REBUILD_LEDGER_CONFIRM: False}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    ledger_store.async_rebuild_sources.assert_not_called()


def _single_electric_discovery() -> SpGroupCoordinatorData:
    return SpGroupCoordinatorData(
        premises=(
            SpGroupPremise(
                key="opaque_premise",
                active=True,
                signed_up=True,
                shared=False,
                contestable_type=None,
                accounts=(
                    SpGroupUtilityAccount(
                        key="opaque_account",
                        premise_key="opaque_premise",
                        utility_types=("ELECTRIC",),
                    ),
                ),
                display_label="123 Secret Street",
            ),
        )
    )
