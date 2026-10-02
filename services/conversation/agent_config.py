"""Legacy per-agent settings from config/agents/<script_id>.yaml, falling back to
default.yaml, then built-in defaults."""

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
    # None means build a starter graph from greeting/system_prompt.
    workflow:      dict | None = None
    # Keep in sync with the gateway's CallFsmTimerConfig::goodbye_timeout default.
    goodbye_grace_period_ms: int = 2500


_MAX_GRACE_PERIOD_MS = 60_000  # sanity ceiling — a brief grace window, not a hold queue


def _validate_grace_period(raw: object, filename: str) -> int:
    """Coerce goodbye_grace_period_ms to a valid uint32 range, else the default
    (a bad value would raise mid-call in EndCall)."""
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
    """Load agent config for *script_id*, or defaults if none is found."""
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
    """Wrap the legacy YAML path in the same (RuntimeConfig, ProviderBundle) shape
    as agent_resolver. agent.id="" must stay falsy: calls.agent_id is a UUID FK."""
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
    # Descriptive only; never resolved via ProviderRegistry.
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
