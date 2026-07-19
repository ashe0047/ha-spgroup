"""Test SP Group custom integration translation coverage."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from custom_components.sp_group.const import (
    CONF_APPROXIMATE_SEED_CONFIRM,
    CONF_DEV_AUTH_METHOD,
    CONF_DEV_SESSION_CONFIRM,
    CONF_ELECTRICITY_POLL_INTERVAL_HOURS,
    CONF_ENABLED_UTILITIES,
    CONF_MAINTENANCE_ACTION,
    CONF_NO_SOURCES_CONFIRM,
    CONF_OTP,
    CONF_REBUILD_LEDGER_CONFIRM,
    CONF_RESET_REPLACEMENT,
    CONF_SEED_OBSERVED_AT,
    CONF_SEED_VALUE,
    CONF_STALE_FLOW_RESET,
    CONF_WATER_POLL_DAY,
    CONF_WATER_POLL_TIME,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

TRANSLATIONS = Path("custom_components/sp_group/translations/en.json")
STRINGS = Path("custom_components/sp_group/strings.json")


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON object from a repository path."""
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    assert isinstance(data, dict)
    return data


def _walk_strings(value: Any) -> Iterable[str]:
    """Yield all string values from a nested JSON-compatible object."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def test_custom_integration_translation_file_exists_and_is_literal() -> None:
    """Custom integrations use translations/en.json with literal strings."""
    translations = _load_json(TRANSLATIONS)

    assert translations
    assert not any("[%key:" in value for value in _walk_strings(translations))


def test_legacy_strings_mirror_matches_runtime_translations() -> None:
    """The legacy scaffold strings file mirrors runtime translations exactly."""
    assert _load_json(STRINGS) == _load_json(TRANSLATIONS)


def test_config_flow_field_translation_coverage() -> None:
    """Every config-flow schema field has user-facing translation text."""
    translations = _load_json(TRANSLATIONS)
    steps = translations["config"]["step"]

    expected_fields = {
        "user": {CONF_USERNAME, CONF_PASSWORD},
        "dev_auth_choice": {CONF_DEV_AUTH_METHOD},
        "dev_session_confirm": {CONF_DEV_SESSION_CONFIRM},
        "reauth_confirm": {CONF_USERNAME, CONF_PASSWORD},
        "otp": {CONF_OTP},
        "sources": {CONF_ENABLED_UTILITIES},
        "no_sources_confirm": {CONF_NO_SOURCES_CONFIRM},
        "seed": {CONF_SEED_VALUE, CONF_SEED_OBSERVED_AT},
        "approximate_seed_confirm": {CONF_APPROXIMATE_SEED_CONFIRM},
        "stale_flow_reset_confirm": {CONF_STALE_FLOW_RESET},
    }
    for step_id, fields in expected_fields.items():
        data = steps[step_id]["data"]
        descriptions = steps[step_id].get("data_description", {})
        assert fields <= set(data), step_id
        assert fields <= set(descriptions), step_id
        assert all(data[field] != field for field in fields), step_id


def test_options_flow_field_translation_coverage() -> None:
    """Every options-flow schema field has user-facing translation text."""
    translations = _load_json(TRANSLATIONS)
    steps = translations["options"]["step"]

    expected_fields = {
        "init": {
            CONF_ENABLED_UTILITIES,
            CONF_MAINTENANCE_ACTION,
            CONF_ELECTRICITY_POLL_INTERVAL_HOURS,
            CONF_WATER_POLL_DAY,
            CONF_WATER_POLL_TIME,
        },
        "seed": {CONF_SEED_VALUE, CONF_SEED_OBSERVED_AT},
        "approximate_seed_confirm": {CONF_APPROXIMATE_SEED_CONFIRM},
        "reset_confirm": {CONF_RESET_REPLACEMENT},
        "rebuild_confirm": {CONF_REBUILD_LEDGER_CONFIRM},
    }
    for step_id, fields in expected_fields.items():
        data = steps[step_id]["data"]
        descriptions = steps[step_id].get("data_description", {})
        assert fields <= set(data), step_id
        assert fields <= set(descriptions), step_id
        assert all(data[field] != field for field in fields), step_id


def test_error_abort_entity_and_repair_translation_coverage() -> None:
    """Configured errors, aborts, entities, and repairs have translations."""
    translations = _load_json(TRANSLATIONS)

    assert {
        "approximate_seed_not_confirmed",
        "cannot_connect",
        "discovery_failed",
        "dev_session_invalid",
        "dev_session_malformed",
        "dev_session_missing",
        "dev_session_missing_identity",
        "dev_session_missing_refresh_token",
        "dev_session_refresh_failed",
        "invalid_auth",
        "future_seed",
        "invalid_decimal",
        "invalid_timestamp",
        "seed_negative",
        "seed_too_old",
        "stale_flow_reset_failed",
        "unknown",
    } <= set(translations["config"]["error"])
    assert {
        "already_configured",
        "dev_session_not_confirmed",
        "no_supported_utilities",
        "reauth_successful",
        "stale_flow_not_reset",
        "wrong_account",
    } <= set(translations["config"]["abort"])
    assert {
        "approximate_seed_not_confirmed",
        "future_seed",
        "invalid_decimal",
        "invalid_timestamp",
        "reset_not_confirmed",
        "seed_negative",
        "seed_too_old",
        "unknown",
        "rebuild_not_confirmed",
    } <= set(translations["options"]["error"])
    assert {"no_discovery", "missing_entry"} <= set(translations["options"]["abort"])
    assert {"electricity", "water"} <= set(translations["entity"]["sensor"])
    assert "malformed_ledger" in translations["issues"]
    assert (
        "missing_entry"
        in translations["issues"]["malformed_ledger"]["fix_flow"]["abort"]
    )
