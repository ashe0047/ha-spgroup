"""HA Recorder statistics backfill for SP Group ledger buckets."""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.const import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder.models.statistics import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .ledger import (
    STATISTICS_STRATEGY_NATIVE_OFFSET_V2,
    WARNING_LEGACY_STATISTICS_STRATEGY_MIGRATED,
    SpGroupAcceptedBucket,
    SpGroupLedgerEntry,
)
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


@dataclass(frozen=True, slots=True)
class ProjectedStatistic:
    """Accepted source usage projected onto an HA statistics row."""

    start: datetime
    period_start: datetime
    period_end: datetime
    state: Decimal
    delta: Decimal


@dataclass(frozen=True, slots=True)
class NativeReference:
    """Validated HA-native statistics row used as the import anchor."""

    start: datetime
    state: Decimal
    sum: Decimal


@dataclass(frozen=True, slots=True)
class NativeOffset:
    """Offset that connects imported history with HA-native statistics."""

    reference: NativeReference
    desired: Decimal
    missing: Decimal


@dataclass(frozen=True, slots=True)
class StatisticsImportResult:
    """Summary of one statistics import run."""

    planned_count: int = 0
    importable_count: int = 0
    processed_count: int = 0
    warning_count: int = 0


@dataclass(frozen=True, slots=True)
class LedgerStatisticsIndex:
    """Indexed accepted buckets for efficient cumulative statistics planning."""

    entry: SpGroupLedgerEntry
    accepted_by_start: dict[datetime, SpGroupAcceptedBucket]
    starts: tuple[datetime, ...]
    prefix_values: tuple[Decimal, ...]
    half_hour_slots: tuple[int, ...]

    @classmethod
    def build(cls, entry: SpGroupLedgerEntry) -> LedgerStatisticsIndex:
        """Build an immutable calculation index for one ledger entry."""
        accepted_by_start = _accepted_by_start(entry)
        starts = tuple(sorted(accepted_by_start))
        prefix: list[Decimal] = [Decimal("0")]
        for start in starts:
            prefix.append(prefix[-1] + accepted_by_start[start].value)
        half_hour_slots = tuple(
            sorted(_half_hour_slot(start) for start in starts)
            if entry.utility_type == UTILITY_ELECTRIC
            else ()
        )
        return cls(
            entry=entry,
            accepted_by_start=accepted_by_start,
            starts=starts,
            prefix_values=tuple(prefix),
            half_hour_slots=half_hour_slots,
        )

    def state_at(self, at: datetime) -> Decimal:
        """Return the cumulative meter state at a point in Singapore time."""
        point = at.astimezone(SINGAPORE_TZ)
        seed = self.entry.seed_effective_from
        if self.entry.utility_type == UTILITY_WATER:
            return self._water_state_at(point, seed)
        if point <= seed:
            return self.entry.seed_value - self.sum_between(point, seed)
        return self.entry.seed_value + self.sum_between(seed, point)

    def _water_state_at(self, point: datetime, seed: datetime) -> Decimal:
        """Return water state using closed monthly bucket end boundaries."""
        if point <= seed:
            return self.entry.seed_value - sum(
                (
                    bucket.value
                    for bucket in self.entry.accepted
                    if point < bucket.end.astimezone(SINGAPORE_TZ) <= seed
                ),
                Decimal("0"),
            )
        return self.entry.seed_value + sum(
            (
                bucket.value
                for bucket in self.entry.accepted
                if bucket.start.astimezone(SINGAPORE_TZ) >= seed
                and bucket.end.astimezone(SINGAPORE_TZ) <= point
            ),
            Decimal("0"),
        )

    def sum_between(self, start: datetime, end: datetime) -> Decimal:
        """Return accepted bucket value for starts inside [start, end)."""
        local_start = start.astimezone(SINGAPORE_TZ)
        local_end = end.astimezone(SINGAPORE_TZ)
        left = bisect_left(self.starts, local_start)
        right = bisect_left(self.starts, local_end)
        return self.prefix_values[right] - self.prefix_values[left]

    def has_contiguous_fixed_range(
        self,
        *,
        start: datetime,
        end: datetime,
        step: timedelta,
    ) -> bool:
        """Return whether every fixed-size bucket start exists in [start, end)."""
        local_start = start.astimezone(SINGAPORE_TZ)
        local_end = end.astimezone(SINGAPORE_TZ)
        if local_start >= local_end:
            return True
        step_seconds = int(step.total_seconds())
        duration_seconds = int((local_end - local_start).total_seconds())
        if duration_seconds % step_seconds:
            return False
        expected = duration_seconds // step_seconds
        start_slot = _half_hour_slot(local_start)
        end_slot = start_slot + expected
        left = bisect_left(self.half_hour_slots, start_slot)
        right = bisect_left(self.half_hour_slots, end_slot)
        return right - left == expected

    def has_contiguous_variable_range(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> bool:
        """Return whether chained variable buckets fully cover [start, end)."""
        current = start.astimezone(SINGAPORE_TZ)
        target = end.astimezone(SINGAPORE_TZ)
        if current >= target:
            return True
        while current < target:
            bucket = self.accepted_by_start.get(current)
            if bucket is None:
                return False
            current = bucket.end.astimezone(SINGAPORE_TZ)
        return current == target


_IMPORT_BATCH_SIZE = 1000
_WAITING_ISSUE_PREFIX = "statistics_backfill_waiting"
_CONFLICT_ISSUE_PREFIX = "statistics_backfill_conflict"
_HALF_HOUR = timedelta(minutes=30)
_HALF_HOUR_SECONDS = int(_HALF_HOUR.total_seconds())
_SLOT_EPOCH = datetime(2000, 1, 1, tzinfo=SINGAPORE_TZ)



async def _async_backfill_external(
    hass: HomeAssistant,
    ledger_store: Any,
    sensor_key: str,
    ledger_entry: SpGroupLedgerEntry,
    *,
    entry_id: str,
    force: bool,
) -> StatisticsImportResult:
    """Import external statistics using provider-owned statistic ID."""
    del force
    projected, bridge_warnings = await hass.async_add_executor_job(
        _projected_statistics_with_warnings,
        ledger_entry,
    )
    if not projected:
        if bridge_warnings:
            await _update_statistics_metadata(
                ledger_store, sensor_key,
                warnings=bridge_warnings,
            )
        LOGGER.info(
            "SP Group external statistics skipped: utility=%s projected=0 "
            "warnings=%d",
            ledger_entry.utility_type,
            len(bridge_warnings),
        )
        return StatisticsImportResult(warning_count=len(bridge_warnings))

    planned = _planned_statistics(ledger_entry)
    if not planned:
        LOGGER.info(
            "SP Group external statistics skipped: utility=%s projected=%d "
            "planned=0",
            ledger_entry.utility_type,
            len(projected),
        )
        return StatisticsImportResult(warning_count=len(bridge_warnings))

    statistic_id = ledger_entry.external_statistic_id
    metadata = _metadata(statistic_id, ledger_entry.utility_type)
    async_import_statistics(
        hass, metadata,
        [_statistic_data(row) for row in planned],
    )

    last_start = max(row.start for row in planned)
    await _update_statistics_metadata(
        ledger_store, sensor_key,
        imported_through=last_start,
        warnings=bridge_warnings,
    )

    LOGGER.info(
        "SP Group external statistics reconciled: utility=%s "
        "statistic_id=%s planned=%d imported=%d",
        ledger_entry.utility_type,
        statistic_id,
        len(planned), len(planned),
    )
    return StatisticsImportResult(
        planned_count=len(planned),
        importable_count=len(planned),
        processed_count=len(planned),
        warning_count=len(bridge_warnings),
    )

async def async_backfill_statistics(
    hass: HomeAssistant,
    entry: SpGroupConfigEntry,
    *,
    force: bool = False,
) -> StatisticsImportResult:
    """Import safe HA statistics rows for accepted ledger buckets."""
    if RECORDER_DOMAIN not in hass.config.components:
        return StatisticsImportResult()
    ledger_store = entry.runtime_data.ledger_store
    if ledger_store is None or ledger_store.snapshot.malformed:
        return StatisticsImportResult()
    registry = er.async_get(hass)
    total_planned = 0
    total_importable = 0
    total_processed = 0
    total_warnings = 0
    for sensor_key, ledger_entry in ledger_store.snapshot.entries.items():
        if ledger_entry.external_statistic_id is not None:
            result = await _async_backfill_external(
                hass, ledger_store, sensor_key, ledger_entry,
                entry_id=entry.entry_id, force=force,
            )
            total_planned += result.planned_count
            total_importable += result.importable_count
            total_processed += result.processed_count
            total_warnings += result.warning_count
            continue
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, sensor_key)
        if entity_id is None:
            continue
        projected, bridge_warnings = await hass.async_add_executor_job(
            _projected_statistics_with_warnings,
            ledger_entry,
        )
        if not projected:
            if bridge_warnings:
                LOGGER.info(
                    "SP Group statistics import skipped: utility=%s projected=0 "
                    "warnings=%d",
                    ledger_entry.utility_type,
                    len(bridge_warnings),
                )
                await _update_statistics_metadata(
                    ledger_store,
                    sensor_key,
                    warnings=bridge_warnings,
                )
                total_warnings += len(bridge_warnings)
            continue
        planned, anchor_warnings, native_offset = await _native_anchored_statistics(
            hass,
            entity_id,
            ledger_entry,
            projected,
            force=force,
        )
        all_warnings = tuple(dict.fromkeys((*bridge_warnings, *anchor_warnings)))
        total_planned += len(planned)
        if not planned:
            LOGGER.info(
                "SP Group statistics import not ready: utility=%s projected=%d "
                "planned=0 warnings=%s",
                ledger_entry.utility_type,
                len(projected),
                ",".join(all_warnings) if all_warnings else "none",
            )
            await _update_statistics_metadata(
                ledger_store,
                sensor_key,
                reference_start=(
                    native_offset.reference.start
                    if native_offset is not None
                    else None
                ),
                reference_sum_offset=(
                    native_offset.desired if native_offset is not None else None
                ),
                warnings=all_warnings,
            )
            total_warnings += len(all_warnings)
            _create_or_clear_statistics_issues(
                hass,
                entry.entry_id,
                sensor_key,
                all_warnings,
            )
            continue
        if native_offset is not None and native_offset.missing > Decimal("0"):
            LOGGER.info(
                "SP Group statistics native offset applying: utility=%s "
                "reference_start=%s desired_offset=%s missing_offset=%s",
                ledger_entry.utility_type,
                native_offset.reference.start.isoformat(),
                native_offset.desired,
                native_offset.missing,
            )
            await _async_apply_reference_offset(
                hass,
                entity_id,
                ledger_entry,
                native_offset,
            )
        elif native_offset is not None:
            LOGGER.debug(
                "SP Group statistics native offset skipped: utility=%s "
                "reference_start=%s desired_offset=%s missing_offset=%s",
                ledger_entry.utility_type,
                native_offset.reference.start.isoformat(),
                native_offset.desired,
                native_offset.missing,
            )
        _all_importable, _all_processed, import_warnings, last_successful_start = (
            await _import_in_batches(
                hass,
                entity_id,
                planned,
                ledger_entry,
            )
        )
        final_warnings = tuple(
            dict.fromkeys((*bridge_warnings, *anchor_warnings, *import_warnings))
        )
        LOGGER.info(
            "SP Group statistics import planned: utility=%s planned=%d "
            "importable=%d processed=%d warnings=%d",
            ledger_entry.utility_type,
            len(planned),
            len(_all_importable),
            len(_all_processed),
            len(final_warnings),
        )
        total_importable += len(_all_importable)
        total_processed += len(_all_processed)
        total_warnings += len(final_warnings)
        await _update_statistics_metadata(
            ledger_store,
            sensor_key,
            imported_through=last_successful_start,
            reference_start=(
                native_offset.reference.start
                if native_offset is not None
                else None
            ),
            reference_sum_offset=(
                native_offset.desired if native_offset is not None else None
            ),
            strategy_version=(
                STATISTICS_STRATEGY_NATIVE_OFFSET_V2
                if last_successful_start is not None
                else None
            ),
            warnings=final_warnings,
        )
        LOGGER.info(
            "SP Group statistics metadata updated: utility=%s imported_through=%s "
            "reference_start=%s strategy=%s warnings=%d",
            ledger_entry.utility_type,
            last_successful_start.isoformat()
            if last_successful_start is not None
            else "none",
            native_offset.reference.start.isoformat()
            if native_offset is not None
            else "none",
            STATISTICS_STRATEGY_NATIVE_OFFSET_V2
            if last_successful_start is not None
            else "none",
            len(final_warnings),
        )
        _create_or_clear_statistics_issues(
            hass,
            entry.entry_id,
            sensor_key,
            final_warnings,
        )
    return StatisticsImportResult(
        planned_count=total_planned,
        importable_count=total_importable,
        processed_count=total_processed,
        warning_count=total_warnings,
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
    projected, _warnings = _projected_statistics_with_warnings(entry)
    if not projected:
        return ()
    reference = NativeReference(
        start=max(row.start for row in projected) + timedelta(hours=1),
        state=projected[-1].state,
        sum=sum((row.delta for row in projected), Decimal("0")),
    )
    return _positive_projected_rows(projected, reference)


def _planned_statistics_with_warnings(
    entry: SpGroupLedgerEntry,
) -> tuple[tuple[PlannedStatistic, ...], tuple[str, ...]]:
    projected, warnings = _projected_statistics_with_warnings(entry)
    if not projected:
        return (), warnings
    reference = NativeReference(
        start=max(row.start for row in projected) + timedelta(hours=1),
        state=projected[-1].state,
        sum=sum((row.delta for row in projected), Decimal("0")),
    )
    return _positive_projected_rows(projected, reference), warnings


def _projected_statistics_with_warnings(
    entry: SpGroupLedgerEntry,
) -> tuple[tuple[ProjectedStatistic, ...], tuple[str, ...]]:
    index = LedgerStatisticsIndex.build(entry)
    if entry.utility_type == UTILITY_ELECTRIC:
        return _electricity_statistics(entry, index)
    if entry.utility_type == UTILITY_WATER:
        return _water_statistics(entry, index)
    return (), ()


def _electricity_statistics(
    entry: SpGroupLedgerEntry,
    index: LedgerStatisticsIndex,
) -> tuple[tuple[ProjectedStatistic, ...], tuple[str, ...]]:
    by_hour: dict[datetime, list[SpGroupAcceptedBucket]] = defaultdict(list)
    for bucket in entry.accepted:
        hour_start = bucket.start.astimezone(SINGAPORE_TZ).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        by_hour[hour_start].append(bucket)

    rows: list[ProjectedStatistic] = []
    warnings: list[str] = []
    for hour_start in sorted(by_hour):
        hour_end = hour_start + timedelta(hours=1)
        if hour_start < entry.seed_effective_from < hour_end:
            # The seed bisects this hour.  Only generate a row when both
            # pre-seed and post-seed halves of the hour have contiguous buckets,
            # so the cumulative chain (seed - pre_sum) -> (seed + post_sum) is
            # continuous.
            if not _has_indexed_contiguous_range(
                index,
                start=hour_start,
                end=entry.seed_effective_from,
                step=_HALF_HOUR,
            ):
                continue
            if not _has_indexed_contiguous_range(
                index,
                start=entry.seed_effective_from,
                end=hour_end,
                step=_HALF_HOUR,
            ):
                continue
        if not index.has_contiguous_fixed_range(
            start=hour_start,
            end=hour_end,
            step=_HALF_HOUR,
        ):
            warnings.append(f"statistics_missing_bridge:{hour_start.isoformat()}")
            continue
        if not _has_seed_bridge(
            entry,
            index,
            row_start=hour_start,
            row_end=hour_end,
            step=_HALF_HOUR,
        ):
            warnings.append(f"statistics_missing_bridge:{hour_start.isoformat()}")
            continue
        ordered_buckets = sorted(by_hour[hour_start], key=lambda item: item.start)
        hour_delta = sum((bucket.value for bucket in ordered_buckets), Decimal("0"))
        rows.append(
            ProjectedStatistic(
                start=hour_start,
                period_start=hour_start,
                period_end=hour_end,
                state=index.state_at(hour_end),
                delta=hour_delta,
            )
        )
    return tuple(rows), tuple(dict.fromkeys(warnings))


def _water_statistics(
    entry: SpGroupLedgerEntry,
    index: LedgerStatisticsIndex,
) -> tuple[tuple[ProjectedStatistic, ...], tuple[str, ...]]:
    rows: list[ProjectedStatistic] = []
    warnings: list[str] = []
    for bucket in sorted(entry.accepted, key=lambda item: item.start):
        row_start = (bucket.end.astimezone(SINGAPORE_TZ) - timedelta(hours=1)).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        if not _has_seed_bridge(
            entry,
            index,
            row_start=bucket.start,
            row_end=bucket.end,
            step=None,
        ):
            warnings.append(f"statistics_missing_bridge:{bucket.start.isoformat()}")
            continue
        rows.append(
            ProjectedStatistic(
                start=row_start,
                period_start=bucket.start,
                period_end=bucket.end,
                state=index.state_at(bucket.end),
                delta=bucket.value,
            )
        )
    return tuple(rows), tuple(dict.fromkeys(warnings))


async def _native_anchored_statistics(
    hass: HomeAssistant,
    entity_id: str,
    entry: SpGroupLedgerEntry,
    projected: tuple[ProjectedStatistic, ...],
    *,
    force: bool,
) -> tuple[tuple[PlannedStatistic, ...], tuple[str, ...], NativeOffset | None]:
    existing_start = min(
        (
            min(row.start for row in projected) - timedelta(hours=1),
            entry.statistics_reference_start,
        )
        if entry.statistics_reference_start is not None
        else (min(row.start for row in projected) - timedelta(hours=1),)
    )
    existing = await _existing_statistics(hass, entity_id, existing_start, None)
    LOGGER.info(
        "SP Group statistics existing rows loaded: utility=%s projected=%d "
        "existing=%d",
        entry.utility_type,
        len(projected),
        len(existing),
    )
    return await hass.async_add_executor_job(
        _native_anchored_statistics_from_existing,
        entry,
        projected,
        existing,
        force,
    )


def _native_anchored_statistics_from_existing(
    entry: SpGroupLedgerEntry,
    projected: tuple[ProjectedStatistic, ...],
    existing: list[dict[str, Any]],
    force: bool = False,
) -> tuple[tuple[PlannedStatistic, ...], tuple[str, ...], NativeOffset | None]:
    contamination = _existing_decreasing_sum_warning(existing)
    if contamination is not None:
        return (), (contamination,), None
    if (
        entry.statistics_strategy_version != STATISTICS_STRATEGY_NATIVE_OFFSET_V2
        and entry.statistics_imported_through is not None
    ):
        post_legacy = [
            row
            for row in existing
            if isinstance(row.get("start"), datetime)
            and row["start"] > entry.statistics_imported_through
        ]
        if post_legacy:
            return (
                (),
                (
                    WARNING_LEGACY_STATISTICS_STRATEGY_MIGRATED,
                    "statistics_legacy_rows_need_manual_rebuild",
                ),
                None,
            )
    reference = _select_native_reference(entry, projected, existing)
    if reference is None:
        return (), ("statistics_waiting_for_native_reference",), None
    rows_before_reference = tuple(
        row for row in projected if row.start < reference.start
    )
    native_offset = _native_offset(entry, rows_before_reference, reference)
    if not rows_before_reference and not force:
        return (), (), native_offset
    planned = list(_positive_projected_rows(rows_before_reference, reference))
    if force:
        planned.extend(
            _forward_projected_rows(
                projected,
                reference,
                reference_sum_adjustment=native_offset.missing,
            )
        )
    if not planned:
        return (), (), native_offset
    return tuple(sorted(planned, key=lambda row: row.start)), (), native_offset


async def _existing_statistics(
    hass: HomeAssistant,
    entity_id: str,
    start: datetime,
    end: datetime | None,
) -> list[dict[str, Any]]:
    existing = await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            start.astimezone(UTC),
            end.astimezone(UTC) if end is not None else None,
            {entity_id},
            "hour",
            None,
            {"state", "sum"},
        )
    )
    return cast(list[dict[str, Any]], existing.get(entity_id, []))


