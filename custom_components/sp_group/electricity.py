"""Electricity payload semantics for the SP Group integration."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .ledger import SpGroupBucketObservation

SINGAPORE_TZ = ZoneInfo("Asia/Singapore")
PROVENANCE_AMI_OBSERVED = "ami_observed"
PROVENANCE_HISTORY_ALLOCATED = "history_allocated"
PROVENANCE_HISTORY_MONTHLY = "history_monthly"

_SOURCE_HISTORY_CHART = "history_chart"
_SOURCE_AMI_CHART = "ami_chart"
_SEMANTIC_PERIOD_USAGE = "period_usage"
_SEMANTIC_CHART_BUCKET = "chart_bucket"
_SUPPORTED_KWH_UNITS = {"kwh", "kw h", "kilowatt-hour", "kilowatt-hours"}
_HALF_HOUR = timedelta(minutes=30)
_ALLOCATION_PLACES = Decimal("0.0000000001")


@dataclass(frozen=True, slots=True)
class SpGroupHistoryMonth:
    """Validated monthly electricity total usable by source backfill."""

    start: datetime
    total_kwh: Decimal
    selected_field: str


AMI_COVERAGE_FULL = "full_ami"
AMI_COVERAGE_NONE = "no_ami"
AMI_COVERAGE_PARTIAL = "partial_ami"
AMI_COVERAGE_INVALID = "invalid_ami"


@dataclass(frozen=True, slots=True)
class SpGroupElectricityMonthClassification:
    """AMI coverage classification for one calendar month."""

    month: datetime
    coverage: str
    rows: tuple[tuple[datetime, Decimal], ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SpGroupElectricityComposition:
    """Composed electricity observations and unresolved closed months."""

    observations: tuple[SpGroupBucketObservation, ...] = ()
    unresolved_months: tuple[datetime, ...] = ()
    unavailable_months: tuple[datetime, ...] = ()
    history_invalid: bool = False


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
            warning for warning in (value_warning, unit_warning) if warning is not None
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


def parse_history_electricity_months(
    payload: dict[str, Any],
) -> tuple[SpGroupHistoryMonth, ...]:
    """Return validated actual history totals keyed by calendar month.

    The live transition response has ``current=0`` and the billed month total
    in ``previous``. Keep that narrow, validated rule separate from generic
    chart candidate parsing.
    """
    elec = payload.get("elec")
    rows = elec.get("data") if isinstance(elec, dict) else None
    if not isinstance(rows, list):
        return ()
    months: dict[datetime, SpGroupHistoryMonth] = {}
    for row in rows:
        if not isinstance(row, dict) or str(row.get("status", "")).upper() != "ACTUAL":
            continue
        start = _month_start(row.get("period"))
        unit = _as_string(row.get("unit"))
        consumption = row.get("consumption")
        if (
            start is None
            or _unit_warning(unit) is not None
            or not isinstance(consumption, dict)
        ):
            continue
        current, current_warning = _parse_decimal(consumption.get("current"))
        previous, previous_warning = _parse_decimal(consumption.get("previous"))
        if current_warning is None and current is not None and current >= 0:
            total, field = current, "current"
            if (
                current == 0
                and previous_warning is None
                and previous is not None
                and previous > 0
            ):
                total, field = previous, "previous"
        elif previous_warning is None and previous is not None and previous >= 0:
            total, field = previous, "previous"
        else:
            continue
        months[start] = SpGroupHistoryMonth(start, total, field)
    return tuple(months[start] for start in sorted(months))


def compose_electricity_history_and_ami(
    history_payload: dict[str, Any],
    ami_rows: tuple[tuple[datetime, Decimal], ...],
    *,
    backfill_from: datetime,
    now: datetime,
) -> SpGroupElectricityComposition:
    """Compose validated history and AMI evidence by Singapore calendar month."""
    history = {
        item.start: item for item in parse_history_electricity_months(history_payload)
    }
    rows_by_month: dict[datetime, dict[datetime, Decimal]] = {}
    duplicate_months: set[datetime] = set()
    for start, value in ami_rows:
        if start < backfill_from or start + _HALF_HOUR > now:
            continue
        month = _month_start(start.isoformat())
        if month is not None:
            rows = rows_by_month.setdefault(month, {})
            if start in rows:
                duplicate_months.add(month)
            rows[start] = value
    history_invalid = not _history_payload_shape_valid(history_payload)
    ami_months = set(rows_by_month)
    history_months = set(history)
    months = sorted(ami_months | history_months)
    observations: list[SpGroupBucketObservation] = []
    unresolved: list[datetime] = []
    unavailable: list[datetime] = []
    first_month = _month_start(backfill_from.isoformat())
    for month in months:
        if first_month is None or month < first_month:
            continue
        month_end = _next_month(month)
        closed = month_end <= now
        classification = classify_electricity_ami_month(
            month,
            rows_by_month.get(month, {}),
            duplicate=month in duplicate_months,
        )
        month_history = history.get(month)
        if not closed:
            if classification.coverage in {
                AMI_COVERAGE_FULL,
                AMI_COVERAGE_PARTIAL,
            }:
                observations.extend(
                    _ami_observation(start, value)
                    for start, value in classification.rows
                    if value > 0
                )
            continue
        if classification.coverage == AMI_COVERAGE_FULL:
            observations.extend(
                _ami_observation(start, value)
                for start, value in classification.rows
                if value > 0
            )
            continue
        if classification.coverage == AMI_COVERAGE_NONE:
            if month_history is not None and not history_invalid:
                observations.append(_monthly_observation(month_history))
            elif history_invalid:
                unresolved.append(month)
            else:
                unavailable.append(month)
            continue
        if classification.coverage == AMI_COVERAGE_PARTIAL:
            if month_history is None or history_invalid:
                unresolved.append(month)
                continue
            observed_total = sum(
                (value for _, value in classification.rows if value > 0),
                Decimal("0"),
            )
            remainder = month_history.total_kwh - observed_total
            if remainder < 0:
                unresolved.append(month)
                continue
            expected = tuple(_slots(month, month_end))
            first_positive = next(
                start
                for start, value in classification.rows
                if value > 0
            )
            prefix = tuple(slot for slot in expected if slot < first_positive)
            observations.extend(_allocated_observations(prefix, remainder))
            observations.extend(
                _ami_observation(start, value)
                for start, value in classification.rows
                if value > 0
            )
            continue
        unresolved.append(month)
    return SpGroupElectricityComposition(
        observations=tuple(sorted(observations, key=lambda item: item.start)),
        unresolved_months=tuple(sorted(set(unresolved))),
        unavailable_months=tuple(sorted(set(unavailable))),
        history_invalid=history_invalid,
    )


def classify_electricity_ami_month(
    month: datetime,
    rows: dict[datetime, Decimal],
    *,
    duplicate: bool = False,
) -> SpGroupElectricityMonthClassification:
    """Classify AMI coverage without consulting history totals."""
    expected = tuple(_slots(month, _next_month(month)))
    ordered = tuple(sorted(rows.items()))
    if not ordered:
        return SpGroupElectricityMonthClassification(month, AMI_COVERAGE_NONE)
    if duplicate:
        return SpGroupElectricityMonthClassification(
            month, AMI_COVERAGE_INVALID, ordered, "duplicate_timestamp"
        )
    expected_set = set(expected)
    actual_set = set(rows)
    positive = [(start, value) for start, value in ordered if value > 0]
    if not positive:
        if actual_set == expected_set:
            return SpGroupElectricityMonthClassification(
                month, AMI_COVERAGE_FULL, ordered
            )
        return SpGroupElectricityMonthClassification(
            month, AMI_COVERAGE_NONE, ordered, "placeholder_rows"
        )
    if actual_set != expected_set:
        return SpGroupElectricityMonthClassification(
            month, AMI_COVERAGE_INVALID, ordered, "missing_timestamp"
        )
    first_positive = positive[0][0]
    if first_positive == month:
        return SpGroupElectricityMonthClassification(
            month, AMI_COVERAGE_FULL, ordered
        )
    if any(
        value <= 0
        for start, value in ordered
        if start >= first_positive
    ):
        return SpGroupElectricityMonthClassification(
            month, AMI_COVERAGE_INVALID, ordered, "internal_or_trailing_zero"
        )
    return SpGroupElectricityMonthClassification(
        month, AMI_COVERAGE_PARTIAL, ordered
    )


def _history_payload_shape_valid(payload: dict[str, Any]) -> bool:
    elec = payload.get("elec")
    return isinstance(elec, dict) and isinstance(elec.get("data"), list)


def _allocated_observations(
    slots: tuple[datetime, ...],
    remainder: Decimal,
) -> tuple[SpGroupBucketObservation, ...]:
    from .ledger import SpGroupBucketObservation

    if not slots:
        return ()
    base = (remainder / Decimal(len(slots))).quantize(
        _ALLOCATION_PLACES, rounding=ROUND_DOWN
    )
    units = int(
        ((remainder - base * len(slots)) / _ALLOCATION_PLACES).to_integral_value()
    )
    return tuple(
        SpGroupBucketObservation(
            bucket_key=slot.isoformat(),
            start=slot,
            end=slot + _HALF_HOUR,
            value=base + (_ALLOCATION_PLACES if index < units else Decimal("0")),
            provenance=PROVENANCE_HISTORY_ALLOCATED,
        )
        for index, slot in enumerate(slots)
    )


def _ami_observation(start: datetime, value: Decimal) -> SpGroupBucketObservation:
    from .ledger import SpGroupBucketObservation

    return SpGroupBucketObservation(
        bucket_key=start.isoformat(),
        start=start,
        end=start + _HALF_HOUR,
        value=value,
        provenance=PROVENANCE_AMI_OBSERVED,
    )


def _monthly_observation(month: SpGroupHistoryMonth) -> SpGroupBucketObservation:
    from .ledger import SpGroupBucketObservation

    return SpGroupBucketObservation(
        bucket_key=month.start.isoformat(),
        start=month.start,
        end=_next_month(month.start),
        value=month.total_kwh,
        provenance=PROVENANCE_HISTORY_MONTHLY,
    )


def _month_start(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 7 and value[4] == "-":
            parsed = datetime.strptime(value, "%Y-%m").replace(tzinfo=SINGAPORE_TZ)
        else:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=SINGAPORE_TZ)
            parsed = parsed.astimezone(SINGAPORE_TZ)
        return parsed.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        return None


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


def _slots(start: datetime, end: datetime) -> Iterator[datetime]:
    current = start
    while current < end:
        yield current
        current += _HALF_HOUR


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
    "AMI_COVERAGE_FULL",
    "AMI_COVERAGE_NONE",
    "AMI_COVERAGE_PARTIAL",
    "AMI_COVERAGE_INVALID",
    "SpGroupElectricityCandidate",
    "SpGroupElectricityMonthClassification",
    "SpGroupElectricityParseResult",
    "SpGroupHistoryMonth",
    "SpGroupElectricityComposition",
    "classify_electricity_ami_month",
    "compose_electricity_history_and_ami",
    "parse_history_electricity_months",
    "parse_ami_electricity_chart",
    "parse_history_electricity_chart",
]
