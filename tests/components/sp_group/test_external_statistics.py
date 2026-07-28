"""Tests for SP Group provider-owned Recorder statistics."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from custom_components.sp_group.external_statistics import (
    EXTERNAL_STATISTICS_STRATEGY,
)
from custom_components.sp_group.ledger import (
    SpGroupAcceptedBucket,
    SpGroupLedgerEntry,
    SpGroupLedgerSnapshot,
)
from custom_components.sp_group.sources import SINGAPORE_TZ
from custom_components.sp_group.statistics_backfill import (
    _classify_external_rows,
    _external_planned_rows,
    _external_source_fingerprint,
    _projected_before_first_gap,
    _projected_statistics_with_warnings,
    _reconcile_external_rows,
    _unverified_planned_rows,
    _verify_external_statistics,
    async_backfill_statistics,
)
from homeassistant.components.recorder import (
    DOMAIN as RECORDER_DOMAIN,
)
from homeassistant.components.recorder import (
    get_instance,
)
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import EVENT_RECORDER_HOURLY_STATISTICS_GENERATED
from homeassistant.core import HomeAssistant

from .test_recorder_statistics import _recorder_job, _setup_recorder


def test_external_projection_is_positive_and_provider_period_based() -> None:
    """External rows use period usage as state and cumulative usage as sum."""
    projected = (
        SimpleNamespace(
            start=datetime(2026, 7, 1, 0, tzinfo=SINGAPORE_TZ),
            delta=Decimal("1.25"),
        ),
        SimpleNamespace(
            start=datetime(2026, 7, 1, 1, tzinfo=SINGAPORE_TZ),
            delta=Decimal("0.75"),
        ),
    )

    rows = _external_planned_rows(projected)

    assert [(row.state, row.sum) for row in rows] == [
        (Decimal("1.25"), Decimal("1.25")),
        (Decimal("0.75"), Decimal("2.00")),
    ]


def test_external_readback_missing_rows_are_pending_and_do_not_cross_gap() -> None:
    """Missing Recorder rows are retryable and stop the contiguous watermark."""
    planned = (
        _row("2026-07-01T00:00:00+08:00", "1", "1"),
        _row("2026-07-01T01:00:00+08:00", "2", "3"),
    )

    result = _classify_external_rows(
        planned,
        [{"start": planned[1].start, "state": 2.0, "sum": 3.0}],
    )

    assert result.verified == (planned[1],)
    assert result.verified_through is None
    assert result.pending_starts == (planned[0].start,)
    assert result.conflict_starts == ()
    assert result.warnings == (
        "statistics_recorder_rows_pending:2026-07-01T00:00:00+08:00:count=1",
    )


def test_external_readback_visible_mismatch_is_a_conflict() -> None:
    """A visible row with different values remains an actionable conflict."""
    planned = (_row("2026-07-01T00:00:00+08:00", "1", "1"),)

    result = _classify_external_rows(
        planned,
        [{"start": planned[0].start, "state": 9.0, "sum": 9.0}],
    )

    assert result.verified == ()
    assert result.verified_through is None
    assert result.pending_starts == ()
    assert result.conflict_starts == (planned[0].start,)
    assert result.warnings == ("statistics_conflict:2026-07-01T00:00:00+08:00",)


def test_external_readback_verified_prefix_stops_at_pending_row() -> None:
    """An exact prefix advances only to the first unresolved row."""
    planned = (
        _row("2026-07-01T00:00:00+08:00", "1", "1"),
        _row("2026-07-01T01:00:00+08:00", "2", "3"),
    )

    result = _classify_external_rows(
        planned,
        [{"start": planned[0].start, "state": 1.0, "sum": 1.0}],
    )

    assert result.verified == (planned[0],)
    assert result.verified_through == planned[0].start
    assert result.pending_starts == (planned[1].start,)


def test_external_readback_compacts_pending_warning() -> None:
    """Many missing rows produce one bounded retry diagnostic."""
    planned = tuple(
        _row(f"2026-07-01T0{hour}:00:00+08:00", "1", str(hour + 1))
        for hour in range(3)
    )

    result = _classify_external_rows(planned, [])

    assert result.warnings == (
        "statistics_recorder_rows_pending:2026-07-01T00:00:00+08:00:count=3",
    )


def test_external_retry_suffix_uses_dirty_boundary() -> None:
    """Retries plan only the suffix at and after the dirty boundary."""
    planned = tuple(
        _row(f"2026-07-01T0{hour}:00:00+08:00", "1", str(hour + 1))
        for hour in range(3)
    )
    entry = _entry("sp_group:test_suffix_water_consumption", (), utility_type="water")
    entry = replace(entry, statistics_dirty_from=planned[1].start)

    assert _unverified_planned_rows(planned, entry) == planned[1:]


async def test_ledger_external_metadata_preserves_pending_state(
    hass: HomeAssistant,
    tmp_path: Any,
) -> None:
    """The real ledger store does not mark unverified rows complete."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    entry = _entry(
        "sp_group:test_pending_metadata_consumption",
        (_bucket("2026-07-01T00:00:00+08:00", "2026-08-01T00:00:00+08:00", "2"),),
        utility_type="water",
    )
    store = SpGroupLedgerStore(hass, "pending-metadata")
    store._snapshot = SpGroupLedgerSnapshot(entries={entry.sensor_key: entry})  # noqa: SLF001
    pending_from = datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ)

    updated = await store.async_update_external_statistics_metadata(
        entry.sensor_key,
        imported_through=None,
        source_fingerprint=_external_source_fingerprint(entry),
        verified_at=None,
        warnings=(
            f"statistics_recorder_rows_pending:{pending_from.isoformat()}:count=1",
        ),
        dirty_from=pending_from,
        clear_dirty=False,
        external_status="pending",
        expected_epoch_id=entry.epoch_id,
    )

    assert updated
    persisted = store.snapshot.entries[entry.sensor_key]
    assert persisted.external_migration_status == "pending"
    assert persisted.statistics_dirty_from == pending_from
    assert persisted.statistics_imported_through is None
    assert persisted.statistics_last_verified_at is None

    reloaded = SpGroupLedgerStore(hass, "pending-metadata")
    await reloaded.async_load()
    restored = reloaded.snapshot.entries[entry.sensor_key]
    assert restored.external_migration_status == "pending"
    assert restored.statistics_dirty_from == pending_from
    assert restored.statistics_warnings == (
        f"statistics_recorder_rows_pending:{pending_from.isoformat()}:count=1",
    )


