"""Test SP Group coordinator runtime boundaries."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from custom_components.sp_group import SpGroupRuntimeData
from custom_components.sp_group.const import CONF_ACCOUNT_ID, CONF_SESSION, DOMAIN
from custom_components.sp_group.coordinator import (
    SpGroupCoordinatorData,
    SpGroupDataUpdateCoordinator,
    SpGroupElectricityCoordinator,
    SpGroupWaterCoordinator,
    _ami_rows,
    _electricity_ami_needs_history,
    _electricity_ami_source_poll,
    _missing_closed_water_months,
    _water_history_observations,
    _water_history_source_poll,
    _water_startup_catchup_needed,
)
from custom_components.sp_group.discovery import (
    SpGroupFutureSensorKey,
    SpGroupPremise,
    SpGroupUtilityAccount,
)
from custom_components.sp_group.ledger import (
    LedgerError,
    SpGroupBucketObservation,
    SpGroupLedgerStore,
    SpGroupSourceCoverage,
)
from custom_components.sp_group.sources import (
    SINGAPORE_TZ,
    SOURCE_MANUAL_SEED_MONTHLY,
    build_source_config,
    source_options_payload,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from spgroup.mobile.errors import (
    SpGroupApiError,
    SpGroupMobileError,
    SpGroupTransportError,
)

from .helpers import make_mobile_session
from .test_discovery import (
    jarvis_payload,
    meter_type_payload,
    premise_type_payload,
    yggdrasil_payload,
)
from .test_electricity import history_chart_payload


class UtilityClientSentinel:
    """Raise if HA-2 accidentally starts utility polling."""

    def __getattr__(self, name: str) -> object:
        """Fail on any attempted SDK endpoint access."""
        raise AssertionError(f"unexpected utility SDK access: {name}")


class FakeYggdrasilClient:
    """Fake Yggdrasil client that records discovery calls."""

    def __init__(
        self,
        calls: list[str],
        *,
        premises_payload: dict[str, object] | None = None,
    ) -> None:
        """Initialize the fake client."""
        self.calls = calls
        self.premises_payload = premises_payload

    async def get_premises_details(self, session: object) -> dict[str, object]:
        """Return fixture premise details."""
        self.calls.append("yggdrasil.get_premises_details")
        return self.premises_payload or yggdrasil_payload()

    async def get_meter_type(
        self,
        session: object,
        account_no: str,
    ) -> dict[str, object]:
        """Return fixture meter type data."""
        self.calls.append(f"yggdrasil.get_meter_type:{account_no}")
        return meter_type_payload()

    async def get_premise_type(
        self,
        session: object,
        account_no: str,
    ) -> dict[str, object]:
        """Return fixture premise type data."""
        self.calls.append(f"yggdrasil.get_premise_type:{account_no}")
        return premise_type_payload()

    def __getattr__(self, name: str) -> object:
        """Fail on attempted non-discovery Yggdrasil SDK access."""
        raise AssertionError(f"unexpected Yggdrasil SDK access: {name}")


class FakeJarvisClient:
    """Fake Jarvis client that records discovery and chart calls."""

    def __init__(
        self,
        calls: list[str],
        *,
        account_number: str = "ACC-1",
        me_payload: dict[str, object] | None = None,
        chart_payload: dict[str, object] | None = None,
        chart_error: Exception | None = None,
    ) -> None:
        """Initialize the fake client."""
        self.calls = calls
        self.account_number = account_number
        self.me_payload = me_payload
        self.chart_payload = chart_payload
        self.chart_error = chart_error
        self.ami_payloads: list[dict[str, object]] = []
        self.ami_requests: list[tuple[object, object, str, str | None]] = []

    async def get_me(self, session: object) -> dict[str, object]:
        """Return fixture Jarvis profile data."""
        self.calls.append("jarvis.get_me")
        return self.me_payload or jarvis_payload(account_number=self.account_number)

    async def get_history_charts(
        self,
        session: object,
        premise_id: str,
    ) -> dict[str, object]:
        """Return fixture electricity chart data."""
        self.calls.append(f"jarvis.get_history_charts:{premise_id}")
        if self.chart_error is not None:
            raise self.chart_error
        return self.chart_payload or history_chart_payload()

    async def get_ami_charts(
        self,
        session: object,
        premise_id: str,
        start: object,
        end: object,
        grouped_by: str,
        utility_type: str | None = None,
    ) -> dict[str, object]:
        """Return fixture AMI data."""
        self.calls.append(f"jarvis.get_ami_charts:{grouped_by}:{utility_type}")
        self.ami_requests.append((start, end, grouped_by, utility_type))
        if self.ami_payloads:
            return self.ami_payloads.pop(0)
        return {
            "unit": "kWh",
            "history": [
                {
                    "data": [
                        {
                            "date": "20260714010000",
                            "consumption": "1.25",
                        }
                    ]
                }
            ],
        }

    def __getattr__(self, name: str) -> object:
        """Fail on attempted utility-reading SDK access."""
        raise AssertionError(f"unexpected Jarvis SDK access: {name}")


@pytest.fixture
def runtime_entry() -> MockConfigEntry:
    """Create a config entry with runtime data."""
    session = make_mobile_session("auth0|account-1")
    calls: list[str] = []
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={
            CONF_ACCOUNT_ID: "auth0|account-1",
            CONF_SESSION: session.to_dict(),
        },
    )
    entry.runtime_data = SpGroupRuntimeData(
        auth_client=object(),  # type: ignore[arg-type]
        jarvis_client=FakeJarvisClient(calls),  # type: ignore[arg-type]
        yggdrasil_client=FakeYggdrasilClient(calls),  # type: ignore[arg-type]
        profile_client=UtilityClientSentinel(),  # type: ignore[arg-type]
        session=session,
        account_id="auth0|account-1",
        refresh_lock=asyncio.Lock(),
    )
    entry.discovery_calls = calls
    return entry


async def test_coordinator_refreshes_session_before_discovery(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Coordinator invokes runtime refresh before discovery SDK calls."""
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(return_value=runtime_entry.runtime_data.session),
    ) as refresh:
        data = await coordinator._async_update_data()

    assert len(data.premises) == 1
    assert data.accounts[0].utility_types == ("ELECTRIC", "WATER")
    refresh.assert_awaited_once_with(hass, runtime_entry)
    assert runtime_entry.discovery_calls == [
        "yggdrasil.get_premises_details",
        "jarvis.get_me",
        "yggdrasil.get_meter_type:ACC-1",
        "yggdrasil.get_premise_type:ACC-1",
    ]
    assert data.electricity_candidates == ()
    assert data.water_candidates == ()


