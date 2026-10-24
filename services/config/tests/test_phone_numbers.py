from __future__ import annotations

import uuid

import asyncpg
import pytest

from services.config import agents, cache, phone_numbers


async def test_create_and_get_by_did_resolves_tenant_and_agent(test_tenant, scoped, pool):
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support", name="Support Agent",
    )
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(
        tenant_id=test_tenant["id"], did=did, agent_id=agent["id"],
    )

    resolved = await phone_numbers.get_by_did(did)
    assert resolved == {
        "tenant_slug": test_tenant["slug"], "agent_slug": "support", "version": 1,
    }

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_get_by_did_with_no_agent_resolves_to_default_agent_slug(test_tenant, scoped, pool):
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did, agent_id=None)

    resolved = await phone_numbers.get_by_did(did)
    assert resolved == {
        "tenant_slug": test_tenant["slug"], "agent_slug": "default", "version": None,
    }

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_get_by_did_returns_agent_config_version(test_tenant, scoped, pool):
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="sales", name="Sales Agent",
    )
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(
        tenant_id=test_tenant["id"], did=did, agent_id=agent["id"],
    )

    assert (await phone_numbers.get_by_did(did))["version"] == 1

    # Agent update bumps config_version via trigger.
    await agents.update_agent(agent["id"], tenant_slug=test_tenant["slug"], greeting="Updated greeting")
    await cache.invalidate(f"did:{did}")

    assert (await phone_numbers.get_by_did(did))["version"] == 2

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_get_by_did_unknown_number_returns_none(test_tenant, scoped):
    assert await phone_numbers.get_by_did("test-did-does-not-exist") is None


async def test_create_phone_number_warms_cache_immediately(test_tenant, scoped, pool):
    # A new DID must be routable before any read populates the cache.
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)

    assert await cache.get_json(f"did:{did}") is not None

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_get_by_did_populates_cache_on_miss(test_tenant, scoped, pool):
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)
    await cache.invalidate(f"did:{did}")  # force a miss, independent of create's own warming

    assert await cache.get_json(f"did:{did}") is None
    await phone_numbers.get_by_did(did)
    assert await cache.get_json(f"did:{did}") is not None

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_update_phone_number_rename_updates_did_and_clears_old_row(test_tenant, scoped, pool):
    """Row-level rename; cache behavior is covered by the write-through test below."""
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    new_did = f"9{uuid.uuid4().int % 10**10:010d}"
    created = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)

    updated = await phone_numbers.update_phone_number(created["id"], did=new_did)
    assert updated["did"] == new_did

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", new_did)


async def test_soft_delete_phone_number_invalidates_cache_and_hides_from_get_by_did(test_tenant, scoped, pool):
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    created = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)
    await phone_numbers.get_by_did(did)  # warm cache

    await phone_numbers.soft_delete_phone_number(created["id"])
    assert await cache.get_json(f"did:{did}") is None
    assert await phone_numbers.get_by_did(did) is None

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_did_unique_constraint_rejects_duplicate(test_tenant, scoped, pool):
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)
    with pytest.raises(Exception):
        await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_list_phone_numbers_scoped_to_tenant(test_tenant, scoped, pool):
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)

    listed = await phone_numbers.list_phone_numbers(test_tenant["id"])
    assert [p["did"] for p in listed] == [did]

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_inactive_status_resolves_to_none_not_normal_agent(test_tenant, scoped, pool):
    """A suspended DID resolves like an unknown number."""
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support", name="Support Agent",
    )
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(
        tenant_id=test_tenant["id"], did=did, agent_id=agent["id"], status="suspended",
    )

    assert await phone_numbers.get_by_did(did) is None

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_status_check_constraint_rejects_invalid_value(test_tenant, scoped, pool):
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    with pytest.raises(Exception):
        await phone_numbers.create_phone_number(
            tenant_id=test_tenant["id"], did=did, status="not-a-real-status",
        )


