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


async def test_chrome_only_publish_writes_live_positions(test_tenant, pool):
    """Position-only publish must update agents.workflow (editor compares positions)."""
    agent = await _agent(test_tenant, slug="wf-chrome")
    first = await workflows.publish(agent["id"], tenant_slug=test_tenant["slug"], graph=GRAPH)
    moved = {
        **GRAPH,
        "nodes": [
            {**n, "position": {"x": n["position"]["x"] + 40, "y": n["position"]["y"] + 10}}
            for n in GRAPH["nodes"]
        ],
    }
    await workflows.save_draft(agent["id"], tenant_slug=test_tenant["slug"], graph=moved)
    result = await workflows.publish(
        agent["id"], tenant_slug=test_tenant["slug"], graph=moved,
    )
    # No new history row — logic unchanged — but live JSON and config_version move.
    assert result["version"] == first["version"]
    assert result["config_version"] == first["config_version"] + 1
    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    assert state["workflow"]["nodes"] == moved["nodes"]
    assert state["workflow_draft"]["nodes"] == moved["nodes"]


async def test_draft_save_with_stale_config_version_is_rejected(test_tenant):
    agent = await _agent(test_tenant, slug="wf-stale")
    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    base = state["config_version"]
    await workflows.publish(agent["id"], tenant_slug=test_tenant["slug"], graph=GRAPH)
    with pytest.raises(workflows.StaleDraft):
        await workflows.save_draft(
            agent["id"],
            tenant_slug=test_tenant["slug"],
            graph=DEAD_END,
            base_config_version=base,
        )
    after = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    assert after["workflow_draft"] == GRAPH
    assert after["config_version"] == base + 1


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


async def test_republishing_with_only_position_changes_updates_live_chrome(test_tenant, pool):
    agent = await _agent(test_tenant, slug="wf-pos-noop")
    first = await workflows.publish(
        agent["id"], tenant_slug=test_tenant["slug"], graph=GRAPH,
    )
    moved = {
        **GRAPH,
        "nodes": [
            {**n, "position": {"x": n["position"]["x"] + 40, "y": n["position"]["y"] + 10}}
            for n in GRAPH["nodes"]
        ],
    }
    again = await workflows.publish(
        agent["id"], tenant_slug=test_tenant["slug"], graph=moved,
    )
    assert again["version"] == first["version"]
    assert again["config_version"] == first["config_version"] + 1
    versions = await workflows.list_versions(agent["id"], test_tenant["slug"])
    assert [v["version"] for v in versions] == [first["version"], 1]
    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    assert state["workflow"]["nodes"] == moved["nodes"]


async def test_get_agent_does_not_expose_or_cache_the_draft(test_tenant):
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="wf-no-draft", name="No Draft",
        system_prompt=AGENT_PROMPT, tenant_slug=test_tenant["slug"],
    )
    await workflows.save_draft(agent["id"], tenant_slug=test_tenant["slug"], graph=DEAD_END)
    fetched = await agents.get_agent(test_tenant["slug"], "wf-no-draft")
    assert fetched is not None
    # Published graph stays on the agent GET/cache payload for call-setup;
    # draft is editor-only until draft testing.
    assert "workflow" in fetched and "workflow_draft" not in fetched
    assert fetched["workflow"] == CREATED_GRAPH
    listed = await agents.list_agents(test_tenant["id"])
    row = next(a for a in listed if a["id"] == agent["id"])
    assert "workflow" not in row and "workflow_draft" not in row
    assert row["has_workflow"] is True
    assert row["has_workflow_draft"] is True
    assert row["workflow_diverged"] is True
    assert row["workflow_node_count"] == len(DEAD_END["nodes"])
    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    assert state["workflow"] == CREATED_GRAPH
    assert state["workflow_draft"] == DEAD_END


async def test_patching_greeting_mirrors_into_the_published_graph(test_tenant, pool):
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="wf-mirror", name="Mirror",
        greeting="Old hello", system_prompt=AGENT_PROMPT,
    )
    updated = await agents.update_agent(
        agent["id"], tenant_slug=test_tenant["slug"], greeting="New hello",
    )
    assert updated["greeting"] == "New hello"
    state = await workflows.get_workflow(agent["id"], test_tenant["slug"])
    start = next(n for n in state["workflow"]["nodes"] if n["type"] == "start")
    assert start["data"]["greeting"] == "New hello"
    versions = await workflows.list_versions(agent["id"], test_tenant["slug"])
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["note"] == "mirrored greeting/system_prompt"
    # Mirror keeps graphs in the agent audit row (not stripped).
    audit = await pool.fetchrow(
        "SELECT new_value FROM audit_log WHERE entity_id = $1 AND action = 'updated' "
        "ORDER BY changed_at DESC LIMIT 1",
        agent["id"],
    )
    new_value = audit["new_value"]
    if isinstance(new_value, str):
        import json
        new_value = json.loads(new_value)
    assert "workflow" in new_value