async def test_coordinator_updates_seeded_ledger_after_two_ami_sightings(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Seeded electricity sources poll AMI and accept stable buckets."""
    hass.config.config_dir = str(tmp_path)
    session = make_mobile_session("auth0|account-1")
    sensor_key = SpGroupFutureSensorKey(
        key="sp_group_premise_938f4ae4706115485aaa8512_electric",
        premise_key="sp_group_premise_938f4ae4706115485aaa8512",
        utility_type="electric",
    )
    config = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-10T01:00:00+08:00",
        backfill_from="2026-07-10T01:00:00+08:00",
        now=datetime(2026, 7, 14, 2, 0, tzinfo=SINGAPORE_TZ),
    )
    calls: list[str] = []
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={
            CONF_ACCOUNT_ID: "auth0|account-1",
            CONF_SESSION: session.to_dict(),
        },
        options={"source_config": source_options_payload({sensor_key.key: config})},
    )
    ledger_store = SpGroupLedgerStore(hass, "entry-ledger")
    await ledger_store.async_load()
    entry.runtime_data = SpGroupRuntimeData(
        auth_client=object(),  # type: ignore[arg-type]
        jarvis_client=FakeJarvisClient(calls),  # type: ignore[arg-type]
        yggdrasil_client=FakeYggdrasilClient(calls),  # type: ignore[arg-type]
        profile_client=UtilityClientSentinel(),  # type: ignore[arg-type]
        session=session,
        account_id="auth0|account-1",
        refresh_lock=asyncio.Lock(),
        ledger_store=ledger_store,
    )
    discovery = SpGroupDataUpdateCoordinator(hass, entry)  # type: ignore[arg-type]
    entry.runtime_data.coordinator = discovery
    discovery.async_set_updated_data(
        SpGroupCoordinatorData(
            premises=(
                SpGroupPremise(
                    key="sp_group_premise_938f4ae4706115485aaa8512",
                    active=True,
                    signed_up=True,
                    shared=False,
                    contestable_type=None,
                    accounts=(
                        SpGroupUtilityAccount(
                            key=sensor_key.key,
                            premise_key=sensor_key.premise_key,
                            utility_types=("ELECTRIC",),
                        ),
                    ),
                    jarvis_premise_id="JARVIS-PREMISE-1",
                ),
            )
        )
    )
    coordinator = SpGroupElectricityCoordinator(hass, entry)  # type: ignore[arg-type]
    entry.runtime_data.electricity_coordinator = coordinator

    with (
        patch(
            "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
            AsyncMock(return_value=session),
        ),
        patch(
            "custom_components.sp_group.backfill.async_schedule_statistics_backfill",
        ) as schedule_stats_backfill,
        patch(
            "custom_components.sp_group.backfill.async_run_backfill",
            new_callable=AsyncMock,
        ) as source_backfill,
    ):
        await coordinator._async_update_data()

    assert ledger_store.snapshot.entries[sensor_key.key].latest_total == Decimal(
        "101.25"
    )
    schedule_stats_backfill.assert_called_once_with(
        hass,
        entry,
        name="sp_group_poll_statistics_backfill",
        force=True,
    )
    source_backfill.assert_not_awaited()
    assert "jarvis.get_ami_charts:day:electric" in calls


async def test_normal_poll_recovers_pre_seed_lag(
    hass: HomeAssistant,
    tmp_path,
) -> None:
    """Normal polling recovers pre-seed backfill data that was lagging."""
    hass.config.config_dir = str(tmp_path)
    session = make_mobile_session("auth0|account-1")
    sensor_key = SpGroupFutureSensorKey(
        key="sp_group_premise_938f4ae4706115485aaa8512_electric",
        premise_key="sp_group_premise_938f4ae4706115485aaa8512",
        utility_type="electric",
    )
    config = build_source_config(
        sensor_key=sensor_key,
        seed_value="1000.0",
        seed_observed_at="2026-07-24T14:30:00+08:00",
        backfill_from="2026-06-01T00:00:00+08:00",
        now=datetime(2026, 7, 24, 15, 0, tzinfo=SINGAPORE_TZ),
    )
    ledger_store = SpGroupLedgerStore(hass, "entry-lag-recovery")
    await ledger_store.async_load()
    await ledger_store.async_initialize_sources({sensor_key.key: config})

    # Simulate backfill that returned data up to 13:30 only (provider lag).
    partial_bucket = SpGroupBucketObservation(
        bucket_key="2026-07-24T13:00:00+08:00",
        start=datetime(2026, 7, 24, 13, 0, tzinfo=SINGAPORE_TZ),
        end=datetime(2026, 7, 24, 13, 30, tzinfo=SINGAPORE_TZ),
        value=Decimal("1.0"),
    )
    await ledger_store.async_apply_observations(
        sensor_key.key,
        (partial_bucket,),
        now=datetime(2026, 7, 24, 13, 30, tzinfo=SINGAPORE_TZ),
        require_two_sightings=False,
        coverage=SpGroupSourceCoverage(status="covered"),
    )
    # Cursor should be at the last accepted bucket end (13:30)
    assert ledger_store.snapshot.entries[
        sensor_key.key
    ].source_poll_cursor_start() == datetime(2026, 7, 24, 13, 30, tzinfo=SINGAPORE_TZ)

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={
            CONF_ACCOUNT_ID: "auth0|account-1",
            CONF_SESSION: session.to_dict(),
        },
        options={"source_config": source_options_payload({sensor_key.key: config})},
    )
    calls: list[str] = []
    jarvis = FakeJarvisClient(calls)
    # API returns the lagged 14:00 bucket that was missing from the backfill
    jarvis.ami_payloads = [
        {
            "unit": "kWh",
            "history": [
                {
                    "data": [
                        {"date": "20260724140000", "consumption": "1.5"},
                    ]
                },
            ],
        },
    ]
    entry.runtime_data = SpGroupRuntimeData(
        auth_client=object(),
        jarvis_client=jarvis,
        yggdrasil_client=FakeYggdrasilClient(calls),
        profile_client=UtilityClientSentinel(),
        session=session,
        account_id="auth0|account-1",
        refresh_lock=asyncio.Lock(),
        ledger_store=ledger_store,
    )
    discovery = SpGroupDataUpdateCoordinator(hass, entry)
    entry.runtime_data.coordinator = discovery
    discovery.async_set_updated_data(
        SpGroupCoordinatorData(
            premises=(
                SpGroupPremise(
                    key="sp_group_premise_938f4ae4706115485aaa8512",
                    active=True,
                    signed_up=True,
                    shared=False,
                    contestable_type=None,
                    accounts=(
                        SpGroupUtilityAccount(
                            key=sensor_key.key,
                            premise_key=sensor_key.premise_key,
                            utility_types=("ELECTRIC",),
                        ),
                    ),
                    jarvis_premise_id="JARVIS-PREMISE-1",
                ),
            ),
        )
    )
    coordinator = SpGroupElectricityCoordinator(hass, entry)
    entry.runtime_data.electricity_coordinator = coordinator

    with (
        patch(
            "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
            AsyncMock(return_value=session),
        ),
        patch(
            "custom_components.sp_group.backfill.async_schedule_statistics_backfill",
        ) as schedule_stats_backfill,
    ):
        await coordinator._async_update_data()

    entry_ledger = ledger_store.snapshot.entries[sensor_key.key]
    # Normal poll recovered the lagged 14:00 bucket and accepted it
    assert any(
        b.start == datetime(2026, 7, 24, 14, 0, tzinfo=SINGAPORE_TZ)
        for b in entry_ledger.accepted
    )
    # Cursor moved past the lag gap to the new accepted end
    assert entry_ledger.source_poll_cursor_start() == datetime(
        2026, 7, 24, 14, 30, tzinfo=SINGAPORE_TZ
    )
    # Statistics retry was triggered because accepted_delta > 0
    schedule_stats_backfill.assert_called_once()


async def test_electricity_normal_poll_uses_ledger_cursor(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Normal AMI polling starts from accepted cursor with overlap."""
    hass.config.config_dir = str(tmp_path)
    session = make_mobile_session("auth0|account-1")
    sensor_key = SpGroupFutureSensorKey(
        key="sp_group_premise_938f4ae4706115485aaa8512_electric",
        premise_key="sp_group_premise_938f4ae4706115485aaa8512",
        utility_type="electric",
    )
    config = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-10T01:00:00+08:00",
        backfill_from="2026-07-10T01:00:00+08:00",
        now=datetime(2026, 7, 14, 2, 0, tzinfo=SINGAPORE_TZ),
    )
    ledger_store = SpGroupLedgerStore(hass, "entry-cursor-coordinator")
    await ledger_store.async_load()
    await ledger_store.async_initialize_sources({sensor_key.key: config})
    observation = {
        "unit": "kWh",
        "history": [
            {
                "data": [
                    {"date": "20260714150000", "consumption": "1.0"},
                    {"date": "20260714153000", "consumption": "1.0"},
                ]
            }
        ],
    }
    calls: list[str] = []
    jarvis_client = FakeJarvisClient(calls)
    jarvis_client.ami_payloads = [observation, observation, observation]
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={CONF_ACCOUNT_ID: "auth0|account-1", CONF_SESSION: session.to_dict()},
        options={"source_config": source_options_payload({sensor_key.key: config})},
    )
    entry.runtime_data = SpGroupRuntimeData(
        auth_client=object(),  # type: ignore[arg-type]
        jarvis_client=jarvis_client,  # type: ignore[arg-type]
        yggdrasil_client=FakeYggdrasilClient(calls),  # type: ignore[arg-type]
        profile_client=UtilityClientSentinel(),  # type: ignore[arg-type]
        session=session,
        account_id="auth0|account-1",
        refresh_lock=asyncio.Lock(),
        ledger_store=ledger_store,
    )
    discovery = SpGroupDataUpdateCoordinator(hass, entry)  # type: ignore[arg-type]
    entry.runtime_data.coordinator = discovery
    entry.runtime_data.discovery_coordinator = discovery
    discovery.async_set_updated_data(
        SpGroupCoordinatorData(
            premises=(
                SpGroupPremise(
                    key=sensor_key.premise_key,
                    active=True,
                    signed_up=True,
                    shared=False,
                    contestable_type=None,
                    accounts=(
                        SpGroupUtilityAccount(
                            key=sensor_key.key,
                            premise_key=sensor_key.premise_key,
                            utility_types=("ELECTRIC",),
                        ),
                    ),
                    jarvis_premise_id="JARVIS-PREMISE-1",
                ),
            )
        )
    )
    coordinator = SpGroupElectricityCoordinator(hass, entry)  # type: ignore[arg-type]
    entry.runtime_data.electricity_coordinator = coordinator

    with (
        patch(
            "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
            AsyncMock(return_value=session),
        ),
        patch(
            "custom_components.sp_group.backfill.async_schedule_statistics_backfill",
        ),
    ):
        await coordinator.async_poll_now()
        await coordinator.async_poll_now()
        await coordinator.async_poll_now()

    assert jarvis_client.ami_requests[0][0].isoformat() == "2026-07-10"
    assert jarvis_client.ami_requests[-1][0].isoformat() == "2026-07-14"


