"""Cross-utility statistics backfill invariants."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from custom_components.sp_group.const import DOMAIN
from custom_components.sp_group.ledger import SpGroupLedgerEntry, SpGroupLedgerSnapshot
from custom_components.sp_group.sources import SINGAPORE_TZ
from custom_components.sp_group.statistics_backfill import async_backfill_statistics
from homeassistant.components.recorder import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .test_recorder_statistics import (
    ELECTRIC_KEY,
    WATER_KEY,
    _accepted_bucket,
    _entry_with_ledger,
    _FakeLedgerStore,
    _FakeRecorderExecutor,
    _ledger_entry,
)


async def test_statistics_backfill_no_recorder_is_noop(
    hass: HomeAssistant,
) -> None:
    """Statistics import is a no-op when Recorder is not loaded."""
    store = _FakeLedgerStore(
        _ledger_entry(
            ELECTRIC_KEY,
            "electric",
            "100",
            (
                _accepted_bucket(
                    "2026-07-14T01:00:00+08:00",
                    "2026-07-14T01:30:00+08:00",
                    "1",
                ),
                _accepted_bucket(
                    "2026-07-14T01:30:00+08:00",
                    "2026-07-14T02:00:00+08:00",
                    "1",
                ),
            ),
        )
    )

    result = await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert result.planned_count == 0
    assert result.importable_count == 0
    assert result.processed_count == 0
    assert store.imported_through is None
    assert store.warnings == ()


async def test_statistics_backfill_missing_entity_skips_only_that_utility(
    hass: HomeAssistant,
) -> None:
    """Missing registry entity for one utility must not block another utility."""
    hass.config.components.add(RECORDER_DOMAIN)
    _register_sensor(hass, WATER_KEY, "sp_group_water")
    electric = _ledger_entry(
        ELECTRIC_KEY,
        "electric",
        "100",
        (
            _accepted_bucket(
                "2026-07-14T01:00:00+08:00",
                "2026-07-14T01:30:00+08:00",
                "1",
            ),
            _accepted_bucket(
                "2026-07-14T01:30:00+08:00",
                "2026-07-14T02:00:00+08:00",
                "1",
            ),
        ),
    )
    water = _water_entry(
        (
            _accepted_bucket(
                "2026-06-01T00:00:00+08:00",
                "2026-07-01T00:00:00+08:00",
                "10",
            ),
        )
    )
    store = _MultiFakeLedgerStore(electric, water)
    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    with _patched_statistics(
        {
            "sensor.sp_group_water": [
                {
                    "start": datetime(2026, 7, 20, 15, tzinfo=SINGAPORE_TZ),
                    "state": 1000.0,
                    "sum": 0.0,
                }
            ]
        },
        imported,
    ):
        result = await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert result.planned_count == 2
    assert [meta["statistic_id"] for meta, _rows in imported] == [
        "sensor.sp_group_water"
    ]
    assert ELECTRIC_KEY not in store.updates
    assert store.updates[WATER_KEY]["imported_through"] is not None


async def test_statistics_backfill_one_utility_waiting_does_not_block_other(
    hass: HomeAssistant,
) -> None:
    """A waiting native reference is per-utility metadata, not a global blocker."""
    hass.config.components.add(RECORDER_DOMAIN)
    _register_sensor(hass, ELECTRIC_KEY, "sp_group_electricity")
    _register_sensor(hass, WATER_KEY, "sp_group_water")
    electric = _ledger_entry(
        ELECTRIC_KEY,
        "electric",
        "100",
        (
            _accepted_bucket(
                "2026-07-14T01:00:00+08:00",
                "2026-07-14T01:30:00+08:00",
                "1",
            ),
            _accepted_bucket(
                "2026-07-14T01:30:00+08:00",
                "2026-07-14T02:00:00+08:00",
                "1",
            ),
        ),
    )
    water = _water_entry(
        (
            _accepted_bucket(
                "2026-06-01T00:00:00+08:00",
                "2026-07-01T00:00:00+08:00",
                "10",
            ),
        )
    )
    store = _MultiFakeLedgerStore(electric, water)
    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    with _patched_statistics(
        {
            "sensor.sp_group_electricity": [
                {
                    "start": datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ),
                    "state": 102.0,
                    "sum": 0.0,
                }
            ],
            "sensor.sp_group_water": [],
        },
        imported,
    ):
        await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert [meta["statistic_id"] for meta, _rows in imported] == [
        "sensor.sp_group_electricity"
    ]
    assert store.updates[ELECTRIC_KEY]["imported_through"] is not None
    assert store.updates[WATER_KEY]["warnings"] == (
        "statistics_waiting_for_native_reference",
    )


async def test_statistics_backfill_one_utility_conflict_does_not_corrupt_other(
    hass: HomeAssistant,
) -> None:
    """A conflict for one utility must not advance or corrupt another key."""
    hass.config.components.add(RECORDER_DOMAIN)
    _register_sensor(hass, ELECTRIC_KEY, "sp_group_electricity")
    _register_sensor(hass, WATER_KEY, "sp_group_water")
    water = _water_entry(
        (
            _accepted_bucket(
                "2026-06-01T00:00:00+08:00",
                "2026-07-01T00:00:00+08:00",
                "10",
            ),
        )
    )
    electric = _ledger_entry(
        ELECTRIC_KEY,
        "electric",
        "100",
        (
            _accepted_bucket(
                "2026-07-14T01:00:00+08:00",
                "2026-07-14T01:30:00+08:00",
                "1",
            ),
            _accepted_bucket(
                "2026-07-14T01:30:00+08:00",
                "2026-07-14T02:00:00+08:00",
                "1",
            ),
        ),
    )
    store = _MultiFakeLedgerStore(water, electric)
    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    with _patched_statistics(
        {
            "sensor.sp_group_water": [
                {
                    "start": datetime(2026, 5, 31, 23, tzinfo=SINGAPORE_TZ),
                    "state": 9999.0,
                    "sum": -10.0,
                },
                {
                    "start": datetime(2026, 7, 20, 15, tzinfo=SINGAPORE_TZ),
                    "state": 1000.0,
                    "sum": 0.0,
                },
            ],
            "sensor.sp_group_electricity": [
                {
                    "start": datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ),
                    "state": 102.0,
                    "sum": 0.0,
                }
            ],
        },
        imported,
    ):
        await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert [meta["statistic_id"] for meta, _rows in imported] == [
        "sensor.sp_group_electricity"
    ]
    assert store.updates[WATER_KEY]["warnings"] == (
        "statistics_conflict:2026-05-31T23:00:00+08:00",
    )
    assert store.updates[WATER_KEY]["imported_through"] is None
    assert store.updates[ELECTRIC_KEY]["imported_through"] is not None


@contextmanager
def _patched_statistics(
    existing_rows: dict[str, list[dict[str, Any]]],
    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]],
) -> Iterator[None]:
    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value=existing_rows,
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_import_statistics",
            side_effect=lambda _hass, meta, rows: imported.append((meta, list(rows))),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=_FakeRecorderExecutor(),
        ),
    ):
        yield


def _register_sensor(hass: HomeAssistant, unique_id: str, object_id: str) -> str:
    return er.async_get(hass).async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        unique_id,
        suggested_object_id=object_id,
    ).entity_id


def _water_entry(
    accepted: tuple[Any, ...],
    *,
    seed: datetime | None = None,
) -> SpGroupLedgerEntry:
    seed_at = seed or datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ)
    return SpGroupLedgerEntry(
        sensor_key=WATER_KEY,
        source="manual_seed_plus_monthly_deltas",
        utility_type="water",
        epoch_id="epoch-water-cross",
        seed_value=Decimal("1000"),
        seed_effective_from=seed_at,
        backfill_from=min(bucket.start for bucket in accepted),
        latest_total=Decimal("1000")
        + sum(
            (bucket.value for bucket in accepted if bucket.start >= seed_at),
            Decimal("0"),
        ),
        latest_accepted_at=max((bucket.end for bucket in accepted), default=None),
        accepted=accepted,
    )


class _MultiFakeLedgerStore(_FakeLedgerStore):
    """Fake ledger store with multiple entries and per-key update tracking."""

    def __init__(self, *entries: SpGroupLedgerEntry) -> None:
        super().__init__(entries[0])
        self.snapshot = SpGroupLedgerSnapshot(
            entries={entry.sensor_key: entry for entry in entries}
        )
        self.updates: dict[str, dict[str, Any]] = {}

    async def async_update_statistics_metadata(
        self,
        sensor_key: str,
        *,
        imported_through: datetime | None = None,
        reference_start: datetime | None = None,
        reference_sum_offset: Decimal | None = None,
        strategy_version: str | None = None,
        warnings: tuple[str, ...] = (),
    ) -> SpGroupLedgerSnapshot:
        self.updates[sensor_key] = {
            "imported_through": imported_through,
            "reference_start": reference_start,
            "reference_sum_offset": reference_sum_offset,
            "strategy_version": strategy_version,
            "warnings": warnings,
        }
        return await super().async_update_statistics_metadata(
            sensor_key,
            imported_through=imported_through,
            reference_start=reference_start,
            reference_sum_offset=reference_sum_offset,
            strategy_version=strategy_version,
            warnings=warnings,
        )
