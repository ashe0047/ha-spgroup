"""Test SP Group integration setup boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from custom_components.sp_group import (
    MALFORMED_LEDGER_REPAIR_ISSUE,
    SpGroupRuntimeData,
    async_migrate_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.sp_group.backfill import (
    StatisticsBackfillScheduleStatus,
    async_schedule_statistics_backfill,
)
from custom_components.sp_group.const import (
    CONF_ACCOUNT_ID,
    CONF_SESSION,
    CONF_SOURCE_CONFIG,
    DOMAIN,
)
from homeassistant.components.recorder.const import (
    DOMAIN as RECORDER_DOMAIN,
)
from homeassistant.components.recorder.const import (
    EVENT_RECORDER_HOURLY_STATISTICS_GENERATED,
)
from homeassistant.const import Platform
from homeassistant.core import HassJob, HassJobType, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .helpers import make_mobile_session


class FakeSdkClient:
    """Record the aiohttp session passed to an SDK client constructor."""

    def __init__(self, session: object) -> None:
        """Initialize the fake client."""
        self.session = session


class FakeCoordinator:
    """Fake coordinator for setup lifecycle tests."""

    side_effect: Exception | None = None
    calls: list[str] = []

    def __init__(self, hass: object, entry: object) -> None:
        """Initialize the fake coordinator."""
        self.hass = hass
        self.entry = entry
        FakeCoordinator.calls.append("create")

    async def async_config_entry_first_refresh(self) -> None:
        """Record first-refresh behavior."""
        FakeCoordinator.calls.append("first_refresh")
        if FakeCoordinator.side_effect is not None:
            if isinstance(FakeCoordinator.side_effect, UpdateFailed):
                raise ConfigEntryNotReady() from FakeCoordinator.side_effect
            raise FakeCoordinator.side_effect


class FakeLedgerStore:
    """Fake ledger store for setup lifecycle tests."""

    calls: list[str] = []

    def __init__(self, hass: object, entry_id: str) -> None:
        """Initialize the fake ledger store."""
        self.hass = hass
        self.entry_id = entry_id
        self.snapshot = SimpleNamespace(malformed=False)
        FakeLedgerStore.calls.append("create_ledger")

    async def async_load(self) -> object:
        """Record ledger loading."""
        FakeLedgerStore.calls.append("load_ledger")
        return object()


class FakeSessionStore:
    """In-memory HA Store substitute for setup session tests."""

    load_data: object = None
    instances: list[FakeSessionStore] = []

    def __init__(self, hass: object, version: int, key: str) -> None:
        """Initialize the fake Store."""
        self.hass = hass
        self.version = version
        self.key = key
        self.save_calls: list[dict[str, Any]] = []
        FakeSessionStore.instances.append(self)

    async def async_load(self) -> object:
        """Return the configured Store payload."""
        return FakeSessionStore.load_data

    async def async_save(self, data: dict[str, Any]) -> None:
        """Record a Store write."""
        self.save_calls.append(data)


@pytest.fixture(autouse=True)
def fake_session_store_patch() -> Any:
    """Keep setup tests independent from the developer HA storage directory."""
    FakeSessionStore.load_data = None
    FakeSessionStore.instances = []
    with patch("custom_components.sp_group.auth.Store", FakeSessionStore):
        yield


@pytest.fixture
def sdk_constructor_patches() -> Any:
    """Patch SDK client constructors used by integration setup."""
    with (
        patch("custom_components.sp_group.MobileAuthClient", FakeSdkClient),
        patch("custom_components.sp_group.JarvisClient", FakeSdkClient),
        patch("custom_components.sp_group.YggdrasilClient", FakeSdkClient),
        patch("custom_components.sp_group.ProfileClient", FakeSdkClient),
    ):
        yield


@pytest.fixture
def fake_coordinator_patch() -> Any:
    """Patch coordinator construction for setup lifecycle tests."""
    FakeCoordinator.side_effect = None
    FakeCoordinator.calls = []
    with patch(
        "custom_components.sp_group.SpGroupDataUpdateCoordinator",
        FakeCoordinator,
    ):
        yield FakeCoordinator


@pytest.fixture
def fake_ledger_store_patch() -> Any:
    """Patch ledger storage for setup lifecycle tests."""
    FakeLedgerStore.calls = []
    with patch("custom_components.sp_group.SpGroupLedgerStore", FakeLedgerStore):
        yield FakeLedgerStore


def make_entry(*, session_data: dict[str, Any] | None = None) -> MockConfigEntry:
    """Create an SP Group config entry for setup tests."""
    session = make_mobile_session("auth0|account-1")
    stored_session = session_data if session_data is not None else session.to_dict()
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={
            CONF_ACCOUNT_ID: "auth0|account-1",
            CONF_SESSION: stored_session,
        },
    )


def make_sp_group_ledger_entry(
    *,
    sensor_key: str = "test_sensor",
    statistics_imported_through: object = None,
    statistics_strategy_version: object = None,
    source_backfill_attempted_at: object = None,
    source_backfill_status: object = "never_attempted",
    seed_value: str = "100",
    seed_effective_from: str = "2026-07-01T00:00:00+08:00",
    backfill_from: str = "2026-06-01T00:00:00+08:00",
    malformed: bool = False,
) -> dict[str, Any]:
    """Create a ledger entry dict with configurable watermark state."""
    payload = {
        "sensor_key": sensor_key,
        "source": "manual_seed_plus_ami_deltas",
        "utility_type": "electric",
        "epoch_id": "test_epoch",
        "seed_value": seed_value,
        "seed_effective_from": seed_effective_from,
        "backfill_from": backfill_from,
        "latest_total": seed_value,
        "source_backfill_attempted_at": source_backfill_attempted_at,
        "source_backfill_status": source_backfill_status,
        "statistics_imported_through": statistics_imported_through,
        "statistics_strategy_version": statistics_strategy_version,
        "accepted": [],
        "pending": [],
        "warnings": [],
        "statistics_warnings": [],
    }
    if malformed:
        payload["source_backfill_status"] = object()
    return payload


def _statistics_scheduler_entry(
    *,
    statistics_warnings: tuple[str, ...] = (),
) -> MockConfigEntry:
    """Return an entry eligible for statistics scheduler branch tests."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.runtime_data = SimpleNamespace(
        backfill_tasks=set(),
        ledger_store=SimpleNamespace(
            snapshot=SimpleNamespace(
                malformed=False,
                entries={
                    "test_sensor": SimpleNamespace(
                        source_backfill_status="succeeded",
                        source_backfill_attempted_at="2026-07-16T00:00:00+08:00",
                        statistics_imported_through=None,
                        statistics_strategy_version=None,
                        statistics_warnings=statistics_warnings,
                    )
                },
            )
        ),
    )
    return entry