def _select_native_reference(
    entry: SpGroupLedgerEntry,
    projected: tuple[ProjectedStatistic, ...],
    existing: list[dict[str, Any]],
) -> NativeReference | None:
    index = LedgerStatisticsIndex.build(entry)
    if entry.statistics_reference_start is not None:
        for row in existing:
            start = _statistics_start_datetime(row.get("start"))
            if start == entry.statistics_reference_start:
                return _validated_reference(entry, index, row)
        return None
    latest_projected_start = max(row.start for row in projected)
    for row in sorted(existing, key=_statistics_row_start, reverse=True):
        start = _statistics_start_datetime(row.get("start"))
        if start is None:
            LOGGER.info(
                "SP Group statistics reference rejected: utility=%s reason=no_start "
                "keys=%s",
                entry.utility_type,
                ",".join(str(key) for key in row.keys()),
            )
            continue
        if start <= latest_projected_start:
            LOGGER.info(
                "SP Group statistics reference rejected: utility=%s "
                "reason=before_projected row_start=%s latest_projected=%s",
                entry.utility_type,
                start.isoformat(),
                latest_projected_start.isoformat(),
            )
            continue
        reference = _validated_reference(entry, index, row)
        if reference is not None:
            return reference
        expected_state = index.state_at(
            start.astimezone(SINGAPORE_TZ) + timedelta(hours=1)
        )
        LOGGER.info(
            "SP Group statistics reference rejected: utility=%s "
            "reason=state_mismatch row_start=%s row_state=%s row_sum=%s "
            "expected_state=%s",
            entry.utility_type,
            start.isoformat(),
            row.get("state"),
            row.get("sum"),
            expected_state,
        )
    return None


