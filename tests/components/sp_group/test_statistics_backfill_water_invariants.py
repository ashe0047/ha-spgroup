"""Water-focused statistics backfill invariant tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from custom_components.sp_group.const import DOMAIN
from custom_components.sp_group.ledger import (
    STATISTICS_STRATEGY_NATIVE_OFFSET_V2,
    SpGroupAcceptedBucket,
    SpGroupLedgerEntry,
)
from custom_components.sp_group.sources import SINGAPORE_TZ
from custom_components.sp_group.statistics_backfill import async_backfill_statistics
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.const import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder.models.statistics import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics as ha_async_import_statistics,
)
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .test_recorder_statistics import (
    STATISTICS_TOLERANCE,
    WATER_KEY,
    _accepted_bucket,
    _entry_with_ledger,
    _FakeLedgerStore,
    _FakeRecorderExecutor,
    _live_bug_water_buckets,
    _setup_recorder,
    _statistics_start,
    _wait_for_statistic_rows,
    _with_sum_offset,
)


async def test_water_backfill_reuses_persisted_reference_datetime_shape(
    hass: HomeAssistant,
) -> None:
    """Persisted water references match datetime-shaped Recorder rows."""
    hass.config.components.add(RECORDER_DOMAIN)
    _register_water_entity(hass)
    reference_start = datetime(2026, 8, 1, tzinfo=SINGAPORE_TZ)
    store = _FakeLedgerStore(
        replace(
            _water_ledger(
                (
                    _accepted_bucket(
                        "2026-07-01T00:00:00+08:00",
                        "2026-08-01T00:00:00+08:00",
                        "10",
                    ),
                ),
                seed=reference_start.replace(month=7),
                seed_value="1000",
            ),
            statistics_reference_start=reference_start,
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

    with _patched_statistics_import(
        recorder=recorder,
        existing_rows={
            "sensor.sp_group_water": [
                {"start": reference_start, "state": 1010.0, "sum": 0.0}
            ]
        },
        imported=imported,
    ):
        await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert imported
    assert store.reference_start == reference_start
    assert store.warnings == ()


async def test_water_backfill_reuses_persisted_reference_timestamp_shape(
    hass: HomeAssistant,
) -> None:
    """Persisted water references match timestamp-shaped Recorder rows."""
    hass.config.components.add(RECORDER_DOMAIN)
    _register_water_entity(hass)
    reference_start = datetime(2026, 8, 1, tzinfo=SINGAPORE_TZ)
    store = _FakeLedgerStore(
        replace(
            _water_ledger(
                (
                    _accepted_bucket(
                        "2026-07-01T00:00:00+08:00",
                        "2026-08-01T00:00:00+08:00",
                        "10",
                    ),
                ),
                seed=reference_start.replace(month=7),
                seed_value="1000",
            ),
            statistics_reference_start=reference_start,
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

    with _patched_statistics_import(
        recorder=recorder,
        existing_rows={
            "sensor.sp_group_water": [
                {
                    "start": reference_start.timestamp(),
                    "state": 1010.0,
                    "sum": 0.0,
                }
            ]
        },
        imported=imported,
    ):
        await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert imported
    assert store.reference_start == reference_start
    assert store.warnings == ()


async def test_water_existing_rows_are_idempotent_without_duplicate_import(
    hass: HomeAssistant,
) -> None:
    """Exact existing water rows are processed without duplicate import."""
    hass.config.components.add(RECORDER_DOMAIN)
    _register_water_entity(hass)
    reference_start = datetime(2026, 8, 1, tzinfo=SINGAPORE_TZ)
    store = _FakeLedgerStore(
        _water_ledger(
            (
                _accepted_bucket(
                    "2026-07-01T00:00:00+08:00",
                    "2026-08-01T00:00:00+08:00",
                    "10",
                ),
            ),
            seed=datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ),
            seed_value="1000",
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

    with _patched_statistics_import(
        recorder=recorder,
        existing_rows={
            "sensor.sp_group_water": [
                {
                    "start": datetime(2026, 6, 30, 23, tzinfo=SINGAPORE_TZ),
                    "state": 1000.0,
                    "sum": 0.0,
                },
                {
                    "start": datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ),
                    "state": 1010.0,
                    "sum": 10.0,
                },
                {"start": reference_start, "state": 1010.0, "sum": 10.0},
            ]
        },
        imported=imported,
    ):
        result = await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert imported == []
    assert result.processed_count == 2
    assert store.imported_through == datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ)
    assert store.strategy_version == STATISTICS_STRATEGY_NATIVE_OFFSET_V2
    assert store.warnings == ()


async def test_water_conflicting_existing_row_stops_import_and_creates_repair(
    hass: HomeAssistant,
) -> None:
    """A mismatched water row stops import and creates a conflict Repair."""
    hass.config.components.add(RECORDER_DOMAIN)
    _register_water_entity(hass)
    reference_start = datetime(2026, 8, 1, tzinfo=SINGAPORE_TZ)
    store = _FakeLedgerStore(
        _water_ledger(
            (
                _accepted_bucket(
                    "2026-07-01T00:00:00+08:00",
                    "2026-08-01T00:00:00+08:00",
                    "10",
                ),
            ),
            seed=datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ),
            seed_value="1000",
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

    with _patched_statistics_import(
        recorder=recorder,
        existing_rows={
            "sensor.sp_group_water": [
                {
                    "start": datetime(2026, 6, 30, 23, tzinfo=SINGAPORE_TZ),
                    "state": 9999.0,
                    "sum": -10.0,
                },
                {
                    "start": datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ),
                    "state": 1010.0,
                    "sum": 0.0,
                },
                {"start": reference_start, "state": 1010.0, "sum": 0.0},
            ]
        },
        imported=imported,
    ):
        result = await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert imported == []
    assert result.importable_count == 0
    assert result.processed_count == 0
    assert store.imported_through is None
    assert store.strategy_version is None
    assert store.warnings == (
        "statistics_conflict:2026-06-30T23:00:00+08:00",
    )
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN,
        f"statistics_backfill_conflict_entry-statistics_{WATER_KEY}",
    )
    assert issue is not None


async def test_water_existing_decreasing_sums_create_conflict_repair(
    hass: HomeAssistant,
) -> None:
    """Already-decreasing water sums block import before conflict filtering."""
    hass.config.components.add(RECORDER_DOMAIN)
    _register_water_entity(hass)
    reference_start = datetime(2026, 8, 1, tzinfo=SINGAPORE_TZ)
    store = _FakeLedgerStore(
        _water_ledger(
            (
                _accepted_bucket(
                    "2026-07-01T00:00:00+08:00",
                    "2026-08-01T00:00:00+08:00",
                    "10",
                ),
            ),
            seed=datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ),
            seed_value="1000",
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

    with _patched_statistics_import(
        recorder=recorder,
        existing_rows={
            "sensor.sp_group_water": [
                {
                    "start": datetime(2026, 6, 30, 23, tzinfo=SINGAPORE_TZ),
                    "state": 1000.0,
                    "sum": 0.0,
                },
                {
                    "start": datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ),
                    "state": 1010.0,
                    "sum": -1.0,
                },
                {"start": reference_start, "state": 1010.0, "sum": 0.0},
            ]
        },
        imported=imported,
    ):
        result = await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert imported == []
    assert result.planned_count == 0
    assert store.imported_through is None
    assert store.strategy_version is None
    assert store.warnings[0].startswith("statistics_existing_negative_delta:")
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN,
        f"statistics_backfill_conflict_entry-statistics_{WATER_KEY}",
    )
    assert issue is not None


async def test_water_missing_pre_seed_bridge_records_warning_without_import(
    hass: HomeAssistant,
) -> None:
    """Pre-seed water rows require a complete monthly bridge to the seed."""
    hass.config.components.add(RECORDER_DOMAIN)
    _register_water_entity(hass)
    store = _FakeLedgerStore(
        _water_ledger(
            (
                _accepted_bucket(
                    "2026-05-01T00:00:00+08:00",
                    "2026-06-01T00:00:00+08:00",
                    "7",
                ),
            ),
            seed=datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ),
            seed_value="1000",
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

    with _patched_statistics_import(
        recorder=recorder,
        existing_rows={},
        imported=imported,
    ):
        result = await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert imported == []
    assert result.planned_count == 0
    assert store.imported_through is None
    assert store.warnings == (
        "statistics_missing_bridge:2026-05-01T00:00:00+08:00",
    )


async def test_water_missing_post_seed_bridge_records_warning_without_import(
    hass: HomeAssistant,
) -> None:
    """Post-seed water rows require a complete monthly bridge from the seed."""
    hass.config.components.add(RECORDER_DOMAIN)
    _register_water_entity(hass)
    store = _FakeLedgerStore(
        _water_ledger(
            (
                _accepted_bucket(
                    "2026-08-01T00:00:00+08:00",
                    "2026-09-01T00:00:00+08:00",
                    "11",
                ),
            ),
            seed=datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ),
            seed_value="1000",
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

    with _patched_statistics_import(
        recorder=recorder,
        existing_rows={},
        imported=imported,
    ):
        result = await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert imported == []
    assert result.planned_count == 0
    assert store.imported_through is None
    assert store.warnings == (
        "statistics_missing_bridge:2026-08-01T00:00:00+08:00",
    )


async def test_water_live_fixture_validates_every_monthly_dashboard_delta(
    hass: HomeAssistant,
) -> None:
    """The live Aug 2025-Jun 2026 water fixture has no negative visible month."""
    hass.config.components.add(RECORDER_DOMAIN)
    _register_water_entity(hass)
    accepted = _live_bug_water_buckets()
    seed = datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ)
    store = _FakeLedgerStore(
        SpGroupLedgerEntry(
            sensor_key=WATER_KEY,
            source="manual_seed_plus_monthly_deltas",
            utility_type="water",
            epoch_id="epoch-water-live-bug-all-months",
            seed_value=Decimal("1000"),
            seed_effective_from=seed,
            backfill_from=datetime(2025, 1, 1, tzinfo=SINGAPORE_TZ),
            latest_total=Decimal("1000"),
            latest_accepted_at=seed,
            accepted=accepted,
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()
    native_reference = {
        "start": datetime(2026, 7, 20, 15, tzinfo=SINGAPORE_TZ).timestamp(),
        "state": 1000.0,
        "sum": 0.0,
    }

    with _patched_statistics_import(
        recorder=recorder,
        existing_rows={"sensor.sp_group_water": [native_reference]},
        imported=imported,
    ):
        await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert recorder.adjustments == [
        (
            "sensor.sp_group_water",
            datetime(2026, 7, 20, 15, tzinfo=SINGAPORE_TZ),
            244.4,
            "m³",
        )
    ]
    rows = (*imported[0], _with_sum_offset(native_reference, Decimal("244.4")))
    _assert_dashboard_deltas_from_zero(
        rows,
        visible_start=datetime(2025, 7, 1, tzinfo=SINGAPORE_TZ),
        visible_end=datetime(2026, 7, 21, tzinfo=SINGAPORE_TZ),
        expected=_live_bug_expected_water_deltas(),
    )


async def test_water_live_fixture_real_recorder_validates_every_monthly_delta(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """The real Recorder API must preserve every live-fixture water month."""
    await _setup_recorder(hass, tmp_path)
    entity_id = _register_water_entity(hass)
    accepted = _live_bug_water_buckets()
    seed = datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ)
    store = _FakeLedgerStore(
        SpGroupLedgerEntry(
            sensor_key=WATER_KEY,
            source="manual_seed_plus_monthly_deltas",
            utility_type="water",
            epoch_id="epoch-water-recorder-all-months",
            seed_value=Decimal("1000"),
            seed_effective_from=seed,
            backfill_from=datetime(2025, 1, 1, tzinfo=SINGAPORE_TZ),
            latest_total=Decimal("1000"),
            latest_accepted_at=seed,
            accepted=accepted,
        )
    )
    native_reference = {
        "start": datetime(2026, 7, 20, 15, tzinfo=SINGAPORE_TZ),
        "state": 1000.0,
        "sum": 0.0,
    }
    ha_async_import_statistics(
        hass,
        {
            "has_mean": False,
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": RECORDER_DOMAIN,
            "statistic_id": entity_id,
            "unit_class": "volume",
            "unit_of_measurement": "m³",
        },
        [native_reference],
    )
    await get_instance(hass).async_block_till_done()

    await async_backfill_statistics(hass, _entry_with_ledger(store))
    rows = await _wait_for_statistic_rows(
        hass,
        entity_id,
        datetime(2025, 7, 1, tzinfo=SINGAPORE_TZ),
        datetime(2026, 7, 21, tzinfo=SINGAPORE_TZ),
        minimum_count=12,
    )

    _assert_dashboard_deltas_from_zero(
        tuple(rows),
        visible_start=datetime(2025, 7, 1, tzinfo=SINGAPORE_TZ),
        visible_end=datetime(2026, 7, 21, tzinfo=SINGAPORE_TZ),
        expected=_live_bug_expected_water_deltas(),
    )


@contextmanager
def _patched_statistics_import(
    *,
    recorder: _FakeRecorderExecutor,
    existing_rows: dict[str, list[dict[str, Any]]],
    imported: list[list[dict[str, Any]]],
) -> Iterator[None]:
    """Patch Recorder calls for mocked statistics-import tests."""
    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value=existing_rows,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_import_statistics",
            side_effect=lambda _hass, _meta, rows: imported.append(list(rows)),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=recorder,
        ),
    ):
        yield


def _register_water_entity(hass: HomeAssistant) -> str:
    """Register the deterministic SP Group water entity and return its ID."""
    return er.async_get(hass).async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        WATER_KEY,
        suggested_object_id="sp_group_water",
    ).entity_id


def _water_ledger(
    accepted: tuple[SpGroupAcceptedBucket, ...],
    *,
    seed: datetime,
    seed_value: str,
) -> SpGroupLedgerEntry:
    """Return a water ledger entry with accepted monthly buckets."""
    return SpGroupLedgerEntry(
        sensor_key=WATER_KEY,
        source="manual_seed_plus_monthly_deltas",
        utility_type="water",
        epoch_id="epoch-water-invariants",
        seed_value=Decimal(seed_value),
        seed_effective_from=seed,
        backfill_from=min((bucket.start for bucket in accepted), default=seed),
        latest_total=Decimal(seed_value)
        + sum(
            (bucket.value for bucket in accepted if bucket.start >= seed),
            Decimal("0"),
        ),
        latest_accepted_at=max((bucket.end for bucket in accepted), default=None),
        accepted=accepted,
    )


def _assert_dashboard_deltas_from_zero(
    rows: tuple[dict[str, Any], ...],
    *,
    visible_start: datetime,
    visible_end: datetime,
    expected: dict[datetime, Decimal],
) -> None:
    """Assert visible dashboard deltas when there is no pre-window row."""
    deltas = _dashboard_deltas(
        rows,
        visible_start=visible_start,
        visible_end=visible_end,
        pre_window_sum=Decimal("0"),
    )
    negative = {
        start: delta for start, delta in deltas.items() if delta < -STATISTICS_TOLERANCE
    }
    assert negative == {}
    assert set(expected) <= set(deltas), deltas
    for start, expected_delta in expected.items():
        assert abs(deltas[start] - expected_delta) < STATISTICS_TOLERANCE, deltas


def _dashboard_deltas(
    rows: tuple[dict[str, Any], ...],
    *,
    visible_start: datetime,
    visible_end: datetime,
    pre_window_sum: Decimal,
) -> dict[datetime, Decimal]:
    """Return consecutive sum deltas for rows inside the visible range."""
    previous_sum = pre_window_sum
    deltas: dict[datetime, Decimal] = {}
    for row in sorted(rows, key=lambda item: _statistics_start(item)):
        start = _statistics_start(row)
        row_sum = Decimal(str(row["sum"]))
        if visible_start <= start < visible_end:
            deltas[start] = row_sum - previous_sum
        previous_sum = row_sum
    return deltas


def _live_bug_expected_water_deltas() -> dict[datetime, Decimal]:
    """Return expected visible deltas for every live-fixture water month."""
    expected = {
        datetime(2025, 7, 31, 23, tzinfo=SINGAPORE_TZ): Decimal("0"),
        datetime(2026, 7, 20, 15, tzinfo=SINGAPORE_TZ): Decimal("0"),
    }
    for bucket in _live_bug_water_buckets():
        expected[bucket.end.astimezone(SINGAPORE_TZ).replace(day=1) - _ONE_HOUR] = (
            bucket.value
        )
    return expected


_ONE_HOUR = timedelta(hours=1)
