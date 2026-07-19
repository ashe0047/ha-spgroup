"""Electricity payload semantics for the SP Group integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

_SOURCE_HISTORY_CHART = "history_chart"
_SOURCE_AMI_CHART = "ami_chart"
_SEMANTIC_PERIOD_USAGE = "period_usage"
_SEMANTIC_CHART_BUCKET = "chart_bucket"
_SUPPORTED_KWH_UNITS = {"kwh", "kw h", "kilowatt-hour", "kilowatt-hours"}


@dataclass(frozen=True, slots=True)
class SpGroupElectricityCandidate:
    """Non-publishable electricity usage candidate parsed from SP payloads."""

    premise_key: str
    account_key: str
    source: str
    period_key: str
    status: str | None
    original_unit: str | None
    value: Decimal | None
    value_kwh: Decimal | None
    semantic: str
    publishable: bool = False


@dataclass(frozen=True, slots=True)
class SpGroupElectricityParseResult:
    """Result of parsing electricity payloads."""

    candidates: tuple[SpGroupElectricityCandidate, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def parse_history_electricity_chart(
    payload: dict[str, Any],
    *,
    premise_key: str,
    account_key: str,
) -> SpGroupElectricityParseResult:
    """Parse `/jarvis/v4/charts/{premise_id}` electricity chart data."""
    elec = payload.get("elec")
    if not isinstance(elec, dict):
        return SpGroupElectricityParseResult(warnings=("history_missing_elec",))
    rows = elec.get("data")
    if not isinstance(rows, list):
        return SpGroupElectricityParseResult(warnings=("history_missing_data",))

    candidates: list[SpGroupElectricityCandidate] = []
    warnings: list[str] = []
    seen_periods: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            warnings.append("history_malformed_row")
            continue
        period_key = _as_string(row.get("period"))
        if period_key is None:
            warnings.append("history_missing_period")
            continue
        if period_key in seen_periods:
            warnings.append("history_duplicate_period")
            continue
        seen_periods.add(period_key)

        unit = _as_string(row.get("unit"))
        consumption = row.get("consumption")
        current = consumption.get("current") if isinstance(consumption, dict) else None
        value, value_warning = _parse_decimal(current)
        unit_warning = _unit_warning(unit)
        normalized = value if value is not None and unit_warning is None else None
        if value is not None and value < 0:
            value_warning = "negative_value"
            normalized = None
        warnings.extend(
            warning
            for warning in (value_warning, unit_warning)
            if warning is not None
        )
        candidates.append(
            SpGroupElectricityCandidate(
                premise_key=premise_key,
                account_key=account_key,
                source=_SOURCE_HISTORY_CHART,
                period_key=period_key,
                status=_as_string(row.get("status")),
                original_unit=unit,
                value=value,
                value_kwh=normalized,
                semantic=_SEMANTIC_PERIOD_USAGE,
            )
        )

    return SpGroupElectricityParseResult(
        candidates=tuple(candidates),
        warnings=tuple(warnings),
    )


def parse_ami_electricity_chart(
    payload: dict[str, Any],
    *,
    premise_key: str,
    account_key: str,
) -> SpGroupElectricityParseResult:
    """Parse AMI electricity chart fixtures without enabling AMI polling."""
    unit = _as_string(payload.get("unit"))
    history = payload.get("history")
    if not isinstance(history, list):
        return SpGroupElectricityParseResult(warnings=("ami_missing_history",))

    candidates: list[SpGroupElectricityCandidate] = []
    warnings: list[str] = []
    seen_periods: set[str] = set()
    unit_warning = _unit_warning(unit)
    if unit_warning is not None:
        warnings.append(unit_warning)

    for group in history:
        if not isinstance(group, dict):
            warnings.append("ami_malformed_history")
            continue
        rows = group.get("data")
        if not isinstance(rows, list):
            warnings.append("ami_missing_data")
            continue
        for row in rows:
            if not isinstance(row, dict):
                warnings.append("ami_malformed_row")
                continue
            period_key = _as_string(row.get("date"))
            if period_key is None:
                warnings.append("ami_missing_date")
                continue
            if period_key in seen_periods:
                warnings.append("ami_duplicate_period")
                continue
            seen_periods.add(period_key)
            value, value_warning = _parse_decimal(
                row.get("consumption", row.get("amount"))
            )
            normalized = value if value is not None and unit_warning is None else None
            if value is not None and value < 0:
                value_warning = "negative_value"
                normalized = None
            if value_warning is not None:
                warnings.append(value_warning)
            candidates.append(
                SpGroupElectricityCandidate(
                    premise_key=premise_key,
                    account_key=account_key,
                    source=_SOURCE_AMI_CHART,
                    period_key=period_key,
                    status=None,
                    original_unit=unit,
                    value=value,
                    value_kwh=normalized,
                    semantic=_SEMANTIC_CHART_BUCKET,
                )
            )

    return SpGroupElectricityParseResult(
        candidates=tuple(candidates),
        warnings=tuple(warnings),
    )


def _parse_decimal(value: object) -> tuple[Decimal | None, str | None]:
    if isinstance(value, bool) or value is None:
        return None, "missing_value"
    if isinstance(value, int | str):
        raw_value = str(value)
    elif isinstance(value, float):
        raw_value = repr(value)
    elif isinstance(value, Decimal):
        raw_value = str(value)
    else:
        return None, "malformed_value"
    try:
        return Decimal(raw_value), None
    except InvalidOperation:
        return None, "malformed_value"


def _unit_warning(unit: str | None) -> str | None:
    if unit is None:
        return "missing_unit"
    if unit.strip().lower() not in _SUPPORTED_KWH_UNITS:
        return "unsupported_unit"
    return None


def _as_string(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


__all__ = [
    "SpGroupElectricityCandidate",
    "SpGroupElectricityParseResult",
    "parse_ami_electricity_chart",
    "parse_history_electricity_chart",
]
