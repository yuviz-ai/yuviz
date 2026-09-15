"""
ToolCallOrchestrator — joins LLMAdapter with ToolPolicyResolver /
ExecutorRegistry / ToolProviderManager. Provider-agnostic: no tool names
hardcoded here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Awaitable, Callable, TypeAlias

from ..metrics import IMetrics
from ..providers.interfaces import ChatMessage
from ..tool_latency import ToolLatencyStore
from .date_sanity import (
    correct_year_if_wrong,
    date_field_for_tool,
    is_in_the_past,
    is_too_far_out,
    no_time_stated,
    parse_requested_date,
    stated_day_mismatch,
)
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
        latency_store: ToolLatencyStore | None = None,
        calendar_timezone: str = "UTC",
    ) -> None:
        self._llm_adapter = llm_adapter
        self._policy_resolver = policy_resolver
        self._provider_manager = provider_manager
        self._executor_registry = executor_registry
        self._metrics = metrics
        self._max_tool_iterations = max_tool_iterations
        self._max_local_tool_calls = max_local_tool_calls
        self._latency_store = latency_store
        self._calendar_timezone = calendar_timezone

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
                    # Cheap, synchronous, sub-millisecond — runs before we
                    # spend a remote iteration or announce a filler. A bad
                    # date is a local rejection, not tool work: the caller
                    # would otherwise hear a filler promising work that
                    # never happens, and two rejections in one turn used to
                    # exhaust DEFAULT_MAX_TOOL_ITERATIONS before the model
                    # ever got a corrected retry.
                    date_check = self._check_requested_date(event, history)
                    if date_check is not None:
                        result = date_check
                    else:
                        iteration += 1  # remote only — locals must not burn this budget
                        # Start the real work before announcing it, not after:
                        # yielding first (as this used to) suspends this
                        # generator until the whole filler finishes
                        # synthesizing, so the tool call didn't actually begin
                        # until the filler was done speaking — the opposite of
                        # "the filler covers the wait." Starting the task first
                        # means the filler genuinely overlaps real work instead
                        # of prepending to it.
                        execute_task = asyncio.ensure_future(self._execute_tool_call(
                            event, policies_by_name, tenant_id, agent_id, call_id, session_id, turn_id,
                            iteration, caller_number, cancel_event, phone_number_confirmed,
                        ))
                        yield ToolCallStartedEvent(tool_name=event.tool_name)
                        result = await execute_task
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
            executor, timeout_ms=timeout_ms, metrics=self._metrics, latency_store=self._latency_store,
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

    def _check_requested_date(
        self, event: ToolCallEvent, history: "list[ChatMessage] | None",
    ) -> ToolResult | None:
        """Runs before the executor/middleware chain — an already-known-bad
        date never spends a real calendar API call. Returns None (proceed
        normally) unless a check fails, in which case it returns the
        ToolResult the tool's own description (registry.py) already tells
        the LLM how to react to. See date_sanity.py's module docstring for
        why these three specific checks and why each is narrow-by-design."""
        date_field = date_field_for_tool(event.tool_name)
        if date_field is None:
            return None
        raw_value = event.arguments.get(date_field)
        dt = parse_requested_date(raw_value)
        if dt is None:
            if raw_value:
                # A present but unparseable value (e.g. a literal
                # placeholder like "YYYY-09-14T14:00:00" instead of a real
                # year) would otherwise reach the executor unvalidated,
                # hit Cal.com's real API, and come back as an opaque
                # calendar_error the model has no actionable way to react
                # to, unlike date_in_past/date_not_confirmed/time_not_confirmed.
                log.warning(
                    "ToolCallOrchestrator: rejected %s — %s=%r is not a valid ISO 8601 "
                    "date/time", event.tool_name, date_field, raw_value,
                )
                return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={
                    "missing_fields": [date_field], "reason": "invalid_date_format",
                })
            return None  # Genuinely absent — the executor's own presence check handles it.

        if is_in_the_past(dt, self._calendar_timezone):
            # Usually a wrong-YEAR computation, not a wrong day/month — the
            # model gets the day-of-month right (already cross-checked
            # below against what the caller said) but guesses an old year.
            # Correct just the year and re-validate instead of rejecting
            # outright, turning that rejection loop into an instant,
            # correct booking.
            corrected = correct_year_if_wrong(dt, self._calendar_timezone)
            if corrected is None:
                log.warning(
                    "ToolCallOrchestrator: rejected %s — %s=%r is in the past",
                    event.tool_name, date_field, event.arguments.get(date_field),
                )
                return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={
                    "missing_fields": [date_field], "reason": "date_in_past",
                })
            log.info(
                "ToolCallOrchestrator: corrected %s year %d -> %d (day/time unchanged) tool=%s",
                date_field, dt.year, corrected.year, event.tool_name,
            )
            event.arguments[date_field] = corrected.isoformat()
            dt = corrected
        elif is_too_far_out(dt, self._calendar_timezone):
            # correct_year_if_wrong only ever rolls a past date forward — a
            # wrong year in the FUTURE (2027 instead of 2026) produces a
            # date that isn't in the past at all, so nothing above catches
            # it, and day-of-month/time can be exactly what the caller said.
            # Without this, that books a year out with no guardrail firing.
            log.warning(
                "ToolCallOrchestrator: rejected %s — %s=%r is too far in the future",
                event.tool_name, date_field, event.arguments.get(date_field),
            )
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={
                "missing_fields": [date_field], "reason": "date_too_far_out",
            })

        # The day/time confirmation checks below are false-positive prone by
        # nature (an unrelated numeral, a slot the agent proposed but the
        # caller didn't literally repeat) — a real caller can get asked the
        # same question a handful of times in unlucky phrasing. Past a small
        # cap, keep asking is worse than the residual risk of a wrong date:
        # let the call through rather than loop the caller forever.
        if _recent_rejection_count(history) >= _MAX_DATE_CONFIRMATION_REJECTIONS:
            log.warning(
                "ToolCallOrchestrator: %s date-confirmation rejection cap reached — letting %s "
                "through unconfirmed rather than loop the caller", event.tool_name, date_field,
            )
            return None

        # Checking only the single last utterance rejects an already-stated,
        # correct date when the caller supplies date and time in separate
        # turns (e.g. "14 September" then "10:30 AM" next) — the last
        # utterance alone has no day-of-month digit to match against even
        # though the caller did state one shortly before. Widening to the
        # last few turns keeps the guardrail's real purpose (catching a
        # wrong day-of-month miscalculation) without punishing a date/time
        # split across turns, which is how people actually talk.
        recent_user_text = _recent_user_text(history)
        if recent_user_text and stated_day_mismatch(dt.day, recent_user_text):
            # Caller utterances are never logged verbatim here — they
            # routinely contain the phone number the agent just asked the
            # caller to state digit by digit, plus names, on a path that
            # fires during normal call recovery, not an edge case.
            log.warning(
                "ToolCallOrchestrator: rejected %s — %s=%r day=%d not found in the caller's "
                "recent utterances (text not logged — caller PII)",
                event.tool_name, date_field, event.arguments.get(date_field), dt.day,
            )
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={
                "missing_fields": [date_field], "reason": "date_not_confirmed",
            })

        if no_time_stated(_time_confirmable_texts(history)):
            log.warning(
                "ToolCallOrchestrator: rejected %s — %s=%r but no time-of-day was ever stated "
                "by the caller this call", event.tool_name, date_field, event.arguments.get(date_field),
            )
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={
                "missing_fields": [date_field], "reason": "time_not_confirmed",
            })
        return None


# A real caller can plausibly get asked to reconfirm a date/time once; a
# third ask in the same call is worse for the caller than the residual risk
# of proceeding unconfirmed. See _check_requested_date's escape valve.
_MAX_DATE_CONFIRMATION_REJECTIONS = 2
_REJECTION_REASONS = frozenset({"date_not_confirmed", "time_not_confirmed"})


def _recent_rejection_count(history: "list[ChatMessage] | None") -> int:
    """Counts prior date/time-confirmation rejections this call by reading
    the tool-result messages _fold_tool_result_into_history already writes
    — no new state to thread through pipeline.py's per-session object."""
    if not history:
        return 0
    count = 0
    for m in history:
        if m.role != "tool":
            continue
        try:
            payload = json.loads(m.content)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("reason") in _REJECTION_REASONS:
            count += 1
    return count


