"""Pydantic request models. Responses are the service modules' plain dicts (no response schemas)."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from libs.config_sdk.dial_targets import is_dial_number, is_transfer_destination

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
    # Mirrors the gateway's transfer timeout bounds, so bad values fail here, not silently at call time.
    transfer_timeout_ms:   int | None = Field(default=None, ge=10_000, le=120_000)
    default_stt_config_id: str | None = None
    default_llm_config_id: str | None = None
    default_tts_config_id: str | None = None
    # None means unset; this route can't clear the cap to NULL.
    max_concurrent_calls:  int | None = Field(default=None, ge=1, le=10_000)


class TenantConcurrencyUpdate(BaseModel):
    max_concurrent_calls: int = Field(ge=1, le=10_000)


class SystemPromptGenerate(BaseModel):
    name: str
    purpose: str = ""
    persona: str = ""
    tone: str = ""
    language: str | None = None
    has_knowledge_base: bool = False
    transfer_condition: str | None = None
    compliance_instructions: str = ""
    fallback_response: str = ""
    llm_config_id: str


class AgentCreate(BaseModel):
    slug:           str
    name:           str
    greeting:       str = ""
    system_prompt:  str = ""
    stt_config_id:  str | None = None
    llm_config_id:  str | None = None
    tts_config_id:  str | None = None
    status:         Literal["active", "inactive"] = "active"
    workflow: dict | None = None  # None/{} → starter_graph; validated like publish
    language:               str | None = None
    # Multilingual agents; validated as one unit in agents._validate_languages.
    supported_languages:    list[str] | None = None
    tts_config_by_language: dict[str, str | None] | None = None
    greeting_by_language:   dict[str, str] | None = None

    @field_validator("workflow")
    @classmethod
    def _workflow_bounds(cls, value: dict | None) -> dict | None:
        return _check_graph_bounds(value)


def _no_double_braces(value: str) -> str:
    # The runtime workflow renderer evaluates or deletes {{ ... }} and has no escape syntax.
    if "{{" in value or "}}" in value:
        raise ValueError("double curly brackets are not allowed")
    return value


class AgentFromTemplate(BaseModel):
    template_id:      str
    template_version: int
    name:             str = Field(min_length=1, max_length=80)
    business_name:    str = Field(min_length=1, max_length=120)
    business_facts:   str = Field(max_length=1000)
    language:         str | None = None
    stt_config_id:    str | None = None
    llm_config_id:    str | None = None
    tts_config_id:    str | None = None

    _braces = field_validator("name", "business_name", "business_facts")(_no_double_braces)


class TestSessionCreate(BaseModel):
    __test__ = False  # not a pytest class

    channel: Literal["voice", "chat"]


class TestChatTurn(BaseModel):
    __test__ = False  # not a pytest class

    credential: str
    session_id: str
    message:    str = Field(min_length=1, max_length=1000)


class PromptRevise(BaseModel):
    session_id:    str
    problem:       str = Field(min_length=1, max_length=1000)
    llm_config_id: str | None = None


class PromptRewrite(BaseModel):
    prompt:      str = Field(min_length=1, max_length=20_000)
    instruction: str = Field(min_length=1, max_length=500)


class PromptAccept(BaseModel):
    session_id:         str
    problem:            str = Field(min_length=1, max_length=1000)
    proposed_prompt:    str = Field(max_length=20_000)
    base_prompt_sha256: str


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
    # Caller ID the human sees on a warm transfer; resolved in the Conversation Service.
    caller_id_policy:     Literal["original", "platform", "custom"] | None = None
    platform_did:         str | None = None
    custom_caller_id:     str | None = None

    @field_validator("transfer_destination")
    @classmethod
    def _transfer_destination_shape(cls, value: str | None) -> str | None:
        if value and not is_transfer_destination(value):
            raise ValueError(
                "transfer_destination must be a phone number/extension or sip:user@host, and not "
                "one of this platform's own AI numbers (788, 5000-5009) or a loopback address"
            )
        return value

    @field_validator("platform_did", "custom_caller_id")
    @classmethod
    def _caller_id_shape(cls, value: str | None) -> str | None:
        if value and not is_dial_number(value):
            raise ValueError("must be a phone number: digits with an optional leading +")
        return value

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
    # Hard call-length ceiling in seconds (None = unlimited); bounds mirror the DB CHECK.
    max_call_duration_s:  int | None = Field(default=None, ge=30, le=7200)
    # Call flow that answers ahead of this agent; explicit null detaches it.
    call_flow_id:         str | None = None
    # Multilingual: set = language switching + per-turn reply-language instruction;
    # null/[] = single-language (`language` alone sets STT/TTS). See agents._validate_languages.
    supported_languages:    list[str] | None = None
    tts_config_by_language: dict[str, str | None] | None = None
    greeting_by_language:   dict[str, str] | None = None


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


class VoicePreview(BaseModel):
    """Text to speak in a provider_config's voice."""
    text: str = Field(min_length=1, max_length=600)