class TestAnyEntryMissingWatermark:
    """Tests for the backfill watermark check."""

    @pytest.mark.asyncio
    async def test_missing_watermark_no_ledger_store(self) -> None:
        """Returns True when ledger_store is None."""
        from custom_components.sp_group import _any_entry_missing_watermark

        runtime = SimpleNamespace(ledger_store=None)
        assert _any_entry_missing_watermark(runtime) is True

    @pytest.mark.asyncio
    async def test_missing_watermark_malformed_snapshot(self) -> None:
        """Returns True when ledger snapshot is malformed."""
        from custom_components.sp_group import _any_entry_missing_watermark

        ledger_store = SimpleNamespace(
            snapshot=SimpleNamespace(malformed=True, entries={})
        )
        runtime = SimpleNamespace(ledger_store=ledger_store)
        assert _any_entry_missing_watermark(runtime) is True

    @pytest.mark.asyncio
    async def test_missing_watermark_all_entries_complete(self) -> None:
        """Returns False when all entries have v2 statistics metadata set."""
        from custom_components.sp_group import _any_entry_missing_watermark
        from custom_components.sp_group.ledger import (
            SpGroupLedgerEntry,
            SpGroupLedgerSnapshot,
        )

        entry = SpGroupLedgerEntry.from_dict(
            make_sp_group_ledger_entry(
                statistics_imported_through="2026-07-16T00:00:00+08:00",
                statistics_strategy_version="native_offset_v2",
            )
        )
        snapshot = SpGroupLedgerSnapshot(entries={"test_sensor": entry})
        ledger_store = SimpleNamespace(snapshot=snapshot)
        runtime = SimpleNamespace(ledger_store=ledger_store)
        assert _any_entry_missing_watermark(runtime) is False

    @pytest.mark.asyncio
    async def test_missing_watermark_some_entries_incomplete(self) -> None:
        """Returns True when any entry lacks statistics_imported_through."""
        from custom_components.sp_group import _any_entry_missing_watermark
        from custom_components.sp_group.ledger import (
            SpGroupLedgerEntry,
            SpGroupLedgerSnapshot,
        )

        complete = SpGroupLedgerEntry.from_dict(
            make_sp_group_ledger_entry(
                sensor_key="sensor_complete",
                statistics_imported_through="2026-07-16T00:00:00+08:00",
                statistics_strategy_version="native_anchored_v1",
            )
        )
        incomplete = SpGroupLedgerEntry.from_dict(
            make_sp_group_ledger_entry(
                sensor_key="sensor_incomplete",
                statistics_imported_through=None,
            )
        )
        snapshot = SpGroupLedgerSnapshot(
            entries={"sensor_complete": complete, "sensor_incomplete": incomplete}
        )
        ledger_store = SimpleNamespace(snapshot=snapshot)
        runtime = SimpleNamespace(ledger_store=ledger_store)
        assert _any_entry_missing_watermark(runtime) is True

    @pytest.mark.asyncio
    async def test_missing_watermark_empty_entries(self) -> None:
        """Returns False when there are no entries (nothing to backfill)."""
        from custom_components.sp_group import _any_entry_missing_watermark
        from custom_components.sp_group.ledger import SpGroupLedgerSnapshot

        snapshot = SpGroupLedgerSnapshot(entries={})
        ledger_store = SimpleNamespace(snapshot=snapshot)
        runtime = SimpleNamespace(ledger_store=ledger_store)
        assert _any_entry_missing_watermark(runtime) is False