async def test_prewarm_populates_cache_for_active_dids_only(test_tenant, scoped, pool):
    active_did = f"9{uuid.uuid4().int % 10**10:010d}"
    inactive_did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=active_did, status="active")
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=inactive_did, status="inactive")

    await cache.invalidate(f"did:{active_did}", f"did:{inactive_did}")
    warmed = await phone_numbers.prewarm(overwrite=True)

    assert warmed >= 1  # at least this test's active DID (other tests may leave rows too)
    assert await cache.get_json(f"did:{active_did}") is not None
    assert await cache.get_json(f"did:{inactive_did}") is None  # prewarm only queries active rows
    assert await phone_numbers.routes_loaded()

    await pool.execute("DELETE FROM phone_numbers WHERE did IN ($1, $2)", active_did, inactive_did)


async def test_did_cache_has_no_ttl(test_tenant, scoped, pool):
    """DID entries never expire: the Gateway reads Redis only, with no fallback query."""
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)

    client = cache.get_client()
    assert await client.ttl(f"did:{did}") == -1  # -1 == "no expiry", per redis-py

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_update_phone_number_writes_through_new_did_immediately(test_tenant, scoped, pool):
    """update_phone_number() writes the cache through; invalidating alone leaves the DID unroutable."""
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="write-through-agent", name="Write Through",
    )
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    created = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)

    await phone_numbers.update_phone_number(created["id"], agent_id=agent["id"])

    cached = await cache.get_json(f"did:{did}")
    assert cached is not None
    assert cached["agent_slug"] == "write-through-agent"

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_update_phone_number_rename_writes_through_new_and_clears_old(test_tenant, scoped, pool):
    old_did = f"9{uuid.uuid4().int % 10**10:010d}"
    new_did = f"9{uuid.uuid4().int % 10**10:010d}"
    created = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=old_did)

    await phone_numbers.update_phone_number(created["id"], did=new_did)

    assert await cache.get_json(f"did:{old_did}") is None
    assert await cache.get_json(f"did:{new_did}") is not None

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", new_did)


async def test_prewarm_never_overwrites_a_route_written_by_the_api(test_tenant, scoped, pool):
    # The API writes routes while a reload runs; the reload's older read must not win.
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)
    newer = {"tenant_slug": "written-by-api", "agent_slug": "x", "version": 9}
    await cache.set_json(f"did:{did}", newer, ttl=None)

    await phone_numbers.prewarm(overwrite=False)

    assert await cache.get_json(f"did:{did}") == newer

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_deleting_an_agent_stops_routing_its_numbers_to_it(test_tenant, scoped, pool):
    agent = await agents.create_agent(tenant_id=test_tenant["id"], slug="leaving", name="Leaving")
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did, agent_id=agent["id"])

    await agents.soft_delete_agent(agent["id"], tenant_slug=test_tenant["slug"])

    route = await cache.get_json(f"did:{did}")
    assert route == {"tenant_slug": test_tenant["slug"], "agent_slug": "default", "version": None}

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_route_reload_survives_non_redis_failures(monkeypatch):
    # Redis and Postgres restarting together raises asyncpg/OSError, not RedisError.
    from unittest.mock import AsyncMock

    from services.config import app as config_app

    failing = AsyncMock(side_effect=OSError("postgres restarting"))
    monkeypatch.setattr(config_app.phone_numbers_service, "prewarm", failing)

    await config_app._prewarm_routes(overwrite=False)

    failing.assert_awaited_once()


async def test_startup_prewarm_repairs_a_stale_route(test_tenant, scoped, pool):
    # A failed write during a reassignment leaves the old tenant's route; the startup load fixes it.
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)
    await cache.set_json(f"did:{did}", {"tenant_slug": "previous-owner", "agent_slug": "x", "version": 1}, ttl=None)

    await phone_numbers.prewarm(overwrite=True)

    assert (await cache.get_json(f"did:{did}"))["tenant_slug"] == test_tenant["slug"]

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_agent_update_refreshes_cached_route_version(test_tenant, scoped, pool):
    agent = await agents.create_agent(tenant_id=test_tenant["id"], slug="versioned", name="Versioned")
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did, agent_id=agent["id"])

    await agents.update_agent(agent["id"], tenant_slug=test_tenant["slug"], greeting="Hello again")

    assert (await cache.get_json(f"did:{did}"))["version"] == 2

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_failed_route_write_is_logged_as_an_error(caplog, monkeypatch):
    import logging

    import redis.asyncio as redis

    class Down:
        async def set(self, *a, **k):
            raise redis.ConnectionError("down")

        async def delete(self, *a, **k):
            raise redis.ConnectionError("down")

    monkeypatch.setattr(cache, "get_client", lambda: Down())
    with caplog.at_level(logging.ERROR, logger=phone_numbers.__name__):
        await phone_numbers._store_route("5555", {"tenant_slug": "a", "agent_slug": "b", "version": 1})
        await phone_numbers._drop_route("5555")

    assert [r.levelno for r in caplog.records] == [logging.ERROR, logging.ERROR]


