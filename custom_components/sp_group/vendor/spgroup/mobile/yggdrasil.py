"""Read-only Yggdrasil/YCS GraphQL client for SP Group mobile premises data."""

from __future__ import annotations

from collections.abc import Mapping

from .constants import APK_USER_AGENT, MULESOFT_AUTH_BASE_URL
from .errors import SpGroupApiError
from .models import JsonObject, RawResponse
from .request import AuthPolicy, EndpointFamily, RequestSpec, prepare_request
from .resource import parse_mobile_api_error
from .session import MobileSession
from .transport import MobileTransport, as_object


class YggdrasilClient:
    """Async client for read-only Yggdrasil/YCS GraphQL endpoints."""

    def __init__(
        self,
        session: object,
        *,
        base_url: str = MULESOFT_AUTH_BASE_URL,
        user_agent: str = APK_USER_AGENT,
    ) -> None:
        """Initialize the Yggdrasil client."""
        self._transport = MobileTransport(session)  # type: ignore[arg-type]
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent

    @property
    def user_agent(self) -> str:
        """Return the configured Yggdrasil user-agent."""
        return self._user_agent

    async def get_premises_details(self, session: MobileSession) -> JsonObject:
        """Return raw YCS premise details."""
        return await self._post_graphql(
            session,
            _QUERY_YCS_PREMISES_DETAILS_V3,
            context="Yggdrasil premise details",
        )

    async def get_meter_type(
        self,
        session: MobileSession,
        account_no: str,
    ) -> JsonObject:
        """Return raw YCS meter type data for an account."""
        return await self._post_graphql(
            session,
            _QUERY_YCS_GET_METER_TYPE,
            variables={"accountNo": account_no},
            context="Yggdrasil meter type",
        )

    async def get_premise_type(
        self,
        session: MobileSession,
        account_no: str,
    ) -> JsonObject:
        """Return raw YCS premise type data for an account."""
        return await self._post_graphql(
            session,
            _QUERY_YCS_GET_PREMISE_TYPE,
            variables={"accountNo": account_no},
            context="Yggdrasil premise type",
        )

    async def get_invites_list(self, session: MobileSession) -> JsonObject:
        """Return raw YCS premise-sharing invites data."""
        return await self._post_graphql(
            session,
            _QUERY_YCS_SHARED_GET_INVITE_LIST,
            context="Yggdrasil invites list",
        )

    async def _post_graphql(
        self,
        session: MobileSession,
        query: str,
        *,
        context: str,
        variables: Mapping[str, object] | None = None,
    ) -> JsonObject:
        response = await self._request(
            RequestSpec(
                method="POST",
                family=EndpointFamily.MULESOFT_AUTH,
                path="/yggdrasil/graphql",
                auth_policy=AuthPolicy.AUTHENTICATED,
                json_body=_graphql_body(query, variables),
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
        session: MobileSession,
    ) -> RawResponse:
        request = prepare_request(
            spec,
            session=session,
            user_agent=self._user_agent,
            base_url_overrides={EndpointFamily.MULESOFT_AUTH: self._base_url},
        )
        return await self._transport.request_json(
            request.method,
            request.url,
            headers=request.headers,
            json_body=request.json_body,
        )


def _graphql_body(
    query: str,
    variables: Mapping[str, object] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {"query": query}
    if variables is not None:
        body["variables"] = dict(variables)
    return body


_QUERY_YCS_GET_METER_TYPE = """query ($accountNo: String!) {
  meterTypeV1 (accountNo: $accountNo) {
          ami
          interval
  }
}"""

_QUERY_YCS_GET_PREMISE_TYPE = """query($accountNo:String!) {
   premiseTypeForOwnerV1(accountNo: $accountNo)
}
"""

_QUERY_YCS_PREMISES_DETAILS_V3 = """query {
  premiseDetails {
    premises {
        accountNo  
        active
        signedUp
        contestableType
        sharedResource {
            owner
            ownerIamId
            ownerName
            ownerDisplayName
            ownerEmail
            users {
                id
                iamId
                accountNo
                name
                displayName
                email
                fullAccess
                status
            }
        }        
        accounts {
            accountNo
            bpNo
            bpName
            bpType
            salutation
            emailAddress
            mobileNo
            phone
            fax
            accountStatus
            accountType
            retailerIdentifier
            retailerName
            premiseNo
            premiseAddressFull
            premiseAddress {
                blockNo
                floor
                unitNo
                street
                postalCode
            }
            mailingAddress {
                blkOrAptmt
                street
                unit
                postalCode
            }
            system
            ebsAccountNo
            msslAccountNo
            ppms
            moveInDate
            moveOutDate
            activeServices
        }
    }
    linkStatus
    configs {
        maxUsers
    }
  }
}
"""

_QUERY_YCS_SHARED_GET_INVITE_LIST = """query {
  invites {
    accepted
    pending {
      id
      ownerName
      ownerDisplayName
      ownerIamId
    }
    configs {
        maxUsers
    }
  }
}
"""

__all__ = ["YggdrasilClient"]