def _validated_reference(
    entry: SpGroupLedgerEntry,
    index: LedgerStatisticsIndex,
    row: dict[str, Any],
) -> NativeReference | None:
    del entry
    start = _statistics_start_datetime(row.get("start"))
    state = row.get("state")
    stat_sum = row.get("sum")
    if start is None or state is None or stat_sum is None:
        return None
    expected_state = index.state_at(start.astimezone(SINGAPORE_TZ) + timedelta(hours=1))
    if not _float_equal(state, expected_state):
        return None
    return NativeReference(
        start=start,
        state=Decimal(str(state)),
        sum=Decimal(str(stat_sum)),
    )


def _native_offset(
    entry: SpGroupLedgerEntry,
    projected_before_reference: tuple[ProjectedStatistic, ...],
    reference: NativeReference,
) -> NativeOffset:
    desired = sum(
        (row.delta for row in projected_before_reference),
        Decimal("0"),
    )
    applied = entry.statistics_reference_sum_offset or Decimal("0")
    missing = max(Decimal("0"), desired - applied)
    return NativeOffset(reference=reference, desired=desired, missing=missing)


def _positive_projected_rows(
    projected: tuple[ProjectedStatistic, ...],
    reference: NativeReference,
) -> tuple[PlannedStatistic, ...]:
    del reference
    if not projected:
        return ()
    running_sum = Decimal("0")
    rows: list[PlannedStatistic] = []
    first = projected[0]
    rows.append(
        PlannedStatistic(
            start=first.period_start - timedelta(hours=1),
            state=first.state - first.delta,
            sum=running_sum,
        )
    )
    for row in projected:
        running_sum += row.delta
        rows.append(
            PlannedStatistic(
                start=row.start,
                state=row.state,
                sum=running_sum,
            )
        )
    return tuple(sorted(rows, key=lambda item: item.start))


