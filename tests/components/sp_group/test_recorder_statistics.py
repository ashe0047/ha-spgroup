"""Test SP Group Recorder and statistics readiness."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, patch

from custom_components.sp_group import SpGroupRuntimeData
from custom_components.sp_group.const import (
    CONF_ACCOUNT_ID,
    CONF_SESSION,
    CONF_SOURCE_CONFIG,
    DOMAIN,
)
from custom_components.sp_group.discovery import SpGroupFutureSensorKey
from custom_components.sp_group.ledger import (
    STATISTICS_STRATEGY_NATIVE_OFFSET_V2,
    SpGroupAcceptedBucket,
    SpGroupLedgerEntry,
    SpGroupLedgerSnapshot,
)
from custom_components.sp_group.sources import (
    SINGAPORE_TZ,
    build_source_config,
    source_options_payload,
)
from custom_components.sp_group.statistics_backfill import async_backfill_statistics
from homeassistant.components.recorder import (
    CONF_COMMIT_INTERVAL,
    CONF_DB_URL,
    get_instance,
)
from homeassistant.components.recorder import (
    DOMAIN as RECORDER_DOMAIN,
)
from homeassistant.components.recorder.models.statistics import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics as ha_async_import_statistics,
)
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import StatisticsTask
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.sensor import recorder as sensor_recorder
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .helpers import make_mobile_session
from .test_coordinator import FakeJarvisClient, FakeYggdrasilClient
from .test_sensor import assert_entity_state, client_factory

PREMISE_KEY = "sp_group_premise_938f4ae4706115485aaa8512"
ELECTRIC_KEY = f"{PREMISE_KEY}_electric"
WATER_KEY = f"{PREMISE_KEY}_water"
ATTR_STATE_CLASS = "state_class"
STATISTICS_TOLERANCE = Decimal("0.000001")


async def test_seeded_ledger_sensors_generate_recorder_statistics_metadata(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """Ledger-backed utility sensors expose HA statistics-compatible metadata."""
    await _setup_recorder(hass, tmp_path)
    entry = await _setup_seeded_entry(hass, tmp_path)
    registry = er.async_get(hass)

    electric_state = assert_entity_state(hass, registry, ELECTRIC_KEY, "100.0")
    water_state = assert_entity_state(hass, registry, WATER_KEY, "10.0")

    assert electric_state.attributes[ATTR_DEVICE_CLASS] == "energy"
    assert electric_state.attributes[ATTR_STATE_CLASS] == "total_increasing"
    assert electric_state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "kWh"
    assert water_state.attributes[ATTR_DEVICE_CLASS] == "water"
    assert water_state.attributes[ATTR_STATE_CLASS] == "total_increasing"
    assert water_state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "m³"

    await _flush_recorder(hass)
    await _compile_statistics_for_current_hour(hass)

    electric_entity_id = _entity_id(registry, ELECTRIC_KEY)
    water_entity_id = _entity_id(registry, WATER_KEY)
    metadata = await _wait_for_statistics_metadata(
        hass,
        {electric_entity_id, water_entity_id},
    )
    units = {
        statistic_id: meta["unit_of_measurement"]
        for statistic_id, (_, meta) in metadata.items()
    }

    assert units[electric_entity_id] == "kWh"
    assert units[water_entity_id] == "m³"
    assert isinstance(entry.runtime_data, SpGroupRuntimeData)


async def test_recorder_statistics_advance_after_accepted_ledger_buckets(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """Accepted bucket deltas advance Recorder-visible ledger totals."""
    await _setup_recorder(hass, tmp_path)
    entry = await _setup_seeded_entry(hass, tmp_path)
    electricity_coordinator = entry.runtime_data.electricity_coordinator
    water_coordinator = entry.runtime_data.water_coordinator
    assert electricity_coordinator is not None
    assert water_coordinator is not None
    jarvis = entry.runtime_data.jarvis_client
    assert isinstance(jarvis, FakeJarvisClient)

    jarvis.ami_payloads.extend(
        [
            _electricity_ami_payload("1.25"),
            _electricity_ami_payload("1.25"),
        ]
    )
    await electricity_coordinator.async_poll_now()
    await hass.async_block_till_done()
    await electricity_coordinator.async_poll_now()
    await hass.async_block_till_done()
    jarvis.chart_payload = _water_history_payload("2.5")
    await water_coordinator.async_poll_now()
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert_entity_state(hass, registry, ELECTRIC_KEY, "101.25")
    assert_entity_state(hass, registry, WATER_KEY, "12.5")

    await _flush_recorder(hass)
    await _compile_statistics_for_current_hour(hass)
    electric_entity_id = _entity_id(registry, ELECTRIC_KEY)
    water_entity_id = _entity_id(registry, WATER_KEY)
    await _wait_for_statistics_metadata(hass, {electric_entity_id, water_entity_id})
    stats = await _recorder_job(
        hass,
        statistics_during_period,
        hass,
        _current_hour_start(),
        None,
        None,
        "5minute",
        {"energy": "kWh", "volume": "m³"},
        {"state", "sum"},
    )

    assert stats[electric_entity_id][-1]["state"] == 101.25
    assert stats[water_entity_id][-1]["state"] == 12.5


async def test_unseeded_and_malformed_ledger_sensors_are_not_statistics_sources(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """Only trusted ledger totals become Recorder/statistics sources."""
    await _setup_recorder(hass, tmp_path)
    entry = await _setup_seeded_entry(hass, tmp_path, seeded=False)
    registry = er.async_get(hass)

    assert_entity_state(hass, registry, ELECTRIC_KEY, STATE_UNAVAILABLE)
    assert_entity_state(hass, registry, WATER_KEY, STATE_UNAVAILABLE)

    await _flush_recorder(hass)
    await _compile_statistics_for_current_hour(hass)

    assert not await _recorder_job(
        hass,
        get_metadata,
        hass,
    )
    assert isinstance(entry.runtime_data, SpGroupRuntimeData)


async def test_statistics_backfill_imports_hourly_electricity_projection(
    hass: HomeAssistant,
) -> None:
    """Thirty-minute ledger buckets project into deterministic hourly rows."""
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
                _accepted_bucket(
                    "2026-07-14T02:30:00+08:00",
                    "2026-07-14T03:00:00+08:00",
                    "0.5",
                ),
            ),
        )
    )
    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    recorder = _FakeRecorderExecutor()
    native_reference = {
        "start": datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ).timestamp(),
        "state": 102.5,
        "sum": 0.0,
    }

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={
                "sensor.sp_group_electricity": [native_reference]
            },
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_import_statistics",
            side_effect=lambda _hass, meta, rows: imported.append(
                (meta, list(rows))
            ),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=recorder,
        ),
    ):
        await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert recorder.executor_job_count == 2
    assert imported == [
        (
            {
                "has_mean": False,
                "has_sum": True,
                "mean_type": ANY,
                "name": None,
                "source": RECORDER_DOMAIN,
                "statistic_id": "sensor.sp_group_electricity",
                "unit_class": "energy",
                "unit_of_measurement": "kWh",
            },
            [
                {
                    "start": datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ),
                    "state": 100.0,
                    "sum": 0.0,
                },
                {
                    "start": datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ),
                    "state": 102.0,
                    "sum": 2.0,
                },
            ],
        )
    ]
    assert store.imported_through == datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ)
    assert store.strategy_version == STATISTICS_STRATEGY_NATIVE_OFFSET_V2
    assert store.reference_sum_offset == Decimal("2.00")
    assert recorder.adjustments == [
        (
            "sensor.sp_group_electricity",
            datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ),
            2.0,
            "kWh",
        )
    ]
    assert store.warnings == (
        "statistics_missing_bridge:2026-07-14T02:00:00+08:00",
    )
    _assert_dashboard_deltas(
        (*imported[0][1], _with_sum_offset(native_reference, Decimal("2"))),
        visible_start=datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ),
        visible_end=datetime(2026, 7, 14, 3, tzinfo=SINGAPORE_TZ),
        pre_window_sum=Decimal("0"),
        expected={
            datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ): Decimal("2"),
            datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ): Decimal("0"),
        },
    )


async def test_statistics_backfill_reuses_persisted_timestamp_reference(
    hass: HomeAssistant,
) -> None:
    """Persisted native references still match HA timestamp-shaped rows."""
    hass.config.components.add(RECORDER_DOMAIN)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        ELECTRIC_KEY,
        suggested_object_id="sp_group_electricity",
    )
    reference_start = datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ)
    store = _FakeLedgerStore(
        replace(
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
            ),
            statistics_reference_start=reference_start,
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={
                "sensor.sp_group_electricity": [
                    {
                        "start": reference_start.timestamp(),
                        "state": 102.0,
                        "sum": 0.0,
                    }
                ]
            },
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

    assert imported == [
        [
            {
                "start": datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ),
                "state": 100.0,
                "sum": 0.0,
            },
            {
                "start": datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ),
                "state": 102.0,
                "sum": 2.0,
            },
        ]
    ]
    assert store.reference_start == reference_start
    assert store.reference_sum_offset == Decimal("2.00")
    assert recorder.adjustments == [
        ("sensor.sp_group_electricity", reference_start, 2.0, "kWh")
    ]


async def test_forced_statistics_backfill_rechecks_recorder_after_watermark(
    hass: HomeAssistant,
) -> None:
    """Forced retries import missing deterministic rows after a native reference."""
    hass.config.components.add(RECORDER_DOMAIN)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        ELECTRIC_KEY,
        suggested_object_id="sp_group_electricity",
    )
    reference_start = datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ)
    imported_through = datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ)
    store = _FakeLedgerStore(
        replace(
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
                    _accepted_bucket(
                        "2026-07-14T02:00:00+08:00",
                        "2026-07-14T02:30:00+08:00",
                        "1",
                    ),
                    _accepted_bucket(
                        "2026-07-14T02:30:00+08:00",
                        "2026-07-14T03:00:00+08:00",
                        "1",
                    ),
                    _accepted_bucket(
                        "2026-07-14T03:00:00+08:00",
                        "2026-07-14T03:30:00+08:00",
                        "1.5",
                    ),
                    _accepted_bucket(
                        "2026-07-14T03:30:00+08:00",
                        "2026-07-14T04:00:00+08:00",
                        "1.5",
                    ),
                    _accepted_bucket(
                        "2026-07-14T04:00:00+08:00",
                        "2026-07-14T04:30:00+08:00",
                        "1",
                    ),
                    _accepted_bucket(
                        "2026-07-14T04:30:00+08:00",
                        "2026-07-14T05:00:00+08:00",
                        "1",
                    ),
                ),
            ),
            statistics_reference_start=reference_start,
            statistics_imported_through=imported_through,
            statistics_reference_sum_offset=Decimal("2"),
            statistics_strategy_version=STATISTICS_STRATEGY_NATIVE_OFFSET_V2,
        )
    )
    existing_rows = {
        "sensor.sp_group_electricity": [
            {
                "start": datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ),
                "state": 100.0,
                "sum": 0.0,
            },
            {
                "start": imported_through,
                "state": 102.0,
                "sum": 2.0,
            },
            {
                "start": reference_start,
                "state": 104.0,
                "sum": 2.0,
            },
        ]
    }
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

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
        result = await async_backfill_statistics(
            hass,
            _entry_with_ledger(store),
            force=True,
        )

    assert imported == [
        [
            {
                "start": datetime(2026, 7, 14, 3, tzinfo=SINGAPORE_TZ),
                "state": 107.0,
                "sum": 5.0,
            },
            {
                "start": datetime(2026, 7, 14, 4, tzinfo=SINGAPORE_TZ),
                "state": 109.0,
                "sum": 7.0,
            }
        ]
    ]
    reference_sum = existing_rows["sensor.sp_group_electricity"][-1]["sum"]
    assert imported[0][0]["sum"] - reference_sum == 3.0
    assert imported[0][1]["sum"] - imported[0][0]["sum"] == 2.0
    assert result.importable_count == 2
    assert result.processed_count == 4
    assert store.imported_through == datetime(2026, 7, 14, 4, tzinfo=SINGAPORE_TZ)
    assert store.reference_start == reference_start
    assert store.warnings == ()


async def test_statistics_backfill_projects_overlap_around_seed_anchor(
    hass: HomeAssistant,
) -> None:
    """Pre-seed rows subtract from the seed while post-seed rows add to it."""
    hass.config.components.add(RECORDER_DOMAIN)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        ELECTRIC_KEY,
        suggested_object_id="sp_group_electricity",
    )
    seed = datetime(2026, 7, 15, 4, tzinfo=SINGAPORE_TZ)
    accepted = (
        _accepted_bucket(
            "2026-07-15T02:00:00+08:00",
            "2026-07-15T02:30:00+08:00",
            "20",
        ),
        _accepted_bucket(
            "2026-07-15T02:30:00+08:00",
            "2026-07-15T03:00:00+08:00",
            "30",
        ),
        _accepted_bucket(
            "2026-07-15T03:00:00+08:00",
            "2026-07-15T03:30:00+08:00",
            "50",
        ),
        _accepted_bucket(
            "2026-07-15T03:30:00+08:00",
            "2026-07-15T04:00:00+08:00",
            "0",
        ),
        _accepted_bucket(
            "2026-07-15T04:00:00+08:00",
            "2026-07-15T04:30:00+08:00",
            "5",
        ),
        _accepted_bucket(
            "2026-07-15T04:30:00+08:00",
            "2026-07-15T05:00:00+08:00",
            "7",
        ),
    )
    store = _FakeLedgerStore(
        SpGroupLedgerEntry(
            sensor_key=ELECTRIC_KEY,
            source="manual_seed_plus_ami_deltas",
            utility_type="electric",
            epoch_id="epoch-overlap",
            seed_value=Decimal("10000"),
            seed_effective_from=seed,
            backfill_from=accepted[0].start,
            latest_total=Decimal("10012"),
            latest_accepted_at=max(bucket.end for bucket in accepted),
            accepted=accepted,
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={
                "sensor.sp_group_electricity": [
                    {
                        "start": datetime(2026, 7, 15, 5, tzinfo=SINGAPORE_TZ),
                        "state": 10012.0,
                        "sum": 0.0,
                    }
                ]
            },
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

    assert imported == [
        [
                {
                    "start": datetime(2026, 7, 15, 1, tzinfo=SINGAPORE_TZ),
                    "state": 9900.0,
                    "sum": 0.0,
                },
                {
                    "start": datetime(2026, 7, 15, 2, tzinfo=SINGAPORE_TZ),
                    "state": 9950.0,
                    "sum": 50.0,
                },
                {
                    "start": datetime(2026, 7, 15, 3, tzinfo=SINGAPORE_TZ),
                    "state": 10000.0,
                    "sum": 100.0,
                },
                {
                    "start": datetime(2026, 7, 15, 4, tzinfo=SINGAPORE_TZ),
                    "state": 10012.0,
                    "sum": 112.0,
                },
            ]
        ]
    deltas = [
        right["sum"] - left["sum"]
        for left, right in zip(imported[0], imported[0][1:], strict=False)
    ]
    assert deltas == [
        50.0,
        50.0,
        12.0,
    ]


async def test_statistics_backfill_skips_post_seed_rows_without_seed_bridge(
    hass: HomeAssistant,
) -> None:
    """Post-seed statistics rows need contiguous buckets from the seed anchor."""
    hass.config.components.add(RECORDER_DOMAIN)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        ELECTRIC_KEY,
        suggested_object_id="sp_group_electricity",
    )
    seed = datetime(2026, 7, 15, 4, tzinfo=SINGAPORE_TZ)
    accepted = (
        _accepted_bucket(
            "2026-07-15T04:30:00+08:00",
            "2026-07-15T05:00:00+08:00",
            "7",
        ),
    )
    store = _FakeLedgerStore(
        SpGroupLedgerEntry(
            sensor_key=ELECTRIC_KEY,
            source="manual_seed_plus_ami_deltas",
            utility_type="electric",
            epoch_id="epoch-gap",
            seed_value=Decimal("10000"),
            seed_effective_from=seed,
            backfill_from=seed,
            latest_total=Decimal("10007"),
            latest_accepted_at=accepted[0].end,
            accepted=accepted,
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={},
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

    assert imported == []
    assert recorder.executor_job_count == 0
    assert store.warnings == (
        "statistics_missing_bridge:2026-07-15T04:00:00+08:00",
    )


async def test_statistics_backfill_waits_for_native_reference(
    hass: HomeAssistant,
) -> None:
    """Native-reference waiting is tracked without creating an actionless repair."""
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
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={},
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

    assert imported == []
    assert store.imported_through is None
    assert store.strategy_version is None
    assert store.warnings == ("statistics_waiting_for_native_reference",)
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN,
        f"statistics_backfill_waiting_entry-statistics_{ELECTRIC_KEY}",
    )
    assert issue is None


async def test_statistics_backfill_skips_matching_rows_and_stops_on_conflict(
    hass: HomeAssistant,
) -> None:
    """Backfill is idempotent and does not import past conflicting HA history."""
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
                    "1",
                ),
                _accepted_bucket(
                    "2026-07-14T01:30:00+08:00",
                    "2026-07-14T02:00:00+08:00",
                    "1",
                ),
                _accepted_bucket(
                    "2026-07-14T02:00:00+08:00",
                    "2026-07-14T02:30:00+08:00",
                    "1",
                ),
                _accepted_bucket(
                    "2026-07-14T02:30:00+08:00",
                    "2026-07-14T03:00:00+08:00",
                    "1",
                ),
                _accepted_bucket(
                    "2026-07-14T03:00:00+08:00",
                    "2026-07-14T03:30:00+08:00",
                    "1",
                ),
                _accepted_bucket(
                    "2026-07-14T03:30:00+08:00",
                    "2026-07-14T04:00:00+08:00",
                    "1",
                ),
            ),
        )
    )
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
                return_value={
                    "sensor.sp_group_electricity": [
                        {
                            "start": datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ),
                            "state": 100.0,
                            "sum": 0.0,
                        },
                        {
                            "start": datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ),
                            "state": 102.0,
                            "sum": 2.0,
                        },
                        {
                            "start": datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ),
                            "state": 999.0,
                            "sum": 5.0,
                        },
                        {
                            "start": datetime(2026, 7, 14, 4, tzinfo=SINGAPORE_TZ),
                            "state": 106.0,
                            "sum": 6.0,
                        },
                    ]
                },
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

    assert recorder.executor_job_count == 2
    assert imported == []
    assert store.imported_through == datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ)
    assert store.strategy_version == STATISTICS_STRATEGY_NATIVE_OFFSET_V2
    assert store.warnings == (
        "statistics_conflict:2026-07-14T02:00:00+08:00",
    )


async def test_statistics_backfill_processes_existing_baseline_without_duplicate_import(
    hass: HomeAssistant,
) -> None:
    """Exact baseline/projection rows are idempotent and advance the watermark."""
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
    existing_rows = {
        "sensor.sp_group_electricity": [
                {
                    "start": datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ),
                    "state": 100.0,
                    "sum": 0.0,
                },
                {
                    "start": datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ),
                    "state": 102.0,
                    "sum": 2.0,
                },
                {
                    "start": datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ),
                    "state": 102.0,
                    "sum": 2.0,
                },
        ]
    }
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

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
        result = await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert imported == []
    assert result.processed_count == 2
    assert store.imported_through == datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ)
    assert store.strategy_version == STATISTICS_STRATEGY_NATIVE_OFFSET_V2
    assert store.warnings == ()


async def test_statistics_backfill_conflicting_baseline_stops_import(
    hass: HomeAssistant,
) -> None:
    """A mismatched baseline row is a conflict and prevents partial import."""
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
    existing_rows = {
        "sensor.sp_group_electricity": [
            {
                "start": datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ),
                "state": 100.0,
                "sum": -99.0,
            },
            {
                "start": datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ),
                "state": 102.0,
                "sum": 0.0,
            },
            {
                "start": datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ),
                "state": 102.0,
                "sum": 0.0,
            },
        ]
    }
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

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
        result = await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert imported == []
    assert result.importable_count == 0
    assert result.processed_count == 0
    assert store.imported_through is None
    assert store.strategy_version is None
    assert store.warnings == (
        "statistics_conflict:2026-07-14T00:00:00+08:00",
    )


async def test_statistics_backfill_existing_negative_delta_creates_repair(
    hass: HomeAssistant,
) -> None:
    """Already-decreasing Recorder sums block import before row planning."""
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
    existing_rows = {
        "sensor.sp_group_electricity": [
            {
                "start": datetime(2026, 7, 14, 0, tzinfo=SINGAPORE_TZ),
                "state": 100.0,
                "sum": -1.0,
            },
            {
                "start": datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ),
                "state": 102.0,
                "sum": -2.0,
            },
            {
                "start": datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ),
                "state": 102.0,
                "sum": 0.0,
            },
        ]
    }
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

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
        result = await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert imported == []
    assert result.planned_count == 0
    assert store.imported_through is None
    assert store.strategy_version is None
    assert store.warnings[0].startswith("statistics_existing_negative_delta:")
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN,
        f"statistics_backfill_conflict_entry-statistics_{ELECTRIC_KEY}",
    )
    assert issue is not None


async def test_statistics_backfill_imports_water_at_final_month_hour(
    hass: HomeAssistant,
) -> None:
    """Monthly water buckets import as non-negative visible dashboard deltas."""
    hass.config.components.add(RECORDER_DOMAIN)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        WATER_KEY,
        suggested_object_id="sp_group_water",
    )
    accepted = (
        _accepted_bucket(
            "2026-06-01T00:00:00+08:00",
            "2026-07-01T00:00:00+08:00",
            "2.5",
        ),
        _accepted_bucket(
            "2026-07-01T00:00:00+08:00",
            "2026-08-01T00:00:00+08:00",
            "3.0",
        ),
        _accepted_bucket(
            "2026-08-01T00:00:00+08:00",
            "2026-09-01T00:00:00+08:00",
            "4.5",
        ),
    )
    seed = datetime(2026, 9, 1, tzinfo=SINGAPORE_TZ)
    store = _FakeLedgerStore(
        SpGroupLedgerEntry(
            sensor_key=WATER_KEY,
            source="manual_seed_plus_monthly_deltas",
            utility_type="water",
            epoch_id="epoch-water-monthly",
            seed_value=Decimal("10"),
            seed_effective_from=seed,
            backfill_from=accepted[0].start,
            latest_total=Decimal("10"),
            latest_accepted_at=seed,
            accepted=accepted,
        )
    )
    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    recorder = _FakeRecorderExecutor()
    native_reference = {
        "start": seed.timestamp(),
        "state": 10.0,
        "sum": 0.0,
    }

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={
                "sensor.sp_group_water": [native_reference]
            },
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.async_import_statistics",
            side_effect=lambda _hass, meta, rows: imported.append(
                (meta, list(rows))
            ),
        ),
        patch(
            "custom_components.sp_group.statistics_backfill.get_instance",
            return_value=recorder,
        ),
    ):
        await async_backfill_statistics(hass, _entry_with_ledger(store))

    assert recorder.executor_job_count == 2
    assert imported[0][0]["unit_of_measurement"] == "m³"
    assert imported[0][0]["unit_class"] == "volume"
    assert imported[0][0]["has_sum"] is True
    assert imported[0][0]["has_mean"] is False
    imported_rows = imported[0][1]
    assert store.reference_sum_offset == Decimal("10.0")
    assert recorder.adjustments == [
        ("sensor.sp_group_water", seed, 10.0, "m³")
    ]
    _assert_dashboard_deltas(
        (*imported_rows, _with_sum_offset(native_reference, Decimal("10"))),
        visible_start=datetime(2026, 5, 1, tzinfo=SINGAPORE_TZ),
        visible_end=datetime(2026, 9, 2, tzinfo=SINGAPORE_TZ),
        pre_window_sum=Decimal("0"),
        expected={
            datetime(2026, 5, 31, 23, tzinfo=SINGAPORE_TZ): Decimal("0"),
            datetime(2026, 6, 30, 23, tzinfo=SINGAPORE_TZ): Decimal("2.5"),
            datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ): Decimal("3.0"),
            datetime(2026, 8, 31, 23, tzinfo=SINGAPORE_TZ): Decimal("4.5"),
            seed: Decimal("0"),
        },
    )
    assert store.imported_through == datetime(2026, 8, 31, 23, tzinfo=SINGAPORE_TZ)


async def test_statistics_backfill_water_live_bug_first_month_not_negative(
    hass: HomeAssistant,
) -> None:
    """Water history must not show negative usage before the first source bucket."""
    hass.config.components.add(RECORDER_DOMAIN)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        WATER_KEY,
        suggested_object_id="sp_group_water",
    )
    accepted = _live_bug_water_buckets()
    seed = datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ)
    store = _FakeLedgerStore(
        SpGroupLedgerEntry(
            sensor_key=WATER_KEY,
            source="manual_seed_plus_monthly_deltas",
            utility_type="water",
            epoch_id="epoch-water-live-bug",
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

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={"sensor.sp_group_water": [native_reference]},
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

    assert sum((bucket.value for bucket in accepted), Decimal("0")) == Decimal("244.4")
    assert store.reference_sum_offset == Decimal("244.4")
    assert recorder.adjustments == [
        (
            "sensor.sp_group_water",
            datetime(2026, 7, 20, 15, tzinfo=SINGAPORE_TZ),
            244.4,
            "m³",
        )
    ]
    _assert_dashboard_deltas(
        (*imported[0], _with_sum_offset(native_reference, Decimal("244.4"))),
        visible_start=datetime(2025, 7, 1, tzinfo=SINGAPORE_TZ),
        visible_end=datetime(2026, 7, 21, tzinfo=SINGAPORE_TZ),
        pre_window_sum=Decimal("0"),
        expected={
            datetime(2025, 7, 31, 23, tzinfo=SINGAPORE_TZ): Decimal("0"),
            datetime(2025, 8, 31, 23, tzinfo=SINGAPORE_TZ): Decimal("23.5"),
            datetime(2026, 6, 30, 23, tzinfo=SINGAPORE_TZ): Decimal("18.7"),
            datetime(2026, 7, 20, 15, tzinfo=SINGAPORE_TZ): Decimal("0"),
        },
    )


async def test_forced_statistics_backfill_imports_missed_post_reference_water_bucket(
    hass: HomeAssistant,
) -> None:
    """Forced water retry imports post-reference monthly gaps as positive deltas."""
    hass.config.components.add(RECORDER_DOMAIN)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        WATER_KEY,
        suggested_object_id="sp_group_water",
    )
    reference_start = datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ)
    imported_through = datetime(2026, 6, 30, 23, tzinfo=SINGAPORE_TZ)
    accepted = (
        _accepted_bucket(
            "2026-07-01T00:00:00+08:00",
            "2026-08-01T00:00:00+08:00",
            "10",
        ),
        _accepted_bucket(
            "2026-08-01T00:00:00+08:00",
            "2026-09-01T00:00:00+08:00",
            "15",
        ),
    )
    store = _FakeLedgerStore(
        replace(
            SpGroupLedgerEntry(
                sensor_key=WATER_KEY,
                source="manual_seed_plus_monthly_deltas",
                utility_type="water",
                epoch_id="epoch-water-force",
                seed_value=Decimal("1000"),
                seed_effective_from=reference_start,
                backfill_from=reference_start,
                latest_total=Decimal("1025"),
                latest_accepted_at=accepted[-1].end,
                accepted=accepted,
            ),
            statistics_reference_start=reference_start,
            statistics_imported_through=imported_through,
            statistics_reference_sum_offset=Decimal("0"),
            statistics_strategy_version=STATISTICS_STRATEGY_NATIVE_OFFSET_V2,
        )
    )
    existing_rows = {
        "sensor.sp_group_water": [
            {
                "start": reference_start.timestamp(),
                "state": 1000.0,
                "sum": 0.0,
            }
        ]
    }
    imported: list[list[dict[str, Any]]] = []
    recorder = _FakeRecorderExecutor()

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
        result = await async_backfill_statistics(
            hass,
            _entry_with_ledger(store),
            force=True,
        )

    assert imported == [
        [
            {
                "start": datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ),
                "state": 1010.0,
                "sum": 10.0,
            },
            {
                "start": datetime(2026, 8, 31, 23, tzinfo=SINGAPORE_TZ),
                "state": 1025.0,
                "sum": 25.0,
            },
        ]
    ]
    _assert_dashboard_deltas(
        (existing_rows["sensor.sp_group_water"][0], *imported[0]),
        visible_start=reference_start,
        visible_end=datetime(2026, 9, 2, tzinfo=SINGAPORE_TZ),
        pre_window_sum=Decimal("0"),
        expected={
            reference_start: Decimal("0"),
            datetime(2026, 7, 31, 23, tzinfo=SINGAPORE_TZ): Decimal("10"),
            datetime(2026, 8, 31, 23, tzinfo=SINGAPORE_TZ): Decimal("15"),
        },
    )
    assert result.importable_count == 2
    assert store.imported_through == datetime(2026, 8, 31, 23, tzinfo=SINGAPORE_TZ)
    assert store.reference_start == reference_start


async def test_statistics_backfill_water_deltas_from_real_recorder_rows(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """Recorder-backed import must not persist the live negative-delta shape."""
    await _setup_recorder(hass, tmp_path)
    registry = er.async_get(hass)
    entity_id = registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        WATER_KEY,
        suggested_object_id="sp_group_water",
    ).entity_id
    accepted = _live_bug_water_buckets()
    seed = datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ)
    store = _FakeLedgerStore(
        SpGroupLedgerEntry(
            sensor_key=WATER_KEY,
            source="manual_seed_plus_monthly_deltas",
            utility_type="water",
            epoch_id="epoch-water-recorder",
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
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()
    await hass.async_block_till_done()
    rows = await _wait_for_statistic_rows(
        hass,
        entity_id,
        datetime(2025, 7, 1, tzinfo=SINGAPORE_TZ),
        datetime(2026, 7, 21, tzinfo=SINGAPORE_TZ),
        minimum_count=12,
    )

    _assert_dashboard_deltas(
        tuple(rows),
        visible_start=datetime(2025, 7, 1, tzinfo=SINGAPORE_TZ),
        visible_end=datetime(2026, 7, 21, tzinfo=SINGAPORE_TZ),
        pre_window_sum=Decimal("0"),
        expected={
            datetime(2025, 7, 31, 23, tzinfo=SINGAPORE_TZ): Decimal("0"),
            datetime(2025, 8, 31, 23, tzinfo=SINGAPORE_TZ): Decimal("23.5"),
            datetime(2026, 6, 30, 23, tzinfo=SINGAPORE_TZ): Decimal("18.7"),
            datetime(2026, 7, 20, 15, tzinfo=SINGAPORE_TZ): Decimal("0"),
        },
    )


def _assert_dashboard_deltas(
    rows: tuple[dict[str, Any], ...],
    *,
    visible_start: datetime,
    visible_end: datetime,
    pre_window_sum: Decimal,
    expected: dict[datetime, Decimal],
) -> None:
    """Assert Energy Dashboard-style consumption deltas for visible rows."""
    deltas = _dashboard_deltas(
        rows,
        visible_start=visible_start,
        visible_end=visible_end,
        pre_window_sum=pre_window_sum,
    )
    negative = {
        start: delta for start, delta in deltas.items() if delta < -STATISTICS_TOLERANCE
    }
    assert negative == {}
    for start, delta in expected.items():
        assert _decimal_close(deltas.get(start), delta), (start, deltas)


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


def _with_sum_offset(row: dict[str, Any], offset: Decimal) -> dict[str, Any]:
    """Return a statistics row as it appears after Recorder sum adjustment."""
    return {**row, "sum": float(Decimal(str(row["sum"])) + offset)}


def _statistics_start(row: dict[str, Any]) -> datetime:
    """Normalize HA statistics row starts used by tests."""
    value = row["start"]
    if isinstance(value, datetime):
        return value.astimezone(SINGAPORE_TZ)
    return datetime.fromtimestamp(float(value), tz=SINGAPORE_TZ)


def _decimal_close(value: Decimal | None, expected: Decimal) -> bool:
    if value is None:
        return False
    return abs(value - expected) < STATISTICS_TOLERANCE


async def _setup_recorder(hass: HomeAssistant, tmp_path: Path) -> None:
    """Set up HA Recorder against a temporary SQLite database."""
    hass.config.config_dir = str(tmp_path)
    hass.data.setdefault(
        RECORDER_DOMAIN,
        SimpleNamespace(
            db_connected=hass.loop.create_future(),
            recorder_platforms={SENSOR_DOMAIN: sensor_recorder},
        ),
    )
    assert await async_setup_component(
        hass,
        RECORDER_DOMAIN,
        {
            RECORDER_DOMAIN: {
                CONF_DB_URL: f"sqlite:///{tmp_path / 'recorder.db'}",
                CONF_COMMIT_INTERVAL: 1,
            }
        },
    )
    await hass.async_block_till_done()
    hass.data[RECORDER_DOMAIN].recorder_platforms[SENSOR_DOMAIN] = sensor_recorder
    await get_instance(hass).async_block_till_done()


async def _setup_seeded_entry(
    hass: HomeAssistant,
    tmp_path: Path,
    *,
    seeded: bool = True,
) -> MockConfigEntry:
    """Set up the integration with deterministic seeded source options."""
    hass.config.config_dir = str(tmp_path)
    session = make_mobile_session("auth0|account-1")
    calls: list[str] = []
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={
            CONF_ACCOUNT_ID: "auth0|account-1",
            CONF_SESSION: session.to_dict(),
        },
        options=(
            {CONF_SOURCE_CONFIG: source_options_payload(_source_configs())}
            if seeded
            else {}
        ),
    )
    entry.add_to_hass(hass)
    jarvis = FakeJarvisClient(calls)

    with (
        patch(
            "custom_components.sp_group.async_get_clientsession",
            return_value=object(),
        ),
        patch("custom_components.sp_group.MobileAuthClient", client_factory(object())),
        patch("custom_components.sp_group.ProfileClient", client_factory(object())),
        patch("custom_components.sp_group.JarvisClient", client_factory(jarvis)),
        patch(
            "custom_components.sp_group.YggdrasilClient",
            client_factory(FakeYggdrasilClient(calls)),
        ),
        patch(
            "custom_components.sp_group.async_start_initial_backfill",
            side_effect=lambda setup_hass, _entry: setup_hass.async_create_task(
                _noop()
            ),
        ),
        patch(
            "custom_components.sp_group.coordinator.SpGroupWaterCoordinator"
            ".async_start_startup_catchup",
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _source_configs() -> dict[str, Any]:
    """Return source configs for the deterministic discovery fixture."""
    electric_key = SpGroupFutureSensorKey(
        key=ELECTRIC_KEY,
        premise_key=PREMISE_KEY,
        utility_type="electric",
    )
    water_key = SpGroupFutureSensorKey(
        key=WATER_KEY,
        premise_key=PREMISE_KEY,
        utility_type="water",
    )
    return {
        ELECTRIC_KEY: build_source_config(
            sensor_key=electric_key,
            seed_value="100.0",
            seed_observed_at="2026-07-14T00:00:00+08:00",
            backfill_from="2026-07-14T00:00:00+08:00",
            now=datetime(2026, 7, 15, tzinfo=SINGAPORE_TZ),
        ),
        WATER_KEY: build_source_config(
            sensor_key=water_key,
            seed_value="10.0",
            seed_observed_at="2026-06-01T00:00:00+08:00",
            backfill_from="2026-06-01T00:00:00+08:00",
            now=datetime(2026, 7, 15, tzinfo=SINGAPORE_TZ),
        ),
    }


async def _flush_recorder(hass: HomeAssistant) -> None:
    """Wait until Recorder has processed queued state changes."""
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()


async def _noop() -> None:
    """Return immediately for patched background task creation."""
    return None


async def _compile_statistics_for_current_hour(hass: HomeAssistant) -> None:
    """Compile statistics for the current hour."""
    recorder = get_instance(hass)
    recorder.queue_task(StatisticsTask(_current_hour_start(), False))
    await recorder.async_block_till_done()


async def _wait_for_statistics_metadata(
    hass: HomeAssistant,
    statistic_ids: set[str],
) -> dict[str, Any]:
    """Wait until Recorder has committed statistics metadata for the IDs."""
    for _ in range(10):
        await get_instance(hass).async_block_till_done()
        metadata = await _recorder_job(
            hass,
            get_metadata,
            hass,
            statistic_ids=statistic_ids,
        )
        if statistic_ids.issubset(metadata):
            return metadata
        await asyncio.sleep(0)
    raise AssertionError(f"missing statistics metadata for {sorted(statistic_ids)}")


async def _wait_for_statistic_rows(
    hass: HomeAssistant,
    entity_id: str,
    start: datetime,
    end: datetime,
    *,
    minimum_count: int,
) -> list[dict[str, Any]]:
    """Wait until Recorder statistics rows are visible to queries."""
    for _ in range(20):
        await hass.async_block_till_done()
        await get_instance(hass).async_block_till_done()
        rows = await _recorder_job(
            hass,
            statistics_during_period,
            hass,
            start,
            end,
            {entity_id},
            "hour",
            None,
            {"state", "sum"},
        )
        visible = rows.get(entity_id, [])
        if len(visible) >= minimum_count:
            return visible
        await asyncio.sleep(0.01)
    raise AssertionError(f"missing statistics rows for {entity_id}")


async def _recorder_job(
    hass: HomeAssistant,
    target: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run a Recorder database job off the event loop."""
    job = partial(target, *args, **kwargs)
    return await get_instance(hass).async_add_executor_job(job)


