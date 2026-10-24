"""Every /campaigns/{campaign_id} route answers a foreign campaign id exactly as it answers an unknown one."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import Header

from libs.tenancy import set_caller_tenant
from services.campaigns import app as app_module
from services.campaigns.billing import NullBillingAdmission
from services.campaigns.routers import campaigns as campaigns_router
from services.campaigns.tests import seed, world as w
from services.config.auth import CurrentUser
from services.config.deps import get_current_user

_CSV = {"file": ("c.csv", b"phone_number\n+14155550123\n", "text/csv")}
# (method, path template) -> request kwargs. The walk below fails if the router has a route this table lacks.
_REQUESTS = {
    ("GET", "/campaigns/{campaign_id}"): {},
    ("PATCH", "/campaigns/{campaign_id}"): {"json": {"name": "renamed"}},
    ("GET", "/campaigns/{campaign_id}/progress"): {},
    ("GET", "/campaigns/{campaign_id}/readiness"): {},
    ("GET", "/campaigns/{campaign_id}/attempts"): {},
    ("GET", "/campaigns/{campaign_id}/contacts"): {},
    ("POST", "/campaigns/{campaign_id}/contacts/upload"): {"files": _CSV},
    ("POST", "/campaigns/{campaign_id}/start"): {},
    ("POST", "/campaigns/{campaign_id}/pause"): {},
    ("POST", "/campaigns/{campaign_id}/resume"): {},
    ("POST", "/campaigns/{campaign_id}/cancel"): {},
    ("DELETE", "/campaigns/{campaign_id}"): {},
    ("POST", "/campaigns/{campaign_id}/recall"): {"json": {"contact_ids": [str(uuid.uuid4())]}},
}


def _routes() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in campaigns_router.router.routes if "{campaign_id}" in route.path
        for method in route.methods
    }


@pytest.fixture
async def api(scratch_pool):
    user_id = str(await scratch_pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", f"{uuid.uuid4()}@example.test",
    ))

    async def fake_user(x_role: str = Header(), x_tenant: str | None = Header(default=None)):
        set_caller_tenant(x_tenant)
        return CurrentUser(id=user_id, email="t@example.test", role=x_role, tenant_id=x_tenant)

    app = app_module.app
    app.dependency_overrides[get_current_user] = fake_user
    app.state.dial_client, app.state.billing = w.FakeDial(), NullBillingAdmission()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


def test_the_table_covers_every_campaign_id_route():
    assert len(_routes()) == len(_REQUESTS) == 13
    assert _routes() == set(_REQUESTS)


async def test_a_foreign_tenant_gets_the_same_response_as_for_an_unknown_id_on_every_route(scratch_pool, api):
    async with scratch_pool.acquire() as conn:
        a = await w.make(conn)
        b = await w.make(conn)
    as_b = {"x-role": "admin", "x-tenant": str(b["tenant_id"])}
    as_a = {"x-role": "admin", "x-tenant": str(a["tenant_id"])}
    for (method, template), kwargs in _REQUESTS.items():
        foreign = await api.request(method, template.format(campaign_id=a["campaign"]["id"]), headers=as_b, **kwargs)
        unknown = await api.request(method, template.format(campaign_id=uuid.uuid4()), headers=as_b, **kwargs)
        assert (foreign.status_code, foreign.json()) == (unknown.status_code, unknown.json()), (method, template)
        assert foreign.status_code == 404, (method, template)

        own = await api.request(method, template.format(campaign_id=a["campaign"]["id"]), headers=as_a, **kwargs)
        assert own.status_code in (200, 201, 409), (method, template, own.status_code)
