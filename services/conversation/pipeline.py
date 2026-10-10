"""PipelineConversationHandler: STT -> LLM (streamed) -> per-sentence TTS per utterance.
on_cancel() sets an event checked between stages to stop a turn early."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from libs.config_sdk import RuntimeConfig, validate_transfer_timeout_ms
from libs.config_sdk.dial_targets import (
    is_platform_ai_number,
    is_sip_uri_shape,
    is_transfer_destination,
)
from libs.knowledge_sdk import IKnowledgeProvider, RetrievalPolicy

from .directives import (
    Directive,
    DirectiveParser,
    EndCallDirective,
    StreamBuffer,
    TransferDirective,
    TransferRequest,
    strip_markdown_chars,
)
from .fillers import FillerSelector
from .i18n import t
from .guardrails import GuardrailCounter, GuardrailDetector
from . import language as lang_state
from .language import LanguageTracker, reply_language_instruction, script_language, utterance_language
from .metrics import IMetrics, NullMetrics
from .provider_bundle import ProviderBundle
from .tool_latency import ToolLatencyStore
from .providers.interfaces import ChatMessage, SttResult
from libs.config_sdk.languages import normalize_language
from .session import HandlerResponse
from .session_finalizer import FinalizationResult, SessionFinalizer
from .tools.llm_adapter import DeterministicSpokenEvent
from .tools.llm_adapter import LocalToolCompletedEvent
from .tools.llm_adapter import TokenEvent as ToolTokenEvent
from .tools.llm_adapter import ToolCallStartedEvent
from .tools.orchestrator import ToolCallOrchestrator
from .tools.registry import SEARCH_KNOWLEDGE
from .tools.types import ToolResult, ToolStatus
from .transcript_builder import TranscriptBuilder, TurnLatency
from .transfer_engine import (
    DecisionContext,
    TransferDecisionEngine,
    TransferTrigger,
    TriggerType,
)
from .workflow import (
    ContextSummarizer,
    VariableExtractor,
    WorkflowRunner,
    graph_for,
    summary_threshold_for,
)

log = logging.getLogger(__name__)


@dataclass
class _SessionState:
    """All per-session state, so on_session_end() cleans up with one pop."""
    history:                         list[ChatMessage] = field(default_factory=list)
    cancelled:                       asyncio.Event = field(default_factory=asyncio.Event)
    pending_transfer:                "TransferRequest | None" = None
    transfer_requested:              bool = False
    pending_recovery_turns:          list[tuple[str, str, bool]] = field(default_factory=list)
    tool_call_filler_last_phrase:    str | None = None
    tool_call_filler_last_spoken:    float | None = None
    first_turn_filler_spoken:        bool = False
    fabrication_triggered_transfer:  bool = False
    confirmed_booking_slot:          str | None = None
    # Multilingual agents only; None = single-language (fixed language).
    language:                        "LanguageTracker | None" = None
    unchecked_languages_logged:      set = field(default_factory=set)


# Split after ! or ?, and after . unless it follows a title abbreviation or middle initial.
# Devanagari danda/double danda and CJK full stops end a sentence with or without a
# following space (CJK writes none).
_SENTENCE_RE = re.compile(
    r'(?<=[!?])\s+'
    r'|(?<!Mr\.)(?<!Ms\.)(?<!Dr\.)(?<!Sr\.)(?<!Jr\.)(?<!St\.)(?<!Mt\.)(?<!vs\.)'
    r'(?<![A-Z]\.)(?<=[.])\s+'
    r'|(?<=[.!?])$'
    r'|(?<=[।॥。！？])\s*'
)

# Stripped before TTS; triggers EndCall once the turn's audio has streamed.
_END_CALL_MARKER = "[[END_CALL]]"
# The condition is per-agent (agents.end_call_prompt); token mechanics are fixed so
# a custom prompt can't break parsing.
_END_CALL_CONDITION_DEFAULT = (
    "When the conversation is genuinely finished (the caller says "
    "goodbye, has no more questions, or the issue is resolved)"
)


def _build_current_date_context() -> str:
    """Today's date (UTC) so the LLM can resolve relative dates; computed per call."""
    now = datetime.now(timezone.utc)
    return (
        f"\n\nToday's date is {now.strftime('%Y-%m-%d')} ({now.strftime('%A')}), UTC. "
        "Use this to resolve any relative date the caller mentions (e.g. \"tomorrow\", "
        "\"next Monday\", \"in two weeks\") into an exact date yourself before calling any tool."
    )


def _build_caller_number_context(caller_number: str) -> str:
    """Give the LLM the caller ID (digit-spaced for TTS) so it confirms rather than
    asks from scratch, avoiding STT digit errors. "" when there is no ANI."""
    if not caller_number:
        return ""
    spaced = " ".join(caller_number)
    return (
        f"\n\nThe caller's phone number from Caller ID is: {spaced}. Before "
        "booking, state this number back to the caller one digit at a time "
        "(never as a compound number) and ask if it's the best number to "
        "reach them. If they confirm, use it as-is — you don't need to "
        "repeat it in the tool call. If they say it's different or wrong, "
        "ask them to say the correct number one digit at a time, including "
        "country code, then read it back the same way to confirm before "
        "you book anything. "
        "Confirmed live: a caller who doesn't actually answer that question "
        "— changes the subject, asks something else, says something "
        "unrelated — has NOT confirmed the number, even if the "
        "conversation moves on. Never treat silence or a topic change as "
        "confirmation. If you asked and never got a clear yes or a "
        "corrected number, ask again before you book anything — do not "
        "let the conversation drift into scheduling with an unconfirmed "
        "number. "
        "Confirmed live, repeatedly: once you already have a date, a time, "
        "and the caller has confirmed this number, the very next thing you "
        "do must be to actually call the booking tool — not read the "
        "number back again, not ask another question, not say anything "
        "about the appointment yourself. Confirming the same number twice "
        "in a row, or saying anything that sounds like the appointment is "
        "set, without that tool call having actually happened in this "
        "exact turn, is the single most serious mistake you can make on "
        "this call."
    )


def _claim_matches_confirmed_slot(assistant_text: str, confirmed_datetime: str) -> bool:
    """True if the text names the confirmed slot's day and hour as numerals.
    Crude, but anything uncertain returns False so the fabrication check still runs."""
    try:
        dt = datetime.fromisoformat(confirmed_datetime)
    except ValueError:
        return False
    digits_in_text = set(re.findall(r"\d+", assistant_text))
    day_matches = str(dt.day) in digits_in_text
    hour12 = dt.strftime("%I").lstrip("0") or "12"
    hour_matches = hour12 in digits_in_text or str(dt.hour) in digits_in_text
    return day_matches and hour_matches


