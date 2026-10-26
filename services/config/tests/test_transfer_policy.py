"""Transfer-destination policy: write-time 4xx and the tenant opt-in (#168)."""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from services.config import agents
from services.config.app import app


def _client(token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.fixture
async def admin_client(test_admin):
    async with _client(test_admin["token"]) as c:
        yield c


@pytest.fixture
async def superadmin_client(test_superadmin):
    async with _client(test_superadmin["token"]) as c:
        yield c


@pytest.fixture
async def viewer_client(test_viewer):
    async with _client(test_viewer["token"]) as c:
        yield c


@pytest.fixture
async def agent(test_tenant, scoped):
    return await agents.create_agent(tenant_id=test_tenant["id"], slug="desk", name="Desk")


async def _set_destination(client, tenant, agent, destination):
    return await client.patch(
        f"/tenants/{tenant['slug']}/agents/{agent['id']}",
        json={"transfer_type": "cold", "transfer_destination": destination},
    )


@pytest.mark.parametrize("destination", [
    "+442071234567", "00442071234567", "sip:agent@attacker.example", "+8821234567890",
])
async def test_agent_update_refuses_destination_outside_default_policy(
    admin_client, test_tenant, agent, destination,
):
    resp = await _set_destination(admin_client, test_tenant, agent, destination)
    assert resp.status_code == 422, resp.text
    assert "transfer_destination" in resp.json()["detail"]


@pytest.mark.parametrize("destination", ["1001", "+18005550100", "+12125550100"])
async def test_agent_update_keeps_domestic_destinations_working(admin_client, test_tenant, agent, destination):
    resp = await _set_destination(admin_client, test_tenant, agent, destination)
    assert resp.status_code == 200, resp.text
    assert resp.json()["transfer_destination"] == destination


@pytest.mark.parametrize("destination", [
    "+19005550100", "+19765550100", "+8821234567890", "+8835551234", "+870123456789",
    "+9791234567", "+449091234567", "00882123456789", "sip:+8821234567890@pbx.example.com",
])
async def test_premium_rate_is_refused_even_after_opt_in(
    admin_client, superadmin_client, test_tenant, agent, destination,
):
    opt_in = await superadmin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"allow_international_transfers": True},
    )
    assert opt_in.status_code == 200, opt_in.text
    resp = await _set_destination(admin_client, test_tenant, agent, destination)
    assert resp.status_code == 422, resp.text


async def test_opt_in_allows_international_and_external_sip(admin_client, superadmin_client, test_tenant, agent):
    await superadmin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"allow_international_transfers": True},
    )
    for destination in ("+442071234567", "sip:agent@pbx.example.com"):
        resp = await _set_destination(admin_client, test_tenant, agent, destination)
        assert resp.status_code == 200, resp.text


async def test_home_calling_code_decides_what_is_domestic(admin_client, superadmin_client, test_tenant, agent):
    resp = await superadmin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"home_calling_code": "44"},
    )
    assert resp.status_code == 200, resp.text
    assert (await _set_destination(admin_client, test_tenant, agent, "+442071234567")).status_code == 200
    assert (await _set_destination(admin_client, test_tenant, agent, "+12125550100")).status_code == 422


@pytest.mark.parametrize("body", [
    {"allow_international_transfers": True},
    {"home_calling_code": "3"},
    {"home_calling_code": "44", "allow_international_transfers": True},
])
async def test_tenant_admin_cannot_change_the_policy_of_any_tenant(admin_client, test_tenant, pool, body):
    other = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ('Foreign', $1) RETURNING id",
        f"test-foreign-{uuid.uuid4().hex[:8]}",
    )
    try:
        for tenant_id in (test_tenant["id"], other["id"]):
            resp = await admin_client.patch(f"/tenants/{tenant_id}/transfer-policy", json=body)
            assert resp.status_code == 403, resp.text
            row = await pool.fetchrow(
                "SELECT home_calling_code, allow_international_transfers FROM tenants WHERE id = $1", tenant_id,
            )
            assert (row["home_calling_code"], row["allow_international_transfers"]) == ("1", False)
    finally:
        await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


async def test_superadmin_sets_the_policy_and_it_is_audited(superadmin_client, test_superadmin, test_tenant, pool):
    resp = await superadmin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy",
        json={"home_calling_code": "91", "allow_international_transfers": True},
    )
    assert resp.status_code == 200, resp.text
    row = await pool.fetchrow(
        "SELECT home_calling_code, allow_international_transfers FROM tenants WHERE id = $1", test_tenant["id"],
    )
    assert (row["home_calling_code"], row["allow_international_transfers"]) == ("91", True)
    audit = await pool.fetchrow(
        "SELECT user_id, old_value, new_value FROM audit_log WHERE entity_type = 'tenant' AND entity_id = $1 "
        "ORDER BY id DESC LIMIT 1", test_tenant["id"],
    )
    assert audit["user_id"] == test_superadmin["user"]["id"]
    assert json.loads(audit["old_value"])["home_calling_code"] == "1"
    assert json.loads(audit["new_value"])["home_calling_code"] == "91"


