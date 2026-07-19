"""Test SP Group integration setup boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.sp_group import (
    MALFORMED_LEDGER_REPAIR_ISSUE,
    SpGroupRuntimeData,
    async_migrate_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.sp_group.const import (
    CONF_ACCOUNT_ID,
    CONF_SESSION,
    CONF_SOURCE_CONFIG,
    DOMAIN,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
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
    seed_value: str = "100",
    seed_effective_from: str = "2026-07-01T00:00:00+08:00",
    backfill_from: str = "2026-06-01T00:00:00+08:00",
) -> dict[str, Any]:
    """Create a ledger entry dict with configurable watermark state."""
    return {
        "sensor_key": sensor_key,
        "source": "manual_seed_plus_ami_deltas",
        "utility_type": "electric",
        "epoch_id": "test_epoch",
        "seed_value": seed_value,
        "seed_effective_from": seed_effective_from,
        "backfill_from": backfill_from,
        "latest_total": seed_value,
        "statistics_imported_through": statistics_imported_through,
        "accepted": [],
        "pending": [],
        "warnings": [],
        "statistics_warnings": [],
    }


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
        """Returns False when all entries have statistics_imported_through set."""
        from custom_components.sp_group import _any_entry_missing_watermark
        from custom_components.sp_group.ledger import (
            SpGroupLedgerEntry,
            SpGroupLedgerSnapshot,
        )

        entry = SpGroupLedgerEntry.from_dict(
            make_sp_group_ledger_entry(statistics_imported_through="2026-07-16T00:00:00+08:00")
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
            "Backfill should be skipped when all entries have "
            "statistics_imported_through set"
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
    ):
        assert await async_setup_entry(hass, entry) is True

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN,
        f"{MALFORMED_LEDGER_REPAIR_ISSUE}_{entry.entry_id}",
    )
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_key == MALFORMED_LEDGER_REPAIR_ISSUE


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
