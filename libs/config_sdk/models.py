"""
Config SDK data transfer objects — the only shapes Conversation Service (or
any future consumer) ever sees. Deliberately not the raw asyncpg/Redis dicts
services/config/*.py works with internally: those are an implementation
detail of how Config Service stores things, not a contract this SDK should
leak to its callers.

Tenant/Agent/ProviderConfig mirror real schema columns closely (they ARE
row-shaped — that's fine for these three, since they're genuinely "the row,
typed"). RuntimeConfig is different on purpose: a domain object, grouped by
what a call handler actually needs (conversation text, media settings,
timing policy), not by which table each field happens to live in. A caller
reading runtime.media.voice never needs to know that value came from
provider_configs.voice, not agents or tenants — see cache_aside.py's
assembly logic for where that flattening happens.

config_version/updated_at are carried through wherever the source-of-truth
schema actually has them (tenants/agents have a real config_version column +
an auto-bump trigger; provider_configs has updated_at only — no version
counter exists there, so ProviderConfig doesn't claim one). This is plumbing
for cache validation and a future hot-config-reload feature, not something
any code re-checks mid-call today — session-lifetime-only resolution (see
project architecture decisions) is unchanged by adding these fields.

A few RuntimeConfig fields (goodbye_prompt, fallback_prompt, barge_in_enabled)
have no backing column in today's schema. They're modeled as real fields
defaulting to None, not omitted and not faked with invented data — same
"real empty, not a stub exception" posture already used for get_tools().
Forward-compatible shape now; real values whenever those columns exist.

max_call_duration_s (agents.max_call_duration_s, Policies.max_call_duration_s)
was in that same forward-compatible-stub state until it got a real column
and enforcement (services/conversation/pipeline.py's on_speech_ended) —
kept here as an example of the pattern actually paying off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

# Bounds for Policies.transfer_timeout_ms (Phase 5F transfer hardening) —
# mirrored by the gateway's CallFsmTimerConfig transfer_timeout_min/default/
# max. Both sides enforce the same fallback-to-default-with-warning rule so
# a misconfigured value (0, -1, 3600000) can never silently produce a
# transfer that fails instantly or hangs for an hour.
TRANSFER_TIMEOUT_MIN_MS     = 10_000
TRANSFER_TIMEOUT_DEFAULT_MS = 45_000
TRANSFER_TIMEOUT_MAX_MS     = 120_000


def validate_transfer_timeout_ms(value: Any, *, context: str = "") -> int:
    """Returns value as an int when it lies inside
    [TRANSFER_TIMEOUT_MIN_MS, TRANSFER_TIMEOUT_MAX_MS]; anything else
    (non-numeric, zero, negative, out of range) falls back to
    TRANSFER_TIMEOUT_DEFAULT_MS with a warning naming the offending value —
    never raises, matching the config plane's degrade-don't-reject
    posture."""
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
    # Overrides Policies.transfer_timeout_ms's TRANSFER_TIMEOUT_DEFAULT_MS —
    # same tenant-level scope as the other timers above (matches the
    # gateway's CallFsmTimerConfig, which is also tenant-scoped).
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
    # Explicit override — None means "derive from the STT/TTS provider's
    # own language setting" (RuntimeConfig.media's resolution order; see
    # cache_aside.py's get_runtime_config()). Defaults to None so every
    # existing construction site (that predates this field) still works.
    language: str | None = None
    # Condition-clause overrides for the [[END_CALL]]/[[TRANSFER]] trigger
    # instructions (agents.end_call_prompt/transfer_prompt); None/empty =
    # pipeline.py's built-in defaults. Only the condition is configurable —
    # token mechanics are fixed so custom prompts can't break parsing.
    end_call_prompt: str | None = None
    transfer_prompt: str | None = None
    # Exact scripted lines the agent speaks when ending/transferring
    # (agents.farewell_message/transfer_announcement) — synthesized
    # deterministically by pipeline.py, never LLM-worded. None/empty =
    # LLM chooses the wording, the pre-existing behavior.
    farewell_message: str | None = None
    transfer_announcement: str | None = None
    # Human escalation / transfer — mirrors agents.transfer_type/
    # transfer_destination/queue_id/escalation_threshold (database/schema.sql).
    # Defaults match the column defaults (transfer_type='none', the rest
    # nullable) so every pre-existing construction site still works.
    # Nothing downstream consumes these yet (Conversation Service pipeline,
    # gRPC, Gateway) — this only makes the values reachable on RuntimeConfig.
    transfer_type: str = "none"
    transfer_destination: str | None = None
    queue_id: str | None = None
    escalation_threshold: int | None = None
    # Mirrors agents.caller_id_policy/platform_did/custom_caller_id — see
    # Policies.caller_id_policy's own comment for what this controls.
    caller_id_policy: str = "original"
    platform_did: str | None = None
    custom_caller_id: str | None = None
    # Mirrors agents.transfer_waiting_experience — see Policies.
    # transfer_waiting_experience's own comment for what this controls.
    transfer_waiting_experience: str = "announcement_moh"
    # Mirrors agents.max_call_duration_s — see Policies.max_call_duration_s's
    # own comment for what this controls. None = unlimited.
    max_call_duration_s: int | None = None
    # Published / draft graphs (agents.workflow*). Draft is editor-only.
    workflow: dict[str, Any] | None = None
    workflow_draft: dict[str, Any] | None = None


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
    """Stub — Tool Orchestrator is a Phase 6b concept, not built yet (see
    project memory). Modeled now so IConfigProvider.get_tools()'s return
    type is real and won't need a breaking change when tools exist."""
    name: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Prompt:
    """Standalone return type for get_prompt() — the real call path
    (agent_resolver.py) reads greeting/system_prompt off RuntimeConfig.
    conversation instead; this exists for a future consumer that wants just
    the prompt without paying for the full runtime-config assembly."""
    greeting: str
    system_prompt: str


