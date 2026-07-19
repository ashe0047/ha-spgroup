"""Test SP Group discovery mapping."""

from __future__ import annotations

import pytest
from custom_components.sp_group.discovery import (
    DiscoveryDataError,
    _index_jarvis_accounts,
    _meter_type_payload,
    _premise_type_payload,
    _premises_payload,
    account_numbers_from_yggdrasil,
    build_discovery_data,
)


def yggdrasil_payload(*, errors: bool = False) -> dict[str, object]:
    """Return a redacted Yggdrasil fixture with intentionally sensitive fields."""
    payload: dict[str, object] = {
        "data": {
            "premiseDetails": {
                "premises": [
                    {
                        "accountNo": "ACC-ROOT-1",
                        "active": True,
                        "signedUp": True,
                        "contestableType": "NON_CONTESTABLE",
                        "sharedResource": {
                            "ownerName": "Sensitive Owner",
                            "ownerEmail": "owner@example.test",
                        },
                        "accounts": [
                            {
                                "accountNo": "ACC-1",
                                "accountStatus": "ACTIVE",
                                "accountType": "RESIDENTIAL",
                                "premiseNo": "PREM-1",
                                "premiseAddressFull": "123 Secret Street",
                                "emailAddress": "person@example.test",
                                "mobileNo": "+6512345678",
                                "activeServices": ["WATER", "ELECTRIC"],
                            }
                        ],
                    }
                ]
            }
        }
    }
    if errors:
        payload["errors"] = [{"message": "partial"}]
    return payload


def jarvis_payload(*, account_number: str = "ACC-1") -> dict[str, object]:
    """Return a redacted Jarvis fixture with intentionally sensitive fields."""
    return {
        "status": "LINKED",
        "premises": [
            {
                "id": "JARVIS-PREMISE-1",
                "address": "123 Secret Street",
                "accounts": [
                    {
                        "account_number": account_number,
                        "premise_number": "PREM-1",
                        "utilities": ["ELECTRIC", "WATER"],
                        "email": "person@example.test",
                    }
                ],
            }
        ],
    }


def meter_type_payload() -> dict[str, object]:
    """Return a Yggdrasil meter-type fixture."""
    return {
        "data": {
            "meterTypeV1": {
                "ami": ["ELECTRICITY"],
                "interval": ["WATER"],
            }
        }
    }


def premise_type_payload() -> dict[str, object]:
    """Return a Yggdrasil premise-type fixture."""
    return {"data": {"premiseTypeForOwnerV1": "RESIDENTIAL"}}


def test_discovery_maps_redacted_stable_data() -> None:
    """Discovery produces useful metadata without leaking raw identifiers."""
    data = build_discovery_data(
        account_id="auth0|account-1",
        yggdrasil_premises=yggdrasil_payload(),
        jarvis_me=jarvis_payload(),
        meter_types_by_account={"ACC-1": meter_type_payload()},
        premise_types_by_account={"ACC-1": premise_type_payload()},
    )

    assert data.has_partial_errors is False
    assert len(data.premises) == 1
    premise = data.premises[0]
    assert premise.active is True
    assert premise.signed_up is True
    assert premise.shared is True
    assert premise.jarvis_correlation == "matched"
    assert premise.jarvis_premise_id == "JARVIS-PREMISE-1"
    assert premise.display_label == "123 Secret Street"
    assert premise.source_provenance == ("yggdrasil", "jarvis")
    assert len(premise.accounts) == 1
    account = premise.accounts[0]
    assert account.utility_types == ("ELECTRIC", "WATER")
    assert account.account_status == "ACTIVE"
    assert account.account_type == "RESIDENTIAL"
    assert account.meter_ami_types == ("ELECTRICITY",)
    assert account.meter_interval_types == ("WATER",)
    assert account.premise_type == "RESIDENTIAL"
    assert account.jarvis_correlation == "matched"
    assert data.future_sensor_keys()[0].utility_type == "electric"
    assert data.future_sensor_keys()[0].display_label == "123 Secret Street"

    text = repr(data)
    assert "JARVIS-PREMISE-1" not in repr(premise)
    for sensitive in (
        "ACC-1",
        "ACC-ROOT-1",
        "PREM-1",
        "JARVIS-PREMISE-1",
        "123 Secret Street",
        "person@example.test",
        "Sensitive Owner",
        "+6512345678",
    ):
        assert sensitive not in text


