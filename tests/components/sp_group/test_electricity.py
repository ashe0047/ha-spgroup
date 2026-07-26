"""Test SP Group electricity semantics parsing."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from custom_components.sp_group.electricity import (
    AMI_COVERAGE_FULL,
    AMI_COVERAGE_INVALID,
    AMI_COVERAGE_NONE,
    AMI_COVERAGE_PARTIAL,
    classify_electricity_ami_month,
    compose_electricity_history_and_ami,
    parse_ami_electricity_chart,
    parse_history_electricity_chart,
    parse_history_electricity_months,
)
from custom_components.sp_group.sources import SINGAPORE_TZ


def history_chart_payload() -> dict[str, object]:
    """Return a v4 history-chart electricity fixture."""
    return {
        "elec": {
            "data": [
                {
                    "period": "2026-06",
                    "status": "ACTUAL",
                    "unit": "kWh",
                    "consumption": {"current": 123.45, "previous": 120.0},
                    "unknown": "preserved by SDK but ignored by HA parser",
                },
                {
                    "period": "2026-07",
                    "status": "ESTIMATED",
                    "unit": "kWh",
                    "consumption": {"current": "130.50"},
                },
            ]
        },
        "water": {
            "data": [
                {
                    "period": "2026-06",
                    "status": "ACTUAL",
                    "unit": "m³",
                    "consumption": {"current": 20.0, "previous": 22.0},
                },
                {
                    "period": "2026-07",
                    "status": "ESTIMATED",
                    "unit": "L",
                    "consumption": {"current": "21500"},
                },
            ]
        },
    }


def test_history_chart_parser_extracts_non_publishable_kwh_candidates() -> None:
    """History chart rows are period usage evidence, not sensor totals."""
    result = parse_history_electricity_chart(
        history_chart_payload(),
        premise_key="premise-key",
        account_key="account-key",
    )

    assert result.warnings == ()
    assert len(result.candidates) == 2
    first = result.candidates[0]
    assert first.premise_key == "premise-key"
    assert first.account_key == "account-key"
    assert first.source == "history_chart"
    assert first.period_key == "2026-06"
    assert first.status == "ACTUAL"
    assert first.original_unit == "kWh"
    assert first.value == Decimal("123.45")
    assert first.value_kwh == Decimal("123.45")
    assert first.semantic == "period_usage"
    assert first.publishable is False


def test_history_chart_parser_accepts_live_validated_apk_shape() -> None:
    """Live-validated APK chart shape stays non-publishable in HA."""
    result = parse_history_electricity_chart(
        {
            "server_time": "2026-07-14T10:00:00Z",
            "premise_type": "residential",
            "elec": {
                "comparison_type": "previous_period",
                "average_consumption": 111.1,
                "data": [
                    {
                        "period": "2026-07",
                        "status": "ACTUAL",
                        "unit": "kWh",
                        "consumption": {"current": "145.20", "previous": "120.10"},
                        "peer_comparison": {"average": 150.0, "efficient": 80.0},
                    }
                ],
            },
        },
        premise_key="premise-key",
        account_key="account-key",
    )

    assert result.warnings == ()
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.value_kwh == Decimal("145.20")
    assert candidate.source == "history_chart"
    assert candidate.semantic == "period_usage"
    assert candidate.publishable is False


def test_history_month_normalizer_uses_previous_for_validated_live_shape() -> None:
    """Actual history backfill uses previous when current is the live zero shape."""
    months = parse_history_electricity_months(
        {
            "elec": {
                "data": [
                    {
                        "period": "2026-06-01T00:00:00+08:00",
                        "status": "Actual",
                        "unit": "kWh",
                        "consumption": {"current": 0, "previous": "685.07"},
                    }
                ]
            }
        }
    )
    assert len(months) == 1
    assert months[0].total_kwh == Decimal("685.07")
    assert months[0].selected_field == "previous"


def test_month_composer_allocates_leading_prefix_exactly() -> None:
    """A partial AMI month keeps observed values and fills only its prefix."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    composition = compose_electricity_history_and_ami(
        {
            "elec": {
                "data": [
                    {
                        "period": "2026-01",
                        "status": "Actual",
                        "unit": "kWh",
                        "consumption": {"current": 0, "previous": "1488"},
                    }
                ]
            }
        },
        tuple(
            (slot, Decimal("0") if index < 2 else Decimal("1"))
            for index, slot in enumerate(slots)
        ),
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
    )
    assert composition.unresolved_months == ()
    assert len(composition.observations) == 31 * 48
    assert sum(
        (item.value for item in composition.observations), Decimal("0")
    ) == Decimal("1488")
    assert [item.provenance for item in composition.observations[:2]] == [
        "history_allocated",
        "history_allocated",
    ]
    assert all(
        item.provenance == "ami_observed" for item in composition.observations[2:]
    )


