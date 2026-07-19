"""Manual seed source configuration for SP Group sensors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from .const import CONF_SOURCE_CONFIG
from .discovery import SpGroupCoordinatorData, SpGroupFutureSensorKey

SINGAPORE_TZ = ZoneInfo("Asia/Singapore")
SOURCE_MANUAL_SEED_AMI = "manual_seed_plus_ami_deltas"
SOURCE_MANUAL_SEED_MONTHLY = "manual_seed_plus_monthly_deltas"
UTILITY_ELECTRIC = "electric"
UTILITY_WATER = "water"

_MAX_SEED_AGE_DAYS = 370


class SourceConfigError(ValueError):
    """Raised when source configuration is invalid."""


@dataclass(frozen=True, slots=True)
class SpGroupSourceConfig:
    """Dashboard-ready source configuration for one discovered utility."""

    sensor_key: str
    premise_key: str
    utility_type: str
    source: str
    seed_value: Decimal
    seed_observed_at: datetime
    seed_effective_from: datetime
    backfill_from: datetime
    boundary_aligned: bool
    epoch_id: str
    reset_confirmed: bool = False
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize source configuration for config-entry options."""
        return {
            "enabled": self.enabled,
            "premise_key": self.premise_key,
            "utility_type": self.utility_type,
            "source": self.source,
            "seed_value": str(self.seed_value),
            "seed_observed_at": self.seed_observed_at.isoformat(),
            "seed_effective_from": self.seed_effective_from.isoformat(),
            "backfill_from": self.backfill_from.isoformat(),
            "boundary_aligned": self.boundary_aligned,
            "epoch_id": self.epoch_id,
            "reset_confirmed": self.reset_confirmed,
        }

    @classmethod
    def from_dict(cls, sensor_key: str, payload: dict[str, Any]) -> SpGroupSourceConfig:
        """Deserialize source configuration from config-entry options."""
        utility_type = _required_string(payload, "utility_type")
        seed_observed_at = parse_seed_timestamp(
            _required_string(payload, "seed_observed_at")
        )
        seed_effective_from = parse_seed_timestamp(
            _required_string(payload, "seed_effective_from")
        )
        backfill_from = (
            parse_seed_timestamp(backfill_value)
            if isinstance(backfill_value := payload.get("backfill_from"), str)
            else seed_effective_from
        )
        return cls(
            sensor_key=sensor_key,
            premise_key=_required_string(payload, "premise_key"),
            utility_type=utility_type,
            source=_required_string(payload, "source"),
            seed_value=parse_decimal(_required_string(payload, "seed_value")),
            seed_observed_at=seed_observed_at,
            seed_effective_from=seed_effective_from,
            backfill_from=backfill_from,
            boundary_aligned=bool(payload.get("boundary_aligned")),
            epoch_id=_optional_epoch_id(sensor_key, payload),
            reset_confirmed=bool(payload.get("reset_confirmed")),
            enabled=payload.get("enabled") is not False,
        )


def enabled_source_configs(
    options: Mapping[str, Any],
) -> dict[str, SpGroupSourceConfig]:
    """Return enabled source configs keyed by sensor key."""
    raw_configs = options.get(CONF_SOURCE_CONFIG)
    if not isinstance(raw_configs, dict):
        return {}
    configs: dict[str, SpGroupSourceConfig] = {}
    for sensor_key, payload in raw_configs.items():
        if not isinstance(sensor_key, str) or not isinstance(payload, dict):
            continue
        try:
            config = SpGroupSourceConfig.from_dict(sensor_key, payload)
        except (SourceConfigError, ValueError):
            continue
        if config.enabled:
            configs[sensor_key] = config
    return configs


