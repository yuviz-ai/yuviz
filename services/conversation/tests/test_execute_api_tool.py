"""
execute_api — Conversation-side integration tests (T20-T23):
  - ToolRegistry tripwire (AC 1): any future registry addition must be
    consciously accounted for.
  - ToolPolicyResolver._specialize_execute_api / enabled_tools() against a
    fake asyncpg pool (a recording connection, so AC 9's "no second query"
    claim can actually fail if the query becomes unconditional).
  - ApiExecExecutor's chain_status -> ToolStatus mapping, including
    partial -> FAILED/payload["partial"]=True, and the barge-in case
    driven through the real ToolCallOrchestrator + ExecutorRegistry.
  - Logging redaction (finding 8) against the real build_default_chain, not
    a hand-built middleware — so the test fails if redact_arg_keys is never
    passed at orchestrator.py's call site.
"""

from __future__ import annotations

import asyncio
import logging

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.tools.executor_registry import ExecutorRegistry
from services.conversation.tools.executors.api_exec_executor import ApiExecExecutor
from services.conversation.tools.llm_adapter import LLMAdapter, ToolCallEvent, ToolCallStartedEvent
from services.conversation.tools.middleware import build_default_chain
from services.conversation.tools.orchestrator import ToolCallOrchestrator
from services.conversation.tools.policy_resolver import ResolvedToolPolicy, ToolPolicyResolver
from services.conversation.tools.registry import ToolRegistry
from services.conversation.tools.types import ToolExecutionContext, ToolExecutionRequest, ToolResult, ToolStatus


# ── AC 1 tripwire ─────────────────────────────────────────────────────────


def test_registry_tripwire_every_tool_name_is_accounted_for():
    names = {d.name for d in ToolRegistry().all()}
    assert names == {
        "book_appointment", "cancel_appointment", "reschedule_appointment", "send_sms", "execute_api",
    }


# ── ToolPolicyResolver._specialize_execute_api / enabled_tools() ─────────


class _FakeConn:
    def __init__(self, results: list[list[dict]]) -> None:
        self._results = list(results)
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.queries.append((query, args))
        return self._results.pop(0)


class _Acquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


def _atp_row(tool_name: str, tool_provider_config_id: str = "cfg1", max_chain_depth=None) -> dict:
    return {
        "tool_name": tool_name, "timeout_ms": None, "max_calls_per_turn": None,
        "max_chain_depth": max_chain_depth, "tool_provider_config_id": tool_provider_config_id,
        "engine": "toolexec", "api_key_ref": None, "extra": {},
    }


def _api_row(name: str, param_name=None, param_sensitive=False) -> dict:
    return {
        "id": name, "name": name, "description": f"{name} description", "chain_levels": 1,
        "param_name": param_name, "param_description": "a param", "json_type": "string",
        "required": True, "param_sensitive": param_sensitive,
    }


async def test_seven_enabled_apis_yield_exactly_one_execute_api_entry_with_a_seven_value_enum():
    conn = _FakeConn([
        [_atp_row("execute_api")],
        [_api_row(f"api_{i}") for i in range(7)],
    ])
    resolver = ToolPolicyResolver(pool=_FakePool(conn), registry=ToolRegistry())

    resolved = await resolver.enabled_tools("agent1")

    execute_api_policies = [p for p in resolved if p.definition.name == "execute_api"]
    assert len(execute_api_policies) == 1
    enum = execute_api_policies[0].definition.parameters_schema["properties"]["api_name"]["enum"]
    assert len(enum) == 7
    assert len(conn.queries) == 2  # main query + one specialization query


async def test_zero_enabled_apis_means_no_execute_api_entry_and_no_second_query():
    # atp has no row for execute_api at all (the agent never enabled it) —
    # the resolver must not issue the specialization query.
    conn = _FakeConn([[]])
    resolver = ToolPolicyResolver(pool=_FakePool(conn), registry=ToolRegistry())

    resolved = await resolver.enabled_tools("agent1")

    assert all(p.definition.name != "execute_api" for p in resolved)
    assert len(conn.queries) == 1


async def test_execute_api_enabled_but_zero_custom_apis_drops_the_policy():
    conn = _FakeConn([
        [_atp_row("execute_api")],
        [],  # zero enabled custom APIs
    ])
    resolver = ToolPolicyResolver(pool=_FakePool(conn), registry=ToolRegistry())

    resolved = await resolver.enabled_tools("agent1")

    assert all(p.definition.name != "execute_api" for p in resolved)
    assert len(conn.queries) == 2


