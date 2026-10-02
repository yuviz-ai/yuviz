"""
Entry point: python -m services.conversation [--port PORT] [--log-level LEVEL] [--mode MODE]
"""

from __future__ import annotations

# Make generated proto stubs importable as "voiceai.v1.*" (absolute package).
# Must happen before any import that transitively pulls in conversation_pb2_grpc.
import os, sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), "generated"))

import argparse
import asyncio
import logging
import signal
import socket
import sys
from datetime import datetime, timezone

import grpc.aio
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from libs.config_sdk import CacheAsideConfigProvider, HttpConfigRepository, IConfigProvider, RedisConfigRepository
from libs.knowledge_sdk import (
    CacheAsideKnowledgeProvider,
    HttpKnowledgeRepository,
    IKnowledgeProvider,
    RedisKnowledgeRepository,
)

from .agent_config import load_agent, to_runtime_config
from .agent_resolver import resolve_handler_deps
from .ai_provider_manager import AIProviderManager
from .provider_config_subscriber import ProviderConfigSubscriber
from .callflow.handler import CallFlowConversationHandler
from .callflow.resolver import resolve_call_flow
from .callflow.runner import CallFlowRunner
from .echo import EchoConversationHandler
from .fillers import FillerSelector
from .pipeline import PipelineConversationHandler
from .pipeline_config import PipelineConfig
from .provider_bundle import ProviderRegistry, _to_ai_provider_config
from .providers.stt.faster_whisper import FasterWhisperSTT
from .providers.llm.ollama import OllamaLLM
from .secret_resolver import CompositeSecretResolver
from .servicer import ConversationServicer
from .session import SessionContext
from .tools.executor_registry import ExecutorRegistry
from .tools.executors.api_exec_executor import ApiExecExecutor
from .tools.llm_adapter import LLMAdapter
from .tools.orchestrator import ToolCallOrchestrator
from .tools.policy_resolver import ToolPolicyResolver
from .tools.provider_manager import ToolProviderManager
from .tools.registry import ToolRegistry
from .providers.llm.openai import OpenAILLM
from .sentiment import SentimentScorer
from .tool_latency import ToolLatencyStore
from .transcript_builder import TranscriptBuilder
from .workflow import graph_for
from .generated.voiceai.v1 import conversation_pb2_grpc as pb_grpc

SERVICE_NAME = "voiceai.v1.ConversationService"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="VoiceAI ConversationService")
    p.add_argument("--port",      type=int,   default=50051)
    p.add_argument("--log-level", type=str,   default="INFO")
    p.add_argument("--mode",      type=str,   default="pipeline",
                   choices=["pipeline", "echo"],
                   help="pipeline = real STT/LLM/TTS; echo = loopback for testing")
    return p.parse_args(argv)


def _build_tts(cfg: PipelineConfig):
    engine = cfg.tts.engine
    if engine == "kokoro":
        from .providers.tts.kokoro import KokoroTTS
        return KokoroTTS(voice=cfg.tts.voice, speed=cfg.tts.kokoro_speed,
                         lang_code=cfg.tts.lang_code)
    # Default: macOS built-in TTS — always available, zero extra dependencies.
    from .providers.tts.macos import MacOSTTS
    return MacOSTTS(voice=cfg.tts.voice, speed=cfg.tts.macos_wpm)


def _enabled(leg: str) -> bool:
    """VOICEAI_ENABLE_STT/TTS (dev.sh --no-stt/--no-tts); anything but "0" means on."""
    return os.environ.get(f"VOICEAI_ENABLE_{leg}", "1") != "0"


