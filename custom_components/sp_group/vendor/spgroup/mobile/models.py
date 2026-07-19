"""Raw model helpers for SP Group mobile API responses."""

from __future__ import annotations

from dataclasses import dataclass

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class RawResponse:
    """HTTP response with parsed data and original text."""

    status: int
    data: JsonValue
    raw_text: str


@dataclass(frozen=True, slots=True)
class MfaChallenge:
    """Out-of-band MFA challenge returned by Auth0."""

    oob_code: str
    authenticator_id: str | None = None