async def test_sensitive_arg_keys_union_from_p_sensitive():
    conn = _FakeConn([
        [_atp_row("execute_api")],
        [_api_row("lookup_customer", param_name="national_id", param_sensitive=True)],
    ])
    resolver = ToolPolicyResolver(pool=_FakePool(conn), registry=ToolRegistry())

    resolved = await resolver.enabled_tools("agent1")

    execute_api_policy = next(p for p in resolved if p.definition.name == "execute_api")
    assert execute_api_policy.sensitive_arg_keys == {"national_id"}


async def test_no_sensitive_params_means_empty_sensitive_arg_keys():
    conn = _FakeConn([
        [_atp_row("execute_api")],
        [_api_row("lookup_order", param_name="order_id", param_sensitive=False)],
    ])
    resolver = ToolPolicyResolver(pool=_FakePool(conn), registry=ToolRegistry())

    resolved = await resolver.enabled_tools("agent1")

    execute_api_policy = next(p for p in resolved if p.definition.name == "execute_api")
    assert execute_api_policy.sensitive_arg_keys == frozenset()


# ── ApiExecExecutor: chain_status -> ToolStatus ──────────────────────────


def _ctx(deadline: float | None = None) -> ToolExecutionContext:
    import time
    return ToolExecutionContext(
        tenant_id="t1", agent_id="a1", call_id="c1", session_id="s1", turn_id="turn1",
        tool_iteration=0, deadline=deadline if deadline is not None else time.monotonic() + 6.0,
        request_id="r1",
    )


def _request(arguments: dict | None = None) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        tool_call_id="call1", tool_name="execute_api",
        arguments=arguments if arguments is not None else {"api_name": "lookup_order", "inputs": {}},
        context=_ctx(),
    )


