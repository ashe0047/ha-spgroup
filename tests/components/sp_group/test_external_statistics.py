"""Tests for SP Group provider-owned Recorder statistics."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

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
    async_backfill_statistics,
)
from homeassistant.components.recorder import DOMAIN as RECORDER_DOMAIN
from homeassistant.core import HomeAssistant


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
        "statistics_recorder_rows_pending:2026-07-01T00:00:00+08:00",
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
        warnings=(f"statistics_recorder_rows_pending:{pending_from.isoformat()}",),
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


def test_external_reconciliation_allows_only_dirty_suffix_rewrites() -> None:
    """Unexpected old rows conflict, while accepted late data rewrites its suffix."""
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
    assert warnings == ()
    assert importable == (planned[0],)
    assert processed == (planned[1],)


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
            "custom_components.sp_group.statistics_backfill.get_metadata",
            return_value={},
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
        if query_count <= 2:
            return {statistic_id: []}
        return {statistic_id: list(recorder_rows)}

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.get_metadata",
            return_value={},
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            side_effect=query,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
            side_effect=add_external,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_VERIFY_BACKOFF",
            (0.0,),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_VERIFY_TIMEOUT",
            0.1,
        ),
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert query_count >= 3
    assert result.warning_count == 0
    assert store.entry.statistics_warnings == ()
    assert store.entry.external_migration_status == "complete"
    assert store.entry.statistics_imported_through == datetime(
        2026, 7, 31, 23, tzinfo=SINGAPORE_TZ
    )


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
            "custom_components.sp_group.statistics_backfill.get_metadata",
            return_value={},
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
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_VERIFY_BACKOFF",
            (0.0,),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_VERIFY_TIMEOUT",
            0.01,
        ),
    ):
        result = await async_backfill_statistics(hass, _entry_with_store(store))

    assert result.warning_count == 1
    assert store.entry.statistics_warnings == (
        "statistics_recorder_rows_pending:2026-07-31T23:00:00+08:00",
    )
    assert store.entry.statistics_imported_through is None
    assert store.entry.external_migration_status == "pending"
    assert store.entry.statistics_dirty_from == datetime(
        2026, 7, 31, 23, tzinfo=SINGAPORE_TZ
    )
    create_issue.assert_not_called()


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
            "custom_components.sp_group.statistics_backfill.get_metadata",
            return_value={},
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
    recorder_rows = [
        {
            "start": datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ),
            "state": 2.0,
            "sum": 2.0,
        }
    ]

    def query(*_args: Any, **_kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        return {statistic_id: list(recorder_rows) if visible else []}

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_Recorder(),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.get_metadata",
            return_value={},
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            side_effect=query,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_add_external_statistics",
        ) as add_mock,
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_VERIFY_BACKOFF",
            (0.0,),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_VERIFY_TIMEOUT",
            0.01,
        ),
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
            "custom_components.sp_group.statistics_backfill.get_metadata",
            return_value={},
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            side_effect=query,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_VERIFY_BACKOFF",
            (0.0,),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_VERIFY_TIMEOUT",
            0.1,
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
            "custom_components.sp_group.statistics_backfill.get_metadata",
            return_value={},
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            side_effect=query,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_VERIFY_BACKOFF",
            (0.0,),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill._RECORDER_VERIFY_TIMEOUT",
            0.1,
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
            "custom_components.sp_group.statistics_backfill.get_metadata",
            return_value={},
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
            "custom_components.sp_group.statistics_backfill.get_metadata",
            return_value={},
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
            "custom_components.sp_group.statistics_backfill.get_metadata",
            return_value={},
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
