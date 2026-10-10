"""Production IConfigProvider: Redis first, HTTP fallback, raw dicts mapped to models.

Never writes back to Redis: Config Service's GET handlers own that (one writer per key).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..exceptions import ConfigUnavailableError, RepositoryUnavailableError
from ..interfaces import IConfigRepository
from ..languages import resolve_languages, same_tenant_tts_overrides
from ..models import (
    TRANSFER_TIMEOUT_DEFAULT_MS,
    Agent,
    CallFlow,
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

# Outage handling: well inside the Gateway's 10 s wait for the session to be ready.
_RESOLVE_DEADLINE_S = 2.0
_BREAKER_THRESHOLD = 3
_BREAKER_OPEN_S = 10.0


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _parse_extra(value: Any) -> dict[str, Any]:
    parsed = _parse_json(value)
    return parsed if isinstance(parsed, dict) else {}


def _parse_json(value: Any) -> Any:
    """JSONB / Redis-cached JSON may arrive as a string or already-parsed."""
    import json
    if value is None:
        return None
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
        call_flow_id=row.get("call_flow_id"),
        supported_languages=tuple(row["supported_languages"]) if row.get("supported_languages") else None,
        tts_config_by_language=_parse_json(row.get("tts_config_by_language")) or None,
        greeting_by_language=_parse_json(row.get("greeting_by_language")) or None,
    )


def _call_flow_from_dict(row: dict[str, Any]) -> CallFlow:
    return CallFlow(
        id=str(row["id"]),
        tenant_slug=row["tenant_slug"],
        config_version=row.get("config_version", 0),
        graph=_parse_json(row.get("graph")) or {},
        agent_slugs=_parse_json(row.get("agent_slugs")) or {},
        resolved_tts_config_id=row.get("resolved_tts_config_id"),
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
        tenant_id=str(row["tenant_id"]) if row.get("tenant_id") is not None else None,
    )


class CacheAsideConfigProvider:
    def __init__(self, redis_repo: IConfigRepository, http_repo: IConfigRepository) -> None:
        self._redis_repo = redis_repo
        self._http_repo = http_repo
        self._last_good: dict[tuple[str, str], RuntimeConfig] = {}
        self._failures = 0
        self._open_until = 0.0

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= _BREAKER_THRESHOLD:
            self._open_until = time.monotonic() + _BREAKER_OPEN_S
            log.warning("CacheAsideConfigProvider: config stores unavailable, skipping them for %.0fs",
                        _BREAKER_OPEN_S)

    async def _fetch(self, redis_call, http_call) -> dict[str, Any] | None:
        """Raw dict, None if not found, or RepositoryUnavailableError if neither store answered."""
        if time.monotonic() < self._open_until:
            raise RepositoryUnavailableError("config stores unavailable (circuit open)")
        try:
            raw = await redis_call(self._redis_repo)
            if raw is not None:
                self._failures = 0
                return raw
        except RepositoryUnavailableError:
            pass
        try:
            raw = await http_call(self._http_repo)
        except RepositoryUnavailableError:
            self._record_failure()
            raise
        self._failures = 0
        return raw

    async def _lenient(self, fetch) -> dict[str, Any] | None:
        try:
            return await fetch
        except RepositoryUnavailableError:
            log.warning("CacheAsideConfigProvider: config stores unavailable", exc_info=True)
            return None

    def _agent_raw(self, tenant_slug: str, agent_slug: str):
        return self._fetch(
            lambda r: r.fetch_agent(tenant_slug, agent_slug),
            lambda r: r.fetch_agent(tenant_slug, agent_slug),
        )

    def _tenant_raw(self, tenant_slug: str):
        return self._fetch(lambda r: r.fetch_tenant(tenant_slug), lambda r: r.fetch_tenant(tenant_slug))

    def _provider_raw(self, provider_id: str):
        return self._fetch(
            lambda r: r.fetch_provider_config(provider_id), lambda r: r.fetch_provider_config(provider_id),
        )

    async def get_tenant(self, tenant_slug: str) -> Tenant | None:
        raw = await self._lenient(self._tenant_raw(tenant_slug))
        return _tenant_from_dict(raw) if raw is not None else None

    async def get_agent(self, tenant_slug: str, agent_slug: str) -> Agent | None:
        raw = await self._lenient(self._agent_raw(tenant_slug, agent_slug))
        return _agent_from_dict(raw) if raw is not None else None

    async def get_provider_config(self, provider_id: str) -> ProviderConfig | None:
        raw = await self._lenient(self._provider_raw(provider_id))
        return _provider_config_from_dict(raw) if raw is not None else None

    async def get_call_flow(self, tenant_slug: str, call_flow_id: str) -> CallFlow | None:
        raw = await self._lenient(self._fetch(
            lambda r: r.fetch_call_flow(tenant_slug, call_flow_id),
            lambda r: r.fetch_call_flow(tenant_slug, call_flow_id),
        ))
        return _call_flow_from_dict(raw) if raw is not None else None

    async def get_runtime_config(
        self, tenant_slug: str, agent_slug: str, *, include_inactive: bool = False,
    ) -> RuntimeConfig | None:
        """None means the agent is genuinely missing/inactive/incomplete; an outage serves the
        last-known-good copy, or raises ConfigUnavailableError when there is none."""
        key = (tenant_slug, agent_slug)
        try:
            config = await asyncio.wait_for(
                self._build_runtime_config(tenant_slug, agent_slug, include_inactive),
                _RESOLVE_DEADLINE_S,
            )
        except (RepositoryUnavailableError, asyncio.TimeoutError):
            cached = self._last_good.get(key)
            if cached is None or (cached.agent.status != "active" and not include_inactive):
                raise ConfigUnavailableError(f"no config for tenant={tenant_slug} agent={agent_slug}")
            log.warning("CacheAsideConfigProvider: serving last-known-good config tenant=%s agent=%s",
                        tenant_slug, agent_slug)
            return cached
        if config is None:
            self._last_good.pop(key, None)
        else:
            self._last_good[key] = config
        return config

    async def _build_runtime_config(
        self, tenant_slug: str, agent_slug: str, include_inactive: bool,
    ) -> RuntimeConfig | None:
        raw_agent = await self._agent_raw(tenant_slug, agent_slug)
        agent = _agent_from_dict(raw_agent) if raw_agent is not None else None
        if agent is None or (agent.status != "active" and not include_inactive):
            return None
        raw_tenant = await self._tenant_raw(tenant_slug)
        if raw_tenant is None:
            return None
        tenant = _tenant_from_dict(raw_tenant)

        provider_ids: dict[str, str] = {}
        for role in _ROLES:
            config_id = getattr(agent, f"{role}_config_id") or getattr(tenant, f"default_{role}_config_id")
            if config_id is None:
                return None
            provider_ids[role] = config_id

        providers: dict[str, ProviderConfig] = {}
        for role, config_id in provider_ids.items():
            raw_provider = await self._provider_raw(config_id)
            if raw_provider is None:
                return None
            providers[role] = _provider_config_from_dict(raw_provider)

        langs = resolve_languages(agent, providers["stt"], providers["tts"])
        tts_by_language = {}
        if langs.supported_languages and agent.tts_config_by_language:
            tts_by_language = same_tenant_tts_overrides(agent, langs.supported_languages, {
                lang: await self.get_provider_config(config_id)
                for lang, config_id in agent.tts_config_by_language.items()
            })

        return RuntimeConfig(
            tenant=tenant,
            agent=agent,
            providers=ProviderConfigs(
                stt=providers["stt"], llm=providers["llm"], tts=providers["tts"],
                tts_by_language=tts_by_language,
            ),
            conversation=ConversationInfo(
                greeting=agent.greeting, system_prompt=agent.system_prompt,
                end_call_prompt=agent.end_call_prompt,
                transfer_prompt=agent.transfer_prompt,
                farewell_message=agent.farewell_message,
                transfer_announcement=agent.transfer_announcement,
                workflow=agent.workflow,
                workflow_draft=agent.workflow_draft,
                greeting_by_language=agent.greeting_by_language,
            ),
            media=MediaInfo(
                voice=providers["tts"].voice,
                language=agent.language or providers["stt"].language or providers["tts"].language,
                stt_language=langs.stt_language,
                tts_language=langs.tts_language,
                default_language=langs.default_language,
                supported_languages=langs.supported_languages,
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
        # No tools source exists yet.
        return []

    async def close(self) -> None:
        await self._redis_repo.close()
        await self._http_repo.close()
