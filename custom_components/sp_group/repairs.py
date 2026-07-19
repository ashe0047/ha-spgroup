"""Repairs support for the SP Group integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import MALFORMED_LEDGER_REPAIR_ISSUE
from .const import DOMAIN
from .ledger import (
    WARNING_REPAIR_REBUILT_MALFORMED_LEDGER,
    SpGroupLedgerStore,
)
from .sources import enabled_source_configs


class MalformedLedgerRepairFlow(RepairsFlow):
    """Fix flow for malformed SP Group accumulation ledger state."""

    def __init__(
        self,
        hass: HomeAssistant,
        issue_id: str,
        data: dict[str, str | int | float | None] | None,
    ) -> None:
        """Initialize the repair flow."""
        self._hass = hass
        self._issue_id = issue_id
        self._data = data or {}

    async def async_step_init(
        self,
        user_input: dict[str, str] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Start the malformed-ledger repair flow."""
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(
        self,
        user_input: dict[str, str] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Confirm rebuilding the malformed ledger."""
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
            )

        entry_id = self._data.get("entry_id")
        if not isinstance(entry_id, str):
            return self.async_abort(reason="missing_entry")
        entry = self._hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return self.async_abort(reason="missing_entry")

        configs = enabled_source_configs(entry.options)
        ledger_store = SpGroupLedgerStore(self._hass, entry.entry_id)
        await ledger_store.async_load()
        await ledger_store.async_rebuild_sources(
            configs,
            warning=WARNING_REPAIR_REBUILT_MALFORMED_LEDGER,
        )
        ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
        await self._hass.config_entries.async_reload(entry.entry_id)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a fix flow for a repair issue."""
    if not issue_id.startswith(MALFORMED_LEDGER_REPAIR_ISSUE):
        return ConfirmRepairFlow()
    return MalformedLedgerRepairFlow(hass, issue_id, data)