@pytest.mark.parametrize("code", ["3", "4", "5", "0", "999", "882", "800", "1234", "+91", "abc", ""])
async def test_home_calling_code_must_be_a_real_country_code(superadmin_client, test_tenant, pool, code):
    resp = await superadmin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"home_calling_code": code},
    )
    assert resp.status_code == 422, resp.text
    assert await pool.fetchval("SELECT home_calling_code FROM tenants WHERE id = $1", test_tenant["id"]) == "1"


async def test_viewer_cannot_opt_in(viewer_client, test_tenant, pool):
    resp = await viewer_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"allow_international_transfers": True},
    )
    assert resp.status_code == 403
    assert not await pool.fetchval(
        "SELECT allow_international_transfers FROM tenants WHERE id = $1", test_tenant["id"],
    )


async def test_tenant_admin_cannot_use_the_superadmin_patch_to_opt_in(admin_client, test_tenant):
    resp = await admin_client.patch(
        f"/tenants/{test_tenant['id']}", json={"allow_international_transfers": True},
    )
    assert resp.status_code == 403


def _transfer_workflow(destination):
    def node(id_, type_, name, **data):
        return {"id": id_, "type": type_, "position": {"x": 0, "y": 0}, "data": {"name": name, **data}}

    def edge(id_, source, target, label):
        return {"id": id_, "source": source, "target": target,
                "data": {"label": label, "condition": "something happened"}}

    return {
        "version": 1,
        "nodes": [
            node("n1", "start", "greeting"),
            node("n2", "transfer", "to_human", prompt="Connecting you now.", transfer_destination=destination),
            node("n3", "end", "goodbye"),
        ],
        "edges": [edge("e1", "n1", "n2", "needs a person"), edge("e2", "n1", "n3", "done")],
    }


async def _publish_workflow(client, tenant, agent, destination):
    return await client.post(
        f"/tenants/{tenant['slug']}/agents/{agent['id']}/workflow/publish",
        json={"graph": _transfer_workflow(destination)},
    )


async def test_workflow_publish_refuses_a_literal_transfer_destination_outside_the_policy(
    admin_client, test_tenant, agent,
):
    resp = await _publish_workflow(admin_client, test_tenant, agent, "+447911123456")
    assert resp.status_code == 400, resp.text
    errors = resp.json()["errors"]
    assert [(e["id"], e["field"]) for e in errors] == [("n2", "transfer_destination")]


async def test_workflow_publish_allows_the_same_destination_after_opt_in(
    admin_client, superadmin_client, test_tenant, agent,
):
    opt_in = await superadmin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"allow_international_transfers": True},
    )
    assert opt_in.status_code == 200, opt_in.text
    resp = await _publish_workflow(admin_client, test_tenant, agent, "+447911123456")
    assert resp.status_code == 200, resp.text


async def test_workflow_publish_leaves_a_templated_transfer_destination_for_runtime(
    admin_client, test_tenant, agent,
):
    resp = await _publish_workflow(admin_client, test_tenant, agent, "{{ branch_number }}")
    assert resp.status_code == 200, resp.text


async def _set_policy(tenant, **fields):
    from services.config import tenants
    await tenants.update_tenant(tenant["id"], **fields)


async def _create_agent_with_workflow(client, tenant, destination, slug="wfdesk"):
    return await client.post(
        f"/tenants/{tenant['slug']}/agents",
        json={"slug": slug, "name": "Wf Desk", "workflow": _transfer_workflow(destination)},
    )


async def _validate_workflow_route(client, tenant, agent, destination):
    return await client.post(
        f"/tenants/{tenant['slug']}/agents/{agent['id']}/workflow/validate",
        json={"graph": _transfer_workflow(destination)},
    )


async def test_agent_create_with_workflow_refuses_a_destination_outside_the_policy(
    admin_client, test_tenant, pool,
):
    resp = await _create_agent_with_workflow(admin_client, test_tenant, "+447911123456")
    assert resp.status_code == 400, resp.text
    assert [(e["id"], e["field"]) for e in resp.json()["errors"]] == [("n2", "transfer_destination")]
    assert await pool.fetchval("SELECT count(*) FROM agents WHERE tenant_id = $1", test_tenant["id"]) == 0


