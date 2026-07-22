"""Test SP Group diagnostics privacy and summary behavior."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from custom_components.sp_group import SpGroupRuntimeData
from custom_components.sp_group.const import CONF_ACCOUNT_ID, CONF_SESSION, DOMAIN
from custom_components.sp_group.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.sp_group.discovery import (
    SpGroupCoordinatorData,
    SpGroupPremise,
    SpGroupUtilityAccount,
)
from custom_components.sp_group.electricity import SpGroupElectricityCandidate
from custom_components.sp_group.ledger import SpGroupLedgerEntry, SpGroupLedgerSnapshot
from custom_components.sp_group.water import SpGroupWaterCandidate
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .helpers import make_mobile_session

SENSITIVE_VALUES = (
    "access-token-secret",
    "id-token-secret",
    "refresh-token-secret",
    "auth0|iam-sensitive",
    "ACC-SECRET-1",
    "PREM-SECRET-1",
    "JARVIS-PREMISE-SECRET",
    "123 Secret Street",
    "Sensitive Owner",
    "person@example.test",
    "+6512345678",
)


async def test_config_entry_diagnostics_are_bounded_summaries(
    hass: HomeAssistant,
) -> None:
    """Config-entry diagnostics expose counts, not raw session or payload data."""
    entry = _diagnostics_entry()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics == {
        "domain": DOMAIN,
        "entry": {
            "title": "SP Group",
            "data_keys": [CONF_ACCOUNT_ID, CONF_SESSION],
            "options_keys": [],
        },
        "runtime": {
            "has_runtime_data": True,
            "has_coordinator": True,
            "has_discovery_coordinator": True,
            "has_electricity_coordinator": True,
            "has_water_coordinator": True,
            "has_session": True,
            "ledger": {
                "has_ledger_store": False,
                "malformed": False,
                "entry_count": 0,
                "accepted_bucket_count": 0,
                "pending_bucket_count": 0,
                "warning_count": 0,
                "statistics_warning_count": 0,
                "source_backfill_statuses": {},
            },
        },
        "coordinators": {
            "discovery": {
                "last_update_success": True,
                "update_interval_seconds": None,
                "data": {
                    "available": True,
                    "premise_count": 1,
                    "account_count": 1,
                    "future_sensor_count": 2,
                    "electricity_candidate_count": 1,
                    "electricity_warning_count": 2,
                    "electricity_warning_codes": {
                        "unsupported_unit": 1,
                        "malformed_value": 1,
                    },
                    "water_candidate_count": 1,
                    "water_warning_count": 1,
                    "water_warning_codes": {"history_missing_data": 1},
                    "has_partial_errors": True,
                },
            },
            "electricity": {
                "last_update_success": True,
                "update_interval_seconds": 3600.0,
                "data": None,
            },
            "water": {
                "last_update_success": True,
                "update_interval_seconds": None,
                "data": None,
            },
        },
        "coordinator": {
            "last_update_success": True,
            "update_interval_seconds": None,
            "data": {
                "available": True,
                "premise_count": 1,
                "account_count": 1,
                "future_sensor_count": 2,
                "electricity_candidate_count": 1,
                "electricity_warning_count": 2,
                "electricity_warning_codes": {
                    "unsupported_unit": 1,
                    "malformed_value": 1,
                },
                "water_candidate_count": 1,
                "water_warning_count": 1,
                "water_warning_codes": {"history_missing_data": 1},
                "has_partial_errors": True,
            },
        },
    }
    _assert_sensitive_values_absent(diagnostics)
    rendered = repr(diagnostics)
    assert "SpGroupPremise" not in rendered
    assert "SpGroupElectricityCandidate" not in rendered
    assert "SpGroupWaterCandidate" not in rendered
    assert "accessToken" not in rendered


async def test_device_diagnostics_reuse_config_entry_summary(
    hass: HomeAssistant,
) -> None:
    """Device diagnostics stay aligned with config-entry diagnostics."""
    entry = _diagnostics_entry()

    diagnostics = await async_get_device_diagnostics(
        hass,
        entry,
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    _assert_sensitive_values_absent(diagnostics)
    assert diagnostics["coordinator"]["data"]["premise_count"] == 1


async def test_diagnostics_handle_missing_runtime_data(hass: HomeAssistant) -> None:
    """Diagnostics remain available before setup attaches runtime data."""
    session = make_mobile_session("auth0|iam-sensitive")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SP Group",
        data={
            CONF_ACCOUNT_ID: "auth0|iam-sensitive",
            CONF_SESSION: session.to_dict(),
        },
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["runtime"] == {
        "has_runtime_data": False,
        "has_coordinator": False,
        "has_discovery_coordinator": False,
        "has_electricity_coordinator": False,
        "has_water_coordinator": False,
        "has_session": False,
        "ledger": {
            "has_ledger_store": False,
            "malformed": False,
            "entry_count": 0,
            "accepted_bucket_count": 0,
            "pending_bucket_count": 0,
            "warning_count": 0,
            "statistics_warning_count": 0,
            "source_backfill_statuses": {},
        },
    }
    assert diagnostics["coordinator"]["data"] is None
    _assert_sensitive_values_absent(diagnostics)


async def test_diagnostics_include_bounded_source_backfill_status_histogram(
    hass: HomeAssistant,
) -> None:
    """Diagnostics expose source status counts without raw ledger identifiers."""
    entry = _diagnostics_entry()
    ledger_entries = {
        "raw-source-key-electric": SpGroupLedgerEntry.from_dict(
            _ledger_payload("raw-source-key-electric", "electric", "succeeded")
        ),
        "raw-source-key-water": SpGroupLedgerEntry.from_dict(
            _ledger_payload("raw-source-key-water", "water", "failed")
        ),
        "raw-source-key-water-2": SpGroupLedgerEntry.from_dict(
            _ledger_payload("raw-source-key-water-2", "water", "never_attempted")
        ),
    }
    entry.runtime_data.ledger_store = SimpleNamespace(
        snapshot=SpGroupLedgerSnapshot(entries=ledger_entries)
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["runtime"]["ledger"]["source_backfill_statuses"] == {
        "failed": 1,
        "never_attempted": 1,
        "succeeded": 1,
    }
    rendered = repr(diagnostics)
    assert "raw-source-key" not in rendered
    _assert_sensitive_values_absent(diagnostics)


def _diagnostics_entry() -> MockConfigEntry:
    """Create an entry with sensitive runtime values for diagnostics tests."""
    session = make_mobile_session(
        "auth0|iam-sensitive",
        access_token="access-token-secret",
        id_token="id-token-secret",
        refresh_token="refresh-token-secret",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SP Group",
        data={
            CONF_ACCOUNT_ID: "auth0|iam-sensitive",
            CONF_SESSION: session.to_dict(),
        },
    )
    data = SpGroupCoordinatorData(
        premises=(
            SpGroupPremise(
                key="opaque-premise",
                active=True,
                signed_up=True,
                shared=False,
                contestable_type=None,
                accounts=(
                    SpGroupUtilityAccount(
                        key="opaque-account",
                        premise_key="opaque-premise",
                        utility_types=("ELECTRIC", "WATER"),
                    ),
                ),
                jarvis_premise_id="JARVIS-PREMISE-SECRET",
            ),
        ),
        electricity_candidates=(
            SpGroupElectricityCandidate(
                premise_key="opaque-premise",
                account_key="opaque-account",
                source="history_chart",
                period_key="2026-07",
                status="ACTUAL",
                original_unit="kWh",
                value=Decimal("1.2"),
                value_kwh=Decimal("1.2"),
                semantic="period_usage",
            ),
        ),
        electricity_warnings=("unsupported_unit", "malformed_value"),
        water_candidates=(
            SpGroupWaterCandidate(
                premise_key="opaque-premise",
                account_key="opaque-account",
                source="history_chart",
                period_key="2026-07",
                status="ACTUAL",
                original_unit="m³",
                value=Decimal("2.3"),
                value_cubic_meters=Decimal("2.3"),
                semantic="premise_period_usage",
            ),
        ),
        water_warnings=("history_missing_data",),
        has_partial_errors=True,
    )
    discovery_coordinator = SimpleNamespace(
        data=data,
        last_update_success=True,
        update_interval=None,
    )
    electricity_coordinator = SimpleNamespace(
        last_update_success=True,
        update_interval=timedelta(hours=1),
    )
    water_coordinator = SimpleNamespace(
        last_update_success=True,
        update_interval=None,
    )
    entry.runtime_data = SpGroupRuntimeData(
        auth_client=object(),  # type: ignore[arg-type]
        jarvis_client=object(),  # type: ignore[arg-type]
        yggdrasil_client=object(),  # type: ignore[arg-type]
        profile_client=object(),  # type: ignore[arg-type]
        session=session,
        account_id="auth0|iam-sensitive",
        refresh_lock=asyncio.Lock(),
        discovery_coordinator=discovery_coordinator,  # type: ignore[arg-type]
        electricity_coordinator=electricity_coordinator,  # type: ignore[arg-type]
        water_coordinator=water_coordinator,  # type: ignore[arg-type]
    )
    return entry


def _ledger_payload(sensor_key: str, utility_type: str, status: str) -> dict[str, Any]:
    """Return a minimal ledger entry payload for diagnostics tests."""
    return {
        "sensor_key": sensor_key,
        "source": "manual_seed_plus_ami_deltas",
        "utility_type": utility_type,
        "epoch_id": f"epoch-{sensor_key}",
        "seed_value": "100",
        "seed_effective_from": "2026-07-01T00:00:00+08:00",
        "backfill_from": "2026-07-01T00:00:00+08:00",
        "latest_total": "100",
        "source_backfill_status": status,
        "accepted": [],
        "pending": [],
    }


def _assert_sensitive_values_absent(value: Any) -> None:
    """Assert sensitive fixture values do not appear anywhere in diagnostics."""
    rendered = repr(value)
    for sensitive in SENSITIVE_VALUES:
        assert sensitive not in rendered
