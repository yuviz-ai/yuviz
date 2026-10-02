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
    """Each TestClient runs its own event loop; reset module redis clients to avoid "Event loop is closed"."""
    _idempotency._client = None
    _config_cache._client = None
    yield
    _idempotency._client = None
    _config_cache._client = None