def _forward_projected_rows(
    projected: tuple[ProjectedStatistic, ...],
    reference: NativeReference,
    *,
    reference_sum_adjustment: Decimal = Decimal("0"),
) -> tuple[PlannedStatistic, ...]:
    """Return rows after a native reference for forced gap repair."""
    running_sum = reference.sum + reference_sum_adjustment
    rows: list[PlannedStatistic] = []
    for row in sorted(projected, key=lambda item: item.start):
        if row.start <= reference.start:
            continue
        running_sum += row.delta
        rows.append(
            PlannedStatistic(
                start=row.start,
                state=row.state,
                sum=running_sum,
            )
        )
    return tuple(rows)


async def _async_apply_reference_offset(
    hass: HomeAssistant,
    entity_id: str,
    ledger_entry: SpGroupLedgerEntry,
    native_offset: NativeOffset,
) -> None:
    """Shift HA-native sums from the reference onward by the missing offset."""
    recorder = get_instance(hass)
    recorder.async_adjust_statistics(
        entity_id,
        native_offset.reference.start,
        float(native_offset.missing),
        str(_metadata(entity_id, ledger_entry.utility_type)["unit_of_measurement"]),
    )
    await recorder.async_block_till_done()


def _existing_decreasing_sum_warning(
    existing: list[dict[str, Any]],
) -> str | None:
    previous: Decimal | None = None
    previous_start: datetime | None = None
    for row in sorted(existing, key=_statistics_row_start):
        start = _statistics_start_datetime(row.get("start"))
        value = row.get("sum")
        if start is None or value is None:
            continue
        current = Decimal(str(value))
        if previous is not None and current < previous - Decimal("0.000001"):
            return (
                "statistics_existing_negative_delta:"
                f"{previous_start.isoformat() if previous_start else 'unknown'}"
                f":{start.isoformat()}"
            )
        previous = current
        previous_start = start
    return None


