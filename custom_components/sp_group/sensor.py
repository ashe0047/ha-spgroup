"""Sensor platform for the SP Group integration."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SpGroupRuntimeData
from .coordinator import SpGroupDiscoveryCoordinator, SpGroupUtilityCoordinator
from .discovery import SpGroupFutureSensorKey
from .ledger import SpGroupLedgerEntry

LOGGER = logging.getLogger(__name__)

SENSOR_DESCRIPTIONS = {
    "electric": SensorEntityDescription(
        key="electric",
        translation_key="electricity",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    "water": SensorEntityDescription(
        key="water",
        translation_key="water",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[SpGroupRuntimeData],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SP Group sensors from a config entry."""
    discovery_coordinator = entry.runtime_data.discovery_coordinator
    if discovery_coordinator is None:
        return

    known_keys: set[str] = set()

    def add_discovered_entities() -> None:
        """Add entities for newly discovered coordinator sensor keys."""
        data = discovery_coordinator.data
        if data is None:
            return
        entities: list[SpGroupUtilitySensor] = []
        for sensor_key in data.future_sensor_keys():
            if sensor_key.key in known_keys:
                continue
            description = SENSOR_DESCRIPTIONS.get(sensor_key.utility_type)
            if description is None:
                continue
            utility_coordinator = _utility_coordinator(entry, sensor_key.utility_type)
            if utility_coordinator is None:
                continue
            known_keys.add(sensor_key.key)
            entities.append(
                SpGroupUtilitySensor(
                    utility_coordinator,
                    discovery_coordinator,
                    sensor_key,
                    description,
                )
            )
        if entities:
            LOGGER.debug(
                "SP Group entity update phase completed: added_entity_count=%s",
                len(entities),
            )
            async_add_entities(entities)

    add_discovered_entities()
    entry.async_on_unload(
        discovery_coordinator.async_add_listener(add_discovered_entities)
    )


class SpGroupUtilitySensor(
    CoordinatorEntity[SpGroupUtilityCoordinator], SensorEntity
):
    """Placeholder utility consumption sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SpGroupUtilityCoordinator,
        discovery_coordinator: SpGroupDiscoveryCoordinator | SpGroupFutureSensorKey,
        sensor_key: SpGroupFutureSensorKey | SensorEntityDescription,
        description: SensorEntityDescription | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        actual_discovery_coordinator: Any
        actual_sensor_key: SpGroupFutureSensorKey
        actual_description: SensorEntityDescription
        if description is None:
            if not hasattr(discovery_coordinator, "key"):
                raise TypeError("legacy sensor key is required")
            if not hasattr(sensor_key, "translation_key"):
                raise TypeError("legacy sensor description is required")
            actual_discovery_coordinator = coordinator
            actual_sensor_key = cast(SpGroupFutureSensorKey, discovery_coordinator)
            actual_description = cast(SensorEntityDescription, sensor_key)
        else:
            if not isinstance(discovery_coordinator, SpGroupDiscoveryCoordinator):
                raise TypeError("discovery coordinator is required")
            if not isinstance(sensor_key, SpGroupFutureSensorKey):
                raise TypeError("sensor key is required")
            actual_discovery_coordinator = discovery_coordinator
            actual_sensor_key = sensor_key
            actual_description = description
        self._discovery_coordinator: Any = actual_discovery_coordinator
        self._sensor_key = actual_sensor_key
        self.entity_description = actual_description
        self._attr_unique_id = actual_sensor_key.key
        self._attr_translation_key = actual_description.translation_key
        self._attr_native_unit_of_measurement = (
            actual_description.native_unit_of_measurement
        )

    @property
    def available(self) -> bool:
        """Return whether the discovered utility is currently available."""
        if not super().available:
            return False
        data = self._discovery_coordinator.data
        if data is None:
            return False
        return (
            any(
                sensor_key.key == self._sensor_key.key
                for sensor_key in data.future_sensor_keys()
            )
            and self._ledger_entry() is not None
        )

    @property
    def native_value(self) -> Decimal | None:
        """Return the latest trusted ledger total."""
        entry = self._ledger_entry()
        if entry is None:
            return None
        return entry.latest_total

    @property
    def device_class(self) -> SensorDeviceClass | None:
        """Return the dashboard device class only for trusted ledger sensors."""
        if self._ledger_entry() is None:
            return None
        if self._sensor_key.utility_type == "electric":
            return SensorDeviceClass.ENERGY
        if self._sensor_key.utility_type == "water":
            return SensorDeviceClass.WATER
        return None

    @property
    def state_class(self) -> SensorStateClass | None:
        """Return the statistics state class only for trusted ledger sensors."""
        if self._ledger_entry() is None:
            return None
        return SensorStateClass.TOTAL_INCREASING

    def _ledger_entry(self) -> SpGroupLedgerEntry | None:
        """Return the trusted ledger entry for this sensor, if available."""
        ledger_store = self.coordinator.entry.runtime_data.ledger_store
        if ledger_store is None or ledger_store.snapshot.malformed:
            return None
        entry = ledger_store.snapshot.entries.get(self._sensor_key.key)
        if entry is None or not entry.has_trusted_total:
            return None
        return entry


def _utility_coordinator(
    entry: ConfigEntry[SpGroupRuntimeData],
    utility_type: str,
) -> SpGroupUtilityCoordinator | None:
    if utility_type == "electric":
        return entry.runtime_data.electricity_coordinator
    if utility_type == "water":
        return entry.runtime_data.water_coordinator
    return None
