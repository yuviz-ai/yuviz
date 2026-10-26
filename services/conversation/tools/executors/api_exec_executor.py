"""IToolExecutor for execute_api: posts to the Tool Execution Service and maps the response to ToolResult."""

from __future__ import annotations

import logging
import time

from ..types import ToolExecutionRequest, ToolResult, ToolStatus
from ..providers.toolexec.client import ToolExecClient

log = logging.getLogger(__name__)

# Mirrors toolexec graph.MAX_CHAIN_LEVELS; a client value can only lower it (the server clamps too).
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
        # Read per call: one executor factory is shared by every tenant/agent.
        # None = platform default; a configured value can only lower the ceiling.
        max_chain_depth = min(request.context.max_chain_depth or _PLATFORM_MAX_CHAIN_DEPTH, _PLATFORM_MAX_CHAIN_DEPTH)
        api_name = request.arguments.get("api_name")
        if not api_name:
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, error="missing_api_name")

        # Bounded by the tool's existing timeout budget; floored at 0 if the deadline already passed.
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
            # The call's own metadata, verbatim. Which party is "the remote one"
            # is toolexec's decision alone; the model never supplies any of these.
            "caller_number": request.context.caller_number,
            "called_number": request.context.called_number,
            "call_direction": request.context.call_direction,
        }

        try:
            response = await self._client.execute_chain(body)
        except Exception:
            log.exception("ApiExecExecutor: execute_chain failed api_name=%s", api_name)
            return ToolResult(status=ToolStatus.FAILED, error="toolexec_unavailable")

        chain_status = response.get("chain_status")
        status = _STATUS_MAP.get(chain_status, ToolStatus.FAILED)
        payload: dict = {}
        if chain_status == "confirmation_required":
            # The read-back is in deterministic_response and the orchestrator ends the turn
            # on it; the next call is the caller's answer. No _STATUS_MAP entry on purpose: an
            # unknown status falls through to FAILED, and the model would apologise for a
            # booking that is merely waiting on a yes. Checked first so the INVALID_ARGUMENT
            # branch below never adds missing_fields.
            status = ToolStatus.INVALID_ARGUMENT
            payload["awaiting_caller_confirmation"] = True
        elif chain_status == "partial":
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
            # Lets the LLM ask for the one missing piece (e.g. an order id).
            payload["missing_fields"] = response.get("missing_fields") or []

        return ToolResult(
            status=status,
            payload=payload,
            error=response.get("error"),
            deterministic_response=response.get("deterministic_response"),
        )
