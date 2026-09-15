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
from .echo import EchoConversationHandler
from .fillers import FillerSelector
from .pipeline import PipelineConversationHandler
from .pipeline_config import PipelineConfig
from .provider_bundle import ProviderRegistry
from .providers.stt.faster_whisper import FasterWhisperSTT
from .providers.llm.ollama import OllamaLLM
from .secret_resolver import CompositeSecretResolver
from .servicer import ConversationServicer
from .session import SessionContext
from .tools.executor_registry import ExecutorRegistry
from .tools.executors.calendar_executor import CalendarExecutor
from .tools.executors.cancel_appointment_executor import CancelAppointmentExecutor
from .tools.executors.reschedule_appointment_executor import RescheduleAppointmentExecutor
from .tools.llm_adapter import LLMAdapter
from .tools.orchestrator import ToolCallOrchestrator
from .tools.policy_resolver import ToolPolicyResolver
from .tools.provider_manager import ToolProviderManager
from .tools.registry import ToolRegistry
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
    """VOICEAI_ENABLE_{STT,TTS}; "0" skips prewarm (dev.sh --no-stt/--no-tts)."""
    return os.environ.get(f"VOICEAI_ENABLE_{leg}", "1") != "0"


async def _prewarm_agents(
    http_config_repo: HttpConfigRepository,
    provider_registry: ProviderRegistry,
    config: IConfigProvider,
    cfg: PipelineConfig,
) -> None:
    """Prewarm STT/LLM/TTS (+ graph) for every active agent at startup."""
    log = logging.getLogger(__name__)

    # Either flag skips prewarm (providers resolve together).
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
            if not _enabled("STT") or not _enabled("TTS"):
                # Either flag skips prewarm (deps resolve STT+LLM+TTS together).
                log.info("prewarm: tenant=%s agent=%s skipped (stt=%s tts=%s)",
                         tenant_slug, agent_slug, _enabled("STT"), _enabled("TTS"))
                continue
            try:
                resolved = await resolve_handler_deps(tenant_slug, agent_slug, provider_registry, config)
            except Exception:
                resolved = None
            if resolved is None:
                log.warning("prewarm: tenant=%s agent=%s did not resolve — skipping", tenant_slug, agent_slug)
                continue
            _, bundle = resolved
            # Warm graph cache; parse failure here is a log + starter fallback.
            graph = graph_for(resolved[0])
            # Object construction != model loaded — Ollama needs a real
            # request first (see OllamaLLM.warm()). No-op for cloud LLMs.
            warm = getattr(bundle.llm, "warm", None)
            if warm is not None:
                try:
                    await warm()
                except Exception:
                    log.exception("prewarm: LLM warm() failed tenant=%s agent=%s", tenant_slug, agent_slug)
            # Warm Kokoro voice file (~9s HF fetch on first real synth otherwise).
            try:
                async for _chunk in bundle.tts.synthesize_stream("Hello.", cfg.sample_rate):
                    break
            except Exception:
                log.warning("prewarm: TTS warm-up failed tenant=%s agent=%s — the first call "
                            "will pay the voice-load cost", tenant_slug, agent_slug, exc_info=True)
            log.info(
                "prewarm: tenant=%s agent=%s providers ready%s",
                tenant_slug, agent_slug,
                f", workflow graph parsed ({len(graph.nodes)} nodes)" if graph else "",
            )


