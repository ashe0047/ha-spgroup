"""Shared SP Group discovery fetch service."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from spgroup.mobile import MobileSession

from .discovery import (
    SpGroupCoordinatorData,
    account_numbers_from_yggdrasil,
    build_discovery_data,
)

if TYPE_CHECKING:
    from spgroup.mobile import JarvisClient, YggdrasilClient


async def async_fetch_discovery_data(
    *,
    session: MobileSession,
    yggdrasil_client: YggdrasilClient,
    jarvis_client: JarvisClient,
    account_id: str | None,
    require_jarvis_correlation: bool = True,
) -> SpGroupCoordinatorData:
    """Fetch and normalize discovery data with the runtime client set."""
    yggdrasil_premises, jarvis_me = await asyncio.gather(
        yggdrasil_client.get_premises_details(session),
        jarvis_client.get_me(session),
    )
    account_numbers = account_numbers_from_yggdrasil(yggdrasil_premises)
    meter_types_by_account: dict[str, dict[str, Any]] = {}
    premise_types_by_account: dict[str, dict[str, Any]] = {}
    if account_numbers:
        tasks = [
            asyncio.gather(
                yggdrasil_client.get_meter_type(session, acct),
                yggdrasil_client.get_premise_type(session, acct),
            )
            for acct in account_numbers
        ]
        results = await asyncio.gather(*tasks)
        for i, (mt, pt) in enumerate(results):
            acct = account_numbers[i]
            meter_types_by_account[acct] = mt
            premise_types_by_account[acct] = pt
    return build_discovery_data(
        account_id=account_id,
        yggdrasil_premises=yggdrasil_premises,
        jarvis_me=jarvis_me,
        meter_types_by_account=meter_types_by_account,
        premise_types_by_account=premise_types_by_account,
        require_jarvis_correlation=require_jarvis_correlation,
    )


__all__ = ["async_fetch_discovery_data"]
