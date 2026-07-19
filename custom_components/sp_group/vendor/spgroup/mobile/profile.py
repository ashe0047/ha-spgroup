"""Read-only Profile client for SP Group mobile account metadata."""

from __future__ import annotations

from typing import Any

from .constants import (
    APK_USER_AGENT,
    MULESOFT_AUTH_BASE_URL,
    MULESOFT_PUBLIC_BASE_URL,
)
from .errors import SpGroupApiError
from .models import JsonObject, RawResponse
from .request import AuthPolicy, EndpointFamily, RequestSpec, prepare_request
from .resource import parse_mobile_api_error
from .session import MobileSession
from .transport import MobileTransport, as_array, as_object


class ProfileClient:
    """Async client for SP Group Profile MuleSoft endpoints."""

    def __init__(
        self,
        session: object,
        *,
        base_url: str = MULESOFT_AUTH_BASE_URL,
        public_base_url: str = MULESOFT_PUBLIC_BASE_URL,
        user_agent: str = APK_USER_AGENT,
    ) -> None:
        """Initialize the Profile client."""
        self._transport = MobileTransport(session)  # type: ignore[arg-type]
        self._base_url = base_url.rstrip("/")
        self._public_base_url = public_base_url.rstrip("/")
        self._user_agent = user_agent

    @property
    def user_agent(self) -> str:
        """Return the configured Profile user-agent."""
        return self._user_agent

    async def get_client_config(self) -> JsonObject:
        """Return the raw public client configuration."""
        response = await self._request(
            RequestSpec(
                method="GET",
                family=EndpointFamily.MULESOFT_PUBLIC,
                path="/profile/v2/client",
                auth_policy=AuthPolicy.NO_AUTH,
            )
        )
        if response.status >= 400:
            detail = parse_mobile_api_error(response.status, response.data)
            raise SpGroupApiError(
                f"client config failed: HTTP {response.status}",
                status=response.status,
                body=detail.redacted_body,
                detail=detail,
            )
        data = as_object(response.data, context="client config")
        return data

    async def get_user_profile(self, session: MobileSession) -> JsonObject:
        """Return the raw authenticated user profile."""
        return await self._get_object(
            "/profile/iam/selfservice/profile",
            session,
            context="user profile",
        )

    async def get_mfa_authenticators(
        self,
        session: MobileSession,
    ) -> list[dict[str, Any]]:
        """Return raw Profile self-service MFA authenticator records."""
        response = await self._request(
            RequestSpec(
                method="GET",
                family=EndpointFamily.MULESOFT_AUTH,
                path="/profile/iam/selfservice/mfa/authenticators",
                auth_policy=AuthPolicy.AUTHENTICATED,
            ),
            session=session,
        )
        if response.status >= 400:
            detail = parse_mobile_api_error(response.status, response.data)
            raise SpGroupApiError(
                f"profile MFA authenticators failed: HTTP {response.status}",
                status=response.status,
                body=detail.redacted_body,
                detail=detail,
            )
        data = as_array(response.data, context="profile MFA authenticators")
        return [item for item in data if isinstance(item, dict)]

    async def get_notification_settings(self, session: MobileSession) -> JsonObject:
        """Return raw notification settings."""
        return await self._get_object(
            "/profile/iam/selfservice/notifs",
            session,
            context="notification settings",
        )

    async def get_referral_code(self, session: MobileSession) -> JsonObject:
        """Return the raw referral code payload."""
        return await self._get_object(
            "/profile/iam/selfservice/referral-code",
            session,
            context="referral code",
        )

    async def get_segments(self, session: MobileSession) -> JsonObject:
        """Return raw segment data."""
        return await self._get_object(
            "/profile/iam/segment",
            session,
            context="segments",
        )

    async def get_persona_stats(self, session: MobileSession) -> JsonObject:
        """Return raw persona stats data."""
        return await self._get_object(
            "/profile/iam/segment/persona/stats",
            session,
            context="persona stats",
        )

    async def _get_object(
        self,
        path: str,
        session: MobileSession,
        *,
        context: str,
    ) -> JsonObject:
        response = await self._request(
            RequestSpec(
                method="GET",
                family=EndpointFamily.MULESOFT_AUTH,
                path=path,
                auth_policy=AuthPolicy.AUTHENTICATED,
            ),
            session=session,
        )
        if response.status >= 400:
            detail = parse_mobile_api_error(response.status, response.data)
            raise SpGroupApiError(
                f"{context} failed: HTTP {response.status}",
                status=response.status,
                body=detail.redacted_body,
                detail=detail,
            )
        data = as_object(response.data, context=context)
        return data

    async def _request(
        self,
        spec: RequestSpec,
        *,
        session: MobileSession | None = None,
    ) -> RawResponse:
        request = prepare_request(
            spec,
            session=session,
            user_agent=self._user_agent,
            base_url_overrides={
                EndpointFamily.MULESOFT_AUTH: self._base_url,
                EndpointFamily.MULESOFT_PUBLIC: self._public_base_url,
            },
        )
        return await self._transport.request_json(
            request.method,
            request.url,
            headers=request.headers,
            json_body=request.json_body,
        )


__all__ = ["ProfileClient"]
