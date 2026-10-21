from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from services.config import audit, carriers, phone_numbers
from services.config.app import app


async def _carrier(tenant):
    return await carriers.create_carrier(
        tenant_id=tenant["id"], name="Disconnect test", provider="plivo", allow_pointer_schemes=False,
    )


async def _blocked_by(pool, pid):
    async with asyncio.timeout(5):
        while not await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid)))", pid,
        ):
            await asyncio.sleep(0.01)


async def test_delete_rejects_attached_numbers_and_label_can_be_cleared(test_tenant, test_admin, scoped):
    carrier = await _carrier(test_tenant)
    number = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did="1350001", carrier_id=carrier["id"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {test_admin['token']}"}) as client:
        response = await client.delete(f"/carriers/{carrier['id']}")
        assert response.status_code == 409
        assert "remove or move" in response.json()["detail"]
        await client.patch(f"/carriers/{carrier['id']}", json={"carrier_account_ref": "Main"})
        cleared = await client.patch(f"/carriers/{carrier['id']}", json={"carrier_account_ref": None})
        assert cleared.status_code == 200
        assert cleared.json()["carrier_account_ref"] is None
        await phone_numbers.update_phone_number(number["id"], carrier_id=None)
        response = await client.delete(f"/carriers/{carrier['id']}")
        assert response.status_code == 204


@pytest.mark.parametrize("move_existing", [False, True])
async def test_attachment_wins_race_and_delete_is_rejected(test_tenant, scoped, pool, monkeypatch, move_existing):
    carrier = await _carrier(test_tenant)
    number = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did="1350011") if move_existing else None
    locked, release = asyncio.Event(), asyncio.Event()
    original = phone_numbers._lock_live_carrier
    pid = None

    async def pause_after_lock(conn, carrier_id):
        nonlocal pid
        await original(conn, carrier_id)
        pid = await conn.fetchval("SELECT pg_backend_pid()")
        locked.set()
        await release.wait()

    monkeypatch.setattr(phone_numbers, "_lock_live_carrier", pause_after_lock)
    attach = asyncio.create_task(phone_numbers.update_phone_number(number["id"], carrier_id=carrier["id"]) if number else phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did="1350010", carrier_id=carrier["id"]))
    delete = None
    try:
        await asyncio.wait_for(locked.wait(), 5)
        delete = asyncio.create_task(carriers.soft_delete_carrier(carrier["id"]))
        await _blocked_by(pool, pid)
    finally:
        release.set()
        results = await asyncio.gather(*[task for task in (attach, delete) if task], return_exceptions=True)
    assert isinstance(results[0], dict)
    assert isinstance(results[1], carriers.CarrierHasNumbers)
    assert await carriers.get_carrier_by_id(carrier["id"]) is not None


@pytest.mark.parametrize("move_existing", [False, True])
async def test_delete_wins_race_and_attachment_is_rejected(test_tenant, scoped, pool, monkeypatch, move_existing):
    carrier = await _carrier(test_tenant)
    number = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did="1350021") if move_existing else None
    locked, release = asyncio.Event(), asyncio.Event()
    original = audit.write_audit
    pid = None

    async def pause_before_commit(conn, **kwargs):
        nonlocal pid
        await original(conn, **kwargs)
        if kwargs.get("entity_type") == "carrier" and kwargs.get("action") == "deleted":
            pid = await conn.fetchval("SELECT pg_backend_pid()")
            locked.set()
            await release.wait()

    monkeypatch.setattr(audit, "write_audit", pause_before_commit)
    delete = asyncio.create_task(carriers.soft_delete_carrier(carrier["id"]))
    attach = None
    try:
        await asyncio.wait_for(locked.wait(), 5)
        attach = asyncio.create_task(phone_numbers.update_phone_number(number["id"], carrier_id=carrier["id"]) if number else phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did="1350020", carrier_id=carrier["id"]))
        await _blocked_by(pool, pid)
    finally:
        release.set()
        results = await asyncio.gather(*[task for task in (delete, attach) if task], return_exceptions=True)
    assert results[0] is None
    assert isinstance(results[1], LookupError)
    assert not await pool.fetchval("SELECT count(*) FROM phone_numbers WHERE carrier_id = $1 AND deleted_at IS NULL", carrier["id"])