def _statistics_row_start(row: dict[str, Any]) -> datetime:
    if (start := _statistics_start_datetime(row.get("start"))) is not None:
        return start
    return datetime.min.replace(tzinfo=SINGAPORE_TZ)


def _statistics_start_datetime(value: object) -> datetime | None:
    """Normalize HA statistics row starts across HA return shapes."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, UTC)
    return None


def _accepted_by_start(
    entry: SpGroupLedgerEntry,
) -> dict[datetime, SpGroupAcceptedBucket]:
    return {bucket.start.astimezone(SINGAPORE_TZ): bucket for bucket in entry.accepted}


def _has_seed_bridge(
    entry: SpGroupLedgerEntry,
    index: LedgerStatisticsIndex,
    *,
    row_start: datetime,
    row_end: datetime,
    step: timedelta | None,
) -> bool:
    seed = entry.seed_effective_from
    if row_start < seed < row_end:
        if not _has_indexed_contiguous_range(
            index, start=row_start, end=seed, step=step
        ):
            return False
        if not _has_indexed_contiguous_range(
            index, start=seed, end=row_end, step=step
        ):
            return False
        return True
    if row_end <= seed:
        if entry.utility_type == UTILITY_ELECTRIC:
            return True
        return _has_indexed_contiguous_range(
            index,
            start=row_start,
            end=seed,
            step=step,
        )
    if row_start >= seed:
        return _has_indexed_contiguous_range(
            index,
            start=seed,
            end=row_end,
            step=step,
        )
    return False


def _has_indexed_contiguous_range(
    index: LedgerStatisticsIndex,
    *,
    start: datetime,
    end: datetime,
    step: timedelta | None,
) -> bool:
    if step is None:
        return index.has_contiguous_variable_range(start=start, end=end)
    return index.has_contiguous_fixed_range(start=start, end=end, step=step)


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
    del accepted_by_start
    return LedgerStatisticsIndex.build(entry).state_at(at)


def _half_hour_slot(value: datetime) -> int:
    local = value.astimezone(SINGAPORE_TZ)
    return int((local - _SLOT_EPOCH).total_seconds() // _HALF_HOUR_SECONDS)


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
    existing_rows = await _existing_statistics(hass, entity_id, start, end)
    return await hass.async_add_executor_job(
        _filter_conflicts_from_existing,
        planned,
        existing_rows,
    )


def _filter_conflicts_from_existing(
    planned: tuple[PlannedStatistic, ...],
    existing_rows: list[dict[str, Any]],
) -> tuple[
    tuple[PlannedStatistic, ...],
    tuple[PlannedStatistic, ...],
    tuple[str, ...],
]:
    existing_by_start: dict[datetime, dict[str, Any]] = {
        start: row
        for row in existing_rows
        if (start := _statistics_start_datetime(row.get("start"))) is not None
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
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": f"SP Group {'Electricity' if utility_type == UTILITY_ELECTRIC else 'Water'} consumption",
        "source": RECORDER_DOMAIN,
        "statistic_id": entity_id,
        "unit_class": "energy" if utility_type == UTILITY_ELECTRIC else "volume",
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


async def _update_statistics_metadata(
    ledger_store: Any,
    sensor_key: str,
    *,
    imported_through: datetime | None = None,
    reference_start: datetime | None = None,
    reference_sum_offset: Decimal | None = None,
    strategy_version: str | None = None,
    warnings: tuple[str, ...] = (),
) -> None:
    await ledger_store.async_update_statistics_metadata(
        sensor_key,
        imported_through=imported_through,
        reference_start=reference_start,
        reference_sum_offset=reference_sum_offset,
        strategy_version=strategy_version,
        warnings=warnings,
    )


def _create_or_clear_statistics_issues(
    hass: HomeAssistant,
    entry_id: str,
    sensor_key: str,
    warnings: tuple[str, ...],
) -> None:
    waiting_issue = f"{_WAITING_ISSUE_PREFIX}_{entry_id}_{sensor_key}"
    conflict_issue = f"{_CONFLICT_ISSUE_PREFIX}_{entry_id}_{sensor_key}"
    # Waiting for HA's native statistics compiler is normal after setup and is
    # not user-actionable yet.  Clear any older waiting repairs and only create
    # repair issues for states that need user intervention.
    ir.async_delete_issue(hass, DOMAIN, waiting_issue)

    conflict = next(
        (
            warning
            for warning in warnings
            if warning.startswith("statistics_existing_negative_delta:")
            or warning.startswith("statistics_conflict:")
            or warning == "statistics_legacy_rows_need_manual_rebuild"
        ),
        None,
    )
    if conflict is not None:
        ir.async_create_issue(
            hass,
            DOMAIN,
            conflict_issue,
            is_fixable=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="statistics_backfill_conflict",
            translation_placeholders={"warning": conflict},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, conflict_issue)


__all__ = [
    "PlannedStatistic",
    "StatisticsImportResult",
    "async_backfill_statistics",
]