_BOOKING_CLAIM_RE = re.compile(
    # \bscheduled\b alone never matches "rescheduled".
    r"\b(booked|rebooked|confirmed|scheduled|rescheduled|all set)\b", re.IGNORECASE,
)
_BOOKING_SUBJECT_RE = re.compile(
    r"\b(appointment|demo|booking|meeting|slot)\b", re.IGNORECASE,
)
# Hindi replies mix scripts ("आपका appointment बुक हो गया"), so claim and subject are
# each matched across English, Devanagari and romanised Hindi.
_HI_LETTER = r"[\w\u0900-\u097F]"
_HI_BOOKING_CLAIM_RE = re.compile(
    rf"(?<!{_HI_LETTER})(?:"
    r"(?:बुक|कन्फ़र्म|कन्फर्म|शेड्यूल|पक्का|तय) (?:हो (?:गया|गई|गयी|चुका|चुकी)|कर (?:दिया|दी))"
    r"|(?:book|confirm|schedule|pakka|tay) (?:ho (?:gaya|gayi|chuka|chuki)|kar (?:diya|di))"
    r"|(?:book|confirm|schedule) (?:हो (?:गया|गई|गयी|चुका|चुकी)|कर (?:दिया|दी))"
    rf")(?!{_HI_LETTER})",
    re.IGNORECASE,
)
_HI_BOOKING_SUBJECT_RE = re.compile(
    rf"(?<!{_HI_LETTER})(?:अपॉइंटमेंट|अपॉइन्टमेंट|बुकिंग|मीटिंग|डेमो|स्लॉट)(?!{_HI_LETTER})",
)
_BOOKING_CLAIM_PATTERNS: dict[str, list[tuple[re.Pattern[str], re.Pattern[str]]]] = {
    "en": [(_BOOKING_CLAIM_RE, _BOOKING_SUBJECT_RE)],
    "hi": [
        (_BOOKING_CLAIM_RE, _BOOKING_SUBJECT_RE),
        (_HI_BOOKING_CLAIM_RE, _BOOKING_SUBJECT_RE),
        (_HI_BOOKING_CLAIM_RE, _HI_BOOKING_SUBJECT_RE),
        (_BOOKING_CLAIM_RE, _HI_BOOKING_SUBJECT_RE),
    ],
}


def _claims_booking_without_tool_call(assistant_text: str, language: str | None = None) -> bool:
    """Heuristic backstop for a fabricated booking claim (claim word + subject word).
    A language without patterns is never flagged (no false correction or escalation)."""
    return any(
        claim.search(assistant_text) and subject.search(assistant_text)
        for claim, subject in _BOOKING_CLAIM_PATTERNS.get(language or "en", [])
    )


def _build_end_call_instruction(condition: str | None, scripted: bool = False) -> str:
    """scripted=True: LLM emits only the token; the farewell_message is spoken verbatim."""
    cond = (condition or "").strip().rstrip(".,;") or _END_CALL_CONDITION_DEFAULT
    if scripted:
        return (
            f"\n\n{cond}, reply with ONLY the exact token {_END_CALL_MARKER} "
            "and no other words — the system will speak the farewell message "
            "itself. Never say the token out loud or explain it to the caller."
        )
    return (
        f"\n\n{cond}, end your "
        f"final reply with the exact token {_END_CALL_MARKER} on its own, after "
        "your spoken words. Only use this token when you are truly ending the "
        "call — never say it out loud or explain it to the caller."
    )

# Spoken system strings live in i18n/ per language; these are the English texts.
# Spoken when END_CALL comes with no text: the servicer drops EndCall unless some TTS was sent.
_FALLBACK_GOODBYE = t("fallback_goodbye", "en")

# Bound LLM wait on hangup / pre-transfer flush; outcome is always persisted.
_FINISH_WORKFLOW_TIMEOUT_S = 3.0
_TRANSFER_FLUSH_TIMEOUT_S = 1.5

# Spoken when max_call_duration_s is exceeded; not farewell_message, which implies a natural end.
_MAX_DURATION_GOODBYE = t("max_duration_goodbye", "en")

# Spoken when the LLM stream raises mid-turn, so the caller doesn't hear dead air.
_FALLBACK_LLM_ERROR = t("fallback_llm_error", "en")

_TOOL_CALL_FILLER_MIN_GAP_S = 4.0

# Masks turn-1 LLM latency; generic so it fits any first utterance.
_FIRST_TURN_FILLER = t("first_turn_filler", "en")

# Auto-appended when a transfer is configured, so the destination comes only from config.
# Condition is per-agent (agents.transfer_prompt); token mechanics are fixed.
_TRANSFER_CONDITION_DEFAULT = (
    "If the caller explicitly asks to speak to a human agent or "
    "representative"
)


def _build_transfer_instruction(
    condition: str | None, transfer_type: str, destination: str,
    scripted: bool = False,
) -> str:
    """scripted=True: LLM emits only the token; transfer_announcement is played verbatim."""
    cond = (condition or "").strip().rstrip(".,;") or _TRANSFER_CONDITION_DEFAULT
    token = (
        f'[[TRANSFER type="{transfer_type}" destination="{destination}" '
        'reason="caller_requested_human"]]'
    )
    if scripted:
        return (
            f"\n\n{cond}, reply with ONLY the exact token {token} and no "
            "other words — the system will play the transfer announcement "
            "itself. Never say the token out loud or explain it to the caller."
        )
    return (
        f"\n\n{cond}, briefly acknowledge that you will connect them, then "
        f"end your reply with the exact token {token} on its own, after your "
        "spoken words. Only use this token when that condition is met — "
        "never say it out loud or explain it to the caller."
    )

# Runtime ESL-injection allowlist for transfer destinations, including
# workflow-rendered ones. Must match EslClient.cpp's is_safe_destination;
# never relax it for convenience.
def transfer_destination_problem(destination: str | None) -> str | None:
    """None when the destination looks routable; otherwise a human-readable
    diagnosis for the session-setup error log. Checks the exact value the
    Gateway will receive (no strip), with the Gateway's own allowlist."""
    if destination is None or not destination.strip():
        return "transfer_destination is empty"
    if is_transfer_destination(destination):
        return None
    if is_platform_ai_number(destination):
        return (
            f"transfer_destination {destination!r} is one of this platform's own AI numbers "
            "(788, 5000-5009) — a transfer there loops back and can never connect"
        )
    if destination.lower().startswith(("sip:", "sips:")):
        if is_sip_uri_shape(destination):
            return f"SIP URI {destination!r} points at this host (loopback or maddr)"
        return f"malformed SIP URI {destination!r} (expected sip:user@host)"
    return (
        f"transfer_destination {destination!r} is neither a phone number/extension "
        "(2-15 digits, optional leading +) nor a sip:/sips: URI"
    )

# One-turn LLM input (never stored in history) asking for an apology after a failed
# transfer. JSON is wrapped in prose because small local models ignore bare JSON.
def _build_transfer_failed_system_event(reason: str) -> str:
    event = {"type": "system_event", "event": "transfer_failed", "reason": reason or "unknown"}
    return (
        f"{json.dumps(event)}\n\n"
        "The system event above means the attempted transfer to a human agent "
        "failed. Apologize briefly to the caller for not being able to connect "
        "them to an agent, then continue assisting them with their original "
        "request."
    )

# Used if the LLM produces no apology text.
_TRANSFER_FAILED_FALLBACK = t("transfer_failed_fallback", "en")

# Replaces transfer_announcement for a fabrication-triggered transfer, so the handoff
# doesn't look like a failure to a caller who just heard "Confirmed!".
_BOOKING_FABRICATION_TRANSFER_ANNOUNCEMENT = t("booking_fabrication_transfer_announcement", "en")