def test_discovery_keys_are_deterministic_and_account_scoped() -> None:
    """Opaque keys are deterministic and scoped to the config-entry identity."""
    first = build_discovery_data(
        account_id="auth0|account-1",
        yggdrasil_premises=yggdrasil_payload(),
        jarvis_me=jarvis_payload(),
        meter_types_by_account={"ACC-1": meter_type_payload()},
        premise_types_by_account={"ACC-1": premise_type_payload()},
    )
    second = build_discovery_data(
        account_id="auth0|account-1",
        yggdrasil_premises=yggdrasil_payload(),
        jarvis_me=jarvis_payload(),
        meter_types_by_account={"ACC-1": meter_type_payload()},
        premise_types_by_account={"ACC-1": premise_type_payload()},
    )
    other_account = build_discovery_data(
        account_id="auth0|account-2",
        yggdrasil_premises=yggdrasil_payload(),
        jarvis_me=jarvis_payload(),
        meter_types_by_account={"ACC-1": meter_type_payload()},
        premise_types_by_account={"ACC-1": premise_type_payload()},
    )

    assert first.premises[0].key == second.premises[0].key
    assert first.accounts[0].key == second.accounts[0].key
    assert first.premises[0].key != other_account.premises[0].key
    assert first.accounts[0].key != other_account.accounts[0].key


def test_discovery_records_graphql_partial_errors() -> None:
    """GraphQL errors with usable data become provenance, not hard failure."""
    data = build_discovery_data(
        account_id="auth0|account-1",
        yggdrasil_premises=yggdrasil_payload(errors=True),
        jarvis_me=jarvis_payload(),
        meter_types_by_account={"ACC-1": meter_type_payload()},
        premise_types_by_account={"ACC-1": premise_type_payload()},
    )

    assert data.has_partial_errors is True
    assert data.premises


def test_discovery_marks_jarvis_mismatch() -> None:
    """Jarvis payloads that do not match Yggdrasil are visible as provenance."""
    data = build_discovery_data(
        account_id="auth0|account-1",
        yggdrasil_premises=yggdrasil_payload(),
        jarvis_me=jarvis_payload(account_number="OTHER-ACCOUNT"),
        meter_types_by_account={"ACC-1": meter_type_payload()},
        premise_types_by_account={"ACC-1": premise_type_payload()},
    )

    assert data.premises[0].jarvis_correlation == "mismatched"
    assert data.accounts[0].jarvis_correlation == "mismatched"
    assert data.accounts[0].source_provenance == ("yggdrasil",)


def test_discovery_can_require_jarvis_correlation() -> None:
    """Coordinator mode rejects discovery without future chart correlation."""
    with pytest.raises(DiscoveryDataError, match="Jarvis discovery"):
        build_discovery_data(
            account_id="auth0|account-1",
            yggdrasil_premises=yggdrasil_payload(),
            jarvis_me=jarvis_payload(account_number="OTHER-ACCOUNT"),
            meter_types_by_account={"ACC-1": meter_type_payload()},
            premise_types_by_account={"ACC-1": premise_type_payload()},
            require_jarvis_correlation=True,
        )


