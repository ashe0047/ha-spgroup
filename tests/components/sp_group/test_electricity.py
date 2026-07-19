"""Test SP Group electricity semantics parsing."""

from __future__ import annotations

from decimal import Decimal

from custom_components.sp_group.electricity import (
    parse_ami_electricity_chart,
    parse_history_electricity_chart,
)


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
