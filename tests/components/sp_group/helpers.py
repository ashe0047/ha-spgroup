"""Helpers for SP Group Home Assistant tests."""

from spgroup.mobile import IdentityMetadata, MobileSession


def make_mobile_session(
    subject: str,
    *,
    access_token: str = "access-token",
    id_token: str = "id-token",
    refresh_token: str = "refresh-token",
) -> MobileSession:
    """Create a refresh-capable mobile session with stable identity metadata."""
    return MobileSession(
        access_token=access_token,
        id_token=id_token,
        refresh_token=refresh_token,
        identity_metadata=IdentityMetadata(
            subject=subject,
            fingerprint="identity-fingerprint",
        ),
    )