class TestBackfillSkipOnSetup:
    """Tests that backfill is correctly gated on watermark state."""

    @pytest.mark.asyncio
    async def test_backfill_skipped_when_all_watermarked(
        self,
        hass: HomeAssistant,
        sdk_constructor_patches: Any,
        fake_coordinator_patch: type[FakeCoordinator],
    ) -> None:
        """async_setup_entry does not start backfill when all entries complete."""
        from custom_components.sp_group.ledger import (
            SpGroupLedgerEntry,
            SpGroupLedgerSnapshot,
        )

        ledger_entry = SpGroupLedgerEntry.from_dict(
            make_sp_group_ledger_entry(
                statistics_imported_through="2026-07-16T00:00:00+08:00",
                statistics_strategy_version="native_anchored_v1",
                source_backfill_attempted_at="2026-07-16T00:00:00+08:00",
                source_backfill_status="succeeded",
            )
        )
        snapshot = SpGroupLedgerSnapshot(entries={"test_sensor": ledger_entry})

        class WatermarkedLedgerStore:
            """Duck-typed store with a fully-watermarked snapshot."""

            def __init__(self, hass: object, entry_id: str) -> None:
                self._lock = asyncio.Lock()
                self._snapshot = snapshot

            @property
            def snapshot(self) -> SpGroupLedgerSnapshot:
                return self._snapshot

            async def async_load(self) -> SpGroupLedgerSnapshot:
                return self._snapshot

        session = make_mobile_session("auth0|account-1")
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="auth0|account-1",
            data={CONF_ACCOUNT_ID: "auth0|account-1", CONF_SESSION: session.to_dict()},
        )
        entry.add_to_hass(hass)

        backfill_called = False

        def _track_backfill(*args: Any, **kwargs: Any) -> None:
            nonlocal backfill_called
            backfill_called = True

        with (
            patch(
                "custom_components.sp_group.SpGroupLedgerStore",
                WatermarkedLedgerStore,
            ),
            patch(
                "custom_components.sp_group.async_get_clientsession",
                return_value=object(),
            ),
            patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                AsyncMock(),
            ),
            patch(
                "custom_components.sp_group.async_start_initial_backfill",
                side_effect=_track_backfill,
            ),
        ):
            assert await async_setup_entry(hass, entry) is True

        assert backfill_called is False, (
            "Backfill should be skipped when all entries have attempted "
            "source backfill"
        )

    @pytest.mark.asyncio
    async def test_backfill_runs_when_unwatermarked(
        self,
        hass: HomeAssistant,
        sdk_constructor_patches: Any,
        fake_coordinator_patch: type[FakeCoordinator],
    ) -> None:
        """async_setup_entry starts backfill when an entry is missing watermark."""
        from custom_components.sp_group.ledger import (
            SpGroupLedgerEntry,
            SpGroupLedgerSnapshot,
        )

        ledger_entry = SpGroupLedgerEntry.from_dict(
            make_sp_group_ledger_entry(statistics_imported_through=None)
        )
        snapshot = SpGroupLedgerSnapshot(entries={"test_sensor": ledger_entry})

        class UnwatermarkedLedgerStore:
            """Duck-typed store with an un-watermarked snapshot."""

            def __init__(self, hass: object, entry_id: str) -> None:
                self._lock = asyncio.Lock()
                self._snapshot = snapshot

            @property
            def snapshot(self) -> SpGroupLedgerSnapshot:
                return self._snapshot

            async def async_load(self) -> SpGroupLedgerSnapshot:
                return self._snapshot

        session = make_mobile_session("auth0|account-1")
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="auth0|account-1",
            data={CONF_ACCOUNT_ID: "auth0|account-1", CONF_SESSION: session.to_dict()},
        )
        entry.add_to_hass(hass)

        backfill_called = False

        def _track_backfill(*args: Any, **kwargs: Any) -> None:
            nonlocal backfill_called
            backfill_called = True

        with (
            patch(
                "custom_components.sp_group.SpGroupLedgerStore",
                UnwatermarkedLedgerStore,
            ),
            patch(
                "custom_components.sp_group.async_get_clientsession",
                return_value=object(),
            ),
            patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                AsyncMock(),
            ),
            patch(
                "custom_components.sp_group.async_start_initial_backfill",
                side_effect=_track_backfill,
            ),
        ):
            assert await async_setup_entry(hass, entry) is True

        # Yield to let the HA task created by async_start_initial_backfill run
        await asyncio.sleep(0)

        assert backfill_called is True, (
            "Backfill should run when any entry lacks "
            "statistics_imported_through"
        )


async def test_setup_retries_failed_source_backfill(
    hass: HomeAssistant,
    sdk_constructor_patches: Any,
    fake_coordinator_patch: type[FakeCoordinator],
) -> None:
    """Setup schedules source retry when any source backfill status failed."""
    from custom_components.sp_group.ledger import (
        SpGroupLedgerEntry,
        SpGroupLedgerSnapshot,
    )

    ledger_entry = SpGroupLedgerEntry.from_dict(
        make_sp_group_ledger_entry(
            source_backfill_attempted_at="2026-07-16T00:00:00+08:00",
            source_backfill_status="failed",
            statistics_imported_through="2026-07-16T00:00:00+08:00",
            statistics_strategy_version="native_anchored_v1",
        )
    )
    snapshot = SpGroupLedgerSnapshot(entries={"test_sensor": ledger_entry})

    class FailedBackfillLedgerStore:
        """Duck-typed store with a failed source backfill entry."""

        def __init__(self, hass: object, entry_id: str) -> None:
            self._snapshot = snapshot

        @property
        def snapshot(self) -> SpGroupLedgerSnapshot:
            return self._snapshot

        async def async_load(self) -> SpGroupLedgerSnapshot:
            return self._snapshot

        async def async_mark_interrupted_source_backfills_failed(
            self,
            *,
            failed_at: datetime,
        ) -> SpGroupLedgerSnapshot:
            return self._snapshot

    entry = make_entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.sp_group.SpGroupLedgerStore",
            FailedBackfillLedgerStore,
        ),
        patch(
            "custom_components.sp_group.async_get_clientsession",
            return_value=object(),
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch("custom_components.sp_group.async_start_initial_backfill") as backfill,
    ):
        assert await async_setup_entry(hass, entry) is True

    backfill.assert_called_once_with(hass, entry)


