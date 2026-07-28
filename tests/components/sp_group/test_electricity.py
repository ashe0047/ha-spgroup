"""Test SP Group electricity semantics parsing."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from custom_components.sp_group.electricity import (
    AMI_COVERAGE_FULL,
    AMI_COVERAGE_INVALID,
    AMI_COVERAGE_NONE,
    AMI_COVERAGE_PARTIAL,
    ElectricitySourceError,
    classify_electricity_ami_month,
    compose_electricity_history_and_ami,
    normalize_history_electricity_months,
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


def test_strict_history_normalizer_accepts_estimated_status() -> None:
    """Estimated provider totals are valid backfill evidence and retain status."""
    months = normalize_history_electricity_months(
        {
            "elec": {
                "data": [
                    {
                        "period": "2026-04",
                        "status": "Estimated",
                        "unit": "kWh",
                        "consumption": {"current": 0, "previous": "637.59"},
                    }
                ]
            }
        }
    )
    assert [(item.total_kwh, item.selected_field, item.status) for item in months] == [
        (Decimal("637.59"), "previous", "ESTIMATED")
    ]


def test_strict_history_normalizer_rejects_malformed_actual_row() -> None:
    """Malformed Actual History evidence fails closed for composition."""
    with pytest.raises(ElectricitySourceError, match="history_invalid_consumption"):
        normalize_history_electricity_months(
            {
                "elec": {
                    "data": [
                        {
                            "period": "2026-06",
                            "status": "Actual",
                            "unit": "kWh",
                            "consumption": {"current": "not-a-number"},
                        }
                    ]
                }
            }
        )


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
    assert all(item.source_status == "ACTUAL" for item in composition.observations[:2])
    assert all(
        item.provenance == "ami_observed" for item in composition.observations[2:]
    )


def test_month_composer_uses_history_for_zero_only_ami_month() -> None:
    """A complete all-zero AMI month has no usable AMI coverage."""
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
    assert [(item.value, item.provenance) for item in composition.observations] == [
        (Decimal("12"), "history_monthly")
    ]
    assert composition.unresolved_months == ()


def test_ami_month_classifier_covers_full_none_partial_and_invalid() -> None:
    """AMI coverage classification is independent of history totals."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    assert (
        classify_electricity_ami_month(
            month, {slot: Decimal("0") for slot in slots}
        ).coverage
        == AMI_COVERAGE_NONE
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
        == AMI_COVERAGE_PARTIAL
    )
    assert (
        classify_electricity_ami_month(month, partial_rows, duplicate=True).coverage
        == AMI_COVERAGE_INVALID
    )


def test_terminal_complete_grid_with_zero_suffix_is_partial() -> None:
    """Terminal explicit-zero placeholders do not establish full coverage."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(16))
    rows = {
        slot: Decimal("1") if index < 8 else Decimal("0")
        for index, slot in enumerate(slots)
    }

    classification = classify_electricity_ami_month(
        month,
        rows,
        window_start=month,
        window_end=month + timedelta(hours=8),
    )

    assert classification.coverage == AMI_COVERAGE_PARTIAL


def test_terminal_explicit_zero_suffix_becomes_provider_lag() -> None:
    """Explicit terminal placeholders are withheld for normal retry."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(16))
    rows = tuple(
        (slot, Decimal("1") if index < 8 else Decimal("0"))
        for index, slot in enumerate(slots)
    )

    composition = compose_electricity_history_and_ami(
        {"elec": {"data": []}},
        rows,
        backfill_from=month,
        now=month + timedelta(hours=8),
    )

    assert tuple(item.start for item in composition.observations) == slots[:8]
    assert composition.terminal_lag_ranges == (
        (slots[8], slots[-1] + timedelta(minutes=30)),
    )
    assert all(item.value > 0 for item in composition.observations)


