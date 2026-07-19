"""Opt-in live HA runtime soak for the SP Group integration."""

from __future__ import annotations

import json
import os
from functools import partial
from pathlib import Path

import pytest
import pytest_socket
from custom_components.sp_group import SpGroupRuntimeData
from custom_components.sp_group.const import CONF_ACCOUNT_ID, CONF_SESSION, DOMAIN
from custom_components.sp_group.diagnostics import async_get_config_entry_diagnostics
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from spgroup.mobile import MobileSession

pytestmark = [pytest.mark.live, pytest.mark.spgroup_ha_live_soak]

SENSITIVE_TOKEN_KEYS = ("accessToken", "idToken", "refreshToken")


async def test_live_ha_runtime_soak_uses_cached_session(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """Run a read-only live coordinator soak through the HA integration runtime."""
    if os.environ.get("SPGROUP_HA_LIVE_SOAK") != "1":
        pytest.skip("requires SPGROUP_HA_LIVE_SOAK=1")
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("SP Group HA live soak must run serially; disable xdist")
    pytest_socket._remove_restrictions()

    hass.config.config_dir = str(tmp_path)
    session_file = Path(
        os.environ.get("SP_MOBILE_SESSION_FILE", ".runtime/mobile-session.json")
    )
    try:
        raw_session = await hass.async_add_executor_job(
            partial(session_file.read_text, encoding="utf-8")
        )
        session = MobileSession.from_dict(
            json.loads(raw_session)
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as err:
        pytest.skip(f"cached live session unavailable: {type(err).__name__}")

    unique_id = (
        session.identity_metadata.subject if session.identity_metadata else None
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        data={
            CONF_ACCOUNT_ID: (
                session.identity_metadata.subject
                if session.identity_metadata
                else "live-account"
            ),
            CONF_SESSION: session.to_dict(),
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert isinstance(entry.runtime_data, SpGroupRuntimeData)
    coordinator = entry.runtime_data.coordinator
    assert coordinator is not None
    assert coordinator.data is not None
    assert coordinator.data.premises

    cycles = int(os.environ.get("SPGROUP_HA_LIVE_SOAK_CYCLES", "2"))
    for _ in range(max(cycles - 1, 0)):
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    registry_entries = er.async_entries_for_config_entry(
        er.async_get(hass),
        entry.entry_id,
    )
    rendered = repr(
        {
            "diagnostics": diagnostics,
            "entity_count": len(registry_entries),
            "premise_count": len(coordinator.data.premises),
            "future_sensor_count": len(coordinator.data.future_sensor_keys()),
        }
    )
    for token_key in SENSITIVE_TOKEN_KEYS:
        assert token_key not in rendered
    assert "refresh-token" not in rendered