async def test_publish_mirrors_greeting_back_into_the_agent_columns(test_tenant):
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="wf-pub-mirror", name="Pub Mirror",
        greeting="Column greeting", system_prompt="Column prompt",
    )
    published = {
        "version": 1,
        "nodes": [
            {"id": "global", "type": "global", "position": {"x": 330, "y": 0},
             "data": {"name": "always applies", "prompt": "Graph prompt"}},
            {"id": "n1", "type": "start", "position": {"x": 0, "y": 0},
             "data": {"name": "greeting", "prompt": "Ask.", "greeting": "Graph greeting"}},
            {"id": "n2", "type": "end", "position": {"x": 0, "y": 190},
             "data": {"name": "goodbye", "prompt": "Close.", "disposition": "completed"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2",
             "data": {"label": "done", "condition": "Finished."}},
        ],
    }
    await workflows.publish(agent["id"], tenant_slug=test_tenant["slug"], graph=published)
    fetched = await agents.get_agent(test_tenant["slug"], "wf-pub-mirror")
    assert fetched["greeting"] == "Graph greeting"
    assert fetched["system_prompt"] == "Graph prompt"


async def test_starter_graph_sql_matches_python_starter_graph(pool):
    """database/schema.sql starter_graph_sql() must equal starter_graph()."""
    import json

    from libs.config_sdk.workflow import graphs_equivalent, starter_graph

    row = await pool.fetchval(
        "SELECT starter_graph_sql($1, $2, $3::jsonb, $4::jsonb)",
        "Thanks for calling.", "Be helpful.",
        '["book_appointment"]', '["kb-1"]',
    )
    sql_graph = json.loads(row) if isinstance(row, str) else dict(row)
    expected = starter_graph(
        "Thanks for calling.", "Be helpful.",
        ["book_appointment"], ["kb-1"],
    )
    assert graphs_equivalent(sql_graph, expected)
    assert sql_graph == expected

    # 3-arg form still works (knowledge defaults to []).
    row3 = await pool.fetchval(
        "SELECT starter_graph_sql($1, $2, $3::jsonb)",
        "Thanks for calling.", "Be helpful.", '["book_appointment"]',
    )
    sql3 = json.loads(row3) if isinstance(row3, str) else dict(row3)
    assert sql3 == starter_graph("Thanks for calling.", "Be helpful.", ["book_appointment"])


async def test_create_with_a_graph_stores_prompts_from_the_graph_not_the_body(test_tenant):
    graph = {
        "version": 1,
        "nodes": [
            {"id": "global", "type": "global", "position": {"x": 330, "y": 0},
             "data": {"name": "always applies", "prompt": "Graph prompt"}},
            {"id": "n1", "type": "start", "position": {"x": 0, "y": 0},
             "data": {"name": "greeting", "prompt": "Ask.", "greeting": "Graph hello"}},
            {"id": "n2", "type": "end", "position": {"x": 0, "y": 190},
             "data": {"name": "goodbye", "prompt": "Close.", "disposition": "completed"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2",
             "data": {"label": "done", "condition": "Finished."}},
        ],
    }
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="wf-create-prompts", name="Prompts",
        greeting="Body hello", system_prompt="Body prompt", workflow=graph,
        tenant_slug=test_tenant["slug"],
    )
    assert agent["greeting"] == "Graph hello"
    assert agent["system_prompt"] == "Graph prompt"


async def test_publish_without_a_global_node_clears_system_prompt(test_tenant):
    agent = await _agent(test_tenant, slug="wf-no-global")
    assert agent["system_prompt"] == AGENT_PROMPT
    await workflows.publish(agent["id"], tenant_slug=test_tenant["slug"], graph=GRAPH)
    fetched = await agents.get_agent(test_tenant["slug"], "wf-no-global")
    assert fetched["system_prompt"] == ""
    start = next(n for n in GRAPH["nodes"] if n["type"] == "start")
    assert fetched["greeting"] == (start["data"].get("greeting") or "")


async def test_patch_system_prompt_rejects_a_graph_with_no_global_node(test_tenant):
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="wf-no-global-patch", name="No Global",
        workflow=GRAPH,
    )
    with pytest.raises(ValueError, match="no always-on"):
        await agents.update_agent(
            agent["id"], tenant_slug=test_tenant["slug"], system_prompt="Nope",
        )

