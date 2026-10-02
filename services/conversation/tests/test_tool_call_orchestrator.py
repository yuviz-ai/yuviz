"""ToolCallOrchestrator.run_turn() unit tests with fake collaborators: passthrough, tool fold-back,
max_tool_iterations bound, and unknown-tool failure."""

from __future__ import annotations

import asyncio
import json

import pytest

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.tools.executor_registry import ExecutorRegistry
from services.conversation.tools.llm_adapter import (
    DeterministicSpokenEvent, LLMAdapter, LocalToolCompletedEvent, TokenEvent, ToolCallEvent, ToolCallStartedEvent,
)
from services.conversation.tools.orchestrator import ToolCallOrchestrator
from services.conversation.tools.policy_resolver import ResolvedToolPolicy
from services.conversation.tools.registry import ToolRegistry
from services.conversation.tools.types import ToolDefinition, ToolResult, ToolStatus


def _policy(tool_name: str = "execute_api") -> ResolvedToolPolicy:
    defn = ToolRegistry().resolve(tool_name)
    return ResolvedToolPolicy(
        definition=defn, tool_provider_config_id="cfg1", engine="toolexec",
        api_key_ref="env:X",
        extra={}, timeout_ms=None, max_calls_per_turn=None,
    )


class _FakePolicyResolver:
    def __init__(self, policies: list[ResolvedToolPolicy]) -> None:
        self._policies = policies

    async def enabled_tools(
        self, agent_id: str, tenant_slug: str, only: list[str] | None = None,
    ) -> list[ResolvedToolPolicy]:
        self.last_only = only
        return self._policies


class _FakeProviderManager:
    async def get(self, policy: ResolvedToolPolicy):
        return object()  # opaque — only ExecutorRegistry's factory cares


class _FixedExecutor:
    def __init__(self, result: ToolResult) -> None:
        self._result = result
        self.calls: list = []

    async def execute(self, request) -> ToolResult:
        self.calls.append(request)
        return self._result


class _ScriptedLLM:
    """Fake IToolAwareLLM yielding one pre-scripted event list per call."""

    def __init__(self, scripted_calls: list[list]) -> None:
        self._scripted_calls = scripted_calls
        self.call_count = 0
        self.seen_messages: list[list[ChatMessage]] = []
        self.seen_schemas: list = []

    async def generate(self, messages):
        # Plain-generate fallback (no tools, or tools withdrawn after max_tool_iterations):
        # same script, unwrapped to bare tokens.
        self.seen_messages.append(list(messages))
        events = self._scripted_calls[self.call_count]
        self.call_count += 1
        for e in events:
            assert isinstance(e, TokenEvent), "plain generate() can't script a ToolCallEvent"
            yield e.text

    async def generate_with_tools(self, messages, schemas, tool_choice=None):
        self.seen_messages.append(list(messages))
        self.seen_schemas.append(schemas)
        events = self._scripted_calls[self.call_count]
        self.call_count += 1
        for e in events:
            yield e


async def test_llm_invisible_tool_is_configurable_but_never_offered_to_the_llm():
    """An llm_visible=False tool can be enabled but never appears in the LLM's schemas."""
    llm = _ScriptedLLM([[TokenEvent(text="Hi")]])
    hidden_policy = ResolvedToolPolicy(
        definition=ToolDefinition(
            name="hidden_side_effect", description="never offered", parameters_schema={},
            llm_visible=False,
        ),
        tool_provider_config_id="cfg2", engine="toolexec", api_key_ref=None,
        extra={}, timeout_ms=None, max_calls_per_turn=None,
    )
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy(), hidden_policy]),
        provider_manager=_FakeProviderManager(),
        executor_registry=ExecutorRegistry(),
    )

    _ = [e async for e in orchestrator.run_turn("agent1", "t1", "c1", "s1", [ChatMessage(role="user", content="hi")])]

    seen_names = {s["name"] for s in llm.seen_schemas[0]}
    assert seen_names == {"execute_api"}


async def test_plain_text_turn_with_no_tools_enabled_never_touches_executor():
    llm = _ScriptedLLM([[TokenEvent(text="Hi"), TokenEvent(text=" there")]])
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([]),  # no tools enabled for this agent
        provider_manager=_FakeProviderManager(),
        executor_registry=ExecutorRegistry(),
    )

    events = [e async for e in orchestrator.run_turn("agent1", "t1", "c1", "s1", [ChatMessage(role="user", content="hi")])]

    assert events == [TokenEvent(text="Hi"), TokenEvent(text=" there")]
    assert llm.call_count == 1


