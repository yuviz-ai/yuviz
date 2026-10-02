"""Config SDK DTOs: the only shapes consumers see.

Tenant/Agent/ProviderConfig are row-shaped; RuntimeConfig is grouped by what a
call handler needs. Fields with no backing column yet default to None.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

# Mirrored by the gateway's CallFsmTimerConfig; keep in sync.
TRANSFER_TIMEOUT_MIN_MS     = 10_000
TRANSFER_TIMEOUT_DEFAULT_MS = 45_000
TRANSFER_TIMEOUT_MAX_MS     = 120_000


def validate_transfer_timeout_ms(value: Any, *, context: str = "") -> int:
    """Return value as int if within bounds, else the default with a warning. Never raises."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        ms = -1
    if TRANSFER_TIMEOUT_MIN_MS <= ms <= TRANSFER_TIMEOUT_MAX_MS:
        return ms
    log.warning(
        "transfer_timeout_ms=%r out of bounds [%d, %d]%s — using default %d",
        value, TRANSFER_TIMEOUT_MIN_MS, TRANSFER_TIMEOUT_MAX_MS,
        f" ({context})" if context else "", TRANSFER_TIMEOUT_DEFAULT_MS,
    )
    return TRANSFER_TIMEOUT_DEFAULT_MS


@dataclass(frozen=True)
class Tenant:
    id: str
    slug: str
    name: str
    region: str
    vad_engine: str | None
    vad_onset_ms: int | None
    vad_hold_ms: int | None
    vad_speech_threshold: float | None
    no_speech_timeout_ms: int | None
    stt_timeout_ms: int | None
    llm_timeout_ms: int | None
    transfer_timeout_ms: int | None
    default_stt_config_id: str | None
    default_llm_config_id: str | None
    default_tts_config_id: str | None
    config_version: int
    updated_at: datetime


@dataclass(frozen=True)
class Agent:
    id: str
    slug: str
    tenant_id: str
    name: str
    greeting: str
    system_prompt: str
    goodbye_grace_ms: int
    stt_config_id: str | None
    llm_config_id: str | None
    tts_config_id: str | None
    status: str
    config_version: int
    updated_at: datetime
    # None = derive from the STT/TTS provider's language.
    language: str | None = None
    # Condition clause only; token mechanics stay fixed so prompts can't break parsing.
    end_call_prompt: str | None = None
    transfer_prompt: str | None = None
    # Scripted lines spoken verbatim; None = LLM chooses the wording.
    farewell_message: str | None = None
    transfer_announcement: str | None = None
    transfer_type: str = "none"
    transfer_destination: str | None = None
    queue_id: str | None = None
    escalation_threshold: int | None = None
    caller_id_policy: str = "original"
    platform_did: str | None = None
    custom_caller_id: str | None = None
    transfer_waiting_experience: str = "announcement_moh"
    max_call_duration_s: int | None = None  # None = unlimited
    workflow: dict[str, Any] | None = None
    workflow_draft: dict[str, Any] | None = None
    # Non-null pins this agent to a call flow's IVR runtime instead of its workflow.
    call_flow_id: str | None = None


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    role: str  # 'stt' | 'llm' | 'tts'
    engine: str
    model: str | None
    voice: str | None
    language: str | None
    api_key_ref: str | None
    extra: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ToolSpec:
    """Placeholder until the Tool Orchestrator exists."""
    name: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Prompt:
    """Return type of get_prompt(); the call path reads RuntimeConfig.conversation instead."""
    greeting: str
    system_prompt: str


@dataclass(frozen=True)
class ProviderConfigs:
    """Resolved provider configs for a call; input to ProviderRegistry.resolve()."""
    stt: ProviderConfig
    llm: ProviderConfig
    tts: ProviderConfig


@dataclass(frozen=True)
class ConversationInfo:
    greeting: str
    system_prompt: str
    goodbye_prompt: str | None = None    # no schema column yet
    fallback_prompt: str | None = None   # no schema column yet
    end_call_prompt: str | None = None
    transfer_prompt: str | None = None
    farewell_message: str | None = None
    transfer_announcement: str | None = None
    workflow: dict[str, Any] | None = None
    workflow_draft: dict[str, Any] | None = None


@dataclass(frozen=True)
class MediaInfo:
    voice: str | None            # flattened from providers.tts.voice
    language: str | None         # agent.language override, else providers.stt.language, else tts.language
    sample_rate: int = 16_000    # wire constant (gateway.yaml media.sample_rate)


@dataclass(frozen=True)
class Policies:
    vad_engine: str | None
    vad_onset_ms: int | None
    vad_hold_ms: int | None
    vad_speech_threshold: float | None
    silence_timeout_ms: int | None   # = tenant.no_speech_timeout_ms
    stt_timeout_ms: int | None
    llm_timeout_ms: int | None
    goodbye_grace_ms: int            # = agent.goodbye_grace_ms
    barge_in_enabled: bool | None = None      # no schema column yet
    # Hard ceiling on call length in seconds, checked after STT; None = unlimited.
    max_call_duration_s: int | None = None
    transfer_type: str = "none"
    transfer_destination: str | None = None
    queue_id: str | None = None
    escalation_threshold: int | None = None
    # Warm transfer only; resolved to a caller_id string by the conversation service.
    caller_id_policy: str = "original"
    platform_did: str | None = None
    custom_caller_id: str | None = None
    # Warm transfer only; passed raw to the gateway (hold/MOH or silence).
    transfer_waiting_experience: str = "announcement_moh"
    # How long the gateway waits for the transfer outcome before failing it.
    transfer_timeout_ms: int = TRANSFER_TIMEOUT_DEFAULT_MS


@dataclass(frozen=True)
class CallFlow:
    """Published call flow. agent_slugs/resolved_tts_config_id are server-validated
    as same-tenant; unresolvable ids are absent/None."""
    id: str
    tenant_slug: str
    config_version: int
    graph: dict[str, Any]
    agent_slugs: dict[str, str] = field(default_factory=dict)
    resolved_tts_config_id: str | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    """Immutable per-session snapshot of everything a call handler needs.

    version = agent.config_version; resolved_at is stamped at assembly time.
    """
    tenant: Tenant
    agent: Agent
    providers: ProviderConfigs
    conversation: ConversationInfo
    media: MediaInfo
    policies: Policies
    tools: list[ToolSpec]
    version: int
    resolved_at: datetime