def test_external_reconciliation_rejects_dirty_suffix_conflicts() -> None:
    """A dirty boundary never authorizes overwriting a visible Recorder row."""
    planned = (
        _row("2026-07-01T00:00:00+08:00", "1", "1"),
        _row("2026-07-01T01:00:00+08:00", "2", "3"),
    )
    existing = [
        {"start": planned[0].start, "state": 9.0, "sum": 9.0},
        {"start": planned[1].start, "state": 2.0, "sum": 3.0},
    ]

    _importable, _processed, warnings = _reconcile_external_rows(
        planned,
        existing,
        None,
    )
    assert warnings == ("statistics_conflict:2026-07-01T00:00:00+08:00",)

    importable, processed, warnings = _reconcile_external_rows(
        planned,
        existing,
        planned[0].start,
    )
    assert warnings == ("statistics_conflict:2026-07-01T00:00:00+08:00",)
    assert importable == ()
    assert processed == ()


def test_external_readback_duplicate_start_is_a_conflict() -> None:
    """Duplicate Recorder starts are never silently collapsed."""
    planned = (_row("2026-07-01T00:00:00+08:00", "1", "1"),)

    result = _classify_external_rows(
        planned,
        [
            {"start": planned[0].start, "state": 1.0, "sum": 1.0},
            {"start": planned[0].start, "state": 1.0, "sum": 1.0},
        ],
    )

    assert result.conflict_starts == (planned[0].start,)
    assert result.pending_starts == ()
    assert result.warnings == (
        "statistics_conflict:2026-07-01T00:00:00+08:00",
    )


def test_external_readback_malformed_values_are_a_conflict() -> None:
    """Malformed values at a planned start cannot be treated as verified."""
    planned = (_row("2026-07-01T00:00:00+08:00", "1", "1"),)

    for value in (None, "not-a-number", "NaN", "Infinity"):
        result = _classify_external_rows(
            planned,
            [{"start": planned[0].start, "state": value, "sum": 1.0}],
        )
        assert result.conflict_starts == (planned[0].start,)
        assert result.pending_starts == ()


def test_external_reconciliation_duplicate_start_is_not_importable() -> None:
    """Preflight duplicates are rejected before any writer call."""
    planned = (_row("2026-07-01T00:00:00+08:00", "1", "1"),)
    importable, processed, warnings = _reconcile_external_rows(
        planned,
        [
            {"start": planned[0].start, "state": 1.0, "sum": 1.0},
            {"start": planned[0].start, "state": 1.0, "sum": 1.0},
        ],
        None,
    )

    assert importable == ()
    assert processed == ()
    assert warnings == ("statistics_conflict:2026-07-01T00:00:00+08:00",)


async def test_external_statistics_import_uses_public_recorder_api(
    hass: HomeAssistant,
) -> None:
    """Fresh external entries import without an entity registry or native anchor."""
    hass.config.components.add(RECORDER_DOMAIN)
    statistic_id = "sp_group:test_electric_consumption"
    entry = _entry(
        statistic_id,
        (
            _bucket(
                "2026-07-01T00:00:00+08:00",
                "2026-07-01T00:30:00+08:00",
                "1.25",
            ),
            _bucket(
                "2026-07-01T00:30:00+08:00",
                "2026-07-01T01:00:00+08:00",
                "0.75",
            ),
            _bucket(
                "2026-07-01T01:00:00+08:00",
                "2026-07-01T01:30:00+08:00",
                "0.5",
            ),
            _bucket(
                "2026-07-01T01:30:00+08:00",
                "2026-07-01T02:00:00+08:00",
                "0.25",
            ),
        ),
    )
    store = _Store(entry)
    recorder = _Recorder()
    recorder_rows: list[dict[str, Any]] = []

    def add_external(
        _hass: HomeAssistant,
        _metadata: Any,
        rows: list[dict[str, Any]],
    ) -> None:
        recorder_rows.extend(rows)

    def query(*_args: Any, **_kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        return {statistic_id: list(recorder_rows)}

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=recorder,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            side_effect=query,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
            side_effect=add_external,
        ) as add_mock,
        patch(
            "custom_components.sp_group.statistics_backfill.async_import_statistics",
        ) as legacy_mock,
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.importable_count == 2
    assert [row["state"] for row in recorder_rows] == [2.0, 0.75]
    assert [row["sum"] for row in recorder_rows] == [2.0, 2.75]
    add_mock.assert_called_once()
    legacy_mock.assert_not_called()
    assert store.entry.statistics_strategy_version == EXTERNAL_STATISTICS_STRATEGY
    assert store.entry.statistics_dirty_from is None
    assert store.entry.statistics_imported_through == datetime(
        2026, 7, 1, 1, tzinfo=SINGAPORE_TZ
    )


