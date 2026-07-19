"""Auth0 client for SP Group mobile authentication."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .constants import (
    AUTH0_AUDIENCE,
    AUTH0_BASE_URL,
    AUTH0_CLIENT_ID,
    AUTH0_GET_JSON_HEADERS,
    AUTH0_JSON_HEADERS,
    AUTH0_REALM,
    DEFAULT_SCOPE,
    MFA_OOB_GRANT_TYPE,
    MFA_OTP_GRANT_TYPE,
    PASSWORD_REALM_GRANT_TYPE,
    REFRESH_TOKEN_GRANT_TYPE,
)
from .errors import MfaRequired, SpGroupAuthError
from .models import MfaChallenge, RawResponse
from .request import AuthPolicy, EndpointFamily, RequestSpec, prepare_request
from .resource import parse_mobile_api_error
from .session import MobileSession, extract_identity_metadata, extract_token_metadata
from .transport import MobileTransport, as_object


class MobileAuthClient:
    """Async Auth0 client for the SP Group mobile flow."""

    def __init__(
        self,
        session: object,
        *,
        base_url: str = AUTH0_BASE_URL,
        client_id: str = AUTH0_CLIENT_ID,
        audience: str = AUTH0_AUDIENCE,
        default_scope: str = DEFAULT_SCOPE,
    ) -> None:
        """Initialize the auth client."""
        self._transport = MobileTransport(session)  # type: ignore[arg-type]
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._audience = audience
        self._default_scope = default_scope

    async def attempt_password_realm_login(
        self,
        username: str,
        password: str,
        *,
        scope: str | None = None,
    ) -> MobileSession:
        """Attempt password-realm login and return a session."""
        body: dict[str, object] = {
            "grant_type": PASSWORD_REALM_GRANT_TYPE,
            "client_id": self._client_id,
            "audience": self._audience,
            "username": username,
            "password": password,
            "scope": scope or self._default_scope,
            "realm": AUTH0_REALM,
        }
        data = await self._post_oauth_token(body, context="password login")
        if data.get("error") == "mfa_required":
            mfa_token = data.get("mfa_token")
            if not isinstance(mfa_token, str) or not mfa_token:
                raise SpGroupAuthError(
                    "MFA required but response did not include mfa_token"
                )
            description = data.get("error_description")
            raise MfaRequired(
                mfa_token,
                description if isinstance(description, str) else None,
            )
        self._raise_auth_error(data, context="password login")
        return self._session_from_token_response(data, fallback_scope=scope)

    async def get_mfa_authenticators(self, mfa_token: str) -> list[dict[str, Any]]:
        """Return raw MFA authenticators for an MFA token."""
        response = await self._request(
            RequestSpec(
                method="GET",
                family=EndpointFamily.AUTH0,
                path="/mfa/authenticators",
                auth_policy=AuthPolicy.NO_AUTH,
                static_headers={
                    **AUTH0_GET_JSON_HEADERS,
                    "Authorization": f"Bearer {mfa_token}",
                },
            )
        )
        if response.status >= 400:
            self._raise_auth_http_error(
                response.data,
                context="MFA authenticators",
                status=response.status,
            )
        if not isinstance(response.data, list):
            raise SpGroupAuthError("MFA authenticators response was not a list")
        return [item for item in response.data if isinstance(item, dict)]

    async def trigger_mfa_challenge(
        self,
        mfa_token: str,
        *,
        authenticator_id: str | None = None,
    ) -> MfaChallenge:
        """Trigger an OOB MFA challenge and return the OOB code."""
        body: dict[str, object] = {
            "client_id": self._client_id,
            "mfa_token": mfa_token,
            "challenge_type": "oob",
        }
        if authenticator_id is not None:
            body["authenticator_id"] = authenticator_id

        response = await self._request(
            RequestSpec(
                method="POST",
                family=EndpointFamily.AUTH0,
                path="/mfa/challenge",
                auth_policy=AuthPolicy.NO_AUTH,
                static_headers=AUTH0_JSON_HEADERS,
                json_body=body,
            )
        )
        if response.status >= 400:
            self._raise_auth_http_error(
                response.data,
                context="MFA challenge",
                status=response.status,
            )
        data = as_object(response.data, context="MFA challenge")
        oob_code = data.get("oob_code")
        if not isinstance(oob_code, str) or not oob_code:
            raise SpGroupAuthError("MFA challenge response missing oob_code")
        return MfaChallenge(oob_code=oob_code, authenticator_id=authenticator_id)

    async def complete_mfa_oob(
        self,
        mfa_token: str,
        oob_code: str,
        otp: str,
    ) -> MobileSession:
        """Complete an OOB MFA challenge with the OTP binding code."""
        data = await self._post_oauth_token(
            {
                "grant_type": MFA_OOB_GRANT_TYPE,
                "client_id": self._client_id,
                "mfa_token": mfa_token,
                "oob_code": oob_code,
                "binding_code": otp,
            },
            context="MFA OOB completion",
        )
        self._raise_auth_error(data, context="MFA OOB completion")
        return self._session_from_token_response(data)

    async def complete_mfa_otp(self, mfa_token: str, otp: str) -> MobileSession:
        """Complete a TOTP/authenticator-app MFA challenge."""
        data = await self._post_oauth_token(
            {
                "grant_type": MFA_OTP_GRANT_TYPE,
                "client_id": self._client_id,
                "mfa_token": mfa_token,
                "otp": otp,
            },
            context="MFA OTP completion",
        )
        self._raise_auth_error(data, context="MFA OTP completion")
        return self._session_from_token_response(data)

    async def refresh_session(self, session: MobileSession) -> MobileSession:
        """Refresh a mobile session using its refresh token."""
        if not session.refresh_token:
            raise SpGroupAuthError("Cannot refresh session without refresh token")
        data = await self._post_oauth_token(
            {
                "grant_type": REFRESH_TOKEN_GRANT_TYPE,
                "client_id": self._client_id,
                "refresh_token": session.refresh_token,
                "scope": session.scope or self._default_scope,
            },
            context="refresh",
        )
        self._raise_auth_error(data, context="refresh")
        return self._session_from_token_response(
            data,
            fallback_refresh_token=session.refresh_token,
            fallback_scope=session.scope,
        )

    async def request_force_mfa_challenge_token(
        self,
        refresh_token: str,
    ) -> dict[str, Any]:
        """Request the APK force-MFA refresh flow using a refresh token."""
        if not refresh_token:
            raise SpGroupAuthError("Cannot request force MFA without refresh token")
        response = await self._request(
            RequestSpec(
                method="POST",
                family=EndpointFamily.AUTH0,
                path="/oauth/token",
                auth_policy=AuthPolicy.NO_AUTH,
                static_headers=AUTH0_JSON_HEADERS,
                json_body={
                    "grant_type": REFRESH_TOKEN_GRANT_TYPE,
                    "client_id": self._client_id,
                    "refresh_token": refresh_token,
                    "force_mfa": True,
                },
            )
        )
        if response.status >= 400:
            self._raise_auth_http_error(
                response.data,
                context="force MFA refresh",
                status=response.status,
            )
        if response.data is None:
            data: dict[str, Any] = {}
        else:
            data = as_object(response.data, context="force MFA refresh")
        return data

    async def _post_oauth_token(
        self,
        body: dict[str, object],
        *,
        context: str,
    ) -> dict[str, Any]:
        response = await self._request(
            RequestSpec(
                method="POST",
                family=EndpointFamily.AUTH0,
                path="/oauth/token",
                auth_policy=AuthPolicy.NO_AUTH,
                static_headers=AUTH0_JSON_HEADERS,
                json_body=body,
            )
        )
        if response.status >= 400:
            if (
                isinstance(response.data, dict)
                and response.data.get("error") == "mfa_required"
            ):
                return as_object(response.data, context=context)
            self._raise_auth_http_error(
                response.data,
                context=context,
                status=response.status,
            )
        data = as_object(response.data, context=context)
        return data

    async def _request(self, spec: RequestSpec) -> RawResponse:
        request = prepare_request(
            spec,
            user_agent=None,
            base_url_overrides={EndpointFamily.AUTH0: self._base_url},
        )
        return await self._transport.request_json(
            request.method,
            request.url,
            headers=request.headers,
            json_body=request.json_body,
        )

    def _session_from_token_response(
        self,
        data: dict[str, Any],
        *,
        fallback_refresh_token: str = "",
        fallback_scope: str | None = None,
    ) -> MobileSession:
        access_token = data.get("access_token")
        id_token = data.get("id_token")
        if not isinstance(access_token, str) or not isinstance(id_token, str):
            raise SpGroupAuthError("Token response missing access_token or id_token")
        refresh_token = data.get("refresh_token")
        scope = data.get("scope")
        return MobileSession(
            access_token=access_token,
            id_token=id_token,
            refresh_token=(
                refresh_token
                if isinstance(refresh_token, str)
                else fallback_refresh_token
            ),
            scope=scope if isinstance(scope, str) else fallback_scope,
            token_metadata=extract_token_metadata(access_token),
            identity_metadata=extract_identity_metadata(id_token),
            created_at=datetime.now(UTC),
        )

    @staticmethod
    def _raise_auth_error(
        data: dict[str, Any],
        *,
        context: str,
        status: int | None = None,
    ) -> None:
        error = data.get("error")
        if not error:
            return
        description = data.get("error_description")
        detail = f"{error}"
        if isinstance(description, str) and description:
            detail = f"{detail} - {description}"
        api_error = parse_mobile_api_error(status or 0, data)
        raise SpGroupAuthError(
            f"{context} failed: {detail}; response={api_error.redacted_body}",
            status=status,
            body=api_error.redacted_body,
            detail=api_error,
        )

    @staticmethod
    def _raise_auth_http_error(
        data: object,
        *,
        context: str,
        status: int,
    ) -> None:
        if isinstance(data, dict):
            MobileAuthClient._raise_auth_error(data, context=context, status=status)
        detail = parse_mobile_api_error(status, data)
        raise SpGroupAuthError(
            f"{context} failed: HTTP {status}; response={detail.redacted_body}",
            status=status,
            body=detail.redacted_body,
            detail=detail,
        )
