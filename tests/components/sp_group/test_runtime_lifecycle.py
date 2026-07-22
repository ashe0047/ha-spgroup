"""Test SP Group Home Assistant runtime lifecycle behavior."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from spgroup.mobile.errors import SpGroupMobileError

from .test_recorder_statistics import (
    ELECTRIC_KEY,
    WATER_KEY,
    _setup_seeded_entry,
)
from .test_sensor import assert_entity_state


async def test_seeded_runtime_setup_creates_dashboard_ready_entities(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """Config-entry setup creates seeded runtime entities through HA setup."""
    entry = await _setup_seeded_entry(hass, tmp_path)
    registry = er.async_get(hass)

    electric = assert_entity_state(hass, registry, ELECTRIC_KEY, "100.0")
    water = assert_entity_state(hass, registry, WATER_KEY, "10.0")

    assert entry.runtime_data.coordinator is not None
    assert entry.runtime_data.ledger_store is not None
    assert electric.attributes["device_class"] == "energy"
    assert electric.attributes["state_class"] == "total_increasing"
    assert water.attributes["device_class"] == "water"
    assert water.attributes["state_class"] == "total_increasing"


async def test_transient_runtime_refresh_failure_preserves_trusted_state(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """A transient coordinator failure does not clear the last trusted total."""
    entry = await _setup_seeded_entry(hass, tmp_path)
    coordinator = entry.runtime_data.electricity_coordinator
    assert coordinator is not None
    entry.runtime_data.jarvis_client.get_ami_charts = AsyncMock(  # type: ignore[method-assign]
        side_effect=SpGroupMobileError("raw secret")
    )

    await coordinator.async_request_refresh()
    await hass.async_block_till_done()

    assert isinstance(coordinator.last_exception, UpdateFailed)
    registry = er.async_get(hass)
    assert_entity_state(hass, registry, ELECTRIC_KEY, STATE_UNAVAILABLE)
    assert_entity_state(hass, registry, WATER_KEY, "10.0")
    assert (
        entry.runtime_data.ledger_store.snapshot.entries[ELECTRIC_KEY].latest_total
        == 100
    )


async def test_unseeded_runtime_entities_remain_unavailable(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """Runtime setup without seeds creates unavailable non-dashboard entities."""
    await _setup_seeded_entry(hass, tmp_path, seeded=False)
    registry = er.async_get(hass)

    electric = assert_entity_state(hass, registry, ELECTRIC_KEY, STATE_UNAVAILABLE)
    water = assert_entity_state(hass, registry, WATER_KEY, STATE_UNAVAILABLE)

    assert "device_class" not in electric.attributes
    assert "state_class" not in electric.attributes
    assert "device_class" not in water.attributes
    assert "state_class" not in water.attributes