def test_month_composer_keeps_history_only_month_monthly() -> None:
    """A complete all-zero AMI month remains AMI-authoritative."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    composition = compose_electricity_history_and_ami(
        {
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
        },
        tuple((slot, Decimal("0")) for slot in slots),
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
    )
    assert composition.observations == ()
    assert composition.unresolved_months == ()


def test_ami_month_classifier_covers_full_none_partial_and_invalid() -> None:
    """AMI coverage classification is independent of history totals."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    assert (
        classify_electricity_ami_month(
            month, {slot: Decimal("0") for slot in slots}
        ).coverage
        == AMI_COVERAGE_FULL
    )
    full_with_zero_interval = {slot: Decimal("1") for slot in slots}
    full_with_zero_interval[slots[10]] = Decimal("0")
    assert (
        classify_electricity_ami_month(month, full_with_zero_interval).coverage
        == AMI_COVERAGE_FULL
    )
    assert classify_electricity_ami_month(month, {}).coverage == AMI_COVERAGE_NONE
    assert (
        classify_electricity_ami_month(
            month, {slots[0]: Decimal("0"), slots[1]: Decimal("0")}
        ).coverage
        == AMI_COVERAGE_NONE
    )
    partial_rows = {slot: Decimal("1") for slot in slots}
    partial_rows[slots[0]] = Decimal("0")
    assert (
        classify_electricity_ami_month(month, partial_rows).coverage
        == AMI_COVERAGE_PARTIAL
    )
    invalid_rows = dict(partial_rows)
    del invalid_rows[slots[1]]
    assert (
        classify_electricity_ami_month(month, invalid_rows).coverage
        == AMI_COVERAGE_INVALID
    )
    assert (
        classify_electricity_ami_month(month, partial_rows, duplicate=True).coverage
        == AMI_COVERAGE_INVALID
    )


def test_month_composer_uses_history_when_ami_is_absent() -> None:
    """A history month absent from AMI becomes one monthly bucket."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    composition = compose_electricity_history_and_ami(
        {
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
        },
        (),
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
    )
    assert [(item.value, item.provenance) for item in composition.observations] == [
        (Decimal("12"), "history_monthly")
    ]


def test_month_composer_rejects_invalid_partial_without_synthesis() -> None:
    """Positive AMI with a missing timestamp remains unresolved."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    rows = tuple(
        (slot, Decimal("0") if index == 0 else Decimal("1"))
        for index, slot in enumerate(slots)
        if index != 1
    )
    composition = compose_electricity_history_and_ami(
        {
            "elec": {
                "data": [
                    {
                        "period": "2026-01",
                        "status": "Actual",
                        "unit": "kWh",
                        "consumption": {"current": "48"},
                    }
                ]
            }
        },
        rows,
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
    )
    assert composition.observations == ()
    assert composition.unresolved_months == (month,)