async def _prewarm_agents(
    http_config_repo: HttpConfigRepository,
    provider_registry: ProviderRegistry,
    config: IConfigProvider,
) -> None:
    """Load every active agent's providers at startup so a first call never pays
    model-load cost mid-call. Never raises; failures fall back to on-demand loading."""
    log = logging.getLogger(__name__)

    # resolve_handler_deps() builds all three providers together, so one leg can't be warmed alone.
    if not _enabled("STT") or not _enabled("TTS"):
        log.info("prewarm: skipped (stt=%s tts=%s) — providers load on first use",
                 _enabled("STT"), _enabled("TTS"))
        return

    try:
        tenants = await http_config_repo.list_tenants()
    except Exception:
        log.exception("prewarm: could not list tenants — skipping prewarm")
        return

    for tenant in tenants:
        tenant_slug = tenant.get("slug")
        if not tenant_slug:
            continue
        try:
            agents = await http_config_repo.list_agents(tenant_slug)
        except Exception:
            log.exception("prewarm: could not list agents tenant=%s — skipping", tenant_slug)
            continue

        for agent in agents:
            if agent.get("status") != "active":
                continue
            agent_slug = agent.get("slug")
            if not agent_slug:
                continue
            try:
                resolved = await resolve_handler_deps(tenant_slug, agent_slug, provider_registry, config)
            except Exception:
                resolved = None
            if resolved is None:
                log.warning("prewarm: tenant=%s agent=%s did not resolve — skipping", tenant_slug, agent_slug)
                continue
            _, bundle = resolved
            graph = graph_for(resolved[0])
            # Ollama only loads the model on a real request; no-op for cloud LLMs.
            warm = getattr(bundle.llm, "warm", None)
            if warm is not None:
                try:
                    await warm()
                except Exception:
                    log.exception("prewarm: LLM warm() failed tenant=%s agent=%s", tenant_slug, agent_slug)
            log.info(
                "prewarm: tenant=%s agent=%s providers ready, workflow graph parsed (%d nodes)",
                tenant_slug, agent_slug, len(graph.nodes),
            )