async def test_setup_converts_persisted_running_source_backfill_to_failed(
    hass: HomeAssistant,
    sdk_constructor_patches: Any,
    fake_coordinator_patch: type[FakeCoordinator],
) -> None:
    """Setup makes interrupted running source backfill retryable."""
    from custom_components.sp_group.ledger import (
        SOURCE_BACKFILL_FAILED,
        WARNING_SOURCE_BACKFILL_INTERRUPTED,
        SpGroupLedgerEntry,
        SpGroupLedgerSnapshot,
    )

    ledger_entry = SpGroupLedgerEntry.from_dict(
        make_sp_group_ledger_entry(source_backfill_status="running")
    )
    snapshot = SpGroupLedgerSnapshot(entries={"test_sensor": ledger_entry})

    class RunningBackfillLedgerStore:
        """Duck-typed store that converts running status to failed."""

        def __init__(self, hass: object, entry_id: str) -> None:
            self._snapshot = snapshot

        @property
        def snapshot(self) -> SpGroupLedgerSnapshot:
            return self._snapshot

        async def async_load(self) -> SpGroupLedgerSnapshot:
            return self._snapshot

        async def async_mark_interrupted_source_backfills_failed(
            self,
            *,
            failed_at: datetime,
        ) -> SpGroupLedgerSnapshot:
            updated = SpGroupLedgerEntry.from_dict(
                {
                    **self._snapshot.entries["test_sensor"].to_dict(),
                    "source_backfill_status": SOURCE_BACKFILL_FAILED,
                    "source_backfill_failed_at": failed_at.isoformat(),
                    "warnings": [WARNING_SOURCE_BACKFILL_INTERRUPTED],
                }
            )
            self._snapshot = SpGroupLedgerSnapshot(entries={"test_sensor": updated})
            return self._snapshot

    entry = make_entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.sp_group.SpGroupLedgerStore",
            RunningBackfillLedgerStore,
        ),
        patch(
            "custom_components.sp_group.async_get_clientsession",
            return_value=object(),
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch("custom_components.sp_group.async_start_initial_backfill") as backfill,
    ):
        assert await async_setup_entry(hass, entry) is True

    backfill.assert_called_once_with(hass, entry)
    ledger_entry = entry.runtime_data.ledger_store.snapshot.entries["test_sensor"]
    assert ledger_entry.source_backfill_status == SOURCE_BACKFILL_FAILED
    assert WARNING_SOURCE_BACKFILL_INTERRUPTED in ledger_entry.warnings


@pytest.mark.asyncio
async def test_hourly_recorder_event_retries_after_source_backfill_attempt(
    hass: HomeAssistant,
) -> None:
    """Hourly Recorder events retry stats once source backfill has run."""
    from custom_components.sp_group import _async_register_statistics_retry_listener

    delayed: list[Any] = []
    ledger_entry = SimpleNamespace(
        source_backfill_attempted_at=None,
        statistics_imported_through=None,
        statistics_strategy_version=None,
        statistics_warnings=(),
    )
    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.runtime_data = SimpleNamespace(
        ledger_store=SimpleNamespace(
            snapshot=SimpleNamespace(
                malformed=False,
                entries={"test_sensor": ledger_entry},
            )
        ),
        backfill_tasks=set(),
    )

    with (
        patch(
            "custom_components.sp_group.async_schedule_statistics_backfill",
        ) as schedule_stats_backfill,
        patch(
            "custom_components.sp_group.async_call_later",
            side_effect=lambda _hass, _delay, action: delayed.append(action)
            or (lambda: None),
        ) as call_later,
    ):
        _async_register_statistics_retry_listener(hass, entry)

        hass.bus.async_fire(EVENT_RECORDER_HOURLY_STATISTICS_GENERATED)
        await hass.async_block_till_done()
        schedule_stats_backfill.assert_not_called()
        call_later.assert_not_called()

        ledger_entry.source_backfill_attempted_at = "2026-07-16T00:00:00+08:00"
        hass.bus.async_fire(EVENT_RECORDER_HOURLY_STATISTICS_GENERATED)
        await hass.async_block_till_done()
        schedule_stats_backfill.assert_not_called()

        assert len(delayed) == 1
        assert HassJob(delayed[0]).job_type is HassJobType.Callback
        delayed[0](None)
        schedule_stats_backfill.assert_called_once_with(hass, entry)


def test_hourly_statistics_scheduler_is_event_loop_callback() -> None:
    """The Recorder event scheduler must not run via executor threads."""
    from custom_components.sp_group import _schedule_hourly_statistics_backfill

    assert (
        HassJob(_schedule_hourly_statistics_backfill).job_type
        is HassJobType.Callback
    )


