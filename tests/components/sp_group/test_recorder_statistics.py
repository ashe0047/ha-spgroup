"""Test SP Group Recorder and statistics readiness."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from custom_components.sp_group import SpGroupRuntimeData
from custom_components.sp_group.const import (
    CONF_ACCOUNT_ID,
    CONF_SESSION,
    CONF_SOURCE_CONFIG,
    DOMAIN,
)
from custom_components.sp_group.discovery import SpGroupFutureSensorKey
from custom_components.sp_group.ledger import (
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

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={},
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

    assert recorder.executor_job_count == 1
    assert imported == [
        (
            {
                "has_mean": False,
                "has_sum": False,
                "name": None,
                "source": RECORDER_DOMAIN,
                "statistic_id": "sensor.sp_group_electricity",
                "unit_of_measurement": "kWh",
            },
            [
                    {
                        "start": datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ),
                        "state": 102.0,
                    },
                ],
            )
    ]
    assert store.imported_through == datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ)
    assert store.warnings == (
        "statistics_missing_bridge:2026-07-14T02:00:00+08:00",
    )


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

    assert imported == [
        [
            {
                "start": datetime(2026, 7, 15, 2, tzinfo=SINGAPORE_TZ),
                "state": 9950.0,
            },
            {
                "start": datetime(2026, 7, 15, 3, tzinfo=SINGAPORE_TZ),
                "state": 10000.0,
            },
            {
                "start": datetime(2026, 7, 15, 4, tzinfo=SINGAPORE_TZ),
                "state": 10012.0,
            },
        ]
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
                        "start": datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ),
                        "state": 102.0,
                    },
                {
                    "start": datetime(2026, 7, 14, 2, tzinfo=SINGAPORE_TZ),
                    "state": 999.0,
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

    assert recorder.executor_job_count == 1
    assert imported == []
    assert store.imported_through == datetime(2026, 7, 14, 1, tzinfo=SINGAPORE_TZ)
    assert store.warnings == (
        "statistics_conflict:2026-07-14T02:00:00+08:00",
    )


async def test_statistics_backfill_imports_water_at_final_month_hour(
    hass: HomeAssistant,
) -> None:
    """Monthly water buckets project to one row at the final hour of the month."""
    hass.config.components.add(RECORDER_DOMAIN)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        WATER_KEY,
        suggested_object_id="sp_group_water",
    )
    store = _FakeLedgerStore(
        _ledger_entry(
            WATER_KEY,
            "water",
            "10",
            (
                _accepted_bucket(
                    "2026-06-01T00:00:00+08:00",
                    "2026-07-01T00:00:00+08:00",
                    "2.5",
                ),
            ),
        )
    )
    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    recorder = _FakeRecorderExecutor()

    with (
        patch(
            "custom_components.sp_group.statistics_backfill.statistics_during_period",
            return_value={},
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

    assert recorder.executor_job_count == 1
    assert imported[0][0]["unit_of_measurement"] == "m³"
    assert imported[0][1] == [
        {
        "start": datetime(2026, 6, 30, 23, tzinfo=SINGAPORE_TZ),
        "state": 12.5,
    }
    ]
    assert store.imported_through == datetime(2026, 6, 30, 23, tzinfo=SINGAPORE_TZ)


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
    """Return the current UTC hour start used by HA statistics."""
    now = dt_util.utcnow()
    return now.replace(minute=0, second=0, microsecond=0)


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
        self.warnings: tuple[str, ...] = ()

    async def async_update_statistics_metadata(
        self,
        sensor_key: str,
        *,
        imported_through: datetime | None = None,
        warnings: tuple[str, ...] = (),
    ) -> SpGroupLedgerSnapshot:
        """Record statistics metadata updates."""
        assert sensor_key in self.snapshot.entries
        self.imported_through = imported_through
        self.warnings = warnings
        return self.snapshot


class _FakeRecorderExecutor:
    """Minimal Recorder instance that executes DB jobs through its executor API."""

    def __init__(self) -> None:
        """Initialize executor call tracking."""
        self.executor_job_count = 0
        self.block_till_done_count = 0

    async def async_add_executor_job(self, job: Any) -> Any:
        """Run a recorder DB job."""
        self.executor_job_count += 1
        return job()

    async def async_block_till_done(self) -> None:
        """Record waits for queued recorder tasks."""
        self.block_till_done_count += 1


def _entry_with_ledger(store: _FakeLedgerStore) -> Any:
    """Return a config-entry-like object with runtime ledger data."""
    return SimpleNamespace(runtime_data=SimpleNamespace(ledger_store=store))


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
