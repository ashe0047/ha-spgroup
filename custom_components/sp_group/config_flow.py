"""Config flow for the SP Group integration."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult, OptionsFlow
from homeassistant.config_entries import ConfigFlow as BaseConfigFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, UnknownFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from spgroup.mobile import (
    JarvisClient,
    MfaRequired,
    MobileAuthClient,
    MobileSession,
    SpGroupAuthError,
    SpGroupMobileError,
    YggdrasilClient,
)

from .backfill import async_schedule_statistics_backfill, async_start_initial_backfill
from .const import (
    CONF_ACCOUNT_ID,
    CONF_APPROXIMATE_SEED_CONFIRM,
    CONF_BACKFILL_FROM,
    CONF_DEV_AUTH_METHOD,
    CONF_DEV_SESSION_CONFIRM,
    CONF_ELECTRICITY_POLL_INTERVAL,
    CONF_ELECTRICITY_POLL_INTERVAL_HOURS,
    CONF_ENABLED_UTILITIES,
    CONF_MAINTENANCE_ACTION,
    CONF_NO_SOURCES_CONFIRM,
    CONF_OTP,
    CONF_REBUILD_LEDGER_CONFIRM,
    CONF_RESET_REPLACEMENT,
    CONF_SEED_OBSERVED_AT,
    CONF_SEED_VALUE,
    CONF_SESSION,
    CONF_SOURCE_CONFIG,
    CONF_STALE_FLOW_RESET,
    CONF_WATER_POLL_DAY,
    CONF_WATER_POLL_TIME,
    DEFAULT_ELECTRICITY_POLL_INTERVAL_HOURS,
    DEFAULT_WATER_POLL_DAY,
    DEFAULT_WATER_POLL_TIME,
    DOMAIN,
)
from .discovery import (
    DiscoveryDataError,
    SpGroupCoordinatorData,
    SpGroupFutureSensorKey,
)
from .discovery_service import async_fetch_discovery_data
from .polling_options import SpGroupPollingOptions, normalize_options, polling_options
from .sources import (
    SINGAPORE_TZ,
    UTILITY_ELECTRIC,
    UTILITY_WATER,
    SourceConfigError,
    SpGroupSourceConfig,
    build_source_config,
    enabled_source_configs,
    source_options_payload,
    utility_label,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.EMAIL,
                autocomplete="username",
            )
        ),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.PASSWORD,
                autocomplete="current-password",
            )
        ),
    }
)
STEP_OTP_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OTP): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.TEL,
                autocomplete="one-time-code",
            )
        )
    }
)
STEP_NO_SOURCES_CONFIRM_SCHEMA = vol.Schema(
    {vol.Required(CONF_NO_SOURCES_CONFIRM, default=False): selector.BooleanSelector()}
)
STEP_APPROXIMATE_SEED_CONFIRM_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_APPROXIMATE_SEED_CONFIRM,
            default=False,
        ): selector.BooleanSelector()
    }
)
STEP_RESET_CONFIRM_SCHEMA = vol.Schema(
    {vol.Required(CONF_RESET_REPLACEMENT, default=False): selector.BooleanSelector()}
)
STEP_REBUILD_CONFIRM_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_REBUILD_LEDGER_CONFIRM,
            default=False,
        ): selector.BooleanSelector()
    }
)
STEP_STALE_FLOW_RESET_CONFIRM_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_STALE_FLOW_RESET,
            default=False,
        ): selector.BooleanSelector()
    }
)

_DATETIME_SELECTOR_SPLIT_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[T ]00:00:00 "
    r"(?P<time>\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)$"
)

CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"
ACTION_NONE = "none"
ACTION_RESEED_PREFIX = "reseed:"
ACTION_REBUILD_PREFIX = "rebuild:"
ACTION_REFRESH_DISCOVERY = "refresh_discovery"
ACTION_RETRY_STATISTICS_BACKFILL = "retry_statistics_backfill"
ACTION_RETRY_SOURCE_BACKFILL = "retry_source_backfill"
DEV_AUTH_BYPASS_ENV = "SPGROUP_HA_DEV_AUTH_BYPASS"
DEV_SESSION_FILE_ENV = "SPGROUP_HA_DEV_SESSION_FILE"
DEFAULT_DEV_SESSION_FILE = ".runtime/mobile-session.json"
DEV_AUTH_METHOD_CACHED_SESSION = "cached_session"
DEV_AUTH_METHOD_PASSWORD = "password_auth"

STEP_DEV_AUTH_CHOICE_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_DEV_AUTH_METHOD,
            default=DEV_AUTH_METHOD_CACHED_SESSION,
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(
                        value=DEV_AUTH_METHOD_CACHED_SESSION,
                        label="Use cached developer session",
                    ),
                    selector.SelectOptionDict(
                        value=DEV_AUTH_METHOD_PASSWORD,
                        label="Sign in with SP Group credentials",
                    ),
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
    }
)
STEP_DEV_SESSION_CONFIRM_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_DEV_SESSION_CONFIRM,
            default=True,
        ): selector.BooleanSelector()
    }
)


def _authenticator_id(authenticator: dict[str, Any]) -> str | None:
    value = authenticator.get("id")
    return value if isinstance(value, str) and value else None


def _authenticator_channel(authenticator: dict[str, Any]) -> str | None:
    value = authenticator.get("oob_channel")
    if not isinstance(value, str):
        value = authenticator.get("oobChannel")
    return value.lower() if isinstance(value, str) else None


def _active_oob_authenticators(
    authenticators: list[dict[str, Any]],
    channel: str,
) -> list[dict[str, Any]]:
    return [
        authenticator
        for authenticator in authenticators
        if _authenticator_channel(authenticator) == channel
        and authenticator.get("active") is not False
        and _authenticator_id(authenticator)
    ]


class _SeedDateTimeSelector(selector.DateTimeSelector):
    """HA datetime selector that accepts split date/time frontend payloads."""

    def __call__(self, data: Any) -> Any:
        """Validate and normalize the selected datetime value."""
        return super().__call__(_normalize_datetime_selector_value(data))


def _normalize_datetime_selector_value(value: Any) -> Any:
    """Normalize HA frontend split date/time values into ISO datetime strings."""
    if not isinstance(value, str):
        return value
    match = _DATETIME_SELECTOR_SPLIT_RE.match(value)
    if match is None:
        return value
    return f"{match.group('date')}T{match.group('time')}"


async def _async_get_auth_client(hass: HomeAssistant) -> MobileAuthClient:
    """Create the SDK Auth0 client using Home Assistant's shared aiohttp session."""
    return MobileAuthClient(async_get_clientsession(hass))


