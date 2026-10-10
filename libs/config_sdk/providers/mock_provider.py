"""In-memory IConfigProvider for tests; zero I/O."""

from __future__ import annotations

from datetime import datetime, timezone

from libs.config_sdk.workflow import starter_graph

from ..languages import resolve_languages, same_tenant_tts_overrides
from ..models import (
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
)

_ROLES = ("stt", "llm", "tts")


class MockConfigProvider:
    def __init__(self) -> None:
        self.tenants: dict[str, Tenant] = {}
        self.agents: dict[tuple[str, str], Agent] = {}
        self.provider_configs: dict[str, ProviderConfig] = {}
        self.tools: dict[tuple[str, str], list[ToolSpec]] = {}

    # ── Test setup helpers ──────────────────────────────────────────────

    def add_tenant(self, **kwargs) -> Tenant:
        defaults = dict(
            id="tenant-id", region="us", vad_engine=None, vad_onset_ms=None, vad_hold_ms=None,
            vad_speech_threshold=None, no_speech_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None,
            transfer_timeout_ms=None,
            default_stt_config_id=None, default_llm_config_id=None, default_tts_config_id=None,
            config_version=1, updated_at=datetime.now(timezone.utc),
        )
        tenant = Tenant(**{**defaults, **kwargs})
        self.tenants[tenant.slug] = tenant
        return tenant

    def add_agent(self, tenant_slug: str, **kwargs) -> Agent:
        defaults = dict(
            id="agent-id", tenant_id="tenant-id", greeting="", system_prompt="",
            goodbye_grace_ms=3000, stt_config_id=None, llm_config_id=None, tts_config_id=None,
            status="active", config_version=1, updated_at=datetime.now(timezone.utc),
        )
        agent = Agent(**{**defaults, **kwargs})
        self.agents[(tenant_slug, agent.slug)] = agent
        return agent

    def add_provider_config(self, **kwargs) -> ProviderConfig:
        defaults = dict(model=None, voice=None, language=None, api_key_ref=None, extra={}, updated_at=None)
        cfg = ProviderConfig(**{**defaults, **kwargs})
        self.provider_configs[cfg.id] = cfg
        return cfg

    # ── IConfigProvider ──────────────────────────────────────────────────

    async def get_tenant(self, tenant_slug: str) -> Tenant | None:
        return self.tenants.get(tenant_slug)

    async def get_agent(self, tenant_slug: str, agent_slug: str) -> Agent | None:
        return self.agents.get((tenant_slug, agent_slug))

    async def get_provider_config(self, provider_id: str) -> ProviderConfig | None:
        return self.provider_configs.get(provider_id)

    async def get_runtime_config(
        self, tenant_slug: str, agent_slug: str, *, include_inactive: bool = False,
    ) -> RuntimeConfig | None:
        agent = await self.get_agent(tenant_slug, agent_slug)
        if agent is None or (agent.status != "active" and not include_inactive):
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

        langs = resolve_languages(agent, providers["stt"], providers["tts"])
        tts_by_language = {}
        if langs.supported_languages and agent.tts_config_by_language:
            tts_by_language = same_tenant_tts_overrides(agent, langs.supported_languages, {
                lang: await self.get_provider_config(config_id)
                for lang, config_id in agent.tts_config_by_language.items()
            })

        graph = agent.workflow or starter_graph(agent.greeting, agent.system_prompt)
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
                workflow=graph,
                workflow_draft=agent.workflow_draft or graph,
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
                goodbye_grace_ms=agent.goodbye_grace_ms,
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
        return self.tools.get((tenant_slug, agent_slug), [])

    async def close(self) -> None:
        pass  # no real transport to close
