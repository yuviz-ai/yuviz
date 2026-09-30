from __future__ import annotations

import pytest

from services.config import provider_configs
from services.knowledge import agent_kb as agent_kb_service
from services.knowledge import cache, knowledge_bases as kb_service

pytestmark = pytest.mark.integration


async def _make_kb(tenant):
    embedding_cfg = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name="Embed", role="embedding", engine="ollama",
    )
    return await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug="policies", name="Policies", embedding_config_id=embedding_cfg["id"],
    )


async def test_has_enabled_kb_false_when_nothing_assigned(tenant_agent):
    tenant, agent = tenant_agent
    assert await agent_kb_service.has_enabled_kb(tenant["slug"], agent["slug"]) is False


async def test_assign_makes_has_enabled_kb_true_and_writes_redis_flag(tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)

    await agent_kb_service.assign(agent["id"], kb["id"])

    assert await agent_kb_service.has_enabled_kb(tenant["slug"], agent["slug"]) is True
    assert await cache.get_has_enabled_kb(tenant["slug"], agent["slug"]) is True


async def test_disabling_assignment_flips_flag_false(tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    await agent_kb_service.assign(agent["id"], kb["id"])

    await agent_kb_service.set_enabled(agent["id"], kb["id"], enabled=False)

    assert await agent_kb_service.has_enabled_kb(tenant["slug"], agent["slug"]) is False
    assert await cache.get_has_enabled_kb(tenant["slug"], agent["slug"]) is False


async def test_detach_removes_assignment_and_flips_flag_false(tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    await agent_kb_service.assign(agent["id"], kb["id"])

    await agent_kb_service.detach(agent["id"], kb["id"])

    assert await agent_kb_service.list_for_agent(agent["id"]) == []
    assert await cache.get_has_enabled_kb(tenant["slug"], agent["slug"]) is False


async def test_inactive_kb_does_not_count_as_enabled(tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    await agent_kb_service.assign(agent["id"], kb["id"])
    await kb_service.update_knowledge_base(kb["id"], status="inactive")

    assert await agent_kb_service.has_enabled_kb(tenant["slug"], agent["slug"]) is False