@dataclass(frozen=True)
class ProviderConfigs:
    """The three resolved-but-not-yet-instantiated provider configs for a
    call — input to services.conversation's ProviderRegistry.resolve(),
    never read from directly by a handler (see ConversationInfo/MediaInfo
    for the fields a handler actually needs). Kept distinct from the
    conversation-service-level ProviderBundle (which holds live ISTT/ILLM/
    ITTS instances) deliberately: the SDK must never import provider
    interface types, and a handler must never see raw ProviderConfig rows."""
    stt: ProviderConfig
    llm: ProviderConfig
    tts: ProviderConfig


@dataclass(frozen=True)
class ConversationInfo:
    greeting: str
    system_prompt: str
    goodbye_prompt: str | None = None    # no schema column yet — see module docstring
    fallback_prompt: str | None = None   # no schema column yet — see module docstring
    # Condition-clause overrides (see Agent.end_call_prompt/transfer_prompt).
    end_call_prompt: str | None = None
    transfer_prompt: str | None = None
    # Scripted spoken lines (see Agent.farewell_message/transfer_announcement).
    farewell_message: str | None = None
    transfer_announcement: str | None = None
    # Published graph only — drafts never reach the call path.
    workflow: dict[str, Any] | None = None


@dataclass(frozen=True)
class MediaInfo:
    voice: str | None            # flattened from providers.tts.voice
    language: str | None         # agent.language override, else providers.stt.language, else tts.language
    sample_rate: int = 16_000    # wire/protocol constant (matches gateway.yaml media.sample_rate),
                                  # not a per-tenant column — surfaced here for a consistent shape


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
    barge_in_enabled: bool | None = None      # no schema column yet — see module docstring
    # = agent.max_call_duration_s. Admin-configured hard ceiling on call
    # length in seconds; None = unlimited. Enforced in
    # services/conversation/pipeline.py's on_speech_ended(): checked after
    # STT (so the caller's final utterance is still transcribed), before
    # the LLM call (so no response is generated only to be discarded) —
    # once exceeded, the pipeline skips the LLM, speaks a fixed wrap-up
    # line, and ends the call the same way farewell_message/[[END_CALL]]
    # do. Agent-scoped (not tenant-scoped like the gateway's FSM timers —
    # see CallFsmTimerConfig) since a reasonable call length is a business
    # decision that varies per agent (a quick reception agent vs. a longer
    # sales conversation), not a platform/tenant-wide dial.
    max_call_duration_s: int | None = None
    # Mirrors agent.transfer_type/transfer_destination/queue_id/
    # escalation_threshold — surfaced here (grouped with other call-behavior
    # policy) in addition to Agent, same precedent as goodbye_grace_ms above.
    transfer_type: str = "none"
    transfer_destination: str | None = None
    queue_id: str | None = None
    escalation_threshold: int | None = None
    # What caller ID the human agent sees on a warm transfer's agent leg —
    # mirrors agent.caller_id_policy/platform_did/custom_caller_id. Resolved
    # into a single caller_id string entirely within the Conversation
    # Service (transfer_engine.py) — the gateway never sees this policy,
    # only the resolved value on the TransferRequest gRPC message. No
    # equivalent for cold transfer: uuid_transfer never originates a new
    # leg, so there's no caller-id parameter to set.
    caller_id_policy: str = "original"
    platform_did: str | None = None
    custom_caller_id: str | None = None
    # What the caller experiences while a warm transfer's agent leg is
    # ringing — mirrors agent.transfer_waiting_experience. Unlike
    # caller_id_policy above, this is a telephony-execution detail (does
    # the gateway issue uuid_hold or not), not a business decision — the
    # raw value rides the TransferRequest gRPC message unresolved, and
    # WarmTransferCoordinator itself switches on it. No equivalent for
    # cold transfer (the caller's own leg is redirected immediately, no
    # waiting period exists).
    transfer_waiting_experience: str = "announcement_moh"
    # Phase 5F: how long the gateway waits for CHANNEL_BRIDGE/CHANNEL_HANGUP
    # before declaring a transfer failed. No schema column yet — always the
    # default until one exists; the gateway additionally honors a
    # "transfer_timeout_ms" key in its tenant:{id} Redis overlay (see
    # gateway TenantConfig::from_redis). Bounds enforced by
    # validate_transfer_timeout_ms() above.
    transfer_timeout_ms: int = TRANSFER_TIMEOUT_DEFAULT_MS


@dataclass(frozen=True)
class RuntimeConfig:
    """Everything a call handler needs for one session, assembled once and
    never mutated — grouped by purpose (conversation text, media, timing
    policy), not by source table. The single object PipelineConversation
    Handler's construction site reads from, replacing five separate
    lookups (tenant + agent + 3x provider_config) with one
    get_runtime_config() call.

    version mirrors the resolved agent's config_version — matches the
    existing precedent already used elsewhere (phone_numbers.py's cached
    "version" field, calls.agent_config_version) rather than inventing a
    new combined-hash scheme. resolved_at is stamped by the SDK at assembly
    time (not a DB column) — how old this particular snapshot is in a
    running process, useful for future hot-reload/staleness observability.
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
