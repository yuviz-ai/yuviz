"""Guided agent creation routes (from-template, test sessions, test chat, revise, accept, undo,
the template catalog) against real Postgres and Redis with real JWTs.

The only mocks are the vendor model call and, in the sentinel test, the vendor's HTTP transport.
Every assertion about stored state reads the row back from Postgres. Postgres runs as a
superuser here, so RLS is inert and these tests prove the explicit predicates, not RLS.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import json
import logging
import pathlib
import re
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from libs.config_sdk.providers.cache_aside import CacheAsideConfigProvider
from libs.config_sdk.repositories.http_repository import HttpConfigRepository
from libs.config_sdk.repositories.redis_repository import RedisConfigRepository
from libs.config_sdk.test_credentials import _key
from services.config import agent_testing, auth, cache, deps
from services.config import system_prompt as sp
from services.config import users as users_service
from services.config.app import AgentAssistThrottle, app
from services.config.routers import agents as agents_router

SECRET_PASSWORD = "test-password-not-real"

# The six new routes under /tenants/{tenant_slug}/agents, plus the catalog.
NEW_ROUTES = {
    ("POST", "/tenants/{tenant_slug}/agents/from-template"),
    ("POST", "/tenants/{tenant_slug}/agents/{agent_id}/test-sessions"),
    ("POST", "/tenants/{tenant_slug}/agents/{agent_id}/test-chat"),
    ("POST", "/tenants/{tenant_slug}/agents/{agent_id}/prompt/revise"),
    ("POST", "/tenants/{tenant_slug}/agents/{agent_id}/prompt/accept"),
    ("POST", "/tenants/{tenant_slug}/agents/{agent_id}/prompt/undo"),
    ("GET", "/agent-templates"),
}
SLOT_KEYS = ("prompt_undo_previous", "prompt_undo_accepted_sha256")


# ── helpers ──────────────────────────────────────────────────────────────

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _client_for(token: str | None = None) -> AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


def _shape(resp: httpx.Response) -> tuple[int, bytes]:
    return resp.status_code, resp.content


def _extend(prompt: str, line: str) -> str:
    """A well-formed revision: the same prompt with one more job line."""
    return f"{prompt}\n{line}"


class _Model:
    """Stands in for the vendor call. `gate`, when set, holds every call until released."""

    def __init__(self):
        self.calls: list[tuple[list, str | None]] = []
        self.reply = "a reply"
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def __call__(self, api_key, model, messages, system, max_tokens):
        self.calls.append((list(messages), system))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.reply


class _StubResolver:
    async def resolve(self, ref):
        return "sk-test"


@pytest.fixture(autouse=True)
def _stub_secret_resolver(monkeypatch):
    monkeypatch.setattr(agents_router, "_secret_resolver", _StubResolver())


@pytest.fixture(autouse=True)
def _fresh_throttle():
    app.state.agent_assist_throttle = AgentAssistThrottle()
    yield
    app.state.agent_assist_throttle = AgentAssistThrottle()


@pytest.fixture
def model(monkeypatch):
    m = _Model()
    monkeypatch.setitem(sp._CALLERS, "openai", m)
    return m


async def _insert_configs(pool, tenant_id) -> dict[str, str]:
    ids = {}
    for role, engine in (("stt", "deepgram"), ("llm", "openai"), ("tts", "cartesia")):
        ids[role] = str(await pool.fetchval(
            "INSERT INTO provider_configs (tenant_id, name, role, engine, voice, api_key_ref) "
            "VALUES ($1, $2, $3, $4, 'voice-1', 'ref') RETURNING id",
            tenant_id, f"{role} cfg", role, engine,
        ))
    return ids


async def _purge_tenant(pool, tenant: dict) -> None:
    await pool.execute(
        "DELETE FROM transcript_entries WHERE session_id IN "
        "(SELECT session_id FROM calls WHERE tenant_id = $1)", tenant["slug"],
    )
    await pool.execute("DELETE FROM calls WHERE tenant_id = $1", tenant["slug"])
    redis = cache.get_client()
    async for key in redis.scan_iter(match="testcred:*"):
        raw = await redis.get(key)
        if raw and json.loads(raw)["tenant_slug"] == tenant["slug"]:
            await redis.delete(key)
    await redis.delete(f"tenant:{tenant['slug']}")


@pytest_asyncio.fixture(loop_scope="session")
async def configs(pool, test_tenant):
    return await _insert_configs(pool, test_tenant["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def tenant_b(pool):
    slug = f"test-b-{uuid.uuid4().hex[:8]}"
    row = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"Tenant B {slug}", slug,
    )
    tenant = dict(row)
    admin = await users_service.create_user(
        email=f"test-b-admin-{uuid.uuid4().hex[:8]}@example.com", password=SECRET_PASSWORD,
        role="admin", tenant_id=tenant["id"],
    )
    configs_b = await _insert_configs(pool, tenant["id"])
    yield {"tenant": tenant, "admin": admin, "token": auth.create_access_token(admin),
           "configs": configs_b}
    await _purge_tenant(pool, tenant)
    await pool.execute("DELETE FROM agent_tool_policies WHERE agent_id IN "
                       "(SELECT id FROM agents WHERE tenant_id = $1)", tenant["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("UPDATE users SET deleted_at = now(), tenant_id = NULL WHERE id = $1",
                       admin["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def client(pool, test_tenant, test_admin):
    async with _client_for(test_admin["token"]) as c:
        yield c
    await _purge_tenant(pool, test_tenant)


@pytest_asyncio.fixture(loop_scope="session")
async def client_b(tenant_b):
    async with _client_for(tenant_b["token"]) as c:
        yield c


def _url(tenant: dict, suffix: str = "") -> str:
    return f"/tenants/{tenant['slug']}/agents{suffix}"


def _template_body(template_id: str, configs: dict, **overrides) -> dict:
    body = {
        "template_id": template_id, "template_version": 2, "name": "Front Desk",
        "business_name": "Acme Dental", "business_facts": "Open 9 to 5.",
        "llm_config_id": configs["llm"],
    }
    if template_id != "faq-support":
        body |= {"stt_config_id": configs["stt"], "tts_config_id": configs["tts"]}
    return body | overrides


async def _create_agent(client, tenant, configs, template_id="faq-support", **overrides) -> dict:
    resp = await client.post(
        _url(tenant, "/from-template"), json=_template_body(template_id, configs, **overrides),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _chat_session(client, tenant, agent) -> dict:
    resp = await client.post(
        _url(tenant, f"/{agent['id']}/test-sessions"), json={"channel": "chat"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _agent_row(pool, agent_id) -> dict:
    return dict(await pool.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id))


async def _seed_test_call(pool, *, tenant_slug, agent_id, caller="hi there") -> str:
    session_id = f"test-sess-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, agent_id, turn_count, ended_at) "
        "VALUES ($1, $2, 'test', $3, 1, now())", session_id, tenant_slug, agent_id,
    )
    await pool.execute(
        "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response) "
        "VALUES ($1, 1, $2, 'how can I help?')", session_id, caller,
    )
    return session_id


def _revise(client, tenant, agent_id, session_id, problem="It was too wordy.", **extra):
    return client.post(
        _url(tenant, f"/{agent_id}/prompt/revise"),
        json={"session_id": session_id, "problem": problem, **extra},
    )


def _accept(client, tenant, agent_id, session_id, proposed, base_prompt, problem="It was too wordy."):
    return client.post(
        _url(tenant, f"/{agent_id}/prompt/accept"),
        json={"session_id": session_id, "problem": problem, "proposed_prompt": proposed,
              "base_prompt_sha256": _sha(base_prompt)},
    )


# ── 1. isolation (criteria 47, 48) ───────────────────────────────────────

class TestIsolation:
    async def test_revise_and_accept_never_read_another_tenants_session(
        self, pool, client, test_tenant, tenant_b, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        # The calls row names tenant A's agent but tenant B's slug: only the tenant predicate
        # on the calls row separates it from a session A owns.
        foreign_session = await _seed_test_call(
            pool, tenant_slug=tenant_b["tenant"]["slug"], agent_id=agent["id"],
        )
        own_session = await _seed_test_call(
            pool, tenant_slug=test_tenant["slug"], agent_id=agent["id"],
        )
        random_session = f"test-sess-{uuid.uuid4().hex[:8]}"
        proposed = _extend(agent["system_prompt"], "Offer a callback.")

        foreign = await _revise(client, test_tenant, agent["id"], foreign_session)
        random_ = await _revise(client, test_tenant, agent["id"], random_session)
        assert foreign.status_code == 404
        assert _shape(foreign) == _shape(random_)
        assert model.calls == []

        accept_foreign = await _accept(
            client, test_tenant, agent["id"], foreign_session, proposed, agent["system_prompt"])
        accept_random = await _accept(
            client, test_tenant, agent["id"], random_session, proposed, agent["system_prompt"])
        assert accept_foreign.status_code == 404
        assert _shape(accept_foreign) == _shape(accept_random)
        assert (await _agent_row(pool, agent["id"]))["system_prompt"] == agent["system_prompt"]

        # Control: the same request against a session the tenant owns gets past the 404.
        model.reply = proposed
        own = await _revise(client, test_tenant, agent["id"], own_session)
        assert own.status_code == 200, own.text
        assert len(model.calls) == 1

    async def test_foreign_llm_config_id_matches_a_random_one_on_generate_and_revise(
        self, pool, client, test_tenant, tenant_b, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        session = await _chat_session(client, test_tenant, agent)

        async def generate(config_id):
            return await client.post(
                _url(test_tenant, "/generate-system-prompt"),
                json={"name": "X", "llm_config_id": config_id},
            )

        revise = lambda config_id: _revise(  # noqa: E731
            client, test_tenant, agent["id"], session["session_id"], llm_config_id=config_id)

        for call in (generate, revise):
            foreign = await call(tenant_b["configs"]["llm"])
            random_ = await call(str(uuid.uuid4()))
            assert foreign.status_code == 404, call
            assert _shape(foreign) == _shape(random_), call
        assert model.calls == []

    @pytest.mark.parametrize("role", ["stt", "llm", "tts"])
    async def test_foreign_provider_id_matches_a_random_one_and_changes_nothing(
        self, pool, client, test_tenant, tenant_b, configs, model, role,
    ):
        agent = await _create_agent(client, test_tenant, configs, "inbound-triage")
        before = await _agent_row(pool, agent["id"])
        field = f"{role}_config_id"
        count_sql = "SELECT count(*) FROM agents WHERE tenant_id = $1"
        count_before = await pool.fetchval(count_sql, test_tenant["id"])

        def attempts(config_id):
            return {
                "from-template": client.post(
                    _url(test_tenant, "/from-template"),
                    json=_template_body("inbound-triage", configs, name="Other", **{field: config_id}),
                ),
                "create": client.post(
                    _url(test_tenant), json={"slug": "other", "name": "Other", field: config_id}),
                "patch": client.patch(
                    _url(test_tenant, f"/{agent['id']}"), json={field: config_id}),
            }

        foreign = {k: await v for k, v in attempts(tenant_b["configs"][role]).items()}
        random_ = {k: await v for k, v in attempts(str(uuid.uuid4())).items()}
        for route in foreign:
            assert foreign[route].status_code == 400, route
            assert _shape(foreign[route]) == _shape(random_[route]), route
            assert foreign[route].json() == {"detail": f"{field} not found"}, route
        assert await pool.fetchval(count_sql, test_tenant["id"]) == count_before
        assert await _agent_row(pool, agent["id"]) == before

    async def test_chat_turn_with_a_foreign_or_dangling_llm_config_is_one_404(
        self, pool, client, test_tenant, tenant_b, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        session = await _chat_session(client, test_tenant, agent)
        body = {"credential": session["credential"], "session_id": session["session_id"],
                "message": "hello"}

        async def turn_with_llm(config_id):
            async with pool.acquire() as conn, conn.transaction():
                # Replica mode lets the dangling id past the FK, which the API would refuse.
                await conn.execute("SET LOCAL session_replication_role = replica")
                await conn.execute(
                    "UPDATE agents SET llm_config_id = $1 WHERE id = $2", config_id, agent["id"])
            return await client.post(_url(test_tenant, f"/{agent['id']}/test-chat"), json=body)

        foreign = await turn_with_llm(uuid.UUID(tenant_b["configs"]["llm"]))
        dangling = await turn_with_llm(uuid.uuid4())
        assert foreign.status_code == 404
        assert foreign.json() == {"detail": "provider_config not found"}
        assert _shape(foreign) == _shape(dangling)
        assert model.calls == []
        assert await pool.fetchval(
            "SELECT turn_count FROM calls WHERE session_id = $1", session["session_id"]) == 0

    async def test_foreign_agent_id_matches_a_random_one_on_every_new_agent_route(
        self, pool, client, client_b, test_tenant, tenant_b, configs, model,
    ):
        foreign_agent = await _create_agent(client_b, tenant_b["tenant"], tenant_b["configs"])
        foreign_session = await _chat_session(client_b, tenant_b["tenant"], foreign_agent)
        prompt = foreign_agent["system_prompt"]
        bodies = {
            "test-sessions": {"channel": "chat"},
            "test-chat": {"credential": foreign_session["credential"],
                          "session_id": foreign_session["session_id"], "message": "hi"},
            "prompt/revise": {"session_id": foreign_session["session_id"], "problem": "bad"},
            "prompt/accept": {"session_id": foreign_session["session_id"], "problem": "bad",
                              "proposed_prompt": _extend(prompt, "One more."),
                              "base_prompt_sha256": _sha(prompt)},
            "prompt/undo": None,
        }
        outcomes = {}
        for suffix, body in bodies.items():
            foreign = await client.post(
                _url(test_tenant, f"/{foreign_agent['id']}/{suffix}"), json=body)
            random_ = await client.post(
                _url(test_tenant, f"/{uuid.uuid4()}/{suffix}"), json=body)
            outcomes[suffix] = (foreign.status_code, _shape(foreign) == _shape(random_))
        assert outcomes == {suffix: (404, True) for suffix in bodies}
        assert model.calls == []
        assert (await _agent_row(pool, foreign_agent["id"]))["system_prompt"] == prompt
        assert await pool.fetchval(
            "SELECT count(*) FROM calls WHERE session_id = $1 AND turn_count > 0",
            foreign_session["session_id"]) == 0

    async def test_a_tenant_b_credential_is_a_missing_session_on_tenant_a(
        self, pool, client, client_b, test_tenant, tenant_b, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        own = await _chat_session(client, test_tenant, agent)
        agent_b = await _create_agent(client_b, tenant_b["tenant"], tenant_b["configs"])
        session_b = await _chat_session(client_b, tenant_b["tenant"], agent_b)

        def turn(credential, session_id):
            return client.post(
                _url(test_tenant, f"/{agent['id']}/test-chat"),
                json={"credential": credential, "session_id": session_id, "message": "hi"})

        missing = await turn("nope", own["session_id"])
        for credential, session_id in (
            (session_b["credential"], own["session_id"]),
            (session_b["credential"], session_b["session_id"]),
            (own["credential"], session_b["session_id"]),
        ):
            resp = await turn(credential, session_id)
            assert resp.status_code == 404
            assert _shape(resp) == _shape(missing)
        assert model.calls == []


# ── from-template, test-sessions, catalog ────────────────────────────────

class TestFromTemplate:
    async def test_creates_an_inactive_templated_agent_with_rendered_text(
        self, pool, client, test_tenant, configs,
    ):
        agent = await _create_agent(client, test_tenant, configs, "inbound-triage")
        row = await _agent_row(pool, agent["id"])
        assert row["status"] == "inactive"
        assert (row["template_id"], row["template_version"]) == ("inbound-triage", 2)
        assert row["slug"] == "front-desk"
        assert row["tenant_id"] == test_tenant["id"]
        assert "Acme Dental" in row["greeting"] + row["system_prompt"]
        assert "Open 9 to 5." in row["system_prompt"]
        assert str(row["stt_config_id"]) == configs["stt"]
        assert agent["can_undo"] is False and agent["prompt_fixable"] is True
        assert not set(SLOT_KEYS) & set(agent)

    @pytest.mark.parametrize(("overrides", "why"), [
        ({"template_id": "nope"}, "unknown template"),
        ({"template_version": 3}, "version that is not the shipped one"),
        ({"stt_config_id": None}, "a needed role is null"),
        ({"llm_config_id": None}, "llm is null"),
        ({"name": "!!!"}, "a name with no letter or digit"),
    ])
    async def test_400_creates_nothing(self, pool, client, test_tenant, configs, overrides, why):
        resp = await client.post(
            _url(test_tenant, "/from-template"),
            json=_template_body("inbound-triage", configs) | overrides,
        )
        assert resp.status_code == 400, why
        assert await pool.fetchval(
            "SELECT count(*) FROM agents WHERE tenant_id = $1", test_tenant["id"]) == 0

    @pytest.mark.parametrize("role", ["stt_config_id", "tts_config_id"])
    async def test_a_chat_job_refuses_voice_configs(
        self, pool, client, test_tenant, configs, role,
    ):
        resp = await client.post(
            _url(test_tenant, "/from-template"),
            json=_template_body("faq-support", configs, **{role: configs[role[:3]]}),
        )
        assert resp.status_code == 400
        assert await pool.fetchval(
            "SELECT count(*) FROM agents WHERE tenant_id = $1", test_tenant["id"]) == 0

    @pytest.mark.parametrize("field", ["name", "business_name", "business_facts"])
    @pytest.mark.parametrize("token", ["{{secret}}", "{{", "}}"])
    async def test_double_braces_are_refused(
        self, pool, client, test_tenant, configs, field, token,
    ):
        resp = await client.post(
            _url(test_tenant, "/from-template"),
            json=_template_body("faq-support", configs, **{field: f"A {token} B"}),
        )
        assert resp.status_code == 422
        assert "double curly brackets are not allowed" in resp.text
        assert await pool.fetchval(
            "SELECT count(*) FROM agents WHERE tenant_id = $1", test_tenant["id"]) == 0

    async def test_an_advanced_create_stays_active_with_no_template(
        self, pool, client, test_tenant, configs,
    ):
        resp = await client.post(
            _url(test_tenant),
            json={"slug": "adv", "name": "Adv", "llm_config_id": configs["llm"]},
        )
        assert resp.status_code == 201, resp.text
        row = await _agent_row(pool, resp.json()["id"])
        assert (row["status"], row["template_id"], row["template_version"]) == ("active", None, None)

    async def test_a_superadmin_acts_only_inside_the_path_tenant(
        self, pool, test_tenant, tenant_b, configs, test_superadmin,
    ):
        async with _client_for(test_superadmin["token"]) as su:
            agent = await _create_agent(su, test_tenant, configs)
            assert (await _agent_row(pool, agent["id"]))["tenant_id"] == test_tenant["id"]
            # Tenant A's agent id named under tenant B's path is a missing agent.
            other = await su.post(
                _url(tenant_b["tenant"], f"/{agent['id']}/test-sessions"), json={"channel": "chat"})
            missing = await su.post(
                _url(tenant_b["tenant"], f"/{uuid.uuid4()}/test-sessions"), json={"channel": "chat"})
            assert (other.status_code, _shape(other)) == (404, _shape(missing))
            # Tenant B's config named under tenant A's path creates nothing.
            before = await pool.fetchval(
                "SELECT count(*) FROM agents WHERE tenant_id = $1", test_tenant["id"])
            foreign = await su.post(
                _url(test_tenant, "/from-template"),
                json=_template_body("faq-support", configs, name="Other",
                                    llm_config_id=tenant_b["configs"]["llm"]))
            assert foreign.status_code == 400
            # Per-caller invariance: the same caller, a random id, the same answer.
            random_id = await su.post(
                _url(test_tenant, "/from-template"),
                json=_template_body("faq-support", configs, name="Other",
                                    llm_config_id=str(uuid.uuid4())))
            assert _shape(foreign) == _shape(random_id)
            assert await pool.fetchval(
                "SELECT count(*) FROM agents WHERE tenant_id = $1", test_tenant["id"]) == before

    async def test_a_name_collision_is_the_existing_409(self, client, test_tenant, configs):
        await _create_agent(client, test_tenant, configs)
        resp = await client.post(
            _url(test_tenant, "/from-template"), json=_template_body("faq-support", configs))
        assert resp.status_code == 409
        assert resp.json() == {"detail": "That name or slug is already taken in this account."}


class TestTestSessions:
    async def test_chat_mint_shape_and_a_mismatched_channel(
        self, pool, client, test_tenant, configs,
    ):
        chat = await _create_agent(client, test_tenant, configs)
        session = await _chat_session(client, test_tenant, chat)
        assert set(session) == {"credential", "session_id", "greeting", "expires_in"}
        assert session["expires_in"] == 900
        assert session["greeting"] == chat["greeting"]
        wrong = await client.post(
            _url(test_tenant, f"/{chat['id']}/test-sessions"), json={"channel": "voice"})
        assert wrong.status_code == 400

    async def test_voice_mint_shape_and_a_mismatched_channel(self, client, test_tenant, configs):
        phone = await _create_agent(client, test_tenant, configs, "inbound-triage")
        resp = await client.post(
            _url(test_tenant, f"/{phone['id']}/test-sessions"), json={"channel": "voice"})
        assert resp.status_code == 200
        assert set(resp.json()) == {"credential", "expires_in"}
        assert resp.json()["expires_in"] == 60
        wrong = await client.post(
            _url(test_tenant, f"/{phone['id']}/test-sessions"), json={"channel": "chat"})
        assert wrong.status_code == 400

    async def test_an_agent_without_a_template_tests_by_voice(self, client, test_tenant, configs):
        created = await client.post(
            _url(test_tenant), json={"slug": "plain", "name": "Plain", "system_prompt": "Hi"})
        resp = await client.post(
            _url(test_tenant, f"/{created.json()['id']}/test-sessions"), json={"channel": "voice"})
        assert resp.status_code == 200

    async def test_the_catalog_lists_the_shipped_jobs(self, client):
        resp = await client.get("/agent-templates")
        assert resp.status_code == 200
        jobs = resp.json()
        assert {j["id"] for j in jobs} >= {"faq-support", "inbound-triage", "payment-reminder"}
        assert set(jobs[0]) == {
            "id", "version", "channel", "label", "blurb", "does", "wont_do", "handoff", "needs"}


# ── 2. accept and undo (criteria 38, 40, 42-44) ──────────────────────────

class TestAcceptAndUndo:
    async def _agent_and_session(self, pool, client, test_tenant, configs):
        agent = await _create_agent(client, test_tenant, configs)
        session = await _chat_session(client, test_tenant, agent)
        return agent, session["session_id"]

    async def test_two_concurrent_accepts_on_one_base_have_one_winner(
        self, pool, client, test_tenant, configs,
    ):
        agent, session = await self._agent_and_session(pool, client, test_tenant, configs)
        base = agent["system_prompt"]
        first, second = _extend(base, "Offer a callback."), _extend(base, "Offer an email.")
        responses = await asyncio.gather(
            _accept(client, test_tenant, agent["id"], session, first, base),
            _accept(client, test_tenant, agent["id"], session, second, base),
        )
        assert sorted(r.status_code for r in responses) == [200, 409]
        winner = next(p for p, r in zip((first, second), responses) if r.status_code == 200)
        row = await _agent_row(pool, agent["id"])
        assert row["system_prompt"] == winner
        assert row["prompt_undo_previous"] == base
        assert row["prompt_undo_accepted_sha256"] == _sha(winner)

    async def test_accept_accept_undo_restores_the_middle_prompt_and_a_second_undo_is_409(
        self, pool, client, test_tenant, configs,
    ):
        agent, session = await self._agent_and_session(pool, client, test_tenant, configs)
        p0 = agent["system_prompt"]
        p1, p2 = _extend(p0, "Offer a callback."), None
        p2 = _extend(p1, "Offer an email.")
        assert (await _accept(client, test_tenant, agent["id"], session, p1, p0)).status_code == 200
        accepted = await _accept(client, test_tenant, agent["id"], session, p2, p1)
        assert accepted.status_code == 200
        body = accepted.json()
        assert body["system_prompt"] == p2 and body["can_undo"] is True
        assert not set(SLOT_KEYS) & set(body)

        undone = await client.post(_url(test_tenant, f"/{agent['id']}/prompt/undo"))
        assert undone.status_code == 200
        assert undone.json()["system_prompt"] == p1 and undone.json()["can_undo"] is False
        assert not set(SLOT_KEYS) & set(undone.json())
        row = await _agent_row(pool, agent["id"])
        assert row["system_prompt"] == p1
        assert row["prompt_undo_previous"] is None and row["prompt_undo_accepted_sha256"] is None

        again = await client.post(_url(test_tenant, f"/{agent['id']}/prompt/undo"))
        assert again.status_code == 409
        assert (await _agent_row(pool, agent["id"]))["system_prompt"] == p1

    async def test_no_test_revise_accept_or_undo_step_ever_activates_the_agent(
        self, pool, client, test_tenant, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        assert (await _agent_row(pool, agent["id"]))["status"] == "inactive"

        async def status() -> str:
            return (await _agent_row(pool, agent["id"]))["status"]

        session = await _chat_session(client, test_tenant, agent)
        assert await status() == "inactive"  # test-sessions
        turn = await client.post(
            _url(test_tenant, f"/{agent['id']}/test-chat"),
            json={"credential": session["credential"], "session_id": session["session_id"],
                  "message": "hi"})
        assert turn.status_code == 200, turn.text
        assert await status() == "inactive"  # test-chat
        base = agent["system_prompt"]
        model.reply = _extend(base, "Offer a callback.")
        revised = await _revise(client, test_tenant, agent["id"], session["session_id"])
        assert revised.status_code == 200, revised.text
        assert await status() == "inactive"  # revise
        accepted = await _accept(
            client, test_tenant, agent["id"], session["session_id"], model.reply, base)
        assert accepted.status_code == 200, accepted.text
        assert await status() == "inactive"  # accept
        undone = await client.post(_url(test_tenant, f"/{agent['id']}/prompt/undo"))
        assert undone.status_code == 200, undone.text
        assert await status() == "inactive"  # undo

    async def test_a_hand_edit_after_accept_makes_undo_a_409_and_clears_can_undo(
        self, pool, client, test_tenant, configs,
    ):
        agent, session = await self._agent_and_session(pool, client, test_tenant, configs)
        p0 = agent["system_prompt"]
        accepted = await _accept(
            client, test_tenant, agent["id"], session, _extend(p0, "Offer a callback."), p0)
        assert accepted.json()["can_undo"] is True
        edited = await client.patch(
            _url(test_tenant, f"/{agent['id']}"), json={"system_prompt": "Edited by hand"})
        assert edited.status_code == 200
        assert edited.json()["can_undo"] is False

        undo = await client.post(_url(test_tenant, f"/{agent['id']}/prompt/undo"))
        assert undo.status_code == 409
        assert (await _agent_row(pool, agent["id"]))["system_prompt"] == "Edited by hand"
        fetched = await client.get(_url(test_tenant, f"/{agent['slug']}"))
        assert fetched.json()["can_undo"] is False
        assert not set(SLOT_KEYS) & set(fetched.json())

    async def test_a_stale_base_hash_is_a_409_and_changes_nothing(
        self, pool, client, test_tenant, configs,
    ):
        agent, session = await self._agent_and_session(pool, client, test_tenant, configs)
        base = agent["system_prompt"]
        resp = await client.post(
            _url(test_tenant, f"/{agent['id']}/prompt/accept"),
            json={"session_id": session, "problem": "bad", "base_prompt_sha256": _sha("other"),
                  "proposed_prompt": _extend(base, "Offer a callback.")})
        assert resp.status_code == 409
        row = await _agent_row(pool, agent["id"])
        assert row["system_prompt"] == base and row["prompt_undo_previous"] is None

    @pytest.mark.parametrize(("make", "why"), [
        (lambda base: base.replace("Guardrails", "Rules", 1), "a heading is missing"),
        (lambda base: _extend(base, "Say {{ caller_number }}."), "it adds template braces"),
        (lambda base: _extend(base, "Call back on +1 555 123 4567."), "it holds a phone number"),
    ])
    async def test_an_unacceptable_proposal_is_a_400(
        self, pool, client, test_tenant, configs, make, why,
    ):
        agent, session = await self._agent_and_session(pool, client, test_tenant, configs)
        base = agent["system_prompt"]
        resp = await _accept(client, test_tenant, agent["id"], session, make(base), base)
        assert resp.status_code == 400, why
        assert (await _agent_row(pool, agent["id"]))["system_prompt"] == base

    async def test_accept_for_a_soft_deleted_agent_is_a_404(
        self, pool, client, test_tenant, configs,
    ):
        agent, session = await self._agent_and_session(pool, client, test_tenant, configs)
        base = agent["system_prompt"]
        assert (await client.delete(_url(test_tenant, f"/{agent['id']}"))).status_code == 204
        resp = await _accept(
            client, test_tenant, agent["id"], session, _extend(base, "Offer a callback."), base)
        assert resp.status_code == 404
        undo = await client.post(_url(test_tenant, f"/{agent['id']}/prompt/undo"))
        assert undo.status_code == 404

    async def test_the_accept_exemption_follows_the_problem_text(
        self, pool, client, test_tenant, configs, model,
    ):
        agent, session = await self._agent_and_session(pool, client, test_tenant, configs)
        base = agent["system_prompt"]
        problem = "It could not find order 1234567."
        proposed = _extend(base, "Look up order 1234567 first.")
        model.reply = proposed

        revised = await _revise(client, test_tenant, agent["id"], session, problem)
        assert revised.status_code == 200, revised.text
        assert revised.json()["after"] == proposed

        different = await _accept(
            client, test_tenant, agent["id"], session, proposed, base, problem="It was too wordy.")
        assert different.status_code == 400
        assert (await _agent_row(pool, agent["id"]))["system_prompt"] == base
        same = await _accept(client, test_tenant, agent["id"], session, proposed, base, problem)
        assert same.status_code == 200
        assert (await _agent_row(pool, agent["id"]))["system_prompt"] == proposed


# ── revise: order, shapes and error tokens ───────────────────────────────

class TestRevise:
    async def test_returns_before_after_and_hash_and_persists_nothing(
        self, pool, client, test_tenant, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        session = await _chat_session(client, test_tenant, agent)
        base = agent["system_prompt"]
        model.reply = _extend(base, "Offer a callback.")
        row_before = await _agent_row(pool, agent["id"])

        resp = await _revise(client, test_tenant, agent["id"], session["session_id"])
        assert resp.status_code == 200
        assert resp.json() == {
            "before": base, "after": model.reply, "base_prompt_sha256": _sha(base)}
        assert await _agent_row(pool, agent["id"]) == row_before

    async def test_an_agent_made_from_an_older_template_version_keeps_its_channel(
        self, pool, client, test_tenant, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        session = await _chat_session(client, test_tenant, agent)
        await pool.execute("UPDATE agents SET template_version = 1 WHERE id = $1", agent["id"])
        model.reply = _extend(agent["system_prompt"], "Offer a callback.")

        resp = await _revise(client, test_tenant, agent["id"], session["session_id"])
        assert resp.status_code == 200, resp.text
        system = model.calls[-1][1]
        assert sp.HUMAN_SPEECH_CHAT in system
        assert sp.HUMAN_SPEECH_VOICE not in system

    async def test_a_hand_edited_prompt_is_422_before_the_session_or_model_is_touched(
        self, client, test_tenant, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        await client.patch(_url(test_tenant, f"/{agent['id']}"), json={"system_prompt": "By hand"})
        resp = await _revise(client, test_tenant, agent["id"], "no-such-session")
        assert (resp.status_code, resp.json()) == (422, {"detail": "prompt_not_fixable"})
        assert model.calls == []

    async def test_the_hand_edit_422_is_not_throttled(self, client, test_tenant, configs, model):
        agent = await _create_agent(client, test_tenant, configs)
        await client.patch(_url(test_tenant, f"/{agent['id']}"), json={"system_prompt": "By hand"})
        statuses = {
            (await _revise(client, test_tenant, agent["id"], "s")).status_code for _ in range(25)}
        assert statuses == {422}

    async def test_the_model_error_tokens(self, pool, client, test_tenant, configs, model):
        agent = await _create_agent(client, test_tenant, configs)
        session = await _chat_session(client, test_tenant, agent)
        base = agent["system_prompt"]
        turn = await client.post(
            _url(test_tenant, f"/{agent['id']}/test-chat"),
            json={"credential": session["credential"], "session_id": session["session_id"],
                  "message": "my number is 5551234567"})
        assert turn.status_code == 200

        async def revise_with(reply=None, error=None):
            model.reply, model.error = reply, error
            resp = await _revise(client, test_tenant, agent["id"], session["session_id"])
            return resp.status_code, resp.json()

        assert await revise_with(reply="no headings at all") == (422, {"detail": "unusable_output"})
        assert await revise_with(reply=_extend(base, "Call 5551234567.")) == (
            422, {"detail": "customer_data"})
        assert await revise_with(reply=_extend(base, "Use {{ x }}.")) == (
            422, {"detail": "unusable_output"})
        assert await revise_with(error=ValueError("OpenAI returned 500")) == (
            502, {"detail": "ai_unavailable"})
        assert await revise_with(error=httpx.ConnectError("boom")) == (
            502, {"detail": "ai_unavailable"})
        assert (await _agent_row(pool, agent["id"]))["system_prompt"] == base

    async def test_the_throttle_counts_after_the_structure_check_and_caps_at_twenty_an_hour(
        self, client, test_tenant, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        statuses = [
            (await _revise(client, test_tenant, agent["id"], "no-such-session")).status_code
            for _ in range(21)
        ]
        assert statuses == [404] * 20 + [429]
        limited = await _revise(client, test_tenant, agent["id"], "no-such-session")
        assert limited.headers["Retry-After"].isdigit()


# ── 3. freshness (finding 1) ─────────────────────────────────────────────

class TestFreshness:
    async def test_accept_and_activate_reach_conversations_config_provider(
        self, pool, client, test_tenant, configs, model,
    ):
        service = await users_service.create_user(
            email=f"test-svc-{uuid.uuid4().hex[:8]}@example.com", password=SECRET_PASSWORD,
            role="viewer",
        )
        await pool.execute("UPDATE users SET is_service_account = true WHERE id = $1", service["id"])
        http_repo = HttpConfigRepository(
            "http://test", service["email"], SECRET_PASSWORD, transport=ASGITransport(app=app))
        redis_repo = RedisConfigRepository("redis://localhost:6379/0")
        provider = CacheAsideConfigProvider(redis_repo, http_repo)
        try:
            agent = await _create_agent(client, test_tenant, configs, "inbound-triage")
            assert agent["status"] == "inactive"
            base = agent["system_prompt"]
            slug = test_tenant["slug"]

            assert await provider.get_runtime_config(slug, agent["slug"]) is None
            warm = await provider.get_runtime_config(slug, agent["slug"], include_inactive=True)
            assert warm.conversation.system_prompt == base  # the cache now holds the old prompt

            session_id = await _seed_test_call(
                pool, tenant_slug=slug, agent_id=agent["id"])
            proposed = _extend(base, "Offer a callback.")
            accepted = await _accept(client, test_tenant, agent["id"], session_id, proposed, base)
            assert accepted.status_code == 200
            fresh = await provider.get_runtime_config(slug, agent["slug"], include_inactive=True)
            assert fresh.conversation.system_prompt == proposed

            undone = await client.post(_url(test_tenant, f"/{agent['id']}/prompt/undo"))
            assert undone.status_code == 200
            back = await provider.get_runtime_config(slug, agent["slug"], include_inactive=True)
            assert back.conversation.system_prompt == base

            assert await provider.get_runtime_config(slug, agent["slug"]) is None
            activated = await client.patch(
                _url(test_tenant, f"/{agent['id']}"), json={"status": "active"})
            assert activated.status_code == 200
            live = await provider.get_runtime_config(slug, agent["slug"])
            assert live is not None and live.agent.status == "active"
        finally:
            await http_repo.close()
            await redis_repo.close()
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", service["id"])


# ── 4. chat test path (criteria 26, 28, finding 3) ───────────────────────

class TestChatPath:
    async def test_one_turn_writes_a_test_call_with_turns_zero_and_one(
        self, pool, client, test_tenant, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        session = await _chat_session(client, test_tenant, agent)
        model.reply = "We open at nine."
        resp = await client.post(
            _url(test_tenant, f"/{agent['id']}/test-chat"),
            json={"credential": session["credential"], "session_id": session["session_id"],
                  "message": "When do you open?"})
        assert resp.status_code == 200 and resp.json() == {"reply": "We open at nine."}

        call = await pool.fetchrow("SELECT * FROM calls WHERE session_id = $1", session["session_id"])
        assert (call["direction"], call["tenant_id"], call["agent_id"]) == (
            "test", test_tenant["slug"], uuid.UUID(agent["id"]))
        rows = await pool.fetch(
            "SELECT turn_number, caller_text, ai_response FROM transcript_entries "
            "WHERE session_id = $1 ORDER BY turn_number", session["session_id"])
        assert [(r["turn_number"], r["caller_text"], r["ai_response"]) for r in rows] == [
            (0, None, agent["greeting"]), (1, "When do you open?", "We open at nine.")]

    async def test_expired_voice_and_foreign_credentials_all_match_a_missing_session(
        self, pool, client, test_tenant, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        session = await _chat_session(client, test_tenant, agent)

        def turn(credential, session_id):
            return client.post(
                _url(test_tenant, f"/{agent['id']}/test-chat"),
                json={"credential": credential, "session_id": session_id, "message": "hi"})

        missing = await turn("not-a-credential", session["session_id"])
        assert missing.status_code == 404 and missing.json() == {"detail": "test session not found"}

        # A real 900 s credential, expired by PEXPIRE rather than by shrinking the constant.
        redis = cache.get_client()
        assert 890_000 < await redis.pttl(_key(session["credential"])) <= 900_000
        await redis.pexpire(_key(session["credential"]), 1)
        await asyncio.sleep(0.05)
        expired = await turn(session["credential"], session["session_id"])
        assert _shape(expired) == _shape(missing)

        fresh = await _chat_session(client, test_tenant, agent)
        voice = await agent_testing.mint_test_session(
            redis, tenant=test_tenant, agent=agent, channel="voice")
        wrong_channel = await turn(voice["credential"], fresh["session_id"])
        assert _shape(wrong_channel) == _shape(missing)
        assert await redis.exists(_key(voice["credential"])) == 1  # a refused redeem never consumes
        assert model.calls == []
        ok = await turn(fresh["credential"], fresh["session_id"])
        assert ok.status_code == 200

    async def test_turn_forty_one_is_429_after_forty_real_turns(
        self, pool, client, test_tenant, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        session = await _chat_session(client, test_tenant, agent)
        body = {"credential": session["credential"], "session_id": session["session_id"],
                "message": "hi"}
        url = _url(test_tenant, f"/{agent['id']}/test-chat")
        for _ in range(40):
            assert (await client.post(url, json=body)).status_code == 200
        over = await client.post(url, json=body)
        assert over.status_code == 429
        assert len(model.calls) == 40
        assert await pool.fetchval(
            "SELECT turn_count FROM calls WHERE session_id = $1", session["session_id"]) == 40

    async def test_two_turns_in_flight_at_thirty_nine_get_one_winner_and_unique_numbers(
        self, pool, client, test_tenant, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        session = await _chat_session(client, test_tenant, agent)
        await pool.execute(
            "UPDATE calls SET turn_count = 39 WHERE session_id = $1", session["session_id"])
        body = {"credential": session["credential"], "session_id": session["session_id"],
                "message": "hi"}
        url = _url(test_tenant, f"/{agent['id']}/test-chat")
        model.gate = asyncio.Event()
        tasks = [asyncio.create_task(client.post(url, json=body)) for _ in range(2)]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        assert [t.result().status_code for t in done] == [429]  # the loser never reached the model
        model.gate.set()
        results = await asyncio.gather(*tasks)
        assert sorted(r.status_code for r in results) == [200, 429]
        numbers = [r["turn_number"] for r in await pool.fetch(
            "SELECT turn_number FROM transcript_entries WHERE session_id = $1",
            session["session_id"])]
        assert len(numbers) == len(set(numbers))
        assert 40 in numbers

    async def test_a_chat_test_leaves_no_live_call_and_does_not_block_delete(
        self, pool, client, test_tenant, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        session = await _chat_session(client, test_tenant, agent)
        await client.post(
            _url(test_tenant, f"/{agent['id']}/test-chat"),
            json={"credential": session["credential"], "session_id": session["session_id"],
                  "message": "hi"})
        live_id = f"test-live-{uuid.uuid4().hex[:8]}"
        await pool.execute(
            "INSERT INTO calls (session_id, tenant_id, direction) VALUES ($1, $2, 'inbound')",
            live_id, test_tenant["slug"])
        assert await pool.fetchval(
            "SELECT ended_at FROM calls WHERE session_id = $1", session["session_id"]) is not None

        listed = await client.get("/live-calls", params={"tenant_slug": test_tenant["slug"]})
        assert listed.status_code == 200, listed.text
        ids = [item["session_id"] for item in listed.json()["items"]]
        assert live_id in ids and session["session_id"] not in ids  # the control row is listed

        assert (await client.delete(_url(test_tenant, f"/{agent['id']}"))).status_code == 204
        await pool.execute("DELETE FROM calls WHERE session_id = $1", live_id)

    async def test_the_mint_and_turn_throttles_cap_at_twenty_and_sixty_a_minute(
        self, client, test_tenant, configs, model,
    ):
        agent = await _create_agent(client, test_tenant, configs)
        mints = [
            (await client.post(
                _url(test_tenant, f"/{agent['id']}/test-sessions"), json={"channel": "chat"}
            )).status_code for _ in range(21)]
        assert mints == [200] * 20 + [429]

        body = {"credential": "x", "session_id": "y", "message": "hi"}
        url = _url(test_tenant, f"/{agent['id']}/test-chat")
        turns = [(await client.post(url, json=body)).status_code for _ in range(61)]
        assert turns == [404] * 60 + [429]


# ── 6. the undo slot never reaches history (lesson 33) ───────────────────

class TestSlotNeverReachesHistory:
    async def _history(self, pool, agent_id) -> list[str]:
        audit_rows = await pool.fetch(
            "SELECT old_value::text AS a, new_value::text AS b FROM audit_log WHERE entity_id = $1",
            agent_id)
        versions = await pool.fetch(
            "SELECT graph::text AS g FROM agent_workflow_versions WHERE agent_id = $1", agent_id)
        return [r["a"] or "" for r in audit_rows] + [r["b"] or "" for r in audit_rows] + [
            r["g"] for r in versions]

    async def _assert_clean(self, pool, agent_id, hashes):
        texts = await self._history(pool, agent_id)
        assert len(texts) >= 2  # the sweep is not vacuous
        for text in texts:
            for key in SLOT_KEYS:
                assert key not in text
            for digest in hashes:
                assert digest not in text

    async def test_audit_and_workflow_versions_never_hold_the_slot(
        self, pool, client, test_tenant, configs,
    ):
        agent = await _create_agent(client, test_tenant, configs)  # starter graph has a global node
        session = (await _chat_session(client, test_tenant, agent))["session_id"]
        p0 = _extend(agent["system_prompt"], "Greet warmly.")
        p1, p2 = _extend(p0, "Offer a callback."), _extend(p0, "Offer an email.")
        hashes = [_sha(p1), _sha(p2)]
        patched = await client.patch(
            _url(test_tenant, f"/{agent['id']}"), json={"system_prompt": p0})
        assert patched.status_code == 200
        await self._assert_clean(pool, agent["id"], hashes)

        assert (await _accept(client, test_tenant, agent["id"], session, p1, p0)).status_code == 200
        await self._assert_clean(pool, agent["id"], hashes)
        assert (await _accept(client, test_tenant, agent["id"], session, p2, p1)).status_code == 200
        await self._assert_clean(pool, agent["id"], hashes)

        # A hand edit while the slot is full runs update_agent's mirrored-graph audit branch.
        assert (await client.patch(
            _url(test_tenant, f"/{agent['id']}"), json={"system_prompt": p1}
        )).status_code == 200
        await self._assert_clean(pool, agent["id"], hashes)

        assert (await _accept(client, test_tenant, agent["id"], session, p2, p1)).status_code == 200
        assert (await client.post(_url(test_tenant, f"/{agent['id']}/prompt/undo"))).status_code == 200
        await self._assert_clean(pool, agent["id"], hashes)
        assert (await _agent_row(pool, agent["id"]))["system_prompt"] == p1


# ── 7. roles, guards and logging (criteria 46, 50, 53) ───────────────────

def _api_routes(routes):
    """app.include_router nests a router's routes behind .original_router."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _api_routes(route.original_router.routes)


