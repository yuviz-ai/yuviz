"""
Pydantic request models — validate what comes in over HTTP before it reaches
Config Service. Responses are the plain dicts tenants.py/agents.py/
provider_configs.py already return (FastAPI serializes UUID/datetime in a
dict automatically) — no separate response schema, so there's exactly one
place field lists are maintained, not two that can drift apart.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MAX_WORKFLOW_NODES = 50
MAX_WORKFLOW_EDGES = 120
# Caps prompt bloat: a 2-node graph can still carry multi-MB strings.
MAX_WORKFLOW_BYTES = 256_000


def _check_graph_bounds(graph: dict[str, Any] | None) -> dict[str, Any] | None:
    if graph is None:
        return None
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if isinstance(nodes, list) and len(nodes) > MAX_WORKFLOW_NODES:
        raise ValueError(f"workflow has too many nodes (max {MAX_WORKFLOW_NODES})")
    if isinstance(edges, list) and len(edges) > MAX_WORKFLOW_EDGES:
        raise ValueError(f"workflow has too many edges (max {MAX_WORKFLOW_EDGES})")
    # Cheap size check before CPU-bound parse_graph / graph_warnings.
    size = len(json.dumps(graph, default=str))
    if size > MAX_WORKFLOW_BYTES:
        raise ValueError(
            f"workflow is too large ({size} bytes; max {MAX_WORKFLOW_BYTES})"
        )
    return graph


class TenantCreate(BaseModel):
    name: str
    slug: str
    region: str = "us"


class TenantUpdate(BaseModel):
    name:                  str | None = None
    region:                str | None = None
    vad_engine:            str | None = None
    vad_onset_ms:          int | None = None
    vad_hold_ms:           int | None = None
    vad_speech_threshold:  float | None = None
    no_speech_timeout_ms:  int | None = None
    stt_timeout_ms:        int | None = None
    llm_timeout_ms:        int | None = None
    # Bounds mirror the gateway's CallFsmTimerConfig::transfer_timeout_min/max
    # (10s-120s) — enforced here too so a bad value is rejected at config
    # time instead of silently falling back to the default at call time.
    transfer_timeout_ms:   int | None = Field(default=None, ge=10_000, le=120_000)
    default_stt_config_id: str | None = None
    default_llm_config_id: str | None = None
    default_tts_config_id: str | None = None
    # None = field absent, per `exclude_unset` (matching every other field on
    # this model) — clearing the cap back to NULL is not offered through
    # this endpoint; use PATCH /tenants/{id}/concurrency's own dedicated
    # route (routers/tenants.py) for that, which shares this same bound.
    max_concurrent_calls:  int | None = Field(default=None, ge=1, le=10_000)


class TenantConcurrencyUpdate(BaseModel):
    max_concurrent_calls: int = Field(ge=1, le=10_000)


class AgentCreate(BaseModel):
    slug:           str
    name:           str
    greeting:       str = ""
    system_prompt:  str = ""
    stt_config_id:  str | None = None
    llm_config_id:  str | None = None
    tts_config_id:  str | None = None
    workflow: dict | None = None  # None/{} → starter_graph; validated like publish

    @field_validator("workflow")
    @classmethod
    def _workflow_bounds(cls, value: dict | None) -> dict | None:
        return _check_graph_bounds(value)


class AgentUpdate(BaseModel):
    name:                 str | None = None
    greeting:             str | None = None
    system_prompt:        str | None = None
    goodbye_grace_ms:     int | None = None
    language:             str | None = None
    stt_config_id:        str | None = None
    llm_config_id:        str | None = None
    tts_config_id:        str | None = None
    transfer_type:        Literal["warm", "cold", "none"] | None = None
    transfer_destination: str | None = None
    queue_id:             str | None = None
    escalation_threshold: int | None = None
    # What caller ID the human agent sees on a warm transfer's agent leg —
    # resolved entirely in the Conversation Service; the gateway never sees
    # this, only the final caller_id string (see transfer_engine.py).
    caller_id_policy:     Literal["original", "platform", "custom"] | None = None
    platform_did:         str | None = None
    custom_caller_id:     str | None = None
    transfer_waiting_experience: Literal["announcement_moh", "announcement_silence"] | None = None
    # Condition-clause overrides for the [[END_CALL]]/[[TRANSFER]] trigger
    # instructions; None/empty = built-in defaults (see pipeline.py).
    end_call_prompt:      str | None = None
    transfer_prompt:      str | None = None
    # Exact scripted lines the agent speaks when ending/transferring;
    # None/empty = LLM chooses the wording (see pipeline.py).
    farewell_message:      str | None = None
    transfer_announcement: str | None = None
    status:               Literal["active", "inactive"] | None = None
    # Admin-configured hard ceiling on call length, in seconds; None = no
    # limit set (leaves the column NULL — unlimited, the pre-existing
    # behavior). Bounds mirror the DB CHECK constraint (agents_max_call_
    # duration_s_check) so a bad value is rejected at config time instead
    # of failing the INSERT/UPDATE.
    max_call_duration_s:  int | None = Field(default=None, ge=30, le=7200)


class WorkflowDraft(BaseModel):
    graph: dict[str, Any]
    # When set, save is rejected with 409 if agents.config_version moved
    # (publish won a race). Omit for backward-compatible last-write-wins.
    base_config_version: int | None = None

    @field_validator("graph")
    @classmethod
    def _graph_bounds(cls, value: dict[str, Any]) -> dict[str, Any]:
        checked = _check_graph_bounds(value)
        assert checked is not None
        return checked


class WorkflowPublish(BaseModel):
    graph: dict[str, Any] | None = None  # None → publish workflow_draft
    note:  str | None = None

    @field_validator("graph")
    @classmethod
    def _graph_bounds(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_graph_bounds(value)


class ProviderConfigCreate(BaseModel):
    name:        str
    # 'embedding' added for Phase 6A's Knowledge Platform (services/knowledge/
    # embedding_manager.py) — provider_configs.role's CHECK constraint
    # already allows it (see database/knowledge_schema.sql); this schema
    # just hadn't been widened to match until now.
    role:        Literal["stt", "llm", "tts", "embedding"]
    engine:      str
    environment: Literal["prod", "staging", "dev"] = "prod"
    model:       str | None = None
    voice:       str | None = None
    language:    str | None = None
    region:      str | None = None
    api_key_ref: str | None = None
    # The credential itself, encrypted before it reaches Postgres and stored
    # as an enc: api_key_ref. Never a column — see resolve_api_key_input().
    api_key:     str | None = None
    extra:       dict[str, Any] | None = None


class ProviderConfigUpdate(BaseModel):
    name:        str | None = None
    engine:      str | None = None
    environment: Literal["prod", "staging", "dev"] | None = None
    model:       str | None = None
    voice:       str | None = None
    language:    str | None = None
    region:      str | None = None
    api_key_ref: str | None = None
    api_key:     str | None = None
    # Replaces the whole extra object — service layer does not deep-merge
    # (see provider_configs.update_provider_config). Was missing here even
    # though the service layer and admin-ui's own TS type both already
    # supported it — PATCHing extra silently 400'd with "no fields to
    # update" since FastAPI drops any JSON key with no matching Pydantic
    # field before exclude_unset=True ever runs.
    extra:       dict[str, Any] | None = None


class TelephonyConfigCreate(BaseModel):
    name:                str
    # Validated against libs.telephony_sdk's registered provider names at
    # the business-logic layer (services/config/telephony_configs.py), not
    # here — new providers are additive there, not a schema change here.
    provider:            str
    credentials:         dict[str, Any] = {}
    is_default_outbound: bool = False


class TelephonyConfigUpdate(BaseModel):
    name:                str | None = None
    # provider is immutable after creation — same posture as
    # ProviderConfigUpdate dropping `role`.
    credentials:         dict[str, Any] | None = None
    is_default_outbound: bool | None = None


class ToolProviderConfigCreate(BaseModel):
    name:        str
    # 'tool_name' matches the static catalog in services/conversation/tools/
    # registry.py (ToolRegistry) — "book_appointment"/"send_sms" today.
    tool_name:   str
    engine:      str
    # One of api_key_ref/api_key is required — every tool engine today
    # (cal_com, twilio) is a cloud API requiring credentials; the router
    # checks that at least one was given (see resolve_api_key_input()).
    api_key_ref: str | None = None
    # The credential itself, encrypted before it reaches Postgres — see
    # provider_configs.resolve_api_key_input().
    api_key:     str | None = None
    extra:       dict[str, Any] | None = None


class ToolProviderConfigUpdate(BaseModel):
    name:        str | None = None
    engine:      str | None = None
    api_key_ref: str | None = None
    api_key:     str | None = None
    extra:       dict[str, Any] | None = None


class AgentToolPolicyCreate(BaseModel):
    tool_name:                str
    tool_provider_config_id:  str
    enabled:                  bool = True
    timeout_ms:               int | None = None
    max_calls_per_turn:       int | None = None
    # NULL = use the platform ceiling (services/toolexec/graph.py's
    # MAX_CHAIN_LEVELS = 4); a set value can only LOWER it, never raise
    # it, enforced where it's actually applied (agent_apis.py's
    # _effective_max_chain_depth and executor.py's ceiling clamp), never
    # 0/disabled. Meaningful only for tool_name='execute_api'; harmless
    # (unread) on every other tool's row.
    max_chain_depth:          int | None = None


class AgentToolPolicyUpdate(BaseModel):
    enabled:             bool | None = None
    timeout_ms:          int | None = None
    max_calls_per_turn:  int | None = None
    max_chain_depth:     int | None = None
    extra:       dict[str, Any] | None = None


class CarrierCreate(BaseModel):
    name:                 str
    provider:             Literal["twilio", "plivo", "vonage"]
    auth_id:              str | None = None
    auth_token_ref:       str | None = None
    carrier_account_ref:  str | None = None


class CarrierUpdate(BaseModel):
    name:                 str | None = None
    auth_id:              str | None = None
    auth_token_ref:       str | None = None
    carrier_account_ref:  str | None = None


class PhoneNumberCreate(BaseModel):
    did:               str
    agent_id:          str | None = None
    fallback_agent_id: str | None = None
    carrier_id:        str | None = None
    region:            str | None = None
    status:            Literal["active", "inactive", "suspended"] = "active"


class PhoneNumberUpdate(BaseModel):
    did:               str | None = None
    agent_id:          str | None = None
    fallback_agent_id: str | None = None
    carrier_id:        str | None = None
    region:            str | None = None
    status:            Literal["active", "inactive", "suspended"] | None = None


class LoginRequest(BaseModel):
    email:    str
    password: str


class BootstrapRequest(BaseModel):
    email:    str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8)


class UserUpdate(BaseModel):
    role:      Literal["superadmin", "admin", "supervisor", "agent", "viewer"] | None = None
    tenant_id: str | None = None
    password:  str | None = Field(default=None, min_length=8)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password:      str = Field(min_length=8)


class InviteCreate(BaseModel):
    email:     str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role:      Literal["superadmin", "admin", "supervisor", "agent", "viewer"]
    tenant_id: str | None = None  # None == superadmin scope
    team:      str | None = None


class InviteAccept(BaseModel):
    password: str = Field(min_length=8)