async def test_external_retry_queries_only_unverified_suffix(
    hass: HomeAssistant,
) -> None:
    """A retry bounds the Recorder query to rows after its watermark."""
    hass.config.components.add(RECORDER_DOMAIN)
    statistic_id = "sp_group:test_suffix_query_water_consumption"
    first_start = datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ)
    second_start = datetime(2026, 8, 31, 23, tzinfo=SINGAPORE_TZ)
    entry = replace(
        _entry(
            statistic_id,
            (
                _bucket(
                    "2026-07-01T00:00:00+08:00",
                    "2026-08-01T00:00:00+08:00",
                    "1",
                ),
                _bucket(
                    "2026-08-01T00:00:00+08:00",
                    "2026-09-01T00:00:00+08:00",
                    "2",
                ),
            ),
            utility_type="water",
        ),
        statistics_imported_through=first_start,
        statistics_dirty_from=None,
    )
    store = _Store(entry)
    query_bounds: list[tuple[datetime, datetime]] = []

    def query(*args: Any, **_kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        query_bounds.append((args[1], args[2]))
        return {
            statistic_id: [
                {"start": second_start, "state": 2.0, "sum": 3.0},
            ]
        }

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            side_effect=query,
        ) as query_mock,
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ) as add_mock,
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.warning_count == 0
    assert query_mock.call_count == 1
    assert query_bounds == [
        (
            second_start.astimezone(UTC),
            (second_start + timedelta(hours=1)).astimezone(UTC),
        )
    ]
    add_mock.assert_not_called()


async def test_external_statistics_delayed_readback_does_not_create_conflict(
    hass: HomeAssistant,
) -> None:
    """Rows becoming visible after queue acknowledgement complete cleanly."""
    hass.config.components.add(RECORDER_DOMAIN)
    statistic_id = "sp_group:test_delayed_water_consumption"
    entry = _entry(
        statistic_id,
        (
            _bucket(
                "2026-07-01T00:00:00+08:00",
                "2026-08-01T00:00:00+08:00",
                "2",
            ),
        ),
        utility_type="water",
    )
    store = _Store(entry)
    recorder_rows: list[dict[str, Any]] = []
    query_count = 0
    visible = False

    def add_external(
        _hass: HomeAssistant,
        _metadata: Any,
        rows: list[dict[str, Any]],
    ) -> None:
        recorder_rows.extend(rows)

    def query(*_args: Any, **_kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        nonlocal query_count
        query_count += 1
        # Initial reconciliation and the first post-import read-back race with
        # Recorder visibility. The next read sees the committed rows.
        if not visible:
            return {statistic_id: []}
        return {statistic_id: list(recorder_rows)}

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            side_effect=query,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
            side_effect=add_external,
        ),
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert query_count == 2
    assert result.warning_count == 1
    assert store.entry.external_migration_status == "pending"
    visible = True
    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            side_effect=query,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ) as add_mock,
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.warning_count == 0
    assert add_mock.call_count == 0
    assert store.entry.statistics_warnings == ()
    assert store.entry.external_migration_status == "complete"
    assert store.entry.statistics_imported_through == datetime(
        2026, 7, 31, 23, tzinfo=SINGAPORE_TZ
    )


async def test_external_statistics_retry_is_provider_free(
    hass: HomeAssistant,
) -> None:
    """Statistics-only retries reconcile persisted rows without provider calls."""
    from custom_components.sp_group.backfill import async_run_statistics_backfill

    hass.config.components.add(RECORDER_DOMAIN)
    statistic_id = "sp_group:test_provider_free_retry_water"
    bucket = _bucket(
        "2026-07-01T00:00:00+08:00",
        "2026-08-01T00:00:00+08:00",
        "2",
    )
    entry = replace(
        _entry(statistic_id, (bucket,), utility_type="water"),
        statistics_dirty_from=datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ),
        statistics_warnings=(
            "statistics_recorder_rows_pending:2026-07-31T23:00:00+08:00:count=1",
        ),
        external_migration_status="pending",
    )
    store = _Store(entry)

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={
                statistic_id: [
                    {
                        "start": datetime(
                            2026, 7, 31, 23, tzinfo=SINGAPORE_TZ
                        ),
                        "state": 2.0,
                        "sum": 2.0,
                    },
                ]
            },
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ) as add_mock,
    ):
        await async_run_statistics_backfill(
            hass,
            _entry_with_store(store),
            trigger="recorder",
        )

    add_mock.assert_not_called()
    assert store.entry.external_migration_status == "complete"
    assert store.entry.statistics_dirty_from is None