def test_water_history_observations_accepts_history_chart_water_section() -> None:
    """History chart water section parses to correct bucket observations."""
    observations = _water_history_observations(
        {
            "water": {
                "data": [
                    {
                        "period": "2026-06",
                        "status": "ACTUAL",
                        "unit": "m³",
                        "consumption": {"current": "20.0"},
                    },
                    {
                        "period": "2026-07",
                        "status": "ACTUAL",
                        "unit": "L",
                        "consumption": {"current": "21500"},
                    },
                ]
            }
        },
        backfill_from=datetime(2026, 6, 1, tzinfo=SINGAPORE_TZ),
        now=datetime(2026, 8, 5, tzinfo=SINGAPORE_TZ),
    )

    assert len(observations) == 2
    assert observations[0].bucket_key == "2026-06"
    assert observations[0].value == Decimal("20.0")
    assert observations[1].bucket_key == "2026-07"
    assert observations[1].value == Decimal("21.5")
    assert observations[0].start == datetime(2026, 6, 1, tzinfo=SINGAPORE_TZ)
    assert observations[0].end == datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ)
    assert observations[1].start == datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ)
    assert observations[1].end == datetime(2026, 8, 1, tzinfo=SINGAPORE_TZ)


async def test_water_startup_catchup_needed_after_configured_time(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Startup catch-up runs when a ready month is missing after poll time."""
    hass.config.config_dir = str(tmp_path)
    session = make_mobile_session("auth0|account-1")
    sensor_key = SpGroupFutureSensorKey(
        key="sp_group_premise_938f4ae4706115485aaa8512_water",
        premise_key="sp_group_premise_938f4ae4706115485aaa8512",
        utility_type="water",
    )
    config = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-06-01T00:00:00+08:00",
        backfill_from="2026-06-01T00:00:00+08:00",
        now=datetime(2026, 7, 10, tzinfo=SINGAPORE_TZ),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={CONF_ACCOUNT_ID: "auth0|account-1", CONF_SESSION: session.to_dict()},
        options={
            "source_config": {
                sensor_key.key: {
                    **source_options_payload({sensor_key.key: config})[sensor_key.key],
                    "source": SOURCE_MANUAL_SEED_MONTHLY,
                }
            }
        },
    )
    ledger_store = SpGroupLedgerStore(hass, "entry-water-catchup")
    await ledger_store.async_load()
    await ledger_store.async_initialize_sources({sensor_key.key: config})
    entry.runtime_data = SpGroupRuntimeData(
        auth_client=object(),  # type: ignore[arg-type]
        jarvis_client=FakeJarvisClient([]),  # type: ignore[arg-type]
        yggdrasil_client=FakeYggdrasilClient([]),  # type: ignore[arg-type]
        profile_client=UtilityClientSentinel(),  # type: ignore[arg-type]
        session=session,
        account_id="auth0|account-1",
        refresh_lock=asyncio.Lock(),
        ledger_store=ledger_store,
    )
    discovery = SpGroupDataUpdateCoordinator(hass, entry)  # type: ignore[arg-type]
    entry.runtime_data.discovery_coordinator = discovery
    discovery.async_set_updated_data(
        SpGroupCoordinatorData(
            premises=(
                SpGroupPremise(
                    key=sensor_key.premise_key,
                    active=True,
                    signed_up=True,
                    shared=False,
                    contestable_type=None,
                    accounts=(
                        SpGroupUtilityAccount(
                            key=sensor_key.key,
                            premise_key=sensor_key.premise_key,
                            utility_types=("WATER",),
                        ),
                    ),
                    jarvis_premise_id="JARVIS-PREMISE-1",
                ),
            )
        )
    )

    assert _water_startup_catchup_needed(
        entry, datetime(2026, 7, 5, 1, 0, tzinfo=SINGAPORE_TZ)
    )
    assert not _water_startup_catchup_needed(
        entry, datetime(2026, 7, 1, 0, 0, tzinfo=SINGAPORE_TZ)
    )


async def test_water_startup_catchup_schedules_one_refresh_task(
    hass: HomeAssistant,
) -> None:
    """Startup catch-up schedules exactly one active refresh task."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.runtime_data = SimpleNamespace(backfill_tasks=set())
    coordinator = SpGroupWaterCoordinator(hass, entry)  # type: ignore[arg-type]
    release = asyncio.Event()
    coordinator.async_request_refresh = AsyncMock(  # type: ignore[method-assign]
        side_effect=release.wait
    )

    with patch(
        "custom_components.sp_group.coordinator._water_startup_catchup_needed",
        return_value=True,
    ):
        coordinator.async_start_startup_catchup()
        coordinator.async_start_startup_catchup()
        await asyncio.sleep(0)

    assert len(entry.runtime_data.backfill_tasks) == 1
    coordinator.async_request_refresh.assert_awaited_once()
    release.set()
    await hass.async_block_till_done()
    assert entry.runtime_data.backfill_tasks == set()


async def test_water_startup_catchup_cancel_stops_pending_task(
    hass: HomeAssistant,
) -> None:
    """Canceling the water schedule also cancels startup catch-up."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.runtime_data = SimpleNamespace(backfill_tasks=set())
    coordinator = SpGroupWaterCoordinator(hass, entry)  # type: ignore[arg-type]
    release = asyncio.Event()
    coordinator.async_request_refresh = AsyncMock(  # type: ignore[method-assign]
        side_effect=release.wait
    )
    unsub_timer = Mock()
    coordinator._unsub_timer = unsub_timer

    with patch(
        "custom_components.sp_group.coordinator._water_startup_catchup_needed",
        return_value=True,
    ):
        coordinator.async_start_startup_catchup()
        await asyncio.sleep(0)

    coordinator.async_cancel_schedule()
    await hass.async_block_till_done()

    unsub_timer.assert_called_once()
    assert coordinator._startup_catchup_task is None
    assert entry.runtime_data.backfill_tasks == set()


def test_water_startup_catchup_skips_when_not_needed(
    hass: HomeAssistant,
) -> None:
    """Starting before the catch-up window does not schedule work."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.runtime_data = SimpleNamespace(backfill_tasks=set())
    coordinator = SpGroupWaterCoordinator(hass, entry)  # type: ignore[arg-type]

    with patch(
        "custom_components.sp_group.coordinator._water_startup_catchup_needed",
        return_value=False,
    ):
        coordinator.async_start_startup_catchup()

    assert entry.runtime_data.backfill_tasks == set()


def test_water_history_observations_empty_payload() -> None:
    """Empty or missing water sections return empty observations."""
    assert (
        _water_history_observations(
            {},
            backfill_from=datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
            now=datetime(2026, 8, 5, tzinfo=SINGAPORE_TZ),
        )
        == ()
    )
    assert (
        _water_history_observations(
            {"water": {}},
            backfill_from=datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
            now=datetime(2026, 8, 5, tzinfo=SINGAPORE_TZ),
        )
        == ()
    )


def test_water_history_observations_grace_period() -> None:
    """Current month observations are excluded by the closed-month grace."""
    observations = _water_history_observations(
        {
            "water": {
                "data": [
                    {
                        "period": "2026-07",
                        "unit": "m³",
                        "consumption": {"current": "5.0"},
                    }
                ]
            }
        },
        backfill_from=datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
        now=datetime(2026, 7, 2, tzinfo=SINGAPORE_TZ),
    )
    assert observations == ()


def test_water_history_observations_backfill_filter() -> None:
    """Periods before backfill_from are excluded."""
    observations = _water_history_observations(
        {
            "water": {
                "data": [
                    {
                        "period": "2026-01",
                        "unit": "m³",
                        "consumption": {"current": "10.0"},
                    },
                    {
                        "period": "2026-02",
                        "unit": "m³",
                        "consumption": {"current": "15.0"},
                    },
                ]
            }
        },
        backfill_from=datetime(2026, 2, 1, tzinfo=SINGAPORE_TZ),
        now=datetime(2026, 3, 10, tzinfo=SINGAPORE_TZ),
    )
    assert len(observations) == 1
    assert observations[0].bucket_key == "2026-02"


def test_water_history_observations_malformed_rows() -> None:
    """Malformed rows (missing period, negative, no consumption) are skipped."""
    observations = _water_history_observations(
        {
            "water": {
                "data": [
                    {"unit": "m³", "consumption": {"current": "1.0"}},
                    {"period": "2026-06", "unit": "m³", "consumption": {}},
                    {"period": "2026-07", "unit": "m³", "consumption": {"current": -1}},
                    {
                        "period": "2026-08",
                        "unit": "kWh",
                        "consumption": {"current": "5.0"},
                    },
                    "not-a-dict",
                ]
            }
        },
        backfill_from=datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
        now=datetime(2026, 9, 5, tzinfo=SINGAPORE_TZ),
    )
    assert observations == ()


def test_electricity_history_fetch_decision_distinguishes_coverage() -> None:
    """Backfill fetches history for absent/partial coverage, not full AMI."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    full_payload = {
        "history": [
            {
                "data": [
                    {
                        "date": slot.strftime("%Y%m%d%H%M%S"),
                        "consumption": "1",
                    }
                    for slot in slots
                ]
            }
        ]
    }
    assert not _electricity_ami_needs_history(
        full_payload,
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
        backfill=False,
    )
    assert _electricity_ami_needs_history(
        {"history": []},
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
        backfill=False,
    )
    assert not _electricity_ami_needs_history(
        full_payload,
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
        backfill=True,
    )


async def test_electricity_async_parse_full_ami_does_not_fetch_history(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """The runtime backfill path honours full AMI precedence."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    jarvis = runtime_entry.runtime_data.jarvis_client
    assert isinstance(jarvis, FakeJarvisClient)
    jarvis.ami_payloads = [
        {
            "unit": "kWh",
            "history": [
                {
                    "data": [
                        {"date": slot.strftime("%Y%m%d%H%M%S"), "consumption": "1"}
                        for slot in slots
                    ]
                }
            ],
        }
    ]
    coordinator = SpGroupElectricityCoordinator(hass, runtime_entry)
    parsed = await coordinator._async_parse_source(
        jarvis.ami_payloads.pop(),
        session=runtime_entry.runtime_data.session,
        premise_id="JARVIS-PREMISE-1",
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
        backfill=True,
    )
    assert len(parsed.observations) == len(slots)
    assert not any(
        call.startswith("jarvis.get_history_charts") for call in jarvis.calls
    )


async def test_electricity_async_parse_partial_fetches_and_caches_history(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Partial AMI composition fetches History once for a premise."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    jarvis = runtime_entry.runtime_data.jarvis_client
    assert isinstance(jarvis, FakeJarvisClient)
    jarvis.chart_payload = {
        "elec": {
            "data": [
                {
                    "period": "2026-01",
                    "status": "Actual",
                    "unit": "kWh",
                    "consumption": {"current": "3"},
                }
            ]
        }
    }
    payload = {
        "unit": "kWh",
        "history": [{"data": [{"date": "20260101010000", "consumption": "1"}]}],
    }
    coordinator = SpGroupElectricityCoordinator(hass, runtime_entry)
    for _ in range(2):
        parsed = await coordinator._async_parse_source(
            payload,
            session=runtime_entry.runtime_data.session,
            premise_id="JARVIS-PREMISE-1",
            backfill_from=month,
            now=datetime(2026, 2, 2, 0, 0, tzinfo=SINGAPORE_TZ),
            backfill=False,
        )
        assert parsed.observations
    history_calls = [
        call
        for call in jarvis.calls
        if call == "jarvis.get_history_charts:JARVIS-PREMISE-1"
    ]
    assert len(history_calls) == 1


async def test_electricity_async_parse_zero_only_uses_history_monthly(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Zero-only closed AMI coverage does not suppress History."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(48))
    jarvis = runtime_entry.runtime_data.jarvis_client
    assert isinstance(jarvis, FakeJarvisClient)
    jarvis.chart_payload = {
        "elec": {
            "data": [
                {
                    "period": "2026-01",
                    "status": "Actual",
                    "unit": "kWh",
                    "consumption": {"current": "12"},
                }
            ]
        }
    }
    coordinator = SpGroupElectricityCoordinator(hass, runtime_entry)
    parsed = await coordinator._async_parse_source(
        {
            "unit": "kWh",
            "history": [
                {
                    "data": [
                        {
                            "date": slot.strftime("%Y%m%d%H%M%S"),
                            "consumption": "0",
                        }
                        for slot in slots
                    ]
                }
            ],
        },
        session=runtime_entry.runtime_data.session,
        premise_id="JARVIS-PREMISE-1",
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
        backfill=True,
    )
    assert [(item.value, item.provenance) for item in parsed.observations] == [
        (Decimal("12"), "history_monthly")
    ]
    assert "jarvis.get_history_charts:JARVIS-PREMISE-1" in jarvis.calls


async def test_electricity_async_parse_rejects_malformed_history(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Malformed History evidence fails before ledger application."""
    jarvis = runtime_entry.runtime_data.jarvis_client
    assert isinstance(jarvis, FakeJarvisClient)
    jarvis.chart_payload = {
        "elec": {
            "data": [
                {
                    "period": "2026-01",
                    "status": "Actual",
                    "unit": "MWh",
                    "consumption": {"current": "1"},
                }
            ]
        }
    }
    coordinator = SpGroupElectricityCoordinator(hass, runtime_entry)
    with pytest.raises(LedgerError, match="history_invalid_period_or_unit"):
        await coordinator._async_parse_source(
            {
                "unit": "kWh",
                "history": [{"data": [{"date": "20260101010000", "consumption": "1"}]}],
            },
            session=runtime_entry.runtime_data.session,
            premise_id="JARVIS-PREMISE-1",
            backfill_from=datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
            now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
            backfill=False,
        )


async def test_electricity_async_parse_rejects_duplicate_ami_timestamp(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Duplicate AMI timestamps are source failures, not gap evidence."""
    jarvis = runtime_entry.runtime_data.jarvis_client
    assert isinstance(jarvis, FakeJarvisClient)
    coordinator = SpGroupElectricityCoordinator(hass, runtime_entry)
    payload = {
        "unit": "kWh",
        "history": [
            {
                "data": [
                    {"date": "20260101010000", "consumption": "1"},
                    {"date": "20260101010000", "consumption": "1"},
                ]
            }
        ],
    }
    with pytest.raises(LedgerError, match="duplicate_electricity_ami_timestamp"):
        await coordinator._async_parse_source(
            payload,
            session=runtime_entry.runtime_data.session,
            premise_id="JARVIS-PREMISE-1",
            backfill_from=datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
            now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
            backfill=False,
        )


async def test_water_coordinator_calls_get_history_charts(
    hass: HomeAssistant,
    tmp_path: object,
) -> None:
    """Water coordinator override calls get_history_charts, not get_ami_charts."""
    from .test_recorder_statistics import _setup_seeded_entry, _water_history_payload

    entry = await _setup_seeded_entry(hass, tmp_path)
    water_coordinator = entry.runtime_data.water_coordinator
    assert water_coordinator is not None
    jarvis = entry.runtime_data.jarvis_client

    jarvis.chart_payload = _water_history_payload("5.0")  # type: ignore[attr-defined]

    with (
        patch(
            "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
            AsyncMock(return_value=entry.runtime_data.session),
        ),
        patch(
            "custom_components.sp_group.backfill.async_schedule_statistics_backfill",
        ) as schedule_stats_backfill,
        patch(
            "custom_components.sp_group.backfill.async_run_backfill",
            new_callable=AsyncMock,
        ) as source_backfill,
    ):
        await water_coordinator.async_poll_now()
        await hass.async_block_till_done()

    assert isinstance(jarvis, FakeJarvisClient)
    assert any("get_history_charts" in call for call in jarvis.calls)
    assert not any("get_ami_charts" in call for call in jarvis.calls)
    schedule_stats_backfill.assert_called_once_with(
        hass,
        entry,
        name="sp_group_poll_statistics_backfill",
        force=True,
    )
    source_backfill.assert_not_awaited()


async def test_coordinator_debug_logs_are_phase_summaries(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Coordinator debug logs contain phases and counts, not raw identifiers."""
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator
    caplog.set_level(logging.DEBUG, logger="custom_components.sp_group")

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(return_value=runtime_entry.runtime_data.session),
    ):
        await coordinator._async_update_data()

    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "auth refresh phase completed" in rendered_logs
    assert "discovery phase completed: account_count=1" in rendered_logs
    assert "normalization phase completed: premise_count=1" in rendered_logs
    assert "chart fetch phase completed" not in rendered_logs
    assert "utility normalization phase completed" not in rendered_logs
    for sensitive in (
        "ACC-1",
        "ACC-ROOT-1",
        "PREM-1",
        "JARVIS-PREMISE-1",
        "access-token",
        "id-token",
        "refresh-token",
    ):
        assert sensitive not in rendered_logs


async def test_coordinator_propagates_auth_failure(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Runtime auth failures propagate so HA can start reauth."""
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(side_effect=ConfigEntryAuthFailed("reauth")),
    ):
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()


@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        (
            SpGroupTransportError("network timeout ACC-SECRET"),
            "Error refreshing SP Group discovery data",
        ),
        (
            SpGroupApiError("api unavailable PREM-SECRET", status=503),
            "Error refreshing SP Group discovery data",
        ),
        (
            SpGroupMobileError("mobile sdk failure JARVIS-SECRET"),
            "Error refreshing SP Group discovery data",
        ),
        (TimeoutError(), "Timed out refreshing SP Group data"),
    ],
)
async def test_coordinator_maps_transient_failures_to_update_failed(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
    error: Exception,
    expected_message: str,
) -> None:
    """Non-auth SDK and timeout failures become HA update failures."""
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(side_effect=error),
    ):
        with pytest.raises(UpdateFailed) as err:
            await coordinator._async_update_data()

    assert str(err.value) == expected_message
    assert "ACC-SECRET" not in str(err.value)
    assert "PREM-SECRET" not in str(err.value)
    assert "JARVIS-SECRET" not in str(err.value)


async def test_repeated_transient_failures_do_not_add_integration_log_spam(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated failures do not create repeated unavailable logs from this code."""
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator
    caplog.set_level(logging.INFO, logger="custom_components.sp_group")

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(side_effect=SpGroupTransportError("temporary outage ACC-SECRET")),
    ):
        await coordinator.async_refresh()
        await coordinator.async_refresh()

    integration_unavailable_logs = [
        record
        for record in caplog.records
        if record.name.startswith("custom_components.sp_group")
        and "unavailable" in record.getMessage().lower()
    ]
    assert integration_unavailable_logs == []
    assert "ACC-SECRET" not in "\n".join(
        record.getMessage() for record in caplog.records
    )


async def test_coordinator_rejects_unmatched_jarvis_discovery(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Coordinator fails transiently if Jarvis cannot correlate future charts."""
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator
    runtime_entry.runtime_data.jarvis_client = FakeJarvisClient(
        runtime_entry.discovery_calls,
        account_number="OTHER-ACCOUNT",
    )  # type: ignore[assignment]

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(return_value=runtime_entry.runtime_data.session),
    ):
        with pytest.raises(
            UpdateFailed,
            match="Error refreshing SP Group discovery data",
        ):
            await coordinator._async_update_data()


async def test_coordinator_fetches_history_charts_for_water_only_premises(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Water-only discovery fetches chart data for water evidence only."""
    yggdrasil_water = {
        "data": {
            "premiseDetails": {
                "premises": [
                    {
                        "accountNo": "ACC-ROOT-1",
                        "accounts": [
                            {
                                "accountNo": "ACC-1",
                                "premiseNo": "PREM-1",
                                "activeServices": ["WATER"],
                            }
                        ],
                    }
                ]
            }
        }
    }
    jarvis_water = {
        "premises": [
            {
                "id": "JARVIS-PREMISE-1",
                "accounts": [
                    {
                        "account_number": "ACC-1",
                        "premise_number": "PREM-1",
                        "utilities": ["WATER"],
                    }
                ],
            }
        ]
    }
    runtime_entry.runtime_data.yggdrasil_client = FakeYggdrasilClient(
        runtime_entry.discovery_calls,
        premises_payload=yggdrasil_water,
    )  # type: ignore[assignment]
    runtime_entry.runtime_data.jarvis_client = FakeJarvisClient(
        runtime_entry.discovery_calls,
        me_payload=jarvis_water,
    )  # type: ignore[assignment]
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(return_value=runtime_entry.runtime_data.session),
    ):
        data = await coordinator._async_update_data()

    assert data.electricity_candidates == ()
    assert data.electricity_warnings == ()
    assert data.water_candidates == ()
    assert data.water_warnings == ()
    assert runtime_entry.discovery_calls == [
        "yggdrasil.get_premises_details",
        "jarvis.get_me",
        "yggdrasil.get_meter_type:ACC-1",
        "yggdrasil.get_premise_type:ACC-1",
    ]


async def test_coordinator_fetches_one_chart_for_multiple_water_accounts(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Multiple water accounts on one premise do not duplicate chart requests."""
    yggdrasil_water = {
        "data": {
            "premiseDetails": {
                "premises": [
                    {
                        "accountNo": "ACC-ROOT-1",
                        "accounts": [
                            {
                                "accountNo": "ACC-1",
                                "premiseNo": "PREM-1",
                                "activeServices": ["WATER"],
                            },
                            {
                                "accountNo": "ACC-2",
                                "premiseNo": "PREM-1",
                                "activeServices": ["WATER"],
                            },
                        ],
                    }
                ]
            }
        }
    }
    jarvis_water = {
        "premises": [
            {
                "id": "JARVIS-PREMISE-1",
                "accounts": [
                    {
                        "account_number": "ACC-1",
                        "premise_number": "PREM-1",
                        "utilities": ["WATER"],
                    },
                    {
                        "account_number": "ACC-2",
                        "premise_number": "PREM-1",
                        "utilities": ["WATER"],
                    },
                ],
            }
        ]
    }
    runtime_entry.runtime_data.yggdrasil_client = FakeYggdrasilClient(
        runtime_entry.discovery_calls,
        premises_payload=yggdrasil_water,
    )  # type: ignore[assignment]
    runtime_entry.runtime_data.jarvis_client = FakeJarvisClient(
        runtime_entry.discovery_calls,
        me_payload=jarvis_water,
    )  # type: ignore[assignment]
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(return_value=runtime_entry.runtime_data.session),
    ):
        data = await coordinator._async_update_data()

    assert data.water_candidates == ()
    assert (
        runtime_entry.discovery_calls.count(
            "jarvis.get_history_charts:JARVIS-PREMISE-1"
        )
        == 0
    )


async def test_coordinator_maps_history_chart_failure_to_update_failed(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """History chart failures remain inside the coordinator transient boundary."""
    runtime_entry.runtime_data.jarvis_client = FakeJarvisClient(
        runtime_entry.discovery_calls,
        chart_error=SpGroupTransportError("chart outage"),
    )  # type: ignore[assignment]
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(return_value=runtime_entry.runtime_data.session),
    ):
        await coordinator._async_update_data()

    assert (
        "jarvis.get_history_charts:JARVIS-PREMISE-1"
        not in runtime_entry.discovery_calls
    )


async def test_coordinator_maps_ledger_failure_to_sanitized_update_failed(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Ledger persistence failures remain inside the coordinator boundary."""

    class FailingLedgerStore:
        async def async_initialize_sources(self, configs: object) -> object:
            raise OSError("disk full ACC-SECRET")

    runtime_entry.runtime_data.ledger_store = FailingLedgerStore()  # type: ignore[assignment]
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(return_value=runtime_entry.runtime_data.session),
    ):
        await coordinator._async_update_data()

    assert runtime_entry.runtime_data.ledger_store is not None


async def test_coordinator_keeps_malformed_chart_evidence_non_publishable(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Malformed successful chart payloads produce warnings, not sensor values."""
    runtime_entry.runtime_data.jarvis_client = FakeJarvisClient(
        runtime_entry.discovery_calls,
        chart_payload={
            "elec": {
                "data": [
                    {
                        "period": "2026-06",
                        "unit": "MWh",
                        "consumption": {"current": "bad"},
                    }
                ]
            }
        },
    )  # type: ignore[assignment]
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(return_value=runtime_entry.runtime_data.session),
    ):
        data = await coordinator._async_update_data()

    assert data.electricity_warnings == ()
    assert data.water_warnings == ()
    assert data.electricity_candidates == ()
    assert data.water_candidates == ()


async def test_coordinator_keeps_malformed_water_chart_evidence_non_publishable(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Malformed successful water chart payloads produce bounded warnings."""
    runtime_entry.runtime_data.jarvis_client = FakeJarvisClient(
        runtime_entry.discovery_calls,
        chart_payload={
            "elec": {"data": []},
            "water": {
                "data": [
                    {
                        "period": "2026-06",
                        "unit": "gal",
                        "consumption": {"current": "bad"},
                    }
                ]
            },
        },
    )  # type: ignore[assignment]
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(return_value=runtime_entry.runtime_data.session),
    ):
        data = await coordinator._async_update_data()

    assert data.electricity_candidates == ()
    assert data.electricity_warnings == ()
    assert data.water_warnings == ()
    assert data.water_candidates == ()


async def test_coordinator_replaces_electricity_candidates_on_refresh(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """Repeated coordinator refreshes replace parsed candidates instead of appending."""
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(return_value=runtime_entry.runtime_data.session),
    ):
        await coordinator.async_refresh()
        assert coordinator.data is not None
        assert coordinator.data.electricity_candidates == ()
        assert coordinator.data.water_candidates == ()

        await coordinator.async_refresh()
        assert coordinator.data is not None
        assert coordinator.data.electricity_candidates == ()
        assert coordinator.data.water_candidates == ()


async def test_coordinator_preserves_data_after_transient_refresh_failure(
    hass: HomeAssistant,
    runtime_entry: MockConfigEntry,
) -> None:
    """DataUpdateCoordinator keeps previous data after transient failures."""
    coordinator = SpGroupDataUpdateCoordinator(hass, runtime_entry)  # type: ignore[arg-type]
    runtime_entry.runtime_data.coordinator = coordinator
    existing = SpGroupCoordinatorData(has_partial_errors=True)
    coordinator.data = existing
    coordinator.last_update_success = True

    with patch(
        "custom_components.sp_group.coordinator.async_refresh_runtime_session_if_needed",
        AsyncMock(side_effect=SpGroupTransportError("temporary outage")),
    ):
        await coordinator.async_refresh()

    assert coordinator.data == existing
    assert coordinator.last_update_success is False


def test_ami_rows_parses_valid_response() -> None:
    """_ami_rows extracts all valid rows from a standard AMI response."""
    payload = {
        "unit": "kWh",
        "grouped_by": "day",
        "history": [
            {
                "date": "20260714",
                "data": [
                    {"date": "20260714010000", "consumption": "1.25"},
                    {"date": "20260714020000", "consumption": "0.75"},
                ],
            },
            {
                "date": "20260715",
                "data": [
                    {"date": "20260715010000", "consumption": "1.50"},
                ],
            },
        ],
    }
    rows = _ami_rows(payload)
    assert len(rows) == 3
    assert rows[0]["date"] == "20260714010000"
    assert rows[1]["date"] == "20260714020000"
    assert rows[2]["date"] == "20260715010000"


def test_ami_rows_empty_history() -> None:
    """_ami_rows returns [] when history is empty."""
    rows = _ami_rows({"unit": "kWh", "history": []})
    assert rows == []


def test_ami_rows_missing_history() -> None:
    """_ami_rows returns [] when history key is absent."""
    rows = _ami_rows({"unit": "kWh"})
    assert rows == []


def test_ami_rows_non_list_history() -> None:
    """_ami_rows returns [] when history is not a list."""
    rows = _ami_rows({"unit": "kWh", "history": "invalid"})
    assert rows == []


def test_ami_rows_skips_non_dict_group() -> None:
    """_ami_rows skips groups that are not dicts."""
    payload = {
        "unit": "kWh",
        "history": [
            "not-a-dict",
            {"data": [{"date": "20260714010000", "consumption": "1.25"}]},
        ],
    }
    rows = _ami_rows(payload)
    assert len(rows) == 1


def test_ami_rows_skips_missing_data_in_group() -> None:
    """_ami_rows skips groups with missing or non-list data."""
    payload = {
        "unit": "kWh",
        "history": [
            {"date": "20260714"},  # no 'data' key
            {"data": "not-a-list"},
            {"data": [{"date": "20260714010000", "consumption": "1.25"}]},
        ],
    }
    rows = _ami_rows(payload)
    assert len(rows) == 1


def test_ami_rows_skips_row_missing_date() -> None:
    """_ami_rows skips rows without a valid date field."""
    payload = {
        "unit": "kWh",
        "history": [
            {
                "data": [
                    {"consumption": "1.25"},  # no date
                    {"date": "20260714010000", "consumption": "2.50"},
                ]
            }
        ],
    }
    rows = _ami_rows(payload)
    assert len(rows) == 1
    assert rows[0]["date"] == "20260714010000"


def test_ami_rows_skips_row_missing_consumption_and_amount() -> None:
    """_ami_rows skips rows without consumption or amount fields."""
    payload = {
        "unit": "kWh",
        "history": [
            {
                "data": [
                    {"date": "20260714010000"},  # no consumption/amount
                    {"date": "20260714020000", "amount": "33.0"},
                ]
            }
        ],
    }
    rows = _ami_rows(payload)
    assert len(rows) == 1
    assert rows[0]["amount"] == "33.0"


def test_ami_rows_uses_amount_as_fallback() -> None:
    """_ami_rows falls back to 'amount' when 'consumption' is absent."""
    payload = {
        "unit": "kWh",
        "history": [
            {
                "data": [
                    {"date": "20260714010000", "amount": "42.0"},
                ]
            }
        ],
    }
    rows = _ami_rows(payload)
    assert len(rows) == 1
    assert rows[0]["amount"] == "42.0"


def test_electricity_source_poll_persists_zeros_and_fills_internal_gap() -> None:
    """Explicit and inferred zero intervals remain accepted coverage."""
    now = datetime(2026, 7, 15, 15, 0, tzinfo=SINGAPORE_TZ)
    parsed = _electricity_ami_source_poll(
        {
            "unit": "kWh",
            "history": [
                {
                    "data": [
                        {"date": "20260715000000", "consumption": "0"},
                        {"date": "20260715130000", "consumption": "1.0"},
                        {"date": "20260715133000", "consumption": "0"},
                        {"date": "20260715143000", "consumption": "2.0"},
                    ]
                }
            ],
        },
        backfill_from=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        now=now,
    )
    assert [item.value for item in parsed.observations] == [
        Decimal("1.0"),
        Decimal("0"),
        Decimal("0"),
        Decimal("2.0"),
    ]
    assert parsed.coverage.status == "covered"
    assert parsed.coverage.unresolved_ranges == ()


def test_electricity_source_poll_with_only_zero_rows_is_lagging() -> None:
    """A zero-only AMI response cannot advance the ledger."""
    parsed = _electricity_ami_source_poll(
        {
            "unit": "kWh",
            "history": [{"data": [{"date": "20260715130000", "consumption": "0"}]}],
        },
        backfill_from=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    assert parsed.observations == ()
    assert parsed.coverage.status == "lagging"


def test_electricity_source_poll_treats_complete_zero_grid_as_unavailable() -> None:
    """A complete all-zero AMI month does not establish coverage."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    parsed = _electricity_ami_source_poll(
        {
            "unit": "kWh",
            "history": [
                {
                    "data": [
                        {
                            "date": slot.strftime("%Y%m%d%H%M%S"),
                            "consumption": "0",
                        }
                        for slot in slots
                    ]
                }
            ],
        },
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
    )
    assert parsed.observations == ()
    assert parsed.coverage.status == "lagging"
    assert parsed.coverage.unresolved_ranges == ()


def test_electricity_source_poll_ignores_future_and_in_progress_rows() -> None:
    """Only completed source buckets establish AMI coverage."""
    now = datetime(2026, 7, 15, 14, 15, tzinfo=SINGAPORE_TZ)
    parsed = _electricity_ami_source_poll(
        {
            "unit": "kWh",
            "history": [
                {
                    "data": [
                        {"date": "20260715130000", "consumption": "1.0"},
                        {"date": "20260715140000", "consumption": "2.0"},
                        {"date": "20260715143000", "consumption": "3.0"},
                    ]
                }
            ],
        },
        backfill_from=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        now=now,
    )
    assert [item.start for item in parsed.observations] == [
        datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ)
    ]
    assert parsed.coverage.start == datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ)
    assert parsed.coverage.end == datetime(2026, 7, 15, 13, 30, tzinfo=SINGAPORE_TZ)