async def serve(port: int, args: argparse.Namespace) -> None:
    log = logging.getLogger(__name__)
    cfg = PipelineConfig()

    # hostname:port identifies this specific process instance (see
    # TranscriptBuilder's docstring on conv_node/reconcile_stale_calls) —
    # this project runs two Conversation Service processes (:50051,
    # :50052) behind Envoy, so "this process" is never "the only process."
    node_id = f"{socket.gethostname()}:{port}"
    transcripts = await TranscriptBuilder.connect(cfg.db.database_url, node_id=node_id)
    await transcripts.reconcile_stale_calls()
    provider_manager = AIProviderManager(CompositeSecretResolver())
    provider_registry = ProviderRegistry(provider_manager)
    provider_config_subscriber = ProviderConfigSubscriber(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"), provider_manager,
    )
    provider_config_subscriber.start()

    # Config SDK — the only way this process talks to Config Service now
    # (see libs/config_sdk/__init__.py and agent_resolver.py's docstring for
    # why: no services.config import here or anywhere else in this
    # package). Construction never fails even with blank/wrong service-
    # account creds — HttpConfigRepository authenticates lazily on first
    # fetch, and resolve_handler_deps() already degrades to the legacy YAML
    # path on any resolution failure, so a misconfigured service account
    # shows up as "always falls back," never a crash at startup.
    http_config_repo = HttpConfigRepository(
        base_url=os.environ.get("CONFIG_SERVICE_URL", "http://localhost:8000"),
        service_email=os.environ.get("CONFIG_SERVICE_EMAIL", ""),
        service_password=os.environ.get("CONFIG_SERVICE_PASSWORD", ""),
    )
    config: IConfigProvider = CacheAsideConfigProvider(
        redis_repo=RedisConfigRepository(os.environ.get("REDIS_URL", "redis://localhost:6379/0")),
        http_repo=http_config_repo,
    )

    # Knowledge SDK — same construction pattern as Config SDK: a Redis
    # boolean pre-check (has this agent got any enabled KB?) plus an HTTP
    # fallback/real-retrieval repository against Knowledge Service. Never
    # fails at startup for the same reasons Config SDK's construction
    # doesn't — CacheAsideKnowledgeProvider degrades to "no context" on
    # any unreachable backend, not a crash (see cache_aside.py).
    knowledge: IKnowledgeProvider = CacheAsideKnowledgeProvider(
        availability_repo=RedisKnowledgeRepository(os.environ.get("REDIS_URL", "redis://localhost:6379/0")),
        retrieval_repo=HttpKnowledgeRepository(
            base_url=os.environ.get("KNOWLEDGE_SERVICE_URL", "http://localhost:8100"),
            auth_base_url=os.environ.get("CONFIG_SERVICE_URL", "http://localhost:8000"),
            service_email=os.environ.get("CONFIG_SERVICE_EMAIL", ""),
            service_password=os.environ.get("CONFIG_SERVICE_PASSWORD", ""),
        ),
    )

    # Tool Execution Framework — shared across every stream, same
    # "stateless/thread-safe, constructed once" posture as the STT/LLM/TTS
    # providers just below. ToolRegistry/ExecutorRegistry are pure static
    # catalogs; ToolProviderManager caches provider instances by
    # tool_provider_config.id (mirrors AIProviderManager exactly);
    # ToolPolicyResolver owns its own asyncpg pool (see its own docstring
    # for why this is a direct-Postgres v1 simplification, not the full
    # Config SDK cache-aside pattern). LLMAdapter, by contrast, wraps one
    # specific ILLM instance and must be constructed per-handler below,
    # since different tenants/agents can resolve to different LLM engines.
    tool_registry = ToolRegistry()
    executor_registry = ExecutorRegistry()
    # Booking-confirmation SMS is its own independently configured tool
    # ("send_sms", engine="twilio") — ToolCallOrchestrator resolves it as
    # book_appointment's companion (see ToolDefinition.companion_tool_name)
    # and passes it here as the second argument.
    executor_registry.register(
        "book_appointment",
        lambda provider, companion=None: CalendarExecutor(provider, sms_provider=companion),
    )
    executor_registry.register("cancel_appointment", lambda provider, companion=None: CancelAppointmentExecutor(provider))
    executor_registry.register("reschedule_appointment", lambda provider, companion=None: RescheduleAppointmentExecutor(provider))
    tool_provider_manager = ToolProviderManager(CompositeSecretResolver())
    tool_policy_resolver = await ToolPolicyResolver.connect(
        os.environ.get("POSTGRES_DSN"), tool_registry,
    )

    # Dynamic call fillers: one process-scoped store/selector, shared across
    # every stream — calibration must survive across calls, and both are
    # safe to share because the store's keys are tenant/agent-scoped and its
    # eviction budget is per tenant (see tool_latency.py).
    tool_latency_store = ToolLatencyStore()
    filler_selector = FillerSelector()

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

        async def handler_factory(ctx: SessionContext) -> PipelineConversationHandler:
            # Live path: one Config SDK call resolves tenant + agent + all
            # three provider roles into an immutable RuntimeConfig (Redis-
            # cached, HTTP fallback to Config Service), then ProviderRegistry
            # turns that into live instances. Falls back to the legacy YAML/
            # global-singleton path below on any miss — never a mix of the
            # two for one call (see agent_resolver.py). Both paths produce
            # the exact same (RuntimeConfig, ProviderBundle) shape, so
            # PipelineConversationHandler has one construction contract
            # regardless of which path resolved it.
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

            # Draft off agent Redis/GET; chat draft loads via /workflow.
            if ctx.use_workflow_draft:
                from dataclasses import replace as _dc_replace
                try:
                    state = await http_config_repo.fetch_agent_workflow(
                        ctx.tenant_id or "", runtime_config.agent.id,
                    )
                    draft = (state or {}).get("workflow_draft")
                except Exception:
                    log.exception(
                        "Failed to fetch workflow draft tenant=%s agent=%s",
                        ctx.tenant_id, runtime_config.agent.slug,
                    )
                    draft = None
                runtime_config = _dc_replace(
                    runtime_config,
                    conversation=_dc_replace(
                        runtime_config.conversation, workflow_draft=draft,
                    ),
                )

            # Whether the caller-ID-confirmation prompt block makes any
            # sense for this agent at all — it talks about "before
            # booking," which is actively confusing (and contradicts a
            # reception-only agent's own "you cannot book" instruction) if
            # book_appointment isn't actually enabled for it.
            enabled_policies = await tool_policy_resolver.enabled_tools(runtime_config.agent.id)
            has_booking_tool = any(p.definition.name == "book_appointment" for p in enabled_policies)
            booking_policy = next((p for p in enabled_policies if p.definition.name == "book_appointment"), None)
            reschedule_policy = next(
                (p for p in enabled_policies if p.definition.name == "reschedule_appointment"), None,
            )
            # Same field _make_cal_com reads (provider_manager.py) — not a
            # new timezone convention, just reused for date grounding too.
            # Sourced from whichever calendar tool is actually enabled — a
            # reschedule-only agent has no booking_policy, so gating this on
            # has_booking_tool alone silently used UTC for its date
            # grounding and requested-date validation. has_booking_tool
            # itself stays booking-specific below (caller-ID confirmation
            # prompt), which genuinely doesn't apply to reschedule.
            calendar_policy = booking_policy or reschedule_policy
            calendar_timezone = (calendar_policy.extra.get("timezone") if calendar_policy else None) or "UTC"

            tool_orchestrator = ToolCallOrchestrator(
                llm_adapter=LLMAdapter(bundle.llm),
                policy_resolver=tool_policy_resolver,
                provider_manager=tool_provider_manager,
                executor_registry=executor_registry,
                latency_store=tool_latency_store,
                calendar_timezone=calendar_timezone,
            )

            return PipelineConversationHandler(
                runtime_config, bundle,
                sample_rate=cfg.sample_rate,
                max_history=cfg.max_history,
                transcripts=transcripts,
                tenant_id=ctx.tenant_id,
                call_id=ctx.call_id,
                direction=ctx.direction,
                tool_orchestrator=tool_orchestrator,
                caller_number=ctx.caller_did,
                called_number=ctx.called_did,
                knowledge=knowledge,
                has_booking_tool=has_booking_tool,
                # Admin-UI test only; live calls always use published graph.
                use_workflow_draft=ctx.use_workflow_draft,
                # Admin-UI chat: skip STT/TTS.
                text_only=ctx.text_only,
                calendar_timezone=calendar_timezone,
                latency_store=tool_latency_store,
                filler_selector=filler_selector,
            )

    # grpc.aio.server() defaults to SO_REUSEPORT, which lets a second process
    # silently bind the same port as an already-running instance instead of
    # failing with "address already in use" — a stale/duplicate process from
    # an earlier run then keeps serving requests invisibly, splitting traffic
    # and logs between old and new. Disabling it makes a duplicate start fail
    # loudly instead, which is what should happen.
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
        if stt is not None and _enabled("STT"):
            await stt.load()
        if args.mode != "echo":
            await _prewarm_agents(http_config_repo, provider_registry, config, cfg)
        health_servicer.set(SERVICE_NAME, health_pb2.HealthCheckResponse.SERVING)
        log.info("ConversationService SERVING")

    load_task = asyncio.create_task(_load_and_promote())

    # Heartbeat + dead-node reconciliation loop — entirely separate task
    # from anything call-related (see transcript_builder.py's heartbeat()/
    # reconcile_dead_nodes() docstrings on why this can't add latency to a
    # live call: no shared lock/state, just a periodic tiny DB write).
    # HEARTBEAT_INTERVAL_S=15 / stale-after-3x=45s is a deliberate choice:
    # frequent enough that a dead node's calls don't stay wrongly "live"
    # for long, infrequent enough to be background noise on the DB pool.
    HEARTBEAT_INTERVAL_S = 15
    # A single call going silent (client vanished uncleanly — see
    # reconcile_inactive_calls's own docstring) has nothing to do with node
    # liveness, so it needs its own, much longer threshold: the Gateway's
    # own no-speech-timeout already ends a genuinely silent-but-connected
    # caller within ~19s, so anything still "live" after minutes of no
    # transcript activity is a zombie connection, not a slow talker.
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
        await transcripts.close()
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
