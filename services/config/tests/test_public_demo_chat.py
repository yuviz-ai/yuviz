"""POST /public/demo-chat and /public/demo-chat/turn against real Postgres and Redis.
The only mock is the AI vendor call."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from services.config import agents
from services.config import system_prompt as sp
from services.config.app import AgentAssistThrottle, app
from services.config.routers import public_demo
from services.config.routers.public_demo import PUBLIC_CHAT_TURNS, PublicChatThrottle

SITE = "http://localhost:3002"


class _Resolver:
    async def resolve(self, ref):
        return "sk-test"


class _Model:
    def __init__(self):
        self.calls = 0

    async def __call__(self, api_key, model, messages, system, max_tokens):
        self.calls += 1
        return f"reply {self.calls}"


@pytest.fixture
def model(monkeypatch):
    m = _Model()
    monkeypatch.setitem(sp._CALLERS, "openai", m)
    monkeypatch.setattr(public_demo, "_secret_resolver", _Resolver())
    return m


@pytest_asyncio.fixture(loop_scope="session")
async def chat_agent(pool, test_tenant, scoped):
    tenant_id, slug = test_tenant["id"], test_tenant["slug"]
    llm_id = await pool.fetchval(
        "INSERT INTO provider_configs (tenant_id, name, role, engine, api_key_ref) "
        "VALUES ($1, 'Demo LLM', 'llm', 'openai', 'ref') RETURNING id", tenant_id,
    )
    agent = await agents.create_agent(tenant_id=tenant_id, tenant_slug=slug, slug="site-chat", name="Site",
                                      llm_config_id=str(llm_id))
    await agents.create_agent(tenant_id=tenant_id, tenant_slug=slug, slug="site-off", name="Off", status="inactive")
    yield agent
    # calls/transcript_entries FK-reference agents/calls; test_tenant's teardown deletes agents.
    await pool.execute(
        "DELETE FROM transcript_entries WHERE session_id IN (SELECT session_id FROM calls WHERE agent_id = $1)",
        agent["id"],
    )
    await pool.execute("DELETE FROM calls WHERE agent_id = $1", agent["id"])
    await pool.execute("UPDATE agents SET llm_config_id = NULL WHERE llm_config_id = $1", llm_id)
    await pool.execute("DELETE FROM provider_configs WHERE id = $1", llm_id)


@pytest.fixture
def demo_env(monkeypatch, test_tenant):
    def configure(agent_slug: str = "site-chat", origins: str = SITE, per_ip: str = "5"):
        monkeypatch.setenv("PUBLIC_DEMO_AGENT", f"{test_tenant['slug']}/{agent_slug}")
        monkeypatch.setenv("PUBLIC_DEMO_ORIGINS", origins)
        monkeypatch.setenv("PUBLIC_DEMO_CHAT_PER_IP_PER_HOUR", per_ip)
        monkeypatch.setattr(app.state, "public_chat_throttle", PublicChatThrottle())
        monkeypatch.setattr(app.state, "agent_assist_throttle", AgentAssistThrottle())
    configure()
    return configure


async def _post(path: str, body: dict | str | None = None, origin: str | None = SITE):
    headers = {"Content-Type": "text/plain"}
    if origin:
        headers["Origin"] = origin
    content = body if isinstance(body, str) else json.dumps(body) if body is not None else None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, headers=headers, content=content)


async def _open_chat():
    resp = await _post("/public/demo-chat")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_opening_a_chat_returns_a_session_with_cors(chat_agent, demo_env):
    resp = await _post("/public/demo-chat")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"credential", "session_id", "greeting", "expires_in", "max_turns"}
    assert body["max_turns"] == PUBLIC_CHAT_TURNS
    assert resp.headers["access-control-allow-origin"] == SITE


@pytest.mark.parametrize("path", ["/public/demo-chat", "/public/demo-chat/turn"])
@pytest.mark.parametrize("origin", [None, "https://evil.example"])
async def test_foreign_origin_is_refused_without_cors(chat_agent, demo_env, model, path, origin):
    resp = await _post(path, {"credential": "x", "session_id": "y", "message": "hi"}, origin=origin)

    assert resp.status_code == 403
    assert "access-control-allow-origin" not in resp.headers
    assert model.calls == 0


async def test_a_turn_returns_the_agents_live_reply(chat_agent, demo_env, model):
    chat = await _open_chat()

    resp = await _post("/public/demo-chat/turn", {**_keys(chat), "message": "What does Yuviz do?"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"reply": "reply 1"}
    assert resp.headers["access-control-allow-origin"] == SITE


async def test_the_session_stops_after_its_turn_cap(chat_agent, demo_env, model):
    chat = await _open_chat()
    for _ in range(PUBLIC_CHAT_TURNS):
        assert (await _post("/public/demo-chat/turn", {**_keys(chat), "message": "hi"})).status_code == 200

    resp = await _post("/public/demo-chat/turn", {**_keys(chat), "message": "one more"})

    assert resp.status_code == 429
    assert resp.json()["detail"] == "turn limit reached"
    assert model.calls == PUBLIC_CHAT_TURNS


async def test_a_credential_only_opens_its_own_session(chat_agent, demo_env, model):
    first, second = await _open_chat(), await _open_chat()

    resp = await _post("/public/demo-chat/turn",
                       {"credential": first["credential"], "session_id": second["session_id"], "message": "hi"})

    assert resp.status_code == 404
    assert model.calls == 0


@pytest.mark.parametrize("body", ['{"credential": "x", "session_id": "y", "message": ""}', "not json",
                                  json.dumps({"credential": "x", "session_id": "y", "message": "a" * 501})])
async def test_malformed_or_overlong_message_is_400(chat_agent, demo_env, model, body):
    resp = await _post("/public/demo-chat/turn", body)

    assert resp.status_code == 400
    assert resp.headers["access-control-allow-origin"] == SITE
    assert model.calls == 0


async def test_oversized_body_is_refused_before_parsing(chat_agent, demo_env, model):
    resp = await _post("/public/demo-chat/turn", "x" * 5000)

    assert resp.status_code == 413
    assert model.calls == 0


async def test_one_visitor_can_open_only_so_many_chats_an_hour(chat_agent, demo_env):
    demo_env(per_ip="2")
    await _open_chat()
    await _open_chat()

    resp = await _post("/public/demo-chat")

    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) > 0


async def test_inactive_demo_agent_is_404(chat_agent, demo_env):
    demo_env(agent_slug="site-off")

    resp = await _post("/public/demo-chat")

    assert resp.status_code == 404


def _keys(chat: dict) -> dict:
    return {"credential": chat["credential"], "session_id": chat["session_id"]}
