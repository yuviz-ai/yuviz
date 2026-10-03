"""Accept/Undo of a prompt revision, the undo slot's isolation from every
audit/API sink, and the single "not found" provider-id message. Every
assertion about stored state reads the row back from Postgres."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import pytest
import pytest_asyncio

from services.config import agents
from services.config.system_prompt import (
    CustomerDataError,
    PromptStructureError,
    adds_template_braces,
)

SLOT_KEYS = ("prompt_undo_previous", "prompt_undo_accepted_sha256")


def _prompt(tag: str) -> str:
    return (
        "How you speak\nBe brief.\nGuardrails\nNever guess.\n"
        f"Doing your job well\nLine one {tag}.\nLine two {tag}.\nLine three {tag}."
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest_asyncio.fixture(loop_scope="session")
async def foreign_llm_id(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    tenant_id = await pool.fetchval(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING id", f"Foreign {slug}", slug,
    )
    config_id = await pool.fetchval(
        "INSERT INTO provider_configs (tenant_id, name, role, engine) "
        "VALUES ($1, 'Theirs', 'llm', 'anthropic') RETURNING id",
        tenant_id,
    )
    yield str(config_id)
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant_id)
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant_id)


async def _make_agent(test_tenant, base: str) -> dict:
    return await agents.create_agent(
        tenant_id=test_tenant["id"], slug="revise-me", name="Revise me",
        greeting="Hello", system_prompt=base, tenant_slug=test_tenant["slug"],
    )


async def _accept(test_tenant, agent_id, base: str, proposed: str):
    return await agents.accept_prompt_revision(
        agent_id, tenant_id=test_tenant["id"], tenant_slug=test_tenant["slug"],
        proposed_prompt=proposed, base_prompt_sha256=_sha(base),
    )


async def _undo(test_tenant, agent_id):
    return await agents.undo_prompt_revision(
        agent_id, tenant_id=test_tenant["id"], tenant_slug=test_tenant["slug"],
    )


async def _stored(pool, agent_id) -> dict:
    return dict(await pool.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id))


async def test_two_concurrent_accepts_on_one_base_give_one_winner(pool, test_tenant, scoped):
    base = _prompt("base")
    agent = await _make_agent(test_tenant, base)
    p1, p2 = _prompt("one"), _prompt("two")

    results = await asyncio.gather(
        _accept(test_tenant, agent["id"], base, p1),
        _accept(test_tenant, agent["id"], base, p2),
    )

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    row = await _stored(pool, agent["id"])
    assert row["system_prompt"] == winners[0]["system_prompt"]
    assert row["system_prompt"] in (p1, p2)
    assert row["prompt_undo_previous"] == base


async def test_accept_accept_undo_restores_prior_prompt_and_second_undo_is_none(pool, test_tenant, scoped):
    p0, p1, p2 = _prompt("zero"), _prompt("one"), _prompt("two")
    agent = await _make_agent(test_tenant, p0)

    assert await _accept(test_tenant, agent["id"], p0, p1) is not None
    assert await _accept(test_tenant, agent["id"], p1, p2) is not None
    undone = await _undo(test_tenant, agent["id"])

    row = await _stored(pool, agent["id"])
    assert undone is not None
    assert row["system_prompt"] == p1
    assert row["prompt_undo_previous"] is None
    assert row["prompt_undo_accepted_sha256"] is None
    assert await _undo(test_tenant, agent["id"]) is None
    assert (await _stored(pool, agent["id"]))["system_prompt"] == p1


async def test_accept_on_stale_base_is_none_and_changes_nothing(pool, test_tenant, scoped):
    p0 = _prompt("zero")
    agent = await _make_agent(test_tenant, p0)

    assert await _accept(test_tenant, agent["id"], _prompt("other"), _prompt("one")) is None

    row = await _stored(pool, agent["id"])
    assert row["system_prompt"] == p0
    assert row["prompt_undo_previous"] is None


async def test_accept_then_hand_patch_then_undo_is_none_and_can_undo_false(pool, test_tenant, scoped):
    p0, p1, hand = _prompt("zero"), _prompt("one"), _prompt("hand")
    agent = await _make_agent(test_tenant, p0)
    accepted = await _accept(test_tenant, agent["id"], p0, p1)
    assert accepted["can_undo"] is True

    await agents.update_agent(agent["id"], tenant_slug=test_tenant["slug"], system_prompt=hand)

    assert await _undo(test_tenant, agent["id"]) is None
    row = await _stored(pool, agent["id"])
    assert row["system_prompt"] == hand
    assert row["prompt_undo_previous"] == p0
    fetched = await agents.get_agent(test_tenant["slug"], "revise-me")
    assert fetched["can_undo"] is False


async def test_can_undo_and_prompt_fixable_follow_the_stored_row(test_tenant, scoped):
    p0, p1 = _prompt("zero"), _prompt("one")
    agent = await _make_agent(test_tenant, p0)
    assert agent["can_undo"] is False
    assert agent["prompt_fixable"] is True

    await _accept(test_tenant, agent["id"], p0, p1)
    assert (await agents.get_agent(test_tenant["slug"], "revise-me"))["can_undo"] is True

    await agents.update_agent(agent["id"], tenant_slug=test_tenant["slug"], system_prompt="Be helpful.")
    fetched = await agents.get_agent_by_id(agent["id"])
    assert fetched["prompt_fixable"] is False
    assert not set(SLOT_KEYS) & set(fetched)


async def test_accept_and_undo_mirror_the_prompt_into_the_graphs(pool, test_tenant, scoped):
    p0, p1 = _prompt("zero"), _prompt("one")
    agent = await _make_agent(test_tenant, p0)

    await _accept(test_tenant, agent["id"], p0, p1)
    row = await _stored(pool, agent["id"])
    for column in ("workflow", "workflow_draft"):
        nodes = json.loads(row[column])["nodes"]
        assert [n["data"]["prompt"] for n in nodes if n["type"] == "global"] == [p1]

    await _undo(test_tenant, agent["id"])
    row = await _stored(pool, agent["id"])
    assert [n["data"]["prompt"] for n in json.loads(row["workflow"])["nodes"] if n["type"] == "global"] == [p0]


async def test_foreign_and_random_provider_ids_give_the_same_error(test_tenant, scoped, foreign_llm_id):
    agent = await _make_agent(test_tenant, _prompt("zero"))
    messages = []
    for config_id in (foreign_llm_id, str(uuid.uuid4())):
        with pytest.raises(ValueError) as created:
            await agents.create_agent(
                tenant_id=test_tenant["id"], slug=f"x-{uuid.uuid4().hex[:6]}", name="X",
                llm_config_id=config_id,
            )
        with pytest.raises(ValueError) as updated:
            await agents.update_agent(
                agent["id"], tenant_slug=test_tenant["slug"], llm_config_id=config_id,
            )
        messages += [str(created.value), str(updated.value)]
    assert len(set(messages)) == 1
    assert foreign_llm_id not in messages[0]


async def test_slot_never_reaches_audit_log_or_workflow_versions(pool, test_tenant, scoped):
    p0, p1, p2, hand = _prompt("zero"), _prompt("one"), _prompt("two"), _prompt("hand")
    agent = await _make_agent(test_tenant, p0)
    assert any(n["type"] == "global" for n in agent["workflow"]["nodes"])

    await agents.update_agent(agent["id"], tenant_slug=test_tenant["slug"], system_prompt=hand)
    await _accept(test_tenant, agent["id"], hand, p1)
    await _accept(test_tenant, agent["id"], p1, p2)
    await _undo(test_tenant, agent["id"])

    audits = await pool.fetch(
        "SELECT old_value::text AS o, new_value::text AS n FROM audit_log WHERE entity_id = $1",
        agent["id"],
    )
    versions = await pool.fetch(
        "SELECT graph::text AS g FROM agent_workflow_versions WHERE agent_id = $1", agent["id"],
    )
    blobs = [v for r in audits for v in (r["o"], r["n"]) if v] + [r["g"] for r in versions]
    assert len(audits) == 5 and len(versions) >= 4
    # The audit rows exist for the mirrored branch, so the slot check is not vacuous.
    assert any('"workflow"' in b for b in blobs)
    for blob in blobs:
        for key in SLOT_KEYS:
            assert key not in blob
        assert _sha(p1) not in blob


async def test_accept_brace_rule_refuses_only_added_template_braces():
    base = _prompt("zero") + "\nKeep {{name}} as is."
    assert adds_template_braces(base + "\nAlso {{date}}.", base)
    assert adds_template_braces(base + "\nstray }}", base)
    assert not adds_template_braces(base.replace("Line one", "Line uno"), base)


def test_customer_data_error_is_a_prompt_structure_error():
    # Router handlers must list CustomerDataError first; this pins why.
    assert issubclass(CustomerDataError, PromptStructureError)
