"""
ToolCallOrchestrator — joins LLMAdapter with ToolPolicyResolver /
ExecutorRegistry / ToolProviderManager. Provider-agnostic: no tool names
hardcoded here.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Awaitable, Callable, TypeAlias

from ..metrics import IMetrics
from ..providers.interfaces import ChatMessage
from .executor_registry import ExecutorRegistry
from .llm_adapter import (
    DeterministicSpokenEvent,
    LLMAdapter,
    LocalToolCompletedEvent,
    TokenEvent,
    ToolCallEvent,
    ToolCallStartedEvent,
    TurnEvent,
)
from .middleware import build_default_chain
from .policy_resolver import ToolPolicyResolver
from .provider_manager import ToolProviderManager
from .types import ToolDefinition, ToolExecutionContext, ToolExecutionRequest, ToolResult, ToolStatus

log = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_MS = 6000
DEFAULT_MAX_TOOL_ITERATIONS = 2
# Separate from remote iterations: a cyclic workflow can otherwise spin forever inside one turn.
DEFAULT_MAX_LOCAL_TOOL_CALLS = 8

# In-process tools (no policy/provider/circuit breaker). Callable source = re-read mid-turn.
LocalTools: TypeAlias = dict[
    str, tuple[ToolDefinition, Callable[[dict[str, Any]], Awaitable[ToolResult]]]
]
LocalToolsSource: TypeAlias = LocalTools | Callable[[], LocalTools] | None


class ToolCallOrchestrator:
    def __init__(
        self,
        llm_adapter:       LLMAdapter,
        policy_resolver:   ToolPolicyResolver,
        provider_manager:  ToolProviderManager,
        executor_registry: ExecutorRegistry,
        metrics:           IMetrics | None = None,
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
        max_local_tool_calls: int = DEFAULT_MAX_LOCAL_TOOL_CALLS,
    ) -> None:
        self._llm_adapter = llm_adapter
        self._policy_resolver = policy_resolver
        self._provider_manager = provider_manager
        self._executor_registry = executor_registry
        self._metrics = metrics
        self._max_tool_iterations = max_tool_iterations
        self._max_local_tool_calls = max_local_tool_calls

    async def run_turn(
        self, agent_id: str, tenant_id: str, call_id: str, session_id: str, history: list[ChatMessage],
        caller_number: str = "", cancel_event: "asyncio.Event | None" = None,
        force_tool_name: str | None = None, phone_number_confirmed: bool = False,
        local_tools: LocalToolsSource = None,
        only_tools: list[str] | Callable[[], list[str] | None] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        """only_tools subsets DB tools for this turn (never grants). Both tool
        args may be callables so a mid-turn node change re-resolves schemas."""
        local_calls = 0
        seen_local_names: set[str] = set()

        async def resolve() -> tuple[LocalTools, dict, list[dict] | None, list[dict]]:
            raw = local_tools() if callable(local_tools) else (local_tools or {})
            # Key by definition.name so lookup matches LLM schema names.
            local = {defn.name: (defn, handler) for defn, handler in raw.values()}
            seen_local_names.update(local)
            if local_calls >= self._max_local_tool_calls:
                local = {}
            if iteration >= self._max_tool_iterations:
                policies: list = []  # yank remotes for real, not only from schemas
            else:
                only = only_tools() if callable(only_tools) else only_tools
                policies = await self._policy_resolver.enabled_tools(agent_id, only=only)
            llm_schemas = [
                p.definition.to_generic_schema() for p in policies if p.definition.llm_visible
            ]
            local_schemas = [d.to_generic_schema() for d, _ in local.values()]
            schemas = llm_schemas + local_schemas
            return local, {p.definition.name: p for p in policies}, (schemas or None), local_schemas

        # iteration is read by resolve(); start at 0 before first resolve.
        turn_id = str(uuid.uuid4())
        iteration = 0
        local, policies_by_name, schemas, local_schemas = await resolve()

        # force_tool_name applies to the first generate() only.
        tool_choice = (
            {"type": "function", "function": {"name": force_tool_name}}
            if force_tool_name else None
        )

        while True:
            tool_call_happened = False
            this_call_tool_choice, tool_choice = tool_choice, None
            async for event in self._llm_adapter.generate(history, schemas, tool_choice=this_call_tool_choice):
                if isinstance(event, TokenEvent):
                    yield event
                    continue

                assert isinstance(event, ToolCallEvent)
                tool_call_happened = True

                local_entry = local.get(event.tool_name)
                if local_entry is not None:
                    local_calls += 1
                    result = await _execute_local_tool(
                        event.tool_name, local_entry[1], event.arguments, cancel_event,
                    )
                    if not (result.status == ToolStatus.FAILED and result.error == "cancelled"):
                        yield LocalToolCompletedEvent(tool_name=event.tool_name)
                        # Re-resolve: a local tool may have moved the workflow node.
                        local, policies_by_name, schemas, local_schemas = await resolve()
                elif (
                    local_calls >= self._max_local_tool_calls
                    and event.tool_name in seen_local_names
                ):
                    # Cap yank emptied `local`; do not burn a remote iteration on the miss.
                    result = ToolResult(
                        status=ToolStatus.FAILED, error="local_tool_call_cap_exceeded",
                    )
                else:
                    iteration += 1  # remote only — locals must not burn this budget
                    yield ToolCallStartedEvent(tool_name=event.tool_name)
                    result = await self._execute_tool_call(
                        event, policies_by_name, tenant_id, agent_id, call_id, session_id, turn_id,
                        iteration, caller_number, cancel_event, phone_number_confirmed,
                    )
                _fold_tool_result_into_history(history, event, result)
                if result.deterministic_response is not None:
                    yield DeterministicSpokenEvent(
                        text=result.deterministic_response, confirmed_datetime=result.confirmed_datetime,
                    )
                    return
                if result.status == ToolStatus.FAILED and result.error == "cancelled":
                    return
                break

            if not tool_call_happened:
                return

            if iteration >= self._max_tool_iterations:
                local, policies_by_name, schemas, local_schemas = await resolve()

    async def _execute_tool_call(
        self, event: ToolCallEvent, policies_by_name: dict, tenant_id: str, agent_id: str,
        call_id: str, session_id: str, turn_id: str, iteration: int, caller_number: str = "",
        cancel_event: "asyncio.Event | None" = None, phone_number_confirmed: bool = False,
    ) -> ToolResult:
        policy = policies_by_name.get(event.tool_name)
        if policy is None:
            log.warning("ToolCallOrchestrator: LLM called unoffered tool_name=%r", event.tool_name)
            return ToolResult(status=ToolStatus.FAILED, error="unknown_tool")

        try:
            provider = await self._provider_manager.get(policy)
        except Exception:
            log.exception("ToolCallOrchestrator: provider construction failed tool=%s", event.tool_name)
            return ToolResult(status=ToolStatus.FAILED, error="provider_unavailable")

        companion = None
        companion_tool_name = policy.definition.companion_tool_name
        if companion_tool_name is not None:
            companion_policy = policies_by_name.get(companion_tool_name)
            if companion_policy is not None:
                try:
                    companion = await self._provider_manager.get(companion_policy)
                except Exception:
                    log.exception(
                        "ToolCallOrchestrator: companion provider construction failed tool=%s companion=%s",
                        event.tool_name, companion_tool_name,
                    )

        executor = self._executor_registry.resolve(event.tool_name, provider, companion)
        if executor is None:
            log.error("ToolCallOrchestrator: no executor registered for tool_name=%r", event.tool_name)
            return ToolResult(status=ToolStatus.FAILED, error="no_executor_registered")

        timeout_ms = policy.timeout_ms or DEFAULT_TOOL_TIMEOUT_MS
        chain = build_default_chain(
            executor, timeout_ms=timeout_ms, metrics=self._metrics,
            redact_arg_keys=policy.sensitive_arg_keys,
        )

        request = ToolExecutionRequest(
            tool_call_id=event.tool_call_id,
            tool_name=event.tool_name,
            arguments=event.arguments,
            context=ToolExecutionContext(
                tenant_id=tenant_id, agent_id=agent_id, call_id=call_id, session_id=session_id,
                turn_id=turn_id, tool_iteration=iteration,
                deadline=time.monotonic() + timeout_ms / 1000,
                request_id=str(uuid.uuid4()),
                caller_number=caller_number,
                phone_number_confirmed=phone_number_confirmed,
                max_chain_depth=policy.max_chain_depth,
            ),
        )

        if cancel_event is None:
            return await chain.execute(request)

        # Race cancel: stop waiting on barge-in; leave the request running (may still land server-side).
        execute_task = asyncio.ensure_future(chain.execute(request))
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        try:
            done, _ = await asyncio.wait({execute_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
            if execute_task in done:
                return execute_task.result()
            log.info(
                "ToolCallOrchestrator: caller interrupted mid tool_call=%r — no longer waiting on its result",
                event.tool_name,
            )
            return ToolResult(status=ToolStatus.FAILED, error="cancelled")
        finally:
            cancel_task.cancel()
            if not execute_task.done():
                execute_task.add_done_callback(_log_background_tool_result)


def _log_background_tool_result(task: "asyncio.Task[ToolResult]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("ToolCallOrchestrator: backgrounded tool call (post-cancel) failed: %r", exc)


async def _invoke_local_handler(
    tool_name: str, handler: Callable[[dict[str, Any]], Awaitable[ToolResult]], arguments: dict[str, Any],
) -> ToolResult:
    """Raised local handlers become FAILED — do not take down the turn."""
    try:
        return await handler(arguments)
    except Exception:
        log.exception("ToolCallOrchestrator: local tool raised tool_name=%r", tool_name)
        return ToolResult(status=ToolStatus.FAILED, error="local_tool_failed")


async def _execute_local_tool(
    tool_name: str,
    handler: Callable[[dict[str, Any]], Awaitable[ToolResult]],
    arguments: dict[str, Any],
    cancel_event: asyncio.Event | None,
) -> ToolResult:
    """Race cancel, but never leave the handler running — workflow transitions
    mutate call state; a background completion would desync history vs node."""
    if cancel_event is None:
        return await _invoke_local_handler(tool_name, handler, arguments)

    execute_task = asyncio.ensure_future(_invoke_local_handler(tool_name, handler, arguments))
    cancel_task = asyncio.ensure_future(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({execute_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        if execute_task in done:
            return execute_task.result()
        execute_task.cancel()
        try:
            await execute_task
        except asyncio.CancelledError:
            # Bare `pass` swallows call-teardown cancellation of *this*
            # coroutine. Only absorb the cancel we sent to execute_task.
            me = asyncio.current_task()
            if me is not None and me.cancelling():
                raise
            if not execute_task.cancelled():
                raise
        if execute_task.cancelled():
            log.info(
                "ToolCallOrchestrator: caller interrupted mid local tool_call=%r — handler cancelled",
                tool_name,
            )
            return ToolResult(status=ToolStatus.FAILED, error="cancelled")
        # Handler finished before cancel took effect — keep its result.
        return execute_task.result()
    finally:
        cancel_task.cancel()


def _fold_tool_result_into_history(history: list[ChatMessage], event: ToolCallEvent, result: ToolResult) -> None:
    """Append assistant tool_call + tool result in the generic ChatMessage shape."""
    import json

    call_dict: dict[str, Any] = {"id": event.tool_call_id, "name": event.tool_name, "arguments": event.arguments}
    if event.provider_metadata:
        call_dict["provider_metadata"] = event.provider_metadata
    history.append(ChatMessage(role="assistant", content="", tool_calls=[call_dict]))
    payload: dict[str, Any] = {"status": result.status.value, **result.payload}
    if result.error:
        payload["error"] = result.error
    history.append(ChatMessage(
        role="tool", content=json.dumps(payload), tool_call_id=event.tool_call_id,
    ))
