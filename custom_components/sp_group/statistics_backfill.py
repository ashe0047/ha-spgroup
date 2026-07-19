"""HA Recorder statistics backfill for SP Group ledger buckets."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.const import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder.models.statistics import (
    StatisticData,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_utils

from .const import DOMAIN
from .ledger import SpGroupAcceptedBucket, SpGroupLedgerEntry
from .sources import SINGAPORE_TZ, UTILITY_ELECTRIC, UTILITY_WATER

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from . import SpGroupConfigEntry


@dataclass(frozen=True, slots=True)
class PlannedStatistic:
    """Deterministic statistic projection from accepted ledger buckets."""

    start: datetime
    state: Decimal
    sum: Decimal


_IMPORT_BATCH_SIZE = 1000


async def async_backfill_statistics(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
) -> None:
    """Import safe HA statistics rows for accepted ledger buckets."""
    if RECORDER_DOMAIN not in hass.config.components:
        return
    ledger_store = entry.runtime_data.ledger_store
    if ledger_store is None or ledger_store.snapshot.malformed:
        return
    registry = er.async_get(hass)
    for sensor_key, ledger_entry in ledger_store.snapshot.entries.items():
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, sensor_key)
        if entity_id is None:
            continue
        planned, bridge_warnings = _planned_statistics_with_warnings(ledger_entry)
        if ledger_entry.statistics_imported_through is not None:
            planned = tuple(
                row
                for row in planned
                if row.start > ledger_entry.statistics_imported_through
            )
        if not planned:
            if bridge_warnings:
                await ledger_store.async_update_statistics_metadata(
                    sensor_key,
                    warnings=bridge_warnings,
                )
            continue
        all_importable, all_processed, all_warnings, last_successful_start = (
            await _import_in_batches(
                hass,
                entity_id,
                planned,
                ledger_entry,
            )
        )
        # Import a continuation row at the current hour with the last known
        # sum to prevent HA's Recorder from inserting a sum=0 live row that
        # would cause the Energy Dashboard to show negative consumption for
        # the current period equal to the total backfill sum.
        if all_processed and last_successful_start is not None:
            last_processed = max(all_processed, key=lambda r: r.start)
            if last_processed.sum is not None:
                now = dt_utils.utcnow()
                current_hour = now.replace(minute=0, second=0, microsecond=0)
                continuation = PlannedStatistic(
                    start=current_hour,
                    state=Decimal(str(ledger_entry.latest_total)),
                    sum=last_processed.sum,
                )
                try:
                    async_import_statistics(
                        hass,
                        _metadata(entity_id, ledger_entry.utility_type),
                        [_statistic_data(continuation)],
                    )
                except Exception:  # noqa: BLE001
                    LOGGER.warning(
                        "Continuation row failed for %s (non-critical)",
                        entity_id,
                    )
        await ledger_store.async_update_statistics_metadata(
            sensor_key,
            imported_through=last_successful_start,
            warnings=tuple(dict.fromkeys((*bridge_warnings, *all_warnings))),
        )


async def _import_in_batches(
    hass: HomeAssistant,
    entity_id: str,
    planned: tuple[PlannedStatistic, ...],
    ledger_entry: SpGroupLedgerEntry,
) -> tuple[
    tuple[PlannedStatistic, ...],
    tuple[PlannedStatistic, ...],
    tuple[str, ...],
    datetime | None,
]:
    """Import planned statistics in batches to avoid blocking the Recorder queue."""
    all_importable: list[PlannedStatistic] = []
    all_processed: list[PlannedStatistic] = []
    all_warnings: list[str] = []
    metadata = _metadata(entity_id, ledger_entry.utility_type)
    last_successful: datetime | None = None

    for i in range(0, len(planned), _IMPORT_BATCH_SIZE):
        batch = planned[i : i + _IMPORT_BATCH_SIZE]
        importable, processed, warnings = await _filter_conflicts(
            hass, entity_id, batch,
        )
        all_importable.extend(importable)
        all_processed.extend(processed)
        all_warnings.extend(warnings)
        if importable:
            async_import_statistics(
                hass,
                metadata,
                [_statistic_data(row) for row in importable],
            )
        if processed:
            last_successful = max(processed, key=lambda r: r.start).start

    if not last_successful and all_processed:
        last_successful = max(all_processed, key=lambda r: r.start).start
    return (
        tuple(all_importable),
        tuple(all_processed),
            tuple(dict.fromkeys(all_warnings)),
        last_successful,
    )


def _planned_statistics(entry: SpGroupLedgerEntry) -> tuple[PlannedStatistic, ...]:
    """Return statistic rows, omitting warnings for test/backward compatibility."""
    planned, _warnings = _planned_statistics_with_warnings(entry)
    return planned


def _planned_statistics_with_warnings(
    entry: SpGroupLedgerEntry,
) -> tuple[tuple[PlannedStatistic, ...], tuple[str, ...]]:
    if entry.utility_type == UTILITY_ELECTRIC:
        return _electricity_statistics(entry)
    if entry.utility_type == UTILITY_WATER:
        return _water_statistics(entry)
    return (), ()


def _electricity_statistics(
    entry: SpGroupLedgerEntry,
) -> tuple[tuple[PlannedStatistic, ...], tuple[str, ...]]:
    by_hour: dict[datetime, list[SpGroupAcceptedBucket]] = defaultdict(list)
    accepted_by_start = _accepted_by_start(entry)
    for bucket in entry.accepted:
        hour_start = bucket.start.astimezone(SINGAPORE_TZ).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        by_hour[hour_start].append(bucket)

    rows: list[PlannedStatistic] = []
    warnings: list[str] = []
    cumulative = Decimal("0")
    for hour_start in sorted(by_hour):
        hour_end = hour_start + timedelta(hours=1)
        if hour_start < entry.seed_effective_from < hour_end:
            continue
        if not _has_contiguous_buckets(
            accepted_by_start,
            start=hour_start,
            end=hour_end,
            step=timedelta(minutes=30),
        ):
            warnings.append(f"statistics_missing_bridge:{hour_start.isoformat()}")
            continue
        if not _has_seed_bridge(
            entry,
            accepted_by_start,
            row_start=hour_start,
            row_end=hour_end,
            step=timedelta(minutes=30),
        ):
            warnings.append(f"statistics_missing_bridge:{hour_start.isoformat()}")
            continue
        ordered_buckets = sorted(by_hour[hour_start], key=lambda item: item.start)
        hour_delta = sum((bucket.value for bucket in ordered_buckets), Decimal("0"))
        cumulative += hour_delta
        rows.append(
            PlannedStatistic(
                start=hour_start,
                state=_state_at(entry, accepted_by_start, hour_end),
                sum=cumulative,
            )
        )
    return tuple(rows), tuple(dict.fromkeys(warnings))


def _water_statistics(
    entry: SpGroupLedgerEntry,
) -> tuple[tuple[PlannedStatistic, ...], tuple[str, ...]]:
    rows: list[PlannedStatistic] = []
    warnings: list[str] = []
    cumulative = Decimal("0")
    accepted_by_start = _accepted_by_start(entry)
    for bucket in sorted(entry.accepted, key=lambda item: item.start):
        cumulative += bucket.value
        row_start = (bucket.end.astimezone(SINGAPORE_TZ) - timedelta(hours=1)).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        if not _has_seed_bridge(
            entry,
            accepted_by_start,
            row_start=bucket.start,
            row_end=bucket.end,
            step=None,
        ):
            cumulative -= bucket.value
            warnings.append(f"statistics_missing_bridge:{bucket.start.isoformat()}")
            continue
        rows.append(
            PlannedStatistic(
                start=row_start,
                state=_state_at(entry, accepted_by_start, bucket.end),
                sum=cumulative,
            )
        )
    return tuple(rows), tuple(dict.fromkeys(warnings))


def _accepted_by_start(
    entry: SpGroupLedgerEntry,
) -> dict[datetime, SpGroupAcceptedBucket]:
    return {bucket.start.astimezone(SINGAPORE_TZ): bucket for bucket in entry.accepted}


def _has_seed_bridge(
    entry: SpGroupLedgerEntry,
    accepted_by_start: dict[datetime, SpGroupAcceptedBucket],
    *,
    row_start: datetime,
    row_end: datetime,
    step: timedelta | None,
) -> bool:
    seed = entry.seed_effective_from
    if row_start < seed < row_end:
        return False
    if row_end <= seed:
        return _has_contiguous_buckets(
            accepted_by_start,
            start=row_start,
            end=seed,
            step=step,
        )
    if row_start >= seed:
        return _has_contiguous_buckets(
            accepted_by_start,
            start=seed,
            end=row_end,
            step=step,
        )
    return False


def _has_contiguous_buckets(
    accepted_by_start: dict[datetime, SpGroupAcceptedBucket],
    *,
    start: datetime,
    end: datetime,
    step: timedelta | None,
) -> bool:
    current = start.astimezone(SINGAPORE_TZ)
    target = end.astimezone(SINGAPORE_TZ)
    if current >= target:
        return True
    while current < target:
        bucket = accepted_by_start.get(current)
        if bucket is None:
            return False
        current = (
            current + step
            if step is not None
            else bucket.end.astimezone(SINGAPORE_TZ)
        )
    return current == target


def _state_at(
    entry: SpGroupLedgerEntry,
    accepted_by_start: dict[datetime, SpGroupAcceptedBucket],
    at: datetime,
) -> Decimal:
    point = at.astimezone(SINGAPORE_TZ)
    seed = entry.seed_effective_from
    if point <= seed:
        return entry.seed_value - sum(
            (
                bucket.value
                for start, bucket in accepted_by_start.items()
                if point <= start < seed
            ),
            Decimal("0"),
        )
    return entry.seed_value + sum(
        (
            bucket.value
            for start, bucket in accepted_by_start.items()
            if seed <= start < point
        ),
        Decimal("0"),
    )


async def _filter_conflicts(
    hass: HomeAssistant,
    entity_id: str,
    planned: tuple[PlannedStatistic, ...],
) -> tuple[
    tuple[PlannedStatistic, ...],
    tuple[PlannedStatistic, ...],
    tuple[str, ...],
]:
    start = min(row.start for row in planned)
    end = max(row.start for row in planned) + timedelta(hours=1)
    existing = await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            start,
            end,
            {entity_id},
            "hour",
            None,
            {"state", "sum"},
        )
    )
    existing_rows = cast(list[dict[str, Any]], existing.get(entity_id, []))
    existing_by_start: dict[datetime, dict[str, Any]] = {
        row["start"]: row
        for row in existing_rows
        if isinstance(row.get("start"), datetime)
    }
    importable: list[PlannedStatistic] = []
    processed: list[PlannedStatistic] = []
    warnings: list[str] = []
    for row in planned:
        current = existing_by_start.get(row.start)
        if current is None:
            importable.append(row)
            processed.append(row)
            continue
        if _float_equal(current.get("state"), row.state) and _float_equal(
            current.get("sum"), row.sum
        ):
            processed.append(row)
            continue
        warnings.append(f"statistics_conflict:{row.start.isoformat()}")
        break
    return tuple(importable), tuple(processed), tuple(warnings)


def _metadata(entity_id: str, utility_type: str) -> StatisticMetaData:
    return {
        "has_mean": False,
        "has_sum": True,
        "name": None,
        "source": RECORDER_DOMAIN,
        "statistic_id": entity_id,
        "unit_of_measurement": (
            UnitOfEnergy.KILO_WATT_HOUR
            if utility_type == UTILITY_ELECTRIC
            else UnitOfVolume.CUBIC_METERS
        ),
    }


def _statistic_data(row: PlannedStatistic) -> StatisticData:
    return {
        "start": row.start,
        "state": float(row.state),
        "sum": float(row.sum),
    }


def _float_equal(value: object, expected: Decimal) -> bool:
    try:
        return abs(Decimal(str(value)) - expected) < Decimal("0.000001")
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "PlannedStatistic",
    "async_backfill_statistics",
]