def test_non_terminal_explicit_zero_suffix_remains_accepted() -> None:
    """Trailing zeros in a closed non-terminal month remain valid coverage."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(16))
    rows = tuple(
        (slot, Decimal("1") if index < 8 else Decimal("0"))
        for index, slot in enumerate(slots)
    )

    composition = compose_electricity_history_and_ami(
        {"elec": {"data": []}},
        rows,
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
    )

    assert composition.terminal_lag_ranges == ()
    assert len(composition.observations) == 31 * 48
    assert all(
        item.provenance == "ami_observed_zero"
        for item in composition.observations[8:16]
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


def test_month_composer_marks_leading_no_source_month_unavailable() -> None:
    """A leading month with no source evidence does not become zero usage."""
    composition = compose_electricity_history_and_ami(
        {"elec": {"data": []}},
        (),
        backfill_from=datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
        now=datetime(2026, 3, 2, tzinfo=SINGAPORE_TZ),
    )
    assert composition.observations
    assert [
        (item.start, item.end, item.cadence, item.reason)
        for item in composition.known_unavailable_ranges
    ] == [
        (
            datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
            datetime(2026, 2, 1, tzinfo=SINGAPORE_TZ),
            "month",
            "no_source_leading_month",
        )
    ]
    assert all(
        item.provenance == "no_source_gap_zero" for item in composition.observations
    )


def test_month_composer_partial_without_history_uses_zero_gaps() -> None:
    """Non-terminal partial AMI gaps are accepted without History."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    rows = tuple(
        (slot, Decimal("1"))
        for index, slot in enumerate(slots)
        if index not in {0, 10, len(slots) - 1}
    )
    composition = compose_electricity_history_and_ami(
        {"elec": {"data": []}},
        rows,
        backfill_from=month,
        now=datetime(2026, 3, 2, tzinfo=SINGAPORE_TZ),
    )
    assert composition.terminal_lag_ranges == ()
    january = tuple(
        item
        for item in composition.observations
        if item.start < datetime(2026, 2, 1, tzinfo=SINGAPORE_TZ)
    )
    assert len(january) == len(slots) - 1
    assert composition.known_unavailable_ranges[0].reason == "ami_leading_unavailable"
    assert next(item for item in january if item.start == slots[10]).provenance == (
        "ami_gap_zero"
    )
    assert january[-1].provenance == "ami_gap_zero"


def test_month_composer_partial_leading_prefix_is_unavailable_without_history() -> None:
    """A leading AMI prefix is not persisted as zero usage without History."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(48))
    composition = compose_electricity_history_and_ami(
        {"elec": {"data": []}},
        tuple((slot, Decimal("1")) for slot in slots[2:]),
        backfill_from=month,
        now=month + timedelta(days=1),
    )
    assert tuple(item.start for item in composition.observations) == slots[2:]
    assert composition.known_unavailable_ranges[0].cadence == "30m"
    assert composition.known_unavailable_ranges[0].reason == "ami_leading_unavailable"


def test_month_composer_zero_history_remainder_keeps_prefix_unavailable() -> None:
    """A zero History remainder does not turn the leading prefix into zeros."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(48))
    composition = compose_electricity_history_and_ami(
        {
            "elec": {
                "data": [
                    {
                        "period": "2026-01",
                        "status": "Actual",
                        "unit": "kWh",
                        "consumption": {"current": "46"},
                    }
                ]
            }
        },
        tuple(
            (slot, Decimal("0") if index < 2 else Decimal("1"))
            for index, slot in enumerate(slots)
        ),
        backfill_from=month,
        now=month + timedelta(days=1),
    )
    assert len(composition.observations) == 46
    assert all(item.provenance == "ami_observed" for item in composition.observations)
    assert composition.known_unavailable_ranges[0].reason == "ami_leading_unavailable"


def test_ami_classifier_uses_clipped_window_for_full_coverage() -> None:
    """A mid-month polling window does not invent a leading prefix."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(48))
    assert (
        classify_electricity_ami_month(
            month,
            {slot: Decimal("1") for slot in slots[10:]},
            window_start=slots[10],
            window_end=month + timedelta(days=1),
        ).coverage
        == AMI_COVERAGE_FULL
    )


def test_month_composer_terminal_trailing_gap_is_the_only_unresolved_range() -> None:
    """Completed terminal gaps remain retryable provider lag."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    rows = tuple(
        (month + timedelta(minutes=30 * index), Decimal("1")) for index in range(10)
    )
    composition = compose_electricity_history_and_ami(
        {"elec": {"data": []}},
        rows,
        backfill_from=month,
        now=datetime(2026, 1, 1, 8, 0, tzinfo=SINGAPORE_TZ),
    )
    assert composition.known_unavailable_ranges == ()
    assert composition.terminal_lag_ranges == (
        (
            datetime(2026, 1, 1, 5, 0, tzinfo=SINGAPORE_TZ),
            datetime(2026, 1, 1, 8, 0, tzinfo=SINGAPORE_TZ),
        ),
    )