async def test_tool_call_executes_folds_result_and_continues_to_final_answer():
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="execute_api", arguments={"api_name": "x"})],
        [TokenEvent(text="You're booked!")],
    ])
    executor = _FixedExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={"booked": True, "booking_id": "b1"}))
    registry = ExecutorRegistry()
    registry.register("execute_api", lambda provider: executor)

    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy()]),
        provider_manager=_FakeProviderManager(),
        executor_registry=registry,
    )

    history = [ChatMessage(role="user", content="book me tomorrow at 3")]
    events = [e async for e in orchestrator.run_turn("agent1", "t1", "c1", "s1", history)]

    assert events == [
        ToolCallStartedEvent(tool_name="execute_api"),
        TokenEvent(text="You're booked!"),
    ]
    assert llm.call_count == 2
    assert len(executor.calls) == 1
    assert executor.calls[0].arguments == {"api_name": "x"}

    # History was mutated in place with the tool call + result.
    assert history[1].role == "assistant" and history[1].tool_calls[0]["name"] == "execute_api"
    assert history[2].role == "tool"
    assert json.loads(history[2].content) == {"status": "success", "booked": True, "booking_id": "b1"}

    # Second generate_with_tools() call saw the folded-in history.
    assert len(llm.seen_messages[1]) == 3


async def test_force_tool_name_forces_tool_choice_on_first_call_only():
    """force_tool_name sets tool_choice on the turn's first call only, not on later iterations."""
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="execute_api", arguments={"api_name": "x"})],
        [TokenEvent(text="booked")],
    ])
    executor = _FixedExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={"booked": True}))
    registry = ExecutorRegistry()
    registry.register("execute_api", lambda provider: executor)

    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy()]),
        provider_manager=_FakeProviderManager(),
        executor_registry=registry,
    )

    seen_tool_choices = []
    original_generate_with_tools = llm.generate_with_tools

    async def spying_generate_with_tools(messages, schemas, tool_choice=None):
        seen_tool_choices.append(tool_choice)
        async for e in original_generate_with_tools(messages, schemas, tool_choice=tool_choice):
            yield e

    llm.generate_with_tools = spying_generate_with_tools

    [e async for e in orchestrator.run_turn(
        "agent1", "t1", "c1", "s1", [ChatMessage(role="user", content="yes")],
        force_tool_name="execute_api",
    )]

    assert seen_tool_choices == [
        {"type": "function", "function": {"name": "execute_api"}},
        None,
    ]


async def test_deterministic_response_short_circuits_llm_narration():
    """A deterministic_response is spoken verbatim and the LLM isn't called again this turn."""
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="execute_api", arguments={"api_name": "x"})],
        [TokenEvent(text="should never be requested")],
    ])
    executor = _FixedExecutor(ToolResult(
        status=ToolStatus.SUCCESS, payload={"booked": True, "booking_id": "b1"},
        deterministic_response="You're all set — I've booked your appointment for Friday at 3 PM.",
    ))
    registry = ExecutorRegistry()
    registry.register("execute_api", lambda provider: executor)

    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy()]),
        provider_manager=_FakeProviderManager(),
        executor_registry=registry,
    )

    events = [e async for e in orchestrator.run_turn(
        "agent1", "t1", "c1", "s1", [ChatMessage(role="user", content="book me tomorrow at 3")],
    )]

    assert events == [
        ToolCallStartedEvent(tool_name="execute_api"),
        DeterministicSpokenEvent(text="You're all set — I've booked your appointment for Friday at 3 PM."),
    ]
    assert llm.call_count == 1  # never asked to narrate the result


async def test_max_tool_iterations_forces_final_generation_without_tools():
    # After max_tool_iterations, tools are withdrawn so the final call must answer in plain text.
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="execute_api", arguments={"api_name": "x"})],
        [ToolCallEvent(tool_call_id="c2", tool_name="execute_api", arguments={"api_name": "y"})],
        [TokenEvent(text="Sorry, having trouble booking that.")],
    ])
    executor = _FixedExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={"booked": False, "available_slots": []}))
    registry = ExecutorRegistry()
    registry.register("execute_api", lambda provider: executor)

    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy()]),
        provider_manager=_FakeProviderManager(),
        executor_registry=registry,
        max_tool_iterations=2,
    )

    events = [e async for e in orchestrator.run_turn("agent1", "t1", "c1", "s1", [ChatMessage(role="user", content="book")])]

    assert events == [
        ToolCallStartedEvent(tool_name="execute_api"),
        ToolCallStartedEvent(tool_name="execute_api"),
        TokenEvent(text="Sorry, having trouble booking that."),
    ]
    assert llm.call_count == 3
    # The third (final) call must have been made with no tool schemas.
    assert llm.call_count == 3