class _FakeToolExecClient:
    def __init__(self, response: dict | None = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.calls: list[dict] = []

    async def execute_chain(self, body: dict) -> dict:
        self.calls.append(body)
        if self._exc is not None:
            raise self._exc
        return self._response


async def test_success_maps_to_success_and_returns_only_data_projection():
    client = _FakeToolExecClient({
        "chain_status": "success", "data": {"order_id": "o1"}, "steps": [{"api_name": "x"}],
    })
    executor = ApiExecExecutor(client)

    result = await executor.execute(_request())

    assert result.status == ToolStatus.SUCCESS
    assert result.payload == {"order_id": "o1"}


async def test_partial_maps_to_failed_with_partial_flag():
    client = _FakeToolExecClient({"chain_status": "partial", "data": {}, "error": "b failed"})
    executor = ApiExecExecutor(client)

    result = await executor.execute(_request())

    assert result.status == ToolStatus.FAILED
    assert result.payload["partial"] is True


async def test_chain_status_mapping_table():
    mapping = {
        "failed": ToolStatus.FAILED,
        "timeout": ToolStatus.TIMEOUT,
        "invalid_argument": ToolStatus.INVALID_ARGUMENT,
        "unavailable": ToolStatus.UNAVAILABLE,
        "rate_limited": ToolStatus.RATE_LIMITED,
    }
    for chain_status, expected in mapping.items():
        client = _FakeToolExecClient({"chain_status": chain_status, "data": {}})
        executor = ApiExecExecutor(client)
        result = await executor.execute(_request())
        assert result.status == expected, chain_status


async def test_invalid_argument_forwards_missing_fields_into_the_payload():
    """QA defect 9, end to end from the executor response through to what
    the conversation side receives: a caller who asks for a refund
    without an order id must get back the name of the field that's
    missing, not an empty payload — that is the one question the LLM
    needs to ask to complete the task."""
    client = _FakeToolExecClient({
        "chain_status": "invalid_argument", "data": {}, "error": "missing_fields",
        "missing_fields": [{"name": "order_id", "description": ""}],
    })
    executor = ApiExecExecutor(client)

    result = await executor.execute(_request())

    assert result.status == ToolStatus.INVALID_ARGUMENT
    assert result.payload["missing_fields"] == [{"name": "order_id", "description": ""}]


async def test_chain_budget_ms_derived_from_context_deadline():
    import time
    client = _FakeToolExecClient({"chain_status": "success", "data": {}})
    executor = ApiExecExecutor(client)

    request = ToolExecutionRequest(
        tool_call_id="call1", tool_name="execute_api",
        arguments={"api_name": "lookup_order", "inputs": {}},
        context=_ctx(deadline=time.monotonic() + 3.0),
    )
    await executor.execute(request)

    sent = client.calls[0]
    assert 2000 <= sent["chain_budget_ms"] <= 3000


async def test_execute_chain_raising_maps_to_failed_toolexec_unavailable():
    client = _FakeToolExecClient(exc=RuntimeError("network down"))
    executor = ApiExecExecutor(client)

    result = await executor.execute(_request())

    assert result.status == ToolStatus.FAILED
    assert result.error == "toolexec_unavailable"


# ── FIX 1 — the per-agent max_chain_depth override must reach the body ───

async def test_context_max_chain_depth_none_falls_back_to_platform_default():
    client = _FakeToolExecClient({"chain_status": "success", "data": {}})
    executor = ApiExecExecutor(client)
    await executor.execute(_request())  # _ctx() default: max_chain_depth=None
    assert client.calls[0]["max_chain_depth"] == 4


async def test_context_max_chain_depth_override_reaches_the_body():
    client = _FakeToolExecClient({"chain_status": "success", "data": {}})
    executor = ApiExecExecutor(client)
    request = ToolExecutionRequest(
        tool_call_id="call1", tool_name="execute_api",
        arguments={"api_name": "lookup_order", "inputs": {}},
        context=ToolExecutionContext(
            tenant_id="t1", agent_id="a1", call_id="c1", session_id="s1", turn_id="turn1",
            tool_iteration=0, deadline=__import__("time").monotonic() + 6.0, request_id="r1",
            max_chain_depth=2,
        ),
    )
    await executor.execute(request)
    assert client.calls[0]["max_chain_depth"] == 2


async def test_context_max_chain_depth_cannot_exceed_platform_ceiling():
    """Defense in depth: even if something upstream ever let an override
    above the platform ceiling through, this executor still clamps it —
    it never just forwards the context value verbatim."""
    client = _FakeToolExecClient({"chain_status": "success", "data": {}})
    executor = ApiExecExecutor(client)
    request = ToolExecutionRequest(
        tool_call_id="call1", tool_name="execute_api",
        arguments={"api_name": "lookup_order", "inputs": {}},
        context=ToolExecutionContext(
            tenant_id="t1", agent_id="a1", call_id="c1", session_id="s1", turn_id="turn1",
            tool_iteration=0, deadline=__import__("time").monotonic() + 6.0, request_id="r1",
            max_chain_depth=64,
        ),
    )
    await executor.execute(request)
    assert client.calls[0]["max_chain_depth"] == 4


# ── barge-in (AC 8) driven through the real orchestrator ────────────────


def _policy(sensitive_arg_keys: frozenset[str] = frozenset(), max_chain_depth: int | None = None) -> ResolvedToolPolicy:
    defn = ToolRegistry().resolve("execute_api")
    return ResolvedToolPolicy(
        definition=defn, tool_provider_config_id="cfg1", engine="toolexec", api_key_ref=None,
        extra={}, timeout_ms=None, max_calls_per_turn=None, sensitive_arg_keys=sensitive_arg_keys,
        max_chain_depth=max_chain_depth,
    )


async def test_orchestrator_threads_policy_max_chain_depth_into_the_request():
    """Drives the real ToolCallOrchestrator (not a hand-built
    ToolExecutionContext) so this fails if orchestrator.py's
    ToolExecutionContext(...) call site ever drops max_chain_depth=
    policy.max_chain_depth — the only place that holds both the resolved
    policy and the context it builds."""
    from services.conversation.tools.llm_adapter import TokenEvent

    client = _FakeToolExecClient({"chain_status": "success", "data": {}})
    registry = ExecutorRegistry()
    registry.register("execute_api", lambda provider, companion=None: ApiExecExecutor(provider))
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(_ScriptedLLM([
            [ToolCallEvent(tool_call_id="c1", tool_name="execute_api", arguments={"api_name": "lookup_order"})],
            [TokenEvent(text="done")],
        ])),
        policy_resolver=_FakePolicyResolver([_policy(max_chain_depth=2)]),
        provider_manager=_FakeProviderManager(client),
        executor_registry=registry,
    )
    history = [ChatMessage(role="user", content="what's my order status")]
    [_ async for _ in orchestrator.run_turn("agent1", "t1", "c1", "s1", history)]

    assert client.calls[0]["max_chain_depth"] == 2


class _FakePolicyResolver:
    def __init__(self, policies: list[ResolvedToolPolicy]) -> None:
        self._policies = policies

    async def enabled_tools(self, agent_id: str, only=None) -> list[ResolvedToolPolicy]:
        return self._policies