class CallFlowCreate(BaseModel):
    slug:        str
    name:        str
    description: str = ""
    direction:   Literal["inbound", "outbound", "both"] = "inbound"
    # At most one seeds the graph (clone a same-tenant flow, or a builder scaffold); neither = starter.
    clone_from_id: str | None = None
    graph:         dict[str, Any] | None = None

    @field_validator("graph")
    @classmethod
    def _graph_bounds(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_graph_bounds(value)


class CallFlowUpdate(BaseModel):
    name:        str | None = None
    description: str | None = None
    status:      Literal["active", "inactive"] | None = None
    direction:   Literal["inbound", "outbound", "both"] | None = None


class CallFlowDraft(BaseModel):
    graph: dict[str, Any]
    # 409 if call_flows.config_version moved since the editor loaded.
    expected_version: int | None = None

    @field_validator("graph")
    @classmethod
    def _graph_bounds(cls, value: dict[str, Any]) -> dict[str, Any]:
        checked = _check_graph_bounds(value)
        assert checked is not None
        return checked


class CallFlowPublish(BaseModel):
    graph: dict[str, Any]
    note:  str | None = None

    @field_validator("graph")
    @classmethod
    def _graph_bounds(cls, value: dict[str, Any]) -> dict[str, Any]:
        checked = _check_graph_bounds(value)
        assert checked is not None
        return checked


class ProviderConfigCreate(BaseModel):
    name:        str
    role:        Literal["stt", "llm", "tts", "embedding"]
    engine:      str
    environment: Literal["prod", "staging", "dev"] = "prod"
    model:       str | None = None
    voice:       str | None = None
    language:    str | None = None
    region:      str | None = None
    api_key_ref: str | None = None
    # Plaintext credential; encrypted into an enc: api_key_ref, never stored as-is.
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
    # Replaces the whole extra object (no deep merge).
    extra:       dict[str, Any] | None = None


class TelephonyConfigCreate(BaseModel):
    name:                str
    # Validated against the telephony_sdk registry in telephony_configs.py.
    provider:            str
    credentials:         dict[str, Any] = {}
    is_default_outbound: bool = False


class TelephonyConfigUpdate(BaseModel):
    name:                str | None = None
    # provider is immutable after creation.
    credentials:         dict[str, Any] | None = None
    is_default_outbound: bool | None = None


class ToolProviderConfigCreate(BaseModel):
    name:        str
    tool_name:   str
    engine:      str
    # The router requires api_key_ref or api_key for every engine except toolexec.
    api_key_ref: str | None = None
    # Plaintext credential; encrypted before it reaches Postgres.
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
    # NULL = platform ceiling (4); a set value can only lower it. Read only for execute_api.
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
    auth_token:           SecretStr | None = None
    carrier_account_ref:  str | None = None


class CarrierUpdate(BaseModel):
    name:                 str | None = None
    auth_id:              str | None = None
    auth_token_ref:       str | None = None
    auth_token:           SecretStr | None = None
    carrier_account_ref:  str | None = None


class PhoneNumberCreate(BaseModel):
    did:                 str
    agent_id:            str | None = None
    fallback_agent_id:   str | None = None
    carrier_id:          str | None = None
    telephony_config_id: str | None = None
    region:              str | None = None
    status:              Literal["active", "inactive", "suspended"] = "active"


class PhoneNumberUpdate(BaseModel):
    did:                 str | None = None
    agent_id:            str | None = None
    fallback_agent_id:   str | None = None
    carrier_id:          str | None = None
    telephony_config_id: str | None = None
    region:              str | None = None
    status:              Literal["active", "inactive", "suspended"] | None = None


class LoginRequest(BaseModel):
    email:    str
    password: str


SignupSource = Literal[
    "google_ad", "facebook_ad", "linkedin", "x", "friend", "youtube", "blog", "product_hunt", "other",
]


class RegisterRequest(BaseModel):
    """Public signup. Deliberately has no role/tenant field — see users.register_admin."""
    organization_name: str = Field(min_length=1, max_length=120)
    first_name:        str = Field(min_length=1, max_length=80)
    last_name:         str = Field(min_length=1, max_length=80)
    email:             str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone:             str = Field(pattern=r"^\+\d{1,4} ?\d{6,14}$")
    password:          str = Field(min_length=8)
    signup_source:     SignupSource


class UserUpdate(BaseModel):
    # superadmin is seeded only — never granted through the API.
    role:      Literal["admin", "supervisor", "agent", "viewer"] | None = None
    tenant_id: str | None = None
    password:  str | None = Field(default=None, min_length=8)


class ChangePasswordRequest(BaseModel):
    current_password: str = ""  # ignored until the account has set a password
    new_password:      str = Field(min_length=8)


class ChangeEmailRequest(BaseModel):
    current_password: str = ""
    new_email:        str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class VerifyEmailRequest(BaseModel):
    email: str
    code:  str = Field(pattern=r"^\d{6}$")


class ResendCodeRequest(BaseModel):
    email: str


class ConfirmEmailChangeRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email:        str
    code:         str = Field(pattern=r"^\d{6}$")
    new_password: str = Field(min_length=8)


class InviteCreate(BaseModel):
    email:     str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role:      Literal["superadmin", "admin", "supervisor", "agent", "viewer"]
    tenant_id: str | None = None  # None == superadmin scope
    team:      str | None = None


class InviteAccept(BaseModel):
    password: str = Field(min_length=8)
