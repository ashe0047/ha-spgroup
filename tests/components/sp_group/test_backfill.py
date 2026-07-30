"""Test SP Group background backfill orchestration."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.sp_group.backfill import (
    _run_backfill_with_retry,
    _track_backfill_task,
)
from custom_components.sp_group.const import DOMAIN
from custom_components.sp_group.coordinator import SpGroupUtilityPollResult
from custom_components.sp_group.discovery import SpGroupFutureSensorKey
from custom_components.sp_group.ledger import (
    SOURCE_BACKFILL_FAILED,
    SOURCE_BACKFILL_SUCCEEDED,
    SpGroupLedgerStore,
)
from custom_components.sp_group.sources import build_source_config
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_source_backfill_marks_only_failed_water_utility(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Water failure does not regress successful electricity source backfill."""
    entry, ledger_store = await _entry_with_source_ledger(hass, tmp_path)
    entry.runtime_data.electricity_coordinator.async_poll_now = AsyncMock()
    entry.runtime_data.water_coordinator.async_poll_now = AsyncMock(
        side_effect=UpdateFailed("water outage")
    )

    with patch(
        "custom_components.sp_group.backfill.async_backfill_statistics",
        new_callable=AsyncMock,
    ) as stats_backfill:
        await _run_backfill_with_retry(hass, entry)

    assert (
        ledger_store.snapshot.entries["opaque_electric"].source_backfill_status
        == SOURCE_BACKFILL_SUCCEEDED
    )
    assert (
        ledger_store.snapshot.entries["opaque_water"].source_backfill_status
        == SOURCE_BACKFILL_FAILED
    )
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "backfill_failed") is not None
    )
    stats_backfill.assert_not_awaited()


