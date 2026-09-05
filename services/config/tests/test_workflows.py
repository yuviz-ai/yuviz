"""Workflow draft/publish/versions against real Postgres."""

from __future__ import annotations

import pytest

from libs.config_sdk.workflow import starter_graph
from services.config import agents, workflows

GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "n1", "type": "start", "position": {"x": 0, "y": 0},
         "data": {"name": "greeting", "prompt": "Ask what they need."}},
        {"id": "n2", "type": "agent", "position": {"x": 0, "y": 190},
         "data": {"name": "booking", "prompt": "Book it."}},
        {"id": "n3", "type": "end", "position": {"x": 0, "y": 380},
         "data": {"name": "goodbye", "prompt": "Close.", "disposition": "qualified"}},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2",
         "data": {"label": "wants to book", "condition": "The caller asked to book."}},
        {"id": "e2", "source": "n2", "target": "n3",
         "data": {"label": "booked", "condition": "The appointment is booked."}},
    ],
}

# n2 has no outbound edge — validation should reject as a dead end.
DEAD_END = {
    "version": 1,
    "nodes": GRAPH["nodes"],
    "edges": [GRAPH["edges"][0]],
}

AGENT_PROMPT = "Be helpful."
CREATED_GRAPH = starter_graph("", AGENT_PROMPT)


async def _agent(test_tenant, slug="wf-agent"):
    return await agents.create_agent(
        tenant_id=test_tenant["id"], slug=slug, name="Workflow Agent",
        system_prompt=AGENT_PROMPT,
    )


async def test_draft_autosave_does_not_bump_config_version(test_tenant, pool):
    agent = await _agent(test_tenant)
    before = await pool.fetchrow("SELECT config_version, updated_at FROM agents WHERE id = $1", agent["id"])

    await workflows.save_draft(agent["id"], tenant_slug=test_tenant["slug"], graph=GRAPH)
    await workflows.save_draft(agent["id"], tenant_slug=test_tenant["slug"], graph=GRAPH)
    await workflows.save_draft(agent["id"], tenant_slug=test_tenant["slug"], graph=DEAD_END)

    after = await pool.fetchrow("SELECT config_version, updated_at FROM agents WHERE id = $1", agent["id"])
    assert after["config_version"] == before["config_version"]
    assert after["updated_at"] == before["updated_at"]

    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    assert state["workflow_draft"] == DEAD_END
    assert state["workflow"] == CREATED_GRAPH and state["published"] is True


async def test_a_normal_agent_edit_still_bumps_config_version(test_tenant, pool):
    agent = await _agent(test_tenant)
    updated = await agents.update_agent(
        agent["id"], tenant_slug=test_tenant["slug"], name="Renamed",
    )
    assert updated["config_version"] == agent["config_version"] + 1


async def test_publish_writes_the_live_graph_bumps_version_and_appends_history(test_tenant):
    agent = await _agent(test_tenant)
    await workflows.save_draft(agent["id"], tenant_slug=test_tenant["slug"], graph=GRAPH)

    result = await workflows.publish(agent["id"], tenant_slug=test_tenant["slug"])

    # create_agent already published starter as version 1
    assert result["version"] == 2
    assert result["config_version"] == agent["config_version"] + 1
    assert result["warnings"] == []

    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    assert state["workflow"] == GRAPH and state["published"] is True

    versions = await workflows.list_versions(agent["id"], test_tenant["slug"])
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["node_count"] == 3 and versions[0]["edge_count"] == 2


async def test_an_invalid_graph_can_never_reach_the_live_column(test_tenant):
    agent = await _agent(test_tenant)
    await workflows.save_draft(agent["id"], tenant_slug=test_tenant["slug"], graph=DEAD_END)

    with pytest.raises(workflows.WorkflowValidationError) as exc:
        await workflows.publish(agent["id"], tenant_slug=test_tenant["slug"])

    assert any(e.id == "n2" and "no way out" in e.message for e in exc.value.errors)
    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    assert state["workflow"] == CREATED_GRAPH
    versions = await workflows.list_versions(agent["id"], test_tenant["slug"])
    assert [v["version"] for v in versions] == [1]


async def test_a_new_agent_is_born_running_a_graph(test_tenant):
    agent = await _agent(test_tenant)

    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    assert state["published"] is True
    assert state["workflow"] == CREATED_GRAPH
    assert state["workflow_draft"] == state["workflow"]

    versions = await workflows.list_versions(agent["id"], test_tenant["slug"])
    assert [v["version"] for v in versions] == [1]
    assert versions[0]["note"] == "created with the agent"


async def test_the_system_prompt_given_at_create_lands_on_the_global_node(test_tenant):
    agent = await _agent(test_tenant, slug="wf-global")
    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    node = next(n for n in state["workflow"]["nodes"] if n["type"] == "global")
    assert node["data"]["prompt"] == AGENT_PROMPT