def test_month_composer_rejects_history_remainder_without_prefix() -> None:
    """A positive remainder cannot disappear when AMI starts at month start."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    with pytest.raises(ElectricitySourceError, match="without_prefix"):
        compose_electricity_history_and_ami(
            {
                "elec": {
                    "data": [
                        {
                            "period": "2026-01",
                            "status": "Actual",
                            "unit": "kWh",
                            "consumption": {"current": "2000"},
                        }
                    ]
                }
            },
            tuple(
                (slot, Decimal("1")) for index, slot in enumerate(slots) if index != 10
            ),
            backfill_from=month,
            now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
        )


def test_month_composer_accepts_middle_gap_as_zero() -> None:
    """A missing middle timestamp is accepted as an inferred zero bucket."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    rows = tuple(
        (slot, Decimal("1")) for index, slot in enumerate(slots) if index != 10
    )
    composition = compose_electricity_history_and_ami(
        {
            "elec": {
                "data": [
                    {
                        "period": "2026-01",
                        "status": "Actual",
                        "unit": "kWh",
                        "consumption": {"current": "1487"},
                    }
                ]
            }
        },
        rows,
        backfill_from=month,
        now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
    )
    assert len(composition.observations) == len(slots)
    missing = composition.observations[10]
    assert missing.value == 0
    assert missing.provenance == "ami_gap_zero"
    assert composition.unresolved_months == ()


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


@pytest.mark.parametrize(
    ("consumption", "total", "field"),
    [
        ({"current": 0, "previous": "bad"}, Decimal("0"), "current"),
        ({"current": 0, "previous": 0}, Decimal("0"), "current"),
        ({"current": "12.5", "previous": "99"}, Decimal("12.5"), "current"),
    ],
)
def test_strict_history_normalizer_falls_back_to_valid_current(
    consumption: dict[str, object], total: Decimal, field: str
) -> None:
    """A non-positive or invalid previous value does not replace current."""
    months = normalize_history_electricity_months(
        {
            "elec": {
                "data": [
                    {
                        "period": "2026-06",
                        "status": "Actual",
                        "unit": "kWh",
                        "consumption": consumption,
                    }
                ]
            }
        }
    )
    assert [(item.total_kwh, item.selected_field) for item in months] == [
        (total, field)
    ]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            {
                "elec": {
                    "data": [
                        {
                            "period": "2026-06",
                            "status": "Actual",
                            "unit": "MWh",
                            "consumption": {"current": 1},
                        }
                    ]
                }
            },
            "period_or_unit",
        ),
        (
            {
                "elec": {
                    "data": [
                        {
                            "period": "not-a-month",
                            "status": "Actual",
                            "unit": "kWh",
                            "consumption": {"current": 1},
                        }
                    ]
                }
            },
            "period_or_unit",
        ),
        (
            {
                "elec": {
                    "data": [
                        {
                            "period": "2026-06",
                            "status": "Actual",
                            "unit": "kWh",
                            "consumption": {"current": -1},
                        }
                    ]
                }
            },
            "negative_or_missing",
        ),
        (
            {
                "elec": {
                    "data": [
                        {
                            "period": "2026-06",
                            "status": "Actual",
                            "unit": "kWh",
                            "consumption": {"current": 1},
                        },
                        {
                            "period": "2026-06-01T00:00:00+08:00",
                            "status": "Actual",
                            "unit": "kWh",
                            "consumption": {"current": 2},
                        },
                    ]
                }
            },
            "duplicate_period",
        ),
    ],
)
def test_strict_history_normalizer_rejects_invalid_actual_evidence(
    payload: dict[str, object], reason: str
) -> None:
    """Invalid Actual History evidence fails closed with a stable reason."""
    with pytest.raises(ElectricitySourceError, match=reason):
        normalize_history_electricity_months(payload)


