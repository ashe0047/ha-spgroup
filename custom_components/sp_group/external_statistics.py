"""Identity and metadata helpers for SP Group external statistics."""

from __future__ import annotations

from hashlib import sha256

from homeassistant.components.recorder.models.statistics import (
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import valid_entity_id
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .const import DOMAIN
from .sources import UTILITY_ELECTRIC

EXTERNAL_STATISTICS_STRATEGY = "external_provider_v3"
EXTERNAL_STATISTICS_SOURCE = DOMAIN


def external_statistic_id(sensor_key: str) -> str:
    """Return the stable external statistic ID for an opaque sensor key."""
    statistic_id = f"{DOMAIN}.{sensor_key}_consumption"
    if not valid_entity_id(statistic_id):
        raise ValueError("invalid_external_statistic_id")
    return statistic_id


def external_identity_fingerprint(sensor_key: str, utility_type: str) -> str:
    """Return a stable non-secret identity fingerprint."""
    material = f"{DOMAIN}|{sensor_key}|{utility_type}"
    return sha256(material.encode()).hexdigest()[:16]


def external_metadata(
    statistic_id: str,
    utility_type: str,
) -> StatisticMetaData:
    """Return the HA metadata contract for one SP Group external series."""
    electricity = utility_type == UTILITY_ELECTRIC
    return {
        "has_mean": False,
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": f"SP Group {'Electricity' if electricity else 'Water'} consumption",
        "source": EXTERNAL_STATISTICS_SOURCE,
        "statistic_id": statistic_id,
        "unit_class": (
            EnergyConverter.UNIT_CLASS if electricity else VolumeConverter.UNIT_CLASS
        ),
        "unit_of_measurement": (
            UnitOfEnergy.KILO_WATT_HOUR if electricity else UnitOfVolume.CUBIC_METERS
        ),
    }


__all__ = [
    "EXTERNAL_STATISTICS_SOURCE",
    "EXTERNAL_STATISTICS_STRATEGY",
    "external_identity_fingerprint",
    "external_metadata",
    "external_statistic_id",
]