def _entity_id(registry: er.EntityRegistry, unique_id: str) -> str:
    """Return the entity ID for a sensor unique ID."""
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


def _current_hour_start() -> datetime:
    """Return the current UTC five-minute start used by HA statistics."""
    now = dt_util.utcnow()
    return now.replace(
        minute=now.minute - (now.minute % 5),
        second=0,
        microsecond=0,
    )


def _electricity_ami_payload(consumption: str) -> dict[str, Any]:
    """Return an electricity AMI payload with one closed 30-minute bucket."""
    return {
        "unit": "kWh",
        "history": [{"data": [{"date": "20260714010000", "consumption": consumption}]}],
    }


def _water_history_payload(consumption: str) -> dict[str, Any]:
    """Return a water monthly history-chart payload with one closed period."""
    return {
        "water": {
            "data": [
                {
                    "period": "2026-06",
                    "status": "ACTUAL",
                    "unit": "m³",
                    "consumption": {"current": consumption},
                }
            ]
        }
    }


class _FakeLedgerStore:
    """Minimal ledger store used by statistics import tests."""

    def __init__(self, entry: SpGroupLedgerEntry) -> None:
        """Initialize the fake store."""
        self.snapshot = SpGroupLedgerSnapshot(entries={entry.sensor_key: entry})
        self.imported_through: datetime | None = None
        self.reference_start: datetime | None = None
        self.reference_sum_offset: Decimal | None = None
        self.strategy_version: str | None = None
        self.warnings: tuple[str, ...] = ()

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
        """Record statistics metadata updates."""
        assert sensor_key in self.snapshot.entries
        self.imported_through = imported_through
        self.reference_start = reference_start
        self.reference_sum_offset = reference_sum_offset
        self.strategy_version = strategy_version
        self.warnings = warnings
        current = self.snapshot.entries[sensor_key]
        self.snapshot = SpGroupLedgerSnapshot(
            entries={
                **self.snapshot.entries,
                sensor_key: replace(
                    current,
                    statistics_imported_through=(
                        imported_through
                        if imported_through is not None
                        else current.statistics_imported_through
                    ),
                    statistics_reference_start=(
                        reference_start
                        if reference_start is not None
                        else current.statistics_reference_start
                    ),
                    statistics_reference_sum_offset=(
                        reference_sum_offset
                        if reference_sum_offset is not None
                        else current.statistics_reference_sum_offset
                    ),
                    statistics_strategy_version=(
                        strategy_version
                        if strategy_version is not None
                        else current.statistics_strategy_version
                    ),
                    statistics_warnings=warnings,
                ),
            },
            malformed=self.snapshot.malformed,
        )
        return self.snapshot


