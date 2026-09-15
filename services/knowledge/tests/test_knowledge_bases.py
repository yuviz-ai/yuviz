from __future__ import annotations

import pytest

from libs.tenancy import set_caller_tenant
from services.knowledge import knowledge_bases as kb_service


async def test_create_and_get_round_trips(tenant_agent):
    tenant, _ = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    kb = await kb_service.create_knowledge_base(tenant_id=tenant["id"], slug="policies", name="Policies")

    fetched = await kb_service.get_knowledge_base(kb["id"])
    assert fetched is not None
    assert fetched["slug"] == "policies"
    assert fetched["status"] == "active"
    assert fetched["config_version"] == 1


async def test_get_by_slug(tenant_agent):
    tenant, _ = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    await kb_service.create_knowledge_base(tenant_id=tenant["id"], slug="faq", name="FAQ")

    fetched = await kb_service.get_by_slug(tenant["id"], "faq")
    assert fetched is not None and fetched["name"] == "FAQ"
    assert await kb_service.get_by_slug(tenant["id"], "no-such-slug") is None


async def test_list_excludes_deleted(tenant_agent):
    tenant, _ = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    kb1 = await kb_service.create_knowledge_base(tenant_id=tenant["id"], slug="a", name="A")
    await kb_service.create_knowledge_base(tenant_id=tenant["id"], slug="b", name="B")
    await kb_service.soft_delete_knowledge_base(kb1["id"])

    listed = await kb_service.list_knowledge_bases(tenant["id"])
    assert {kb["slug"] for kb in listed} == {"b"}


async def test_update_bumps_config_version_via_trigger(tenant_agent):
    tenant, _ = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    kb = await kb_service.create_knowledge_base(tenant_id=tenant["id"], slug="c", name="C")

    updated = await kb_service.update_knowledge_base(kb["id"], name="C Renamed")
    assert updated["name"] == "C Renamed"
    assert updated["config_version"] == kb["config_version"] + 1


async def test_update_rejects_unknown_field(tenant_agent):
    tenant, _ = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    kb = await kb_service.create_knowledge_base(tenant_id=tenant["id"], slug="d", name="D")
    with pytest.raises(ValueError):
        await kb_service.update_knowledge_base(kb["id"], slug="not-updatable")


async def test_update_missing_kb_raises_lookup_error(tenant_agent):
    tenant, _ = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    with pytest.raises(LookupError):
        await kb_service.update_knowledge_base("00000000-0000-0000-0000-000000000000", name="x")
