"""Test SP Group water semantics parsing."""

from __future__ import annotations

from decimal import Decimal

from custom_components.sp_group.water import (
    parse_ami_water_chart,
    parse_history_water_chart,
)

from .test_electricity import history_chart_payload


def test_history_water_parser_extracts_non_publishable_volume_candidates() -> None:
    """History chart rows are premise-period evidence, not sensor totals."""
    result = parse_history_water_chart(
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
    assert first.original_unit == "m³"
    assert first.value == Decimal("20.0")
    assert first.value_cubic_meters == Decimal("20.0")
    assert first.semantic == "premise_period_usage"
    assert first.publishable is False
    assert result.candidates[1].value_cubic_meters == Decimal("21.5")


def test_history_water_parser_accepts_live_validated_apk_shape() -> None:
    """Live-validated APK chart shape stays non-publishable in HA."""
    result = parse_history_water_chart(
        {
            "server_time": "2026-07-14T10:00:00Z",
            "premise_type": "residential",
            "water": {
                "comparison_type": "previous_period",
                "average_consumption": 4.1,
                "data": [
                    {
                        "period": "2026-07",
                        "status": "ACTUAL",
                        "unit": "m³",
                        "consumption": {"current": "5.20", "previous": "4.10"},
                        "peer_comparison": {"average": 6.0, "efficient": 3.0},
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
    assert candidate.value_cubic_meters == Decimal("5.20")
    assert candidate.source == "history_chart"
    assert candidate.semantic == "premise_period_usage"
    assert candidate.publishable is False


def test_history_water_parser_supports_m3_text_variants() -> None:
    """Known cubic-meter spellings normalize without lossy conversion."""
    result = parse_history_water_chart(
        {
            "water": {
                "data": [
                    {
                        "period": "2026-06",
                        "unit": "m3",
                        "consumption": {"current": "1.25"},
                    },
                    {
                        "period": "2026-07",
                        "unit": "cubic metres",
                        "consumption": {"current": "2.50"},
                    },
                ]
            }
        },
        premise_key="premise-key",
        account_key="account-key",
    )

    assert result.warnings == ()
    assert [candidate.value_cubic_meters for candidate in result.candidates] == [
        Decimal("1.25"),
        Decimal("2.50"),
    ]


def test_history_water_parser_reports_malformed_and_uncertain_rows() -> None:
    """Malformed history rows produce fixed warnings and non-publishable candidates."""
    result = parse_history_water_chart(
        {
            "water": {
                "data": [
                    {
                        "period": "2026-06",
                        "unit": "gal",
                        "consumption": {"current": "bad"},
                    },
                    {
                        "period": "2026-06",
                        "unit": "m³",
                        "consumption": {"current": 1},
                    },
                    {
                        "period": "2026-07",
                        "unit": "m³",
                        "consumption": {"current": -1},
                    },
                    {"status": "ACTUAL", "unit": "m³"},
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
    assert result.candidates[0].value_cubic_meters is None
    assert result.candidates[1].value == Decimal("-1")
    assert result.candidates[1].value_cubic_meters is None
    assert all("gal" not in warning for warning in result.warnings)


def test_history_water_parser_reports_missing_sections() -> None:
    """Missing water sections are explicit parser warnings."""
    assert parse_history_water_chart(
        {},
        premise_key="premise-key",
        account_key="account-key",
    ).warnings == ("history_missing_water",)
    assert parse_history_water_chart(
        {"water": {}},
        premise_key="premise-key",
        account_key="account-key",
    ).warnings == ("history_missing_data",)


def test_ami_water_parser_supports_fixture_semantics_only() -> None:
    """AMI parser extracts bucket evidence without enabling AMI polling."""
    result = parse_ami_water_chart(
        {
            "unit": "L",
            "history": [
                {
                    "data": [
                        {"date": "2026-06-01", "consumption": 1250},
                        {"date": "2026-06-02", "amount": "2500"},
                    ]
                }
            ],
        },
        premise_key="premise-key",
        account_key="account-key",
    )

    assert result.warnings == ()
    assert [candidate.value_cubic_meters for candidate in result.candidates] == [
        Decimal("1.25"),
        Decimal("2.5"),
    ]
    assert {candidate.source for candidate in result.candidates} == {"ami_chart"}
    assert {candidate.semantic for candidate in result.candidates} == {"chart_bucket"}
    assert all(not candidate.publishable for candidate in result.candidates)


def test_ami_water_parser_accepts_live_validated_apk_shape() -> None:
    """AMI water DTO shape remains chart-bucket evidence only."""
    result = parse_ami_water_chart(
        {
            "daily_limit": "10",
            "unit": "L",
            "history": [
                {
                    "next": False,
                    "previous": True,
                    "start": "20260701000000",
                    "end": "20260714235959",
                    "data": [
                        {
                            "date": "2026-07-01",
                            "amount": "1200",
                            "consumption": 1250,
                            "metadata": [],
                        }
                    ],
                }
            ],
        },
        premise_key="premise-key",
        account_key="account-key",
    )

    assert result.warnings == ()
    assert result.candidates[0].value_cubic_meters == Decimal("1.25")
    assert result.candidates[0].semantic == "chart_bucket"
    assert result.candidates[0].publishable is False


def test_ami_water_parser_reports_uncertain_buckets() -> None:
    """AMI malformed fixture data remains non-publishable evidence."""
    result = parse_ami_water_chart(
        {
            "unit": "ft³",
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
    assert result.candidates[0].value_cubic_meters is None