def build_source_config(
    *,
    sensor_key: SpGroupFutureSensorKey,
    seed_value: str,
    seed_observed_at: str,
    backfill_from: str,
    reset_confirmed: bool = False,
    now: datetime | None = None,
) -> SpGroupSourceConfig:
    """Build and validate a source config from config/options-flow input."""
    observed_at = parse_seed_timestamp(seed_observed_at)
    _validate_seed_timestamp(observed_at, now or datetime.now(SINGAPORE_TZ))
    value = parse_decimal(seed_value)
    if value < 0:
        raise SourceConfigError("seed_negative")
    if sensor_key.utility_type == UTILITY_ELECTRIC:
        effective_from, aligned = next_half_hour_boundary(observed_at)
        normalized_backfill_from = previous_half_hour_boundary(
            parse_backfill_timestamp(backfill_from)
        )
        source = SOURCE_MANUAL_SEED_AMI
    elif sensor_key.utility_type == UTILITY_WATER:
        effective_from, aligned = next_month_boundary(observed_at)
        normalized_backfill_from = month_start_boundary(
            parse_backfill_timestamp(backfill_from)
        )
        source = SOURCE_MANUAL_SEED_MONTHLY
    else:
        raise SourceConfigError("unsupported_utility")
    _validate_backfill_from(
        normalized_backfill_from,
        effective_from,
        now or datetime.now(SINGAPORE_TZ),
    )
    return SpGroupSourceConfig(
        sensor_key=sensor_key.key,
        premise_key=sensor_key.premise_key,
        utility_type=sensor_key.utility_type,
        source=source,
        seed_value=value,
        seed_observed_at=observed_at,
        seed_effective_from=effective_from,
        backfill_from=normalized_backfill_from,
        boundary_aligned=aligned,
        epoch_id=_epoch_id(
            sensor_key=sensor_key.key,
            source=source,
            seed_value=value,
            seed_observed_at=observed_at,
            seed_effective_from=effective_from,
            backfill_from=normalized_backfill_from,
        ),
        reset_confirmed=reset_confirmed,
    )


def source_options_payload(configs: dict[str, SpGroupSourceConfig]) -> dict[str, Any]:
    """Serialize source configs for config-entry options."""
    return {sensor_key: config.to_dict() for sensor_key, config in configs.items()}


def normalize_source_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return source options with readable source configs normalized."""
    return source_options_payload(enabled_source_configs(options))


def source_configs_for_data(
    data: SpGroupCoordinatorData,
    options: Mapping[str, Any],
) -> dict[str, SpGroupSourceConfig]:
    """Return enabled source configs that still match current discovery."""
    discovered = {
        sensor_key.key: sensor_key for sensor_key in data.future_sensor_keys()
    }
    configs: dict[str, SpGroupSourceConfig] = {}
    for sensor_key, config in enabled_source_configs(options).items():
        discovered_key = discovered.get(sensor_key)
        if discovered_key is None:
            continue
        if config.premise_key != discovered_key.premise_key:
            continue
        if config.utility_type != discovered_key.utility_type:
            continue
        if not _source_matches_utility(config.source, discovered_key.utility_type):
            continue
        configs[sensor_key] = config
    return configs


def parse_decimal(value: str) -> Decimal:
    """Parse a non-boolean decimal string."""
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as err:
        raise SourceConfigError("invalid_decimal") from err
    if not parsed.is_finite():
        raise SourceConfigError("invalid_decimal")
    return parsed


def parse_seed_timestamp(value: str) -> datetime:
    """Parse a seed timestamp and normalize it to Singapore time."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise SourceConfigError("invalid_timestamp") from err
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SINGAPORE_TZ)
    return parsed.astimezone(SINGAPORE_TZ)


def parse_backfill_timestamp(value: str) -> datetime:
    """Parse a backfill timestamp and normalize it to Singapore time."""
    try:
        return parse_seed_timestamp(value)
    except SourceConfigError as err:
        if str(err) == "invalid_timestamp":
            raise SourceConfigError("invalid_backfill") from err
        raise


def next_half_hour_boundary(value: datetime) -> tuple[datetime, bool]:
    """Return the next non-overlapping 30-minute AMI boundary."""
    local = value.astimezone(SINGAPORE_TZ).replace(second=0, microsecond=0)
    if local.minute in (0, 30):
        return local, True
    if local.minute < 30:
        return local.replace(minute=30), False
    return (local.replace(minute=0) + timedelta(hours=1), False)


def previous_half_hour_boundary(value: datetime) -> datetime:
    """Return the non-future 30-minute AMI boundary for historical backfill."""
    local = value.astimezone(SINGAPORE_TZ).replace(second=0, microsecond=0)
    if local.minute >= 30:
        return local.replace(minute=30)
    return local.replace(minute=0)


