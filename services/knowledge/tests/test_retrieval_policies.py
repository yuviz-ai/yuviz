from __future__ import annotations

import pytest

from services.knowledge import retrieval_policies as policy_service

pytestmark = pytest.mark.integration


async def test_get_policy_returns_none_when_unset(tenant_agent):
    _, agent = tenant_agent
    assert await policy_service.get_policy(agent["id"]) is None


async def test_upsert_creates_then_updates(tenant_agent):
    _, agent = tenant_agent

    created = await policy_service.upsert_policy(agent["id"], top_k=8)
    assert created["top_k"] == 8
    assert created["max_tokens"] is None  # not set — three-tier resolution falls to system default

    updated = await policy_service.upsert_policy(agent["id"], top_k=15, minimum_score=0.6)
    assert updated["top_k"] == 15
    assert updated["minimum_score"] == 0.6

    fetched = await policy_service.get_policy(agent["id"])
    assert fetched["top_k"] == 15


async def test_upsert_rejects_unknown_field(tenant_agent):
    _, agent = tenant_agent
    with pytest.raises(ValueError):
        await policy_service.upsert_policy(agent["id"], not_a_real_field=1)


async def test_upsert_with_no_fields_raises(tenant_agent):
    _, agent = tenant_agent
    with pytest.raises(ValueError):
        await policy_service.upsert_policy(agent["id"])
