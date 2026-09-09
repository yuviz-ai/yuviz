"""
Pydantic request/response models for Tool Execution Service's HTTP API —
same convention as services/knowledge/schemas.py: responses are the plain
dicts the service modules already return, no separate response schema,
except ChainExecuteRequest/Response which cross the internal service
boundary (services/toolexec/routers/execute.py) and so are validated on
both sides of that call.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from . import graph


class CustomApiParamSpec(BaseModel):
    name: str
    location: Literal["body", "query", "header", "path"]
    json_type: Literal["string", "number", "integer", "boolean", "object", "array"]
    description: str = ""
    required: bool = True
    source: Literal["literal", "caller", "upstream"]
    literal_value: Any | None = None
    upstream_api_id: str | None = None
    upstream_json_path: str | None = None
    sensitive: bool = False


class CustomApiCreate(BaseModel):
    name: str
    description: str
    endpoint_url: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    body_style: Literal["json", "form"] = "json"
    auth_scheme: Literal["none", "api_key", "bearer", "oauth2_client_credentials"] = "none"
    auth_config: dict[str, Any] = {}
    side_effecting: bool = True
    idempotency_header: str | None = None
    timeout_ms: int | None = None
    sensitive_response_paths: list[str] = []
    success_template: str | None = None
    params: list[CustomApiParamSpec] = []


class CustomApiUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    endpoint_url: str | None = None
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] | None = None
    body_style: Literal["json", "form"] | None = None
    auth_scheme: Literal["none", "api_key", "bearer", "oauth2_client_credentials"] | None = None
    auth_config: dict[str, Any] | None = None
    side_effecting: bool | None = None
    idempotency_header: str | None = None
    timeout_ms: int | None = None
    sensitive_response_paths: list[str] | None = None
    success_template: str | None = None
    params: list[CustomApiParamSpec] | None = None


class AgentCustomApiEnable(BaseModel):
    enabled: bool = True


class ChainExecuteRequest(BaseModel):
    tenant_id: str
    agent_id: str
    call_id: str
    session_id: str
    turn_id: str
    tool_call_id: str
    idempotency_key: str
    api_name: str
    caller_arguments: dict[str, Any] = {}
    # Whole-chain wall clock, derived from the turn's deadline; server
    # clamps to TOOLEXEC_MAX_CHAIN_BUDGET_MS (can only lower).
    chain_budget_ms: int
    # Effective per-agent ceiling. NOT trusted verbatim — bounded here
    # against the platform maximum (never trust the caller already
    # clamped it), and independently re-clamped again inside
    # graph.resolve_order() and against agent_apis._effective_max_chain_depth()
    # in the executor, so no single dropped clamp can let a chain past 4.
    max_chain_depth: int = Field(ge=1, le=graph.MAX_CHAIN_LEVELS)


class ChainStepReport(BaseModel):
    api_name: str
    level: int
    status: Literal["claimed", "success", "failed", "timeout", "skipped", "invalid_argument", "unavailable"]
    # Param names sourced from an upstream response, for the admin-facing
    # chain history view.
    from_prior_step: list[str] = []


class ChainExecuteResponse(BaseModel):
    run_id: str
    chain_status: Literal[
        "success", "partial", "failed", "timeout", "invalid_argument", "unavailable", "rate_limited",
    ]
    steps: list[ChainStepReport] = []
    # AC 6: what DID happen, never dropped even when the chain as a whole failed.
    completed_steps: list[str] = []
    failed_step: ChainStepReport | None = None
    # Redacted projection of the FINAL step only, populated only when
    # chain_status == "success".
    data: dict[str, Any] = {}
    missing_fields: list[dict] = []
    deterministic_response: str | None = None
    error: str | None = None
