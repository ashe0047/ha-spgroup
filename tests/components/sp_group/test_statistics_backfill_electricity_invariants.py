"""Electricity invariants for SP Group historical statistics backfill."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from custom_components.sp_group.const import DOMAIN
from custom_components.sp_group.ledger import (
    PROVENANCE_HISTORY_MONTHLY,
    SpGroupLedgerEntry,
)
from custom_components.sp_group.sources import SINGAPORE_TZ
from custom_components.sp_group.statistics_backfill import (
    _projected_statistics_with_warnings,
    async_backfill_statistics,
)
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


def test_electricity_history_month_projects_as_end_of_period_row() -> None:
    """A history-only electricity month coexists with later AMI hourly rows."""
    history_month = replace(
        _accepted_bucket(
            "2026-06-01T00:00:00+08:00",
            "2026-07-01T00:00:00+08:00",
            "12",
        ),
        provenance=PROVENANCE_HISTORY_MONTHLY,
    )
    entry = _ledger_entry(
        ELECTRIC_KEY,
        "electric",
        "100",
        (
            history_month,
            _accepted_bucket(
                "2026-07-01T00:00:00+08:00",
                "2026-07-01T00:30:00+08:00",
                "1",
            ),
            _accepted_bucket(
                "2026-07-01T00:30:00+08:00",
                "2026-07-01T01:00:00+08:00",
                "2",
            ),
        ),
    )

    rows, warnings = _projected_statistics_with_warnings(entry)

    assert warnings == ()
    assert [(row.start, row.delta) for row in rows] == [
        (datetime(2026, 6, 30, 23, tzinfo=SINGAPORE_TZ), Decimal("12")),
        (datetime(2026, 7, 1, 0, tzinfo=SINGAPORE_TZ), Decimal("3")),
    ]


def test_electricity_statistics_rejects_projected_start_collision() -> None:
    """Mixed-cadence overlap fails closed instead of dropping silently."""
    history_month = replace(
        _accepted_bucket(
            "2026-06-01T00:00:00+08:00",
            "2026-07-01T00:00:00+08:00",
            "12",
        ),
        provenance=PROVENANCE_HISTORY_MONTHLY,
    )
    entry = _ledger_entry(
        ELECTRIC_KEY,
        "electric",
        "100",
        (
            history_month,
            _accepted_bucket(
                "2026-06-30T23:00:00+08:00",
                "2026-06-30T23:30:00+08:00",
                "1",
            ),
            _accepted_bucket(
                "2026-06-30T23:30:00+08:00",
                "2026-07-01T00:00:00+08:00",
                "1",
            ),
        ),
    )
    rows, warnings = _projected_statistics_with_warnings(entry)
    assert rows == ()
    assert warnings == ("statistics_duplicate_projected_start",)


def test_electricity_statistics_projects_consecutive_history_months() -> None:
    """Consecutive monthly History buckets produce one row per month."""
    entry = _ledger_entry(
        ELECTRIC_KEY,
        "electric",
        "100",
        tuple(
            replace(
                _accepted_bucket(start, end, value),
                provenance=PROVENANCE_HISTORY_MONTHLY,
            )
            for start, end, value in (
                ("2026-01-01T00:00:00+08:00", "2026-02-01T00:00:00+08:00", "10"),
                ("2026-02-01T00:00:00+08:00", "2026-03-01T00:00:00+08:00", "20"),
            )
        ),
    )
    rows, warnings = _projected_statistics_with_warnings(entry)
    assert warnings == ()
    assert [row.delta for row in rows] == [Decimal("10"), Decimal("20")]
    assert [row.start for row in rows] == [
        datetime(2026, 1, 31, 23, tzinfo=SINGAPORE_TZ),
        datetime(2026, 2, 28, 23, tzinfo=SINGAPORE_TZ),
    ]


def test_electricity_statistics_projects_history_partial_and_full_transition() -> None:
    """History, allocated half-hours, and full AMI remain one cadence chain."""
    history = replace(
        _accepted_bucket(
            "2026-01-01T00:00:00+08:00",
            "2026-02-01T00:00:00+08:00",
            "10",
        ),
        provenance=PROVENANCE_HISTORY_MONTHLY,
    )
    partial = (
        replace(
            _accepted_bucket(
                "2026-02-01T00:00:00+08:00",
                "2026-02-01T00:30:00+08:00",
                "1",
            ),
            provenance="history_allocated",
        ),
        _accepted_bucket(
            "2026-02-01T00:30:00+08:00",
            "2026-02-01T01:00:00+08:00",
            "1",
        ),
    )
    full = (
        _accepted_bucket(
            "2026-02-01T01:00:00+08:00",
            "2026-02-01T01:30:00+08:00",
            "1",
        ),
        _accepted_bucket(
            "2026-02-01T01:30:00+08:00",
            "2026-02-01T02:00:00+08:00",
            "1",
        ),
    )
    entry = _ledger_entry(ELECTRIC_KEY, "electric", "100", (history, *partial, *full))
    rows, warnings = _projected_statistics_with_warnings(entry)
    assert warnings == ()
    assert sum((row.delta for row in rows), Decimal("0")) == Decimal("14")
    assert len({row.start for row in rows}) == len(rows)


def test_electricity_zero_provenance_values_project_with_same_statistics_effect() -> (
    None
):
    """Observed, AMI-gap, and no-source zeros preserve one hourly bridge."""
    buckets = tuple(
        replace(
            _accepted_bucket(start, end, "0"),
            provenance=provenance,
        )
        for start, end, provenance in (
            (
                "2026-07-01T00:00:00+08:00",
                "2026-07-01T00:30:00+08:00",
                "ami_observed_zero",
            ),
            (
                "2026-07-01T00:30:00+08:00",
                "2026-07-01T01:00:00+08:00",
                "ami_gap_zero",
            ),
            (
                "2026-07-01T01:00:00+08:00",
                "2026-07-01T01:30:00+08:00",
                "no_source_gap_zero",
            ),
            (
                "2026-07-01T01:30:00+08:00",
                "2026-07-01T02:00:00+08:00",
                "ami_observed_zero",
            ),
        )
    )
    entry = _ledger_entry(ELECTRIC_KEY, "electric", "100", buckets)
    rows, warnings = _projected_statistics_with_warnings(entry)
    assert warnings == ()
    assert len(rows) == 2
    assert all(row.delta == 0 for row in rows)


async def test_electricity_hybrid_projection_imports_monthly_and_half_hour_rows(
    hass: HomeAssistant,
) -> None:
    """The public statistics path accepts a monthly-to-AMI transition."""
    hass.config.components.add(RECORDER_DOMAIN)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        ELECTRIC_KEY,
        suggested_object_id="sp_group_electricity",
    )
    history = replace(
        _accepted_bucket(
            "2026-06-01T00:00:00+08:00",
            "2026-07-01T00:00:00+08:00",
            "12",
        ),
        provenance=PROVENANCE_HISTORY_MONTHLY,
    )
    ami = (
        _accepted_bucket("2026-07-01T00:00:00+08:00", "2026-07-01T00:30:00+08:00", "1"),
        _accepted_bucket("2026-07-01T00:30:00+08:00", "2026-07-01T01:00:00+08:00", "2"),
    )
    store = _FakeLedgerStore(
        _ledger_entry(ELECTRIC_KEY, "electric", "100", (history, *ami))
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()
    native_reference = {
        "start": datetime(2026, 7, 1, 1, tzinfo=SINGAPORE_TZ),
        "state": 115.0,
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
        result = await async_backfill_statistics(hass, _entry_with_ledger(store))
    assert result.importable_count > 0
    assert imported
    assert all(row["sum"] >= 0 for row in imported[0])


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


async def test_electricity_seed_boundary_hour_generates_continuous_chain(
    hass: HomeAssistant,
) -> None:
    """The hour containing seed_effective_from must bridge pre-seed and post-seed.

    When the seed falls on a half-hour boundary (e.g., 12:30) and both
    30-minute buckets in the containing hour exist (12:00-12:30 pre-seed
    AND 12:30-13:00 post-seed), the statistics row for that hour must be
    generated.  Without it the cumulative state/sum chain has a permanent
    gap that makes the Energy dashboard skip the hour entirely.

    This test asserts that a ProjectedStatistic row IS generated for the
    seed-boundary hour when both halves have contiguous buckets.  It will
    fail on the current code because two unconditional guards skip the
    hour: _electricity_statistics hour_start < seed < hour_end → continue,
    and _has_seed_bridge row_start < seed < row_end → return False.
    """
    hass.config.components.add(RECORDER_DOMAIN)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        ELECTRIC_KEY,
        suggested_object_id="sp_group_electricity",
    )

    # Seed at 12:30 (half-hour boundary).  Buckets span 11:00-14:00.
    seed = datetime(2026, 7, 14, 12, 30, tzinfo=SINGAPORE_TZ)
    accepted = (
        # Pre-seed hour (11-12)
        _accepted_bucket(
            "2026-07-14T11:00:00+08:00",
            "2026-07-14T11:30:00+08:00",
            "1",
        ),
        _accepted_bucket(
            "2026-07-14T11:30:00+08:00",
            "2026-07-14T12:00:00+08:00",
            "1",
        ),
        # Seed-boundary hour (12-13): 12:00-12:30 is pre-seed,
        # 12:30-13:00 is post-seed.  Both MUST be present for the
        # bridge to work; the current code skips this hour unconditionally.
        _accepted_bucket(
            "2026-07-14T12:00:00+08:00",
            "2026-07-14T12:30:00+08:00",
            "2",
        ),
        _accepted_bucket(
            "2026-07-14T12:30:00+08:00",
            "2026-07-14T13:00:00+08:00",
            "3",
        ),
        # Post-seed hour (13-14)
        _accepted_bucket(
            "2026-07-14T13:00:00+08:00",
            "2026-07-14T13:30:00+08:00",
            "1",
        ),
        _accepted_bucket(
            "2026-07-14T13:30:00+08:00",
            "2026-07-14T14:00:00+08:00",
            "1",
        ),
    )
    post_seed_sum = sum(bucket.value for bucket in accepted if bucket.start >= seed)
    store = _FakeLedgerStore(
        SpGroupLedgerEntry(
            sensor_key=ELECTRIC_KEY,
            source="manual_seed_plus_ami_deltas",
            utility_type="electric",
            epoch_id="epoch-boundary-gap",
            seed_value=Decimal("1000"),
            seed_effective_from=seed,
            backfill_from=accepted[0].start,
            latest_total=Decimal("1000") + post_seed_sum,
            latest_accepted_at=accepted[-1].end,
            accepted=accepted,
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()
    native_reference = {
        "start": datetime(2026, 7, 14, 14, tzinfo=SINGAPORE_TZ),
        "state": float(Decimal("1000") + post_seed_sum),
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

    # The 12:00 row must be present — this is the seed-boundary hour.
    # Without it there is a gap in the state/sum chain.
    assert imported, "no statistics were imported"

    planned_rows = imported[0]
    row_starts = [row["start"] for row in planned_rows]

    expected_starts = [
        datetime(2026, 7, 14, 10, tzinfo=SINGAPORE_TZ),  # anchor
        datetime(2026, 7, 14, 11, tzinfo=SINGAPORE_TZ),  # 11-12 pre-seed
        datetime(2026, 7, 14, 12, tzinfo=SINGAPORE_TZ),  # 12-13 seed-boundary ← THE FIX
        datetime(2026, 7, 14, 13, tzinfo=SINGAPORE_TZ),  # 13-14 post-seed
    ]
    assert row_starts == expected_starts, (
        f"Expected row starts {expected_starts}, got {row_starts}. "
        "The 12:00 seed-boundary hour is missing — the current code "
        "unconditionally skips hours containing seed_effective_from."
    )

    # Verify monotonicity: both state and sum must increase continuously
    # 10:00 anchor: state=996, sum=0
    # 11:00 pre:    state=998, sum=2   ← delta=2 (11-12 consumption)
    # 12:00 bridge: state=1003, sum=7  ← delta=5 (12-13: 2 pre + 3 post)
    # 13:00 post:   state=1005, sum=9  ← delta=2 (13-14 consumption)
    expected_sum = Decimal("0")
    for _i, (start, row) in enumerate(zip(row_starts, planned_rows, strict=True)):
        assert row["start"] == start
        row_sum = Decimal(str(row["sum"]))
        assert row_sum >= expected_sum, (
            f"Row at {start}: sum {row_sum} < previous {expected_sum} — "
            "chain is not monotonic"
        )
        expected_sum = row_sum

    # Verify the adjustment was applied
    assert recorder.adjustments == [
        (
            "sensor.sp_group_electricity",
            datetime(2026, 7, 14, 14, tzinfo=SINGAPORE_TZ),
            9.0,
            "kWh",
        )
    ], recorder.adjustments


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