async def test_unknown_tool_name_is_a_failed_result_not_a_crash():
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="send_email", arguments={})],  # not enabled/offered
        [TokenEvent(text="I can't do that.")],
    ])
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy()]),  # only book_appointment enabled
        provider_manager=_FakeProviderManager(),
        executor_registry=ExecutorRegistry(),
    )

    history = [ChatMessage(role="user", content="email me")]
    events = [e async for e in orchestrator.run_turn("agent1", "t1", "c1", "s1", history)]

    assert events == [
        ToolCallStartedEvent(tool_name="send_email"),
        TokenEvent(text="I can't do that."),
    ]
    assert json.loads(history[2].content)["status"] == "failed"
    assert json.loads(history[2].content)["error"] == "unknown_tool"


class _SlowExecutor:
    """Blocks until release_event is set, so tests can assert on in-flight behavior."""

    def __init__(self, result: ToolResult) -> None:
        self._result = result
        self.release_event = asyncio.Event()
        self.started_event = asyncio.Event()

    async def execute(self, request) -> ToolResult:
        self.started_event.set()
        await self.release_event.wait()
        return self._result


async def test_cancel_event_stops_waiting_on_an_in_flight_tool_call():
    """Setting cancel_event stops run_turn() waiting on an in-flight tool call immediately."""
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="execute_api", arguments={"api_name": "x"})],
    ])
    executor = _SlowExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={"booked": True}))
    registry = ExecutorRegistry()
    registry.register("execute_api", lambda provider: executor)

    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy()]),
        provider_manager=_FakeProviderManager(),
        executor_registry=registry,
    )

    cancel_event = asyncio.Event()
    history = [ChatMessage(role="user", content="book me tomorrow at 3")]

    async def _collect():
        return [e async for e in orchestrator.run_turn(
            "agent1", "t1", "c1", "s1", history, cancel_event=cancel_event,
        )]

    task = asyncio.ensure_future(_collect())
    await asyncio.wait_for(executor.started_event.wait(), timeout=1.0)
    assert not task.done()  # still waiting on the slow tool call

    cancel_event.set()
    events = await asyncio.wait_for(task, timeout=1.0)

    assert events == [ToolCallStartedEvent(tool_name="execute_api")]
    assert json.loads(history[2].content)["status"] == "failed"
    assert json.loads(history[2].content)["error"] == "cancelled"
    assert llm.call_count == 1  # never asked the LLM what to do next — the turn is stale

    # Let the backgrounded call finish so nothing's left dangling for the
    # test process to warn about.
    executor.release_event.set()
    await asyncio.sleep(0)


async def test_cancel_event_set_before_the_tool_call_even_starts_still_stops_the_turn():
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="execute_api", arguments={"api_name": "x"})],
    ])
    executor = _SlowExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={"booked": True}))
    registry = ExecutorRegistry()
    registry.register("execute_api", lambda provider: executor)

    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy()]),
        provider_manager=_FakeProviderManager(),
        executor_registry=registry,
    )

    cancel_event = asyncio.Event()
    cancel_event.set()  # already cancelled before run_turn is even called
    history = [ChatMessage(role="user", content="book me tomorrow at 3")]

    events = [e async for e in orchestrator.run_turn(
        "agent1", "t1", "c1", "s1", history, cancel_event=cancel_event,
    )]

    assert events == [ToolCallStartedEvent(tool_name="execute_api")]
    assert json.loads(history[2].content)["error"] == "cancelled"

    executor.release_event.set()
    await asyncio.sleep(0)


async def test_no_cancel_event_behaves_exactly_as_before():
    """Without cancel_event, run_turn() awaits the tool call to completion."""
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="execute_api", arguments={"api_name": "x"})],
        [TokenEvent(text="You're booked!")],
    ])
    executor = _FixedExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={"booked": True, "booking_id": "b1"}))
    registry = ExecutorRegistry()
    registry.register("execute_api", lambda provider: executor)

    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy()]),
        provider_manager=_FakeProviderManager(),
        executor_registry=registry,
    )

    history = [ChatMessage(role="user", content="book me tomorrow at 3")]
    events = [e async for e in orchestrator.run_turn("agent1", "t1", "c1", "s1", history)]

    assert events == [
        ToolCallStartedEvent(tool_name="execute_api"),
        TokenEvent(text="You're booked!"),
    ]


