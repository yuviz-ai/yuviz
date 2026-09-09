"""
AgentConfig — per-agent settings loaded from config/agents/<script_id>.yaml.

Each YAML file may define:
  name          — human-readable label (informational only)
  greeting      — text the AI speaks immediately when the call connects;
                  empty string disables the greeting
  system_prompt — LLM system prompt; overrides the pipeline default when set
  goodbye_grace_period_ms — ms the gateway waits after the goodbye finishes
                  playing, watching for the caller to speak up, before
                  actually hanging up (see EndCall in the gRPC protocol)

File resolution:
  1. config/agents/<script_id>.yaml  (caller-supplied agent ID)
  2. config/agents/default.yaml      (fallback)
  3. Hard-coded defaults in AgentConfig (if no YAML exists at all)

The agents directory is resolved relative to the repo root so the service
can be launched from any working directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from libs.config_sdk.workflow import starter_graph
from libs.config_sdk import (
    Agent,
    ConversationInfo,
    MediaInfo,
    Policies,
    ProviderConfig,
    ProviderConfigs,
    RuntimeConfig,
    Tenant,
)

from .provider_bundle import ProviderBundle

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

log = logging.getLogger(__name__)

# Resolve repo root: services/conversation/agent_config.py → ../../..
_REPO_ROOT  = Path(__file__).parent.parent.parent
AGENTS_DIR  = _REPO_ROOT / "config" / "agents"


@dataclass
class AgentConfig:
    name:          str = "Default Assistant"
    greeting:      str = "Hello! How can I help you today?"
    system_prompt: str = ""
    # The conversation graph this fallback agent runs. None means "build one
    # from greeting/system_prompt above" — this path exists for when the
    # config plane is unreachable mid-call (see agent_resolver.py), and a
    # degraded call still has to be a graph-driven one. A YAML file may
    # supply its own under `workflow:`.
    workflow:      dict | None = None
    # Matches CallFsmTimerConfig::goodbye_timeout's gateway-side default
    # (config/gateway.yaml has no per-tenant override yet) — keep them in
    # sync unless an agent explicitly overrides this value.
    goodbye_grace_period_ms: int = 2500


_MAX_GRACE_PERIOD_MS = 60_000  # sanity ceiling — a brief grace window, not a hold queue


def _validate_grace_period(raw: object, filename: str) -> int:
    """
    Coerce/validate goodbye_grace_period_ms, falling back to the default on
    any bad input.  Without this, a malformed value (negative, non-numeric,
    or absurdly large) from a hand-edited agent YAML would flow uncaught into
    EndCall.grace_period_ms (a gRPC proto uint32 field) and raise mid-call,
    turning what should be a graceful goodbye into an abrupt fatal error.
    """
    if raw is None:
        return AgentConfig.goodbye_grace_period_ms
    try:
        value = int(raw)
        if value < 0 or value > _MAX_GRACE_PERIOD_MS:
            raise ValueError(f"out of range [0, {_MAX_GRACE_PERIOD_MS}]")
        return value
    except (TypeError, ValueError) as e:
        log.warning(
            "Invalid goodbye_grace_period_ms=%r in %s (%s) — using default %d",
            raw, filename, e, AgentConfig.goodbye_grace_period_ms,
        )
        return AgentConfig.goodbye_grace_period_ms


def load_agent(script_id: str, agents_dir: Path = AGENTS_DIR) -> AgentConfig:
    """
    Load agent config for *script_id*.  Returns AgentConfig with defaults if
    no matching file is found or YAML is unavailable.
    """
    if not _YAML_AVAILABLE:
        log.warning("PyYAML not installed — using built-in AgentConfig defaults")
        return AgentConfig()

    agent_id = (script_id or "default").strip("/")
    candidates = [
        agents_dir / f"{agent_id}.yaml",
        agents_dir / "default.yaml",
    ]

    for path in candidates:
        if path.exists():
            try:
                with open(path) as fh:
                    data = yaml.safe_load(fh) or {}
                cfg = AgentConfig(
                    name=data.get("name", AgentConfig.name),
                    greeting=data.get("greeting", AgentConfig.greeting),
                    system_prompt=data.get("system_prompt", AgentConfig.system_prompt),
                    workflow=data.get("workflow"),
                    goodbye_grace_period_ms=_validate_grace_period(
                        data.get("goodbye_grace_period_ms"), path.name,
                    ),
                )
                log.info("Loaded agent config: %s (script_id=%r)", path.name, script_id)
                return cfg
            except Exception:
                log.exception("Failed to load agent config %s", path)

    log.warning("No agent YAML found for script_id=%r — using defaults", script_id)
    return AgentConfig()


def to_runtime_config(
    agent: AgentConfig,
    tenant_slug: str,
    script_id: str,
    stt: Any,
    llm: Any,
    tts: Any,
) -> tuple[RuntimeConfig, ProviderBundle]:
    """Wraps the legacy YAML/global-singleton fallback path in the exact
    same (RuntimeConfig, ProviderBundle) shape agent_resolver.py's real,
    Config-SDK-backed path produces — so PipelineConversationHandler has
    exactly one construction contract regardless of which path resolved it
    (see pipeline.py). stt/llm/tts here are already-constructed, shared
    global instances (services/conversation/__main__.py's serve()), not
    ProviderConfig rows — there is no provider_configs table backing this
    path, so ProviderRegistry.resolve() is never called; the bundle is
    built directly from what's already live.

    Tenant/Agent are synthetic placeholders — there is no Postgres row
    behind this call at all. agent.id="" and RuntimeConfig.version=0 are a
    deliberate, honest "not a real id/version" sentinel (empty string /
    zero are both falsy in Python): PipelineConversationHandler derives
    self._agent_id via `runtime_config.agent.id or None`, which is exactly
    what keeps TranscriptBuilder.begin_call() passing agent_id=None for a
    legacy-path call, same as before this refactor. calls.agent_id is a
    UUID FK — a fake non-UUID string like "legacy" would break the insert
    outright, not just be cosmetically wrong, so this can't be an arbitrary
    placeholder string the way tenant_slug/script_id can.
    """
    now = datetime.now(timezone.utc)
    tenant = Tenant(
        id="", slug=tenant_slug, name=tenant_slug, region="us",
        vad_engine=None, vad_onset_ms=None, vad_hold_ms=None, vad_speech_threshold=None,
        no_speech_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None,
        transfer_timeout_ms=None,
        default_stt_config_id=None, default_llm_config_id=None, default_tts_config_id=None,
        config_version=0, updated_at=now,
    )
    graph = agent.workflow or starter_graph(agent.greeting, agent.system_prompt)
    agent_row = Agent(
        id="", slug=script_id, tenant_id="", name=agent.name,
        greeting=agent.greeting, system_prompt=agent.system_prompt,
        goodbye_grace_ms=agent.goodbye_grace_period_ms,
        stt_config_id=None, llm_config_id=None, tts_config_id=None,
        status="active", config_version=0, updated_at=now,
        workflow=graph,
    )
    # Descriptive only — never resolved via ProviderRegistry for this path,
    # see docstring above. engine=type name is the closest honest label
    # available; there's no real provider_configs.engine string here.
    placeholder_providers = ProviderConfigs(
        stt=ProviderConfig(id="legacy", role="stt", engine=type(stt).__name__, model=None, voice=None, language=None, api_key_ref=None),
        llm=ProviderConfig(id="legacy", role="llm", engine=type(llm).__name__, model=None, voice=None, language=None, api_key_ref=None),
        tts=ProviderConfig(id="legacy", role="tts", engine=type(tts).__name__, model=None, voice=None, language=None, api_key_ref=None),
    )
    runtime_config = RuntimeConfig(
        tenant=tenant,
        agent=agent_row,
        providers=placeholder_providers,
        conversation=ConversationInfo(
            greeting=agent.greeting, system_prompt=agent.system_prompt,
            workflow=graph, workflow_draft=graph,
        ),
        # legacy YAML path has no prompt-override columns — defaults apply
        media=MediaInfo(voice=None, language=None),
        policies=Policies(
            vad_engine=None, vad_onset_ms=None, vad_hold_ms=None, vad_speech_threshold=None,
            silence_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None,
            goodbye_grace_ms=agent.goodbye_grace_period_ms,
        ),
        tools=[],
        version=0,
        resolved_at=now,
    )
    return runtime_config, ProviderBundle(stt=stt, llm=llm, tts=tts)