def test_month_composer_prefers_full_ami_when_history_is_present() -> None:
    """Full AMI coverage never adds a history bucket."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    composition = compose_electricity_history_and_ami(
        {
            "elec": {
                "data": [
                    {
                        "period": "2026-01",
                        "status": "Actual",
                        "unit": "kWh",
                        "consumption": {"current": "999"},
                    }
                ]
            }
        },
        tuple((slot, Decimal("1")) for slot in slots),
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
    )
    assert len(composition.observations) == len(slots)
    assert all(item.provenance == "ami_observed" for item in composition.observations)
    assert composition.unresolved_months == ()


def test_history_chart_parser_reports_malformed_and_uncertain_rows() -> None:
    """Malformed history rows produce warnings and non-publishable candidates."""
    result = parse_history_electricity_chart(
        {
            "elec": {
                "data": [
                    {
                        "period": "2026-06",
                        "unit": "MWh",
                        "consumption": {"current": "bad"},
                    },
                    {
                        "period": "2026-06",
                        "unit": "kWh",
                        "consumption": {"current": 1},
                    },
                    {
                        "period": "2026-07",
                        "unit": "kWh",
                        "consumption": {"current": -1},
                    },
                    {"status": "ACTUAL", "unit": "kWh"},
                    "not-a-row",
                ]
            }
        },
        premise_key="premise-key",
        account_key="account-key",
    )

    assert result.warnings == (
        "malformed_value",
        "unsupported_unit",
        "history_duplicate_period",
        "negative_value",
        "history_missing_period",
        "history_malformed_row",
    )
    assert result.candidates[0].value is None
    assert result.candidates[0].value_kwh is None
    assert result.candidates[1].value == Decimal("-1")
    assert result.candidates[1].value_kwh is None


def test_history_chart_parser_reports_missing_sections() -> None:
    """Missing electricity sections are explicit parser warnings."""
    assert parse_history_electricity_chart(
        {},
        premise_key="premise-key",
        account_key="account-key",
    ).warnings == ("history_missing_elec",)
    assert parse_history_electricity_chart(
        {"elec": {}},
        premise_key="premise-key",
        account_key="account-key",
    ).warnings == ("history_missing_data",)


def test_ami_chart_parser_supports_fixture_semantics_only() -> None:
    """AMI parser extracts bucket evidence without enabling AMI polling."""
    result = parse_ami_electricity_chart(
        {
            "unit": "kWh",
            "history": [
                {
                    "data": [
                        {"date": "2026-06-01", "consumption": 1.25},
                        {"date": "2026-06-02", "amount": "2.50"},
                    ]
                }
            ],
        },
        premise_key="premise-key",
        account_key="account-key",
    )

    assert result.warnings == ()
    assert [candidate.value_kwh for candidate in result.candidates] == [
        Decimal("1.25"),
        Decimal("2.50"),
    ]
    assert {candidate.source for candidate in result.candidates} == {"ami_chart"}
    assert {candidate.semantic for candidate in result.candidates} == {"chart_bucket"}
    assert all(not candidate.publishable for candidate in result.candidates)


def test_ami_chart_parser_accepts_live_validated_apk_shape() -> None:
    """AMI chart DTO shape remains chart-bucket evidence only."""
    result = parse_ami_electricity_chart(
        {
            "daily_limit": "10",
            "unit": "kWh",
            "history": [
                {
                    "next": False,
                    "previous": True,
                    "start": "20260701000000",
                    "end": "20260714235959",
                    "data": [
                        {
                            "date": "2026-07-01",
                            "amount": "1.20",
                            "consumption": 1.25,
                            "metadata": [
                                {
                                    "date": "2026-07-01",
                                    "anomaly": None,
                                    "tags": [],
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        premise_key="premise-key",
        account_key="account-key",
    )

    assert result.warnings == ()
    assert result.candidates[0].value_kwh == Decimal("1.25")
    assert result.candidates[0].semantic == "chart_bucket"
    assert result.candidates[0].publishable is False


def test_ami_chart_parser_reports_uncertain_buckets() -> None:
    """AMI malformed fixture data remains non-publishable evidence."""
    result = parse_ami_electricity_chart(
        {
            "unit": "Wh",
            "history": [
                {
                    "data": [
                        {"amount": "1.0"},
                        {"date": "2026-06-01", "amount": "bad"},
                    ]
                }
            ],
        },
        premise_key="premise-key",
        account_key="account-key",
    )

    assert result.warnings == (
        "unsupported_unit",
        "ami_missing_date",
        "malformed_value",
    )
    assert result.candidates[0].value is None
    assert result.candidates[0].value_kwh is None
