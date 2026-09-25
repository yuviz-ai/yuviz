from __future__ import annotations

import ast
import inspect
import uuid

import pytest
from fastapi import HTTPException

from services.config.auth import CurrentUser
from services.telephony import auth as auth_module
from services.telephony.accounts import Account, accounts


def _user(role: str, tenant_id: str | None, *, is_service_account: bool = False) -> CurrentUser:
    return CurrentUser(id="u1", email="u@example.test", role=role, tenant_id=tenant_id, is_service_account=is_service_account)


@pytest.mark.asyncio
async def test_console_admin_passes():
    user = await auth_module.require_telephony_caller(_user("admin", None))
    assert user.role == "admin"


@pytest.mark.asyncio
async def test_null_tenant_service_account_passes():
    user = await auth_module.require_telephony_caller(_user("viewer", None, is_service_account=True))
    assert user.is_service_account is True


@pytest.mark.asyncio
async def test_tenant_scoped_viewer_is_rejected():
    with pytest.raises(HTTPException) as exc_info:
        await auth_module.require_telephony_caller(_user("viewer", str(uuid.uuid4())))
    assert exc_info.value.status_code == 403


def test_resolve_caller_tenant_is_async_def_with_awaited_assert_tenant_access():
    assert inspect.iscoroutinefunction(auth_module.resolve_caller_tenant)
    source = inspect.getsource(auth_module.resolve_caller_tenant)
    tree = ast.parse(source)
    awaited_calls = [
        n.value.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Await) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
    ]
    assert "assert_tenant_access" in awaited_calls


@pytest.mark.asyncio
async def test_resolve_caller_tenant_maps_slug_and_gates_cross_tenant(monkeypatch):
    tenant_a_id = str(uuid.uuid4())
    accounts._accounts = {
        ("vobiz", "cfg-a"): Account(
            provider="vobiz", account_ref="cfg-a", tenant_id=tenant_a_id, tenant_slug="tenant-a",
            is_default_outbound=True, credentials={}, instance=object(),
        ),
    }
    try:
        platform_user = _user("viewer", None, is_service_account=True)
        tenant_id, slug = await auth_module.resolve_caller_tenant(platform_user, "tenant-a")
        assert str(tenant_id) == tenant_a_id
        assert slug == "tenant-a"

        with pytest.raises(HTTPException) as exc_info:
            await auth_module.resolve_caller_tenant(platform_user, "no-such-tenant")
        assert exc_info.value.status_code == 404

        tenant_b_user = _user("admin", str(uuid.uuid4()))
        with pytest.raises(HTTPException):
            await auth_module.resolve_caller_tenant(tenant_b_user, "tenant-a")
    finally:
        accounts._accounts = {}
