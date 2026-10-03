"""Chat test sessions (agent_testing.py) against real Postgres and Redis. The only
mock is the vendor call; every assertion about stored state reads the row back."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio

from services.config import agent_testing, agents, cache, live_calls
from services.config import system_prompt as sp
from services.config.agent_testing import TURN_CAP, TurnLimitReached

GREETING = "Hi, thanks for calling Acme."
PROMPT = "You are Acme's receptionist. Be brief."


class _Resolver:
    async def resolve(self, ref):
        return "sk-test"


class _Model:
    """Stands in for the vendor call. `gate`, when set, holds every call until released."""

    def __init__(self):
        self.calls: list[tuple[list, str | None]] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, api_key, model, messages, system, max_tokens):
        self.calls.append((list(messages), system))
        if self.gate is not None:
            await self.gate.wait()
        return f"reply {len(self.calls)}"


@pytest.fixture
def model(monkeypatch):
    m = _Model()
    monkeypatch.setitem(sp._CALLERS, "openai", m)
    return m


@pytest_asyncio.fixture(loop_scope="session")
async def llm_id(pool, test_tenant):
    return await pool.fetchval(
        "INSERT INTO provider_configs (tenant_id, name, role, engine, api_key_ref) "
        "VALUES ($1, 'Mine', 'llm', 'openai', 'ref') RETURNING id",
        test_tenant["id"],
    )


@pytest_asyncio.fixture(loop_scope="session")
async def foreign_llm_id(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    tenant_id = await pool.fetchval(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING id", f"Foreign {slug}", slug,
    )
    config_id = await pool.fetchval(
        "INSERT INTO provider_configs (tenant_id, name, role, engine, api_key_ref) "
        "VALUES ($1, 'Theirs', 'llm', 'openai', 'ref') RETURNING id",
        tenant_id,
    )
    yield config_id
    await pool.execute("UPDATE agents SET llm_config_id = NULL WHERE llm_config_id = $1", config_id)
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant_id)
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant_id)


@pytest_asyncio.fixture(loop_scope="session")
async def agent(pool, test_tenant, llm_id, scoped):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="chat-me", name="Chat me", greeting=GREETING,
        system_prompt=PROMPT, llm_config_id=str(llm_id), status="inactive",
        tenant_slug=test_tenant["slug"],
    )
    yield created
    # calls/transcript_entries FK-reference agents/calls; test_tenant's teardown deletes agents.
    await pool.execute(
        "DELETE FROM transcript_entries WHERE session_id IN "
        "(SELECT session_id FROM calls WHERE agent_id = $1)", created["id"],
    )
    await pool.execute("DELETE FROM calls WHERE agent_id = $1", created["id"])
    async for key in cache.get_client().scan_iter(match="testcred:*"):
        raw = await cache.get_client().get(key)
        if raw and json.loads(raw)["agent_id"] == str(created["id"]):
            await cache.get_client().delete(key)


@pytest.fixture
def redis():
    return cache.get_client()


async def _mint(redis, test_tenant, agent):
    return await agent_testing.mint_test_session(
        redis, tenant=test_tenant, agent=agent, channel="chat",
    )


async def _turn(redis, test_tenant, agent, session, message="hello"):
    return await agent_testing.run_chat_turn(
        redis, tenant=test_tenant, agent=agent, session_id=session["session_id"],
        credential=session["credential"], message=message, secret_resolver=_Resolver(),
    )


async def _entries(pool, session_id):
    return [dict(r) for r in await pool.fetch(
        "SELECT turn_number, caller_text, ai_response, llm_engine FROM transcript_entries "
        "WHERE session_id = $1 ORDER BY turn_number", session_id,
    )]


async def test_chat_mint_writes_a_born_ended_test_call_and_the_greeting_row(
    pool, redis, test_tenant, agent,
):
    session = await _mint(redis, test_tenant, agent)

    call = await pool.fetchrow("SELECT * FROM calls WHERE session_id = $1", session["session_id"])
    assert call["direction"] == "test"
    assert call["tenant_id"] == test_tenant["slug"]
    assert call["agent_id"] == agent["id"]
    assert call["ended_at"] is not None
    assert call["turn_count"] == 0
    assert await _entries(pool, session["session_id"]) == [
        {"turn_number": 0, "caller_text": None, "ai_response": GREETING, "llm_engine": None},
    ]
    assert session["greeting"] == GREETING
    assert session["expires_in"] == 900


async def test_a_turn_stores_turn_one_and_the_model_sees_the_greeting_then_a_user_message(
    pool, redis, test_tenant, agent, model,
):
    session = await _mint(redis, test_tenant, agent)

    reply = await _turn(redis, test_tenant, agent, session, "what are your hours?")

    assert reply == "reply 1"
    assert await _entries(pool, session["session_id"]) == [
        {"turn_number": 0, "caller_text": None, "ai_response": GREETING, "llm_engine": None},
        {"turn_number": 1, "caller_text": "what are your hours?", "ai_response": "reply 1",
         "llm_engine": "openai"},
    ]
    assert await pool.fetchval(
        "SELECT turn_count FROM calls WHERE session_id = $1", session["session_id"]) == 1
    messages, system = model.calls[0]
    assert GREETING in system and PROMPT in system
    assert messages == [{"role": "user", "content": "what are your hours?"}]


async def test_second_turn_replays_the_first_as_history(redis, test_tenant, agent, model):
    session = await _mint(redis, test_tenant, agent)
    await _turn(redis, test_tenant, agent, session, "one")
    await _turn(redis, test_tenant, agent, session, "two")

    assert model.calls[1][0] == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "two"},
    ]


async def test_turn_41_raises_the_cap_error_and_stores_nothing(pool, redis, test_tenant, agent, model):
    session = await _mint(redis, test_tenant, agent)
    for i in range(TURN_CAP):
        await _turn(redis, test_tenant, agent, session, f"m{i}")

    with pytest.raises(TurnLimitReached):
        await _turn(redis, test_tenant, agent, session, "one too many")

    assert len(model.calls) == TURN_CAP
    assert await pool.fetchval(
        "SELECT turn_count FROM calls WHERE session_id = $1", session["session_id"]) == TURN_CAP
    assert await pool.fetchval(
        "SELECT count(*) FROM transcript_entries WHERE session_id = $1 AND caller_text = 'one too many'",
        session["session_id"]) == 0


async def test_two_concurrent_turns_at_39_give_one_winner_and_unique_turn_numbers(
    pool, redis, test_tenant, agent, model,
):
    session = await _mint(redis, test_tenant, agent)
    await pool.execute(
        "UPDATE calls SET turn_count = 39 WHERE session_id = $1", session["session_id"])
    model.gate = asyncio.Event()

    async def release_once_both_have_had_their_chance():
        await asyncio.sleep(0.3)
        model.gate.set()

    releaser = asyncio.create_task(release_once_both_have_had_their_chance())
    results = await asyncio.gather(
        _turn(redis, test_tenant, agent, session, "a"),
        _turn(redis, test_tenant, agent, session, "b"),
        return_exceptions=True,
    )
    await releaser

    assert sum(isinstance(r, TurnLimitReached) for r in results) == 1
    assert sum(isinstance(r, str) for r in results) == 1
    numbers = [e["turn_number"] for e in await _entries(pool, session["session_id"])]
    assert numbers == [0, 40]
    assert await pool.fetchval(
        "SELECT turn_count FROM calls WHERE session_id = $1", session["session_id"]) == 40


async def test_chat_test_row_is_not_live_and_does_not_block_deleting_the_agent(
    pool, redis, test_tenant, agent, model,
):
    session = await _mint(redis, test_tenant, agent)
    await _turn(redis, test_tenant, agent, session)

    live = await live_calls.get_live_calls(test_tenant["slug"], include_transcript=True)
    assert live["items"] == []
    assert live["kpis"]["live_calls"] == 0
    assert await pool.fetchval(
        "SELECT ended_at IS NOT NULL FROM calls WHERE session_id = $1", session["session_id"])

    await agents.soft_delete_agent(agent["id"], tenant_slug=test_tenant["slug"])

    assert await pool.fetchval(
        "SELECT deleted_at IS NOT NULL FROM agents WHERE id = $1", agent["id"])


async def test_load_test_transcript_ignores_another_tenants_calls_row(
    pool, redis, test_tenant, agent, foreign_llm_id,
):
    # Tenant B's own test call that points at tenant A's agent. The agent predicate
    # alone would match it; only the tenant predicate keeps it out.
    other_slug = await pool.fetchval(
        "SELECT t.slug FROM provider_configs p JOIN tenants t ON t.id = p.tenant_id WHERE p.id = $1",
        foreign_llm_id,
    )
    session_id = str(uuid.uuid4())
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, agent_id, ended_at) "
        "VALUES ($1, $2, 'test', $3, now())", session_id, other_slug, agent["id"],
    )
    await pool.execute(
        "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response) "
        "VALUES ($1, 1, 'tenant B caller', 'tenant B agent')", session_id,
    )

    async with pool.acquire() as conn:
        as_a = await agent_testing.load_test_transcript(
            conn, tenant_slug=test_tenant["slug"], agent_id=agent["id"], session_id=session_id)
        as_b = await agent_testing.load_test_transcript(
            conn, tenant_slug=other_slug, agent_id=agent["id"], session_id=session_id)

    assert as_a == []
    assert as_b == [("tenant B caller", "tenant B agent")]


async def test_load_test_transcript_returns_caller_agent_tuples_in_turn_order(
    pool, redis, test_tenant, agent, model,
):
    session = await _mint(redis, test_tenant, agent)
    await _turn(redis, test_tenant, agent, session, "one")

    async with pool.acquire() as conn:
        rows = await agent_testing.load_test_transcript(
            conn, tenant_slug=test_tenant["slug"], agent_id=agent["id"],
            session_id=session["session_id"])

    assert rows == [(None, GREETING), ("one", "reply 1")]


async def test_foreign_and_random_llm_config_ids_raise_the_same_error_with_no_model_call(
    pool, redis, test_tenant, agent, foreign_llm_id, model,
):
    session = await _mint(redis, test_tenant, agent)
    messages = []
    for bad in (foreign_llm_id, uuid.uuid4()):
        async with pool.acquire() as conn, conn.transaction():
            # The FK would refuse a random id, so switch it off for this one write.
            await conn.execute("SET LOCAL session_replication_role = replica")
            await conn.execute("UPDATE agents SET llm_config_id = $2 WHERE id = $1", agent["id"], bad)
        stored = dict(await pool.fetchrow("SELECT * FROM agents WHERE id = $1", agent["id"]))
        assert stored["llm_config_id"] == bad
        with pytest.raises(LookupError) as exc:
            await _turn(redis, test_tenant, stored, session)
        messages.append(str(exc.value))

    assert messages == ["provider_config not found"] * 2
    assert model.calls == []
    assert await pool.fetchval(
        "SELECT turn_count FROM calls WHERE session_id = $1", session["session_id"]) == 0


async def test_agent_without_its_own_llm_falls_back_to_the_tenant_default(
    pool, redis, test_tenant, agent, llm_id, model,
):
    await pool.execute("UPDATE agents SET llm_config_id = NULL WHERE id = $1", agent["id"])
    await pool.execute(
        "UPDATE tenants SET default_llm_config_id = $2 WHERE id = $1", test_tenant["id"], llm_id)
    tenant = dict(await pool.fetchrow("SELECT * FROM tenants WHERE id = $1", test_tenant["id"]))
    stored = dict(await pool.fetchrow("SELECT * FROM agents WHERE id = $1", agent["id"]))
    session = await _mint(redis, tenant, stored)

    assert await _turn(redis, tenant, stored, session) == "reply 1"


async def test_credential_for_another_session_agent_or_channel_is_session_not_found(
    pool, redis, test_tenant, agent, model,
):
    first = await _mint(redis, test_tenant, agent)
    second = await _mint(redis, test_tenant, agent)
    voice = await agent_testing.mint_test_session(
        redis, tenant=test_tenant, agent=agent, channel="voice")
    other_agent = {**agent, "id": uuid.uuid4()}

    for session, ag in (
        ({**first, "credential": second["credential"]}, agent),
        ({**first, "credential": voice["credential"]}, agent),
        ({**first, "credential": "nope"}, agent),
        (first, other_agent),
    ):
        with pytest.raises(LookupError, match="test session not found"):
            await _turn(redis, test_tenant, ag, session)
    assert model.calls == []