class _FakeRecorderExecutor:
    """Minimal Recorder instance that executes DB jobs through its executor API."""

    def __init__(self) -> None:
        """Initialize executor call tracking."""
        self.executor_job_count = 0
        self.block_till_done_count = 0
        self.adjustments: list[tuple[str, datetime, float, str]] = []

    async def async_add_executor_job(self, job: Any) -> Any:
        """Run a recorder DB job."""
        self.executor_job_count += 1
        return job()

    async def async_block_till_done(self) -> None:
        """Record waits for queued recorder tasks."""
        self.block_till_done_count += 1

    def async_adjust_statistics(
        self,
        statistic_id: str,
        start_time: datetime,
        sum_adjustment: float,
        adjustment_unit: str,
    ) -> None:
        """Record statistics sum adjustments."""
        self.adjustments.append(
            (statistic_id, start_time, sum_adjustment, adjustment_unit)
        )


def _entry_with_ledger(store: _FakeLedgerStore) -> Any:
    """Return a config-entry-like object with runtime ledger data."""
    return SimpleNamespace(
        entry_id="entry-statistics",
        runtime_data=SimpleNamespace(ledger_store=store),
    )


def _ledger_entry(
    sensor_key: str,
    utility_type: str,
    seed_value: str,
    accepted: tuple[SpGroupAcceptedBucket, ...],
) -> SpGroupLedgerEntry:
    """Return a ledger entry with accepted buckets."""
    seed = min(
        (bucket.start for bucket in accepted),
        default=datetime(2026, 6, 1, tzinfo=SINGAPORE_TZ),
    )
    return SpGroupLedgerEntry(
        sensor_key=sensor_key,
        source="manual_seed_plus_ami_deltas",
        utility_type=utility_type,
        epoch_id="epoch-1",
        seed_value=Decimal(seed_value),
        seed_effective_from=seed,
        backfill_from=min(
            (bucket.start for bucket in accepted),
            default=seed,
        ),
        latest_total=Decimal(seed_value) + sum(
            (
                bucket.value
                for bucket in accepted
                if bucket.start >= seed
            ),
            Decimal("0"),
        ),
        latest_accepted_at=max((bucket.end for bucket in accepted), default=None),
        accepted=accepted,
    )


