"""
CacheAsideConfigProvider — the production IConfigProvider implementation.
Composes two injected IConfigRepository instances (Redis-first, HTTP-
fallback) and maps their raw dicts into this package's typed, immutable
models. This is the only class in the SDK that knows both "there are two
repositories" and "here's how a raw dict becomes a Tenant/Agent/
ProviderConfig" — everything above it (agent_resolver.py) only ever sees
IConfigProvider's business-level methods.

Deliberate non-duplication: on an HTTP fallback hit, this class does NOT
write the result back into Redis itself. Config Service's own GET handlers
(services/config/{tenants,agents,provider_configs}.py) already do that as
part of their own existing cache-aside — see http_repository.py's docstring.
Writing it again here would be a second, independently-maintained
implementation of the exact same "populate Redis" logic, which is exactly
the kind of drift that caused the DID-routing cache bugs fixed earlier this
project (see project memory) — one writer per key, always.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..exceptions import RepositoryUnavailableError
from ..interfaces import IConfigRepository
from ..models import (
    TRANSFER_TIMEOUT_DEFAULT_MS,
    Agent,
    ConversationInfo,
    MediaInfo,
    Policies,
    Prompt,
    ProviderConfig,
    ProviderConfigs,
    RuntimeConfig,
    Tenant,
    ToolSpec,
    validate_transfer_timeout_ms,
)

log = logging.getLogger(__name__)

_ROLES = ("stt", "llm", "tts")


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _parse_json(value: Any) -> dict[str, Any] | None:
    """Like _parse_extra but preserves None — "no workflow" vs empty dict."""
    import json
    if value is None or value == "":
        return None
    return json.loads(value) if isinstance(value, str) else value


def _parse_extra(value: Any) -> dict[str, Any]:
    # JSONB columns come back from asyncpg (and therefore from both Redis's
    # cached JSON and Config Service's REST responses, which both trace back
    # to the same raw row) as a JSON *string*, not a parsed dict — see
    # agent_resolver.py's identical historical handling of this.
    import json
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return value


def _tenant_from_dict(row: dict[str, Any]) -> Tenant:
    return Tenant(
        id=str(row["id"]),
        slug=row["slug"],
        name=row["name"],
        region=row.get("region", "us"),
        vad_engine=row.get("vad_engine"),
        vad_onset_ms=row.get("vad_onset_ms"),
        vad_hold_ms=row.get("vad_hold_ms"),
        vad_speech_threshold=row.get("vad_speech_threshold"),
        no_speech_timeout_ms=row.get("no_speech_timeout_ms"),
        stt_timeout_ms=row.get("stt_timeout_ms"),
        llm_timeout_ms=row.get("llm_timeout_ms"),
        transfer_timeout_ms=row.get("transfer_timeout_ms"),
        default_stt_config_id=row.get("default_stt_config_id"),
        default_llm_config_id=row.get("default_llm_config_id"),
        default_tts_config_id=row.get("default_tts_config_id"),
        config_version=row.get("config_version", 0),
        updated_at=_parse_dt(row.get("updated_at")),
    )


def _agent_from_dict(row: dict[str, Any]) -> Agent:
    return Agent(
        id=str(row["id"]),
        slug=row["slug"],
        tenant_id=str(row["tenant_id"]),
        name=row["name"],
        greeting=row.get("greeting", ""),
        system_prompt=row.get("system_prompt", ""),
        goodbye_grace_ms=row.get("goodbye_grace_ms", 3000),
        stt_config_id=row.get("stt_config_id"),
        llm_config_id=row.get("llm_config_id"),
        tts_config_id=row.get("tts_config_id"),
        status=row.get("status", "active"),
        config_version=row.get("config_version", 0),
        updated_at=_parse_dt(row.get("updated_at")),
        language=row.get("language"),
        transfer_type=row.get("transfer_type", "none"),
        transfer_destination=row.get("transfer_destination"),
        queue_id=row.get("queue_id"),
        escalation_threshold=row.get("escalation_threshold"),
        caller_id_policy=row.get("caller_id_policy", "original"),
        platform_did=row.get("platform_did"),
        custom_caller_id=row.get("custom_caller_id"),
        transfer_waiting_experience=row.get("transfer_waiting_experience", "announcement_moh"),
        end_call_prompt=row.get("end_call_prompt"),
        transfer_prompt=row.get("transfer_prompt"),
        farewell_message=row.get("farewell_message"),
        transfer_announcement=row.get("transfer_announcement"),
        max_call_duration_s=row.get("max_call_duration_s"),
        workflow=_parse_json(row.get("workflow")),
        workflow_draft=_parse_json(row.get("workflow_draft")),
    )


def _provider_config_from_dict(row: dict[str, Any]) -> ProviderConfig:
    return ProviderConfig(
        id=str(row["id"]),
        role=row["role"],
        engine=row["engine"],
        model=row.get("model"),
        voice=row.get("voice"),
        language=row.get("language"),
        api_key_ref=row.get("api_key_ref"),
        extra=_parse_extra(row.get("extra")),
        updated_at=_parse_dt(row.get("updated_at")),
    )


class CacheAsideConfigProvider:
    def __init__(self, redis_repo: IConfigRepository, http_repo: IConfigRepository) -> None:
        self._redis_repo = redis_repo
        self._http_repo = http_repo

    async def _fetch(self, redis_call, http_call) -> dict[str, Any] | None:
        raw = await redis_call(self._redis_repo)
        if raw is not None:
            return raw
        try:
            return await http_call(self._http_repo)
        except RepositoryUnavailableError:
            log.warning("CacheAsideConfigProvider: HTTP fallback unavailable", exc_info=True)
            return None

    async def get_tenant(self, tenant_slug: str) -> Tenant | None:
        raw = await self._fetch(
            lambda r: r.fetch_tenant(tenant_slug), lambda r: r.fetch_tenant(tenant_slug),
        )
        return _tenant_from_dict(raw) if raw is not None else None

    async def get_agent(self, tenant_slug: str, agent_slug: str) -> Agent | None:
        raw = await self._fetch(
            lambda r: r.fetch_agent(tenant_slug, agent_slug),
            lambda r: r.fetch_agent(tenant_slug, agent_slug),
        )
        return _agent_from_dict(raw) if raw is not None else None

    async def get_provider_config(self, provider_id: str) -> ProviderConfig | None:
        raw = await self._fetch(
            lambda r: r.fetch_provider_config(provider_id), lambda r: r.fetch_provider_config(provider_id),
        )
        return _provider_config_from_dict(raw) if raw is not None else None

    async def get_runtime_config(self, tenant_slug: str, agent_slug: str) -> RuntimeConfig | None:
        agent = await self.get_agent(tenant_slug, agent_slug)
        if agent is None or agent.status != "active":
            return None
        tenant = await self.get_tenant(tenant_slug)
        if tenant is None:
            return None

        provider_ids: dict[str, str] = {}
        for role in _ROLES:
            config_id = getattr(agent, f"{role}_config_id") or getattr(tenant, f"default_{role}_config_id")
            if config_id is None:
                return None
            provider_ids[role] = config_id

        providers: dict[str, ProviderConfig] = {}
        for role, config_id in provider_ids.items():
            cfg = await self.get_provider_config(config_id)
            if cfg is None:
                return None
            providers[role] = cfg

        return RuntimeConfig(
            tenant=tenant,
            agent=agent,
            providers=ProviderConfigs(
                stt=providers["stt"], llm=providers["llm"], tts=providers["tts"],
            ),
            conversation=ConversationInfo(
                greeting=agent.greeting, system_prompt=agent.system_prompt,
                end_call_prompt=agent.end_call_prompt,
                transfer_prompt=agent.transfer_prompt,
                farewell_message=agent.farewell_message,
                transfer_announcement=agent.transfer_announcement,
                workflow=agent.workflow,
            ),
            media=MediaInfo(
                voice=providers["tts"].voice,
                # agent.language is an explicit per-agent override; when
                # unset (the pre-existing behavior), still falls back to
                # whatever the STT/TTS providers themselves are configured
                # with.
                language=agent.language or providers["stt"].language or providers["tts"].language,
            ),
            policies=Policies(
                vad_engine=tenant.vad_engine,
                vad_onset_ms=tenant.vad_onset_ms,
                vad_hold_ms=tenant.vad_hold_ms,
                vad_speech_threshold=tenant.vad_speech_threshold,
                silence_timeout_ms=tenant.no_speech_timeout_ms,
                stt_timeout_ms=tenant.stt_timeout_ms,
                llm_timeout_ms=tenant.llm_timeout_ms,
                transfer_timeout_ms=validate_transfer_timeout_ms(
                    tenant.transfer_timeout_ms
                    if tenant.transfer_timeout_ms is not None
                    else TRANSFER_TIMEOUT_DEFAULT_MS,
                    context=f"tenant={tenant.slug}",
                ),
                goodbye_grace_ms=agent.goodbye_grace_ms,
                transfer_type=agent.transfer_type,
                transfer_destination=agent.transfer_destination,
                queue_id=agent.queue_id,
                escalation_threshold=agent.escalation_threshold,
                caller_id_policy=agent.caller_id_policy,
                platform_did=agent.platform_did,
                custom_caller_id=agent.custom_caller_id,
                transfer_waiting_experience=agent.transfer_waiting_experience,
                max_call_duration_s=agent.max_call_duration_s,
            ),
            tools=await self.get_tools(tenant_slug, agent_slug),
            version=agent.config_version,
            resolved_at=datetime.now(timezone.utc),
        )

    async def get_prompt(self, tenant_slug: str, agent_slug: str) -> Prompt | None:
        agent = await self.get_agent(tenant_slug, agent_slug)
        if agent is None:
            return None
        return Prompt(greeting=agent.greeting, system_prompt=agent.system_prompt)

    async def get_voice(self, tenant_slug: str, agent_slug: str) -> str | None:
        runtime_config = await self.get_runtime_config(tenant_slug, agent_slug)
        return runtime_config.media.voice if runtime_config is not None else None

    async def get_tools(self, tenant_slug: str, agent_slug: str) -> list[ToolSpec]:
        # Tool Orchestrator is Phase 6b, not built — no tools table/column
        # exists yet to source this from. Real, empty, not a stub exception:
        # a caller asking "what tools does this agent have" today correctly
        # gets "none," not an error.
        return []

    async def close(self) -> None:
        # Delegates to whatever the two injected repositories need closed
        # (a redis-py client, an httpx.AsyncClient) — the caller (e.g.
        # services/conversation/__main__.py's shutdown path) closes the
        # provider, never the repositories directly; that would leak the
        # transport detail this whole abstraction exists to hide.
        await self._redis_repo.close()
        await self._http_repo.close()
