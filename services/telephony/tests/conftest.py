from __future__ import annotations

import os

os.environ.setdefault("TELEPHONY_PUBLIC_BASE_URL", "https://telephony.example.test")
os.environ.setdefault("CONFIG_SERVICE_URL", "http://localhost:8000")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-real")

from libs.config_sdk.secrets import generate_key  # noqa: E402

os.environ.setdefault("SECRET_ENCRYPTION_KEY", generate_key())

# Registers "fake" (hidden) alongside vobiz/cloudonix for every test in this package.
from libs.telephony_sdk import providers  # noqa: F401,E402

import pytest  # noqa: E402

from services.config import cache as _config_cache  # noqa: E402
from services.telephony import idempotency as _idempotency  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_redis_clients_per_event_loop():
    """Each TestClient(app) in this package runs its own anyio event loop —
    a module-level redis client created under a prior test's loop raises
    "Event loop is closed" on first use here, so every test starts with a
    fresh client bound to whichever loop it actually runs under."""
    _idempotency._client = None
    _config_cache._client = None
    yield
    _idempotency._client = None
    _config_cache._client = None
