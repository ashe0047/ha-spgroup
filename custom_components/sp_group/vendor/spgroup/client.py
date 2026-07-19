"""Compatibility exports for SP Group mobile API clients."""

from __future__ import annotations

from .mobile import JarvisClient, MobileAuthClient


class SpGroupClient:
    """Compatibility facade for the current Home Assistant scaffold.

    Real mobile API behavior lives in :class:`spgroup.mobile.JarvisClient` and
    :class:`spgroup.mobile.MobileAuthClient`. This facade keeps the placeholder
    HA integration importable until it is wired to an injected aiohttp session.
    """


__all__ = ["JarvisClient", "MobileAuthClient", "SpGroupClient"]
