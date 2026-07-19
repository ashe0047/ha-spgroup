"""Session and token metadata helpers for SP Group mobile auth."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Self

from .constants import TOKEN_EXPIRY_BUFFER_SECONDS

DEFAULT_TOKEN_EXPIRY_BUFFER = timedelta(seconds=TOKEN_EXPIRY_BUFFER_SECONDS)


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """Safe metadata extracted from an access token."""

    issued_at_epoch: int
    expires_at_epoch: int
    issuer: str | None
    audience: tuple[str, ...]
    scope: str | None
    fingerprint: str

    @property
    def expires_at(self) -> datetime:
        """Return the token expiry as a timezone-aware datetime."""
        return datetime.fromtimestamp(self.expires_at_epoch, tz=UTC)


@dataclass(frozen=True, slots=True)
class IdentityMetadata:
    """Stable account identity extracted from an ID token."""

    subject: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class MobileSession:
    """Serializable SP Group mobile token session."""

    access_token: str
    id_token: str
    refresh_token: str
    scope: str | None = None
    token_metadata: TokenMetadata | None = None
    identity_metadata: IdentityMetadata | None = None
    created_at: datetime | None = None

    @property
    def expired(self) -> bool:
        """Return whether the access token is expired or inside the APK buffer."""
        return self.expires_within()

    def expires_within(
        self,
        buffer: timedelta = DEFAULT_TOKEN_EXPIRY_BUFFER,
    ) -> bool:
        """Return whether the access token expires within the provided buffer."""
        if self.token_metadata is None:
            return False
        return datetime.now(UTC) >= self.token_metadata.expires_at - buffer

    def to_dict(self) -> dict[str, Any]:
        """Serialize the session without mutating token values."""
        return {
            "accessToken": self.access_token,
            "idToken": self.id_token,
            "refreshToken": self.refresh_token,
            "scope": self.scope,
            "tokenMetadata": (
                {
                    "issuedAtEpoch": self.token_metadata.issued_at_epoch,
                    "expiresAtEpoch": self.token_metadata.expires_at_epoch,
                    "issuer": self.token_metadata.issuer,
                    "audience": list(self.token_metadata.audience),
                    "scope": self.token_metadata.scope,
                    "fingerprint": self.token_metadata.fingerprint,
                }
                if self.token_metadata
                else None
            ),
            "identityMetadata": (
                {
                    "subject": self.identity_metadata.subject,
                    "fingerprint": self.identity_metadata.fingerprint,
                }
                if self.identity_metadata
                else None
            ),
            "createdAt": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize a session saved by :meth:`to_dict`."""
        metadata = data.get("tokenMetadata")
        token_metadata = None
        if isinstance(metadata, dict):
            audience = metadata.get("audience")
            token_metadata = TokenMetadata(
                issued_at_epoch=int(metadata["issuedAtEpoch"]),
                expires_at_epoch=int(metadata["expiresAtEpoch"]),
                issuer=metadata.get("issuer"),
                audience=tuple(audience if isinstance(audience, list) else []),
                scope=metadata.get("scope"),
                fingerprint=str(metadata["fingerprint"]),
            )

        identity = data.get("identityMetadata")
        identity_metadata = None
        if isinstance(identity, dict):
            subject = identity.get("subject")
            fingerprint = identity.get("fingerprint")
            if isinstance(subject, str) and subject:
                identity_metadata = IdentityMetadata(
                    subject=subject,
                    fingerprint=str(fingerprint or ""),
                )

        created_at = data.get("createdAt")
        return cls(
            access_token=str(data["accessToken"]),
            id_token=str(data["idToken"]),
            refresh_token=str(data.get("refreshToken") or ""),
            scope=data.get("scope"),
            token_metadata=token_metadata,
            identity_metadata=identity_metadata,
            created_at=(
                datetime.fromisoformat(created_at)
                if isinstance(created_at, str) and created_at
                else None
            ),
        )


def extract_token_metadata(token: str) -> TokenMetadata | None:
    """Extract safe metadata from a JWT access token."""
    payload = _decode_jwt_payload(token)
    if payload is None:
        return None

    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        return None

    audience_value = payload.get("aud")
    audience: tuple[str, ...]
    if isinstance(audience_value, str):
        audience = (audience_value,)
    elif isinstance(audience_value, list):
        audience = tuple(str(item) for item in audience_value)
    else:
        audience = ()

    return TokenMetadata(
        issued_at_epoch=issued_at,
        expires_at_epoch=expires_at,
        issuer=payload.get("iss") if isinstance(payload.get("iss"), str) else None,
        audience=audience,
        scope=payload.get("scope") if isinstance(payload.get("scope"), str) else None,
        fingerprint=_fingerprint_token(token),
    )


def extract_identity_metadata(token: str) -> IdentityMetadata | None:
    """Extract the stable Auth0 subject from a JWT ID token."""
    payload = _decode_jwt_payload(token)
    if payload is None:
        return None

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        return None

    return IdentityMetadata(
        subject=subject,
        fingerprint=_fingerprint_token(token),
    )


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{payload}{padding}").decode()
        value = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _fingerprint_token(token: str) -> str:
    header_payload = ".".join(token.split(".")[:2])
    hash_value = 0
    for char in header_payload:
        hash_value = ((hash_value << 5) - hash_value) + ord(char)
        hash_value = _to_signed_int32(hash_value)
    return f"{abs(hash_value):08x}"


def _to_signed_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    return value