def test_discovery_matches_jarvis_padded_account_numbers() -> None:
    """Live Jarvis account identifiers may include leading zero padding."""
    payload = {
        "data": {
            "premiseDetails": {
                "premises": [
                    {
                        "accountNo": "ROOT",
                        "accounts": [
                            {
                                "accountNo": "4475308714",
                                "premiseNo": "2000536445",
                                "activeServices": ["water"],
                            }
                        ],
                    }
                ]
            }
        }
    }
    jarvis = {
        "premises": [
            {
                "id": "2000536445",
                "accounts": [
                    {
                        "account_number": "004475308714",
                        "premise_number": "2000536445",
                        "utilities": ["water"],
                    }
                ],
            }
        ]
    }

    data = build_discovery_data(
        account_id="auth0|account-1",
        yggdrasil_premises=payload,
        jarvis_me=jarvis,
        meter_types_by_account={},
        premise_types_by_account={},
        require_jarvis_correlation=True,
    )

    assert data.premises[0].jarvis_correlation == "matched"
    assert data.premises[0].jarvis_premise_id == "2000536445"
    assert data.accounts[0].jarvis_correlation == "matched"
    assert data.accounts[0].utility_types == ("WATER",)
    assert data.future_sensor_keys()[0].utility_type == "water"


def test_discovery_matches_contestable_electricity_details() -> None:
    """Contestable retailer electricity is a supported Jarvis account shape."""
    payload = {
        "data": {
            "premiseDetails": {
                "premises": [
                    {
                        "accountNo": "ROOT",
                        "accounts": [
                            {
                                "accountNo": "4475308714",
                                "premiseNo": "2000536445",
                                "activeServices": ["water"],
                            },
                            {
                                "accountNo": "9309492875",
                                "premiseNo": "6100780272",
                                "activeServices": ["electricity"],
                            },
                        ],
                    }
                ]
            }
        }
    }
    jarvis = {
        "premises": [
            {
                "id": "2000536445",
                "accounts": [
                    {
                        "account_number": "004475308714",
                        "premise_number": "2000536445",
                        "utilities": ["water"],
                    }
                ],
                "contestable_details": {
                    "account_number": "009309492875",
                    "premise_number": "6100780272",
                    "retailer_name": "Third Party Retailer",
                },
                "meter_details": {
                    "ami_elec": True,
                    "interval_utilities": ["electric"],
                },
            }
        ]
    }

    data = build_discovery_data(
        account_id="auth0|account-1",
        yggdrasil_premises=payload,
        jarvis_me=jarvis,
        meter_types_by_account={},
        premise_types_by_account={},
        require_jarvis_correlation=True,
    )

    assert data.premises[0].jarvis_correlation == "matched"
    assert data.premises[0].jarvis_premise_id == "2000536445"
    assert {account.jarvis_correlation for account in data.accounts} == {"matched"}
    assert {account.utility_types for account in data.accounts} == {
        ("ELECTRIC",),
        ("WATER",),
    }
    assert {sensor_key.utility_type for sensor_key in data.future_sensor_keys()} == {
        "electric",
        "water",
    }


def test_discovery_tolerates_missing_optional_sections() -> None:
    """Missing optional account details produce bounded model data."""
    payload = {
        "data": {
            "premiseDetails": {
                "premises": [
                    {
                        "accountNo": "ACC-ROOT-1",
                        "active": False,
                        "accounts": [{"accountNo": "ACC-1"}],
                    }
                ]
            }
        }
    }

    data = build_discovery_data(
        account_id="auth0|account-1",
        yggdrasil_premises=payload,
        jarvis_me={},
        meter_types_by_account={},
        premise_types_by_account={},
    )

    assert data.premises[0].active is False
    assert data.premises[0].signed_up is None
    assert data.premises[0].shared is False
    assert data.accounts[0].utility_types == ()


def test_malformed_yggdrasil_payload_fails_discovery() -> None:
    """Malformed primary discovery data is not silently accepted."""
    with pytest.raises(DiscoveryDataError):
        account_numbers_from_yggdrasil({"data": {"premiseDetails": {}}})


def test_premises_payload_rejects_non_dict_in_list() -> None:
    """_premises_payload raises if a premise in the list is not a dict."""
    payload = {
        "data": {
            "premiseDetails": {
                "premises": [
                    "not-a-premise-dict",
                ]
            }
        }
    }
    with pytest.raises(DiscoveryDataError, match="expected dict"):
        _premises_payload(payload)