async def _async_get_discovery_clients(
    hass: HomeAssistant,
) -> tuple[YggdrasilClient, JarvisClient]:
    """Create SDK discovery clients using Home Assistant's shared aiohttp session."""
    session = async_get_clientsession(hass)
    return YggdrasilClient(session), JarvisClient(session)


def _seed_data_schema(sensor_key: SpGroupFutureSensorKey) -> vol.Schema:
    """Return the seed schema with a utility-specific unit selector."""
    return vol.Schema(
        {
            vol.Required(CONF_SEED_VALUE): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    step="any",
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement=_utility_unit(sensor_key.utility_type),
                )
            ),
            vol.Required(CONF_SEED_OBSERVED_AT): _SeedDateTimeSelector(),
            vol.Required(CONF_BACKFILL_FROM): _SeedDateTimeSelector(),
        }
    )


def _source_selection_schema(
    sensor_keys: list[SpGroupFutureSensorKey],
    labels: dict[str, str],
    *,
    default: list[str],
) -> vol.Schema:
    """Return a multi-select schema for discovered utilities."""
    return vol.Schema(
        {
            vol.Required(
                CONF_ENABLED_UTILITIES,
                default=default,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=sensor_key.key,
                            label=labels[sensor_key.key],
                        )
                        for sensor_key in sensor_keys
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        }
    )


def _maintenance_action_schema(
    sensor_keys: list[SpGroupFutureSensorKey],
    labels: dict[str, str],
    existing_configs: dict[str, SpGroupSourceConfig],
    current_options: dict[str, Any],
) -> vol.Schema:
    """Return the options schema for enabled utilities and one maintenance action."""
    options: list[selector.SelectOptionDict] = [
        selector.SelectOptionDict(value=ACTION_NONE, label="No maintenance action"),
        selector.SelectOptionDict(
            value=ACTION_REFRESH_DISCOVERY,
            label="Refresh discovered utilities",
        ),
        selector.SelectOptionDict(
            value=ACTION_RETRY_STATISTICS_BACKFILL,
            label="Retry statistics import",
        ),
        selector.SelectOptionDict(
            value=ACTION_RETRY_SOURCE_BACKFILL,
            label="Retry source backfill",
        ),
    ]
    poll_options = polling_options(current_options)
    for sensor_key in sensor_keys:
        if sensor_key.key not in existing_configs:
            continue
        label = labels[sensor_key.key]
        options.extend(
            (
                selector.SelectOptionDict(
                    value=f"{ACTION_RESEED_PREFIX}{sensor_key.key}",
                    label=f"Reseed {label}",
                ),
                selector.SelectOptionDict(
                    value=f"{ACTION_REBUILD_PREFIX}{sensor_key.key}",
                    label=f"Rebuild ledger for {label}",
                ),
            )
        )
    return vol.Schema(
        {
            vol.Required(
                CONF_ENABLED_UTILITIES,
                default=[
                    sensor_key.key
                    for sensor_key in sensor_keys
                    if sensor_key.key in existing_configs
                ],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=sensor_key.key,
                            label=labels[sensor_key.key],
                        )
                        for sensor_key in sensor_keys
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_MAINTENANCE_ACTION,
                default=ACTION_NONE,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_ELECTRICITY_POLL_INTERVAL,
                default=_duration_from_poll(poll_options),
            ): selector.DurationSelector(
                selector.DurationSelectorConfig(
                    enable_day=False,
                )
            ),
            vol.Required(
                CONF_WATER_POLL_DAY,
                default=poll_options.water_poll_day,
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=28,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_WATER_POLL_TIME,
                default=poll_options.water_poll_time.isoformat(),
            ): selector.TimeSelector(),
        }
    )


def _seed_error_field(error_key: str) -> str:
    """Return the config-flow error field for a source-config validation error."""
    if error_key in {"invalid_decimal", "seed_negative"}:
        return CONF_SEED_VALUE
    if error_key in {"invalid_timestamp", "future_seed", "seed_too_old"}:
        return CONF_SEED_OBSERVED_AT
    if error_key in {
        "invalid_backfill",
        "future_backfill",
        "backfill_after_seed",
        "backfill_too_old",
        "missing_backfill_from",
    }:
        return CONF_BACKFILL_FROM
    return "base"