class PipelineConversationHandler:
    """IConversationHandler chaining STT -> LLM -> TTS.

    runtime_config is read once at construction; nothing later calls the Config SDK.
    sample_rate must match the gateway's MediaConfig.
    """

    # No out-of-band egress.
    out_responses: "asyncio.Queue[HandlerResponse] | None" = None

    def __init__(
        self,
        runtime_config: RuntimeConfig,
        provider_bundle: ProviderBundle,
        sample_rate:   int = 16_000,
        max_history:   int = 10,
        default_system_prompt: str = "",
        transcripts:   TranscriptBuilder | None = None,
        tenant_id:     str = "",
        call_id:       str = "",
        # No default direction: a construction site that omits it must fail closed
        # in toolexec's caller-id resolution, not be treated as an inbound call.
        direction:     str = "",
        caller_number: str = "",
        called_number: str = "",
        knowledge:     IKnowledgeProvider | None = None,
        metrics:       IMetrics | None = None,
        tool_orchestrator: ToolCallOrchestrator | None = None,
        has_booking_tool: bool = False,
        initial_variables: dict[str, Any] | None = None,
        latency_store: ToolLatencyStore | None = None,
        filler_selector: FillerSelector | None = None,
    ) -> None:
        self._latency_store = latency_store
        self._filler_selector = filler_selector or FillerSelector()
        self._default_system_prompt = (default_system_prompt or "").strip()
        self._stt          = provider_bundle.stt
        self._llm          = provider_bundle.llm
        self._tts          = provider_bundle.tts
        self._tts_by_language: dict[str, Any] = dict(getattr(provider_bundle, "tts_by_language", None) or {})
        # The bundle owns the override-or-base rule; bundles predating it have no overrides.
        self._tts_for = getattr(provider_bundle, "tts_for", None) or (lambda _language: self._tts)
        self._init_languages(runtime_config)
        self._sample_rate  = sample_rate
        self._max_history  = max_history
        self._transcripts  = transcripts
        self._tenant_id    = tenant_id
        self._call_id      = call_id
        self._direction     = direction
        self._caller_number = caller_number
        self._called_number = called_number
        # Legacy path uses ""/0; calls.agent_id is a UUID FK, so map to None.
        self._agent_id             = runtime_config.agent.id or None
        self._agent_config_version = runtime_config.version or None
        # When set, spoken verbatim and the LLM is told to emit only the token.
        self._farewell_message = (
            (runtime_config.conversation.farewell_message or "").strip() or None
        )
        self._transfer_announcement = (
            (runtime_config.conversation.transfer_announcement or "").strip() or None
        )
        self._has_action_tool = has_booking_tool
        # Date / caller-number / directive tokens appended after each node's prompt.
        self._prompt_suffix = (
            _build_current_date_context()
            + (_build_caller_number_context(self._caller_number) if has_booking_tool else "")
            + _build_end_call_instruction(
                runtime_config.conversation.end_call_prompt,
                scripted=self._farewell_message is not None,
            )
        )
        self._goodbye_grace_period_ms = runtime_config.policies.goodbye_grace_ms
        # None = unlimited. Checked per turn rather than by a timer; the handler is built per call.
        self._max_call_duration_s = runtime_config.policies.max_call_duration_s
        self._call_started_at = time.monotonic()
        self._transfer_type_default        = runtime_config.policies.transfer_type
        self._transfer_destination_default = runtime_config.policies.transfer_destination
        self._escalation_threshold         = runtime_config.policies.escalation_threshold
        # Caller-ID resolution inputs (see transfer_engine._resolve_caller_id).
        self._caller_id_policy  = runtime_config.policies.caller_id_policy
        self._platform_did      = runtime_config.policies.platform_did
        self._custom_caller_id  = runtime_config.policies.custom_caller_id
        self._waiting_experience = runtime_config.policies.transfer_waiting_experience
        # Broken transfer config is logged at setup and the trigger is not injected (call stays AI-only).
        self._transfer_timeout_ms = validate_transfer_timeout_ms(
            runtime_config.policies.transfer_timeout_ms,
            context=f"agent={runtime_config.tenant.slug}/{runtime_config.agent.slug}",
        )
        tt = self._transfer_type_default
        if tt and tt not in ("none", "cold", "warm"):
            log.error(
                "Invalid transfer_type=%r for agent %s — treating as 'none'; "
                "fix the agent's Escalation config",
                tt, runtime_config.agent.slug,
            )
        elif tt and tt != "none":
            problem = transfer_destination_problem(self._transfer_destination_default)
            if problem:
                log.error(
                    "Transfer misconfigured for agent %s (transfer_type=%s): %s "
                    "— transfer trigger disabled for this session; fix the "
                    "agent's Escalation config",
                    runtime_config.agent.slug, tt, problem,
                )
            else:
                self._prompt_suffix += _build_transfer_instruction(
                    runtime_config.conversation.transfer_prompt,
                    tt,
                    self._transfer_destination_default,
                    scripted=self._transfer_announcement is not None,
                )
        self._tenant_slug = runtime_config.tenant.slug
        self._agent_slug  = runtime_config.agent.slug
        self._knowledge = knowledge
        self._session_finalizer = SessionFinalizer(transcripts, metrics)
        self._metrics = metrics if metrics is not None else NullMetrics()
        # None means plain self._llm.generate().
        self._tool_orchestrator = tool_orchestrator
        # Stateless; this handler owns the per-session state it reads and produces.
        self._transfer_engine = TransferDecisionEngine(self._metrics)
        self._guardrail_counter = GuardrailCounter()
        # Separate from _guardrail_counter, which resets on every clean caller turn
        # and would erase the fabrication streak.
        self._booking_fabrication_counter = GuardrailCounter()
        self._sessions: dict[str, _SessionState] = {}
        # Conversation workflow — graph_for() falls back to starter, never None.
        now = datetime.now(timezone.utc)
        self._extractor = VariableExtractor(self._llm, self._on_variables_extracted)
        # Threshold from this pipeline's trim cap so summarization and trim
        # stay aligned (see summary_threshold_for).
        self._summarizer = ContextSummarizer(
            self._llm, threshold_msgs=summary_threshold_for(max_history),
        )
        graph = graph_for(runtime_config)
        self._workflow = WorkflowRunner(
            graph,
            base_suffix=self._prompt_suffix,
            default_global=self._default_system_prompt,
            # Call-flow handoff values override the call-context defaults.
            variables={
                "caller_number": caller_number,
                "called_number": called_number,
                "direction":     direction,
                "agent_name":    runtime_config.agent.name,
                "business_name": runtime_config.tenant.name,
                "current_date":  now.strftime("%Y-%m-%d"),
                "current_time":  now.strftime("%H:%M"),
                **(initial_variables or {}),
            },
            extractor=self._extractor,
            summarizer=self._summarizer,
        )
        if (
            any(n.type == "transfer" for n in graph.nodes.values())
            and self._transfer_type_default in ("", "none")
        ):
            log.error(
                "Workflow for agent %s has a transfer node but the agent's "
                "transfer_type is 'none' — those transfers will be rejected; "
                "set warm/cold on the agent's Escalation config",
                runtime_config.agent.slug,
            )
        log.info(
            "Workflow active for agent %s: %d nodes, starting at %r",
            runtime_config.agent.slug, len(graph.nodes), graph.start.name,
        )

    def _init_languages(self, runtime_config: RuntimeConfig) -> None:
        """Effective STT/TTS languages and the multilingual path's state.

        A language kwarg is only passed when it differs from the provider row's own
        (or the agent is multilingual), so single-language agents whose agents.language
        is unset call their providers exactly as before."""
        media = runtime_config.media
        providers = runtime_config.providers
        self._supported_languages: tuple[str, ...] = tuple(getattr(media, "supported_languages", ()) or ())
        self._multilingual = bool(self._supported_languages)
        self._default_language = getattr(media, "default_language", None) or (
            self._supported_languages[0] if self._multilingual else None
        )
        stt_language = getattr(media, "stt_language", None)
        tts_language = getattr(media, "tts_language", None)
        self._stt_kwargs: dict[str, Any] = (
            {"language": stt_language}
            if getattr(self._stt, "accepts_language", False) is True
            and (self._multilingual or stt_language != providers.stt.language)
            else {}
        )
        # Auto-detecting STT (Whisper): choose only among the agent's languages.
        if (
            self._multilingual and stt_language is None
            and getattr(self._stt, "accepts_language_candidates", False) is True
        ):
            self._stt_kwargs["languages"] = self._supported_languages
        # Single-language: a fixed TTS language when agents.language overrides the row's.
        self._tts_fixed_language: str | None = (
            tts_language if (not self._multilingual and tts_language != providers.tts.language) else None
        )
        self._agent_language = runtime_config.agent.language
        self._greeting_by_language = getattr(runtime_config.conversation, "greeting_by_language", None)
        # Spoken system strings: the session language, else an explicit agents.language.
        # Never a provider row's language, so agents without one keep their English strings.
        self._strings_language = normalize_language(runtime_config.agent.language) or "en"
        if self._multilingual:
            log.info(
                "Multilingual agent %s: default=%s supported=%s stt_language=%s voice_overrides=%s",
                runtime_config.agent.slug, self._default_language, ",".join(self._supported_languages),
                stt_language, ",".join(sorted(self._tts_by_language)) or "none",
            )
            self._prewarm_tts_languages()

    def _prewarm_tts_languages(self) -> None:
        """Build per-language TTS state (Kokoro pipelines) off the turn's critical path."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for tts in {id(p): p for p in (self._tts, *self._tts_by_language.values())}.values():
            prewarm = getattr(tts, "prewarm", None)
            if prewarm is None:
                continue
            task = loop.create_task(prewarm(list(self._supported_languages)))
            task.add_done_callback(
                lambda done: done.cancelled() or done.exception() is None
                or log.error("TTS language prewarm failed", exc_info=done.exception())
            )

    def _greeting_text(self) -> str | None:
        """The per-language greeting for the starting language when the agent has one,
        rendered like the workflow's; otherwise the workflow start node's greeting."""
        language = self._default_language or normalize_language(self._agent_language)
        localized = (self._greeting_by_language or {}).get(language) if language else None
        if localized and localized.strip():
            return self._workflow.render(localized).strip() or None
        return self._workflow.greeting()

    def _log_unchecked_language(self, session_id: str, language: str | None) -> None:
        """Guardrails and the booking-claim check have no lexicon for this language:
        they're skipped (fail safe). Logged once per session, per language."""
        if GuardrailDetector.supports(language):
            return
        state = self._session(session_id)
        if language in state.unchecked_languages_logged:
            return
        state.unchecked_languages_logged.add(language)
        log.info(
            "guardrail and booking-claim checks skipped: no lexicon for language=%s session=%s",
            language, session_id,
        )

    def _session_language(self, session_id: str) -> str | None:
        """The language this session speaks now; None for single-language agents."""
        tracker = self._session(session_id).language
        return tracker.current if tracker is not None else None

    def _spoken_language(self, session_id: str) -> str:
        """Language for system strings (fillers, goodbyes, fallbacks)."""
        return self._session_language(session_id) or self._strings_language

    def _on_variables_extracted(self, values: dict) -> None:
        """Merge extraction into the runner for later prompts and session-end persistence."""
        self._workflow.update_variables(values)

    # ── IConversationHandler ───────────────────────────────────────────────────

    async def greeting(self, session_id: str) -> list[bytes]:
        if self._transcripts is not None:
            # One field, two sinks with opposite fail-closed directions. The
            # empty direction that makes toolexec refuse to guess a remote
            # party is not a value calls.direction accepts — its CHECK is
            # ('inbound','outbound','test'), so writing "" here loses the call
            # row and its whole transcript. The transcript's safe reading of
            # an unset direction is the common case, inbound.
            self._transcripts.begin_call(
                session_id, self._tenant_id, self._call_id,
                self._direction or "inbound", self._caller_number, self._called_number,
                self._agent_id, self._agent_config_version,
            )
        # Speaking the instant the line opens gets the first syllable
        # clipped on some outbound carriers — the start node can hold off.
        if self._workflow.delayed_start_ms > 0:
            await asyncio.sleep(self._workflow.delayed_start_ms / 1000)
        text = self._greeting_text() or ""
        if not text:
            return []
        return [chunk async for chunk in self._synthesize_sentence_stream(text, session_id)]

    async def on_audio(self, session_id: str, payload: bytes) -> HandlerResponse:
        # Lets streaming STT transcribe continuously; no-op for batch providers.
        try:
            await self._stt.feed_stream(session_id, payload, self._sample_rate, **self._stt_kwargs)
        except Exception:
            log.exception("STT feed_stream failed session=%s", session_id)
        return HandlerResponse()

    async def on_speech_ended(
        self,
        session_id:  str,
        audio:       bytes,
        duration_ms: int,
        energy_db:   float,
    ) -> AsyncGenerator[HandlerResponse, None]:
        # Fresh per-turn event, replaced before any await: a cancel targets the prior response only.
        cancel_event = asyncio.Event()
        self._session(session_id).cancelled = cancel_event

        # Free the shared call LLM before this turn's generate — a prior
        # turn's extract/summarize would otherwise queue behind Ollama.
        self._interrupt_workflow_background_llm()

        # Sub-1s blips make Whisper hallucinate text or a wrong language. Multilingual
        # agents let 0.45-1.0 s through to STT so "haan"/"ji"/"sí" survive, but keep them
        # only in the session's language at high confidence (checked after STT).
        min_bytes = int(1.0 * self._sample_rate) * 2  # 1000 ms of S16LE mono
        floor_bytes = (
            int(lang_state.SHORT_MIN_S * self._sample_rate) * 2 if self._multilingual else min_bytes
        )
        if len(audio) < floor_bytes:
            log.debug(
                "Skipping short utterance (%d bytes < %d) session=%s",
                len(audio), floor_bytes, session_id,
            )
            return
        is_short = len(audio) < min_bytes

        # Voice-to-voice latency is measured from the end of the caller's speech.
        turn_start = time.monotonic()

        # ── 1. STT ─────────────────────────────────────────────────────────────
        stt_t0 = time.monotonic()
        stt_kwargs = self._stt_kwargs
        tracker = self._session(session_id).language
        if is_short and tracker is not None and "languages" in stt_kwargs:
            # Whisper detects first and skips the decode for a short blip the gate would drop.
            # Hindi uses its own (lower) bar: Devanagari text makes it Hindi for the gate
            # (utterance_language), so a short "हाँ" isn't lost to Whisper's English prior.
            min_confidence = (
                lang_state.SHORT_MIN_CONFIDENCE_HI if tracker.current == "hi"
                else lang_state.SHORT_MIN_CONFIDENCE
            )
            stt_kwargs = {**stt_kwargs, "require_language": (tracker.current, min_confidence)}
        try:
            stt_result: SttResult = await self._stt.finalize_stream(
                session_id, audio, self._sample_rate, **stt_kwargs,
            )
        except Exception:
            log.exception("STT failed session=%s", session_id)
            return
        stt_ms = (time.monotonic() - stt_t0) * 1000

        if not stt_result.text or cancel_event.is_set():
            log.debug("STT empty or cancelled session=%s", session_id)
            return

        if tracker is not None:
            heard = utterance_language(stt_result, self._supported_languages)
            if is_short:
                # The language check measures language, not speech: also require the engine's
                # own transcript confidence (Deepgram; Whisper reports 1.0 and gates on no_speech_prob).
                if stt_result.confidence < lang_state.SHORT_MIN_SPEECH_CONFIDENCE or not tracker.accept_short(heard):
                    log.debug(
                        "Skipping short utterance %r lang=%s conf=%s stt_conf=%.2f (session language %s) session=%s",
                        stt_result.text, heard.language, heard.confidence, stt_result.confidence,
                        tracker.current, session_id,
                    )
                    return
            else:
                tracker.observe(heard, session_id)

        if tracker is not None:
            log.info(
                "STT result=%r language=%s conf=%s session_language=%s session=%s",
                stt_result.text, heard.language,
                f"{heard.confidence:.2f}" if heard.confidence is not None else None,
                tracker.current, session_id,
            )
        else:
            log.info("STT result=%r session=%s", stt_result.text, session_id)
        yield HandlerResponse(stt_text=stt_result.text, stt_confidence=stt_result.confidence)

        # A breach only stores a pending transfer; the agent still answers this turn first.
        check_language = self._session_language(session_id) or (
            self._strings_language if self._agent_language else None
        )
        self._log_unchecked_language(session_id, check_language)
        violation = GuardrailDetector.check(stt_result.text, check_language)
        if violation is not None:
            log.info(
                "Guardrail violation category=%s matched=%r session=%s",
                violation.category, violation.matched, session_id,
            )
            self.record_guardrail_violation(session_id)
        else:
            # Consecutive count: a clean turn resets the streak.
            self._guardrail_counter.reset(session_id)

        # ── Max call duration ────────────────────────────────────────────────
        # After STT (utterance still recorded), before the LLM (no wasted generation).
        if (
            self._max_call_duration_s is not None
            and (time.monotonic() - self._call_started_at) >= self._max_call_duration_s
        ):
            log.info(
                "Max call duration (%ds) reached — ending call session=%s",
                self._max_call_duration_s, session_id,
            )
            got_audio = False
            spoken = self._spoken_language(session_id)
            max_duration_goodbye = t("max_duration_goodbye", spoken)
            async for chunk in self._synthesize_sentence_stream(max_duration_goodbye, session_id):
                got_audio = True
                yield HandlerResponse(tts_payloads=[chunk])
            if not got_audio:
                # Some TTS must be sent or EndCall is dropped.
                async for chunk in self._synthesize_sentence_stream(t("fallback_goodbye", spoken), session_id):
                    yield HandlerResponse(tts_payloads=[chunk])
            if self._transcripts is not None:
                self._transcripts.record_turn(
                    session_id, stt_result.text, stt_result.confidence,
                    max_duration_goodbye, False,
                    latency=TurnLatency(
                        stt_ms=stt_ms, stt_engine=type(self._stt).__name__,
                        tts_engine=type(self._tts).__name__,
                    ),
                )
            yield HandlerResponse(
                end_call=True,
                end_call_grace_period_ms=self._goodbye_grace_period_ms,
            )
            return

        # ── 2. LLM ─────────────────────────────────────────────────────────────
        history = self._get_history(session_id)
        is_first_turn = not history
        # history[0] is the active node's prompt — refreshed every turn so a
        # mid-call transition lands before the next generation.
        self._refresh_node_prompt(history, session_id)
        history.append(ChatMessage(role="user", content=stt_result.text))

        if is_first_turn and not self._session(session_id).first_turn_filler_spoken:
            self._session(session_id).first_turn_filler_spoken = True
            first_turn_filler = t("first_turn_filler", self._spoken_language(session_id))
            async for chunk in self._synthesize_sentence_stream(first_turn_filler, session_id):
                yield HandlerResponse(tts_payloads=[chunk])

        # Knowledge retrieval is the `search_knowledge` local tool, called only when needed.
        messages_for_llm = history

        directives: list[Directive] = []
        full_response: list[str] = []
        tool_calls_made: list[str] = []
        end_call = False
        any_audio = False
        llm_t0 = time.monotonic()
        first_token_at: float | None = None
        first_audio_at: float | None = None
        try:
            async for chunk, tts_audio, marker_seen in self._llm_to_tts(
                messages_for_llm, cancel_event, session_id, directives, tool_calls_made,
                store=history,
            ):
                now = time.monotonic()
                if first_token_at is None:
                    first_token_at = now
                full_response.append(chunk)
                end_call = marker_seen
                if tts_audio:
                    if first_audio_at is None:
                        first_audio_at = now
                    any_audio = True
                    yield HandlerResponse(tts_payloads=[tts_audio])
                if cancel_event.is_set():
                    break
        except Exception:
            log.exception("LLM/TTS pipeline failed session=%s", session_id)

        # Transitions queue extract/summarize; start after the live generate.
        # on_speech_ended/on_cancel interrupt them before the next turn.
        self._start_workflow_background_llm()

        # Sequential buckets; voice_to_voice is measured directly since a cancelled turn can leave either unset.
        llm_ms = (first_token_at - llm_t0) * 1000 if first_token_at else None
        tts_ms = (first_audio_at - first_token_at) * 1000 if (first_audio_at and first_token_at) else None
        voice_to_voice_ms = (first_audio_at - turn_start) * 1000 if first_audio_at else None

        # full_response holds raw tokens, so strip directives again.
        assistant_text = DirectiveParser.parse("".join(full_response)).clean_text.strip()

        # Models sometimes claim a booking without calling a tool. Already spoken, so
        # correct the next turn and escalate repeats; a recap of the real slot is allowed.
        _confirmed_slot = self._session(session_id).confirmed_booking_slot
        recap_of_real_booking = (
            _confirmed_slot is not None
            and _claim_matches_confirmed_slot(assistant_text, _confirmed_slot)
        )
        # Booking API names are per tenant, so check for no tool call at all.
        fabricated_booking_claim = (
            self._has_action_tool
            and not recap_of_real_booking
            and not tool_calls_made
            and _claims_booking_without_tool_call(assistant_text, check_language)
        )

        if full_response and not cancel_event.is_set():
            yield HandlerResponse(response_text=assistant_text)
            # Cancelled turns are discarded so history only holds complete pairs.
            history.append(ChatMessage(role="assistant", content=assistant_text))
            if fabricated_booking_claim:
                log.warning(
                    "Possible fabricated booking claim (no tool call this turn) "
                    "session=%s text=%r", session_id, assistant_text,
                )
                history.append(ChatMessage(
                    role="system",
                    content=(
                        "Correction: nothing was actually booked, confirmed, or scheduled just "
                        "now — you did not call any tool. If the caller still wants an "
                        "appointment, call the booking API for real before saying anything is "
                        "booked or confirmed."
                    ),
                ))
                self.record_booking_fabrication(session_id)
            self._trim_history(session_id)
        else:
            # Barge-in, cancel, or LLM failure: remove the unpaired user message
            # so history stays consistent for the next turn's LLM context.
            if history and history[-1].role == "user":
                history.pop()

        if self._transcripts is not None:
            self._transcripts.record_turn(
                session_id, stt_result.text, stt_result.confidence,
                assistant_text, cancel_event.is_set(),
                latency=TurnLatency(
                    stt_ms=stt_ms, llm_ms=llm_ms, tts_ms=tts_ms, voice_to_voice_ms=voice_to_voice_ms,
                    stt_engine=type(self._stt).__name__, llm_engine=type(self._llm).__name__,
                    tts_engine=type(self._tts).__name__,
                ),
            )

        # End node hangup; [[END_CALL]] off-graph marks ended_off_graph for disposition.
        ended_on_end_node = self._workflow.pending_end
        if ended_on_end_node:
            end_call = True
        elif end_call:
            self._workflow.ended_off_graph = True

        # At most one transfer per turn: directive, then workflow, then pending escalation.
        # Computed before end_call, since yielding end_call first would tear the session down.
        transfer_request: TransferRequest | None = None
        if not cancel_event.is_set():
            transfer_directive = next(
                (d for d in directives if isinstance(d, TransferDirective)), None,
            )
            if transfer_directive is not None:
                decision = self._transfer_engine.evaluate(
                    self._decision_context(session_id),
                    TransferTrigger(type=TriggerType.LLM_DIRECTIVE, directive=transfer_directive),
                )
                if decision.accepted:
                    transfer_request = decision.request
                    self._session(session_id).transfer_requested = True
            if transfer_request is None:
                transfer_request = await self._workflow_transfer(session_id)
            # Also covers a directive rejected as already_transferring because of this pending request.
            if transfer_request is None:
                state = self._session(session_id)
                transfer_request = state.pending_transfer
                state.pending_transfer = None

        # Not on barge-in (stale decision), a pending transfer, or a fabricated claim
        # (keep the line open for the correction turn).
        if end_call and not cancel_event.is_set() and transfer_request is None and not fabricated_booking_claim:
            got_farewell_audio = False
            if self._farewell_message:
                # Plays even if the model spoke anyway.
                async for chunk in self._synthesize_sentence_stream(self._farewell_message, session_id):
                    got_farewell_audio = True
                    yield HandlerResponse(tts_payloads=[chunk])
            if not any_audio and not got_farewell_audio:
                # The servicer needs some audio to key EndCall off.
                goodbye = t("fallback_goodbye", self._spoken_language(session_id))
                async for chunk in self._synthesize_sentence_stream(goodbye, session_id):
                    yield HandlerResponse(tts_payloads=[chunk])
            yield HandlerResponse(
                end_call=True,
                end_call_grace_period_ms=self._goodbye_grace_period_ms,
            )

        if transfer_request is not None:
            announcement = (
                t("booking_fabrication_transfer_announcement", self._spoken_language(session_id))
                if self._session(session_id).fabrication_triggered_transfer
                else self._transfer_announcement
            )
            self._session(session_id).fabrication_triggered_transfer = False
            if announcement:
                # The servicer holds the TransferRequest until this has played.
                async for chunk in self._synthesize_sentence_stream(announcement, session_id):
                    yield HandlerResponse(tts_payloads=[chunk])
            yield HandlerResponse(transfer_request=transfer_request)

    async def on_cancel(self, session_id: str) -> None:
        self._cancel_event(session_id).set()
        # Barge-in before pending_speech is consumed must not replay it next turn.
        self._workflow.pending_speech = None
        self._interrupt_workflow_background_llm()

    async def on_dtmf(self, session_id: str, digit: str) -> None:
        pass

    async def on_session_end(self, session_id: str, reason: str,
                             final_state: str | None = None) -> None:
        # Outcome must spawn before end_call() drops the write chain.
        await self._finish_workflow(session_id)
        # Transfer-failure recovery turns are persisted only at session end.
        state = self._sessions.get(session_id)
        pending_recovery_turns = state.pending_recovery_turns if state is not None else []
        log.info(
            "workflow outcome session=%s disposition=%s visited=%s reason=%s",
            session_id, self._workflow.disposition, self._workflow.visited, reason,
        )
        if self._transcripts is not None:
            for caller_text, ai_response, interrupted in pending_recovery_turns:
                self._transcripts.record_turn(session_id, caller_text, 1.0, ai_response, interrupted)
            if state is not None and state.language is not None and state.language.detected:
                self._transcripts.record_detected_languages(session_id, state.language.detected)
            self._transcripts.end_call(session_id, reason, final_state=final_state)
        self._guardrail_counter.reset(session_id)
        self._booking_fabrication_counter.reset(session_id)
        self._sessions.pop(session_id, None)
        try:
            await self._stt.cancel_stream(session_id)
        except Exception:
            log.exception("STT cancel_stream failed session=%s", session_id)

    def _decision_context(
        self, session_id: str, destination_override: str | None = None,
    ) -> DecisionContext:
        """destination_override: workflow transfer node destination for this decision."""
        return DecisionContext(
            session_id=session_id, tenant_id=self._tenant_id, call_id=self._call_id,
            transfer_type=self._transfer_type_default,
            transfer_destination=destination_override or self._transfer_destination_default,
            escalation_threshold=self._escalation_threshold,
            already_requested=self._session(session_id).transfer_requested,
            caller_id_policy=self._caller_id_policy,
            platform_did=self._platform_did,
            custom_caller_id=self._custom_caller_id,
            caller_number=self._caller_number,
            waiting_experience=self._waiting_experience,
        )

    def record_guardrail_violation(self, session_id: str) -> TransferRequest | None:
        """Count a violation and, if the engine accepts, store a pending transfer for
        on_speech_ended(). Safe to call unconditionally (threshold None never escalates)."""
        return self._evaluate_escalation(session_id, self._guardrail_counter.increment(session_id))

    def record_booking_fabrication(self, session_id: str) -> TransferRequest | None:
        """Like record_guardrail_violation(), on the separate fabrication counter."""
        request = self._evaluate_escalation(session_id, self._booking_fabrication_counter.increment(session_id))
        if request is not None:
            self._session(session_id).fabrication_triggered_transfer = True
        return request

    def _evaluate_escalation(self, session_id: str, count: int) -> TransferRequest | None:
        decision = self._transfer_engine.evaluate(
            self._decision_context(session_id),
            TransferTrigger(type=TriggerType.ESCALATION, violation_count=count),
        )
        if not decision.accepted:
            return None
        state = self._session(session_id)
        state.transfer_requested = True
        state.pending_transfer = decision.request
        return decision.request

    async def on_transfer_failed(
        self, session_id: str, destination: str, reason: str,
    ) -> AsyncGenerator[HandlerResponse, None]:
        """Transfer failed: speak an LLM apology and continue the call. The system-event
        prompt is ephemeral and never stored in history."""
        cancel_event = asyncio.Event()
        self._session(session_id).cancelled = cancel_event

        # Allow a fresh transfer request later.
        self._session(session_id).transfer_requested = False
        # The call isn't ending after all.
        self._session_finalizer.discard_pending_summary(session_id)

        history = self._get_history(session_id)
        self._refresh_node_prompt(history, session_id)

        notice = ChatMessage(
            role="user",
            content=_build_transfer_failed_system_event(reason),
        )
        messages_for_llm = history + [notice]

        directives: list[Directive] = []
        full_response: list[str] = []
        any_audio = False
        try:
            async for chunk, tts_audio, _end_call in self._llm_to_tts(
                messages_for_llm, cancel_event, session_id, directives, store=history,
            ):
                full_response.append(chunk)
                if tts_audio:
                    any_audio = True
                    yield HandlerResponse(tts_payloads=[tts_audio])
                if cancel_event.is_set():
                    break
        except Exception:
            log.exception("Transfer-failure recovery LLM/TTS pipeline failed session=%s", session_id)

        assistant_text = DirectiveParser.parse("".join(full_response)).clean_text.strip()

        if not assistant_text and not any_audio and not cancel_event.is_set():
            assistant_text = t("transfer_failed_fallback", self._spoken_language(session_id))
            async for chunk in self._synthesize_sentence_stream(assistant_text, session_id):
                yield HandlerResponse(tts_payloads=[chunk])

        # Stored without a matching user turn; the notice is never persisted.
        if assistant_text and not cancel_event.is_set():
            history.append(ChatMessage(role="assistant", content=assistant_text))
            self._trim_history(session_id)

        # Transcript persistence is deferred to on_session_end().
        self._session(session_id).pending_recovery_turns.append((
            f"[transfer_failed: {reason}]", assistant_text, cancel_event.is_set(),
        ))

    def on_transfer_cancelled(self, session_id: str) -> None:
        """Pending transfer dropped before dispatch (barge-in); allow a fresh request."""
        self._session(session_id).transfer_requested = False
        self._session_finalizer.discard_pending_summary(session_id)

    def start_finalization(self, session_id: str) -> None:
        """Called on TransferInitiated to start the summary early."""
        self._session_finalizer.start_summary_early(
            session_id, self._get_history(session_id), self._llm,
        )

    def record_live_stage(self, session_id: str, stage: str) -> None:
        """No-op when transcript persistence is disabled."""
        if self._transcripts is not None:
            self._transcripts.record_live_stage(session_id, stage)

    async def finalize_session(
        self, session_id: str, reason: str = "transfer_completed",
    ) -> FinalizationResult:
        """Run post-transfer finalization with this handler's private per-session state."""
        return await self._session_finalizer.finalize(
            session_id,
            self._get_history(session_id),
            self._llm,
            self._cancel_event(session_id),
            reason,
        )

    # ── Workflow helpers ─────────────────────────────────────────────────

    def _refresh_node_prompt(self, history: list[ChatMessage], session_id: str | None = None) -> None:
        """Refresh history[0] with the active node's rendered prompt, plus the reply-language
        line on the multilingual path (per turn, so it follows a language switch)."""
        content = self._workflow.system_prompt()
        language = self._session_language(session_id) if session_id is not None else None
        if language:
            content += reply_language_instruction(language)
        prompt = ChatMessage(role="system", content=content)
        if history and history[0].role == "system":
            history[0] = prompt
        else:
            history.insert(0, prompt)

    async def _workflow_transfer(self, session_id: str) -> TransferRequest | None:
        node = self._workflow.pending_transfer
        if node is None:
            return None
        self._workflow.pending_transfer = None
        # Do not let a queued summary race the transfer path on the call LLM.
        self._summarizer.cancel()
        self._extractor.start_deferred()
        pending = self._extractor.pending_tasks()
        if pending:
            _done, still = await asyncio.wait(
                pending, timeout=_TRANSFER_FLUSH_TIMEOUT_S,
            )
            if still:
                log.warning(
                    "workflow: extraction flush timed out before transfer session=%s — "
                    "routing with variables collected so far; stragglers left running",
                    session_id,
                )
        destination = self._workflow.render(node.transfer_destination or "") or None
        problem = transfer_destination_problem(destination)
        if problem is not None:
            log.warning(
                "Workflow transfer node %r rejected: %s session=%s",
                node.name, problem, session_id,
            )
            self._workflow.abandon_transfer()
            return None
        decision = self._transfer_engine.evaluate(
            self._decision_context(session_id, destination_override=destination),
            TransferTrigger(
                type=TriggerType.WORKFLOW, workflow_reason=f"workflow_node:{node.name}",
            ),
        )
        if not decision.accepted:
            log.warning(
                "Workflow transfer node %r rejected: %s session=%s",
                node.name, decision.rejection_reason, session_id,
            )
            self._workflow.abandon_transfer()
            return None
        self._session(session_id).transfer_requested = True
        return decision.request

    def _interrupt_workflow_background_llm(self) -> None:
        self._extractor.interrupt_for_live_turn()
        self._summarizer.interrupt_for_live_turn()

    def _start_workflow_background_llm(self) -> None:
        self._extractor.start_deferred()
        self._summarizer.start_deferred()

    async def _finish_workflow(self, session_id: str) -> None:
        """Best-effort final extract, then always persist path/disposition/vars."""
        self._summarizer.cancel()
        try:
            await asyncio.wait_for(
                self._finalize_extraction(session_id),
                timeout=_FINISH_WORKFLOW_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning(
                "Workflow finalization timed out after %.1fs session=%s — "
                "persisting path/disposition with variables collected so far",
                _FINISH_WORKFLOW_TIMEOUT_S, session_id,
            )
        except Exception:
            log.exception("Workflow final extraction failed session=%s", session_id)
        finally:
            # Do not leave extracts calling the shared LLM after the call ends.
            self._extractor.cancel_pending()
            self._summarizer.cancel()
        if self._transcripts is not None:
            self._transcripts.record_workflow_outcome(
                session_id,
                nodes_visited=self._workflow.visited,
                disposition=self._workflow.disposition,
                extracted_variables=self._workflow.extracted_variables(),
            )

    async def _finalize_extraction(self, session_id: str) -> None:
        # Flush leave-node extracts first so extract_final cannot starve them.
        self._extractor.start_deferred()
        await self._extractor.flush()
        await self._extractor.extract_final(
            self._workflow.node, self._get_history(session_id),
        )

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _token_stream(
        self,
        history: list[ChatMessage],
        session_id: str,
        cancel_event: asyncio.Event,
        tool_calls_made: list[str],
        store: list[ChatMessage] | None = None,
    ) -> AsyncGenerator[str | ToolCallStartedEvent | LocalToolCompletedEvent, None]:
        """LLM tokens + ToolCallStartedEvent filler + LocalToolCompletedEvent for bridging speech."""
        if self._tool_orchestrator is None:
            async for token in self._llm.generate(history):
                yield token
            return

        # Callables so a mid-turn transition re-reads the new node's tools.
        local_tools = lambda: self._local_tools(history, store, session_id)  # noqa: E731
        only_tools = lambda: self._workflow.allowed_tool_names()            # noqa: E731

        async for event in self._tool_orchestrator.run_turn(
            self._agent_id or "", self._tenant_id, self._call_id, session_id, history,
            caller_number=self._caller_number, called_number=self._called_number,
            call_direction=self._direction, cancel_event=cancel_event,
            local_tools=local_tools, only_tools=only_tools,
        ):
            if isinstance(event, ToolCallStartedEvent):
                tool_calls_made.append(event.tool_name)
                yield event
                continue
            if isinstance(event, LocalToolCompletedEvent):
                yield event
                continue
            if isinstance(event, DeterministicSpokenEvent):
                # Speak verbatim; persist confirmed slot for later fabrication checks.
                if event.confirmed_datetime:
                    self._session(session_id).confirmed_booking_slot = event.confirmed_datetime
                yield event.text
                continue
            assert isinstance(event, ToolTokenEvent)
            yield event.text

    async def _llm_to_tts(
        self,
        history:      list[ChatMessage],
        cancel_event: asyncio.Event,
        session_id:   str,
        directives:   list[Directive],
        tool_calls_made: list[str] | None = None,
        store:        list[ChatMessage] | None = None,
    ) -> AsyncGenerator[tuple[str, bytes, bool], None]:
        """Stream LLM tokens and synthesize per sentence, yielding (raw_token, tts_bytes, end_call).
        Directives are stripped before TTS and appended to `directives` in place."""
        stream_buf  = StreamBuffer()
        text_buffer = ""  # directive-free text awaiting a sentence boundary
        if tool_calls_made is None:
            tool_calls_made = []
        # end_call latched so transition speech / filler cannot clear [[END_CALL]].
        end_call = False
        try:
            async for item in self._token_stream(
                history, session_id, cancel_event, tool_calls_made, store,
            ):
                if cancel_event.is_set():
                    self._workflow.pending_speech = None
                    break
                if isinstance(item, LocalToolCompletedEvent):
                    speech = self._workflow.pending_speech
                    if speech:
                        self._workflow.pending_speech = None
                        # Yield text once so full_response/history/transcript record it.
                        first = True
                        async for chunk in self._synthesize_sentence_stream(speech, session_id):
                            yield speech if first else "", chunk, end_call
                            first = False
                    continue
                if isinstance(item, ToolCallStartedEvent):
                    now = time.monotonic()
                    state = self._session(session_id)
                    last_spoken = state.tool_call_filler_last_spoken
                    if last_spoken is None or (now - last_spoken) >= _TOOL_CALL_FILLER_MIN_GAP_S:
                        state.tool_call_filler_last_spoken = now
                        average_ms = (
                            self._latency_store.average_ms(self._tenant_id, self._agent_id or "", item.tool_name)
                            if self._latency_store is not None else None
                        )
                        phrase = self._filler_selector.select_tool_filler(
                            item.tool_name, state.tool_call_filler_last_phrase, average_ms,
                            **({"language": self._spoken_language(session_id)}
                               if self._spoken_language(session_id) != "en" else {}),
                        )
                        state.tool_call_filler_last_phrase = phrase
                        any_filler_chunk = False
                        async for chunk in self._synthesize_sentence_stream(phrase, session_id):
                            if not any_filler_chunk:
                                any_filler_chunk = True
                                log.info(
                                    "Tool-call filler spoken tool=%s phrase=%r session=%s",
                                    item.tool_name, phrase, session_id,
                                )
                            yield "", chunk, end_call
                    continue
                token = item
                result = DirectiveParser.parse(stream_buf.feed(token))
                directives.extend(result.directives)
                text_buffer += result.clean_text
                end_call = any(isinstance(d, EndCallDirective) for d in directives)
                if result.directives:
                    for d in result.directives:
                        if isinstance(d, EndCallDirective):
                            log.info("End-call marker detected session=%s", session_id)
                yield token, b"", end_call

                # Split on sentence boundaries and synthesise each complete sentence.
                while True:
                    parts = _SENTENCE_RE.split(text_buffer, maxsplit=1)
                    if len(parts) < 2:
                        break
                    sentence, text_buffer = parts[0].strip(), parts[1]
                    if sentence:
                        async for chunk in self._synthesize_sentence_stream(sentence, session_id):
                            yield "", chunk, end_call
        except Exception:
            log.exception("LLM streaming failed session=%s", session_id)
            if not cancel_event.is_set():
                fallback_text = t("fallback_llm_error", self._spoken_language(session_id))
                async for chunk in self._synthesize_sentence_stream(fallback_text, session_id):
                    yield fallback_text, chunk, False
                    fallback_text = ""  # yielded once — see full_response.append(chunk) at the call site

        # An unclosed "[[" was prose, not a directive.
        text_buffer += stream_buf.flush()

        end_call = any(isinstance(d, EndCallDirective) for d in directives)
        if text_buffer.strip() and not cancel_event.is_set():
            async for chunk in self._synthesize_sentence_stream(text_buffer.strip(), session_id):
                yield "", chunk, end_call

    def _local_tools(
        self, history: list[ChatMessage], store: list[ChatMessage] | None, session_id: str,
    ) -> dict[str, tuple[Any, Any]]:
        """The node's transition tools, plus search_knowledge when enabled (local, so it
        doesn't consume the remote tool-iteration budget)."""
        tools = dict(self._workflow.local_tools(history, store))
        if self._knowledge is not None and self._workflow.knowledge_enabled():
            tools[SEARCH_KNOWLEDGE.name] = (SEARCH_KNOWLEDGE, self._make_knowledge_handler(session_id))
        return tools

    def _make_knowledge_handler(self, session_id: str):
        async def handler(arguments: dict[str, Any]) -> ToolResult:
            query = str(arguments.get("query") or "").strip()
            if not query:
                return ToolResult(status=ToolStatus.INVALID_ARGUMENT, error="missing_query")
            # Not _retrieve_context(): the model must distinguish "lookup failed" from "nothing found".
            try:
                context = await self._knowledge.retrieve(
                    self._tenant_slug, self._agent_slug, query, RetrievalPolicy(),
                )
            except Exception:
                log.exception("search_knowledge retrieval failed session=%s", session_id)
                return ToolResult(status=ToolStatus.FAILED, error="knowledge_unavailable")
            if context is None or not context.chunks:
                return ToolResult(status=ToolStatus.SUCCESS, payload={"found": False, "passages": []})
            payload: dict[str, Any] = {
                "found": True,
                "passages": [chunk.content for chunk in context.chunks],
            }
            if context.include_citations and context.sources:
                payload["sources"] = list(context.sources)
            return ToolResult(status=ToolStatus.SUCCESS, payload=payload)

        return handler

    async def _retrieve_context(self, query: str, session_id: str):
        # A retrieval failure must never fail the turn.
        try:
            return await self._knowledge.retrieve(
                self._tenant_slug, self._agent_slug, query, RetrievalPolicy(),
            )
        except Exception:
            log.exception("Knowledge retrieval failed session=%s", session_id)
            return None

    @staticmethod
    def _format_context(context) -> str:
        text = "Relevant information that may help answer the caller's question:\n"
        text += "\n\n".join(chunk.content for chunk in context.chunks)
        if context.include_citations and context.sources:
            text += "\n\nSources: " + ", ".join(context.sources)
        return text

    async def _synthesize_sentence_stream(self, text: str, session_id: str) -> AsyncGenerator[bytes, None]:
        # Every text source reaches TTS here, so strip markdown once for all of them.
        text = strip_markdown_chars(text)
        language = self._session_language(session_id) or self._tts_fixed_language
        if self._multilingual:
            # The LLM sometimes answers in another supported language than the session's;
            # voice the sentence in the language its script says it's in.
            written_in = script_language(text, self._supported_languages)
            if written_in and written_in != language:
                log.info("TTS sentence written in %s during a %s session — voicing it in %s session=%s",
                         written_in, language, written_in, session_id)
                language = written_in
        tts = self._tts_for(language)
        kwargs = {"language": language} if language and getattr(tts, "accepts_language", False) is True else {}
        try:
            async for chunk in tts.synthesize_stream(text, self._sample_rate, **kwargs):
                yield chunk
        except Exception:
            log.exception("TTS streaming failed text=%r session=%s", text, session_id)

    def _session(self, session_id: str) -> _SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            state = _SessionState()
            if self._multilingual and self._default_language:
                state.language = LanguageTracker(self._default_language, self._supported_languages)
            self._sessions[session_id] = state
        return state

    def _cancel_event(self, session_id: str) -> asyncio.Event:
        return self._session(session_id).cancelled

    def _get_history(self, session_id: str) -> list[ChatMessage]:
        return self._session(session_id).history

    def _trim_history(self, session_id: str) -> None:
        history = self._session(session_id).history
        # Preserve the leading system message.
        base = 1 if (history and history[0].role == "system") else 0
        max_msgs = self._max_history * 2 + base
        if len(history) > max_msgs:
            # Cut at a user message: orphaning a tool reply from its tool_calls makes OpenAI 400.
            cut = len(history) - self._max_history * 2
            while cut < len(history) and history[cut].role != "user":
                cut += 1
            if cut < len(history):
                # Splice in place: ContextSummarizer holds this list reference.
                history[base:] = history[cut:]