def _new_route_urls(tenant_slug: str) -> list[tuple[str, str]]:
    found = []
    for route in _api_routes(app.routes):
        for method in route.methods:
            if (method, route.path) in NEW_ROUTES:
                found.append((method, route.path.format(
                    tenant_slug=tenant_slug, agent_id=str(uuid.uuid4()))))
    return found


class TestRolesAndGuards:
    async def test_every_new_route_is_401_unauthenticated_and_403_for_a_viewer(
        self, test_tenant, test_viewer,
    ):
        urls = _new_route_urls(test_tenant["slug"])
        assert len(urls) == len(NEW_ROUTES)  # none was renamed or mounted twice
        async with _client_for() as anon, _client_for(test_viewer["token"]) as viewer:
            for method, url in urls:
                assert (await anon.request(method, url, json={})).status_code == 401, url
                assert (await viewer.request(method, url, json={})).status_code == 403, url

    def test_every_agents_router_route_runs_both_path_tenant_dependencies(self):
        names = {d.dependency.__name__ for d in agents_router.router.dependencies}
        assert names == {"bind_path_tenant", "require_path_tenant_access"}

    def test_every_guard_call_is_awaited(self):
        agents_src = inspect.getsource(agents_router)
        deps_src = inspect.getsource(deps)
        for source in (agents_src, deps_src):
            assert _unawaited_guard_calls(source) == []

        # Not vacuous: the sweep sees every guard call, and each new route awaits _resolve_tenant.
        assert _guard_call_count(agents_src) >= 12
        assert _guard_call_count(deps_src) >= 1
        awaited = _awaited_names_per_function(agents_src)
        for method, path in NEW_ROUTES - {("GET", "/agent-templates")}:
            endpoint = next(
                r.endpoint for r in _api_routes(app.routes)
                if r.path == path and method in r.methods)
            assert "_resolve_tenant" in awaited[endpoint.__name__], path

    def test_the_awaited_check_goes_red_on_an_unawaited_guard(self):
        bad = "async def f(u):\n    assert_tenant_access('t', u)\n"
        good = "async def f(u):\n    await assert_tenant_access('t', u)\n"
        assert _unawaited_guard_calls(bad) == ["assert_tenant_access"]
        assert _unawaited_guard_calls(good) == []
        undone = inspect.getsource(deps).replace(
            "await assert_tenant_access(tenant, current_user)",
            "assert_tenant_access(tenant, current_user)")
        assert undone != inspect.getsource(deps)
        assert _unawaited_guard_calls(undone) == ["assert_tenant_access"]