class _FakeProviderManager:
    def __init__(self, provider) -> None:
        self._provider = provider

    async def get(self, policy: ResolvedToolPolicy):
        return self._provider


class _ScriptedLLM:
    def __init__(self, scripted_calls: list[list]) -> None:
        self._scripted_calls = scripted_calls
        self.call_count = 0

    async def generate_with_tools(self, messages, schemas, tool_choice=None):
        events = self._scripted_calls[self.call_count]
        self.call_count += 1
        for e in events:
            yield e


class _SlowToolExecClient:
    def __init__(self, response: dict) -> None:
        self._response = response
        self.release_event = asyncio.Event()
        self.started_event = asyncio.Event()

    async def execute_chain(self, body: dict) -> dict:
        self.started_event.set()
        await self.release_event.wait()
        return self._response


def _orchestrator(client) -> ToolCallOrchestrator:
    registry = ExecutorRegistry()
    registry.register("execute_api", lambda provider, companion=None: ApiExecExecutor(provider))
    return ToolCallOrchestrator(
        llm_adapter=LLMAdapter(_ScriptedLLM([[
            ToolCallEvent(tool_call_id="c1", tool_name="execute_api", arguments={"api_name": "lookup_order"}),
        ]])),
        policy_resolver=_FakePolicyResolver([_policy()]),
        provider_manager=_FakeProviderManager(client),
        executor_registry=registry,
    )


async def test_cancel_event_mid_call_yields_failed_cancelled_and_never_folds_the_late_result():
    import json

    client = _SlowToolExecClient({"chain_status": "success", "data": {"order_id": "o1"}})
    orchestrator = _orchestrator(client)
    cancel_event = asyncio.Event()
    history = [ChatMessage(role="user", content="what's my order status")]

    async def _collect():
        return [e async for e in orchestrator.run_turn(
            "agent1", "t1", "c1", "s1", history, cancel_event=cancel_event,
        )]

    task = asyncio.ensure_future(_collect())
    await asyncio.wait_for(client.started_event.wait(), timeout=1.0)
    assert not task.done()

    cancel_event.set()
    events = await asyncio.wait_for(task, timeout=1.0)

    assert events == [ToolCallStartedEvent(tool_name="execute_api")]
    folded = json.loads(history[2].content)
    assert folded["status"] == "failed"
    assert folded["error"] == "cancelled"

    # Let the abandoned call finish server-side and confirm its (late,
    # successful) result is never folded into history after the fact.
    client.release_event.set()
    await asyncio.sleep(0)
    assert json.loads(history[2].content)["error"] == "cancelled"


# ── logging redaction (finding 8), against the real build_default_chain ──


async def test_sensitive_arg_keys_never_appear_in_any_log_record(caplog):
    client = _FakeToolExecClient({"chain_status": "success", "data": {"looked_up": True}})
    executor = ApiExecExecutor(client)
    chain = build_default_chain(executor, timeout_ms=1000, redact_arg_keys=frozenset({"national_id"}))
    request = _request(arguments={
        "api_name": "lookup_customer", "inputs": {"national_id": "SENTINEL-SSN-123", "name": "Alex"},
    })

    with caplog.at_level(logging.INFO, logger="services.conversation.tools.middleware"):
        result = await chain.execute(request)

    assert result.status == ToolStatus.SUCCESS
    for record in caplog.records:
        assert "SENTINEL-SSN-123" not in record.getMessage()
        assert "SENTINEL-SSN-123" not in repr(record.args)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "lookup_customer" in joined
    assert "Alex" in joined


async def test_empty_redact_arg_keys_default_logs_byte_identical_to_today(caplog):
    client = _FakeToolExecClient({"chain_status": "success", "data": {}})
    executor = ApiExecExecutor(client)
    chain = build_default_chain(executor, timeout_ms=1000)  # default redact_arg_keys=frozenset()
    request = _request(arguments={"api_name": "lookup_order", "inputs": {"order_id": "o1"}})

    with caplog.at_level(logging.INFO, logger="services.conversation.tools.middleware"):
        await chain.execute(request)

    start_record = next(r for r in caplog.records if "tool_call start" in r.getMessage())
    assert start_record.getMessage() == (
        "tool_call start tool=execute_api call_id=call1 tenant=t1 agent=a1 "
        "arguments={'api_name': 'lookup_order', 'inputs': {'order_id': 'o1'}}"
    )