async def test_agent_create_with_workflow_allows_the_destination_after_opt_in(admin_client, test_tenant):
    await _set_policy(test_tenant, allow_international_transfers=True)
    resp = await _create_agent_with_workflow(admin_client, test_tenant, "+447911123456")
    assert resp.status_code == 201, resp.text


async def test_agent_create_with_workflow_leaves_a_templated_destination_for_runtime(admin_client, test_tenant):
    resp = await _create_agent_with_workflow(admin_client, test_tenant, "{{ branch_number }}")
    assert resp.status_code == 201, resp.text


async def test_agent_create_with_workflow_for_a_home_91_tenant_allows_91_and_refuses_44(
    admin_client, test_tenant,
):
    await _set_policy(test_tenant, home_calling_code="91")
    assert (await _create_agent_with_workflow(admin_client, test_tenant, "+919876543210")).status_code == 201
    refused = await _create_agent_with_workflow(admin_client, test_tenant, "+447911123456", slug="wfdesk2")
    assert refused.status_code == 400, refused.text


@pytest.mark.parametrize("destination", ["+8821234567890", "+449091234567"])
async def test_agent_create_with_workflow_refuses_premium_even_after_opt_in(admin_client, test_tenant, destination):
    await _set_policy(test_tenant, allow_international_transfers=True)
    resp = await _create_agent_with_workflow(admin_client, test_tenant, destination)
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("destination, home, refused", [
    ("+44{{ x }}", "1", True),
    ("+44791112{{ x }}", "1", True),
    ("0044{{ x }}", "1", True),
    ("+882{{ x }}", "1", True),
    ("+1900{{ x }}", "1", True),
    ("+1345{{ x }}", "1", True),
    ("+1212{{ x }}", "1", False),
    ("+1{{ x }}", "1", False),
    ("+{{ x }}", "1", False),
    ("1001{{ x }}", "1", False),
    ("+91{{ x }}", "91", False),
    ("+44{{ x }}", "91", True),
    ("+44 20 {{ x }}", "1", True),
    ("+44-20{{ x }}", "1", True),
    ("+91-98{{ x }}", "91", False),
    ("sip:{{ x }}@external.host", "1", True),
    ("sip:+4420{{ x }}@external.host", "1", True),
    ("sip:agent@{{ x }}", "1", True),
    ("sip:{{ x }}@external.host", "91", True),
])
async def test_literal_prefix_of_a_templated_destination_is_checked(
    admin_client, test_tenant, destination, home, refused,
):
    await _set_policy(test_tenant, home_calling_code=home)
    resp = await _create_agent_with_workflow(admin_client, test_tenant, destination)
    assert resp.status_code == (400 if refused else 201), resp.text


async def test_literal_prefix_of_a_templated_destination_is_allowed_after_opt_in(admin_client, test_tenant):
    await _set_policy(test_tenant, allow_international_transfers=True)
    assert (await _create_agent_with_workflow(admin_client, test_tenant, "+44{{ x }}")).status_code == 201


async def test_workflow_validate_reports_what_publish_refuses(admin_client, test_tenant, agent):
    resp = await _validate_workflow_route(admin_client, test_tenant, agent, "+447911123456")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert [(e["id"], e["field"]) for e in body["errors"]] == [("n2", "transfer_destination")]
    published = await _publish_workflow(admin_client, test_tenant, agent, "+447911123456")
    assert [(e["id"], e["field"]) for e in published.json()["errors"]] == [("n2", "transfer_destination")]


async def test_workflow_validate_accepts_what_publish_accepts(admin_client, test_tenant, agent):
    for destination in ("{{ branch_number }}", "+12125550100"):
        resp = await _validate_workflow_route(admin_client, test_tenant, agent, destination)
        assert resp.json()["valid"] is True, resp.text


async def test_tenant_patch_sets_number_purchase_but_ignores_transfer_policy(
    superadmin_client, test_tenant, pool,
):
    resp = await superadmin_client.patch(
        f"/tenants/{test_tenant['id']}",
        json={"number_purchase_enabled": True, "home_calling_code": "44", "allow_international_transfers": True},
    )
    assert resp.status_code == 200, resp.text
    row = await pool.fetchrow(
        "SELECT number_purchase_enabled, home_calling_code, allow_international_transfers "
        "FROM tenants WHERE id = $1", test_tenant["id"],
    )
    assert (row["number_purchase_enabled"], row["home_calling_code"], row["allow_international_transfers"]) == (
        True, "1", False,
    )