@pytest.mark.asyncio
async def test_hourly_recorder_event_does_not_block_on_missing_bridge_warning(
    hass: HomeAssistant,
) -> None:
    """Missing-bridge warnings do not block retry of importable older rows."""
    from custom_components.sp_group import _async_register_statistics_retry_listener

    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.runtime_data = SimpleNamespace(
        ledger_store=SimpleNamespace(
            snapshot=SimpleNamespace(
                malformed=False,
                entries={
                    "test_sensor": SimpleNamespace(
                        source_backfill_attempted_at="2026-07-16T00:00:00+08:00",
                        statistics_imported_through=None,
                        statistics_strategy_version=None,
                        statistics_warnings=(
                            "statistics_missing_bridge:2026-07-14T02:00:00+08:00",
                        ),
                    )
                },
            )
        ),
        backfill_tasks=set(),
    )

    delayed: list[Any] = []
    with (
        patch(
            "custom_components.sp_group.async_schedule_statistics_backfill",
        ) as schedule_stats_backfill,
        patch(
            "custom_components.sp_group.async_call_later",
            side_effect=lambda _hass, _delay, action: delayed.append(action)
            or (lambda: None),
        ),
    ):
        _async_register_statistics_retry_listener(hass, entry)
        hass.bus.async_fire(EVENT_RECORDER_HOURLY_STATISTICS_GENERATED)
        await hass.async_block_till_done()

        assert len(delayed) == 1
        assert HassJob(delayed[0]).job_type is HassJobType.Callback
        delayed[0](None)
        schedule_stats_backfill.assert_called_once_with(hass, entry)


@pytest.mark.asyncio
async def test_hourly_recorder_event_skips_manual_review_warnings(
    hass: HomeAssistant,
) -> None:
    """Hourly retry is not scheduled for warnings that need user action."""
    from custom_components.sp_group import _async_register_statistics_retry_listener

    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.runtime_data = SimpleNamespace(
        ledger_store=SimpleNamespace(
            snapshot=SimpleNamespace(
                malformed=False,
                entries={
                    "test_sensor": SimpleNamespace(
                        source_backfill_attempted_at="2026-07-16T00:00:00+08:00",
                        statistics_imported_through=None,
                        statistics_strategy_version=None,
                        statistics_warnings=(
                            "statistics_conflict:2026-07-14T02:00:00+08:00",
                        ),
                    )
                },
            )
        ),
        backfill_tasks=set(),
    )

    with patch(
        "custom_components.sp_group.async_schedule_statistics_backfill",
    ) as schedule_stats_backfill:
        _async_register_statistics_retry_listener(hass, entry)
        hass.bus.async_fire(EVENT_RECORDER_HOURLY_STATISTICS_GENERATED)
        await hass.async_block_till_done()

    schedule_stats_backfill.assert_not_called()