def _guard_names(tree: ast.AST) -> set[str]:
    return {"assert_tenant_access"} | {
        n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
        and not any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") in {
            "get", "post", "patch", "put", "delete"} for d in n.decorator_list)
    }


def _guard_calls(source: str) -> list[tuple[str, bool]]:
    """(name, awaited) for every call to an async helper defined in the module, or to
    assert_tenant_access. Route handlers are excluded: nothing calls them."""
    tree = ast.parse(source)
    names = _guard_names(tree)
    awaited_ids = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if name in names:
                out.append((name, id(node) in awaited_ids))
    return out


def _unawaited_guard_calls(source: str) -> list[str]:
    return [name for name, awaited in _guard_calls(source) if not awaited]


def _guard_call_count(source: str) -> int:
    return len(_guard_calls(source))


def _awaited_names_per_function(source: str) -> dict[str, set[str]]:
    out = {}
    for fn in ast.walk(ast.parse(source)):
        if isinstance(fn, ast.AsyncFunctionDef):
            out[fn.name] = {
                n.value.func.id for n in ast.walk(fn)
                if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Name)}
    return out


class TestNothingSensitiveIsLogged:
    async def test_no_log_record_holds_a_facts_problem_message_or_credential_sentinel(
        self, pool, client, test_tenant, configs, monkeypatch, caplog,
    ):
        sentinel = {k: f"SENTINEL-{k}-{uuid.uuid4().hex}" for k in ("facts", "problem", "message")}
        sentinel_prompt = f"{sentinel['facts']} and a problem"
        mode = {"ok": False}
        real_client = httpx.AsyncClient

        def vendor(request: httpx.Request) -> httpx.Response:
            # A vendor that echoes the whole request back in an error body, as real ones do.
            if mode["ok"]:
                return httpx.Response(200, json={"choices": [{"message": {"content": reply[0]}}]})
            return httpx.Response(500, text=request.content.decode())

        reply = [""]
        monkeypatch.setattr(
            sp.httpx, "AsyncClient",
            lambda **kw: real_client(**kw, transport=httpx.MockTransport(vendor)))
        caplog.set_level(logging.DEBUG)
        for name in ("httpx", "httpcore", "services", "libs"):
            logging.getLogger(name).setLevel(logging.DEBUG)

        agent = await _create_agent(
            client, test_tenant, configs, business_facts=sentinel_prompt)
        session = await _chat_session(client, test_tenant, agent)
        secrets = [sentinel["facts"], sentinel["problem"], sentinel["message"],
                   session["credential"]]
        base = agent["system_prompt"]
        assert sentinel["facts"] in base

        turn_body = {"credential": session["credential"], "session_id": session["session_id"],
                     "message": sentinel["message"]}
        failed_turn = await client.post(_url(test_tenant, f"/{agent['id']}/test-chat"), json=turn_body)
        failed_revise = await _revise(
            client, test_tenant, agent["id"], session["session_id"], sentinel["problem"])
        assert (failed_turn.status_code, failed_revise.status_code) == (502, 502)

        mode["ok"] = True
        reply[0] = "a fine reply"
        assert (await client.post(
            _url(test_tenant, f"/{agent['id']}/test-chat"), json=turn_body)).status_code == 200
        reply[0] = _extend(base, "Offer a callback.")
        revised = await _revise(
            client, test_tenant, agent["id"], session["session_id"], sentinel["problem"])
        assert revised.status_code == 200, revised.text
        accepted = await _accept(
            client, test_tenant, agent["id"], session["session_id"], reply[0], base,
            sentinel["problem"])
        assert accepted.status_code == 200

        assert caplog.records  # something was captured, so an empty log cannot pass
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "api.openai.com" in logged  # the vendor path itself was exercised and logged
        for secret in secrets:
            assert secret not in logged


# ── the Advanced wizard's client-side builder mirrors the prompt blocks ──

def _ts_block(source: str, name: str) -> str:
    """The lines of `const NAME = [ "...", ... ].join("\\n");`, as the string it builds."""
    match = re.search(rf"const {name} = \[\n(.*?)\n\]\.join\(\"\\n\"\);", source, re.S)
    assert match, name
    return "\n".join(json.loads(line.strip().removesuffix(",")) for line in match.group(1).splitlines())


def test_the_admin_ui_prompt_builder_mirrors_the_speech_and_guardrail_blocks():
    path = pathlib.Path(__file__).parents[3] / "admin-ui" / "lib" / "systemPromptBuilder.ts"
    source = path.read_text()
    assert _ts_block(source, "HUMAN_SPEECH_VOICE") == sp.HUMAN_SPEECH_VOICE
    assert _ts_block(source, "GUARDRAILS") == sp._GUARDRAILS