# ── local_tools ─────────────────────────────────────────────────────────


def _local(name: str = "caller_verified"):
    calls: list[dict] = []

    async def handler(args: dict) -> ToolResult:
        calls.append(args)
        return ToolResult(status=ToolStatus.SUCCESS, payload={"status": "done"})

    definition = ToolDefinition(
        name=name, description="The caller confirmed who they are.",
        parameters_schema={"type": "object", "properties": {}},
    )
    return {name: (definition, handler)}, calls


async def test_a_local_tool_executes_without_touching_policy_or_provider():
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="caller_verified", arguments={})],
        [TokenEvent(text="Thanks!")],
    ])
    local_tools, calls = _local()
    provider_manager = _FakeProviderManager()
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([]),
        provider_manager=provider_manager,
        executor_registry=ExecutorRegistry(),
        metrics=None,
    )

    history = [ChatMessage(role="user", content="my dob is...")]
    events = [
        e async for e in orchestrator.run_turn(
            "agent1", "t1", "c1", "s1", history, local_tools=local_tools,
        )
    ]

    assert calls == [{}]
    assert TokenEvent(text="Thanks!") in events
    assert LocalToolCompletedEvent(tool_name="caller_verified") in events
    assert not any(isinstance(e, ToolCallStartedEvent) for e in events)
    assert json.loads(history[-1].content)["status"] == "done"


async def test_a_local_tool_does_not_consume_a_tool_iteration():
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="caller_verified", arguments={})],
        [ToolCallEvent(tool_call_id="c2", tool_name="execute_api",
                       arguments={"requested_datetime": "2026-01-01T10:00:00Z"})],
        [TokenEvent(text="Booked.")],
    ])
    local_tools, _ = _local()
    executor = _FixedExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={"booked": True}))
    registry = ExecutorRegistry()
    registry.register("execute_api", lambda provider: executor)
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy()]),
        provider_manager=_FakeProviderManager(),
        executor_registry=registry,
        max_tool_iterations=1,
    )

    events = [
        e async for e in orchestrator.run_turn(
            "agent1", "t1", "c1", "s1",
            [ChatMessage(role="user", content="book me in")], local_tools=local_tools,
        )
    ]

    assert len(executor.calls) == 1
    assert TokenEvent(text="Booked.") in events


async def test_local_tool_schemas_are_offered_alongside_policy_tools():
    llm = _ScriptedLLM([[TokenEvent(text="hi")]])
    local_tools, _ = _local()
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy()]),
        provider_manager=_FakeProviderManager(),
        executor_registry=ExecutorRegistry(),
    )
    seen: list[list[dict]] = []
    original = llm.generate_with_tools

    async def _record(messages, schemas, tool_choice=None):
        seen.append(schemas)
        async for e in original(messages, schemas, tool_choice=tool_choice):
            yield e

    llm.generate_with_tools = _record

    [e async for e in orchestrator.run_turn(
        "agent1", "t1", "c1", "s1", [ChatMessage(role="user", content="hi")],
        local_tools=local_tools,
    )]

    assert [s["name"] for s in seen[0]] == ["execute_api", "caller_verified"]


async def test_local_tools_are_capped_so_a_cyclic_workflow_cannot_spin_forever():
    local_tools, calls = _local()
    llm = _ScriptedLLM(
        [[ToolCallEvent(tool_call_id=f"c{i}", tool_name="caller_verified", arguments={})]
         for i in range(3)]
        + [[TokenEvent(text="Anyway.")]]
    )
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([]),
        provider_manager=_FakeProviderManager(),
        executor_registry=ExecutorRegistry(),
        max_local_tool_calls=3,
    )

    events = [
        e async for e in orchestrator.run_turn(
            "agent1", "t1", "c1", "s1",
            [ChatMessage(role="user", content="hi")], local_tools=local_tools,
        )
    ]

    assert len(calls) == 3
    assert TokenEvent(text="Anyway.") in events