async def serve(port: int, args: argparse.Namespace) -> None:
    log = logging.getLogger(__name__)
    cfg = PipelineConfig()

    if not os.environ.get("VOICEAI_LLM_MODEL"):
        try:
            import asyncpg
            conn = await asyncpg.connect(dsn=os.environ.get("POSTGRES_DSN"))
            try:
                model = await conn.fetchval(
                    "SELECT pc.model FROM tenants t "
                    "JOIN provider_configs pc ON pc.id = t.default_llm_config_id "
                    "WHERE t.slug = 'default' AND pc.model IS NOT NULL"
                )
            finally:
                await conn.close()
            if model:
                cfg.llm.model = model
                log.info("Legacy LLM fallback model resolved from tenant 'default' config: %s", model)
        except Exception:
            log.exception("Failed to resolve legacy LLM fallback model from DB — keeping built-in default %s", cfg.llm.model)

    # Several Conversation processes run behind Envoy; hostname:port identifies this one.
    node_id = f"{socket.gethostname()}:{port}"

    # Sentiment uses its own hosted model; without a key calls.sentiment stays NULL.
    sentiment_scorer: SentimentScorer | None = None
    sentiment_llm: OpenAILLM | None = None
    if args.mode != "echo" and cfg.sentiment.api_key:
        sentiment_llm = OpenAILLM(
            api_key=cfg.sentiment.api_key,
            model=cfg.sentiment.model,
            system="",  # SentimentScorer injects its own system message
            temperature=0.0,
            base_url=cfg.sentiment.base_url,
            timeout_s=cfg.sentiment.timeout_s,
        )
        sentiment_scorer = SentimentScorer(
            sentiment_llm,
            max_turns=cfg.sentiment.max_turns,
            timeout_s=cfg.sentiment.timeout_s,
        )
    elif args.mode != "echo":
        log.info(
            "Call-sentiment scoring disabled — no VOICEAI_SENTIMENT_API_KEY/OPENAI_API_KEY",
        )

    transcripts = await TranscriptBuilder.connect(
        cfg.db.database_url, node_id=node_id, sentiment=sentiment_scorer,
    )
    await transcripts.reconcile_stale_calls()
    provider_manager = AIProviderManager(CompositeSecretResolver())
    provider_registry = ProviderRegistry(provider_manager)
    provider_config_subscriber = ProviderConfigSubscriber(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"), provider_manager,
    )
    provider_config_subscriber.start()

    # Authenticates lazily, so bad service-account creds fall back to YAML rather than crash.
    http_config_repo = HttpConfigRepository(
        base_url=os.environ.get("CONFIG_SERVICE_URL", "http://localhost:8000"),
        service_email=os.environ.get("CONFIG_SERVICE_EMAIL", ""),
        service_password=os.environ.get("CONFIG_SERVICE_PASSWORD", ""),
    )
    config: IConfigProvider = CacheAsideConfigProvider(
        redis_repo=RedisConfigRepository(os.environ.get("REDIS_URL", "redis://localhost:6379/0")),
        http_repo=http_config_repo,
    )

    # Degrades to "no context" when a backend is unreachable.
    knowledge: IKnowledgeProvider = CacheAsideKnowledgeProvider(
        availability_repo=RedisKnowledgeRepository(os.environ.get("REDIS_URL", "redis://localhost:6379/0")),
        retrieval_repo=HttpKnowledgeRepository(
            base_url=os.environ.get("KNOWLEDGE_SERVICE_URL", "http://localhost:8100"),
            auth_base_url=os.environ.get("CONFIG_SERVICE_URL", "http://localhost:8000"),
            service_email=os.environ.get("CONFIG_SERVICE_EMAIL", ""),
            service_password=os.environ.get("CONFIG_SERVICE_PASSWORD", ""),
        ),
    )

    # Tool framework objects are shared across streams; LLMAdapter is per handler
    # because agents can resolve to different LLMs.
    tool_registry = ToolRegistry()
    executor_registry = ExecutorRegistry()

    # Process-scoped so filler calibration survives across calls; keys are tenant/agent-scoped.
    tool_latency_store = ToolLatencyStore()
    filler_selector = FillerSelector()
    # execute_api is the only DB-gated tool. Per-agent max_chain_depth arrives via
    # ToolExecutionContext, since this factory is shared by every tenant.
    executor_registry.register("execute_api", lambda provider: ApiExecExecutor(provider))
    tool_provider_manager = ToolProviderManager(CompositeSecretResolver())
    tool_policy_resolver = await ToolPolicyResolver.connect(
        os.environ.get("POSTGRES_DSN"), tool_registry,
    )

    # Providers (STT/LLM/TTS) are shared across streams because they are stateless
    # or internally thread-safe. Only the handler (which holds per-session history
    # and cancel state) is constructed fresh per Converse() stream.
    # STT construction is fast (no model load); load() is called asynchronously
    # after server.start() so Envoy health checks don't time out.
    stt: FasterWhisperSTT | None = None
    llm: OllamaLLM | None = None
    if args.mode == "echo":
        log.info("Handler mode: echo (loopback)")

        async def handler_factory(ctx: SessionContext) -> EchoConversationHandler:
            return EchoConversationHandler()
    else:
        log.info("Handler mode: pipeline (FasterWhisper→Ollama→%s-TTS)", cfg.tts.engine)
        stt = FasterWhisperSTT(
            model_size=cfg.stt.model_size,
            device=cfg.stt.device,
            compute_type=cfg.stt.compute_type,
            language=cfg.stt.language,
        )
        llm = OllamaLLM(
            model=cfg.llm.model,
            system=cfg.llm.system,
            temperature=cfg.llm.temperature,
            base_url=cfg.llm.base_url,
            timeout_s=cfg.llm.timeout_s,
        )
        tts = _build_tts(cfg)

        async def _build_pipeline_handler(
            ctx: SessionContext, runtime_config, bundle,
            *, initial_variables: dict | None = None,
        ) -> PipelineConversationHandler:
            """Build a pipeline handler; shared by direct calls and call-flow handoffs."""
            tool_orchestrator = ToolCallOrchestrator(
                llm_adapter=LLMAdapter(bundle.llm),
                policy_resolver=tool_policy_resolver,
                provider_manager=tool_provider_manager,
                executor_registry=executor_registry,
                latency_store=tool_latency_store,
            )

            # "Can act" (gates caller-ID prompt block and booking-claim guard) means execute_api is enabled.
            enabled_policies = await tool_policy_resolver.enabled_tools(
                runtime_config.agent.id, runtime_config.tenant.slug,
            )
            has_booking_tool = any(p.definition.name == "execute_api" for p in enabled_policies)

            return PipelineConversationHandler(
                runtime_config, bundle,
                sample_rate=cfg.sample_rate,
                max_history=cfg.max_history,
                default_system_prompt=cfg.llm.system,
                transcripts=transcripts,
                tenant_id=ctx.tenant_id,
                call_id=ctx.call_id,
                direction=ctx.direction,
                tool_orchestrator=tool_orchestrator,
                caller_number=ctx.caller_did,
                called_number=ctx.called_did,
                knowledge=knowledge,
                has_booking_tool=has_booking_tool,
                initial_variables=initial_variables,
                latency_store=tool_latency_store,
                filler_selector=filler_selector,
            )

        async def handler_factory(ctx: SessionContext) -> PipelineConversationHandler:
            # Config SDK path, else legacy YAML path; never a mix for one call.
            resolved = await resolve_handler_deps(
                ctx.tenant_id or "default", ctx.script_id or "default", provider_registry, config,
            )
            if resolved is not None:
                runtime_config, bundle = resolved
            else:
                agent = load_agent(ctx.script_id)
                runtime_config, bundle = to_runtime_config(
                    agent, ctx.tenant_id or "default", ctx.script_id or "default", stt, llm, tts,
                )

            if not runtime_config.agent.call_flow_id:
                return await _build_pipeline_handler(ctx, runtime_config, bundle)

            # On flow-resolution failure fall back to the ordinary agent rather than drop the call.
            resolved_flow = await resolve_call_flow(runtime_config, config)
            if resolved_flow is None:
                return await _build_pipeline_handler(ctx, runtime_config, bundle)
            graph, flow = resolved_flow

            now = datetime.now(timezone.utc)
            call_context_variables = {
                "caller_number": ctx.caller_did,
                "called_number": ctx.called_did,
                "direction":     ctx.direction,
                "agent_name":    runtime_config.agent.name,
                "business_name": runtime_config.tenant.name,
                "current_date":  now.strftime("%Y-%m-%d"),
                "current_time":  now.strftime("%H:%M"),
            }
            runner = CallFlowRunner(
                graph, tts_config_id=flow.resolved_tts_config_id, variables=call_context_variables,
            )

            async def handoff(agent_slug: str, variables: dict) -> PipelineConversationHandler | None:
                target = await resolve_handler_deps(
                    runtime_config.tenant.slug, agent_slug, provider_registry, config,
                )
                if target is None:
                    return None
                target_runtime_config, target_bundle = target
                return await _build_pipeline_handler(
                    ctx, target_runtime_config, target_bundle, initial_variables=variables,
                )

            async def voice_for(tts_config_id: str):
                # Unscoped lookup is safe: Config Service already validated this id same-tenant, role='tts'.
                provider_config = await config.get_provider_config(tts_config_id)
                if provider_config is None:
                    return None
                return await provider_manager.get(_to_ai_provider_config(provider_config))

            return CallFlowConversationHandler(
                runner,
                tts=bundle.tts,
                sample_rate=cfg.sample_rate,
                session_id=ctx.session_id,
                tenant_id=ctx.tenant_id,
                call_id=ctx.call_id,
                handoff=handoff,
                voice_for=voice_for,
                agent_slugs=flow.agent_slugs,
            )

    # Disable SO_REUSEPORT so a duplicate process fails to bind instead of silently sharing the port.
    server = grpc.aio.server(options=[("grpc.so_reuseport", 0)])

    # Conversation service
    pb_grpc.add_ConversationServiceServicer_to_server(
        ConversationServicer(handler_factory),
        server,
    )

    # Health check — stays NOT_SERVING until the Whisper model finishes loading.
    health_servicer = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    health_servicer.set(SERVICE_NAME, health_pb2.HealthCheckResponse.NOT_SERVING)

    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)
    await server.start()
    log.info("ConversationService listening on %s mode=%s (NOT_SERVING — loading model)",
             listen_addr, args.mode)

    async def _load_and_promote() -> None:
        if stt is not None:
            await stt.load()
        if args.mode != "echo":
            await _prewarm_agents(http_config_repo, provider_registry, config)
        health_servicer.set(SERVICE_NAME, health_pb2.HealthCheckResponse.SERVING)
        log.info("ConversationService SERVING")

    load_task = asyncio.create_task(_load_and_promote())

    # Node is considered dead after 3 missed heartbeats (45s).
    HEARTBEAT_INTERVAL_S = 15
    # Gateway ends silent callers in ~19s, so minutes without transcript activity means a zombie call.
    INACTIVE_CALL_TIMEOUT_S = 300

    async def _heartbeat_loop() -> None:
        while True:
            await transcripts.heartbeat()
            await transcripts.reconcile_dead_nodes(stale_after_seconds=HEARTBEAT_INTERVAL_S * 3)
            await transcripts.reconcile_inactive_calls(inactive_after_seconds=INACTIVE_CALL_TIMEOUT_S)
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    heartbeat_task = asyncio.create_task(_heartbeat_loop())

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    loop.add_signal_handler(signal.SIGINT,  stop.set_result, None)
    loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)

    try:
        await stop
        load_task.cancel()
        heartbeat_task.cancel()
        for task in (load_task, heartbeat_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await provider_config_subscriber.stop()
        log.info("Shutting down…")
        health_servicer.set(SERVICE_NAME, health_pb2.HealthCheckResponse.NOT_SERVING)
        await server.stop(grace=5)
    finally:
        if llm is not None:
            await llm.aclose()
        # Before sentiment_llm: transcripts.close() drains in-flight end_call
        # chains, and a chain still scoring needs its HTTP client alive.
        await transcripts.close()
        if sentiment_llm is not None:
            await sentiment_llm.aclose()
        await config.close()
        await knowledge.close()
        await tool_policy_resolver.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(serve(args.port, args))


if __name__ == "__main__":
    main()
