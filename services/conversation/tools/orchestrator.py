"""ToolCallOrchestrator — joins LLMAdapter with policy resolution, providers and executors.
Provider-agnostic: no tool names hardcoded here."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace
from typing import Any, AsyncGenerator, Awaitable, Callable, TypeAlias

from ..metrics import IMetrics
from ..providers.interfaces import ChatMessage, with_directive
from ..tool_latency import ToolLatencyStore
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
from .envelope import build_envelope
from .guard import (
    DEFAULT_MAX_TOOL_CALLS_PER_TURN, LastGoodCache, TurnGuard, call_signature, scrub_scope_args, validate_json,
)
from .middleware import BreakerBoard, _redact, build_default_chain
from .policy_resolver import ToolPolicyResolver
from .provider_manager import ToolProviderManager
from .types import ToolDefinition, ToolExecutionContext, ToolExecutionRequest, ToolResult, ToolStatus

log = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_MS = 6000
DEFAULT_MAX_TOOL_ITERATIONS = 2
# Separate from remote iterations: a cyclic workflow can otherwise spin forever inside one turn.
DEFAULT_MAX_LOCAL_TOOL_CALLS = 8
# A tool still running after this long gets a spoken filler (ToolCallStartedEvent -> the pipeline's
# filler path). Remote tools signal at dispatch instead, sized by their measured latency.
SLOW_TOOL_FILLER_AFTER_S = 0.8
_HELD_WRITE_TTL_S = 600.0   # a held write can be confirmed for this long
_MAX_HELD_WRITES = 16
_MAX_TRACKED_CALLS = 256

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
        latency_store: ToolLatencyStore | None = None,
        max_tool_calls_per_turn: int = DEFAULT_MAX_TOOL_CALLS_PER_TURN,
        breakers: BreakerBoard | None = None,
        last_good: LastGoodCache | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._llm_adapter = llm_adapter
        self._policy_resolver = policy_resolver
        self._provider_manager = provider_manager
        self._executor_registry = executor_registry
        self._metrics = metrics
        self._max_tool_iterations = max_tool_iterations
        self._max_local_tool_calls = max_local_tool_calls
        self._latency_store = latency_store
        self._max_tool_calls_per_turn = max_tool_calls_per_turn
        # An orchestrator is built per call, so these only outlive a call if the caller injects shared
        # instances (services/conversation/__main__.py does). The defaults are per-orchestrator.
        self._breakers = breakers if breakers is not None else BreakerBoard()
        self._last_good = last_good if last_good is not None else LastGoodCache()
        # Per call (the orchestrator is built per call), keyed (tenant, call_id) so a reused instance cannot
        # mix calls: whether retrieved text is still in context, and writes held for the caller's yes.
        self._clock = clock  # injectable so the held-write expiry can be tested without sleeping
        self._call_taint: dict[tuple[str, str], bool] = {}
        self._held_writes: dict[tuple[str, str], dict[str, tuple[str, float]]] = {}

    async def run_turn(
        self, agent_id: str, tenant_id: str, call_id: str, session_id: str, history: list[ChatMessage],
        caller_number: str = "", called_number: str = "", call_direction: str = "",
        cancel_event: "asyncio.Event | None" = None,
        force_tool_name: str | None = None,
        local_tools: LocalToolsSource = None,
        only_tools: list[str] | Callable[[], list[str] | None] | None = None,
        carry_taint: bool = False,
        trailing_message: ChatMessage | None = None,
        pre_tainted: bool = False,
    ) -> AsyncGenerator[TurnEvent, None]:
        """only_tools subsets DB tools for this turn (never grants). Both tool
        args may be callables so a mid-turn node change re-resolves schemas.
        carry_taint: this turn is not a new caller utterance (e.g. recovery after a failed transfer), so
        retrieved text read earlier is still in context and the held writes are not yet confirmed.
        trailing_message is sent last on every LLM call of the turn but never stored in history.
        pre_tainted: retrieved, tenant-uploaded text was put in front of the model before the turn started (no
        search_knowledge call ran to taint it), so writes are held for the caller's yes exactly as after a lookup."""
        local_calls = 0
        guard = TurnGuard(self._max_tool_calls_per_turn)
        call_key = (tenant_id, call_id)
        guard.tainted = (carry_taint and self._call_taint.get(call_key, False)) or pre_tainted
        self._call_taint[call_key] = guard.tainted
        if len(self._call_taint) > _MAX_TRACKED_CALLS:
            self._call_taint.pop(next(iter(self._call_taint)))
        seen_local_names: set[str] = set()

        async def resolve() -> tuple[LocalTools, dict, list[dict] | None, list[dict]]:
            raw = local_tools() if callable(local_tools) else (local_tools or {})
            # Key by definition.name so lookup matches LLM schema names.
            local = {defn.name: (defn, handler) for defn, handler in raw.values()}
            seen_local_names.update(local)
            if local_calls >= self._max_local_tool_calls or guard.exhausted:
                local = {}
            if iteration >= self._max_tool_iterations or guard.exhausted:
                policies: list = []  # yank remotes for real, not only from schemas
            else:
                only = only_tools() if callable(only_tools) else only_tools
                policies = await self._policy_resolver.enabled_tools(agent_id, tenant_id, only=only)
                if guard.taint_refused:
                    # A write was held: only tools known to be read-only stay, so the model must ask the caller.
                    policies = [p for p in policies if p.definition.read_only]
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

        # Offered-but-not-called is a prompt problem; nothing offered is a config problem.
        log.info(
            "tools offered agent=%s turn=%s: remote=%s local=%s%s",
            agent_id, turn_id,
            [p.definition.name for p in policies_by_name.values() if p.definition.llm_visible] or "NONE",
            [d.name for d, _ in local.values()] or "NONE",
            "" if schemas else "  (no schemas sent — the LLM cannot call anything this turn)",
        )

        # force_tool_name applies to the first generate() only.
        tool_choice = (
            {"type": "function", "function": {"name": force_tool_name}}
            if force_tool_name else None
        )

        while True:
            tool_call_happened = False
            this_call_tool_choice, tool_choice = tool_choice, None
            async for event in self._llm_adapter.generate(
                with_directive(history, trailing_message), schemas, tool_choice=this_call_tool_choice):
                if isinstance(event, TokenEvent):
                    yield event
                    continue

                assert isinstance(event, ToolCallEvent)
                tool_call_happened = True
                call_t0 = time.monotonic()
                trace_id = uuid.uuid4().hex[:16]
                # The model supplies arguments, never scope: tenant/agent/KB ids come from the session.
                clean_args, dropped = scrub_scope_args(event.arguments)
                if dropped:
                    log.warning(
                        "tool call turn=%s tool=%s: ignored model-supplied scope args %s",
                        turn_id, event.tool_name, dropped,
                    )
                    event = replace(event, arguments=clean_args)
                fixed_args = strip_api_namespace(event.tool_name, event.arguments)
                if fixed_args is not event.arguments:
                    event = replace(event, arguments=fixed_args)
                log.info(
                    "tool call turn=%s: %s(%s)",
                    turn_id, event.tool_name,
                    ", ".join(sorted(event.arguments)) if isinstance(event.arguments, dict) else "<not an object>",
                )

                local_entry = local.get(event.tool_name)
                policy = policies_by_name.get(event.tool_name)
                definition = local_entry[0] if local_entry is not None else (
                    policy.definition if policy is not None else None
                )
                if (
                    local_entry is not None and isinstance(event.arguments, dict) and event.arguments
                    and local_entry[0].parameters_schema.get("properties") == {}
                    and local_entry[0].parameters_schema.get("additionalProperties") is not True
                ):
                    # A tool that declares no parameters takes none: whatever the model stuffed in (caller PII
                    # included) is dropped here rather than accepted, passed on and logged.
                    log.warning("tool call turn=%s tool=%s: dropped undeclared args %s",
                                turn_id, event.tool_name, sorted(event.arguments))
                    event = replace(event, arguments={})
                side_effecting = _is_side_effecting(event, policy, definition) if local_entry is None else False
                signature = call_signature(event.tool_name, event.arguments)
                held = self._held_writes.get(call_key, {})
                held_turn = held.get(signature)
                # Confirmed = this exact write was held on an earlier turn and this is a real new utterance.
                confirmed = (
                    side_effecting and not carry_taint and held_turn is not None
                    and held_turn[0] != turn_id and self._clock() - held_turn[1] < _HELD_WRITE_TTL_S
                )
                refusal = guard.check(
                    event.tool_name, event.arguments,
                    definition.parameters_schema if definition is not None else None,
                    policy.max_calls_per_turn if policy is not None else None,
                    # A local workflow tool may legitimately repeat (a cyclic node); max_local_tool_calls bounds it.
                    detect_repeats=policy is not None or (local_entry is not None and local_entry[0].read_only),
                    side_effecting=side_effecting, confirmed=confirmed,
                )
                if refusal is not None and refusal.error == "needs_caller_confirmation":
                    held = self._held_writes.setdefault(call_key, {})
                    held[signature] = (turn_id, self._clock())
                    while len(held) > _MAX_HELD_WRITES:
                        held.pop(next(iter(held)))
                elif refusal is None and side_effecting and held_turn is not None:
                    held.pop(signature, None)  # a held write is released once, whether or not it was tainted
                if refusal is not None:
                    log.warning("tool call refused turn=%s tool=%s error=%s", turn_id, event.tool_name, refusal.error)
                    result = refusal
                elif local_entry is not None:
                    local_calls += 1
                    local_task = asyncio.ensure_future(self._call_local(
                        tenant_id, agent_id, call_id, local_entry[0], local_entry[1], event.arguments, cancel_event,
                    ))
                    try:
                        done, _ = await asyncio.wait({local_task}, timeout=SLOW_TOOL_FILLER_AFTER_S)
                        if not done:
                            # Progress signal only: it must not count as a tool call made (see ToolCallStartedEvent).
                            yield ToolCallStartedEvent(tool_name=event.tool_name, filler_only=True)
                        result = await local_task
                    except BaseException:
                        local_task.cancel()
                        raise
                    if (
                        local_entry[0].untrusted_output and result.status == ToolStatus.SUCCESS
                        and result.payload.get("found")
                    ):
                        guard.tainted = True
                        self._call_taint[call_key] = True
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
                    # Start before yielding: the yield suspends until the filler finishes speaking.
                    execute_task = asyncio.ensure_future(self._execute_tool_call(
                        event, policies_by_name, tenant_id, agent_id, call_id, session_id, turn_id,
                        iteration, caller_number, called_number, call_direction, cancel_event, trace_id,
                    ))
                    try:
                        yield ToolCallStartedEvent(tool_name=event.tool_name)
                        result = await execute_task
                    except BaseException:
                        execute_task.cancel()
                        raise
                latency_ms = (time.monotonic() - call_t0) * 1000
                read_only = definition.read_only if definition is not None else False
                # Local tools have no policy row to say which values are sensitive, and their arguments are the
                # caller's own words (a search query), so only the key names are logged.
                logged_args = (
                    _redact(event.arguments, policy.sensitive_arg_keys) if policy is not None
                    else sorted(event.arguments) if isinstance(event.arguments, dict) else "<not an object>"
                )
                log.info(
                    "tool_call result turn=%s tool=%s tenant=%s agent=%s status=%s error=%s latency_ms=%d "
                    "trace_id=%s args=%r",
                    turn_id, event.tool_name, tenant_id, agent_id, result.status.value, result.error,
                    latency_ms, trace_id, logged_args,
                )
                _fold_tool_result_into_history(
                    history, event, result, read_only=read_only, latency_ms=latency_ms, trace_id=trace_id,
                )
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

            if iteration >= self._max_tool_iterations or guard.exhausted or guard.taint_refused:
                local, policies_by_name, schemas, local_schemas = await resolve()

    async def _call_local(
        self, tenant_id: str, agent_id: str, call_id: str, definition: ToolDefinition,
        handler: Callable[[dict[str, Any]], Awaitable[ToolResult]], arguments: dict[str, Any],
        cancel_event: "asyncio.Event | None",
    ) -> ToolResult:
        """A local tool under the same envelope rules as a remote one: timeout, output validation and,
        for read-only tools, one retry, a breaker, and a last-good fallback."""
        name = definition.name
        if not definition.read_only:
            result = await _execute_local_tool(name, handler, arguments, cancel_event, definition.timeout_ms)
            return _validated_output(definition, result)

        # Per agent: retrieval runs with each agent's own knowledge bases and embedding config, so one
        # agent's broken embedder must not open the tool for the tenant's other agents.
        breaker_key = (tenant_id, name, agent_id)
        cache_key = LastGoodCache.key(tenant_id, agent_id, call_id, name, arguments)
        admission = self._breakers.admit(breaker_key)
        if admission is None:
            log.warning("tool_call circuit_breaker_open tool=%s", name)
            return self._fallback(cache_key, ToolResult(status=ToolStatus.UNAVAILABLE, error="circuit_breaker_open"))

        try:
            result = await _execute_local_tool(name, handler, arguments, cancel_event, definition.timeout_ms)
            if result.status == ToolStatus.FAILED and result.error != "cancelled":
                # Fast transient failure only: a timeout already spent its budget and is not retried.
                log.info("tool_call retry tool=%s", name)
                result = await _execute_local_tool(name, handler, arguments, cancel_event, definition.timeout_ms)
        except BaseException:
            self._breakers.abandon(breaker_key, admission)
            raise
        result = _validated_output(definition, result)

        if result.error == "cancelled":
            self._breakers.abandon(breaker_key, admission)
            return result
        self._breakers.record(breaker_key, result, admission)
        if result.status == ToolStatus.SUCCESS:
            self._last_good.put(cache_key, result)
            return result
        return self._fallback(cache_key, result)

    def _fallback(self, cache_key: tuple[str, ...], failure: ToolResult) -> ToolResult:
        """Last-good result flagged degraded; failing that, the failure itself (the envelope turns it
        into a sayable message that offers a transfer)."""
        cached = self._last_good.get(cache_key)
        if cached is None:
            return failure
        degraded = [*(cached.payload.get("degraded") or []), "stale_cache"]
        return ToolResult(
            status=ToolStatus.SUCCESS, payload={**cached.payload, "degraded": degraded}, outcome="partial",
        )

    async def _execute_tool_call(
        self, event: ToolCallEvent, policies_by_name: dict, tenant_id: str, agent_id: str,
        call_id: str, session_id: str, turn_id: str, iteration: int, caller_number: str = "",
        called_number: str = "", call_direction: str = "", cancel_event: "asyncio.Event | None" = None,
        trace_id: str = "",
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

        executor = self._executor_registry.resolve(event.tool_name, provider)
        if executor is None:
            log.error("ToolCallOrchestrator: no executor registered for tool_name=%r", event.tool_name)
            return ToolResult(status=ToolStatus.FAILED, error="no_executor_registered")

        timeout_ms = policy.timeout_ms or DEFAULT_TOOL_TIMEOUT_MS
        chain = build_default_chain(
            executor, timeout_ms=timeout_ms, metrics=self._metrics,
            redact_arg_keys=policy.sensitive_arg_keys,
            latency_store=self._latency_store,
            breaker_board=self._breakers,
        )

        request = ToolExecutionRequest(
            tool_call_id=event.tool_call_id,
            tool_name=event.tool_name,
            arguments=event.arguments,
            context=ToolExecutionContext(
                tenant_id=tenant_id, agent_id=agent_id, call_id=call_id, session_id=session_id,
                turn_id=turn_id, tool_iteration=iteration,
                deadline=time.monotonic() + timeout_ms / 1000,
                request_id=trace_id or str(uuid.uuid4()),
                caller_number=caller_number,
                called_number=called_number,
                call_direction=call_direction,
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


_TOOL_NAMESPACE_PREFIX = "functions."


def strip_api_namespace(tool_name: str, arguments: Any) -> Any:
    """OpenAI models sometimes write execute_api's api_name as "functions.<name>", leaking the
    namespace they see their own tools under; the enum then refuses an API the agent has.
    Returns the arguments with the prefix dropped, or the same object when there is none."""
    if tool_name != "execute_api" or not isinstance(arguments, dict):
        return arguments
    api_name = arguments.get("api_name")
    if isinstance(api_name, str) and api_name.startswith(_TOOL_NAMESPACE_PREFIX):
        return {**arguments, "api_name": api_name[len(_TOOL_NAMESPACE_PREFIX):]}
    return arguments


def _is_side_effecting(event: ToolCallEvent, policy: Any, definition: ToolDefinition | None) -> bool:
    """Remote tools write unless known not to. execute_api is judged per API from custom_apis.side_effecting
    (surfaced by the policy resolver); an API missing from that set is treated as a write."""
    if policy is None or (definition is not None and definition.read_only):
        return False
    if event.tool_name == "execute_api" and isinstance(event.arguments, dict):
        return str(event.arguments.get("api_name") or "").lower() not in policy.read_only_apis
    return True


async def _invoke_local_handler(
    tool_name: str, handler: Callable[[dict[str, Any]], Awaitable[ToolResult]], arguments: dict[str, Any],
    timeout_ms: int | None = None,
) -> ToolResult:
    """Raised local handlers become FAILED, and a handler over its budget TIMEOUT — neither takes down the turn."""
    try:
        if timeout_ms is None:
            return await handler(arguments)
        return await asyncio.wait_for(handler(arguments), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError:
        log.warning("ToolCallOrchestrator: local tool timed out tool_name=%r timeout_ms=%d", tool_name, timeout_ms or 0)
        return ToolResult(status=ToolStatus.TIMEOUT, error="timeout")
    except Exception:
        log.exception("ToolCallOrchestrator: local tool raised tool_name=%r", tool_name)
        return ToolResult(status=ToolStatus.FAILED, error="local_tool_failed")


def _validated_output(definition: ToolDefinition, result: ToolResult) -> ToolResult:
    """A payload that breaks the tool's declared output schema is an error, not something to hand the model."""
    if definition.output_schema is None or result.status != ToolStatus.SUCCESS:
        return result
    problems = validate_json(result.payload, definition.output_schema)
    if not problems:
        return result
    log.error("ToolCallOrchestrator: tool_name=%r returned an invalid payload: %s", definition.name, problems)
    return ToolResult(status=ToolStatus.FAILED, error="invalid_tool_output")


async def _execute_local_tool(
    tool_name: str,
    handler: Callable[[dict[str, Any]], Awaitable[ToolResult]],
    arguments: dict[str, Any],
    cancel_event: asyncio.Event | None,
    timeout_ms: int | None = None,
) -> ToolResult:
    """Race cancel, but never leave the handler running — workflow transitions
    mutate call state; a background completion would desync history vs node."""
    if cancel_event is None:
        return await _invoke_local_handler(tool_name, handler, arguments, timeout_ms)

    execute_task = asyncio.ensure_future(_invoke_local_handler(tool_name, handler, arguments, timeout_ms))
    cancel_task = asyncio.ensure_future(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({execute_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        if execute_task in done:
            return execute_task.result()
        execute_task.cancel()
        try:
            await execute_task
        except asyncio.CancelledError:
            # Only absorb the cancel we sent to execute_task, not teardown of this coroutine.
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


def _fold_tool_result_into_history(
    history: list[ChatMessage], event: ToolCallEvent, result: ToolResult,
    *, read_only: bool = False, latency_ms: float = 0.0, trace_id: str = "",
) -> None:
    """Append assistant tool_call + tool result (as the typed envelope) in the generic ChatMessage shape."""
    import json

    call_dict: dict[str, Any] = {"id": event.tool_call_id, "name": event.tool_name, "arguments": event.arguments}
    if event.provider_metadata:
        call_dict["provider_metadata"] = event.provider_metadata
    history.append(ChatMessage(role="assistant", content="", tool_calls=[call_dict]))
    envelope = build_envelope(result, read_only=read_only, latency_ms=latency_ms, trace_id=trace_id)
    history.append(ChatMessage(
        role="tool", content=json.dumps(envelope), tool_call_id=event.tool_call_id,
    ))
