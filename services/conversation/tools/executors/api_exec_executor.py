"""
ApiExecExecutor — the only IToolExecutor the LLM's execute_api tool call
ever reaches; the single seam between the real-time turn and Tool
Execution Service's outbound HTTP chain (services/toolexec/). Posts one
request via ToolExecClient and maps the response back onto the shared
ToolResult/ToolStatus vocabulary every other executor already uses —
ToolCallOrchestrator sees exactly one execute(request) -> ToolResult call,
identical in shape to any other tool.
"""

from __future__ import annotations

import logging
import time

from ..types import ToolExecutionRequest, ToolResult, ToolStatus
from ..providers.toolexec.client import ToolExecClient

log = logging.getLogger(__name__)

# graph.MAX_CHAIN_LEVELS in services/toolexec/graph.py — the platform
# ceiling this client-side value can only ever LOWER, never raise (the
# server clamps to min(request.max_chain_depth, MAX_CHAIN_LEVELS) itself).
_PLATFORM_MAX_CHAIN_DEPTH = 4

# chain_status -> ToolStatus, 1:1 except "partial" (see execute() below).
_STATUS_MAP: dict[str, ToolStatus] = {
    "success": ToolStatus.SUCCESS,
    "failed": ToolStatus.FAILED,
    "timeout": ToolStatus.TIMEOUT,
    "invalid_argument": ToolStatus.INVALID_ARGUMENT,
    "unavailable": ToolStatus.UNAVAILABLE,
    "rate_limited": ToolStatus.RATE_LIMITED,
}


class ApiExecExecutor:
    def __init__(self, client: ToolExecClient) -> None:
        self._client = client

    async def execute(self, request: ToolExecutionRequest) -> ToolResult:
        # Read per-call, from THIS agent's resolved execute_api policy
        # (orchestrator.py threads policy.max_chain_depth into
        # ToolExecutionContext) — never baked into this executor at
        # construction time, since ExecutorRegistry builds one factory
        # once at process startup, shared by every tenant/agent that
        # calls execute_api. None = platform default; a configured value
        # can only lower it, never exceed the platform ceiling (mirrors
        # agent_tool_policies.max_chain_depth's own NULL-means-default
        # contract, and agent_apis._effective_max_chain_depth's identical
        # clamp on the toolexec side).
        max_chain_depth = min(request.context.max_chain_depth or _PLATFORM_MAX_CHAIN_DEPTH, _PLATFORM_MAX_CHAIN_DEPTH)
        api_name = request.arguments.get("api_name")
        if not api_name:
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, error="missing_api_name")

        # The whole chain is bounded by the *existing* timeout_ms budget
        # TimeoutMiddleware already enforces for this tool call (see
        # ToolExecutionContext.deadline, set from that same budget in
        # orchestrator.py) — this composes with the old ceiling instead of
        # bypassing it. Floored at 0: a deadline already passed must still
        # produce a valid (if hopeless) request, never a negative budget.
        chain_budget_ms = max(int((request.context.deadline - time.monotonic()) * 1000), 0)

        body = {
            "tenant_id": request.context.tenant_id,
            "agent_id": request.context.agent_id,
            "call_id": request.context.call_id,
            "session_id": request.context.session_id,
            "turn_id": request.context.turn_id,
            "tool_call_id": request.tool_call_id,
            "idempotency_key": request.idempotency_key,
            "api_name": api_name,
            "caller_arguments": request.arguments.get("inputs") or {},
            "chain_budget_ms": chain_budget_ms,
            "max_chain_depth": max_chain_depth,
        }

        try:
            response = await self._client.execute_chain(body)
        except Exception:
            log.exception("ApiExecExecutor: execute_chain failed api_name=%s", api_name)
            return ToolResult(status=ToolStatus.FAILED, error="toolexec_unavailable")

        chain_status = response.get("chain_status")
        status = _STATUS_MAP.get(chain_status, ToolStatus.FAILED)
        payload: dict = {}
        if chain_status == "partial":
            # No direct ToolStatus counterpart — mapped to FAILED so the
            # LLM never treats it as a success, with payload["partial"]
            # flagging it distinct from an ordinary failure.
            status = ToolStatus.FAILED
            payload["partial"] = True
        elif status is ToolStatus.SUCCESS:
            # Only the redacted `data` projection reaches the LLM/log —
            # never steps/completed_steps/failed_step.
            payload = dict(response.get("data") or {})
        elif status is ToolStatus.INVALID_ARGUMENT:
            # Names the gap (e.g. a missing order id) so the LLM can ask
            # the one question that would complete the task, same shape
            # every other executor's missing_fields payload already uses.
            payload["missing_fields"] = response.get("missing_fields") or []

        return ToolResult(
            status=status,
            payload=payload,
            error=response.get("error"),
            deterministic_response=response.get("deterministic_response"),
        )