def _all_user_messages(history: "list[ChatMessage] | None") -> list[str]:
    if not history:
        return []
    return [m.content for m in history if m.role == "user"]


def _time_confirmable_texts(history: "list[ChatMessage] | None") -> list[str]:
    """Every caller turn, plus an assistant turn only when immediately
    followed by a user turn — i.e. a time the agent proposed that the
    caller then actually responded to. Never the agent's own open question
    on its own: scanning any assistant text let the model's own remediation
    line ("what time works — morning or afternoon?") permanently satisfy
    this check the instant it was asked, disarming the guardrail on the one
    retry it exists to police. Never system (the composed node prompt may
    itself mention business hours) or tool role content (an available_slots
    payload full of clock times) either — only text a human actually said
    in the conversation counts."""
    if not history:
        return []
    out: list[str] = []
    for i, m in enumerate(history):
        if m.role == "user":
            out.append(m.content)
        elif m.role == "assistant" and i + 1 < len(history) and history[i + 1].role == "user":
            out.append(m.content)
    return out


def _recent_user_text(history: "list[ChatMessage] | None", n: int = 3) -> str:
    """Joins the caller's last n turns, not just the very last one — a
    date is commonly stated in one turn with just the time confirmed in a
    follow-up ("10:30 AM") that alone contains no day-of-month digit."""
    user_texts = _all_user_messages(history)
    return " ".join(user_texts[-n:])


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