@pytest.mark.asyncio
async def test_dev_statistics_service_is_env_gated(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Recorder compile accelerator is hidden outside local dev."""
    from custom_components.sp_group.dev import (
        ENV_DEV_TOOLS,
        SERVICE_FORCE_STATISTICS_COMPILE,
        async_register_dev_services,
    )

    monkeypatch.delenv(ENV_DEV_TOOLS, raising=False)

    async_register_dev_services(hass)

    assert not hass.services.has_service(DOMAIN, SERVICE_FORCE_STATISTICS_COMPILE)


@pytest.mark.asyncio
async def test_dev_statistics_service_only_queues_recorder(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dev helper leaves stats import to the Recorder event listener."""
    from custom_components.sp_group.dev import (
        ENV_DEV_TOOLS,
        SERVICE_FORCE_STATISTICS_COMPILE,
        async_register_dev_services,
    )

    class FakeRecorder:
        """Capture queued Recorder tasks."""

        def __init__(self) -> None:
            self.tasks: list[Any] = []

        def queue_task(self, task: Any) -> None:
            self.tasks.append(task)

        async def async_block_till_done(self) -> None:
            return None

    monkeypatch.setenv(ENV_DEV_TOOLS, "1")
    hass.config.components.add(RECORDER_DOMAIN)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.runtime_data = SimpleNamespace(backfill_tasks=set())
    entry.add_to_hass(hass)
    recorder = FakeRecorder()

    with (
        patch(
            "custom_components.sp_group.dev._recorder_instance",
            return_value=recorder,
        ),
        patch(
            "custom_components.sp_group.dev._uncompiled_hourly_trigger_start",
            AsyncMock(return_value=datetime(2026, 7, 20, 7, 55, tzinfo=UTC)),
        ),
        patch(
            "custom_components.sp_group.async_schedule_statistics_backfill",
            return_value=StatisticsBackfillScheduleStatus.SCHEDULED,
        ) as schedule_stats_backfill,
    ):
        async_register_dev_services(hass)
        await hass.services.async_call(
            DOMAIN,
            SERVICE_FORCE_STATISTICS_COMPILE,
            {},
            blocking=True,
        )

    assert len(recorder.tasks) == 2
    assert recorder.tasks[0].fire_events is False
    assert recorder.tasks[1].fire_events is True
    assert recorder.tasks[1].start.minute == 55
    schedule_stats_backfill.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_statistics_backfill_is_single_flight(
    hass: HomeAssistant,
) -> None:
    """Statistics retry scheduler does not overlap active retry tasks."""
    release = asyncio.Event()
    calls = 0

    async def _slow_statistics_backfill(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        await release.wait()

    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.runtime_data = SimpleNamespace(
        backfill_tasks=set(),
        ledger_store=SimpleNamespace(
            snapshot=SimpleNamespace(
                malformed=False,
                entries={
                    "test_sensor": SimpleNamespace(
                        source_backfill_attempted_at="2026-07-16T00:00:00+08:00",
                        statistics_imported_through=None,
                        statistics_strategy_version=None,
                        statistics_warnings=(),
                    )
                },
            )
        ),
    )

    with patch(
        "custom_components.sp_group.backfill.async_run_statistics_backfill",
        side_effect=_slow_statistics_backfill,
    ):
        assert (
            async_schedule_statistics_backfill(hass, entry)
            == StatisticsBackfillScheduleStatus.SCHEDULED
        )
        await asyncio.sleep(0)
        assert (
            async_schedule_statistics_backfill(hass, entry)
            == StatisticsBackfillScheduleStatus.ALREADY_RUNNING
        )
        release.set()
        await hass.async_block_till_done()

    assert calls == 1
    assert entry.runtime_data.backfill_tasks == set()


@pytest.mark.asyncio
async def test_schedule_statistics_backfill_force_rechecks_watermarked_entries(
    hass: HomeAssistant,
) -> None:
    """Manual retry can verify Recorder rows even when ledger metadata is complete."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.runtime_data = SimpleNamespace(
        backfill_tasks=set(),
        ledger_store=SimpleNamespace(
            snapshot=SimpleNamespace(
                malformed=False,
                entries={
                    "test_sensor": SimpleNamespace(
                        source_backfill_attempted_at="2026-07-16T00:00:00+08:00",
                        statistics_imported_through="2026-07-16T00:00:00+08:00",
                        statistics_strategy_version="native_offset_v2",
                        statistics_warnings=(),
                    )
                },
            )
        ),
    )

    with patch(
        "custom_components.sp_group.backfill.async_run_statistics_backfill",
        new_callable=AsyncMock,
    ) as statistics_backfill:
        assert (
            async_schedule_statistics_backfill(hass, entry)
            == StatisticsBackfillScheduleStatus.NOT_READY
        )
        assert (
            async_schedule_statistics_backfill(hass, entry, force=True)
            == StatisticsBackfillScheduleStatus.SCHEDULED
        )
        await hass.async_block_till_done()

    statistics_backfill.assert_awaited_once()


@pytest.mark.parametrize(
    "warning",
    [
        "statistics_conflict:2026-07-14T02:00:00+08:00",
        "statistics_existing_negative_delta:2026-07-14T01:00:00+08:00:"
        "2026-07-14T02:00:00+08:00",
        "statistics_legacy_rows_need_manual_rebuild",
    ],
)
def test_schedule_statistics_backfill_blocks_manual_review_warnings(
    hass: HomeAssistant,
    warning: str,
) -> None:
    """Hard statistics warnings prevent automatic retries."""
    entry = _statistics_scheduler_entry(statistics_warnings=(warning,))

    assert (
        async_schedule_statistics_backfill(hass, entry)
        == StatisticsBackfillScheduleStatus.BLOCKED
    )


@pytest.mark.parametrize(
    "warning",
    [
        "statistics_missing_bridge:2026-07-14T02:00:00+08:00",
        "statistics_waiting_for_native_reference",
    ],
)
async def test_schedule_statistics_backfill_allows_non_blocking_warnings(
    hass: HomeAssistant,
    warning: str,
) -> None:
    """Waiting/missing bridge warnings do not permanently block retry."""
    entry = _statistics_scheduler_entry(statistics_warnings=(warning,))

    with patch(
        "custom_components.sp_group.backfill.async_run_statistics_backfill",
        new_callable=AsyncMock,
    ) as statistics_backfill:
        assert (
            async_schedule_statistics_backfill(hass, entry)
            == StatisticsBackfillScheduleStatus.SCHEDULED
        )
        await hass.async_block_till_done()

    statistics_backfill.assert_awaited_once()


@pytest.mark.asyncio
async def test_schedule_statistics_backfill_uses_loaded_entry_runtime(
    hass: HomeAssistant,
) -> None:
    """Scheduler resolves HA's loaded entry when Options Flow passes a detached one."""
    loaded_entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="loaded-sp-group-entry",
        unique_id="auth0|account-1",
    )
    loaded_entry.runtime_data = SimpleNamespace(
        backfill_tasks=set(),
        ledger_store=SimpleNamespace(
            snapshot=SimpleNamespace(
                malformed=False,
                entries={
                    "test_sensor": SimpleNamespace(
                        source_backfill_attempted_at="2026-07-16T00:00:00+08:00",
                        statistics_imported_through=None,
                        statistics_strategy_version=None,
                        statistics_warnings=(),
                    )
                },
            )
        ),
    )
    loaded_entry.add_to_hass(hass)
    detached_entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="loaded-sp-group-entry",
        unique_id="auth0|account-1",
    )

    with patch(
        "custom_components.sp_group.backfill.async_run_statistics_backfill",
        new_callable=AsyncMock,
    ) as statistics_backfill:
        assert (
            async_schedule_statistics_backfill(hass, detached_entry)
            == StatisticsBackfillScheduleStatus.SCHEDULED
        )
        await hass.async_block_till_done()

    statistics_backfill.assert_awaited_once()
    assert loaded_entry.runtime_data.backfill_tasks == set()


