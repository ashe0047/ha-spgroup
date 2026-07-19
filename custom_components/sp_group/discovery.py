"""Discovery mapping for SP Group premise and utility metadata."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from .const import DOMAIN
from .electricity import SpGroupElectricityCandidate
from .water import SpGroupWaterCandidate

_LOGGER = logging.getLogger(__name__)

_SOURCE_YGGDRASIL = "yggdrasil"
_SOURCE_JARVIS = "jarvis"
_CORRELATION_MATCHED = "matched"
_CORRELATION_MISSING = "missing"
_CORRELATION_MISMATCHED = "mismatched"


class DiscoveryDataError(ValueError):
    """Raised when discovery payloads cannot produce bounded coordinator data."""


@dataclass(frozen=True, slots=True)
class SpGroupUtilityAccount:
    """Redacted utility-account discovery data."""

    key: str
    premise_key: str
    utility_types: tuple[str, ...]
    account_status: str | None = None
    account_type: str | None = None
    meter_ami_types: tuple[str, ...] = field(default_factory=tuple)
    meter_interval_types: tuple[str, ...] = field(default_factory=tuple)
    premise_type: str | None = None
    jarvis_correlation: str = _CORRELATION_MISSING
    source_provenance: tuple[str, ...] = (_SOURCE_YGGDRASIL,)


@dataclass(frozen=True, slots=True)
class SpGroupPremise:
    """Redacted premise discovery data."""

    key: str
    active: bool | None
    signed_up: bool | None
    shared: bool
    contestable_type: str | None
    accounts: tuple[SpGroupUtilityAccount, ...]
    jarvis_correlation: str = _CORRELATION_MISSING
    source_provenance: tuple[str, ...] = (_SOURCE_YGGDRASIL,)
    jarvis_premise_id: str | None = field(default=None, repr=False, compare=False)
    display_label: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SpGroupFutureSensorKey:
    """Future discovery-derived sensor key."""

    key: str
    premise_key: str
    utility_type: str
    display_label: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SpGroupCoordinatorData:
    """Coordinator payload for SP Group discovery data."""

    premises: tuple[SpGroupPremise, ...] = field(default_factory=tuple)
    electricity_candidates: tuple[SpGroupElectricityCandidate, ...] = field(
        default_factory=tuple
    )
    electricity_warnings: tuple[str, ...] = field(default_factory=tuple)
    water_candidates: tuple[SpGroupWaterCandidate, ...] = field(default_factory=tuple)
    water_warnings: tuple[str, ...] = field(default_factory=tuple)
    has_partial_errors: bool = False

    @property
    def accounts(self) -> tuple[SpGroupUtilityAccount, ...]:
        """Return all discovered utility accounts."""
        return tuple(
            account for premise in self.premises for account in premise.accounts
        )

    def future_sensor_keys(self) -> tuple[SpGroupFutureSensorKey, ...]:
        """Return deterministic keys for future discovery-driven utility sensors."""
        keys: list[SpGroupFutureSensorKey] = []
        seen: set[tuple[str, str]] = set()
        for premise in self.premises:
            for account in premise.accounts:
                for utility_type in account.utility_types:
                    normalized = utility_type.lower()
                    identity = (premise.key, normalized)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    keys.append(
                        SpGroupFutureSensorKey(
                            key=f"{premise.key}_{normalized}",
                            premise_key=premise.key,
                            utility_type=normalized,
                            display_label=premise.display_label,
                        )
                    )
        return tuple(keys)


def build_discovery_data(
    *,
    account_id: str | None,
    yggdrasil_premises: dict[str, Any],
    jarvis_me: dict[str, Any],
    meter_types_by_account: dict[str, dict[str, Any]],
    premise_types_by_account: dict[str, dict[str, Any]],
    require_jarvis_correlation: bool = False,
) -> SpGroupCoordinatorData:
    """Build redacted discovery data from raw SDK responses."""
    identity = account_id or "unknown-account"
    premises_payload = _premises_payload(yggdrasil_premises)
    jarvis_index = _index_jarvis_accounts(jarvis_me)
    has_partial_errors = bool(yggdrasil_premises.get("errors"))

    premises: list[SpGroupPremise] = []
    has_unmatched_account = False
    for premise_payload in premises_payload:
        if not isinstance(premise_payload, dict):
            continue
        accounts_payload = premise_payload.get("accounts")
        if accounts_payload is None:
            accounts_payload = ()
        if not isinstance(accounts_payload, list):
            accounts_payload = ()

        premise_source_id = _first_string(
            premise_payload.get("accountNo"),
            *(
                account.get("premiseNo")
                for account in accounts_payload
                if isinstance(account, dict)
            ),
        )
        if premise_source_id is None:
            continue

        premise_key = _opaque_key(
            identity,
            "premise",
            _SOURCE_YGGDRASIL,
            premise_source_id,
        )
        accounts: list[SpGroupUtilityAccount] = []
        premise_has_jarvis_match = False
        premise_has_jarvis_mismatch = False
        jarvis_premise_id: str | None = None
        display_label = _premise_display_label(premise_payload)
        for account_payload in accounts_payload:
            if not isinstance(account_payload, dict):
                continue
            account_no = _as_string(account_payload.get("accountNo"))
            if account_no is None:
                continue
            account_key = _opaque_key(
                identity,
                "account",
                _SOURCE_YGGDRASIL,
                account_no,
            )
            account_match = _find_jarvis_match(
                jarvis_index,
                account_no=account_no,
                premise_no=_as_string(account_payload.get("premiseNo")),
            )
            jarvis_correlation = _CORRELATION_MISSING
            provenance = [_SOURCE_YGGDRASIL]
            if account_match is not None:
                premise_has_jarvis_match = True
                jarvis_correlation = _CORRELATION_MATCHED
                provenance.append(_SOURCE_JARVIS)
                jarvis_premise_id = _as_string(account_match.get("_premise_id"))
            elif jarvis_index:
                premise_has_jarvis_mismatch = True
                jarvis_correlation = _CORRELATION_MISMATCHED
                has_unmatched_account = True
            else:
                has_unmatched_account = True

            meter_type = _meter_type_payload(meter_types_by_account.get(account_no))
            premise_type = _premise_type_payload(
                premise_types_by_account.get(account_no)
            )
            utility_types = _utility_types(account_payload, account_match)

            accounts.append(
                SpGroupUtilityAccount(
                    key=account_key,
                    premise_key=premise_key,
                    utility_types=utility_types,
                    account_status=_as_string(account_payload.get("accountStatus")),
                    account_type=_as_string(account_payload.get("accountType")),
                    meter_ami_types=_string_tuple(meter_type.get("ami")),
                    meter_interval_types=_string_tuple(meter_type.get("interval")),
                    premise_type=premise_type,
                    jarvis_correlation=jarvis_correlation,
                    source_provenance=tuple(provenance),
                )
            )

        premise_correlation = _CORRELATION_MISSING
        premise_provenance = [_SOURCE_YGGDRASIL]
        if premise_has_jarvis_match:
            premise_correlation = _CORRELATION_MATCHED
            premise_provenance.append(_SOURCE_JARVIS)
        elif premise_has_jarvis_mismatch:
            premise_correlation = _CORRELATION_MISMATCHED

        premises.append(
            SpGroupPremise(
                key=premise_key,
                active=_as_bool(premise_payload.get("active")),
                signed_up=_as_bool(premise_payload.get("signedUp")),
                shared=isinstance(premise_payload.get("sharedResource"), dict),
                contestable_type=_as_string(premise_payload.get("contestableType")),
                accounts=tuple(sorted(accounts, key=lambda account: account.key)),
                jarvis_correlation=premise_correlation,
                source_provenance=tuple(premise_provenance),
                jarvis_premise_id=jarvis_premise_id,
                display_label=display_label,
            )
        )

    if require_jarvis_correlation and has_unmatched_account:
        raise DiscoveryDataError(
            "Jarvis discovery did not match all Yggdrasil utility accounts"
        )

    return SpGroupCoordinatorData(
        premises=tuple(sorted(premises, key=lambda premise: premise.key)),
        has_partial_errors=has_partial_errors,
    )


def account_numbers_from_yggdrasil(payload: dict[str, Any]) -> tuple[str, ...]:
    """Return account numbers from a raw Yggdrasil premise payload."""
    account_numbers: set[str] = set()
    for premise_payload in _premises_payload(payload):
        if not isinstance(premise_payload, dict):
            continue
        accounts_payload = premise_payload.get("accounts")
        if not isinstance(accounts_payload, list):
            continue
        for account_payload in accounts_payload:
            if not isinstance(account_payload, dict):
                continue
            account_no = _as_string(account_payload.get("accountNo"))
            if account_no is not None:
                account_numbers.add(account_no)
    return tuple(sorted(account_numbers))


def _premises_payload(payload: dict[str, Any]) -> list[Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DiscoveryDataError("Yggdrasil discovery payload missing 'data' dict")
    premise_details = data.get("premiseDetails")
    if not isinstance(premise_details, dict):
        raise DiscoveryDataError(
            "Yggdrasil discovery payload missing 'premiseDetails' dict"
        )
    premises = premise_details.get("premises")
    if not isinstance(premises, list):
        raise DiscoveryDataError(
            "Yggdrasil discovery payload missing 'premises' list"
        )
    for i, premise in enumerate(premises):
        if not isinstance(premise, dict):
            raise DiscoveryDataError(
                f"Yggdrasil premises[{i}] is {type(premise).__name__}, "
                "expected dict"
            )
    return premises

def _index_jarvis_accounts(
    payload: dict[str, Any],
) -> dict[tuple[str | None, str | None], dict[str, Any]]:
    _data_path = "jarvis/me"
    premises = payload.get("premises")
    if not isinstance(premises, list):
        return {}
    index: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for premise in premises:
        if not isinstance(premise, dict):
            _LOGGER.warning(
                "SP Group %s: premises item is %s, expected dict — skipping",
                _data_path,
                type(premise).__name__,
            )
            continue
        if not isinstance(premise.get("id"), str) or not premise["id"]:
            _LOGGER.warning(
                "SP Group %s: premise missing 'id' field — skipping",
                _data_path,
            )
            continue
        premise_id = _as_string(premise.get("id"))
        accounts = premise.get("accounts")
        if isinstance(accounts, list):
            for account in accounts:
                if isinstance(account, dict):
                    if not isinstance(account.get("account_number"), str):
                        _LOGGER.warning(
                            "SP Group %s: account missing 'account_number' — skipping",
                            _data_path,
                        )
                        continue
                    _add_jarvis_indexed_account(index, account, premise_id)

        contestable_details = premise.get("contestable_details")
        if isinstance(contestable_details, dict):
            if isinstance(contestable_details.get("utilities"), list):
                _LOGGER.debug(
                    "SP Group %s: premise has contestable_details with utilities=%s",
                    _data_path,
                    contestable_details.get("utilities"),
                )
            indexed_contestable = dict(contestable_details)
            indexed_contestable.setdefault("utilities", ["ELECTRIC"])
            indexed_contestable["_jarvis_source"] = "contestable_details"
            _add_jarvis_indexed_account(index, indexed_contestable, premise_id)
    return index


def _add_jarvis_indexed_account(
    index: dict[tuple[str | None, str | None], dict[str, Any]],
    account: dict[str, Any],
    premise_id: str | None,
) -> None:
    """Index one Jarvis account shape by raw and normalized account numbers."""
    account_number = _as_string(account.get("account_number"))
    if account_number is None:
        return
    premise_number = _as_string(account.get("premise_number"))
    indexed_account = dict(account)
    indexed_account["_premise_id"] = premise_id
    for match_key in _account_number_match_keys(account_number):
        index[(match_key, premise_number)] = indexed_account
        index[(match_key, None)] = indexed_account


def _find_jarvis_match(
    index: dict[tuple[str | None, str | None], dict[str, Any]],
    *,
    account_no: str,
    premise_no: str | None,
) -> dict[str, Any] | None:
    for match_key in _account_number_match_keys(account_no):
        if premise_no is not None:
            match = index.get((match_key, premise_no))
            if match is not None:
                return match
        match = index.get((match_key, None))
        if match is not None:
            return match
    return None


def _meter_type_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    _data_path = "yggdrasil/meterType"
    if not isinstance(payload, dict):
        _LOGGER.warning(
            "SP Group %s: expected dict payload, got %s",
            _data_path,
            type(payload).__name__,
        )
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        _LOGGER.warning(
            "SP Group %s: response missing 'data' dict",
            _data_path,
        )
        return {}
    meter_type = data.get("meterTypeV1")
    if not isinstance(meter_type, dict):
        _LOGGER.warning(
            "SP Group %s: response missing 'meterTypeV1' dict",
            _data_path,
        )
        return {}
    return meter_type


def _premise_type_payload(payload: dict[str, Any] | None) -> str | None:
    _data_path = "yggdrasil/premiseType"
    if not isinstance(payload, dict):
        _LOGGER.warning(
            "SP Group %s: expected dict payload, got %s",
            _data_path,
            type(payload).__name__,
        )
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        _LOGGER.warning(
            "SP Group %s: response missing 'data' dict",
            _data_path,
        )
        return None
    return _as_string(data.get("premiseTypeForOwnerV1"))


def _utility_types(
    yggdrasil_account: dict[str, Any],
    jarvis_account: dict[str, Any] | None,
) -> tuple[str, ...]:
    values: set[str] = set()
    for candidate in (
        yggdrasil_account.get("activeServices"),
        jarvis_account.get("utilities") if jarvis_account is not None else None,
    ):
        values.update(
            normalized
            for item in _string_tuple(candidate)
            if (normalized := _normalize_utility_type(item)) is not None
        )
    return tuple(sorted(values))


def _opaque_key(account_id: str, kind: str, source: str, source_id: str) -> str:
    digest = sha256(f"{account_id}|{kind}|{source}|{source_id}".encode()).hexdigest()
    return f"{DOMAIN}_{kind}_{digest[:24]}"


def _first_string(*values: object) -> str | None:
    for value in values:
        text = _as_string(value)
        if text is not None:
            return text
    return None


def _as_string(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _account_number_match_keys(value: str) -> tuple[str, ...]:
    """Return Jarvis/Yggdrasil account-number variants for correlation only."""
    normalized = _normalize_account_number(value)
    if normalized == value:
        return (value,)
    return (value, normalized)


def _normalize_account_number(value: str) -> str:
    if value.isdigit():
        return value.lstrip("0") or "0"
    return value


def _normalize_utility_type(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized in {"electric", "electricity"}:
        return "ELECTRIC"
    if normalized == "water":
        return "WATER"
    return value if value else None


def _premise_display_label(premise_payload: dict[str, Any]) -> str | None:
    accounts_payload = premise_payload.get("accounts")
    if not isinstance(accounts_payload, list):
        return None
    for account_payload in accounts_payload:
        if not isinstance(account_payload, dict):
            continue
        label = _as_string(account_payload.get("premiseAddressFull"))
        if label is not None:
            return label
    return None


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(sorted(item for item in value if isinstance(item, str) and item))


__all__ = [
    "DiscoveryDataError",
    "SpGroupCoordinatorData",
    "SpGroupFutureSensorKey",
    "SpGroupPremise",
    "SpGroupUtilityAccount",
    "account_numbers_from_yggdrasil",
    "build_discovery_data",
]