async def test_past_local_cap_does_not_burn_a_remote_iteration():
    # After the local cap, a repeat local name must fail cheaply — not as
    # unknown_tool consuming max_tool_iterations before a real remote call.
    local_tools, calls = _local()
    llm = _ScriptedLLM(
        [[ToolCallEvent(tool_call_id=f"c{i}", tool_name="caller_verified", arguments={})]
         for i in range(4)]
        + [[ToolCallEvent(tool_call_id="book", tool_name="execute_api",
                          arguments={"requested_datetime": "2026-01-01T10:00:00Z"})],
           [TokenEvent(text="Booked.")]]
    )
    executor = _FixedExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={"booked": True}))
    registry = ExecutorRegistry()
    registry.register("execute_api", lambda provider: executor)
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy()]),
        provider_manager=_FakeProviderManager(),
        executor_registry=registry,
        max_local_tool_calls=3,
        max_tool_iterations=1,
    )
    history = [ChatMessage(role="user", content="hi")]

    events = [
        e async for e in orchestrator.run_turn(
            "agent1", "t1", "c1", "s1", history, local_tools=local_tools,
        )
    ]

    assert len(calls) == 3
    assert len(executor.calls) == 1
    assert TokenEvent(text="Booked.") in events
    assert not any(isinstance(e, ToolCallStartedEvent) and e.tool_name == "caller_verified"
                   for e in events)
    cap_payloads = [
        json.loads(m.content) for m in history
        if m.role == "tool" and "local_tool_call_cap_exceeded" in (m.content or "")
    ]
    assert len(cap_payloads) == 1
    assert cap_payloads[0]["error"] == "local_tool_call_cap_exceeded"


async def test_a_callable_tool_source_is_re_read_after_every_local_call():
    first, _ = _local("step_one")
    second, _ = _local("step_two")
    sets = [first, second, second]
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="step_one", arguments={})],
        [TokenEvent(text="done")],
    ])
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([]),
        provider_manager=_FakeProviderManager(),
        executor_registry=ExecutorRegistry(),
    )
    seen: list[list[str]] = []
    original = llm.generate_with_tools

    async def _record(messages, schemas, tool_choice=None):
        seen.append([s["name"] for s in (schemas or [])])
        async for e in original(messages, schemas, tool_choice=tool_choice):
            yield e

    llm.generate_with_tools = _record

    [e async for e in orchestrator.run_turn(
        "agent1", "t1", "c1", "s1", [ChatMessage(role="user", content="hi")],
        local_tools=lambda: sets.pop(0),
    )]

    assert seen[0] == ["step_one"]
    assert seen[1] == ["step_two"]


async def test_a_local_tool_that_raises_soft_fails_instead_of_crashing_the_turn():
    async def boom(_args: dict) -> ToolResult:
        raise RuntimeError("transition exploded")

    definition = ToolDefinition(
        name="caller_verified", description="x",
        parameters_schema={"type": "object", "properties": {}},
    )
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="caller_verified", arguments={})],
        [TokenEvent(text="Still here.")],
    ])
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([]),
        provider_manager=_FakeProviderManager(),
        executor_registry=ExecutorRegistry(),
    )
    history = [ChatMessage(role="user", content="hi")]
    events = [
        e async for e in orchestrator.run_turn(
            "agent1", "t1", "c1", "s1", history,
            local_tools={"caller_verified": (definition, boom)},
        )
    ]

    assert LocalToolCompletedEvent(tool_name="caller_verified") in events
    assert TokenEvent(text="Still here.") in events
    assert json.loads(history[-1].content)["status"] == "failed"
    assert json.loads(history[-1].content)["error"] == "local_tool_failed"


async def test_cancel_event_stops_waiting_on_an_in_flight_local_tool():
    started = asyncio.Event()
    release = asyncio.Event()
    finished: list[str] = []

    async def slow(_args: dict) -> ToolResult:
        started.set()
        try:
            await release.wait()
            finished.append("ok")
            return ToolResult(status=ToolStatus.SUCCESS, payload={"done": True})
        except asyncio.CancelledError:
            finished.append("cancelled")
            raise

    definition = ToolDefinition(
        name="caller_verified", description="x",
        parameters_schema={"type": "object", "properties": {}},
    )
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="caller_verified", arguments={})],
    ])
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([]),
        provider_manager=_FakeProviderManager(),
        executor_registry=ExecutorRegistry(),
    )
    cancel_event = asyncio.Event()
    history = [ChatMessage(role="user", content="hi")]

    async def _collect():
        return [
            e async for e in orchestrator.run_turn(
                "agent1", "t1", "c1", "s1", history,
                cancel_event=cancel_event,
                local_tools={"caller_verified": (definition, slow)},
            )
        ]

    task = asyncio.ensure_future(_collect())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert not task.done()

    cancel_event.set()
    events = await asyncio.wait_for(task, timeout=1.0)

    assert events == []
    assert json.loads(history[-1].content)["error"] == "cancelled"
    assert llm.call_count == 1

    release.set()
    await asyncio.sleep(0)
    assert finished == ["cancelled"]