@pytest.mark.asyncio
async def test_async_setup_entry_creates_runtime_container(
    sdk_constructor_patches: Any,
    fake_coordinator_patch: type[FakeCoordinator],
    fake_ledger_store_patch: type[FakeLedgerStore],
) -> None:
    """Setup creates SDK clients, session state, lock, and coordinator."""
    shared_session = object()
    forward = AsyncMock(
        side_effect=lambda *args: FakeCoordinator.calls.append("forward")
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_forward_entry_setups=forward)
    )
    entry = make_entry()

    with (
        patch("custom_components.sp_group.async_get_clientsession") as get_session,
        patch("custom_components.sp_group._async_delete_malformed_ledger_issue"),
    ):
        get_session.return_value = shared_session

        assert await async_setup_entry(hass, entry) is True

    assert isinstance(entry.runtime_data, SpGroupRuntimeData)
    assert isinstance(entry.runtime_data.coordinator, FakeCoordinator)
    assert isinstance(entry.runtime_data.ledger_store, FakeLedgerStore)
    assert isinstance(entry.runtime_data.refresh_lock, asyncio.Lock)
    assert entry.runtime_data.account_id == "auth0|account-1"
    assert entry.runtime_data.session.identity_metadata is not None
    assert entry.runtime_data.session.identity_metadata.subject == "auth0|account-1"
    assert entry.runtime_data.auth_client.session is shared_session
    assert entry.runtime_data.jarvis_client.session is shared_session
    assert entry.runtime_data.yggdrasil_client.session is shared_session
    assert entry.runtime_data.profile_client.session is shared_session
    forward.assert_awaited_once_with(entry, [Platform.SENSOR])
    assert FakeCoordinator.calls == ["create", "first_refresh", "forward"]
    assert fake_ledger_store_patch.calls == ["create_ledger", "load_ledger"]


@pytest.mark.asyncio
async def test_async_setup_entry_prefers_store_session(
    sdk_constructor_patches: Any,
    fake_coordinator_patch: type[FakeCoordinator],
    fake_ledger_store_patch: type[FakeLedgerStore],
) -> None:
    """Setup uses a valid Store session instead of stale config-entry data."""
    legacy_session = make_mobile_session(
        "auth0|account-1",
        access_token="legacy-access-token",
    )
    store_session = make_mobile_session(
        "auth0|account-1",
        access_token="store-access-token",
    )
    FakeSessionStore.load_data = store_session.to_dict()
    entry = make_entry(session_data=legacy_session.to_dict())
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock())
    )

    with (
        patch(
            "custom_components.sp_group.async_get_clientsession",
            return_value=object(),
        ),
        patch("custom_components.sp_group._async_delete_malformed_ledger_issue"),
    ):
        assert await async_setup_entry(hass, entry) is True

    assert entry.runtime_data.session.access_token == "store-access-token"
    assert entry.runtime_data.session_store is FakeSessionStore.instances[0]
    assert FakeSessionStore.instances[0].save_calls == []


@pytest.mark.asyncio
async def test_async_setup_entry_migrates_legacy_session_to_store(
    sdk_constructor_patches: Any,
    fake_coordinator_patch: type[FakeCoordinator],
    fake_ledger_store_patch: type[FakeLedgerStore],
) -> None:
    """Setup seeds Store from legacy entry data when no Store exists."""
    session = make_mobile_session("auth0|account-1")
    entry = make_entry(session_data=session.to_dict())
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock())
    )

    with (
        patch(
            "custom_components.sp_group.async_get_clientsession",
            return_value=object(),
        ),
        patch("custom_components.sp_group._async_delete_malformed_ledger_issue"),
    ):
        assert await async_setup_entry(hass, entry) is True

    assert entry.runtime_data.session == session
    assert FakeSessionStore.instances[0].save_calls == [session.to_dict()]


@pytest.mark.asyncio
async def test_async_setup_entry_falls_back_when_store_session_is_invalid(
    sdk_constructor_patches: Any,
    fake_coordinator_patch: type[FakeCoordinator],
    fake_ledger_store_patch: type[FakeLedgerStore],
) -> None:
    """Setup preserves a corrupt Store and falls back to legacy entry data."""
    session = make_mobile_session("auth0|account-1")
    FakeSessionStore.load_data = {"accessToken": "invalid"}
    entry = make_entry(session_data=session.to_dict())
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock())
    )

    with (
        patch(
            "custom_components.sp_group.async_get_clientsession",
            return_value=object(),
        ),
        patch("custom_components.sp_group._async_delete_malformed_ledger_issue"),
    ):
        assert await async_setup_entry(hass, entry) is True

    assert entry.runtime_data.session == session
    assert FakeSessionStore.instances[0].save_calls == []


async def test_async_migrate_entry_normalizes_legacy_source_options(
    hass: HomeAssistant,
) -> None:
    """Config-entry migration preserves readable legacy source configs."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        version=1,
        minor_version=1,
        data={CONF_ACCOUNT_ID: "auth0|account-1", CONF_SESSION: {"legacy": True}},
        options={
            CONF_SOURCE_CONFIG: {
                "opaque_electric": {
                    "premise_key": "opaque_premise",
                    "utility_type": "electric",
                    "source": "manual_seed_plus_ami_deltas",
                    "seed_value": "100.0",
                    "seed_observed_at": "2026-07-15T13:00:00+08:00",
                    "seed_effective_from": "2026-07-15T13:00:00+08:00",
                    "boundary_aligned": True,
                },
                "broken": {"bad": "data"},
            }
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 1
    assert entry.minor_version == 3
    migrated = entry.options[CONF_SOURCE_CONFIG]
    assert set(migrated) == {"opaque_electric"}
    assert migrated["opaque_electric"]["enabled"] is True
    assert migrated["opaque_electric"]["reset_confirmed"] is False
    assert migrated["opaque_electric"]["epoch_id"]
    assert entry.data[CONF_SESSION] == {"legacy": True}


async def test_async_migrate_entry_rejects_unsupported_major_version(
    hass: HomeAssistant,
) -> None:
    """Unsupported future major versions are not migrated."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, minor_version=1)
    entry.add_to_hass(hass)

    assert not await async_migrate_entry(hass, entry)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_data",
    [
        None,
        {"accessToken": "token"},
    ],
)
async def test_async_setup_entry_raises_auth_failed_for_invalid_session(
    session_data: dict[str, Any] | None,
    sdk_constructor_patches: Any,
) -> None:
    """Missing or malformed stored session data starts HA's reauth path."""
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock())
    )
    entry = make_entry(session_data=session_data) if session_data is not None else (
        MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1", data={})
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)

    hass.config_entries.async_forward_entry_setups.assert_not_called()


