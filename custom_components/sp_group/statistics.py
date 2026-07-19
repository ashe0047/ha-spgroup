"""Statistics eligibility policy for SP Group sensors."""

from __future__ import annotations

from .discovery import SpGroupCoordinatorData, SpGroupFutureSensorKey

_CURRENT_CHART_SOURCES = frozenset({"history_chart", "ami_chart"})
_CURRENT_CHART_SEMANTICS = frozenset(
    {
        "period_usage",
        "premise_period_usage",
        "chart_bucket",
    }
)


def has_publishable_statistics_state(
    data: SpGroupCoordinatorData,
    sensor_key: SpGroupFutureSensorKey,
) -> bool:
    """Return whether a sensor has a safe long-term-statistics state.

    HA-8 is intentionally a negative gate: current SP chart candidates are
    period or bucket evidence, not proven cumulative/reset-aware totals.
    Unknown future semantics must be explicitly enabled in a later milestone.
    """
    if sensor_key.key not in {key.key for key in data.future_sensor_keys()}:
        return False

    if sensor_key.utility_type == "electric":
        return any(
            _is_safe_statistics_candidate(candidate.source, candidate.semantic)
            for candidate in data.electricity_candidates
            if candidate.premise_key == sensor_key.premise_key
            and candidate.publishable
        )

    if sensor_key.utility_type == "water":
        return any(
            _is_safe_statistics_candidate(candidate.source, candidate.semantic)
            for candidate in data.water_candidates
            if candidate.premise_key == sensor_key.premise_key
            and candidate.publishable
        )

    return False


def _is_safe_statistics_candidate(source: str, semantic: str) -> bool:
    """Return whether a candidate source/semantic is safe for HA statistics."""
    if source in _CURRENT_CHART_SOURCES:
        return False
    if semantic in _CURRENT_CHART_SEMANTICS:
        return False
    return False


__all__ = ["has_publishable_statistics_state"]