def test_premises_payload_rejects_partial_non_dict() -> None:
    """_premises_payload raises if any premise in the list is not a dict."""
    payload = {
        "data": {
            "premiseDetails": {
                "premises": [
                    {"accountNo": "ACC-1"},
                    None,
                    {"accountNo": "ACC-2"},
                ]
            }
        }
    }
    with pytest.raises(DiscoveryDataError, match="expected dict"):
        _premises_payload(payload)


def test_meter_type_payload_handles_valid() -> None:
    """_meter_type_payload returns the meterTypeV1 dict from valid responses."""
    result = _meter_type_payload(
        {
            "data": {
                "meterTypeV1": {
                    "ami": ["ELECTRICITY"],
                    "interval": ["WATER"],
                }
            }
        }
    )
    assert result == {"ami": ["ELECTRICITY"], "interval": ["WATER"]}


def test_meter_type_payload_handles_none() -> None:
    """_meter_type_payload returns {} for None input."""
    assert _meter_type_payload(None) == {}


def test_meter_type_payload_handles_missing_data() -> None:
    """_meter_type_payload returns {} for response missing data dict."""
    assert _meter_type_payload({}) == {}


def test_meter_type_payload_handles_missing_meter_type() -> None:
    """_meter_type_payload returns {} for response missing meterTypeV1."""
    assert _meter_type_payload({"data": {}}) == {}


def test_premise_type_payload_handles_valid() -> None:
    """_premise_type_payload returns the type string from valid responses."""
    result = _premise_type_payload({"data": {"premiseTypeForOwnerV1": "RESIDENTIAL"}})
    assert result == "RESIDENTIAL"


def test_premise_type_payload_handles_none() -> None:
    """_premise_type_payload returns None for None input."""
    assert _premise_type_payload(None) is None


def test_premise_type_payload_handles_missing_data() -> None:
    """_premise_type_payload returns None for response missing data."""
    assert _premise_type_payload({}) is None


def test_index_jarvis_accounts_handles_empty_premises() -> None:
    """_index_jarvis_accounts returns {} for empty premises list."""
    result = _index_jarvis_accounts({"premises": []})
    assert result == {}


def test_index_jarvis_accounts_handles_missing_premises() -> None:
    """_index_jarvis_accounts returns {} when premises key is absent."""
    result = _index_jarvis_accounts({})
    assert result == {}


def test_index_jarvis_accounts_skips_non_dict_premise() -> None:
    """_index_jarvis_accounts skips premis items that are not dicts."""
    result = _index_jarvis_accounts({"premises": ["string-premise", None, 42]})
    assert result == {}


def test_index_jarvis_accounts_skips_premise_without_id() -> None:
    """_index_jarvis_accounts skips premises that have no id field."""
    result = _index_jarvis_accounts(
        {
            "premises": [
                {"accounts": []},  # no id
            ]
        }
    )
    assert result == {}


def test_index_jarvis_accounts_skips_account_without_account_number() -> None:
    """_index_jarvis_accounts skips accounts that have no account_number."""
    result = _index_jarvis_accounts(
        {
            "premises": [
                {
                    "id": "premise-1",
                    "accounts": [
                        {"utilities": ["ELECTRIC"]},  # no account_number
                    ],
                }
            ]
        }
    )
    assert result == {}


def test_index_jarvis_accounts_indexes_valid_account() -> None:
    """_index_jarvis_accounts indexes valid accounts by account_number."""
    result = _index_jarvis_accounts(
        {
            "premises": [
                {
                    "id": "premise-1",
                    "accounts": [
                        {
                            "account_number": "ACC-1",
                            "utilities": ["ELECTRIC", "WATER"],
                        }
                    ],
                }
            ]
        }
    )
    assert ("ACC-1", None) in result
