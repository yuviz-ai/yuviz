"""
FIX 2 — TOOLEXEC_ARGS_HMAC_KEY_REF must resolve EAGERLY, at service
startup, the same fail-loud posture JWT_SECRET already has: a
misconfigured deploy must not pass /health and then fail on the first
real chain's side-effect claim.

db.get_pool()/close_pool() are monkeypatched to no-ops here — this test's
subject is the HMAC key resolution specifically, not the DB pool
lifecycle (already covered elsewhere), and the pool is a process-wide
singleton shared with every other test in this session; actually closing
it mid-suite would be a real side effect on unrelated tests.
"""

from __future__ import annotations

import pytest

from services.toolexec import app as app_module
from services.toolexec import executor


@pytest.fixture(autouse=True)
def _no_op_db_pool(monkeypatch):
    async def _get_pool():
        return None

    async def _close_pool():
        return None

    monkeypatch.setattr(app_module.db, "get_pool", _get_pool)
    monkeypatch.setattr(app_module.db, "close_pool", _close_pool)


@pytest.mark.asyncio
async def test_service_fails_to_start_when_hmac_key_ref_is_absent(monkeypatch):
    monkeypatch.delenv("TOOLEXEC_ARGS_HMAC_KEY_REF", raising=False)
    executor._hmac_key_cache.clear()

    with pytest.raises(RuntimeError, match="TOOLEXEC_ARGS_HMAC_KEY_REF"):
        async with app_module.lifespan(app_module.app):
            pass  # never reached — startup must raise before yield


@pytest.mark.asyncio
async def test_service_fails_to_start_when_hmac_key_ref_is_unresolvable(monkeypatch):
    monkeypatch.setenv("TOOLEXEC_ARGS_HMAC_KEY_REF", "env:TOOLEXEC_HMAC_KEY_THAT_IS_NEVER_SET")
    monkeypatch.delenv("TOOLEXEC_HMAC_KEY_THAT_IS_NEVER_SET", raising=False)
    executor._hmac_key_cache.clear()

    with pytest.raises(Exception):
        async with app_module.lifespan(app_module.app):
            pass


@pytest.mark.asyncio
async def test_service_starts_when_hmac_key_ref_resolves(monkeypatch):
    monkeypatch.setenv("TOOLEXEC_TEST_HMAC_KEY", "a-real-test-key")
    monkeypatch.setenv("TOOLEXEC_ARGS_HMAC_KEY_REF", "env:TOOLEXEC_TEST_HMAC_KEY")
    executor._hmac_key_cache.clear()

    started = False
    async with app_module.lifespan(app_module.app):
        started = True
    assert started
