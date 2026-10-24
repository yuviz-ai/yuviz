"""The Campaigns app opens and closes its dial client, Redis client and billing seam, and has no ESL-era route."""

from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi.testclient import TestClient

from services.campaigns import app as app_module
from services.campaigns.billing import NullBillingAdmission
from services.campaigns.dial_request import DialRequestClient

CAMPAIGNS = pathlib.Path(app_module.__file__).parent


async def test_lifespan_starts_the_worker_and_closes_all_three_resources(scratch_pool, monkeypatch):
    closed = []

    class SpyDial(DialRequestClient):
        async def close(self):
            closed.append("dial")
            await super().close()

    class SpyBilling(NullBillingAdmission):
        async def aclose(self):
            closed.append("billing")

    real_from_url = app_module.redis.from_url

    def spy_from_url(*args, **kwargs):
        client = real_from_url(*args, **kwargs)
        real_aclose = client.aclose

        async def aclose():
            closed.append("redis")
            await real_aclose()

        client.aclose = aclose
        return client

    started = []
    real_start = app_module.CampaignWorker.start
    monkeypatch.setattr(app_module, "DialRequestClient", SpyDial)
    monkeypatch.setattr(app_module, "NullBillingAdmission", SpyBilling)
    monkeypatch.setattr(app_module.redis, "from_url", spy_from_url)
    monkeypatch.setattr(app_module.CampaignWorker, "start", lambda self: (started.append(self), real_start(self)))

    async with app_module.lifespan(app_module.app):
        assert len(started) == 1 and closed == []
    assert sorted(closed) == ["billing", "dial", "redis"]


def test_the_vobiz_resolved_route_is_gone():
    paths = {getattr(r, "path", "") for r in app_module.app.routes}
    assert not [p for p in paths if "vobiz" in p]
    with TestClient(app_module.app) as client:
        assert client.post("/internal/vobiz-call-resolved", json={}).status_code == 404


def test_null_billing_is_the_only_billing_constructor():
    constructed = []
    sources = [p for p in CAMPAIGNS.rglob("*.py") if "tests" not in p.parts]
    assert len(sources) > 10
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                if name.endswith("BillingAdmission"):
                    constructed.append((path.name, name))
    assert constructed == [("app.py", "NullBillingAdmission")]