def next_month_boundary(value: datetime) -> tuple[datetime, bool]:
    """Return the next non-overlapping monthly bucket boundary."""
    local = value.astimezone(SINGAPORE_TZ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    aligned = local.day == 1 and value.hour == 0 and value.minute == 0
    if aligned and value.second == 0 and value.microsecond == 0:
        return local, True
    if local.month == 12:
        return local.replace(year=local.year + 1, month=1, day=1), False
    return local.replace(month=local.month + 1, day=1), False


def month_start_boundary(value: datetime) -> datetime:
    """Return the represented month start for historical monthly backfill."""
    return value.astimezone(SINGAPORE_TZ).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def utility_label(index: int, sensor_key: SpGroupFutureSensorKey) -> str:
    """Return a user-facing label for setup forms."""
    utility_name = _utility_display_name(sensor_key.utility_type)
    if sensor_key.display_label:
        return f"{sensor_key.display_label} - {utility_name}"
    return f"Premise {index} - {utility_name}"


def _utility_display_name(utility_type: str) -> str:
    if utility_type == UTILITY_ELECTRIC:
        return "Electricity (kWh)"
    if utility_type == UTILITY_WATER:
        return "Water (m³)"
    return utility_type.title()


def _source_matches_utility(source: str, utility_type: str) -> bool:
    if utility_type == UTILITY_ELECTRIC:
        return source == SOURCE_MANUAL_SEED_AMI
    if utility_type == UTILITY_WATER:
        return source == SOURCE_MANUAL_SEED_MONTHLY
    return False


def _validate_seed_timestamp(value: datetime, now: datetime) -> None:
    if value > now.astimezone(SINGAPORE_TZ):
        raise SourceConfigError("future_seed")
    if value < now.astimezone(SINGAPORE_TZ) - timedelta(days=_MAX_SEED_AGE_DAYS):
        raise SourceConfigError("seed_too_old")


def _validate_backfill_from(
    value: datetime,
    seed_effective_from: datetime,
    now: datetime,
) -> None:
    if value > now.astimezone(SINGAPORE_TZ):
        raise SourceConfigError("future_backfill")
    if value > seed_effective_from:
        raise SourceConfigError("backfill_after_seed")
    if value < now.astimezone(SINGAPORE_TZ) - timedelta(days=365 * 5):
        raise SourceConfigError("backfill_too_old")


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SourceConfigError(f"missing_{key}")
    return value


def _optional_epoch_id(sensor_key: str, payload: dict[str, Any]) -> str:
    value = payload.get("epoch_id")
    if isinstance(value, str) and value:
        return value
    seed_value = parse_decimal(_required_string(payload, "seed_value"))
    seed_effective_from = parse_seed_timestamp(
        _required_string(payload, "seed_effective_from")
    )
    return _legacy_epoch_id(
        sensor_key=sensor_key,
        source=_required_string(payload, "source"),
        seed_value=seed_value,
        seed_effective_from=seed_effective_from,
    )


def _legacy_epoch_id(
    *,
    sensor_key: str,
    source: str,
    seed_value: Decimal,
    seed_effective_from: datetime,
) -> str:
    """Return the epoch fallback used before epoch ids were persisted."""
    material = "|".join(
        (
            sensor_key,
            source,
            str(seed_value),
            seed_effective_from.isoformat(),
        )
    )
    return sha256(material.encode()).hexdigest()[:16]


def _epoch_id(
    *,
    sensor_key: str,
    source: str,
    seed_value: Decimal,
    seed_observed_at: datetime,
    seed_effective_from: datetime,
    backfill_from: datetime,
) -> str:
    material = "|".join(
        (
            sensor_key,
            source,
            str(seed_value),
            seed_observed_at.isoformat(),
            seed_effective_from.isoformat(),
            backfill_from.isoformat(),
        )
    )
    return sha256(material.encode()).hexdigest()[:16]


__all__ = [
    "SOURCE_MANUAL_SEED_AMI",
    "SOURCE_MANUAL_SEED_MONTHLY",
    "SINGAPORE_TZ",
    "SourceConfigError",
    "SpGroupSourceConfig",
    "build_source_config",
    "enabled_source_configs",
    "next_half_hour_boundary",
    "next_month_boundary",
    "month_start_boundary",
    "previous_half_hour_boundary",
    "source_configs_for_data",
    "source_options_payload",
    "normalize_source_options",
    "utility_label",
]