def _accepted_bucket(
    start: str,
    end: str,
    value: str,
) -> SpGroupAcceptedBucket:
    """Return an accepted bucket."""
    observed_at = datetime(2026, 7, 15, tzinfo=SINGAPORE_TZ)
    return SpGroupAcceptedBucket(
        bucket_key=f"{start}:{end}",
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        value=Decimal(value),
        first_seen_at=observed_at,
        accepted_at=observed_at,
    )


def _live_bug_water_buckets() -> tuple[SpGroupAcceptedBucket, ...]:
    """Return the monthly water buckets observed in the live negative-delta case."""
    values = (
        ("2025-08-01T00:00:00+08:00", "2025-09-01T00:00:00+08:00", "23.5"),
        ("2025-09-01T00:00:00+08:00", "2025-10-01T00:00:00+08:00", "26.3"),
        ("2025-10-01T00:00:00+08:00", "2025-11-01T00:00:00+08:00", "24.1"),
        ("2025-11-01T00:00:00+08:00", "2025-12-01T00:00:00+08:00", "21.5"),
        ("2025-12-01T00:00:00+08:00", "2026-01-01T00:00:00+08:00", "22.4"),
        ("2026-01-01T00:00:00+08:00", "2026-02-01T00:00:00+08:00", "25.7"),
        ("2026-02-01T00:00:00+08:00", "2026-03-01T00:00:00+08:00", "26.9"),
        ("2026-03-01T00:00:00+08:00", "2026-04-01T00:00:00+08:00", "12.5"),
        ("2026-04-01T00:00:00+08:00", "2026-05-01T00:00:00+08:00", "20"),
        ("2026-05-01T00:00:00+08:00", "2026-06-01T00:00:00+08:00", "22.8"),
        ("2026-06-01T00:00:00+08:00", "2026-07-01T00:00:00+08:00", "18.7"),
    )
    return tuple(_accepted_bucket(start, end, value) for start, end, value in values)