async def test_local_tool_cancel_path_rethrows_outer_cancellation():
    """Awaiting the cancelled handler must not swallow teardown of the turn task."""
    started = asyncio.Event()

    async def slow(_args: dict) -> ToolResult:
        started.set()
        await asyncio.Event().wait()
        return ToolResult(status=ToolStatus.SUCCESS, payload={})

    definition = ToolDefinition(
        name="caller_verified", description="x",
        parameters_schema={"type": "object", "properties": {}},
    )
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="caller_verified", arguments={})],
    ])
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([]),
        provider_manager=_FakeProviderManager(),
        executor_registry=ExecutorRegistry(),
    )
    cancel_event = asyncio.Event()
    history = [ChatMessage(role="user", content="hi")]

    async def _collect():
        return [
            e async for e in orchestrator.run_turn(
                "agent1", "t1", "c1", "s1", history,
                cancel_event=cancel_event,
                local_tools={"caller_verified": (definition, slow)},
            )
        ]

    task = asyncio.ensure_future(_collect())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    cancel_event.set()
    # Cancel the turn task while it's awaiting the cancelled handler.
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_only_tools_is_passed_through_to_the_policy_resolver():
    llm = _ScriptedLLM([[TokenEvent(text="hi")]])
    resolver = _FakePolicyResolver([_policy()])
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=resolver,
        provider_manager=_FakeProviderManager(),
        executor_registry=ExecutorRegistry(),
    )

    [e async for e in orchestrator.run_turn(
        "agent1", "t1", "c1", "s1", [ChatMessage(role="user", content="hi")],
        only_tools=["execute_api"],
    )]

    assert resolver.last_only == ["execute_api"]


async def test_past_remote_cap_hallucinated_remote_does_not_execute():
    local_tools, _ = _local()
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="execute_api",
                       arguments={"requested_datetime": "2026-01-01T10:00:00Z"})],
        [ToolCallEvent(tool_call_id="c2", tool_name="execute_api",
                       arguments={"requested_datetime": "2026-01-01T11:00:00Z"})],
        [TokenEvent(text="ok")],
    ])
    executor = _FixedExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={"booked": True}))
    registry = ExecutorRegistry()
    registry.register("execute_api", lambda provider: executor)
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([_policy()]),
        provider_manager=_FakeProviderManager(),
        executor_registry=registry,
        max_tool_iterations=1,
    )
    history = [ChatMessage(role="user", content="book me")]

    [e async for e in orchestrator.run_turn(
        "agent1", "t1", "c1", "s1", history, local_tools=local_tools,
    )]

    assert len(executor.calls) == 1
    assert json.loads(history[-1].content)["error"] == "unknown_tool"


async def test_local_tool_lookup_uses_definition_name_not_dict_key():
    calls: list[dict] = []

    async def handler(args: dict) -> ToolResult:
        calls.append(args)
        return ToolResult(status=ToolStatus.SUCCESS, payload={"status": "done"})

    definition = ToolDefinition(
        name="caller_verified", description="x",
        parameters_schema={"type": "object", "properties": {}},
    )
    local_tools = {"wrong_key": (definition, handler)}
    llm = _ScriptedLLM([
        [ToolCallEvent(tool_call_id="c1", tool_name="caller_verified", arguments={})],
        [TokenEvent(text="Thanks!")],
    ])
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=_FakePolicyResolver([]),
        provider_manager=_FakeProviderManager(),
        executor_registry=ExecutorRegistry(),
    )

    events = [
        e async for e in orchestrator.run_turn(
            "agent1", "t1", "c1", "s1",
            [ChatMessage(role="user", content="hi")], local_tools=local_tools,
        )
    ]

    assert calls == [{}]
    assert LocalToolCompletedEvent(tool_name="caller_verified") in events
    assert TokenEvent(text="Thanks!") in events