def test_month_composer_negative_history_remainder_fails_closed() -> None:
    """AMI totals above History totals cannot be silently reconciled."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    with pytest.raises(ElectricitySourceError, match="negative_history_remainder"):
        compose_electricity_history_and_ami(
            {
                "elec": {
                    "data": [
                        {
                            "period": "2026-01",
                            "status": "Actual",
                            "unit": "kWh",
                            "consumption": {"current": "10"},
                        }
                    ]
                }
            },
            tuple((slot, Decimal("1")) for slot in slots[:-1]),
            backfill_from=month,
            now=datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ),
        )


def test_month_composer_allocation_is_deterministic_at_fixed_precision() -> None:
    """Uneven remainders are distributed reproducibly and sum exactly."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(31 * 48))
    ami = tuple((slot, Decimal("1")) for slot in slots[3:])
    payload = {
        "elec": {
            "data": [
                {
                    "period": "2026-01",
                    "status": "Actual",
                    "unit": "kWh",
                    "consumption": {"current": "1488.0000000007"},
                }
            ]
        }
    }
    kwargs = {"backfill_from": month, "now": datetime(2026, 2, 2, tzinfo=SINGAPORE_TZ)}
    first = compose_electricity_history_and_ami(payload, ami, **kwargs)
    second = compose_electricity_history_and_ami(payload, ami, **kwargs)
    assert first.observations == second.observations
    allocated = first.observations[:3]
    assert all(item.value.as_tuple().exponent <= -10 for item in allocated)
    assert sum((item.value for item in allocated), Decimal("0")) == Decimal(
        "3.0000000007"
    )


def test_month_composer_terminal_leading_and_middle_gaps_are_zero() -> None:
    """Only a terminal trailing suffix remains unresolved."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(16))
    rows = tuple(
        (slot, Decimal("1"))
        for index, slot in enumerate(slots)
        if index not in {0, 4, 12, 13, 14, 15}
    )
    composition = compose_electricity_history_and_ami(
        {"elec": {"data": []}},
        rows,
        backfill_from=month,
        now=datetime(2026, 1, 1, 8, 0, tzinfo=SINGAPORE_TZ),
    )
    assert composition.terminal_lag_ranges == (
        (
            datetime(2026, 1, 1, 6, 0, tzinfo=SINGAPORE_TZ),
            datetime(2026, 1, 1, 8, 0, tzinfo=SINGAPORE_TZ),
        ),
    )
    assert composition.observations[0].start == slots[1]
    assert composition.observations[3].provenance == "ami_gap_zero"


def test_month_composer_later_no_source_month_is_explicit_zero_timeline() -> None:
    """A missing non-leading closed month advances through explicit zeros."""
    composition = compose_electricity_history_and_ami(
        {"elec": {"data": []}},
        (),
        backfill_from=datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
        now=datetime(2026, 3, 2, tzinfo=SINGAPORE_TZ),
    )
    february = tuple(item for item in composition.observations if item.start.month == 2)
    assert len(february) == 28 * 48
    assert all(item.value == 0 for item in february)
    assert all(item.provenance == "no_source_gap_zero" for item in february)
    assert composition.known_unavailable_ranges[0].reason == "no_source_leading_month"


def test_month_composer_repeat_is_idempotent_with_missing_history_month() -> None:
    """A valid History response missing a requested month remains deterministic."""
    payload = {
        "elec": {
            "data": [
                {
                    "period": "2026-02",
                    "status": "Actual",
                    "unit": "kWh",
                    "consumption": {"current": "2"},
                }
            ]
        }
    }
    kwargs = {
        "backfill_from": datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ),
        "now": datetime(2026, 3, 2, tzinfo=SINGAPORE_TZ),
    }
    assert compose_electricity_history_and_ami(
        payload, (), **kwargs
    ) == compose_electricity_history_and_ami(payload, (), **kwargs)


def test_sparse_zero_only_ami_with_history_uses_history_monthly_bucket() -> None:
    """Placeholder-only AMI does not override valid monthly History."""
    month = datetime(2026, 1, 1, tzinfo=SINGAPORE_TZ)
    slots = tuple(month + timedelta(minutes=30 * index) for index in range(2))
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
    assert composition.observations == (composition.observations[0],)
    assert composition.observations[0].provenance == "history_monthly"
    assert composition.observations[0].value == Decimal("12")


def test_composer_clips_observations_to_mid_month_polling_window() -> None:
    """No inferred bucket is created before or after a clipped request window."""
    window_start = datetime(2026, 1, 2, 1, 0, tzinfo=SINGAPORE_TZ)
    window_end = datetime(2026, 1, 2, 3, 0, tzinfo=SINGAPORE_TZ)
    rows = tuple(
        (window_start + timedelta(minutes=30 * index), Decimal("1"))
        for index in range(2)
    )
    composition = compose_electricity_history_and_ami(
        {"elec": {"data": []}},
        rows,
        backfill_from=window_start,
        now=window_end,
    )
    assert all(item.start >= window_start for item in composition.observations)
    assert all(item.end <= window_end for item in composition.observations)
    assert all(
        start >= window_start and end <= window_end
        for start, end in composition.terminal_lag_ranges
    )


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
