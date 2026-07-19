"""Test SP Group runtime privacy and operational safety surfaces."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from custom_components.sp_group.const import CONF_SOURCE_CONFIG, DOMAIN
from custom_components.sp_group.diagnostics import async_get_config_entry_diagnostics
from custom_components.sp_group.ledger import SpGroupLedgerSnapshot
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from spgroup.mobile.errors import SpGroupMobileError

from .test_recorder_statistics import (
    ELECTRIC_KEY,
    _setup_seeded_entry,
)

SENSITIVE_VALUES = (
    "access-token",
    "id-token",
    "refresh-token",
    "ACC-1",
    "ACC-ROOT-1",
    "PREM-1",
    "JARVIS-PREMISE-1",
    "123 Secret Street",
    "Sensitive Owner",
    "person@example.test",
    "owner@example.test",
    "+6512345678",
    "raw secret",
)


async def test_persisted_options_and_ledger_payloads_are_opaque(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """Config-entry options and ledger storage payloads do not expose raw IDs."""
    entry = await _setup_seeded_entry(hass, tmp_path)
    ledger_store = entry.runtime_data.ledger_store
    assert ledger_store is not None

    _assert_sensitive_values_absent(entry.options)
    _assert_sensitive_values_absent(ledger_store.snapshot.to_dict())
    assert set(entry.options[CONF_SOURCE_CONFIG]) == {
        "sp_group_premise_938f4ae4706115485aaa8512_electric",
        "sp_group_premise_938f4ae4706115485aaa8512_water",
    }


async def test_runtime_logs_and_diagnostics_remain_sanitized(
    hass: HomeAssistant,
    tmp_path: Path,
    caplog: Any,
) -> None:
    """Coordinator errors and diagnostics expose summaries, not raw payload values."""
    caplog.set_level(logging.DEBUG, logger="custom_components.sp_group")
    entry = await _setup_seeded_entry(hass, tmp_path)
    coordinator = entry.runtime_data.electricity_coordinator
    assert coordinator is not None
    entry.runtime_data.jarvis_client.get_ami_charts = AsyncMock(  # type: ignore[method-assign]
        side_effect=SpGroupMobileError("raw secret ACC-1 JARVIS-PREMISE-1")
    )

    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    _assert_sensitive_values_absent(rendered_logs)
    _assert_sensitive_values_absent(diagnostics)
    assert "Error refreshing SP Group utility evidence" in rendered_logs
    assert "raw secret" not in rendered_logs


async def test_entity_and_repair_surfaces_are_minimal(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """Entity registry/state and repair issue payloads avoid raw identifiers."""
    entry = await _setup_seeded_entry(hass, tmp_path)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, ELECTRIC_KEY)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    issue_id = f"malformed_ledger_{entry.entry_id}"

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="malformed_ledger",
    )
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None

    _assert_sensitive_values_absent(
        {
            "entity_id": entity_id,
            "unique_id": ELECTRIC_KEY,
            "state": state.as_dict(),
            "issue_data": issue.data,
            "ledger_repr": repr(SpGroupLedgerSnapshot()),
        }
    )


def _assert_sensitive_values_absent(value: Any) -> None:
    """Assert sensitive fixture values are absent from rendered data."""
    rendered = repr(value)
    for sensitive in SENSITIVE_VALUES:
        assert sensitive not in rendered
