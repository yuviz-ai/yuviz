"""
DB-backed tests for services/toolexec/custom_apis.py create/update/
soft-delete (T7).
"""

from __future__ import annotations

import uuid

import pytest

from services.toolexec import custom_apis


def _api_kwargs(tenant_id, name: str, **overrides) -> dict:
    kwargs = dict(
        tenant_id=tenant_id,
        name=name,
        description="d",
        endpoint_url="https://example.com/api",
        method="GET",
    )
    kwargs.update(overrides)
    return kwargs


async def _cleanup_tenant_apis(pool, tenant_id) -> None:
    await pool.execute(
        "DELETE FROM custom_api_params WHERE custom_api_id IN "
        "(SELECT id FROM custom_apis WHERE tenant_id = $1)", tenant_id,
    )
    await pool.execute("DELETE FROM custom_apis WHERE tenant_id = $1", tenant_id)


@pytest.mark.asyncio
async def test_cross_tenant_and_nonexistent_upstream_rejected(pool, tenant_agent):
    tenant, _agent = tenant_agent
    other_tenant = dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
        "Other", f"other-{uuid.uuid4().hex[:8]}",
    ))
    try:
        other_api = await custom_apis.create_custom_api(**_api_kwargs(other_tenant["id"], "other_api"))

        upstream_param = lambda upstream_id: [{  # noqa: E731
            "name": "x", "location": "query", "json_type": "string", "required": True,
            "source": "upstream", "upstream_api_id": upstream_id, "upstream_json_path": "$.id",
        }]

        with pytest.raises(ValueError, match="unknown_upstream_api"):
            await custom_apis.create_custom_api(
                **_api_kwargs(tenant["id"], "cross_tenant_chain", params=upstream_param(other_api["id"])),
            )
        with pytest.raises(ValueError, match="unknown_upstream_api"):
            await custom_apis.create_custom_api(
                **_api_kwargs(tenant["id"], "nonexistent_upstream_chain", params=upstream_param(str(uuid.uuid4()))),
            )
    finally:
        await _cleanup_tenant_apis(pool, tenant["id"])
        await _cleanup_tenant_apis(pool, other_tenant["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


@pytest.mark.asyncio
async def test_same_name_two_tenants_coexist(pool, tenant_agent):
    tenant, _agent = tenant_agent
    other_tenant = dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
        "Other", f"other-{uuid.uuid4().hex[:8]}",
    ))
    try:
        a = await custom_apis.create_custom_api(**_api_kwargs(tenant["id"], "lookup_account"))
        b = await custom_apis.create_custom_api(**_api_kwargs(other_tenant["id"], "lookup_account"))
        assert a["name"] == b["name"] == "lookup_account"
        assert a["tenant_id"] != b["tenant_id"]
    finally:
        await _cleanup_tenant_apis(pool, tenant["id"])
        await _cleanup_tenant_apis(pool, other_tenant["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


def _upstream_param(name: str, upstream_id) -> dict:
    return {
        "name": name, "location": "query", "json_type": "string", "required": True,
        "source": "upstream", "upstream_api_id": upstream_id, "upstream_json_path": "$.id",
    }


@pytest.mark.asyncio
async def test_five_level_chain_and_mid_graph_edit_rejected(pool, tenant_agent):
    tenant, _agent = tenant_agent
    try:
        api1 = await custom_apis.create_custom_api(**_api_kwargs(tenant["id"], "level1"))
        api2 = await custom_apis.create_custom_api(
            **_api_kwargs(tenant["id"], "level2", params=[_upstream_param("a", api1["id"])]),
        )
        api3 = await custom_apis.create_custom_api(
            **_api_kwargs(tenant["id"], "level3", params=[_upstream_param("a", api2["id"])]),
        )
        api4 = await custom_apis.create_custom_api(
            **_api_kwargs(tenant["id"], "level4", params=[_upstream_param("a", api3["id"])]),
        )
        assert api4["chain_levels"] == 4

        with pytest.raises(ValueError, match="chain_depth_exceeded"):
            await custom_apis.create_custom_api(
                **_api_kwargs(tenant["id"], "level5", params=[_upstream_param("a", api4["id"])]),
            )
        # Rolled back: no half-created level5 row exists.
        assert await pool.fetchrow(
            "SELECT id FROM custom_apis WHERE tenant_id = $1 AND name = 'level5'", tenant["id"],
        ) is None

        # Mid-graph edit: pushing api1's OWN chain from a leaf (height 1) to
        # height 3 must be rejected because it pushes api4 (a transitive
        # dependent, never itself directly edited) from height 4 to height 6.
        new_leaf_a = await custom_apis.create_custom_api(**_api_kwargs(tenant["id"], "new_leaf_a"))
        new_leaf_b = await custom_apis.create_custom_api(
            **_api_kwargs(tenant["id"], "new_leaf_b", params=[_upstream_param("a", new_leaf_a["id"])]),
        )
        with pytest.raises(ValueError, match="chain_depth_exceeded"):
            await custom_apis.update_custom_api(
                api1["id"], params=[_upstream_param("a", new_leaf_b["id"])],
            )
        # Rolled back: api1 still has no params (still a leaf).
        unchanged = await custom_apis.get_custom_api(api1["id"])
        assert unchanged["params"] == []
        assert unchanged["chain_levels"] == 1
    finally:
        await _cleanup_tenant_apis(pool, tenant["id"])


@pytest.mark.asyncio
async def test_cycle_rejected(pool, tenant_agent):
    tenant, _agent = tenant_agent
    try:
        api_a = await custom_apis.create_custom_api(**_api_kwargs(tenant["id"], "cycle_a"))
        api_b = await custom_apis.create_custom_api(
            **_api_kwargs(tenant["id"], "cycle_b", params=[_upstream_param("a", api_a["id"])]),
        )
        with pytest.raises(ValueError, match="dependency_cycle"):
            await custom_apis.update_custom_api(
                api_a["id"], params=[_upstream_param("b", api_b["id"])],
            )
        # Rolled back: api_a still has no params.
        unchanged = await custom_apis.get_custom_api(api_a["id"])
        assert unchanged["params"] == []
    finally:
        await _cleanup_tenant_apis(pool, tenant["id"])


@pytest.mark.asyncio
async def test_soft_delete_refused_while_dependent_live_then_succeeds(pool, tenant_agent):
    tenant, _agent = tenant_agent
    try:
        api1 = await custom_apis.create_custom_api(**_api_kwargs(tenant["id"], "dep_leaf"))
        api2 = await custom_apis.create_custom_api(
            **_api_kwargs(tenant["id"], "dep_root", params=[_upstream_param("a", api1["id"])]),
        )
        with pytest.raises(custom_apis.DependentApiExists):
            await custom_apis.soft_delete_custom_api(api1["id"])

        # api2 (the dependent) has no dependents of its own — its delete succeeds.
        await custom_apis.soft_delete_custom_api(api2["id"])
        # Now api1 has no live dependent — its delete succeeds too.
        await custom_apis.soft_delete_custom_api(api1["id"])

        assert await custom_apis.get_custom_api(api1["id"]) is None
        assert await custom_apis.get_custom_api(api2["id"]) is None
    finally:
        await _cleanup_tenant_apis(pool, tenant["id"])


@pytest.mark.asyncio
async def test_literal_secret_rejected(pool, tenant_agent):
    tenant, _agent = tenant_agent
    try:
        with pytest.raises(ValueError, match="credential_ref_not_a_reference"):
            await custom_apis.create_custom_api(**_api_kwargs(
                tenant["id"], "literal_secret_api",
                auth_scheme="bearer", auth_config={"token_ref": "sk-literal-raw-secret-value"},
            ))
        assert await pool.fetchrow(
            "SELECT id FROM custom_apis WHERE tenant_id = $1 AND name = 'literal_secret_api'", tenant["id"],
        ) is None
    finally:
        await _cleanup_tenant_apis(pool, tenant["id"])


@pytest.mark.asyncio
async def test_success_template_sensitive_equal_rejected(pool, tenant_agent):
    tenant, _agent = tenant_agent
    try:
        with pytest.raises(ValueError, match="invalid_success_template"):
            await custom_apis.create_custom_api(**_api_kwargs(
                tenant["id"], "tmpl_equal",
                sensitive_response_paths=["$.customer.ssn"],
                success_template="Your SSN is {{$.customer.ssn}}",
            ))
    finally:
        await _cleanup_tenant_apis(pool, tenant["id"])


@pytest.mark.asyncio
async def test_success_template_descendant_of_sensitive_rejected(pool, tenant_agent):
    tenant, _agent = tenant_agent
    try:
        with pytest.raises(ValueError, match="invalid_success_template"):
            await custom_apis.create_custom_api(**_api_kwargs(
                tenant["id"], "tmpl_descendant",
                sensitive_response_paths=["$.customer"],
                success_template="Your SSN is {{$.customer.ssn}}",
            ))
    finally:
        await _cleanup_tenant_apis(pool, tenant["id"])


@pytest.mark.asyncio
async def test_success_template_ancestor_of_sensitive_rejected(pool, tenant_agent):
    tenant, _agent = tenant_agent
    try:
        with pytest.raises(ValueError, match="invalid_success_template"):
            await custom_apis.create_custom_api(**_api_kwargs(
                tenant["id"], "tmpl_ancestor",
                sensitive_response_paths=["$.customer.ssn"],
                success_template="Your info: {{$.customer}}",
            ))
    finally:
        await _cleanup_tenant_apis(pool, tenant["id"])


@pytest.mark.asyncio
async def test_success_template_naming_sensitive_param_rejected(pool, tenant_agent):
    tenant, _agent = tenant_agent
    try:
        with pytest.raises(ValueError, match="invalid_success_template"):
            await custom_apis.create_custom_api(**_api_kwargs(
                tenant["id"], "tmpl_param",
                params=[{
                    "name": "ssn", "location": "query", "json_type": "string",
                    "source": "caller", "sensitive": True,
                }],
                success_template="Your ssn is {{$.data.ssn}}",
            ))
    finally:
        await _cleanup_tenant_apis(pool, tenant["id"])


@pytest.mark.asyncio
async def test_success_template_patch_after_the_fact_revalidation(pool, tenant_agent):
    tenant, _agent = tenant_agent
    try:
        api = await custom_apis.create_custom_api(**_api_kwargs(
            tenant["id"], "tmpl_patch",
            success_template="Email sent to {{$.customer.email}}",
        ))
        assert api["sensitive_response_paths"] == []

        with pytest.raises(ValueError, match="invalid_success_template"):
            await custom_apis.update_custom_api(
                api["id"], sensitive_response_paths=["$.customer.email"],
            )

        # Rolled back: sensitive_response_paths is still empty — the PATCH
        # that would have made the ALREADY-STORED template reference a
        # now-sensitive path never committed.
        unchanged = await custom_apis.get_custom_api(api["id"])
        assert unchanged["sensitive_response_paths"] == []
    finally:
        await _cleanup_tenant_apis(pool, tenant["id"])


@pytest.mark.asyncio
async def test_write_audit_redacts_auth_config(pool, tenant_agent):
    """FIX 4d (security finding 8): a tenant admin who pastes a raw key
    into a *_ref field must never have that plaintext land in the
    platform-wide audit_log. Here the ref is a legitimate enc: token
    (this API's own registration succeeds), which is the concrete case
    the finding names: enc:<fernet-token> IS the credential sealed at
    rest, not merely a pointer to one."""
    tenant, _agent = tenant_agent
    tenant_hex = uuid.UUID(str(tenant["id"])).hex.upper()
    ref = f"env:TENANT_{tenant_hex}_TOKEN"
    import os
    os.environ[f"TENANT_{tenant_hex}_TOKEN"] = "SENTINEL-DO-NOT-LEAK-INTO-AUDIT-LOG"
    try:
        api = await custom_apis.create_custom_api(**_api_kwargs(
            str(tenant["id"]), "audited_api", auth_scheme="bearer", auth_config={"token_ref": ref},
        ))

        audit_row = await pool.fetchrow(
            "SELECT * FROM audit_log WHERE entity_type = 'custom_api' AND entity_id = $1 "
            "ORDER BY changed_at DESC LIMIT 1",
            api["id"],
        )
        assert audit_row is not None
        new_value = audit_row["new_value"]
        if isinstance(new_value, str):
            import json
            new_value = json.loads(new_value)

        assert ref not in str(new_value)
        assert "SENTINEL-DO-NOT-LEAK-INTO-AUDIT-LOG" not in str(new_value)
        assert new_value["auth_config"] == "[redacted]"
    finally:
        await pool.execute("DELETE FROM audit_log WHERE entity_type = 'custom_api' AND entity_id = $1", api["id"])
        await _cleanup_tenant_apis(pool, tenant["id"])