async def test_update_writes_redis_while_the_rows_lock_is_still_held(test_tenant, scoped, pool):
    """update_phone_number's Redis write must land before its transaction commits (and so
    before its FOR UPDATE lock on the number's row releases) — otherwise a concurrent
    refresh_routes_for_agent() that squeezes in right after commit, but before this call's
    write lands, gets its own fresher write silently overwritten by this one arriving late.

    Proven directly: a second connection tries to take the same row's lock, NOWAIT, from
    inside the patched _store_route — it must fail, meaning the main transaction (and
    its lock) are still open at write time."""
    agent = await agents.create_agent(tenant_id=test_tenant["id"], slug="lock-check", name="Lock check")
    did = f"9{uuid.uuid4().int % 10**10:010d}"
    number = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did, agent_id=agent["id"])

    original_store = phone_numbers._store_route
    observations: list[bool] = []

    async def spying_store(did_arg, route):
        async with pool.acquire() as probe:
            try:
                await probe.execute(
                    "SELECT 1 FROM phone_numbers WHERE id = $1 FOR UPDATE NOWAIT", number["id"],
                )
                observations.append(False)  # lock was free: write landed after commit — the bug
            except asyncpg.LockNotAvailableError:
                observations.append(True)  # lock still held: write landed before commit
        await original_store(did_arg, route)

    phone_numbers._store_route = spying_store
    try:
        await phone_numbers.update_phone_number(number["id"], region="eu")
    finally:
        phone_numbers._store_route = original_store

    assert observations == [True]

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_did_is_stored_digits_only_and_routes_under_that_key(test_tenant, scoped, pool):
    # A carrier's "+91..." form and the admin's typed form must end up as one key.
    agent = await agents.create_agent(tenant_id=test_tenant["id"], slug="intl", name="Intl")
    digits = f"91{uuid.uuid4().int % 10**10:010d}"
    typed = f"+{digits[:2]} {digits[2:6]}-{digits[6:]}"

    number = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=typed, agent_id=agent["id"])

    assert number["did"] == digits
    assert (await cache.get_json(f"did:{digits}"))["agent_slug"] == "intl"
    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", digits)


async def test_the_same_number_in_another_format_is_already_assigned(test_tenant, scoped, pool):
    digits = f"1{uuid.uuid4().int % 10**10:010d}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=digits)

    with pytest.raises(phone_numbers.DidAlreadyAssigned):
        await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=f"+{digits}")

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", digits)


async def test_update_normalizes_the_did(test_tenant, scoped, pool):
    old, new = (f"1{uuid.uuid4().int % 10**10:010d}" for _ in range(2))
    number = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=old)

    updated = await phone_numbers.update_phone_number(number["id"], did=f"+{new[0]} {new[1:]}")

    assert updated["did"] == new
    assert await cache.get_json(f"did:{new}") is not None
    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", new)


@pytest.mark.parametrize("did", ["", "   "])
async def test_an_empty_did_is_rejected(test_tenant, scoped, did):
    with pytest.raises(ValueError):
        await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)


async def test_a_sip_uri_did_is_stored_as_typed(test_tenant, scoped, pool):
    agent = await agents.create_agent(tenant_id=test_tenant["id"], slug="pbx", name="PBX")
    uri = f"sip:{uuid.uuid4().hex[:6]}@pbx.example.com"

    number = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=uri, agent_id=agent["id"])

    assert number["did"] == uri
    assert (await cache.get_json(f"did:{uri}"))["agent_slug"] == "pbx"
    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", uri)
