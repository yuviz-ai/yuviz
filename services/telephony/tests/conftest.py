from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time

os.environ.setdefault("TELEPHONY_PUBLIC_BASE_URL", "https://telephony.example.test")
os.environ.setdefault("CONFIG_SERVICE_URL", "http://localhost:8000")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-real")

from libs.config_sdk.secrets import generate_key  # noqa: E402

os.environ.setdefault("SECRET_ENCRYPTION_KEY", generate_key())

# Registers "fake" (hidden) alongside vobiz/cloudonix for every test in this package.
from libs.telephony_sdk import providers  # noqa: F401,E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
import redis  # noqa: E402
import redis.asyncio as redis_async  # noqa: E402

from services.config import cache as _config_cache  # noqa: E402
from services.telephony import idempotency as _idempotency  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_redis_clients_per_event_loop(_scratch_redis_port, monkeypatch):
    """Every test talks to the throwaway Redis, never the dev one. Each TestClient runs its own event
    loop, so module redis clients are reset to avoid "Event loop is closed"."""
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{_scratch_redis_port}/0")
    redis.Redis(port=_scratch_redis_port).flushall()
    _idempotency._client = None
    _config_cache._client = None
    yield
    _idempotency._client = None
    _config_cache._client = None


@pytest.fixture(scope="session")
def _scratch_redis_port(tmp_path_factory):
    """A throwaway redis-server, so stream and nonce assertions run against real Redis."""
    if shutil.which("redis-server") is None:
        pytest.skip("BLOCKED-ENV: redis-server not installed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        ["redis-server", "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no",
         "--dir", str(tmp_path_factory.mktemp("redis"))],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("throwaway redis did not start")
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest_asyncio.fixture(loop_scope="session")
async def scratch_redis(_scratch_redis_port):
    """Async client on the throwaway Redis that every test in this package already uses."""
    client = redis_async.Redis(port=_scratch_redis_port, decode_responses=True)
    await client.flushall()
    yield client
    await client.flushall()
    await client.aclose()
