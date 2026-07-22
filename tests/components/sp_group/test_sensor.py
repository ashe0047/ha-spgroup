"""Test SP Group sensor boundary behavior."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from custom_components.sp_group import SpGroupRuntimeData
from custom_components.sp_group.const import CONF_ACCOUNT_ID, CONF_SESSION, DOMAIN
from custom_components.sp_group.coordinator import SpGroupDataUpdateCoordinator
from custom_components.sp_group.discovery import (
    SpGroupCoordinatorData,
    SpGroupFutureSensorKey,
    SpGroupPremise,
    SpGroupUtilityAccount,
)
from custom_components.sp_group.electricity import SpGroupElectricityCandidate
from custom_components.sp_group.ledger import SpGroupLedgerEntry, SpGroupLedgerSnapshot
from custom_components.sp_group.sensor import SENSOR_DESCRIPTIONS, SpGroupUtilitySensor
from custom_components.sp_group.sources import SINGAPORE_TZ
from custom_components.sp_group.water import SpGroupWaterCandidate
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .helpers import make_mobile_session
from .test_coordinator import (
    FakeJarvisClient,
    FakeYggdrasilClient,
    UtilityClientSentinel,
)


def test_sensor_native_value_is_memory_only_placeholder(
    hass: HomeAssistant,
) -> None:
    """Placeholder sensors do not call SDK/network methods for state."""
    session = make_mobile_session("auth0|account-1")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={
            CONF_ACCOUNT_ID: "auth0|account-1",
            CONF_SESSION: session.to_dict(),
        },
    )
    entry.runtime_data = SpGroupRuntimeData(
        auth_client=object(),  # type: ignore[arg-type]
        jarvis_client=UtilityClientSentinel(),  # type: ignore[arg-type]
        yggdrasil_client=UtilityClientSentinel(),  # type: ignore[arg-type]
        profile_client=UtilityClientSentinel(),  # type: ignore[arg-type]
        session=session,
        account_id="auth0|account-1",
        refresh_lock=asyncio.Lock(),
    )
    coordinator = SpGroupDataUpdateCoordinator(hass, entry)  # type: ignore[arg-type]
    entry.runtime_data.coordinator = coordinator
    data = discovery_data(
        (
            "sp_group_premise_opaque",
            ("ELECTRIC", "WATER"),
        )
    )
    coordinator.async_set_updated_data(data)
    electricity = SpGroupUtilitySensor(
        coordinator,
        SpGroupFutureSensorKey(
            key="sp_group_premise_opaque_electric",
            premise_key="sp_group_premise_opaque",
            utility_type="electric",
        ),
        SENSOR_DESCRIPTIONS["electric"],
    )
    water = SpGroupUtilitySensor(
        coordinator,
        SpGroupFutureSensorKey(
            key="sp_group_premise_opaque_water",
            premise_key="sp_group_premise_opaque",
            utility_type="water",
        ),
        SENSOR_DESCRIPTIONS["water"],
    )

    assert electricity.native_value is None
    assert electricity.device_class is None
    assert electricity.state_class is None
    assert water.native_value is None
    assert water.device_class is None
    assert water.state_class is None


def test_sensor_publishes_trusted_ledger_total(
    hass: HomeAssistant,
) -> None:
    """Seeded ledger-backed sensors expose dashboard-ready totals."""
    session = make_mobile_session("auth0|account-1")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={
            CONF_ACCOUNT_ID: "auth0|account-1",
            CONF_SESSION: session.to_dict(),
        },
    )
    entry.runtime_data = SpGroupRuntimeData(
        auth_client=object(),  # type: ignore[arg-type]
        jarvis_client=UtilityClientSentinel(),  # type: ignore[arg-type]
        yggdrasil_client=UtilityClientSentinel(),  # type: ignore[arg-type]
        profile_client=UtilityClientSentinel(),  # type: ignore[arg-type]
        session=session,
        account_id="auth0|account-1",
        refresh_lock=asyncio.Lock(),
        ledger_store=SimpleNamespace(
            snapshot=SpGroupLedgerSnapshot(
                entries={
                    "sp_group_premise_opaque_electric": SpGroupLedgerEntry(
                        sensor_key="sp_group_premise_opaque_electric",
                        source="manual_seed_plus_ami_deltas",
                        utility_type="electric",
                        epoch_id="epoch",
                        seed_value=Decimal("12345.6"),
                        seed_effective_from=datetime(
                            2026,
                            7,
                            15,
                            tzinfo=SINGAPORE_TZ,
                        ),
                        backfill_from=datetime(
                            2026,
                            7,
                            15,
                            tzinfo=SINGAPORE_TZ,
                        ),
                        latest_total=Decimal("12345.6"),
                    )
                }
            )
        ),  # type: ignore[arg-type]
    )
    coordinator = SpGroupDataUpdateCoordinator(hass, entry)  # type: ignore[arg-type]
    entry.runtime_data.coordinator = coordinator
    coordinator.async_set_updated_data(
        discovery_data(("sp_group_premise_opaque", ("ELECTRIC",)))
    )
    electricity = SpGroupUtilitySensor(
        coordinator,
        SpGroupFutureSensorKey(
            key="sp_group_premise_opaque_electric",
            premise_key="sp_group_premise_opaque",
            utility_type="electric",
        ),
        SENSOR_DESCRIPTIONS["electric"],
    )

    assert electricity.available
    assert electricity.native_value == Decimal("12345.6")
    assert electricity.device_class == "energy"
    assert electricity.state_class == "total_increasing"

    coordinator.last_update_success = False
    assert not electricity.available


def test_future_sensor_keys_are_derived_from_discovery_data() -> None:
    """Future sensor identity can be driven by coordinator discovery data."""
    account = SpGroupUtilityAccount(
        key="sp_group_account_opaque",
        premise_key="sp_group_premise_opaque",
        utility_types=("ELECTRIC", "WATER"),
    )
    data = SpGroupCoordinatorData(
        premises=(
            SpGroupPremise(
                key="sp_group_premise_opaque",
                active=True,
                signed_up=True,
                shared=False,
                contestable_type=None,
                accounts=(account,),
            ),
        )
    )

    assert data.future_sensor_keys()[0].key == "sp_group_premise_opaque_electric"
    assert data.future_sensor_keys()[1].key == "sp_group_premise_opaque_water"


async def test_sensor_setup_creates_discovery_driven_states_and_registry_entries(
    hass: HomeAssistant,
    caplog: Any,
) -> None:
    """Platform setup creates HA states and registry entries from discovery data."""
    caplog.set_level(logging.DEBUG, logger="custom_components.sp_group")
    entry = await setup_sensor_entry(hass)
    electric_id = sensor_unique_id(entry, "electric")
    water_id = sensor_unique_id(entry, "water")

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    unique_ids = {entity.unique_id for entity in entries}

    assert {electric_id, water_id} <= unique_ids
    assert_entity_state(hass, registry, electric_id, STATE_UNAVAILABLE)
    assert_entity_state(hass, registry, water_id, STATE_UNAVAILABLE)
    assert "entity update phase completed: added_entity_count=2" in "\n".join(
        record.getMessage() for record in caplog.records
    )


async def test_sensor_unique_ids_are_stable_across_display_payload_changes(
    hass: HomeAssistant,
) -> None:
    """Entity IDs use opaque discovery keys, not raw display fields."""
    entry = await setup_sensor_entry(hass)
    registry = er.async_get(hass)
    before = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }

    entry.runtime_data.coordinator.async_set_updated_data(
        entry.runtime_data.coordinator.data
    )
    await hass.async_block_till_done()
    after = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }

    assert before == after


async def test_sensor_dynamic_updates_add_new_entities_without_duplicates(
    hass: HomeAssistant,
) -> None:
    """Coordinator updates add newly discovered sensors once."""
    entry = await setup_sensor_entry(hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator is not None
    registry = er.async_get(hass)

    coordinator.async_set_updated_data(
        discovery_data(
            ("sp_group_premise_a", ("ELECTRIC", "WATER")),
            ("sp_group_premise_b", ("ELECTRIC",)),
        )
    )
    await hass.async_block_till_done()
    coordinator.async_set_updated_data(
        discovery_data(
            ("sp_group_premise_a", ("ELECTRIC", "WATER")),
            ("sp_group_premise_b", ("ELECTRIC",)),
        )
    )
    await hass.async_block_till_done()

    unique_ids = [
        entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    ]
    assert unique_ids.count("sp_group_premise_b_electric") == 1
    assert_entity_state(
        hass,
        registry,
        "sp_group_premise_b_electric",
        STATE_UNAVAILABLE,
    )


async def test_sensor_removed_discovery_key_becomes_unavailable(
    hass: HomeAssistant,
) -> None:
    """Stale registry entities become unavailable when discovery drops them."""
    entry = await setup_sensor_entry(hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator is not None
    water_id = sensor_unique_id(entry, "water")
    registry = er.async_get(hass)

    coordinator.async_set_updated_data(
        SpGroupCoordinatorData(
            premises=(
                SpGroupPremise(
                    key=coordinator.data.premises[0].key,
                    active=True,
                    signed_up=True,
                    shared=False,
                    contestable_type=None,
                    accounts=(
                        SpGroupUtilityAccount(
                            key=coordinator.data.accounts[0].key,
                            premise_key=coordinator.data.premises[0].key,
                            utility_types=("ELECTRIC",),
                        ),
                    ),
                ),
            )
        )
    )
    await hass.async_block_till_done()

    assert_entity_state(
        hass,
        registry,
        water_id,
        STATE_UNAVAILABLE,
    )


async def test_sensor_privacy_and_metadata_boundaries(
    hass: HomeAssistant,
) -> None:
    """Entities do not expose sensitive identifiers or dashboard metadata."""
    entry = await setup_sensor_entry(hass)
    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    electric_id = sensor_unique_id(entry, "electric")
    state = assert_entity_state(
        hass,
        registry,
        electric_id,
        STATE_UNAVAILABLE,
    )
    entity_entry = registry.async_get_entity_id(
        "sensor",
        DOMAIN,
        electric_id,
    )

    assert entity_entry is not None
    text = (
        f"{entity_entry} {state.attributes} "
        f"{repr(entry.runtime_data.coordinator.data)}"
    )
    for sensitive in (
        "ACC-1",
        "ACC-ROOT-1",
        "PREM-1",
        "JARVIS-PREMISE-1",
        "123 Secret Street",
        "person@example.test",
        "owner@example.test",
        "+6512345678",
    ):
        assert sensitive not in text
    assert "device_class" not in state.attributes
    assert "state_class" not in state.attributes
    assert "last_reset" not in state.attributes
    assert state.attributes.get("unit_of_measurement") == "kWh"
    assert not dr.async_entries_for_config_entry(device_registry, entry.entry_id)


async def test_sensor_rejects_unsafe_publishable_chart_candidates(
    hass: HomeAssistant,
) -> None:
    """Unsafe chart candidates do not make entities available or statistical."""
    entry = await setup_sensor_entry(hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator is not None
    electric_id = sensor_unique_id(entry, "electric")
    water_id = sensor_unique_id(entry, "water")
    premise_key = coordinator.data.premises[0].key
    account_key = coordinator.data.accounts[0].key
    registry = er.async_get(hass)

    coordinator.async_set_updated_data(
        SpGroupCoordinatorData(
            premises=coordinator.data.premises,
            electricity_candidates=(
                SpGroupElectricityCandidate(
                    premise_key=premise_key,
                    account_key=account_key,
                    source="history_chart",
                    period_key="2026-07",
                    status="ACTUAL",
                    original_unit="kWh",
                    value=Decimal("12.3"),
                    value_kwh=Decimal("12.3"),
                    semantic="period_usage",
                    publishable=True,
                ),
            ),
            water_candidates=(
                SpGroupWaterCandidate(
                    premise_key=premise_key,
                    account_key=account_key,
                    source="ami_chart",
                    period_key="20260714",
                    status=None,
                    original_unit="m³",
                    value=Decimal("4.2"),
                    value_cubic_meters=Decimal("4.2"),
                    semantic="chart_bucket",
                    publishable=True,
                ),
            ),
        )
    )
    await hass.async_block_till_done()

    electric_state = assert_entity_state(
        hass,
        registry,
        electric_id,
        STATE_UNAVAILABLE,
    )
    water_state = assert_entity_state(hass, registry, water_id, STATE_UNAVAILABLE)
    for state in (electric_state, water_state):
        assert "device_class" not in state.attributes
        assert "state_class" not in state.attributes
        assert "last_reset" not in state.attributes


def test_sensor_descriptions_use_home_assistant_unit_constants() -> None:
    """HA constants render to the expected dashboard-accepted units."""
    assert SENSOR_DESCRIPTIONS["electric"].native_unit_of_measurement == "kWh"
    assert SENSOR_DESCRIPTIONS["water"].native_unit_of_measurement == "m³"


async def test_sensor_entity_properties_do_not_call_sdk_clients(
    hass: HomeAssistant,
) -> None:
    """Entity properties read coordinator memory only."""
    entry = await setup_sensor_entry(hass)
    runtime = entry.runtime_data
    runtime.jarvis_client = UtilityClientSentinel()  # type: ignore[assignment]
    runtime.yggdrasil_client = UtilityClientSentinel()  # type: ignore[assignment]
    runtime.profile_client = UtilityClientSentinel()  # type: ignore[assignment]

    registry = er.async_get(hass)
    electric_id = sensor_unique_id(entry, "electric")
    state = assert_entity_state(
        hass,
        registry,
        electric_id,
        STATE_UNAVAILABLE,
    )

    assert state.state == STATE_UNAVAILABLE


def discovery_data(*premises: tuple[str, tuple[str, ...]]) -> SpGroupCoordinatorData:
    """Create redacted coordinator discovery data for sensor tests."""
    return SpGroupCoordinatorData(
        premises=tuple(
            SpGroupPremise(
                key=premise_key,
                active=True,
                signed_up=True,
                shared=False,
                contestable_type=None,
                accounts=(
                    SpGroupUtilityAccount(
                        key=f"{premise_key}_account",
                        premise_key=premise_key,
                        utility_types=utility_types,
                    ),
                ),
            )
            for premise_key, utility_types in premises
        )
    )


async def setup_sensor_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up the integration through HA so sensor entities reach the state machine."""
    session = make_mobile_session("auth0|account-1")
    calls: list[str] = []
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={
            CONF_ACCOUNT_ID: "auth0|account-1",
            CONF_SESSION: session.to_dict(),
        },
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.sp_group.async_get_clientsession",
            return_value=object(),
        ),
        patch("custom_components.sp_group.MobileAuthClient", client_factory(object())),
        patch("custom_components.sp_group.ProfileClient", client_factory(object())),
        patch(
            "custom_components.sp_group.JarvisClient",
            client_factory(FakeJarvisClient(calls)),
        ),
        patch(
            "custom_components.sp_group.YggdrasilClient",
            client_factory(FakeYggdrasilClient(calls)),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert isinstance(entry.runtime_data, SpGroupRuntimeData)
    return entry


def client_factory(client: object) -> Any:
    """Return an SDK constructor replacement that ignores the aiohttp session."""

    def create_client(session: object) -> object:
        return client

    return create_client


def assert_entity_state(
    hass: HomeAssistant,
    registry: er.EntityRegistry,
    unique_id: str,
    expected_state: str,
) -> Any:
    """Assert and return the HA state for an entity unique ID."""
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == expected_state
    return state


def sensor_unique_id(entry: MockConfigEntry, utility_type: str) -> str:
    """Return the discovered unique ID for a utility sensor."""
    coordinator = entry.runtime_data.coordinator
    assert coordinator is not None
    data = coordinator.data
    assert data is not None
    for sensor_key in data.future_sensor_keys():
        if sensor_key.utility_type == utility_type:
            return sensor_key.key
    raise AssertionError(f"missing sensor key for {utility_type}")
