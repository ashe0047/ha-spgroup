"""Test SP Group repair flows."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

from custom_components.sp_group import MALFORMED_LEDGER_REPAIR_ISSUE
from custom_components.sp_group.const import CONF_SOURCE_CONFIG, DOMAIN
from custom_components.sp_group.discovery import SpGroupFutureSensorKey
from custom_components.sp_group.ledger import (
    WARNING_REPAIR_REBUILT_MALFORMED_LEDGER,
    SpGroupLedgerSnapshot,
)
from custom_components.sp_group.repairs import async_create_fix_flow
from custom_components.sp_group.sources import SINGAPORE_TZ, build_source_config
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_malformed_ledger_repair_rebuilds_and_deletes_issue(
    hass: HomeAssistant,
) -> None:
    """Repair flow rebuilds malformed ledger state from stored source config."""
    sensor_key = SpGroupFutureSensorKey(
        key="opaque_electric",
        premise_key="opaque_premise",
        utility_type="electric",
    )
    config = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry-repair",
        options={CONF_SOURCE_CONFIG: {sensor_key.key: config.to_dict()}},
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

    class FakeLedgerStore:
        calls: list[object] = []

        def __init__(self, hass: object, entry_id: str) -> None:
            self.snapshot = SpGroupLedgerSnapshot(malformed=True)
            self.entry_id = entry_id

        async def async_load(self) -> SpGroupLedgerSnapshot:
            FakeLedgerStore.calls.append("load")
            return self.snapshot

        async def async_rebuild_sources(
            self,
            configs: object,
            *,
            warning: str,
        ) -> SpGroupLedgerSnapshot:
            FakeLedgerStore.calls.append((configs, warning))
            self.snapshot = SpGroupLedgerSnapshot()
            return self.snapshot

    reload = AsyncMock()
    with (
        patch("custom_components.sp_group.repairs.SpGroupLedgerStore", FakeLedgerStore),
        patch.object(hass.config_entries, "async_reload", reload),
    ):
        flow = await async_create_fix_flow(
            hass,
            issue_id,
            {"entry_id": entry.entry_id},
        )
        result = await flow.async_step_init()
        assert result["type"] is FlowResultType.FORM
        result = await flow.async_step_confirm({})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert FakeLedgerStore.calls == [
        "load",
        ({sensor_key.key: config}, WARNING_REPAIR_REBUILT_MALFORMED_LEDGER),
    ]
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    reload.assert_awaited_once_with(entry.entry_id)


async def test_malformed_ledger_repair_aborts_for_missing_entry(
    hass: HomeAssistant,
) -> None:
    """Repair flow aborts cleanly if the config entry is gone."""
    flow = await async_create_fix_flow(
        hass,
        f"{MALFORMED_LEDGER_REPAIR_ISSUE}_missing",
        {"entry_id": "missing"},
    )

    result = await flow.async_step_confirm({})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "missing_entry"