async def test_async_setup_entry_creates_malformed_ledger_repair_issue(
    hass: HomeAssistant,
    sdk_constructor_patches: Any,
    fake_coordinator_patch: type[FakeCoordinator],
) -> None:
    """Malformed ledger setup creates a fixable repair issue."""
    session = make_mobile_session("auth0|account-1")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={CONF_ACCOUNT_ID: "auth0|account-1", CONF_SESSION: session.to_dict()},
    )
    entry.add_to_hass(hass)

    class MalformedLedgerStore(FakeLedgerStore):
        def __init__(self, hass: object, entry_id: str) -> None:
            super().__init__(hass, entry_id)
            self.snapshot = SimpleNamespace(malformed=True)

    with (
        patch("custom_components.sp_group.SpGroupLedgerStore", MalformedLedgerStore),
        patch(
            "custom_components.sp_group.async_get_clientsession",
            return_value=object(),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(),
        ),
        patch("custom_components.sp_group.async_start_initial_backfill") as backfill,
    ):
        assert await async_setup_entry(hass, entry) is True

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN,
        f"{MALFORMED_LEDGER_REPAIR_ISSUE}_{entry.entry_id}",
    )
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_key == MALFORMED_LEDGER_REPAIR_ISSUE
    backfill.assert_not_called()


async def test_async_setup_entry_deletes_stale_malformed_ledger_issue(
    hass: HomeAssistant,
    sdk_constructor_patches: Any,
    fake_coordinator_patch: type[FakeCoordinator],
    fake_ledger_store_patch: type[FakeLedgerStore],
) -> None:
    """Healthy ledger setup clears stale malformed-ledger repair issues."""
    session = make_mobile_session("auth0|account-1")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|account-1",
        data={CONF_ACCOUNT_ID: "auth0|account-1", CONF_SESSION: session.to_dict()},
    )
    entry.add_to_hass(hass)
    issue_id = f"{MALFORMED_LEDGER_REPAIR_ISSUE}_{entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=MALFORMED_LEDGER_REPAIR_ISSUE,
    )

    with patch(
        "custom_components.sp_group.async_get_clientsession",
        return_value=object(),
    ), patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(),
    ):
        assert await async_setup_entry(hass, entry) is True

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.asyncio
async def test_async_setup_entry_first_refresh_update_failed_retries_setup(
    sdk_constructor_patches: Any,
    fake_coordinator_patch: type[FakeCoordinator],
    fake_ledger_store_patch: type[FakeLedgerStore],
) -> None:
    """Transient first-refresh failures become HA setup retry."""
    fake_coordinator_patch.side_effect = UpdateFailed("temporary outage")
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock())
    )
    entry = make_entry()

    with (
        patch("custom_components.sp_group.async_get_clientsession") as get_session,
        patch("custom_components.sp_group._async_delete_malformed_ledger_issue"),
    ):
        get_session.return_value = object()
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)

    hass.config_entries.async_forward_entry_setups.assert_not_called()
    assert fake_coordinator_patch.calls == ["create", "first_refresh"]


@pytest.mark.asyncio
async def test_async_setup_entry_first_refresh_auth_failure_propagates(
    sdk_constructor_patches: Any,
    fake_coordinator_patch: type[FakeCoordinator],
    fake_ledger_store_patch: type[FakeLedgerStore],
) -> None:
    """Auth first-refresh failures start HA reauth instead of setup retry."""
    fake_coordinator_patch.side_effect = ConfigEntryAuthFailed("reauth")
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock())
    )
    entry = make_entry()

    with (
        patch("custom_components.sp_group.async_get_clientsession") as get_session,
        patch("custom_components.sp_group._async_delete_malformed_ledger_issue"),
    ):
        get_session.return_value = object()
        with pytest.raises(ConfigEntryAuthFailed):
            await async_setup_entry(hass, entry)

    hass.config_entries.async_forward_entry_setups.assert_not_called()
    assert fake_coordinator_patch.calls == ["create", "first_refresh"]


@pytest.mark.asyncio
async def test_async_unload_entry_unloads_platforms_only() -> None:
    """Unload delegates to Home Assistant platform unloading."""
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=True)
        )
    )
    entry = make_entry()

    assert await async_unload_entry(hass, entry) is True
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        entry, [Platform.SENSOR]
    )


async def test_async_unload_entry_cancels_and_awaits_background_tasks(
    hass: HomeAssistant,
) -> None:
    """Unload cancels tracked source/statistics background work."""
    started = asyncio.Event()

    async def _pending_task() -> None:
        started.set()
        await asyncio.Event().wait()

    task = hass.async_create_task(_pending_task(), name="sp_group_full_backfill")
    await started.wait()
    entry = make_entry()
    water_coordinator = SimpleNamespace(async_cancel_schedule=Mock())
    entry.runtime_data = SimpleNamespace(
        backfill_tasks={task},
        water_coordinator=water_coordinator,
    )

    assert await async_unload_entry(hass, entry) is True

    assert task.cancelled()
    water_coordinator.async_cancel_schedule.assert_called_once()
