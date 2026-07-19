"""Read-only Jarvis client for SP Group mobile utility data."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time

from .constants import (
    AMI_GROUPS,
    APK_USER_AGENT,
    JARVIS_ACCEPT,
    JARVIS_BASE_URL,
    JARVIS_PUBLIC_BASE_URL,
    PROTOTYPE_USER_AGENT,
    UTILITY_TYPES,
)
from .errors import SpGroupApiError
from .models import JsonObject, RawResponse
from .request import (
    AuthPolicy,
    EndpointFamily,
    RequestSpec,
    path_param,
    prepare_request,
)
from .resource import parse_mobile_api_error
from .session import MobileSession
from .transport import MobileTransport, as_object


class JarvisClient:
    """Async client for SP Group Jarvis utility endpoints."""

    def __init__(
        self,
        session: object,
        *,
        base_url: str = JARVIS_BASE_URL,
        public_base_url: str = JARVIS_PUBLIC_BASE_URL,
        user_agent: str = APK_USER_AGENT,
    ) -> None:
        """Initialize the Jarvis client."""
        self._transport = MobileTransport(session)  # type: ignore[arg-type]
        self._base_url = base_url.rstrip("/")
        self._public_base_url = public_base_url.rstrip("/")
        self._user_agent = user_agent

    @property
    def user_agent(self) -> str:
        """Return the configured Jarvis user-agent."""
        return self._user_agent

    async def get_me(self, session: MobileSession) -> JsonObject:
        """Return the raw utility account response from `/jarvis/v3/me`."""
        return await self._get_object("/jarvis/v3/me", session)

    async def get_history_charts(
        self,
        session: MobileSession,
        premise_id: str,
    ) -> JsonObject:
        """Return raw v4 history charts for a premise."""
        return await self._get_object(
            f"/jarvis/v4/charts/{path_param(premise_id)}",
            session,
        )

    async def get_ami_charts(
        self,
        session: MobileSession,
        premise_id: str,
        start: date | datetime | str,
        end: date | datetime | str,
        grouped_by: str,
        utility_type: str | None = None,
    ) -> JsonObject:
        """Return raw AMI/smart-meter chart data."""
        return await self._post_object(
            "/jarvis/v3/ami/charts",
            session,
            _smart_meter_chart_body(
                premise_id,
                start,
                end,
                grouped_by,
                utility_type,
            ),
            context="AMI charts",
        )

    async def get_meter_reading(
        self,
        session: MobileSession,
        premise_id: str,
    ) -> JsonObject:
        """Return raw meter reading data for a premise."""
        return await self._get_object(
            f"/jarvis/v3/smrd-uportal/{path_param(premise_id)}",
            session,
        )

    async def fetch_account_meters(
        self,
        session: MobileSession,
        account_numbers: Sequence[str],
    ) -> JsonObject:
        """Return raw electric meter data for account numbers."""
        return await self._post_object(
            "/jarvis/v3/smrd/elec-meters",
            session,
            {"account_numbers": list(account_numbers)},
            context="account meters",
        )

    async def fetch_meter_readings(
        self,
        session: MobileSession,
        account_meters: Sequence[tuple[str, str]],
    ) -> JsonObject:
        """Return raw readings for account and meter pairs."""
        return await self._post_object(
            "/jarvis/v3/smrd/elec-meters/readings",
            session,
            {
                "account_meters": [
                    {
                        "account_number": account_number,
                        "meter_number": meter_number,
                    }
                    for account_number, meter_number in account_meters
                ]
            },
            context="meter readings",
        )

    async def get_public_meter_details(self, meter_id: str) -> JsonObject:
        """Return public raw meter details by meter ID."""
        response = await self._request(
            RequestSpec(
                method="GET",
                family=EndpointFamily.MULESOFT_PUBLIC,
                path=f"/jarvis/v3/smrd/anonymous/meter/{path_param(meter_id)}",
                auth_policy=AuthPolicy.NO_AUTH,
            )
        )
        if response.status >= 400:
            detail = parse_mobile_api_error(response.status, response.data)
            raise SpGroupApiError(
                f"public meter details failed: HTTP {response.status}",
                status=response.status,
                body=detail.redacted_body,
                detail=detail,
            )
        data = as_object(response.data, context="public meter details")
        return data

    async def get_ppms_chart(
        self,
        session: MobileSession,
        premise_id: str,
        start: date | datetime | str,
        end: date | datetime | str,
        grouped_by: str,
        utility_type: str | None = None,
    ) -> JsonObject:
        """Return raw PPMS chart data using the APK smart-meter chart body."""
        return await self._post_object(
            "/jarvis/v3/ppms/charts",
            session,
            _smart_meter_chart_body(
                premise_id,
                start,
                end,
                grouped_by,
                utility_type,
            ),
            context="PPMS charts",
        )

    async def get_credit_balance(
        self,
        session: MobileSession,
        premise_id: str,
    ) -> JsonObject:
        """Return raw PPMS credit balance data for a premise."""
        return await self._get_object(
            f"/jarvis/v3/ppms/balance/{path_param(premise_id)}",
            session,
        )

    async def _get_object(self, path: str, session: MobileSession) -> JsonObject:
        response = await self._request(
            RequestSpec(
                method="GET",
                family=EndpointFamily.MULESOFT_AUTH,
                path=path,
                auth_policy=AuthPolicy.AUTHENTICATED,
                static_headers=_JARVIS_JSON_HEADERS,
            ),
            session=session,
        )
        if response.status >= 400:
            detail = parse_mobile_api_error(response.status, response.data)
            raise SpGroupApiError(
                f"{path} failed: HTTP {response.status}",
                status=response.status,
                body=detail.redacted_body,
                detail=detail,
            )
        data = as_object(response.data, context=path)
        return data

    async def _post_object(
        self,
        path: str,
        session: MobileSession,
        body: dict[str, object],
        *,
        context: str,
    ) -> JsonObject:
        response = await self._request(
            RequestSpec(
                method="POST",
                family=EndpointFamily.MULESOFT_AUTH,
                path=path,
                auth_policy=AuthPolicy.AUTHENTICATED,
                static_headers=_JARVIS_JSON_HEADERS,
                json_body=body,
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


def _smart_meter_chart_body(
    premise_id: str,
    start: date | datetime | str,
    end: date | datetime | str,
    grouped_by: str,
    utility_type: str | None,
) -> dict[str, object]:
    if grouped_by not in AMI_GROUPS:
        raise ValueError(f"Unsupported grouped_by value: {grouped_by}")
    if utility_type is not None and utility_type not in UTILITY_TYPES:
        raise ValueError(f"Unsupported utility_type value: {utility_type}")
    return {
        "premise_id": premise_id,
        "start": format_ami_datetime(start, end_of_day=False),
        "end": format_ami_datetime(end, end_of_day=True),
        "grouped_by": grouped_by,
        "utility_type": utility_type or "",
    }


def format_ami_datetime(value: date | datetime | str, *, end_of_day: bool) -> str:
    """Format AMI chart dates as `yyyyMMddHHmmss`."""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.combine(value, time.max if end_of_day else time.min)
    return dt.strftime("%Y%m%d%H%M%S")


__all__ = ["JarvisClient", "PROTOTYPE_USER_AGENT", "format_ami_datetime"]

_JARVIS_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": JARVIS_ACCEPT,
}
