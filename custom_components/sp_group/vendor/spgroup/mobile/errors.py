"""Error types for the SP Group mobile SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .resource import MobileApiError


class SpGroupMobileError(Exception):
    """Base error for SP Group mobile SDK failures."""


class SpGroupTransportError(SpGroupMobileError):
    """Raised when an HTTP request or response parse fails."""


class SpGroupAuthError(SpGroupMobileError):
    """Raised when Auth0 authentication fails."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: object | None = None,
        detail: MobileApiError | None = None,
    ) -> None:
        """Initialize an auth error."""
        self.status = status
        self.body = body
        self.detail = detail
        super().__init__(message)


class MfaRequired(SpGroupAuthError):
    """Raised when password-realm login requires MFA completion."""

    def __init__(self, mfa_token: str, description: str | None = None) -> None:
        """Initialize the MFA challenge error."""
        self.mfa_token = mfa_token
        self.description = description
        message = "MFA required"
        if description:
            message = f"{message}: {description}"
        super().__init__(message)


class SpGroupApiError(SpGroupMobileError):
    """Raised when a SP Group API endpoint returns an error."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: object | None = None,
        detail: MobileApiError | None = None,
    ) -> None:
        """Initialize an API error."""
        self.status = status
        self.body = body
        self.detail = detail
        super().__init__(message)