async def test_orchestrator_wires_policy_sensitive_arg_keys_into_the_real_logging_middleware(caplog):
    """Drives the real ToolCallOrchestrator (not build_default_chain called
    directly) so this fails if redact_arg_keys is never passed at
    orchestrator.py's build_default_chain call site — the only place that
    holds both the resolved policy and the chain."""
    client = _FakeToolExecClient({"chain_status": "success", "data": {}})
    registry = ExecutorRegistry()
    registry.register(
        "execute_api", lambda provider, companion=None: ApiExecExecutor(provider),
    )
    from services.conversation.tools.llm_adapter import TokenEvent

    llm = _ScriptedLLM([
        [ToolCallEvent(
            tool_call_id="c1", tool_name="execute_api",
            arguments={"api_name": "lookup_customer", "inputs": {"national_id": "SENTINEL-SSN-456"}},
        )],
        [TokenEvent(text="done")],
    ])
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy(sensitive_arg_keys=frozenset({"national_id"}))]),
        provider_manager=_FakeProviderManager(client),
        executor_registry=registry,
    )
    history = [ChatMessage(role="user", content="look me up")]

    with caplog.at_level(logging.INFO, logger="services.conversation.tools.middleware"):
        [e async for e in orchestrator.run_turn("agent1", "t1", "c1", "s1", history)]

    for record in caplog.records:
        assert "SENTINEL-SSN-456" not in record.getMessage()


# ── tenant fence (AC 10), real Postgres — defense-in-depth independent of
#    the write-time gate in services/toolexec/agent_apis.py ──────────────


async def _pg_pool():
    import asyncpg
    import getpass

    dsn = f"postgresql://{getpass.getuser()}@localhost:5432/voiceai"
    return await asyncpg.create_pool(dsn, min_size=1, max_size=2)


async def test_specialize_execute_api_is_tenant_fenced_against_a_cross_tenant_agent_custom_apis_row():
    """agent_custom_apis carries no tenant_id of its own — the tenant fence
    is the a.tenant_id = ca.tenant_id join predicate in
    _specialize_execute_api's own query. Simulates a row that should never
    exist (the write-time gate in services/toolexec/agent_apis.py is
    supposed to prevent it) to prove this READ path is a genuine second
    line of defense, independent of that gate — not merely untestable
    because the write path already excludes the scenario (lesson 12)."""
    pool = await _pg_pool()
    try:
        tenant_a = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Fence Test A", f"fence-a-{uuid_hex()}",
        )
        tenant_b = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Fence Test B", f"fence-b-{uuid_hex()}",
        )
        agent_a = await pool.fetchrow(
            "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *",
            tenant_a["id"],
        )
        # custom_api belongs to tenant B, not tenant A.
        custom_api_b = await pool.fetchrow(
            "INSERT INTO custom_apis (tenant_id, name, description, endpoint_url, method) "
            "VALUES ($1, 'lookup_order', 'desc', 'https://example.com/x', 'GET') RETURNING *",
            tenant_b["id"],
        )
        # A row that should never exist: agent A's id paired with tenant B's
        # custom_api_id (agent_custom_apis has no tenant_id column to stop it
        # at the schema level — the write-time gate is the only other guard,
        # and this test exists precisely to not rely on it).
        await pool.execute(
            "INSERT INTO agent_custom_apis (agent_id, custom_api_id, enabled) VALUES ($1, $2, true)",
            agent_a["id"], custom_api_b["id"],
        )

        # Calls _specialize_execute_api directly (not through enabled_tools()
        # / agent_tool_policies) — this test is about the read-path tenant
        # fence itself, independent of whether execute_api is even enabled.
        resolver = ToolPolicyResolver(pool=pool, registry=ToolRegistry())
        specialized = await resolver._specialize_execute_api(
            ToolRegistry().resolve("execute_api"), str(agent_a["id"]),
        )

        # Tenant A's agent must NOT see tenant B's custom API — the fence
        # must produce None (zero enabled APIs), not tenant B's row.
        assert specialized is None
    finally:
        await pool.execute("DELETE FROM agent_custom_apis WHERE agent_id = $1", agent_a["id"])
        await pool.execute("DELETE FROM custom_apis WHERE tenant_id = $1", tenant_b["id"])
        await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant_a["id"])
        await pool.execute("DELETE FROM tenants WHERE id = ANY($1)", [tenant_a["id"], tenant_b["id"]])
        await pool.close()


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex[:8]
