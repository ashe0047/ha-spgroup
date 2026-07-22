"""Electricity invariants for SP Group historical statistics backfill."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from custom_components.sp_group.const import DOMAIN
from custom_components.sp_group.sources import SINGAPORE_TZ
from custom_components.sp_group.statistics_backfill import async_backfill_statistics
from homeassistant.components.recorder import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models.statistics import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics as ha_async_import_statistics,
)
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .test_recorder_statistics import (
    ELECTRIC_KEY,
    STATISTICS_TOLERANCE,
    _accepted_bucket,
    _entry_with_ledger,
    _FakeLedgerStore,
    _FakeRecorderExecutor,
    _ledger_entry,
    _setup_recorder,
    _wait_for_statistic_rows,
    _with_sum_offset,
)


async def test_electricity_visible_synthetic_baseline_is_not_negative(
    hass: HomeAssistant,
) -> None:
    """A visible synthetic baseline must not become negative consumption."""
    hass.config.components.add(RECORDER_DOMAIN)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        ELECTRIC_KEY,
        suggested_object_id="sp_group_electricity",
    )
    store = _FakeLedgerStore(
        _ledger_entry(
            ELECTRIC_KEY,
            "electric",
            "100",
            (
                _accepted_bucket(
                    "2026-07-14T01:00:00+08:00",
                    "2026-07-14T01:30:00+08:00",
                    "1.25",
                ),
                _accepted_bucket(
                    "2026-07-14T01:30:00+08:00",
                    "2026-07-14T02:00:00+08:00",
                    "0.75",
                ),
            ),
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()
    native_reference = {
        "start": datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ).timestamp(),
        "state": 102.0,
        "sum": 0.0,
    }

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={"sensor.sp_group_electricity": [native_reference]},
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
        await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert imported
    assert recorder.adjustments == [
        (
            "sensor.sp_group_electricity",
            datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ),
            2.0,
            "kWh",
        )
    ]
    _assert_visible_dashboard_deltas(
        (*imported[0], _with_sum_offset(native_reference, Decimal("2"))),
        visible_start=datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ),
        visible_end=datetime(2026, 7, 14, 3, tzinfo=SINGAPORE_TZ),
        pre_window_sum=Decimal("0"),
        expected={
            datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ): Decimal("0"),
            datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ): Decimal("2"),
            datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ): Decimal("0"),
        },
    )


async def test_electricity_live_like_hourly_deltas_are_never_negative(
    hass: HomeAssistant,
) -> None:
    """Imported electricity history must expose source-hour deltas positively."""
    hass.config.components.add(RECORDER_DOMAIN)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        ELECTRIC_KEY,
        suggested_object_id="sp_group_electricity",
    )
    store = _FakeLedgerStore(
        _ledger_entry(
            ELECTRIC_KEY,
            "electric",
            "1000",
            (
                _accepted_bucket(
                    "2026-07-14T01:00:00+08:00",
                    "2026-07-14T01:30:00+08:00",
                    "1.25",
                ),
                _accepted_bucket(
                    "2026-07-14T01:30:00+08:00",
                    "2026-07-14T02:00:00+08:00",
                    "0.75",
                ),
                _accepted_bucket(
                    "2026-07-14T02:00:00+08:00",
                    "2026-07-14T02:30:00+08:00",
                    "1.5",
                ),
                _accepted_bucket(
                    "2026-07-14T02:30:00+08:00",
                    "2026-07-14T03:00:00+08:00",
                    "1.0",
                ),
                _accepted_bucket(
                    "2026-07-14T03:00:00+08:00",
                    "2026-07-14T03:30:00+08:00",
                    "0.5",
                ),
                _accepted_bucket(
                    "2026-07-14T03:30:00+08:00",
                    "2026-07-14T04:00:00+08:00",
                    "0.75",
                ),
            ),
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()
    native_reference = {
        "start": datetime(2026, 7, 14, 4, tzinfo=SINGAPORE_TZ),
        "state": 1005.75,
        "sum": 0.0,
    }

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={"sensor.sp_group_electricity": [native_reference]},
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
        await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert imported
    assert recorder.adjustments == [
        (
            "sensor.sp_group_electricity",
            datetime(2026, 7, 14, 4, tzinfo=SINGAPORE_TZ),
            5.75,
            "kWh",
        )
    ]
    _assert_visible_dashboard_deltas(
        (*imported[0], _with_sum_offset(native_reference, Decimal("5.75"))),
        visible_start=datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ),
        visible_end=datetime(2026, 7, 14, 5, tzinfo=SINGAPORE_TZ),
        pre_window_sum=Decimal("0"),
        expected={
            datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ): Decimal("0"),
            datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ): Decimal("2.0"),
            datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ): Decimal("2.5"),
            datetime(2026, 7, 14, 3, tzinfo=SINGAPORE_TZ): Decimal("1.25"),
            datetime(2026, 7, 14, 4, tzinfo=SINGAPORE_TZ): Decimal("0"),
        },
    )


async def test_electricity_visible_deltas_from_real_recorder_rows(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """The electricity negative-baseline shape must fail through real Recorder."""
    await _setup_recorder(hass, tmp_path)
    registry = er.async_get(hass)
    entity_id = registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        ELECTRIC_KEY,
        suggested_object_id="sp_group_electricity",
    ).entity_id
    store = _FakeLedgerStore(
        _ledger_entry(
            ELECTRIC_KEY,
            "electric",
            "1000",
            (
                _accepted_bucket(
                    "2026-07-14T01:00:00+08:00",
                    "2026-07-14T01:30:00+08:00",
                    "1.25",
                ),
                _accepted_bucket(
                    "2026-07-14T01:30:00+08:00",
                    "2026-07-14T02:00:00+08:00",
                    "0.75",
                ),
                _accepted_bucket(
                    "2026-07-14T02:00:00+08:00",
                    "2026-07-14T02:30:00+08:00",
                    "1.5",
                ),
                _accepted_bucket(
                    "2026-07-14T02:30:00+08:00",
                    "2026-07-14T03:00:00+08:00",
                    "1.0",
                ),
            ),
        )
    )
    native_reference = {
        "start": datetime(2026, 7, 14, 3, tzinfo=SINGAPORE_TZ),
        "state": 1004.5,
        "sum": 0.0,
    }
    ha_async_import_statistics(
        hass,
        {
            "has_mean": False,
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": None,
            "source": RECORDER_DOMAIN,
            "statistic_id": entity_id,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        [native_reference],
    )
    await get_instance(hass).async_block_till_done()

    await async_backfill_statistics(hass, _entry_with_ledger(store))
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()
    rows = await _wait_for_statistic_rows(
        hass,
        entity_id,
        datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ),
        datetime(2026, 7, 14, 4, tzinfo=SINGAPORE_TZ),
        minimum_count=4,
    )

    _assert_visible_dashboard_deltas(
        tuple(rows),
        visible_start=datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ),
        visible_end=datetime(2026, 7, 14, 4, tzinfo=SINGAPORE_TZ),
        pre_window_sum=Decimal("0"),
        expected={
            datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ): Decimal("0"),
            datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ): Decimal("2.0"),
            datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ): Decimal("2.5"),
            datetime(2026, 7, 14, 3, tzinfo=SINGAPORE_TZ): Decimal("0"),
        },
    )


def _assert_visible_dashboard_deltas(
    rows: tuple[dict[str, Any], ...],
    *,
    visible_start: datetime,
    visible_end: datetime,
    pre_window_sum: Decimal,
    expected: dict[datetime, Decimal],
) -> None:
    """Assert visible Energy Dashboard deltas, including the first visible row."""
    deltas = _visible_dashboard_deltas(
        rows,
        visible_start=visible_start,
        visible_end=visible_end,
        pre_window_sum=pre_window_sum,
    )
    negative = {
        start: delta for start, delta in deltas.items() if delta < -STATISTICS_TOLERANCE
    }
    assert negative == {}
    for start, expected_delta in expected.items():
        actual = deltas.get(start)
        assert actual is not None, deltas
        assert abs(actual - expected_delta) < STATISTICS_TOLERANCE, deltas


def _visible_dashboard_deltas(
    rows: tuple[dict[str, Any], ...],
    *,
    visible_start: datetime,
    visible_end: datetime,
    pre_window_sum: Decimal,
) -> dict[datetime, Decimal]:
    """Return consecutive sum deltas for every row visible to the dashboard."""
    previous_sum = pre_window_sum
    deltas: dict[datetime, Decimal] = {}
    for row in sorted(rows, key=_row_start):
        start = _row_start(row)
        row_sum = Decimal(str(row["sum"]))
        if visible_start <= start < visible_end:
            deltas[start] = row_sum - previous_sum
        previous_sum = row_sum
    return deltas


def _row_start(row: dict[str, Any]) -> datetime:
    """Normalize statistics row starts returned by mocks or Recorder."""
    value = row["start"]
    if isinstance(value, datetime):
        return value.astimezone(SINGAPORE_TZ)
    return datetime.fromtimestamp(float(value), tz=SINGAPORE_TZ)
