"""APK-style request construction helpers for SP Group mobile clients."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .constants import (
    APK_USER_AGENT,
    AUTH0_BASE_URL,
    MULESOFT_AUTH_BASE_URL,
    MULESOFT_PUBLIC_BASE_URL,
)
from .session import MobileSession

type JsonBody = Mapping[str, object]
type QueryValue = str | int | bool | None
type QueryParams = Mapping[str, QueryValue | Sequence[QueryValue]]

INTERNAL_AUTH_CONTROL_HEADERS = frozenset(
    {
        "No-Authentication",
        "Optional-Authentication",
        "Requires-Authentication",
    }
)


class EndpointFamily(StrEnum):
    """Known APK endpoint base URL families."""

    AUTH0 = "auth0"
    MULESOFT_AUTH = "mulesoft_auth"
    MULESOFT_PUBLIC = "mulesoft_public"


class AuthPolicy(StrEnum):
    """APK auth policy for request construction."""

    AUTHENTICATED = "authenticated"
    NO_AUTH = "no_auth"
    OPTIONAL_AUTH = "optional_auth"


DEFAULT_ENDPOINT_BASE_URLS: dict[EndpointFamily, str] = {
    EndpointFamily.AUTH0: AUTH0_BASE_URL,
    EndpointFamily.MULESOFT_AUTH: MULESOFT_AUTH_BASE_URL,
    EndpointFamily.MULESOFT_PUBLIC: MULESOFT_PUBLIC_BASE_URL,
}


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """Declarative APK-style request details before transport execution."""

    method: str
    family: EndpointFamily
    path: str
    auth_policy: AuthPolicy
    static_headers: Mapping[str, str] = field(default_factory=dict)
    query: QueryParams | None = None
    json_body: JsonBody | None = None


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    """Concrete request details ready for transport execution."""

    method: str
    url: str
    headers: dict[str, str]
    json_body: JsonBody | None = None


def prepare_request(
    spec: RequestSpec,
    *,
    session: MobileSession | None = None,
    user_agent: str | None = APK_USER_AGENT,
    base_url_overrides: Mapping[EndpointFamily, str] | None = None,
) -> PreparedRequest:
    """Build a concrete request from an APK-style request spec."""
    headers = _wire_headers(
        spec.auth_policy,
        session=session,
        static_headers=spec.static_headers,
        user_agent=user_agent,
    )
    return PreparedRequest(
        method=spec.method,
        url=build_url(
            spec.family,
            spec.path,
            query=spec.query,
            base_url_overrides=base_url_overrides,
        ),
        headers=headers,
        json_body=spec.json_body,
    )


def build_url(
    family: EndpointFamily,
    path: str,
    *,
    query: QueryParams | None = None,
    base_url_overrides: Mapping[EndpointFamily, str] | None = None,
) -> str:
    """Build a URL with standard query encoding."""
    base_url = _base_url(family, base_url_overrides).rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    url = f"{base_url}{normalized_path}"
    if not query:
        return url

    parts = urlsplit(url)
    existing = parse_qsl(parts.query, keep_blank_values=True)
    encoded_query = urlencode(
        [*existing, *_query_items(query)],
        doseq=True,
    )
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, encoded_query, parts.fragment)
    )


def path_param(value: str) -> str:
    """Encode a Retrofit-style path parameter segment."""
    return quote(value, safe="")


def _wire_headers(
    auth_policy: AuthPolicy,
    *,
    session: MobileSession | None,
    static_headers: Mapping[str, str],
    user_agent: str | None,
) -> dict[str, str]:
    headers = _strip_internal_headers(static_headers)
    if auth_policy is AuthPolicy.AUTHENTICATED:
        if session is None:
            raise ValueError("Authenticated requests require a mobile session")
        headers["Authorization"] = f"Bearer {session.access_token}"
        headers["X-id-token"] = session.id_token
    elif auth_policy is AuthPolicy.OPTIONAL_AUTH and session is not None:
        headers["Authorization"] = f"Bearer {session.access_token}"
        headers["X-id-token"] = session.id_token
    if user_agent is not None:
        headers["User-Agent"] = user_agent
    return headers


def _strip_internal_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower()
        not in {header.lower() for header in INTERNAL_AUTH_CONTROL_HEADERS}
    }


def _base_url(
    family: EndpointFamily,
    overrides: Mapping[EndpointFamily, str] | None,
) -> str:
    if overrides is not None and family in overrides:
        return overrides[family]
    return DEFAULT_ENDPOINT_BASE_URLS[family]


def _query_items(
    query: QueryParams,
) -> list[tuple[str, str | int]]:
    items: list[tuple[str, str | int]] = []
    for key, value in query.items():
        values: Sequence[QueryValue]
        if isinstance(value, (str, int, bool)) or value is None:
            values = (value,)
        else:
            values = value
        for item in values:
            if item is None:
                continue
            if isinstance(item, bool):
                items.append((key, str(item).lower()))
            else:
                items.append((key, item))
    return items


__all__ = [
    "AuthPolicy",
    "EndpointFamily",
    "PreparedRequest",
    "RequestSpec",
    "build_url",
    "path_param",
    "prepare_request",
]