def _seed_timestamp_input(value: Any) -> str:
    """Return a source-config timestamp string from DateTimeSelector input."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=SINGAPORE_TZ)
        return str(value.astimezone(SINGAPORE_TZ).isoformat())
    return str(_normalize_datetime_selector_value(value))


def _utility_unit(utility_type: str) -> str:
    """Return the HA unit string for a discovered utility type."""
    if utility_type == UTILITY_ELECTRIC:
        return UnitOfEnergy.KILO_WATT_HOUR
    if utility_type == UTILITY_WATER:
        return UnitOfVolume.CUBIC_METERS
    return ""


def _enabled_sensor_keys(
    sensor_keys: list[SpGroupFutureSensorKey],
    user_input: dict[str, Any],
) -> list[SpGroupFutureSensorKey]:
    """Return selected sensor keys from a source multi-select payload."""
    selected = user_input.get(CONF_ENABLED_UTILITIES, [])
    if not isinstance(selected, list):
        selected = []
    selected_keys = {value for value in selected if isinstance(value, str)}
    return [sensor_key for sensor_key in sensor_keys if sensor_key.key in selected_keys]


def _maintenance_sensor_key(
    sensor_keys: list[SpGroupFutureSensorKey],
    action: str,
    prefix: str,
) -> SpGroupFutureSensorKey | None:
    """Return the sensor key targeted by a maintenance action."""
    if not action.startswith(prefix):
        return None
    target_key = action.removeprefix(prefix)
    return next(
        (sensor_key for sensor_key in sensor_keys if sensor_key.key == target_key),
        None,
    )


def _safe_action_for_log(action: str) -> str:
    """Return an options action name without embedding source identifiers."""
    if action.startswith(ACTION_RESEED_PREFIX):
        return "reseed"
    if action.startswith(ACTION_REBUILD_PREFIX):
        return "rebuild"
    return action


class ConfigFlow(BaseConfigFlow, domain=DOMAIN):
    """Handle a config flow for SP Group."""

    VERSION = 1
    MINOR_VERSION = 2

    @staticmethod
    def async_get_options_flow(config_entry) -> OptionsFlowHandler:  # type: ignore[no-untyped-def]
        """Return the options flow."""
        return OptionsFlowHandler(config_entry)

    def __init__(self) -> None:
        """Initialize transient flow state."""
        self._mfa_token: str | None = None
        self._mfa_oob_code: str | None = None
        self._mfa_channel: str | None = None
        self._session: MobileSession | None = None
        self._account_id: str | None = None
        self._discovery_data: SpGroupCoordinatorData | None = None
        self._selected_sensor_keys: list[SpGroupFutureSensorKey] = []
        self._source_configs: dict[str, SpGroupSourceConfig] = {}
        self._seed_labels: dict[str, str] = {}
        self._pending_approximate_config: SpGroupSourceConfig | None = None
        self._pending_approximate_sensor_key: SpGroupFutureSensorKey | None = None
        self._pending_stale_session: MobileSession | None = None
        self._pending_stale_account_id: str | None = None
        self._stale_flow_ids: list[str] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup step."""
        if user_input is None:
            if _dev_auth_bypass_enabled():
                _LOGGER.info("SP Group config flow using developer auth choice")
                return await self.async_step_dev_auth_choice()
            _LOGGER.debug("SP Group config flow showing credential step")
            return self._show_user_form()

        return await self._async_login_from_user_step(
            user_input,
            form_step_id="user",
        )

    async def async_step_dev_auth_choice(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose a local-development authentication path."""
        if not _dev_auth_bypass_enabled():
            return self._show_user_form()
        if user_input is None:
            _LOGGER.debug("SP Group config flow showing developer auth choice")
            return self.async_show_form(
                step_id="dev_auth_choice",
                data_schema=STEP_DEV_AUTH_CHOICE_SCHEMA,
            )
        method = user_input.get(CONF_DEV_AUTH_METHOD)
        if method == DEV_AUTH_METHOD_PASSWORD:
            _LOGGER.info("SP Group config flow developer choice: password_auth")
            return self._show_user_form()
        if method == DEV_AUTH_METHOD_CACHED_SESSION:
            _LOGGER.info("SP Group config flow developer choice: cached_session")
            return await self.async_step_dev_session_confirm()
        return self.async_show_form(
            step_id="dev_auth_choice",
            data_schema=STEP_DEV_AUTH_CHOICE_SCHEMA,
            errors={CONF_DEV_AUTH_METHOD: "unknown"},
        )

    async def async_step_dev_session_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm and load a cached developer mobile session."""
        if not _dev_auth_bypass_enabled():
            return self._show_user_form()
        if user_input is None:
            _LOGGER.debug("SP Group config flow showing cached-session confirmation")
            return self.async_show_form(
                step_id="dev_session_confirm",
                data_schema=STEP_DEV_SESSION_CONFIRM_SCHEMA,
                description_placeholders={
                    "session_file": str(_dev_session_file()),
                },
            )
        if not user_input.get(CONF_DEV_SESSION_CONFIRM):
            return self.async_abort(reason="dev_session_not_confirmed")
        try:
            _LOGGER.info("SP Group config flow loading cached developer session")
            session = await self.hass.async_add_executor_job(
                _load_dev_cached_session,
                _dev_session_file(),
            )
            session = await self._async_refresh_dev_session_if_needed(session)
            _LOGGER.info("SP Group config flow cached developer session ready")
            return await self._async_finish_auth(session)
        except DevCachedSessionError as err:
            return self.async_show_form(
                step_id="dev_session_confirm",
                data_schema=STEP_DEV_SESSION_CONFIRM_SCHEMA,
                errors={"base": str(err)},
                description_placeholders={
                    "session_file": str(_dev_session_file()),
                },
            )

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the OOB one-time password step."""
        if user_input is None:
            _LOGGER.debug("SP Group config flow showing OTP step")
            return self._show_otp_form()

        errors: dict[str, str] = {}
        otp_value = str(user_input.get(CONF_OTP, ""))
        if not otp_value.isdigit() or len(otp_value) < 6 or len(otp_value) > 8:
            errors[CONF_OTP] = "invalid_otp"
            return self._show_otp_form(errors)
        if not self._mfa_token or not self._mfa_oob_code:
            errors["base"] = "unknown"
            return self._show_otp_form(errors)

        try:
            client = await _async_get_auth_client(self.hass)
            session = await client.complete_mfa_oob(
                self._mfa_token,
                self._mfa_oob_code,
                str(user_input[CONF_OTP]),
            )
            _LOGGER.info("SP Group config flow MFA completed")
            return await self._async_finish_auth(session)
        except SpGroupAuthError:
            errors["base"] = "invalid_auth"
        except SpGroupMobileError:
            errors["base"] = "cannot_connect"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except AbortFlow:
            raise
        except Exception:
            _LOGGER.exception("Unexpected exception completing SP Group MFA")
            errors["base"] = "unknown"

        return self._show_otp_form(errors)

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after runtime auth failure."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect credentials for the reauth flow."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=STEP_USER_DATA_SCHEMA,
            )

        return await self._async_login_from_user_step(
            user_input,
            form_step_id="reauth_confirm",
        )

    async def _async_login_from_user_step(
        self,
        user_input: dict[str, Any],
        *,
        form_step_id: str,
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if len(str(user_input.get(CONF_PASSWORD, ""))) < 8:
            errors[CONF_PASSWORD] = "password_too_short"
            return self._show_form(form_step_id, errors)
        try:
            _LOGGER.info(
                "SP Group config flow authenticating: step=%s",
                form_step_id,
            )
            client = await _async_get_auth_client(self.hass)
            session = await client.attempt_password_realm_login(
                str(user_input[CONF_USERNAME]),
                str(user_input[CONF_PASSWORD]),
            )
            return await self._async_finish_auth(session)
        except MfaRequired as err:
            self._mfa_token = err.mfa_token
            _LOGGER.info("SP Group config flow MFA required")
            try:
                client = await _async_get_auth_client(self.hass)
                await self._async_start_oob_mfa(client, err.mfa_token)
            except SpGroupAuthError:
                errors["base"] = "invalid_auth"
                return self._show_form(form_step_id, errors)
            except SpGroupMobileError:
                errors["base"] = "cannot_connect"
                return self._show_form(form_step_id, errors)
            except AbortFlow:
                raise
            except Exception:
                _LOGGER.exception("Unexpected exception starting SP Group MFA")
                errors["base"] = "unknown"
                return self._show_form(form_step_id, errors)
            return await self.async_step_otp()
        except SpGroupAuthError:
            errors["base"] = "invalid_auth"
        except SpGroupMobileError:
            errors["base"] = "cannot_connect"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except AbortFlow:
            raise
        except Exception:
            _LOGGER.exception("Unexpected exception authenticating SP Group")
            errors["base"] = "unknown"

        return self._show_form(form_step_id, errors)

    async def _async_start_oob_mfa(
        self,
        client: MobileAuthClient,
        mfa_token: str,
    ) -> None:
        authenticators: list[dict[str, Any]] = []
        try:
            authenticators = await client.get_mfa_authenticators(mfa_token)
        except SpGroupMobileError:
            _LOGGER.debug("Falling back to default MFA challenge", exc_info=True)

        email = _first_authenticator_id(authenticators, CHANNEL_EMAIL)
        sms = _first_authenticator_id(authenticators, CHANNEL_SMS)

        if email is not None:
            try:
                challenge = await client.trigger_mfa_challenge(
                    mfa_token,
                    authenticator_id=email,
                )
            except SpGroupMobileError:
                _LOGGER.debug(
                    "Email MFA challenge failed; falling back to SMS/default",
                    exc_info=True,
                )
            else:
                self._mfa_oob_code = challenge.oob_code
                self._mfa_channel = CHANNEL_EMAIL
                return

        if sms is not None:
            challenge = await client.trigger_mfa_challenge(
                mfa_token,
                authenticator_id=sms,
            )
            self._mfa_oob_code = challenge.oob_code
            self._mfa_channel = CHANNEL_SMS
            return

        challenge = await client.trigger_mfa_challenge(mfa_token)
        self._mfa_oob_code = challenge.oob_code
        self._mfa_channel = CHANNEL_SMS

    async def _async_refresh_dev_session_if_needed(
        self,
        session: MobileSession,
    ) -> MobileSession:
        """Refresh an expired cached developer session before setup continues."""
        _validate_dev_cached_session(session)
        if not session.expired:
            _LOGGER.debug("SP Group config flow cached developer session not expired")
            return session
        if not session.refresh_token:
            raise DevCachedSessionError("dev_session_missing_refresh_token")
        try:
            _LOGGER.info("SP Group config flow refreshing cached developer session")
            client = await _async_get_auth_client(self.hass)
            refreshed = await client.refresh_session(session)
        except SpGroupAuthError as err:
            raise DevCachedSessionError("dev_session_refresh_failed") from err
        except SpGroupMobileError as err:
            raise DevCachedSessionError("dev_session_refresh_failed") from err
        _validate_dev_cached_session(refreshed)
        await self.hass.async_add_executor_job(
            _write_dev_cached_session,
            _dev_session_file(),
            refreshed,
        )
        _LOGGER.info("SP Group config flow cached developer session refreshed")
        return refreshed

    async def _async_finish_auth(self, session: MobileSession) -> ConfigFlowResult:
        identity = session.identity_metadata
        if identity is None or not identity.subject:
            raise InvalidAuth("SP Group ID token did not include a stable subject")

        account_id = identity.subject

        if self.source == SOURCE_REAUTH:
            _LOGGER.info("SP Group config flow reauth finishing")
            await self.async_set_unique_id(account_id)
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={
                    CONF_ACCOUNT_ID: account_id,
                    CONF_SESSION: session.to_dict(),
                },
            )

        return await self._async_continue_setup_after_auth(session, account_id)

    async def _async_continue_setup_after_auth(
        self,
        session: MobileSession,
        account_id: str,
        *,
        allow_stale_flow_reset: bool = True,
    ) -> ConfigFlowResult:
        """Continue setup after authentication and handle stale flow conflicts."""
        try:
            await self.async_set_unique_id(account_id)
        except AbortFlow as err:
            if allow_stale_flow_reset and err.reason == "already_in_progress":
                stale_flow_ids = self._matching_stale_flow_ids(account_id)
                if stale_flow_ids:
                    _LOGGER.info(
                        "SP Group config flow stale setup detected: flow_count=%d",
                        len(stale_flow_ids),
                    )
                    self._pending_stale_session = session
                    self._pending_stale_account_id = account_id
                    self._stale_flow_ids = stale_flow_ids
                    return await self.async_step_stale_flow_reset_confirm()
            raise
        self._abort_if_unique_id_configured()
        self._session = session
        self._account_id = account_id
        try:
            _LOGGER.info("SP Group config flow discovery started")
            yggdrasil_client, jarvis_client = await _async_get_discovery_clients(
                self.hass
            )
            self._discovery_data = await async_fetch_discovery_data(
                session=session,
                yggdrasil_client=yggdrasil_client,
                jarvis_client=jarvis_client,
                account_id=account_id,
                require_jarvis_correlation=True,
            )
        except SpGroupMobileError:
            return self._show_user_form({"base": "cannot_connect"})
        except DiscoveryDataError:
            return self._show_user_form({"base": "discovery_failed"})
        except Exception:
            _LOGGER.exception("Unexpected exception discovering SP Group utilities")
            return self._show_user_form({"base": "unknown"})
        if not self._discovery_data.future_sensor_keys():
            return self.async_abort(reason="no_supported_utilities")
        _LOGGER.info(
            "SP Group config flow discovery completed: future_sensor_count=%d",
            len(self._discovery_data.future_sensor_keys()),
        )
        return await self.async_step_sources()

    def _matching_stale_flow_ids(self, account_id: str) -> list[str]:
        """Return same-account SP Group setup flow IDs blocking this setup."""
        return [
            str(progress["flow_id"])
            for progress in self.hass.config_entries.flow.async_progress_by_handler(
                DOMAIN,
                include_uninitialized=True,
                match_context={"unique_id": account_id},
            )
            if progress["flow_id"] != self.flow_id
            and progress.get("context", {}).get("source") != SOURCE_REAUTH
        ]

    async def async_step_stale_flow_reset_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm aborting stale in-progress setup flows for the same account."""
        if (
            self._pending_stale_session is None
            or self._pending_stale_account_id is None
        ):
            return self._show_user_form({"base": "unknown"})
        if user_input is None:
            _LOGGER.debug("SP Group config flow showing stale-flow reset confirmation")
            return self.async_show_form(
                step_id="stale_flow_reset_confirm",
                data_schema=STEP_STALE_FLOW_RESET_CONFIRM_SCHEMA,
            )
        if not user_input.get(CONF_STALE_FLOW_RESET):
            self._clear_pending_stale_flow()
            return self.async_abort(reason="stale_flow_not_reset")
        for flow_id in list(self._stale_flow_ids):
            try:
                self.hass.config_entries.flow.async_abort(flow_id)
            except UnknownFlow:
                continue
        session = self._pending_stale_session
        account_id = self._pending_stale_account_id
        self._clear_pending_stale_flow()
        try:
            return await self._async_continue_setup_after_auth(
                session,
                account_id,
                allow_stale_flow_reset=False,
            )
        except AbortFlow as err:
            if err.reason == "already_in_progress":
                return self.async_show_form(
                    step_id="stale_flow_reset_confirm",
                    data_schema=STEP_STALE_FLOW_RESET_CONFIRM_SCHEMA,
                    errors={"base": "stale_flow_reset_failed"},
                )
            raise

    def _clear_pending_stale_flow(self) -> None:
        """Clear transient stale-flow reset state."""
        self._pending_stale_session = None
        self._pending_stale_account_id = None
        self._stale_flow_ids = []

    async def async_step_sources(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose discovered utilities to enable."""
        if self._discovery_data is None or self._session is None:
            return self._show_user_form({"base": "unknown"})
        sensor_keys = list(self._discovery_data.future_sensor_keys())
        self._seed_labels = {
            sensor_key.key: utility_label(index, sensor_key)
            for index, sensor_key in enumerate(sensor_keys, start=1)
        }
        if user_input is None:
            _LOGGER.debug(
                "SP Group config flow showing sources step: future_sensor_count=%d",
                len(sensor_keys),
            )
            return self.async_show_form(
                step_id="sources",
                data_schema=_source_selection_schema(
                    sensor_keys,
                    self._seed_labels,
                    default=[sensor_key.key for sensor_key in sensor_keys],
                ),
            )
        self._selected_sensor_keys = _enabled_sensor_keys(sensor_keys, user_input)
        _LOGGER.info(
            "SP Group config flow sources selected: selected_count=%d total_count=%d",
            len(self._selected_sensor_keys),
            len(sensor_keys),
        )
        if not self._selected_sensor_keys:
            return await self.async_step_no_sources_confirm()
        return await self.async_step_seed()

    async def async_step_no_sources_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm creating a discovery-only entry with no seeded utilities."""
        if user_input is None:
            _LOGGER.debug("SP Group config flow showing no-source confirmation")
            return self.async_show_form(
                step_id="no_sources_confirm",
                data_schema=STEP_NO_SOURCES_CONFIRM_SCHEMA,
            )
        if not user_input.get(CONF_NO_SOURCES_CONFIRM):
            return await self.async_step_sources()
        return self._async_create_setup_entry()

    async def async_step_seed(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect a manual seed for the next selected utility."""
        if not self._selected_sensor_keys:
            return self._async_create_setup_entry()
        sensor_key = self._selected_sensor_keys[0]
        if user_input is None:
            _LOGGER.debug(
                "SP Group config flow showing seed step: utility=%s remaining=%d",
                sensor_key.utility_type,
                len(self._selected_sensor_keys),
            )
            return self.async_show_form(
                step_id="seed",
                data_schema=_seed_data_schema(sensor_key),
                description_placeholders=self._seed_placeholders(sensor_key),
            )
        if CONF_BACKFILL_FROM not in user_input:
            return self.async_show_form(
                step_id="seed",
                data_schema=_seed_data_schema(sensor_key),
                errors={CONF_BACKFILL_FROM: "missing_backfill_from"},
                description_placeholders=self._seed_placeholders(sensor_key),
            )
        try:
            config = build_source_config(
                sensor_key=sensor_key,
                seed_value=str(user_input[CONF_SEED_VALUE]),
                seed_observed_at=_seed_timestamp_input(
                    user_input[CONF_SEED_OBSERVED_AT]
                ),
                backfill_from=_seed_timestamp_input(user_input[CONF_BACKFILL_FROM]),
            )
        except SourceConfigError as err:
            error_key = str(err)
            return self.async_show_form(
                step_id="seed",
                data_schema=_seed_data_schema(sensor_key),
                errors={_seed_error_field(error_key): error_key},
                description_placeholders=self._seed_placeholders(sensor_key),
            )
        if not config.boundary_aligned:
            _LOGGER.info(
                "SP Group config flow seed requires approximate confirmation: "
                "utility=%s",
                sensor_key.utility_type,
            )
            self._pending_approximate_config = config
            self._pending_approximate_sensor_key = sensor_key
            return await self.async_step_approximate_seed_confirm()
        self._source_configs[sensor_key.key] = config
        self._selected_sensor_keys.pop(0)
        _LOGGER.info(
            "SP Group config flow seed accepted: utility=%s remaining=%d",
            sensor_key.utility_type,
            len(self._selected_sensor_keys),
        )
        return await self.async_step_seed()

    async def async_step_approximate_seed_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm an approximate seed boundary before storing it."""
        if (
            self._pending_approximate_config is None
            or self._pending_approximate_sensor_key is None
        ):
            return await self.async_step_seed()
        sensor_key = self._pending_approximate_sensor_key
        if user_input is None:
            _LOGGER.debug(
                "SP Group config flow showing approximate seed confirmation: "
                "utility=%s",
                sensor_key.utility_type,
            )
            return self.async_show_form(
                step_id="approximate_seed_confirm",
                data_schema=STEP_APPROXIMATE_SEED_CONFIRM_SCHEMA,
                description_placeholders=self._seed_placeholders(sensor_key),
            )
        if not user_input.get(CONF_APPROXIMATE_SEED_CONFIRM):
            self._pending_approximate_config = None
            self._pending_approximate_sensor_key = None
            return self.async_show_form(
                step_id="seed",
                data_schema=_seed_data_schema(sensor_key),
                errors={"base": "approximate_seed_not_confirmed"},
                description_placeholders=self._seed_placeholders(sensor_key),
            )
        self._source_configs[sensor_key.key] = self._pending_approximate_config
        self._pending_approximate_config = None
        self._pending_approximate_sensor_key = None
        self._selected_sensor_keys.pop(0)
        _LOGGER.info(
            "SP Group config flow approximate seed accepted: utility=%s remaining=%d",
            sensor_key.utility_type,
            len(self._selected_sensor_keys),
        )
        return await self.async_step_seed()

    def _seed_placeholders(
        self,
        sensor_key: SpGroupFutureSensorKey,
    ) -> dict[str, str]:
        """Return seed form description placeholders."""
        step = (
            len(self._source_configs)
            + (1 if self._pending_approximate_config is None else 0)
        )
        total = len(self._source_configs) + len(self._selected_sensor_keys)
        return {
            "utility": self._seed_labels.get(sensor_key.key, "selected utility"),
            "unit": _utility_unit(sensor_key.utility_type),
            "step": str(max(step, 1)),
            "total": str(max(total, 1)),
        }

    def _async_create_setup_entry(self) -> ConfigFlowResult:
        """Create the config entry after optional seed collection."""
        if self._session is None or self._account_id is None:
            return self._show_user_form({"base": "unknown"})
        _LOGGER.info(
            "SP Group config flow creating entry: source_count=%d",
            len(self._source_configs),
        )
        return self.async_create_entry(
            title="SP Group",
            data={
                CONF_ACCOUNT_ID: self._account_id,
                CONF_SESSION: self._session.to_dict(),
            },
            options=normalize_options(
                {CONF_SOURCE_CONFIG: source_options_payload(self._source_configs)}
            ),
        )

    def _show_user_form(
        self,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        return self._show_form("user", errors)

    def _show_form(
        self,
        step_id: str,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors or {},
        )

    def _show_otp_form(
        self,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="otp",
            data_schema=STEP_OTP_DATA_SCHEMA,
            errors=errors or {},
            description_placeholders={
                "channel": self._mfa_channel or "your selected channel",
            },
        )


def _first_authenticator_id(
    authenticators: list[dict[str, Any]],
    channel: str,
) -> str | None:
    for authenticator in _active_oob_authenticators(authenticators, channel):
        authenticator_id = _authenticator_id(authenticator)
        if authenticator_id is not None:
            return authenticator_id
    return None


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class DevCachedSessionError(HomeAssistantError):
    """Error to indicate a local developer session cache cannot be used."""


def _dev_auth_bypass_enabled() -> bool:
    """Return whether the developer cached-session auth path is enabled."""
    return os.environ.get(DEV_AUTH_BYPASS_ENV) == "1"


def _dev_session_file() -> Path:
    """Return the configured developer session cache path."""
    return Path(
        os.environ.get(DEV_SESSION_FILE_ENV, DEFAULT_DEV_SESSION_FILE)
    ).expanduser()


def _load_dev_cached_session(path: Path) -> MobileSession:
    """Load a serialized developer mobile session from disk."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise DevCachedSessionError("dev_session_missing") from err
    except (OSError, json.JSONDecodeError) as err:
        raise DevCachedSessionError("dev_session_malformed") from err
    if not isinstance(data, dict):
        raise DevCachedSessionError("dev_session_invalid")
    try:
        session = MobileSession.from_dict(data)
    except (KeyError, TypeError, ValueError) as err:
        raise DevCachedSessionError("dev_session_invalid") from err
    _validate_dev_cached_session(session)
    return session


def _validate_dev_cached_session(session: MobileSession) -> None:
    """Validate that a cached developer session is usable for setup."""
    if not session.access_token or not session.id_token:
        raise DevCachedSessionError("dev_session_invalid")
    if not session.refresh_token:
        raise DevCachedSessionError("dev_session_missing_refresh_token")
    if session.identity_metadata is None or not session.identity_metadata.subject:
        raise DevCachedSessionError("dev_session_missing_identity")


def _write_dev_cached_session(path: Path, session: MobileSession) -> None:
    """Write a refreshed developer mobile session cache atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(session.to_dict(), indent=2, sort_keys=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as temp:
        temp_path = Path(temp.name)
        os.chmod(temp_path, 0o600)
        temp.write(payload)
        temp.write("\n")
    os.replace(temp_path, path)
    os.chmod(path, 0o600)


def _duration_from_poll(poll_options: SpGroupPollingOptions) -> dict[str, float]:
    """Convert polling options timedelta to DurationSelector default dict."""
    seconds = int(poll_options.electricity_poll_interval.total_seconds())
    return {
        "hours": seconds // 3600,
        "minutes": (seconds % 3600) // 60,
        "seconds": seconds % 60,
    }


class OptionsFlowHandler(OptionsFlow):
    """Handle SP Group options for source configuration."""

    def __init__(self, config_entry) -> None:  # type: ignore[no-untyped-def]
        """Initialize options flow."""
        self._config_entry = config_entry
        self._selected_sensor_keys: list[SpGroupFutureSensorKey] = []
        self._source_configs = enabled_source_configs(config_entry.options)
        self._seed_labels: dict[str, str] = {}
        self._pending_approximate_config: SpGroupSourceConfig | None = None
        self._pending_approximate_sensor_key: SpGroupFutureSensorKey | None = None
        self._pending_reset_config: SpGroupSourceConfig | None = None
        self._pending_reset_sensor_key: SpGroupFutureSensorKey | None = None
        self._pending_rebuild_sensor_key: SpGroupFutureSensorKey | None = None
        self._polling_options: dict[str, Any] = {}

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose discovered utilities to enable or reseed."""
        coordinator = getattr(
            self._config_entry.runtime_data,
            "discovery_coordinator",
            None,
        )
        if coordinator is None:
            coordinator = getattr(self._config_entry.runtime_data, "coordinator", None)
        data = getattr(coordinator, "data", None)
        if data is None:
            return self.async_abort(reason="no_discovery")
        sensor_keys = list(data.future_sensor_keys())
        self._seed_labels = {
            sensor_key.key: utility_label(index, sensor_key)
            for index, sensor_key in enumerate(sensor_keys, start=1)
        }
        if user_input is None:
            _LOGGER.debug(
                "SP Group options flow showing init: future_sensor_count=%d "
                "configured_source_count=%d",
                len(sensor_keys),
                len(self._source_configs),
            )
            return self.async_show_form(
                step_id="init",
                data_schema=_maintenance_action_schema(
                    sensor_keys,
                    self._seed_labels,
                    self._source_configs,
                    dict(self._config_entry.options),
                ),
            )
        action = str(user_input.get(CONF_MAINTENANCE_ACTION, ACTION_NONE))
        _LOGGER.info(
            "SP Group options flow action selected: action=%s",
            _safe_action_for_log(action),
        )
        self._polling_options = {
            CONF_ELECTRICITY_POLL_INTERVAL: user_input.get(
                CONF_ELECTRICITY_POLL_INTERVAL,
                {
                    "hours": DEFAULT_ELECTRICITY_POLL_INTERVAL_HOURS,
                    "minutes": 0,
                    "seconds": 0,
                },
            ),
            CONF_ELECTRICITY_POLL_INTERVAL_HOURS: int(
                user_input.get(
                    CONF_ELECTRICITY_POLL_INTERVAL_HOURS,
                    DEFAULT_ELECTRICITY_POLL_INTERVAL_HOURS,
                )
            ),
            CONF_WATER_POLL_DAY: int(
                user_input.get(CONF_WATER_POLL_DAY, DEFAULT_WATER_POLL_DAY)
            ),
            CONF_WATER_POLL_TIME: str(
                user_input.get(CONF_WATER_POLL_TIME, DEFAULT_WATER_POLL_TIME)
            ),
        }
        if action == ACTION_REFRESH_DISCOVERY:
            return self._create_options_entry(reload=True)
        if action == ACTION_RETRY_STATISTICS_BACKFILL:
            status = async_schedule_statistics_backfill(
                self.hass,
                self._config_entry,
                name="sp_group_options_statistics_backfill",
                force=True,
            )
            _LOGGER.info(
                "SP Group options flow statistics retry requested: status=%s",
                status.value,
            )
            return self.async_abort(
                reason=f"statistics_import_{status.value}",
            )
        if action == ACTION_RETRY_SOURCE_BACKFILL:
            _LOGGER.info("SP Group options flow source backfill retry requested")
            async_start_initial_backfill(self.hass, self._config_entry)
            return self.async_abort(reason="source_backfill_scheduled")
        enabled_keys = _enabled_sensor_keys(sensor_keys, user_input)
        self._selected_sensor_keys = [
            sensor_key
            for sensor_key in enabled_keys
            if (
                sensor_key.key not in self._source_configs
            )
        ]
        reseed_key = _maintenance_sensor_key(
            sensor_keys,
            action,
            ACTION_RESEED_PREFIX,
        )
        if reseed_key is not None and reseed_key in enabled_keys:
            self._selected_sensor_keys.append(reseed_key)
        rebuild_key = _maintenance_sensor_key(
            sensor_keys,
            action,
            ACTION_REBUILD_PREFIX,
        )
        disabled = {
            sensor_key.key
            for sensor_key in sensor_keys
            if sensor_key not in enabled_keys
        }
        self._source_configs = {
            key: config
            for key, config in self._source_configs.items()
            if key not in disabled
        }
        if rebuild_key is not None and rebuild_key.key in self._source_configs:
            self._pending_rebuild_sensor_key = rebuild_key
            return await self.async_step_rebuild_confirm()
        if not self._selected_sensor_keys:
            _LOGGER.info(
                "SP Group options flow updating options: source_count=%d reload=%s",
                len(self._source_configs),
                False,
            )
            return self._create_options_entry()
        return await self.async_step_seed()

    async def async_step_seed(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect a seed for a newly enabled utility."""
        if not self._selected_sensor_keys:
            return self._create_options_entry()
        sensor_key = self._selected_sensor_keys[0]
        if user_input is None:
            _LOGGER.debug(
                "SP Group options flow showing seed step: utility=%s remaining=%d",
                sensor_key.utility_type,
                len(self._selected_sensor_keys),
            )
            return self.async_show_form(
                step_id="seed",
                data_schema=_seed_data_schema(sensor_key),
                description_placeholders=self._seed_placeholders(sensor_key),
            )
        if CONF_BACKFILL_FROM not in user_input:
            return self.async_show_form(
                step_id="seed",
                data_schema=_seed_data_schema(sensor_key),
                errors={CONF_BACKFILL_FROM: "missing_backfill_from"},
                description_placeholders=self._seed_placeholders(sensor_key),
            )
        try:
            config = build_source_config(
                sensor_key=sensor_key,
                seed_value=str(user_input[CONF_SEED_VALUE]),
                seed_observed_at=_seed_timestamp_input(
                    user_input[CONF_SEED_OBSERVED_AT]
                ),
                backfill_from=_seed_timestamp_input(user_input[CONF_BACKFILL_FROM]),
            )
        except SourceConfigError as err:
            error_key = str(err)
            return self.async_show_form(
                step_id="seed",
                data_schema=_seed_data_schema(sensor_key),
                errors={_seed_error_field(error_key): error_key},
                description_placeholders=self._seed_placeholders(sensor_key),
            )
        if not config.boundary_aligned:
            _LOGGER.info(
                "SP Group options flow seed requires approximate confirmation: "
                "utility=%s",
                sensor_key.utility_type,
            )
            self._pending_approximate_config = config
            self._pending_approximate_sensor_key = sensor_key
            return await self.async_step_approximate_seed_confirm()
        return await self._async_accept_seed_config(config, sensor_key)

    async def async_step_approximate_seed_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm an approximate seed boundary before storing it."""
        if (
            self._pending_approximate_config is None
            or self._pending_approximate_sensor_key is None
        ):
            return await self.async_step_seed()
        sensor_key = self._pending_approximate_sensor_key
        if user_input is None:
            _LOGGER.debug(
                "SP Group options flow showing approximate seed confirmation: "
                "utility=%s",
                sensor_key.utility_type,
            )
            return self.async_show_form(
                step_id="approximate_seed_confirm",
                data_schema=STEP_APPROXIMATE_SEED_CONFIRM_SCHEMA,
                description_placeholders=self._seed_placeholders(sensor_key),
            )
        if not user_input.get(CONF_APPROXIMATE_SEED_CONFIRM):
            self._pending_approximate_config = None
            self._pending_approximate_sensor_key = None
            return self.async_show_form(
                step_id="seed",
                data_schema=_seed_data_schema(sensor_key),
                errors={"base": "approximate_seed_not_confirmed"},
                description_placeholders=self._seed_placeholders(sensor_key),
            )
        config = self._pending_approximate_config
        self._pending_approximate_config = None
        self._pending_approximate_sensor_key = None
        return await self._async_accept_seed_config(config, sensor_key)

    async def _async_accept_seed_config(
        self,
        config: SpGroupSourceConfig,
        sensor_key: SpGroupFutureSensorKey,
    ) -> ConfigFlowResult:
        """Accept a parsed options seed config or route through reset confirmation."""
        if self._is_lower_reseed(config):
            _LOGGER.info(
                "SP Group options flow seed requires reset confirmation: utility=%s",
                sensor_key.utility_type,
            )
            self._pending_reset_config = config
            self._pending_reset_sensor_key = sensor_key
            return await self.async_step_reset_confirm()
        self._source_configs[sensor_key.key] = config
        self._selected_sensor_keys.pop(0)
        _LOGGER.info(
            "SP Group options flow seed accepted: utility=%s remaining=%d",
            sensor_key.utility_type,
            len(self._selected_sensor_keys),
        )
        return await self.async_step_seed()

    async def async_step_reset_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm a lower-value replacement/reset epoch."""
        if self._pending_reset_config is None or self._pending_reset_sensor_key is None:
            return await self.async_step_seed()
        sensor_key = self._pending_reset_sensor_key
        if user_input is None:
            _LOGGER.debug(
                "SP Group options flow showing reset confirmation: utility=%s",
                sensor_key.utility_type,
            )
            return self.async_show_form(
                step_id="reset_confirm",
                data_schema=STEP_RESET_CONFIRM_SCHEMA,
                description_placeholders={
                    "utility": self._seed_labels.get(sensor_key.key, "selected utility")
                },
            )
        if not user_input.get(CONF_RESET_REPLACEMENT):
            self._pending_reset_config = None
            self._pending_reset_sensor_key = None
            return self.async_show_form(
                step_id="seed",
                data_schema=_seed_data_schema(sensor_key),
                errors={"base": "reset_not_confirmed"},
                description_placeholders=self._seed_placeholders(sensor_key),
            )
        confirmed = build_source_config(
            sensor_key=sensor_key,
            seed_value=str(self._pending_reset_config.seed_value),
            seed_observed_at=self._pending_reset_config.seed_observed_at.isoformat(),
            backfill_from=self._pending_reset_config.backfill_from.isoformat(),
            reset_confirmed=True,
        )
        self._source_configs[sensor_key.key] = confirmed
        self._selected_sensor_keys.pop(0)
        _LOGGER.info(
            "SP Group options flow reset seed accepted: utility=%s remaining=%d",
            sensor_key.utility_type,
            len(self._selected_sensor_keys),
        )
        self._pending_reset_config = None
        self._pending_reset_sensor_key = None
        return await self.async_step_seed()

    async def async_step_rebuild_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm an explicit ledger rebuild for a seeded utility."""
        sensor_key = self._pending_rebuild_sensor_key
        if sensor_key is None:
            return self._create_options_entry()
        if user_input is None:
            _LOGGER.debug(
                "SP Group options flow showing rebuild confirmation: utility=%s",
                sensor_key.utility_type,
            )
            return self.async_show_form(
                step_id="rebuild_confirm",
                data_schema=STEP_REBUILD_CONFIRM_SCHEMA,
                description_placeholders={
                    "utility": self._seed_labels.get(sensor_key.key, "selected utility")
                },
            )
        if not user_input.get(CONF_REBUILD_LEDGER_CONFIRM):
            self._pending_rebuild_sensor_key = None
            return self._create_options_entry()
        runtime = getattr(self._config_entry, "runtime_data", None)
        ledger_store = getattr(runtime, "ledger_store", None)
        config = self._source_configs.get(sensor_key.key)
        if ledger_store is not None and config is not None:
            _LOGGER.info(
                "SP Group options flow rebuilding ledger: utility=%s",
                sensor_key.utility_type,
            )
            await ledger_store.async_rebuild_sources(
                {sensor_key.key: config},
                sensor_keys={sensor_key.key},
            )
            await self.hass.config_entries.async_reload(self._config_entry.entry_id)
        self._pending_rebuild_sensor_key = None
        if self._selected_sensor_keys:
            return await self.async_step_seed()
        return self._create_options_entry()

    def _create_options_entry(self, *, reload: bool = False) -> ConfigFlowResult:
        data = normalize_options(
            {
                **dict(self._config_entry.options),
                **getattr(self, "_polling_options", {}),
                CONF_SOURCE_CONFIG: source_options_payload(self._source_configs),
            }
        )
        if reload:
            _LOGGER.info(
                "SP Group options flow requesting reload: source_count=%d",
                len(self._source_configs),
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self._config_entry.entry_id)
            )
        else:
            _LOGGER.info(
                "SP Group options flow creating options entry: source_count=%d",
                len(self._source_configs),
            )
        return self.async_create_entry(
            title="",
            data=data,
        )

    def _seed_placeholders(
        self,
        sensor_key: SpGroupFutureSensorKey,
    ) -> dict[str, str]:
        """Return seed form description placeholders."""
        return {
            "utility": self._seed_labels.get(sensor_key.key, "selected utility"),
            "unit": _utility_unit(sensor_key.utility_type),
            "step": "1",
            "total": str(max(len(self._selected_sensor_keys), 1)),
        }

    def _is_lower_reseed(self, config: SpGroupSourceConfig) -> bool:
        """Return whether a reseed would lower the currently published total."""
        runtime = getattr(self._config_entry, "runtime_data", None)
        ledger_store = getattr(runtime, "ledger_store", None)
        snapshot = getattr(ledger_store, "snapshot", None)
        entry = getattr(snapshot, "entries", {}).get(config.sensor_key)
        latest_total = getattr(entry, "latest_total", None)
        return latest_total is not None and config.seed_value < latest_total