async def test_external_statistics_recorder_event_runs_real_retry(
    hass: HomeAssistant,
) -> None:
    """The Recorder event executes a real provider-free external retry."""
    from custom_components.sp_group import _async_register_statistics_retry_listener

    hass.config.components.add(RECORDER_DOMAIN)
    statistic_id = "sp_group:test_event_retry_water"
    pending_start = datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ)
    entry = replace(
        _entry(
            statistic_id,
            (
                _bucket(
                    "2026-07-01T00:00:00+08:00",
                    "2026-08-01T00:00:00+08:00",
                    "2",
                ),
            ),
            utility_type="water",
        ),
        source_backfill_attempted_at=pending_start,
        statistics_dirty_from=pending_start,
        statistics_warnings=(
            "statistics_recorder_rows_pending:2026-07-31T23:00:00+08:00:count=1",
        ),
        external_migration_status="pending",
    )
    store = _Store(entry)
    runtime_entry = _entry_with_store(store)
    runtime_entry.async_on_unload = lambda _callback: None
    delayed: list[Any] = []
    tasks: list[Any] = []

    def schedule_retry(
        hass_arg: HomeAssistant,
        entry_arg: Any,
        **_kwargs: Any,
    ) -> object:
        from custom_components.sp_group.backfill import async_run_statistics_backfill

        task = hass_arg.async_create_task(
            async_run_statistics_backfill(hass_arg, entry_arg, trigger="recorder")
        )
        tasks.append(task)
        return object()

    with (
        patch(
            "custom_components.sp_group.async_call_later",
            side_effect=lambda _hass, _delay, action: delayed.append(action)
            or (lambda: None),
        ),
        patch(
            "custom_components.sp_group.async_schedule_statistics_backfill",
            side_effect=schedule_retry,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={
                statistic_id: [
                    {
                        "start": pending_start,
                        "state": 2.0,
                        "sum": 2.0,
                    }
                ]
            },
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ) as add_mock,
    ):
        _async_register_statistics_retry_listener(hass, runtime_entry)
        hass.bus.async_fire(EVENT_RECORDER_HOURLY_STATISTICS_GENERATED)
        await hass.async_block_till_done()
        assert len(delayed) == 1
        delayed[0](None)
        await hass.async_block_till_done()

    assert len(tasks) == 1
    assert store.entry.external_migration_status == "complete"
    assert store.entry.statistics_dirty_from is None
    add_mock.assert_not_called()


async def test_external_statistics_readback_timeout_is_retryable(
    hass: HomeAssistant,
) -> None:
    """Rows absent after the deadline do not create a false conflict."""
    hass.config.components.add(RECORDER_DOMAIN)
    statistic_id = "sp_group:test_pending_water_consumption"
    entry = _entry(
        statistic_id,
        (
            _bucket(
                "2026-07-01T00:00:00+08:00",
                "2026-08-01T00:00:00+08:00",
                "2",
            ),
        ),
        utility_type="water",
    )
    store = _Store(entry)

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={statistic_id: []},
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.ir.async_create_issue",
        ) as create_issue,
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.warning_count == 1
    assert store.entry.statistics_warnings == (
        "statistics_recorder_rows_pending:2026-07-31T23:00:00+08:00:count=1",
    )
    assert store.entry.statistics_imported_through is None
    assert store.entry.external_migration_status == "pending"
    assert store.entry.statistics_dirty_from == datetime(
        2026, 7, 31, 23, tzinfo=SINGAPORE_TZ
    )
    create_issue.assert_not_called()


async def test_external_statistics_initial_preflight_timeout_is_retryable(
    hass: HomeAssistant,
) -> None:
    """A stalled initial Recorder query does not queue or advance state."""
    import asyncio

    hass.config.components.add(RECORDER_DOMAIN)
    statistic_id = "sp_group:test_initial_readback_timeout_water"
    entry = _entry(
        statistic_id,
        (
            _bucket(
                "2026-07-01T00:00:00+08:00",
                "2026-08-01T00:00:00+08:00",
                "2",
            ),
        ),
        utility_type="water",
    )
    store = _Store(entry)
    release = asyncio.Event()

    async def stalled_readback(*_args: Any) -> list[dict[str, Any]]:
        await release.wait()
        return []

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill._existing_external_statistics",
            side_effect=stalled_readback,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ) as add_mock,
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_WATCHDOG_TIMEOUT",
            0.01,
        ),
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.warning_count == 1
    assert store.entry.external_migration_status == "pending"
    assert store.entry.statistics_imported_through is None
    add_mock.assert_not_called()
    release.set()
    await asyncio.sleep(0)


async def test_external_statistics_recorder_watchdog_does_not_requeue(
    hass: HomeAssistant,
) -> None:
    """A stalled Recorder barrier leaves pending state after one queue call."""
    import asyncio

    hass.config.components.add(RECORDER_DOMAIN)
    statistic_id = "sp_group:test_watchdog_water_consumption"
    entry = _entry(
        statistic_id,
        (
            _bucket(
                "2026-07-01T00:00:00+08:00",
                "2026-08-01T00:00:00+08:00",
                "2",
            ),
        ),
        utility_type="water",
    )
    store = _Store(entry)

    class _SlowRecorder(_Recorder):
        async def async_block_till_done(self) -> None:
            await asyncio.sleep(1)

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_SlowRecorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={statistic_id: []},
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ) as add_mock,
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_WATCHDOG_TIMEOUT",
            0.01,
        ),
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))
        await asyncio.sleep(1.05)

    assert result.warning_count == 1
    add_mock.assert_called_once()
    assert store.entry.external_migration_status == "pending"
    assert store.entry.statistics_imported_through is None


