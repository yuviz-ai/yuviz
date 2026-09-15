"""services/config/tests/test_deps_tenant_access.py — T11.

assert_tenant_access's 403 (UUID mismatch) / 404 (slug mismatch or
unknown) shapes must match the two existing precedents verbatim: the UUID
403 is toolexec/routers/custom_apis.py:38's `_require_tenant_access`
("tenant_id does not match the caller's tenant"); the slug 404 is
agents.py's `_resolve_tenant` (f"tenant {slug!r} not found"). bind_path_tenant
must never raise, for any input — it is unauthenticated and order-
independent by construction.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException, Request

from services.config import deps
from services.config.auth import CurrentUser

pytestmark = pytest.mark.asyncio


def _user(*, tenant_id: str | None, role: str = "admin") -> CurrentUser:
    return CurrentUser(id=str(uuid.uuid4()), email="u@example.com", role=role, tenant_id=tenant_id)


def _request(path_params: dict) -> Request:
    scope = {"type": "http", "path_params": path_params, "headers": []}
    return Request(scope)


async def test_uuid_mismatch_is_403_matching_custom_apis_shape():
    caller = _user(tenant_id=str(uuid.uuid4()))
    foreign_tenant_id = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await deps.assert_tenant_access(foreign_tenant_id, caller)
    assert exc.value.status_code == 403
    assert exc.value.detail == "tenant_id does not match the caller's tenant"


async def test_uuid_match_is_allowed():
    tenant_id = str(uuid.uuid4())
    caller = _user(tenant_id=tenant_id)
    await deps.assert_tenant_access(tenant_id, caller)  # must not raise


async def test_none_tenant_is_403_for_a_tenant_scoped_caller():
    caller = _user(tenant_id=str(uuid.uuid4()))
    with pytest.raises(HTTPException) as exc:
        await deps.assert_tenant_access(None, caller)
    assert exc.value.status_code == 403


async def test_none_tenant_is_allowed_for_a_platform_scoped_caller():
    caller = _user(tenant_id=None, role="superadmin")
    await deps.assert_tenant_access(None, caller)  # must not raise


async def test_platform_scoped_caller_bypasses_any_uuid_mismatch():
    caller = _user(tenant_id=None, role="superadmin")
    await deps.assert_tenant_access(str(uuid.uuid4()), caller)  # must not raise


async def test_unknown_slug_is_404_not_a_slug_oracle(monkeypatch):
    caller = _user(tenant_id=str(uuid.uuid4()))
    monkeypatch.setattr(deps.tenants_service, "get_tenant", _fake_get_tenant(None))
    with pytest.raises(HTTPException) as exc:
        await deps.assert_tenant_access("nonexistent-slug", caller)
    assert exc.value.status_code == 404
    assert exc.value.detail == "tenant {!r} not found".format("nonexistent-slug")


async def test_foreign_slug_is_404_identical_to_unknown_slug(monkeypatch):
    """A {tenant_slug} path must 404 identically for 'missing' and 'not
    yours', or it becomes a slug oracle (lesson 2)."""
    caller = _user(tenant_id=str(uuid.uuid4()))
    foreign_tenant_row = {"id": uuid.uuid4()}
    monkeypatch.setattr(deps.tenants_service, "get_tenant", _fake_get_tenant(foreign_tenant_row))
    with pytest.raises(HTTPException) as exc:
        await deps.assert_tenant_access("someone-elses-slug", caller)
    assert exc.value.status_code == 404
    assert exc.value.detail == "tenant 'someone-elses-slug' not found"


async def test_own_slug_is_allowed(monkeypatch):
    tenant_id = uuid.uuid4()
    caller = _user(tenant_id=str(tenant_id))
    monkeypatch.setattr(deps.tenants_service, "get_tenant", _fake_get_tenant({"id": tenant_id}))
    await deps.assert_tenant_access("my-own-slug", caller)  # must not raise


def _fake_get_tenant(row):
    async def _get_tenant(slug: str):
        return row
    return _get_tenant


async def test_bind_path_tenant_raises_nothing_for_any_input():
    for path_params in ({}, {"tenant_id": str(uuid.uuid4())}, {"tenant_slug": "some-slug"},
                         {"tenant_id": "not-a-uuid-at-all"}, {"tenant_slug": ""}):
        await deps.bind_path_tenant(_request(path_params))  # must not raise