async def test_source_backfill_marks_only_failed_electricity_utility(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Electricity failure does not mark water as failed."""
    entry, ledger_store = await _entry_with_source_ledger(hass, tmp_path)
    entry.runtime_data.electricity_coordinator.async_poll_now = AsyncMock(
        side_effect=UpdateFailed("electricity outage")
    )
    entry.runtime_data.water_coordinator.async_poll_now = AsyncMock()

    with patch(
        "custom_components.sp_group.backfill.async_backfill_statistics",
        new_callable=AsyncMock,
    ) as stats_backfill:
        await _run_backfill_with_retry(hass, entry)

    assert (
        ledger_store.snapshot.entries["opaque_electric"].source_backfill_status
        == SOURCE_BACKFILL_FAILED
    )
    assert (
        ledger_store.snapshot.entries["opaque_water"].source_backfill_status
        == SOURCE_BACKFILL_SUCCEEDED
    )
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "backfill_failed") is not None
    )
    stats_backfill.assert_not_awaited()


async def test_source_backfill_success_marks_retryable_entries_and_imports_stats(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Successful source backfill marks entries and continues to stats import."""
    entry, ledger_store = await _entry_with_source_ledger(hass, tmp_path)
    entry.runtime_data.electricity_coordinator.async_poll_now = AsyncMock()
    entry.runtime_data.water_coordinator.async_poll_now = AsyncMock()

    with patch(
        "custom_components.sp_group.backfill.async_backfill_statistics",
        new_callable=AsyncMock,
    ) as stats_backfill:
        await _run_backfill_with_retry(hass, entry)

    assert {
        key: ledger_entry.source_backfill_status
        for key, ledger_entry in ledger_store.snapshot.entries.items()
    } == {
        "opaque_electric": SOURCE_BACKFILL_SUCCEEDED,
        "opaque_water": SOURCE_BACKFILL_SUCCEEDED,
    }
    assert ir.async_get(hass).async_get_issue(DOMAIN, "backfill_failed") is None
    assert entry.runtime_data.electricity_coordinator.async_poll_now.await_count == 2
    entry.runtime_data.water_coordinator.async_poll_now.assert_awaited_once_with(
        backfill=True
    )
    stats_backfill.assert_awaited_once_with(hass, entry)


async def test_provider_noop_result_keeps_source_backfill_retryable(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """A coordinator no-op does not falsely complete source backfill."""
    entry, ledger_store = await _entry_with_source_ledger(hass, tmp_path)
    entry.runtime_data.electricity_coordinator.async_poll_now = AsyncMock(
        return_value=SpGroupUtilityPollResult(
            provider_failure_count=1,
            failed_source_keys=frozenset({"opaque_electric"}),
        )
    )
    entry.runtime_data.water_coordinator.async_poll_now = AsyncMock()

    with patch(
        "custom_components.sp_group.backfill.async_backfill_statistics",
        new_callable=AsyncMock,
    ) as stats_backfill:
        await _run_backfill_with_retry(hass, entry)

    assert (
        ledger_store.snapshot.entries["opaque_electric"].source_backfill_status
        == SOURCE_BACKFILL_FAILED
    )
    assert (
        ledger_store.snapshot.entries["opaque_water"].source_backfill_status
        == SOURCE_BACKFILL_SUCCEEDED
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, "backfill_failed") is not None
    stats_backfill.assert_not_awaited()


async def test_initial_statistics_failure_is_deferred_without_parallel_retry(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Recorder failure defers to event retry instead of sleeping/retrying here."""
    entry, _ledger_store = await _entry_with_source_ledger(hass, tmp_path)
    entry.runtime_data.electricity_coordinator.async_poll_now = AsyncMock()
    entry.runtime_data.water_coordinator.async_poll_now = AsyncMock()

    with patch(
        "custom_components.sp_group.backfill.async_backfill_statistics",
        new_callable=AsyncMock,
        side_effect=UpdateFailed("recorder unavailable"),
    ) as stats_backfill:
        await _run_backfill_with_retry(hass, entry)

    stats_backfill.assert_awaited_once_with(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "backfill_failed") is None


async def test_backfill_task_lifecycle_handles_cancel_and_error(
    hass: HomeAssistant,
) -> None:
    """Tracked task cleanup consumes cancellations and surfaces real failures."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.runtime_data = SimpleNamespace(backfill_tasks=set())

    async def _cancelled() -> None:
        raise asyncio.CancelledError

    async def _failed() -> None:
        raise RuntimeError("boom")

    cancelled = hass.async_create_task(_cancelled(), name="sp_group_full_backfill")
    _track_backfill_task(hass, entry, cancelled, trigger="sp_group_full_backfill")
    await hass.async_block_till_done()
    assert entry.runtime_data.backfill_tasks == set()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "backfill_failed") is None

    failed = hass.async_create_task(_failed(), name="sp_group_full_backfill")
    _track_backfill_task(hass, entry, failed, trigger="sp_group_full_backfill")
    await hass.async_block_till_done()
    assert entry.runtime_data.backfill_tasks == set()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "backfill_failed") is not None


async def _entry_with_source_ledger(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> tuple[MockConfigEntry, SpGroupLedgerStore]:
    """Return an entry with one electricity and one water source ledger entry."""
    hass.config.config_dir = str(tmp_path)
    electric = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_electric",
            premise_key="opaque_premise",
            utility_type="electric",
        ),
        seed_value="100",
        seed_observed_at="2026-07-01T00:00:00+08:00",
        backfill_from="2026-07-01T00:00:00+08:00",
        now=None,
    )
    water = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_water",
            premise_key="opaque_premise",
            utility_type="water",
        ),
        seed_value="10",
        seed_observed_at="2026-07-01T00:00:00+08:00",
        backfill_from="2026-07-01T00:00:00+08:00",
        now=None,
    )
    ledger_store = SpGroupLedgerStore(hass, "entry-source-backfill")
    await ledger_store.async_load()
    await ledger_store.async_initialize_sources(
        {"opaque_electric": electric, "opaque_water": water}
    )
    entry = MockConfigEntry(domain=DOMAIN, unique_id="auth0|account-1")
    entry.runtime_data = SimpleNamespace(
        ledger_store=ledger_store,
        electricity_coordinator=SimpleNamespace(
            async_poll_now=AsyncMock(), require_two_sightings=True
        ),
        water_coordinator=SimpleNamespace(async_poll_now=AsyncMock()),
    )
    return entry, ledger_store