async def test_source_poll_retains_last_known_coverage_when_provider_lags(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """A lagging response changes status without erasing coverage boundaries."""
    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-coverage-lag")
    config = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_electric",
            premise_key="opaque_premise",
            utility_type="electric",
        ),
        seed_value="100.0",
        seed_observed_at="2026-07-15T12:00:00+08:00",
        backfill_from="2026-07-15T12:00:00+08:00",
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})
    await store.async_apply_observations(
        "opaque_electric",
        (),
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=False,
        coverage=SpGroupSourceCoverage(
            status="covered",
            start=datetime(2026, 7, 15, 12, 0, tzinfo=SINGAPORE_TZ),
            end=datetime(2026, 7, 15, 12, 30, tzinfo=SINGAPORE_TZ),
        ),
    )
    snapshot = await store.async_apply_observations(
        "opaque_electric",
        (),
        now=datetime(2026, 7, 15, 15, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=False,
        coverage=SpGroupSourceCoverage(status="lagging"),
    )
    entry = snapshot.entries["opaque_electric"]
    assert entry.source_coverage_status == "lagging"
    assert entry.source_coverage_start == datetime(
        2026, 7, 15, 12, 0, tzinfo=SINGAPORE_TZ
    )
    assert entry.source_coverage_end == datetime(
        2026, 7, 15, 12, 30, tzinfo=SINGAPORE_TZ
    )


def test_water_source_poll_detects_missing_closed_month_between_rows() -> None:
    """Water history uses the same internal-gap model at month cadence."""
    parsed = _water_history_source_poll(
        {
            "water": {
                "data": [
                    {
                        "period": "2026-01",
                        "unit": "m3",
                        "consumption": {"current": "1.0"},
                    },
                    {
                        "period": "2026-03",
                        "unit": "m3",
                        "consumption": {"current": "2.0"},
                    },
                ]
            }
        },
        backfill_from=datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
        now=datetime(2026, 5, 10, tzinfo=SINGAPORE_TZ),
    )
    assert [item.start for item in parsed.coverage.unresolved_ranges] == [
        datetime(2026, 2, 1, tzinfo=SINGAPORE_TZ)
    ]


def test_water_startup_gap_scan_includes_older_missing_months() -> None:
    """Startup recovery does not stop when the latest month is present."""
    entry = SimpleNamespace(
        seed_effective_from=datetime(2026, 4, 1, tzinfo=SINGAPORE_TZ),
        accepted=(
            SimpleNamespace(
                start=datetime(2026, 6, 1, tzinfo=SINGAPORE_TZ),
            ),
        ),
    )
    assert _missing_closed_water_months(
        entry,
        datetime(2026, 6, 1, tzinfo=SINGAPORE_TZ),
    ) == (
        datetime(2026, 4, 1, tzinfo=SINGAPORE_TZ),
        datetime(2026, 5, 1, tzinfo=SINGAPORE_TZ),
    )


def test_water_repeated_full_history_is_idempotent() -> None:
    """A repeated water history response produces identical observations."""
    payload = {
        "water": {
            "data": [
                {
                    "period": "2026-01",
                    "unit": "m3",
                    "consumption": {"current": "1.0"},
                },
                {
                    "period": "2026-02",
                    "unit": "m3",
                    "consumption": {"current": "2.0"},
                },
            ]
        }
    }
    first = _water_history_source_poll(
        payload,
        backfill_from=datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
        now=datetime(2026, 3, 10, tzinfo=SINGAPORE_TZ),
    )
    second = _water_history_source_poll(
        payload,
        backfill_from=datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
        now=datetime(2026, 3, 10, tzinfo=SINGAPORE_TZ),
    )
    assert len(first.observations) == len(second.observations)
    assert [o.bucket_key for o in first.observations] == [
        o.bucket_key for o in second.observations
    ]
    assert [o.value for o in first.observations] == [
        o.value for o in second.observations
    ]
    assert first.coverage == second.coverage


def test_electricity_source_poll_malformed_response_at_coordinator() -> None:
    """A malformed AMI response produces zero usable observations."""
    now = datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ)
    parsed = _electricity_ami_source_poll(
        {"unit": "kWh", "history": "not_a_list"},
        backfill_from=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        now=now,
    )
    assert parsed.observations == ()
    assert parsed.coverage.status == "malformed"
