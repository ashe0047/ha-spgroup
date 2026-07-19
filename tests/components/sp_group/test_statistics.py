"""Test SP Group statistics eligibility policy."""

from __future__ import annotations

from decimal import Decimal

import pytest
from custom_components.sp_group.discovery import (
    SpGroupCoordinatorData,
    SpGroupFutureSensorKey,
    SpGroupPremise,
    SpGroupUtilityAccount,
)
from custom_components.sp_group.electricity import SpGroupElectricityCandidate
from custom_components.sp_group.statistics import has_publishable_statistics_state
from custom_components.sp_group.water import SpGroupWaterCandidate


@pytest.mark.parametrize(
    "candidate",
    [
        SpGroupElectricityCandidate(
            premise_key="premise-1",
            account_key="account-1",
            source="history_chart",
            period_key="2026-07",
            status="ACTUAL",
            original_unit="kWh",
            value=Decimal("12.3"),
            value_kwh=Decimal("12.3"),
            semantic="period_usage",
            publishable=True,
        ),
        SpGroupElectricityCandidate(
            premise_key="premise-1",
            account_key="account-1",
            source="ami_chart",
            period_key="20260714",
            status=None,
            original_unit="kWh",
            value=Decimal("1.23"),
            value_kwh=Decimal("1.23"),
            semantic="chart_bucket",
            publishable=True,
        ),
    ],
)
def test_electricity_chart_candidates_are_not_statistics_eligible(
    candidate: SpGroupElectricityCandidate,
) -> None:
    """Current electricity chart evidence is not a safe HA statistics state."""
    data = coordinator_data(electricity_candidates=(candidate,))

    assert not has_publishable_statistics_state(
        data,
        SpGroupFutureSensorKey(
            key="premise-1_electric",
            premise_key="premise-1",
            utility_type="electric",
        ),
    )


@pytest.mark.parametrize(
    "candidate",
    [
        SpGroupWaterCandidate(
            premise_key="premise-1",
            account_key="account-1",
            source="history_chart",
            period_key="2026-07",
            status="ACTUAL",
            original_unit="m³",
            value=Decimal("4.2"),
            value_cubic_meters=Decimal("4.2"),
            semantic="premise_period_usage",
            publishable=True,
        ),
        SpGroupWaterCandidate(
            premise_key="premise-1",
            account_key="account-1",
            source="ami_chart",
            period_key="20260714",
            status=None,
            original_unit="L",
            value=Decimal("4200"),
            value_cubic_meters=Decimal("4.2"),
            semantic="chart_bucket",
            publishable=True,
        ),
    ],
)
def test_water_chart_candidates_are_not_statistics_eligible(
    candidate: SpGroupWaterCandidate,
) -> None:
    """Current water chart evidence is not a safe HA statistics state."""
    data = coordinator_data(water_candidates=(candidate,))

    assert not has_publishable_statistics_state(
        data,
        SpGroupFutureSensorKey(
            key="premise-1_water",
            premise_key="premise-1",
            utility_type="water",
        ),
    )


def test_unknown_future_semantics_are_rejected_by_default() -> None:
    """Future semantics need an explicit positive gate before HA statistics."""
    candidate = SpGroupElectricityCandidate(
        premise_key="premise-1",
        account_key="account-1",
        source="future_source",
        period_key="current",
        status=None,
        original_unit="kWh",
        value=Decimal("12.3"),
        value_kwh=Decimal("12.3"),
        semantic="future_total",
        publishable=True,
    )

    assert not has_publishable_statistics_state(
        coordinator_data(electricity_candidates=(candidate,)),
        SpGroupFutureSensorKey(
            key="premise-1_electric",
            premise_key="premise-1",
            utility_type="electric",
        ),
    )


def test_missing_discovery_key_is_not_statistics_eligible() -> None:
    """Candidate data alone cannot create a statistics state."""
    candidate = SpGroupElectricityCandidate(
        premise_key="premise-1",
        account_key="account-1",
        source="history_chart",
        period_key="2026-07",
        status="ACTUAL",
        original_unit="kWh",
        value=Decimal("12.3"),
        value_kwh=Decimal("12.3"),
        semantic="period_usage",
        publishable=True,
    )

    assert not has_publishable_statistics_state(
        SpGroupCoordinatorData(electricity_candidates=(candidate,)),
        SpGroupFutureSensorKey(
            key="premise-1_electric",
            premise_key="premise-1",
            utility_type="electric",
        ),
    )


def coordinator_data(
    *,
    electricity_candidates: tuple[SpGroupElectricityCandidate, ...] = (),
    water_candidates: tuple[SpGroupWaterCandidate, ...] = (),
) -> SpGroupCoordinatorData:
    """Return coordinator data with one electric and water premise."""
    return SpGroupCoordinatorData(
        premises=(
            SpGroupPremise(
                key="premise-1",
                active=True,
                signed_up=True,
                shared=False,
                contestable_type=None,
                accounts=(
                    SpGroupUtilityAccount(
                        key="account-1",
                        premise_key="premise-1",
                        utility_types=("ELECTRIC", "WATER"),
                    ),
                ),
            ),
        ),
        electricity_candidates=electricity_candidates,
        water_candidates=water_candidates,
    )
