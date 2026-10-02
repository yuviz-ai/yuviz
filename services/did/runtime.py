"""Lazy DidProviderManager singleton; not on app.state because ASGITransport tests skip lifespan."""

from __future__ import annotations

from .provider_manager import DidProviderManager
from .secret_resolver import CompositeSecretResolver

_provider_manager: DidProviderManager | None = None


def get_provider_manager() -> DidProviderManager:
    global _provider_manager
    if _provider_manager is None:
        _provider_manager = DidProviderManager(CompositeSecretResolver())
    return _provider_manager
