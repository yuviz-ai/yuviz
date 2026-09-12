from __future__ import annotations

from services.config import provider_configs
from services.knowledge import agent_kb as agent_kb_service
from services.knowledge import cache, knowledge_bases as kb_service


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


async def test_list_for_kb_returns_empty_when_unattached(tenant_agent):
    tenant, _agent = tenant_agent
    kb = await _make_kb(tenant)

    assert await agent_kb_service.list_for_kb(kb["id"]) == []


async def test_list_for_kb_returns_both_agents_then_only_the_other_after_detach(pool, tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    other_agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sales', 'Sales') RETURNING *", tenant["id"],
    ))

    await agent_kb_service.assign(agent["id"], kb["id"])
    await agent_kb_service.assign(other_agent["id"], kb["id"])

    both = await agent_kb_service.list_for_kb(kb["id"])
    assert {row["agent_id"] for row in both} == {agent["id"], other_agent["id"]}

    await agent_kb_service.detach(agent["id"], kb["id"])

    remaining = await agent_kb_service.list_for_kb(kb["id"])
    assert [row["agent_id"] for row in remaining] == [other_agent["id"]]
    # The KB row and its documents are untouched by a detach.
    assert await kb_service.get_knowledge_base(kb["id"]) is not None

    # tenant_agent's teardown only clears agent_knowledge_bases for its own
    # agent, so the other agent's assignment must be cleared here too.
    await agent_kb_service.detach(other_agent["id"], kb["id"])


async def test_list_for_kb_excludes_soft_deleted_agent(pool, tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    await agent_kb_service.assign(agent["id"], kb["id"])

    await pool.execute("UPDATE agents SET deleted_at = now() WHERE id = $1", agent["id"])

    assert await agent_kb_service.list_for_kb(kb["id"]) == []
