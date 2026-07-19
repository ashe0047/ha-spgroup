"""Compatibility exports for SP Group mobile auth."""

from __future__ import annotations

from .mobile import MfaRequired, MobileAuthClient, MobileSession, TokenMetadata
from .mobile.errors import SpGroupAuthError

__all__ = [
    "MfaRequired",
    "MobileAuthClient",
    "MobileSession",
    "SpGroupAuthError",
    "TokenMetadata",
]