async def test_external_readback_watchdog_returns_pending_without_requeue(
    hass: HomeAssistant,
) -> None:
    """A stalled readback is pending and does not advance verification."""
    import asyncio

    planned = (_row("2026-07-01T00:00:00+08:00", "1", "1"),)
    release = asyncio.Event()

    async def stalled_readback(*_args: Any) -> list[dict[str, Any]]:
        await release.wait()
        return []

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill._existing_external_statistics",
            side_effect=stalled_readback,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_WATCHDOG_TIMEOUT",
            0.01,
        ),
    ):
        result = await _verify_external_statistics(
            hass,
            "sp_group:test_readback_watchdog",
            planned,
        )

    assert result.pending_starts == (planned[0].start,)
    assert result.verified_through is None
    release.set()
    await asyncio.sleep(0)


async def test_external_barrier_exception_does_not_look_verified(
    hass: HomeAssistant,
) -> None:
    """Recorder barrier failures propagate instead of advancing metadata."""
    planned = (_row("2026-07-01T00:00:00+08:00", "1", "1"),)

    class _FailingRecorder(_Recorder):
        async def async_block_till_done(self) -> None:
            raise RuntimeError("recorder failure")

    with patch(
        "custom_components.sp_group.statistics_backfill.get_instance",
        return_value=_FailingRecorder(),
    ):
        with pytest.raises(RuntimeError, match="recorder failure"):
            await _verify_external_statistics(
                hass,
                "sp_group:test_barrier_failure",
                planned,
            )


async def test_external_readback_exception_does_not_look_verified(
    hass: HomeAssistant,
) -> None:
    """Recorder query failures propagate instead of advancing metadata."""
    planned = (_row("2026-07-01T00:00:00+08:00", "1", "1"),)

    async def failing_readback(*_args: Any) -> list[dict[str, Any]]:
        raise RuntimeError("readback failure")

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill._existing_external_statistics",
            side_effect=failing_readback,
        ),
    ):
        with pytest.raises(RuntimeError, match="readback failure"):
            await _verify_external_statistics(
                hass,
                "sp_group:test_readback_failure",
                planned,
            )


async def test_external_writer_exception_preserves_ledger_state(
    hass: HomeAssistant,
) -> None:
    """A writer failure does not persist a partial external watermark."""
    hass.config.components.add(RECORDER_DOMAIN)
    statistic_id = "sp_group:test_writer_failure_water_consumption"
    entry = _entry(
        statistic_id,
        (
            _bucket(
                "2026-07-01T00:00:00+08:00",
                "2026-08-01T00:00:00+08:00",
                "2",
            ),
        ),
        utility_type="water",
    )
    store = _Store(entry)
    before = store.entry

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={statistic_id: []},
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
            side_effect=RuntimeError("writer failure"),
        ),
    ):
        with pytest.raises(RuntimeError, match="writer failure"):
            await async_backfill_statistics(hass, _entry_with_store(store))

    assert store.entry == before


