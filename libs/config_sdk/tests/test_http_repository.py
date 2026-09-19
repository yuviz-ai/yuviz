"""
In-process against the real services.config.app FastAPI app via
httpx.ASGITransport — same convention services/config/tests/test_api.py
uses (real routing/validation/auth, no live uvicorn process). Proves
HttpConfigRepository's login-then-GET flow against the real auth system
built in services/config/auth.py, not a mock of it.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport

from libs.config_sdk.exceptions import RepositoryUnavailableError
from libs.config_sdk.repositories.http_repository import HttpConfigRepository
from services.config import agents, provider_configs, tenants, users
from services.config.app import app


@pytest.fixture
async def service_account():
    email = f"test-service-{uuid.uuid4().hex[:8]}@example.com"
    user = await users.create_user(email=email, password="service-password", role="viewer")
    yield {"email": email, "password": "service-password"}
    from services.config import db
    pool = await db.get_pool()
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])


def _repo(service_account) -> HttpConfigRepository:
    return HttpConfigRepository(
        base_url="http://test",
        service_email=service_account["email"],
        service_password=service_account["password"],
        transport=ASGITransport(app=app),
    )


async def test_fetch_tenant_authenticates_and_returns_data(service_account, pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    created = await tenants.create_tenant(name="Test Tenant", slug=slug)

    repo = _repo(service_account)
    result = await repo.fetch_tenant(slug)
    assert result is not None
    assert result["slug"] == slug

    await repo.close()
    await pool.execute("DELETE FROM tenants WHERE id = $1", created["id"])


async def test_fetch_tenant_unknown_returns_none(service_account):
    repo = _repo(service_account)
    result = await repo.fetch_tenant(f"no-such-tenant-{uuid.uuid4().hex[:8]}")
    assert result is None
    await repo.close()


async def test_wrong_service_password_raises_unavailable(pool):
    email = f"test-service-{uuid.uuid4().hex[:8]}@example.com"
    user = await users.create_user(email=email, password="correct-password", role="viewer")

    repo = HttpConfigRepository(
        base_url="http://test", service_email=email, service_password="wrong-password",
        transport=ASGITransport(app=app),
    )
    with pytest.raises(RepositoryUnavailableError):
        await repo.fetch_tenant("anything")

    await repo.close()
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])


async def test_fetch_agent_and_provider_config(service_account, pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    tenant = await tenants.create_tenant(name="Test Tenant", slug=slug)
    await agents.create_agent(
        tenant_id=tenant["id"], slug="reception", name="Reception", tenant_slug=slug,
    )
    provider = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name="STT", role="stt", engine="deepgram",
    )

    repo = _repo(service_account)
    agent_result = await repo.fetch_agent(slug, "reception")
    assert agent_result is not None and agent_result["slug"] == "reception"

    provider_result = await repo.fetch_provider_config(provider["id"])
    assert provider_result is not None and provider_result["engine"] == "deepgram"

    await repo.close()
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


async def test_fetch_call_flow_requests_exact_path_and_404_is_none():
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"access_token": "tok"})
        seen_paths.append(request.url.path)
        return httpx.Response(404)

    repo = HttpConfigRepository(
        base_url="http://test", service_email="svc@example.com", service_password="pw",
        transport=httpx.MockTransport(handler),
    )
    result = await repo.fetch_call_flow("acme", "flow-1")
    assert result is None
    assert seen_paths == ["/tenants/acme/call-flows/flow-1/published"]

    await repo.close()