async def test_the_greeting_given_at_create_lands_on_the_start_node(test_tenant):
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="wf-greet", name="Greeter",
        greeting="Thanks for calling Acme.",
    )
    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    start = next(n for n in state["workflow"]["nodes"] if n["type"] == "start")
    assert start["data"]["greeting"] == "Thanks for calling Acme."


async def test_a_caller_supplied_graph_is_validated_at_create(test_tenant):
    with pytest.raises(workflows.WorkflowValidationError):
        await agents.create_agent(
            tenant_id=test_tenant["id"], slug="wf-bad", name="Broken",
            workflow=DEAD_END,
        )


async def test_warnings_do_not_block_a_publish(test_tenant):
    orphaned = {
        "version": 1,
        "nodes": GRAPH["nodes"] + [
            {"id": "n4", "type": "agent", "position": {"x": 400, "y": 0},
             "data": {"name": "orphan", "prompt": "never reached"}},
        ],
        "edges": GRAPH["edges"] + [
            {"id": "e3", "source": "n4", "target": "n3",
             "data": {"label": "done too", "condition": "Finished."}},
        ],
    }
    agent = await _agent(test_tenant)
    result = await workflows.publish(agent["id"], tenant_slug=test_tenant["slug"], graph=orphaned)
    assert [w["id"] for w in result["warnings"]] == ["n4"]
    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    assert state["published"] is True


async def test_rollback_republishes_as_a_new_version_never_rewriting_history(test_tenant):
    agent = await _agent(test_tenant)
    await workflows.publish(agent["id"], tenant_slug=test_tenant["slug"], graph=GRAPH)
    changed = {**GRAPH, "nodes": [
        {**n, "data": {**n["data"], "prompt": "edited"}} if n["id"] == "n2" else n
        for n in GRAPH["nodes"]
    ]}
    await workflows.publish(agent["id"], tenant_slug=test_tenant["slug"], graph=changed)

    # v1 = starter, GRAPH = v2, changed = v3 → rollback of v2 appends v4
    result = await workflows.rollback(agent["id"], tenant_slug=test_tenant["slug"], version=2)

    assert result["version"] == 4
    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    assert state["workflow"] == GRAPH
    versions = await workflows.list_versions(agent["id"], test_tenant["slug"])
    assert [v["version"] for v in versions] == [4, 3, 2, 1]
    assert versions[0]["note"] == "rollback to version 2"


async def test_another_tenants_agent_id_is_indistinguishable_from_missing(test_tenant):
    agent = await _agent(test_tenant)
    with pytest.raises(LookupError):
        await workflows.save_draft(agent["id"], tenant_slug="not-this-tenant", graph=GRAPH)
    with pytest.raises(LookupError):
        await workflows.publish(agent["id"], tenant_slug="not-this-tenant", graph=GRAPH)


async def test_draft_autosave_rejects_a_soft_deleted_agent(test_tenant):
    agent = await _agent(test_tenant, slug="wf-deleted")
    await agents.soft_delete_agent(agent["id"], tenant_slug=test_tenant["slug"])
    with pytest.raises(LookupError):
        await workflows.save_draft(agent["id"], tenant_slug=test_tenant["slug"], graph=GRAPH)


async def test_republishing_the_same_graph_is_a_noop(test_tenant, pool):
    agent = await _agent(test_tenant, slug="wf-noop")
    first = await workflows.publish(
        agent["id"], tenant_slug=test_tenant["slug"], graph=GRAPH,
    )
    before = await pool.fetchrow(
        "SELECT config_version FROM agents WHERE id = $1", agent["id"],
    )
    versions_before = await workflows.list_versions(agent["id"], test_tenant["slug"])

    again = await workflows.publish(
        agent["id"], tenant_slug=test_tenant["slug"], graph=GRAPH,
    )

    assert again["version"] == first["version"]
    assert again["config_version"] == before["config_version"]
    versions_after = await workflows.list_versions(agent["id"], test_tenant["slug"])
    assert [v["version"] for v in versions_after] == [v["version"] for v in versions_before]


async def test_rollback_to_the_already_live_version_is_a_noop(test_tenant):
    agent = await _agent(test_tenant, slug="wf-rb-noop")
    published = await workflows.publish(
        agent["id"], tenant_slug=test_tenant["slug"], graph=GRAPH,
    )
    result = await workflows.rollback(
        agent["id"], tenant_slug=test_tenant["slug"], version=published["version"],
    )
    assert result["version"] == published["version"]
    assert result["config_version"] == published["config_version"]
    versions = await workflows.list_versions(agent["id"], test_tenant["slug"])
    assert [v["version"] for v in versions] == [published["version"], 1]


async def test_empty_workflow_object_at_create_uses_the_starter_graph(test_tenant):
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="wf-empty", name="Empty",
        system_prompt=AGENT_PROMPT, workflow={},
    )
    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    assert state["workflow"] == CREATED_GRAPH
    versions = await workflows.list_versions(agent["id"], test_tenant["slug"])
    assert [v["version"] for v in versions] == [1]