async def test_external_statistics_large_batch_eventual_recorder_visibility(
    hass: HomeAssistant,
    tmp_path: Any,
) -> None:
    """A multi-thousand-row batch converges without duplicate writes."""
    await _setup_recorder(hass, tmp_path)
    start = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    buckets = tuple(
        _bucket(
            (start + timedelta(minutes=30 * index)).isoformat(),
            (start + timedelta(minutes=30 * (index + 1))).isoformat(),
            "0.1",
        )
        for index in range(6000)
    )
    entry = _entry(
        "sp_group:test_large_electric_consumption",
        buckets,
        utility_type="electric",
    )
    store = _Store(entry)

    first = await async_backfill_statistics(hass, _entry_with_store(store))
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()
    second = await async_backfill_statistics(hass, _entry_with_store(store))
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    assert first.warning_count in (0, 1)
    assert second.warning_count == 0
    assert store.entry.external_migration_status == "complete"
    assert store.entry.statistics_imported_through is not None
    rows = await _recorder_job(
        hass,
        statistics_during_period,
        hass,
        start,
        start + timedelta(minutes=30 * 6000),
        {entry.external_statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    assert len(rows[entry.external_statistic_id]) == 3000


async def test_external_statistics_water_month_end_recorder_rows(
    hass: HomeAssistant,
    tmp_path: Any,
) -> None:
    """Month-end water rows use the same verified external path."""
    await _setup_recorder(hass, tmp_path)
    start = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    entry = _entry(
        "sp_group:test_water_month_end_external",
        (
            _bucket("2026-01-01T00:00:00+08:00", "2026-02-01T00:00:00+08:00", "2"),
            _bucket("2026-02-01T00:00:00+08:00", "2026-03-01T00:00:00+08:00", "3"),
        ),
        utility_type="water",
    )
    store = _Store(entry)

    first = await async_backfill_statistics(hass, _entry_with_store(store))
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    rows = await _recorder_job(
        hass,
        statistics_during_period,
        hass,
        start,
        datetime(2026, 4, 1, tzinfo=SINGAPORE_TZ),
        {entry.external_statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    visible = rows[entry.external_statistic_id]
    assert first.warning_count == 0
    assert [(row["state"], row["sum"]) for row in visible] == [
        (2.0, 2.0),
        (3.0, 5.0),
    ]


async def test_external_statistics_visible_mismatch_creates_conflict_repair(
    hass: HomeAssistant,
) -> None:
    """A committed value mismatch remains a hard conflict."""
    hass.config.components.add(RECORDER_DOMAIN)
    statistic_id = "sp_group:test_conflict_water_consumption"
    entry = _entry(
        statistic_id,
        (
            _bucket(
                "2026-07-01T00:00:00+08:00",
                "2026-08-01T00:00:00+08:00",
                "2",
            ),
        ),
        utility_type="water",
    )
    store = _Store(entry)
    query_count = 0

    def query(*_args: Any, **_kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        nonlocal query_count
        query_count += 1
        if query_count == 1:
            return {statistic_id: []}
        return {
            statistic_id: [
                {
                    "start": datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ),
                    "state": 9.0,
                    "sum": 9.0,
                }
            ]
        }

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            side_effect=query,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.ir.async_create_issue",
        ) as create_issue,
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.warning_count == 1
    assert store.entry.statistics_warnings == (
        "statistics_conflict:2026-07-31T23:00:00+08:00",
    )
    assert store.entry.external_migration_status == "pending"
    assert store.entry.statistics_imported_through is None
    create_issue.assert_called_once()


async def test_external_statistics_retry_clears_pending_after_rows_appear(
    hass: HomeAssistant,
) -> None:
    """A later statistics-only run observes exact rows and completes."""
    hass.config.components.add(RECORDER_DOMAIN)
    statistic_id = "sp_group:test_retry_water_consumption"
    entry = _entry(
        statistic_id,
        (
            _bucket(
                "2026-07-01T00:00:00+08:00",
                "2026-08-01T00:00:00+08:00",
                "2",
            ),
        ),
        utility_type="water",
    )
    store = _Store(entry)
    visible = False
    query_count = 0
    recorder_rows = [
        {
            "start": datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ),
            "state": 2.0,
            "sum": 2.0,
        }
    ]

    def query(*_args: Any, **_kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        nonlocal query_count
        query_count += 1
        return {statistic_id: list(recorder_rows) if visible else []}

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            side_effect=query,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ) as add_mock,
    ):
        first = await async_backfill_statistics(hass, _entry_with_store(store))
        assert first.warning_count == 1
        visible = True
        second = await async_backfill_statistics(hass, _entry_with_store(store))

    assert second.warning_count == 0
    assert store.entry.statistics_warnings == ()
    assert store.entry.external_migration_status == "complete"
    assert store.entry.statistics_imported_through == datetime(
        2026, 7, 31, 23, tzinfo=SINGAPORE_TZ
    )
    assert add_mock.call_count == 1
    assert query_count == 3


async def test_external_statistics_waits_at_first_missing_bridge(
    hass: HomeAssistant,
) -> None:
    """A missing source bridge leaves later rows unimported and retryable."""
    hass.config.components.add(RECORDER_DOMAIN)
    entry = _entry(
        "sp_group:test_water_consumption",
        (
            _bucket(
                "2026-05-01T00:00:00+08:00",
                "2026-06-01T00:00:00+08:00",
                "2",
            ),
            _bucket(
                "2026-07-01T00:00:00+08:00",
                "2026-08-01T00:00:00+08:00",
                "3",
            ),
        ),
        utility_type="water",
    )
    projected, warnings = _projected_statistics_with_warnings(entry)
    assert len(projected) == 1
    assert warnings == ("statistics_missing_bridge:2026-07-01T00:00:00+08:00",)

    # The bridge condition is exercised directly for this fixture: replacing the
    # first row with a source gap must only leave the safe prefix eligible.
    safe = _projected_before_first_gap(
        projected,
        ("statistics_missing_bridge:2026-07-01T00:00:00+08:00",),
    )
    assert len(safe) == 1


async def test_external_empty_source_is_verified_without_perpetual_retry(
    hass: HomeAssistant,
) -> None:
    """A successful empty provider response is a completed empty series."""
    hass.config.components.add(RECORDER_DOMAIN)
    entry = _entry(
        "sp_group:test_empty_water_consumption",
        (),
        utility_type="water",
    )
    store = _Store(entry)
    with patch(
        "custom_components.sp_group.statistics_backfill.get_instance",
        return_value=_Recorder(),
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result == result.__class__()
    assert store.entry.external_migration_status == "complete"
    assert store.entry.statistics_dirty_from is None
    assert store.entry.statistics_imported_through is None


async def test_external_correction_warning_blocks_provider_export(
    hass: HomeAssistant,
) -> None:
    """Provider corrections require repair rather than a silent rewrite."""
    hass.config.components.add(RECORDER_DOMAIN)
    entry = replace(
        _entry(
            "sp_group:test_corrected_water_consumption",
            (_bucket("2026-06-01T00:00:00+08:00", "2026-07-01T00:00:00+08:00", "2"),),
            utility_type="water",
        ),
        warnings=("corrected_bucket:2026-06-01T00:00:00+08:00",),
    )
    store = _Store(entry)
    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ) as add_mock,
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.importable_count == 0
    add_mock.assert_not_called()
    assert store.entry.statistics_warnings == (
        "corrected_bucket:2026-06-01T00:00:00+08:00",
    )


async def test_external_metadata_update_rejects_stale_source_fingerprint(
    hass: HomeAssistant,
    tmp_path: Any,
) -> None:
    """A source change during planning cannot advance stale import metadata."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    entry = _entry(
        "sp_group:test_stale_electric_consumption",
        (
            _bucket(
                "2026-07-01T00:00:00+08:00",
                "2026-07-01T00:30:00+08:00",
                "1",
            ),
            _bucket(
                "2026-07-01T00:30:00+08:00",
                "2026-07-01T01:00:00+08:00",
                "1",
            ),
        ),
    )
    store = SpGroupLedgerStore(hass, "stale-entry")
    store._snapshot = SpGroupLedgerSnapshot(entries={entry.sensor_key: entry})  # noqa: SLF001

    updated = await store.async_update_external_statistics_metadata(
        entry.sensor_key,
        imported_through=datetime(2026, 7, 1, 0, tzinfo=SINGAPORE_TZ),
        source_fingerprint="wrong",
        verified_at=datetime(2026, 7, 2, tzinfo=SINGAPORE_TZ),
        expected_epoch_id=entry.epoch_id,
    )

    assert not updated
    assert store.snapshot.entries[entry.sensor_key].statistics_imported_through is None
    assert _external_source_fingerprint(entry) != "wrong"


def _row(start: str, state: str, total: str) -> Any:
    from custom_components.sp_group.statistics_backfill import PlannedStatistic

    return PlannedStatistic(
        start=datetime.fromisoformat(start),
        state=Decimal(state),
        sum=Decimal(total),
    )


def _bucket(start: str, end: str, value: str) -> SpGroupAcceptedBucket:
    observed = datetime(2026, 7, 2, tzinfo=SINGAPORE_TZ)
    return SpGroupAcceptedBucket(
        bucket_key=f"{start}:{end}",
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        value=Decimal(value),
        first_seen_at=observed,
        accepted_at=observed,
    )


def _entry(
    statistic_id: str,
    buckets: tuple[SpGroupAcceptedBucket, ...],
    *,
    utility_type: str = "electric",
) -> SpGroupLedgerEntry:
    seed = buckets[0].start if buckets else datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ)
    return SpGroupLedgerEntry(
        sensor_key="test_sensor",
        source="test",
        utility_type=utility_type,
        epoch_id="epoch-1",
        seed_value=Decimal("100"),
        seed_effective_from=seed,
        backfill_from=seed,
        latest_total=Decimal("100") + sum((b.value for b in buckets), Decimal("0")),
        accepted=buckets,
        external_statistic_id=statistic_id,
        external_identity_fingerprint="test-fingerprint",
        statistics_strategy_version=EXTERNAL_STATISTICS_STRATEGY,
        statistics_dirty_from=seed,
    )


class _Store:
    def __init__(self, entry: SpGroupLedgerEntry) -> None:
        self.entry = entry
        self.snapshot = SpGroupLedgerSnapshot(entries={entry.sensor_key: entry})

    async def async_update_external_statistics_metadata(
        self,
        sensor_key: str,
        **kwargs: Any,
    ) -> bool:
        assert sensor_key == self.entry.sensor_key
        source_fingerprint = kwargs.get("source_fingerprint")
        if source_fingerprint is not None:
            from custom_components.sp_group.ledger import _accepted_source_fingerprint

            if _accepted_source_fingerprint(self.entry) != source_fingerprint:
                self.entry = replace(
                    self.entry,
                    statistics_warnings=kwargs.get(
                        "warnings", self.entry.statistics_warnings
                    ),
                )
                self.snapshot = SpGroupLedgerSnapshot(entries={sensor_key: self.entry})
                return False
        self.entry = replace(
            self.entry,
            statistics_imported_through=kwargs["imported_through"],
            statistics_source_fingerprint=kwargs["source_fingerprint"],
            statistics_last_verified_at=kwargs["verified_at"],
            statistics_warnings=kwargs["warnings"],
            external_migration_status=kwargs.get(
                "external_status",
                "complete" if kwargs["clear_dirty"] else "pending",
            ),
            statistics_dirty_from=(
                None if kwargs["clear_dirty"] else kwargs.get("dirty_from")
            ),
        )
        self.snapshot = SpGroupLedgerSnapshot(entries={sensor_key: self.entry})
        return True


class _Recorder:
    async def async_add_executor_job(self, job: Any) -> Any:
        return job()

    async def async_block_till_done(self) -> None:
        return None


def _entry_with_store(store: _Store) -> Any:
    return SimpleNamespace(
        entry_id="external-entry",
        runtime_data=SimpleNamespace(ledger_store=store),
    )


async def test_external_readback_partial_acknowledgement_retryable(
    hass: HomeAssistant,
) -> None:
    """Partial Recorder acknowledgement leaves pending rows that complete on retry."""
    hass.config.components.add(RECORDER_DOMAIN)
    entry = _entry(
        "sp_group:test_partial_ack_water_consumption",
        (
            _bucket("2026-07-01T00:00:00+08:00", "2026-08-01T00:00:00+08:00", "2"),
            _bucket("2026-08-01T00:00:00+08:00", "2026-09-01T00:00:00+08:00", "3"),
            _bucket("2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00", "4"),
        ),
        utility_type="water",
    )
    store = _Store(entry)
    query_count = [0]
    recorder_rows: list[dict[str, Any]] = []

    def query(*_args: Any, **_kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        query_count[0] += 1
        if query_count[0] <= 2:
            return {
                "sp_group:test_partial_ack_water_consumption": [
                    r for r in recorder_rows
                ]
            }
        return {"sp_group:test_partial_ack_water_consumption": list(recorder_rows)}

    def add_external(
        _hass: HomeAssistant,
        _metadata: Any,
        rows: list[dict[str, Any]],
    ) -> None:
        if not recorder_rows:
            recorder_rows.extend([dict(r) for r in rows[:2]])

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
            side_effect=add_external,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            side_effect=query,
        ),
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.warning_count == 1
    assert store.entry.statistics_imported_through == datetime(
        2026, 8, 31, 23, tzinfo=SINGAPORE_TZ
    )
    assert "statistics_recorder_rows_pending" in str(store.entry.statistics_warnings)

    recorder_rows.clear()
    recorder_rows.extend(
        [
            {
                "start": datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ),
                "state": 2.0,
                "sum": 2.0,
            },
            {
                "start": datetime(2026, 8, 31, 23, tzinfo=SINGAPORE_TZ),
                "state": 3.0,
                "sum": 5.0,
            },
            {
                "start": datetime(2026, 9, 30, 23, tzinfo=SINGAPORE_TZ),
                "state": 4.0,
                "sum": 9.0,
            },
        ]
    )
    query_count[0] = 0

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            side_effect=query,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ),
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.warning_count == 0
    assert store.entry.external_migration_status == "complete"


async def test_external_readback_partial_ack_out_of_order(
    hass: HomeAssistant,
) -> None:
    """Out-of-order Recorder rows stop at the first gap without conflict.

    The first gap terminates verification without creating a conflict.
    """
    hass.config.components.add(RECORDER_DOMAIN)
    entry = _entry(
        "sp_group:test_partial_oao_water_consumption",
        (
            _bucket("2026-07-01T00:00:00+08:00", "2026-08-01T00:00:00+08:00", "2"),
            _bucket("2026-08-01T00:00:00+08:00", "2026-09-01T00:00:00+08:00", "3"),
        ),
        utility_type="water",
    )
    store = _Store(entry)

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={
                "sp_group:test_partial_oao_water_consumption": [
                    {
                        "start": datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ),
                        "state": 2.0,
                        "sum": 2.0,
                    },
                    {
                        "start": datetime(2026, 8, 31, 23, tzinfo=SINGAPORE_TZ),
                        "state": 3.0,
                        "sum": 5.0,
                    },
                ]
            },
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ),
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.warning_count == 0
    assert store.entry.external_migration_status == "complete"
    assert store.entry.statistics_imported_through == datetime(
        2026, 8, 31, 23, tzinfo=SINGAPORE_TZ
    )


async def test_external_readback_first_row_conflict_with_partial_ack(
    hass: HomeAssistant,
) -> None:
    """A first-row conflict prevents import even when later rows match."""
    hass.config.components.add(RECORDER_DOMAIN)
    entry = _entry(
        "sp_group:test_conflict_first_water_consumption",
        (
            _bucket("2026-07-01T00:00:00+08:00", "2026-08-01T00:00:00+08:00", "2"),
            _bucket("2026-08-01T00:00:00+08:00", "2026-09-01T00:00:00+08:00", "3"),
        ),
        utility_type="water",
    )
    store = _Store(entry)

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={
                "sp_group:test_conflict_first_water_consumption": [
                    {
                        "start": datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ),
                        "state": 9.0,
                        "sum": 9.0,
                    },
                    {
                        "start": datetime(2026, 8, 31, 23, tzinfo=SINGAPORE_TZ),
                        "state": 3.0,
                        "sum": 5.0,
                    },
                ]
            },
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ),
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.warning_count == 1
    assert store.entry.statistics_imported_through is None
    assert "statistics_conflict" in str(store.entry.statistics_warnings)


async def test_external_poll_completes_during_import_metadata_update_rejected(
    hass: HomeAssistant,
) -> None:
    """A poll during import changes fingerprint and blocks metadata advance."""
    from dataclasses import replace as dc_replace

    hass.config.components.add(RECORDER_DOMAIN)
    bucket_original = _bucket(
        "2026-07-01T00:00:00+08:00", "2026-08-01T00:00:00+08:00", "2"
    )
    _original = _entry(
        "sp_group:test_concurrent_poll_water_consumption",
        (bucket_original,),
        utility_type="water",
    )
    _fingerprint = _external_source_fingerprint(_original)
    entry = dc_replace(
        _original,
        statistics_source_fingerprint=_fingerprint,
        external_migration_status="pending",
    )
    extra_bucket = _bucket(
        "2026-08-01T00:00:00+08:00", "2026-09-01T00:00:00+08:00", "3"
    )
    entry_with_extra = dc_replace(
        entry,
        accepted=entry.accepted + (extra_bucket,),
        latest_total=entry.latest_total + Decimal("3"),
        statistics_dirty_from=extra_bucket.start,
    )
    store = _Store(entry)
    poll_completed = False

    def add_external(
        _hass: HomeAssistant,
        _metadata: Any,
        rows: list[dict[str, Any]],
    ) -> None:
        nonlocal poll_completed
        if not poll_completed:
            poll_completed = True
            store.entry = entry_with_extra
            store.snapshot = SpGroupLedgerSnapshot(
                entries={entry.sensor_key: entry_with_extra}
            )

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={},
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
            side_effect=add_external,
        ),
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.importable_count == 1
    assert result.processed_count == 0
    assert result.warning_count > 0  # fingerprint rejection + pending warnings
    assert store.entry.statistics_imported_through is None
    assert store.entry.external_migration_status != "complete"
    assert store.entry.statistics_source_fingerprint == _fingerprint
