"""Capacity router — platform limits, CPS limits and per-tenant caps, for the platform operator only."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from libs.tenancy import platform_conn
from services.config.auth import CurrentUser
from services.config.deps import get_current_user, is_platform_scoped

from .. import audit, capacity, db
from ..schemas import CpsLimitUpdate, PlatformLimitsUpdate, TenantCapUpdate

_AUDIT_NAMESPACE = uuid.UUID("6f1b8c1e-4a52-4c3e-9d57-2b0a1f5e8a10")


async def require_platform_operator(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "superadmin" or not is_platform_scoped(user):
        raise HTTPException(status_code=403, detail="only the platform operator can manage capacity")
    return user


capacity_router = APIRouter(
    prefix="/capacity", tags=["capacity"], dependencies=[Depends(require_platform_operator)],
)


def _tenant_uuid(tenant_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="tenant not found") from None


def _tenant_cap_body(tenant_id: uuid.UUID, row: dict[str, Any]) -> dict[str, Any]:
    return {"tenant_id": tenant_id, **row}


async def _platform_limits_body(conn) -> dict[str, Any]:
    return {**await capacity.get_platform_limits(conn), "cps_limits": await capacity.list_cps_limits(conn)}


@capacity_router.get("/platform")
async def get_platform():
    pool = await db.get_pool()
    async with platform_conn(pool, reason="capacity-admin") as conn:
        return await _platform_limits_body(conn)


@capacity_router.put("/platform")
async def put_platform(body: PlatformLimitsUpdate, user: CurrentUser = Depends(require_platform_operator)):
    pool = await db.get_pool()
    async with platform_conn(pool, reason="capacity-admin") as conn:
        # The singleton row is the one mutex for every capacity write, so each audit old_value is exact.
        old = await capacity.get_platform_limits(conn, for_update=True)
        new = await capacity.set_platform_limits(
            conn, total_call_capacity=body.total_call_capacity, inbound_reserve=body.inbound_reserve,
            max_outbound_concurrent_calls=body.max_outbound_concurrent_calls, updated_by=user.id,
        )
        await audit.write_audit(
            conn, entity_type="platform_outbound_limits",
            entity_id=uuid.uuid5(_AUDIT_NAMESPACE, "platform_outbound_limits"), action="updated",
            user_id=user.id, user_email=user.email, old_value=old, new_value=new,
        )
        return await _platform_limits_body(conn)


@capacity_router.put("/cps/{scope}")
async def put_cps(scope: str, body: CpsLimitUpdate, user: CurrentUser = Depends(require_platform_operator)):
    pool = await db.get_pool()
    async with platform_conn(pool, reason="capacity-admin") as conn:
        await capacity.get_platform_limits(conn, for_update=True)
        old = await conn.fetchval("SELECT cps FROM outbound_cps_limits WHERE scope = $1", scope)
        await capacity.set_cps_limit(conn, scope, body.cps, updated_by=user.id)
        await audit.write_audit(
            conn, entity_type="outbound_cps_limit", entity_id=uuid.uuid5(_AUDIT_NAMESPACE, scope),
            action="created" if old is None else "updated", user_id=user.id, user_email=user.email,
            old_value=None if old is None else {"scope": scope, "cps": old}, new_value={"scope": scope, "cps": body.cps},
        )
        return next(r for r in await capacity.list_cps_limits(conn) if r["scope"] == scope)


@capacity_router.get("/usage")
async def get_usage():
    pool = await db.get_pool()
    async with platform_conn(pool, reason="capacity-admin") as conn:
        return await capacity.usage(conn)


@capacity_router.get("/tenants")
async def list_tenants():
    pool = await db.get_pool()
    async with platform_conn(pool, reason="capacity-admin") as conn:
        return await capacity.list_tenant_caps(conn)


@capacity_router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: str):
    tenant_uuid = _tenant_uuid(tenant_id)
    pool = await db.get_pool()
    async with platform_conn(pool, reason="capacity-admin") as conn:
        row = await capacity.get_tenant_cap(conn, tenant_uuid)
    if row is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return _tenant_cap_body(tenant_uuid, row)


@capacity_router.put("/tenants/{tenant_id}")
async def put_tenant(tenant_id: str, body: TenantCapUpdate, user: CurrentUser = Depends(require_platform_operator)):
    tenant_uuid = _tenant_uuid(tenant_id)
    pool = await db.get_pool()
    async with platform_conn(pool, reason="capacity-admin") as conn:
        await capacity.get_platform_limits(conn, for_update=True)
        old = await capacity.get_tenant_cap(conn, tenant_uuid, for_update=True)
        if old is None:
            raise HTTPException(status_code=404, detail="tenant not found")
        await capacity.set_tenant_cap(conn, tenant_uuid, body.max_outbound_concurrent_calls)
        await audit.write_audit(
            conn, entity_type="tenant_outbound_cap", entity_id=tenant_uuid, action="updated",
            user_id=user.id, user_email=user.email,
            old_value={"max_outbound_concurrent_calls": old["max_outbound_concurrent_calls"]},
            new_value={"max_outbound_concurrent_calls": body.max_outbound_concurrent_calls},
        )
        return _tenant_cap_body(tenant_uuid, await capacity.get_tenant_cap(conn, tenant_uuid))
