"""PipelineConversationHandler — ISTT → ILLM → ITTS per utterance; cancel via Event."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from libs.config_sdk import RuntimeConfig, validate_transfer_timeout_ms
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
from .guardrails import GuardrailCounter, GuardrailDetector
from .metrics import IMetrics, NullMetrics
from .tool_latency import ToolLatencyStore
from .provider_bundle import ProviderBundle
from .providers.interfaces import ChatMessage, SttResult
from .session import HandlerResponse, NodeChanged
from .session_finalizer import FinalizationResult, SessionFinalizer
from .tools.llm_adapter import DeterministicSpokenEvent
from .tools.llm_adapter import LocalToolCompletedEvent
from .tools.llm_adapter import TokenEvent as ToolTokenEvent
from .tools.llm_adapter import ToolCallStartedEvent
from .tools.orchestrator import ToolCallOrchestrator
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
    resolve_graph,
    summary_threshold_for,
)

log = logging.getLogger(__name__)


@dataclass
class _SessionState:
    """Per-session state; one entry dropped in on_session_end()."""
    history:                         list[ChatMessage] = field(default_factory=list)
    cancelled:                       asyncio.Event = field(default_factory=asyncio.Event)
    pending_transfer:                "TransferRequest | None" = None
    transfer_requested:              bool = False
    pending_recovery_turns:          list[tuple[str, str, bool]] = field(default_factory=list)
    tool_call_filler_last_phrase:    str | None = None
    tool_call_filler_last_spoken:    float | None = None
    fabrication_triggered_transfer:  bool = False
    confirmed_booking_slot:          str | None = None
    phone_number_confirmed:          bool = False

# Sentence split: !/? always; . unless title abbr / middle initial; also EOS.
_SENTENCE_RE = re.compile(
    r'(?<=[!?])\s+'
    r'|(?<!Mr\.)(?<!Ms\.)(?<!Dr\.)(?<!Sr\.)(?<!Jr\.)(?<!St\.)(?<!Mt\.)(?<!vs\.)'
    r'(?<![A-Z]\.)(?<=[.])\s+'
    r'|(?<=[.!?])$'
)

# Marker stripped before TTS; EndCall after audio drains (servicer.py).
_END_CALL_MARKER = "[[END_CALL]]"
# Fixed safety-net instruction — end steps own natural goodbyes.
_END_CALL_INSTRUCTION = (
    "\n\nWhen the conversation is genuinely finished (the caller says "
    "goodbye, has no more questions, or the issue is resolved), end your "
    f"final reply with the exact token {_END_CALL_MARKER} on its own, after "
    "your spoken words. Only use this token when you are truly ending the "
    "call — never say it out loud or explain it to the caller."
)


_DATE_LOOKUP_DAYS = 8  # today + the next 7 — covers "tomorrow" through "next <weekday>"


def _build_current_date_context(calendar_timezone: str = "UTC") -> str:
    """Nothing tells the LLM what "today" is by default, and models
    reliably miscompute relative dates ("tomorrow") when asked to do the
    arithmetic themselves — a cross-model weakness, not one provider's bug.
    Fixed by computing dates in code and handing the model a lookup table
    for the near term instead of asking it to add/subtract days.

    "Today" is computed in the booking calendar's own timezone
    (policy.extra["timezone"], same field _make_cal_com reads), not UTC —
    near midnight UTC can already be the next calendar day in a business's
    local timezone, so UTC's "today" can lag the caller's and business's
    real local day. Falls back to UTC only if the configured zone name
    doesn't exist. Computed fresh per call (not baked into agent config)
    so it's always accurate regardless of how long the process has run."""
    effective_timezone = calendar_timezone
    try:
        tz = ZoneInfo(calendar_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown calendar_timezone=%r — falling back to UTC for date grounding", calendar_timezone)
        tz = timezone.utc
        effective_timezone = "UTC"
    now = datetime.now(tz)
    lookup = "\n".join(
        f"  {(now + timedelta(days=offset)).strftime('%Y-%m-%d')} = "
        f"{'today' if offset == 0 else 'tomorrow' if offset == 1 else (now + timedelta(days=offset)).strftime('%A')}"
        for offset in range(_DATE_LOOKUP_DAYS)
    )
    return (
        # Label the timezone actually used, not the (possibly invalid)
        # configured one — otherwise a misconfigured zone silently computed
        # the lookup table in UTC while telling the model it was in the
        # business's real timezone, which is a wrong label on every date
        # in the table, not just a cosmetic mismatch.
        f"\n\nToday's date is {now.strftime('%Y-%m-%d')} ({now.strftime('%A')}), {effective_timezone} time — "
        "the business's own local time, which is what matters for scheduling. "
        "Do not compute relative dates yourself — use this exact lookup table instead:\n"
        f"{lookup}\n"
        "For anything beyond this table (e.g. \"in two weeks\"), compute carefully from "
        "today's date above rather than guessing."
    )


def _build_caller_number_context(caller_number: str) -> str:
    """Inject spaced Caller-ID digits for confirmation; "" if no ANI."""
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


_DIGIT_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_AFFIRMATIVE_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|correct|right|that'?s\s+(right|correct)|ok(ay)?)\b", re.IGNORECASE,
)


def _extract_spoken_digits(text: str) -> str:
    """Normalize spoken digit words and numerals to one digit string."""
    out = []
    for word in re.findall(r"[A-Za-z]+|\d+", text):
        if word.isdigit():
            out.append(word)
        else:
            digit = _DIGIT_WORDS.get(word.lower())
            if digit:
                out.append(digit)
    return "".join(out)


def _message_reads_back_phone_number(text: str, caller_number: str) -> bool:
    """True when assistant text appears to read back the caller's number (last 7 digits)."""
    if not caller_number:
        return False
    target = re.sub(r"\D", "", caller_number)
    if len(target) < 7:
        return False
    return target[-7:] in _extract_spoken_digits(text)


def _caller_just_confirmed_phone_number(history: list[ChatMessage], caller_number: str) -> bool:
    """Force book_appointment when prior assistant readback + short affirmative."""
    if len(history) < 2 or history[-2].role != "assistant":
        return False
    if not _AFFIRMATIVE_RE.match(history[-1].content or ""):
        return False
    return _message_reads_back_phone_number(history[-2].content or "", caller_number)


def _claim_matches_confirmed_slot(assistant_text: str, confirmed_datetime: str) -> bool:
    """True if text looks like the confirmed slot (day+hour digits); else treat as other claim."""
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
    # Include rescheduled; \bscheduled\b alone never matches "rescheduled".
    r"\b(booked|rebooked|confirmed|scheduled|rescheduled|all set)\b", re.IGNORECASE,
)
_BOOKING_SUBJECT_RE = re.compile(
    r"\b(appointment|demo|booking|meeting|slot)\b", re.IGNORECASE,
)

# Either tool call is a legitimate reason to use booked/scheduled/rescheduled
# wording — see the fabrication-claim gate's own comment.
_CALENDAR_MUTATION_TOOLS = frozenset({"book_appointment", "reschedule_appointment"})


def _claims_booking_without_tool_call(assistant_text: str) -> bool:
    """Heuristic backstop for fabricated booking claims (false-negatives OK)."""
    return bool(_BOOKING_CLAIM_RE.search(assistant_text) and _BOOKING_SUBJECT_RE.search(assistant_text))

# Marker-only reply needs spoken text or gateway drops EndCall (servicer).
_FALLBACK_GOODBYE = "Goodbye."

# Post-hangup workflow teardown budget (caller already gone).
_FINISH_WORKFLOW_TIMEOUT_S = 3.0
# Pre-transfer extract flush; stragglers continue with whatever landed.
_TRANSFER_FLUSH_TIMEOUT_S = 1.5

# Fixed time-limit line — not an end-step goodbye (would sound like a natural end).
_MAX_DURATION_GOODBYE = (
    "We're at the time limit for this call now. Thanks for calling — goodbye."
)

# Cover dead air when LLM/tool stream raises mid-turn (exception already swallowed).
_FALLBACK_LLM_ERROR = "Sorry, I'm having a little trouble right now. Could you say that again?"

# Collapses a rapid-fire tool-call burst (no real user speech between
# calls — see orchestrator.py's run_turn() while-loop) down to one filler
# instead of several stacked back to back. Phrase wording/sizing itself
# lives in fillers.py (FillerSelector) — see this handler's own
# _fillers/_latency_store fields.
_TOOL_CALL_FILLER_MIN_GAP_S = 4.0

# [[TRANSFER ...]] is detected the same streaming-safe way [[END_CALL]] is
# — via StreamBuffer+DirectiveParser, buffered and stripped mid-stream so a
# directive tag never reaches TTS.
#
# The instruction below is auto-appended to the system prompt whenever the
# agent's policies configure a transfer (see __init__) — operators only
# set transfer_type/transfer_destination (Escalation tab in the admin
# UI), never prompt text, so the destination has a single source of
# truth. servicer.py sends the resulting TransferRequest to the gateway
# (held until the acknowledgment turn's audio finishes playing), and the
# gateway executes it over ESL (uuid_transfer).
_TRANSFER_CONDITION = (
    "If the caller explicitly asks to speak to a human agent or "
    "representative"
)


def _build_transfer_instruction(transfer_type: str, destination: str) -> str:
    token = (
        f'[[TRANSFER type="{transfer_type}" destination="{destination}" '
        'reason="caller_requested_human"]]'
    )
    return (
        f"\n\n{_TRANSFER_CONDITION}, briefly acknowledge that you will connect "
        f"them, then end your reply with the exact token {token} on its own, "
        "after your spoken words. Only use this token when that condition is "
        "met — never say it out loud or explain it to the caller."
    )

# Shallow destination shape check at session setup (routing is FreeSWITCH's).
_SIP_URI_RE = re.compile(r"^sips?:[^@\s]+@[^\s]+$", re.IGNORECASE)
_PHONE_RE   = re.compile(r"^\+?\d{2,15}$")


def transfer_destination_problem(destination: str | None) -> str | None:
    """None if routable; else a short diagnosis for the session-setup log."""
    if destination is None or not destination.strip():
        return "transfer_destination is empty"
    d = destination.strip()
    if d.lower().startswith(("sip:", "sips:")):
        if not _SIP_URI_RE.match(d):
            return f"malformed SIP URI {d!r} (expected sip:user@host)"
        return None
    if not _PHONE_RE.match(d):
        return (
            f"transfer_destination {d!r} is neither a phone number/extension "
            "(2-15 digits, optional leading +) nor a sip:/sips: URI"
        )
    return None

# Ephemeral transfer-failed event for one recovery turn (not stored in history).
def _build_transfer_failed_system_event(reason: str) -> str:
    event = {"type": "system_event", "event": "transfer_failed", "reason": reason or "unknown"}
    return (
        f"{json.dumps(event)}\n\n"
        "The system event above means the attempted transfer to a human agent "
        "failed. Apologize briefly to the caller for not being able to connect "
        "them to an agent, then continue assisting them with their original "
        "request."
    )

# Never leave dead air if recovery LLM produces nothing.
_TRANSFER_FAILED_FALLBACK = "I'm sorry, I couldn't connect you to an agent right now."


# Explain fabrication-triggered handoff (silent transfer read as a bug).
_BOOKING_FABRICATION_TRANSFER_ANNOUNCEMENT = (
    "Let me just double-check that booking with a team member to make sure "
    "it's set up correctly — one moment."
)


class PipelineConversationHandler:
    """ISTT → ILLM → ITTS handler; config resolved once at construction."""


    def __init__(
        self,
        runtime_config: RuntimeConfig,
        provider_bundle: ProviderBundle,
        sample_rate:   int = 16_000,
        max_history:   int = 10,
        transcripts:   TranscriptBuilder | None = None,
        tenant_id:     str = "",
        call_id:       str = "",
        direction:     str = "inbound",
        caller_number: str = "",
        called_number: str = "",
        knowledge:     IKnowledgeProvider | None = None,
        metrics:       IMetrics | None = None,
        tool_orchestrator: ToolCallOrchestrator | None = None,
        has_booking_tool: bool = False,
        use_workflow_draft: bool = False,
        text_only:     bool = False,
        calendar_timezone: str = "UTC",
        latency_store: ToolLatencyStore | None = None,
        filler_selector: FillerSelector | None = None,
    ) -> None:
        self._stt          = provider_bundle.stt
        self._llm          = provider_bundle.llm
        self._tts          = provider_bundle.tts
        self._sample_rate  = sample_rate
        self._max_history  = max_history
        # Admin UI chat: yield words instead of TTS (_speak / proto text_only).
        self._text_only    = text_only
        self._transcripts  = transcripts
        self._tenant_id    = tenant_id
        self._call_id      = call_id
        self._direction     = direction
        self._caller_number = caller_number
        self._called_number = called_number
        # Legacy fallback has empty id — must be None (UUID FK), not a sentinel.
        self._agent_id             = runtime_config.agent.id or None
        self._agent_config_version = runtime_config.version or None
        # Prompt suffix = date / optional ANI context / fixed directive tokens.
        self._has_booking_tool = has_booking_tool
        self._prompt_suffix = (
            # calendar_timezone is already sourced from whichever calendar
            # tool is enabled (book_appointment or reschedule_appointment —
            # see __main__.py) and defaults to "UTC" when neither is, so no
            # has_booking_tool gate is needed here. The caller-number block
            # stays booking-specific — a reschedule-only agent's flow
            # doesn't need the caller-ID confirmation before booking.
            _build_current_date_context(calendar_timezone)
            + (_build_caller_number_context(self._caller_number) if has_booking_tool else "")
            + _END_CALL_INSTRUCTION
        )
        self._goodbye_grace_period_ms = runtime_config.policies.goodbye_grace_ms
        # None = unlimited. Checked per turn in on_speech_ended (no timer task).
        self._max_call_duration_s = runtime_config.policies.max_call_duration_s
        self._call_started_at = time.monotonic()
        # Escalation defaults for record_guardrail_violation().
        self._transfer_type_default        = runtime_config.policies.transfer_type
        self._transfer_destination_default = runtime_config.policies.transfer_destination
        self._escalation_threshold         = runtime_config.policies.escalation_threshold
        # Inputs for transfer_engine._resolve_caller_id().
        self._caller_id_policy  = runtime_config.policies.caller_id_policy
        self._platform_did      = runtime_config.policies.platform_did
        self._custom_caller_id  = runtime_config.policies.custom_caller_id
        self._waiting_experience = runtime_config.policies.transfer_waiting_experience
        # Broken transfer config: log here, skip inject; call stays AI-only.
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
                # Validated config only — avoid stranding mid-"connecting you".
                self._prompt_suffix += _build_transfer_instruction(
                    tt, self._transfer_destination_default,
                )
        self._tenant_slug = runtime_config.tenant.slug
        self._agent_slug  = runtime_config.agent.slug
        self._knowledge = knowledge
        self._session_finalizer = SessionFinalizer(transcripts, metrics)
        self._metrics = metrics if metrics is not None else NullMetrics()
        # None → plain llm.generate(); unused tools still cheap (policy cache).
        self._tool_orchestrator = tool_orchestrator
        # Dynamic call fillers: same opt-in, defaults-to-inert posture as
        # knowledge/metrics above. `latency_store` calibrates the tool-call
        # filler's length; empty store (the default) means every tool call
        # is uncalibrated (fillers.py's _DEFAULT_TARGET_S), identical to
        # today's behavior.
        self._latency_store = latency_store if latency_store is not None else ToolLatencyStore()
        self._fillers = filler_selector if filler_selector is not None else FillerSelector()
        # Phase 6: the single arbiter of *when* to transfer — see
        # transfer_engine.py. Stateless; this handler still owns all
        # per-session state it reads (guardrail count) and produces
        # (_pending_transfer, _transfer_requested below).
        self._transfer_engine = TransferDecisionEngine(self._metrics)
        self._guardrail_counter = GuardrailCounter()
        # Separate from caller-frustration counter (that resets on polite turns).
        self._booking_fabrication_counter = GuardrailCounter()
        self._sessions: dict[str, _SessionState] = {}
        self._last_reported_node_id: str | None = None
        self._draft_fell_back = False
        # Conversation workflow — graph_for() falls back to starter, never None.
        # Same timezone _build_current_date_context() uses below, not UTC —
        # these {{current_date}}/{{current_time}} template variables are
        # dormant (no starter/default node prompt references them today),
        # but a future custom node prompt that does reference them must not
        # silently get the wrong day the same way the lookup table did
        # before this session's fix.
        try:
            _tz = ZoneInfo(calendar_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            _tz = timezone.utc
        now = datetime.now(_tz)
        self._extractor = VariableExtractor(self._llm, self._on_variables_extracted)
        # Tie summary threshold to max_history (else summarization never fires).
        self._summarizer = ContextSummarizer(
            self._llm, threshold_msgs=summary_threshold_for(max_history),
        )
        graph, self._draft_fell_back = resolve_graph(
            runtime_config, draft=use_workflow_draft,
        )
        self._workflow = WorkflowRunner(
            graph,
            base_suffix=self._prompt_suffix,
            variables={
                "caller_number": caller_number,
                "called_number": called_number,
                "direction":     direction,
                "agent_name":    runtime_config.agent.name,
                "business_name": runtime_config.tenant.name,
                "current_date":  now.strftime("%Y-%m-%d"),
                "current_time":  now.strftime("%H:%M"),
                # ponytail: per-contact campaign fields (docs/workflow.md
                # §5.7's second source) would slot in here — they have
                # nowhere to come from today: campaign_contacts stores
                # only phone_number/name, and no channel-variable path
                # carries either to this process. Wire it when contacts
                # grow custom fields; this dict is the only seam it needs.
            },
            extractor=self._extractor,
            summarizer=self._summarizer,
        )
        if (
            any(n.type == "transfer" for n in graph.nodes.values())
            and self._transfer_type_default in ("", "none")
        ):
            # Same fail-loud-at-setup posture as the transfer-destination
            # validation above: a transfer node the engine will always
            # reject is a caller stranded mid-"connecting you now".
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

    def _on_variables_extracted(self, values: dict) -> None:
        """Extraction results merge into the runner, so later nodes' prompts
        can reference them, and get persisted to calls.extracted_variables
        at session end."""
        self._workflow.update_variables(values)

    # ── IConversationHandler ───────────────────────────────────────────────────

    async def greeting(self, session_id: str) -> list[bytes]:
        if self._transcripts is not None:
            self._transcripts.begin_call(
                session_id, self._tenant_id, self._call_id,
                self._direction, self._caller_number, self._called_number,
                self._agent_id, self._agent_config_version,
            )
        # Speaking the instant the line opens gets the first syllable
        # clipped on some outbound carriers — the start node can hold off.
        # 0 (the default) is the behavior every call has today.
        if self._workflow.delayed_start_ms > 0:
            await asyncio.sleep(self._workflow.delayed_start_ms / 1000)
        text = self._workflow.greeting() or ""
        if not text:
            return []
        # Only record the greeting in history once synthesis actually
        # produced audio — _synthesize_sentence_stream swallows a TTS
        # failure internally (logs, then the generator just ends with zero
        # chunks), so appending unconditionally beforehand let the model
        # believe it had greeted the caller even when the caller heard
        # nothing at all, and it would answer straight into the caller's
        # next question with no introduction. Barge-in interrupting
        # otherwise-successful playback is fine to still record — this is
        # scripted, not generated, so the intended line is what the LLM
        # "said," and history needs the LLM's own record, not a transcript
        # of exactly how much audio reached the caller's ear.
        # text_only always yields zero chunks by design (never touches
        # TTS) — that's not a failure signal there, so it's exempted from
        # the gate and always recorded, matching every other spoken line.
        chunks = [chunk async for chunk in self._synthesize_sentence_stream(text, session_id)]
        if chunks or self._text_only:
            self._get_history(session_id).append(ChatMessage(role="assistant", content=text))
        return chunks

    async def on_audio(self, session_id: str, payload: bytes) -> HandlerResponse:
        # Audio is also accumulated by ConversationSession (still the source
        # of truth passed to on_speech_ended) — but forward every chunk to
        # the STT provider immediately too, so a genuinely streaming
        # provider (Deepgram) can transcribe continuously instead of
        # waiting for the whole utterance. A no-op for a provider with no
        # live stream (FasterWhisperSTT) — see ISTT.feed_stream's docstring.
        try:
            await self._stt.feed_stream(session_id, payload, self._sample_rate)
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
        # Fresh per-turn cancel event, replaced before any await.  Never carry
        # over a set event: a cancel targets the response in flight when it was
        # issued, not this new utterance.
        cancel_event = asyncio.Event()
        self._session(session_id).cancelled = cancel_event

        # Free the shared call LLM before this turn's generate — a prior
        # turn's extract/summarize would otherwise queue behind Ollama.
        self._interrupt_workflow_background_llm()

        # Sub-1s blips (echo tails, breaths, ambient noise) make Whisper
        # hallucinate filler or, worse, guess a wrong language entirely on
        # noise with no real content (observed live: a noise blip
        # transcribed as Turkish/Telugu at 50-70% confidence, which then
        # fed a hallucinated LLM response) — 300ms was too permissive.
        # 1000ms matches the threshold validated in an earlier prototype
        # of this pipeline.
        min_bytes = int(1.0 * self._sample_rate) * 2  # 1000 ms of S16LE mono
        if len(audio) < min_bytes:
            log.debug(
                "Skipping short utterance (%d bytes < %d) session=%s",
                len(audio), min_bytes, session_id,
            )
            return

        # Voice-to-voice turn latency instrumentation — see
        # transcript_builder.py's record_turn(); the schema already had
        # stt_latency_ms/llm_latency_ms/tts_latency_ms columns scaffolded
        # since Phase 6a, never actually populated until now. turn_start is
        # the caller's own reference point (end of their speech), the
        # number that actually matters for perceived responsiveness — not
        # any single stage's internal timing.
        turn_start = time.monotonic()

        # ── 1. STT ─────────────────────────────────────────────────────────────
        stt_t0 = time.monotonic()
        try:
            stt_result: SttResult = await self._stt.finalize_stream(session_id, audio, self._sample_rate)
        except Exception:
            log.exception("STT failed session=%s", session_id)
            return
        stt_ms = (time.monotonic() - stt_t0) * 1000

        if not stt_result.text or cancel_event.is_set():
            log.debug("STT empty or cancelled session=%s", session_id)
            return

        log.info("STT result=%r session=%s", stt_result.text, session_id)
        yield HandlerResponse(stt_text=stt_result.text, stt_confidence=stt_result.confidence)

        async for response in self._run_turn(
            session_id, stt_result.text, stt_result.confidence, cancel_event, turn_start, stt_ms,
        ):
            yield response

    async def on_text(
        self, session_id: str, text: str,
    ) -> AsyncGenerator[HandlerResponse, None]:
        """Text-chat turn; same post-STT path as voice (proto TextInput)."""
        text = text.strip()
        if not text:
            return
        # Fresh per-turn cancel event, same contract as on_speech_ended's.
        cancel_event = asyncio.Event()
        self._session(session_id).cancelled = cancel_event
        # Same interrupt as voice: free extract/summarize before this turn.
        self._interrupt_workflow_background_llm()
        yield HandlerResponse(stt_text=text, stt_confidence=1.0)
        async for response in self._run_turn(
            session_id, text, 1.0, cancel_event, time.monotonic(), None,
        ):
            yield response

    async def _run_turn(
        self,
        session_id:   str,
        user_text:    str,
        confidence:   float,
        cancel_event: asyncio.Event,
        turn_start:   float,
        stt_ms:       float | None,
    ) -> AsyncGenerator[HandlerResponse, None]:
        """One turn from caller text to reply; stt_ms is None for typed turns."""
        # Deterministic, inline caller-frustration/abuse signal (see
        # guardrails.py) — the "real detector" record_guardrail_violation()
        # was built to receive. Runs on the transcript already in hand: no
        # LLM call, no network, no media-path cost. Always counted/logged
        # for observability even when escalation_threshold is unset; only
        # actually escalates per that agent's Escalation config. A breach
        # here doesn't yield yet — record_guardrail_violation() just stores
        # a pending TransferRequest, which this same on_speech_ended() call
        # surfaces near its end (see the transfer_request block below): the
        # agent still finishes responding to this utterance normally, then
        # the transfer follows right after, same as an LLM-emitted directive.
        violation = GuardrailDetector.check(user_text)
        if violation is not None:
            log.info(
                "Guardrail violation category=%s matched=%r session=%s",
                violation.category, violation.matched, session_id,
            )
            self.record_guardrail_violation(session_id)
        else:
            # Consecutive counter (per Phase 6 spec): a turn that wasn't
            # flagged resets the streak, so a caller frustrated once, then
            # satisfied, then frustrated again starts counting from 1
            # rather than accumulating across the whole call.
            self._guardrail_counter.reset(session_id)

        # ── Max call duration ────────────────────────────────────────────────
        # Checked here — after STT, so the caller's final utterance is still
        # transcribed/recorded, but before the LLM call, so we never pay for
        # (and then discard) a generated response. Skips the LLM entirely and
        # speaks a fixed, deterministic wrap-up line — same "scripted, not
        # LLM-judged" posture as _FALLBACK_GOODBYE, and reliable
        # regardless of what the model would have said. See __init__ for why
        # this is a per-turn check rather than a separate timer task.
        if (
            self._max_call_duration_s is not None
            and (time.monotonic() - self._call_started_at) >= self._max_call_duration_s
        ):
            log.info(
                "Max call duration (%ds) reached — ending call session=%s",
                self._max_call_duration_s, session_id,
            )
            got_audio = False
            async for response in self._speak(_MAX_DURATION_GOODBYE, session_id):
                got_audio = True
                yield response
            if not got_audio:
                # Synthesis failed outright — fall back so tts_started_sent
                # still flips true on the gateway side (see servicer.py);
                # otherwise EndCall below would be silently dropped, same
                # reasoning as _FALLBACK_GOODBYE.
                async for response in self._speak(_FALLBACK_GOODBYE, session_id):
                    yield response
            if self._transcripts is not None:
                self._transcripts.record_turn(
                    session_id, user_text, confidence,
                    _MAX_DURATION_GOODBYE, False,
                    latency=TurnLatency(
                        stt_ms=stt_ms,
                        stt_engine=None if self._text_only else type(self._stt).__name__,
                        tts_engine=None if self._text_only else type(self._tts).__name__,
                    ),
                )
            yield HandlerResponse(
                end_call=True,
                end_call_grace_period_ms=self._goodbye_grace_period_ms,
            )
            return

        # ── 2. LLM ─────────────────────────────────────────────────────────────
        history = self._get_history(session_id)
        # history[0] is the active node's prompt — refreshed every turn so a
        # mid-call transition lands before the next generation.
        self._refresh_node_prompt(history)
        history.append(ChatMessage(role="user", content=user_text))

        # There's no filler for a plain conversational LLM response, on
        # turn 1 or any other turn — nothing to mask latency-wise, and it
        # would just be stilted small talk. A tool-call filler is different
        # and applies from turn 1 onward: if the caller's very first
        # utterance is a direct request that needs a tool call (e.g. "book
        # me tomorrow at 2pm"), the tool call is real backend latency that
        # needs masking regardless of how little rapport exists yet —
        # silence right after the caller just spoke reads as a dropped
        # call, not politeness. See fillers.py's select_tool_filler and
        # the ToolCallStartedEvent handling below.

        # One retrieve per turn; splice into this turn only (not history).
        messages_for_llm = history
        # In workflow mode retrieval is per-stage: a node with no knowledge
        # base attached does no retrieval at all, the same restrictive
        # reading as its tool list. Unchanged for every other agent.
        if self._knowledge is not None and self._workflow.knowledge_enabled():
            context = await self._retrieve_context(user_text, session_id)
            if context is not None and context.chunks:
                augmented = ChatMessage(
                    role="user",
                    content=f"{self._format_context(context)}\n\nCaller's question: {user_text}",
                )
                messages_for_llm = history[:-1] + [augmented]

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
                messages_for_llm, cancel_event, session_id, directives,
                tool_calls_made, store=history,
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

        # llm_ms: time to the LLM's first token/event — the "thinking" time
        # a caller actually experiences before anything happens. tts_ms:
        # time from that first token to the first synthesized sentence
        # actually being ready — the two are sequential buckets, not
        # overlapping, so they sum toward voice_to_voice_ms below (measured
        # directly too, rather than trusted as a derived sum, since a
        # cancelled/errored turn can leave either timestamp unset).
        llm_ms = (first_token_at - llm_t0) * 1000 if first_token_at else None
        tts_ms = (first_audio_at - first_token_at) * 1000 if (first_audio_at and first_token_at) else None
        voice_to_voice_ms = (first_audio_at - turn_start) * 1000 if first_audio_at else None

        # Directives are already stripped mid-stream (see _llm_to_tts) for
        # whatever actually reached TTS; re-parsing here covers the same
        # ground for the raw joined tokens kept in full_response, which is
        # otherwise unstripped (see _llm_to_tts's docstring: `chunk` is the
        # raw token, not the cleaned text).
        assistant_text = DirectiveParser.parse("".join(full_response)).clean_text.strip()

        # Fabrication check: claim without tool call → escalate (own counter).
# Skip if claim matches a slot we already confirmed via tool.
        _confirmed_slot = self._session(session_id).confirmed_booking_slot
        recap_of_real_booking = (
            _confirmed_slot is not None
            and _claim_matches_confirmed_slot(assistant_text, _confirmed_slot)
        )
        # The claim regex explicitly matches "rescheduled"/"moved" wording
        # too (see _BOOKING_CLAIM_RE's comment), so a genuine
        # reschedule_appointment call must clear this gate the same way a
        # genuine book_appointment call does — checking only for
        # book_appointment let a real reschedule (correctly reporting
        # multiple_bookings_found) get flagged as fabricated.
        real_calendar_mutation = not _CALENDAR_MUTATION_TOOLS.isdisjoint(tool_calls_made)
        fabricated_booking_claim = (
            self._has_booking_tool
            and not recap_of_real_booking
            and not real_calendar_mutation
            and _claims_booking_without_tool_call(assistant_text)
        )

        if full_response and not cancel_event.is_set():
            # A cancelled response (≥1 token, interrupted) is treated as zero
            # tokens: discard it so history only ever holds complete pairs.
            # Marker stripped here too — full_response is raw per-token text, so
            # a marker split across tokens is only contiguous once joined, and
            # it must never leak into history or get referenced in a later turn.
            history.append(ChatMessage(role="assistant", content=assistant_text))
            if fabricated_booking_claim:
                log.warning(
                    "Possible fabricated booking claim (no book_appointment/reschedule_appointment "
                    "call this turn) session=%s text=%r", session_id, assistant_text,
                )
                history.append(ChatMessage(
                    role="system",
                    content=(
                        "Correction: nothing was actually booked, rescheduled, or confirmed just "
                        "now — you did not call book_appointment or reschedule_appointment. If the "
                        "caller still wants that, call the correct tool for real before saying "
                        "anything is booked, moved, or confirmed."
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
                session_id, user_text, confidence,
                assistant_text, cancel_event.is_set(),
                latency=TurnLatency(
                    stt_ms=stt_ms, llm_ms=llm_ms, tts_ms=tts_ms, voice_to_voice_ms=voice_to_voice_ms,
                    # Naming an engine that never ran is worse than naming
                    # none: a typed turn transcribed nothing and spoke
                    # nothing, and call analytics shouldn't read as if it did.
                    stt_engine=None if self._text_only else type(self._stt).__name__,
                    llm_engine=type(self._llm).__name__,
                    tts_engine=None if self._text_only else type(self._tts).__name__,
                ),
                # The node as of the END of the turn: if the model
                # transitioned mid-turn, it generated this reply under the
                # new node's prompt, so that is the stage that said it.
                node_id=self._workflow.node.id,
                node_name=self._workflow.node.name,
            )

        # In a chat session nothing above produced audio, so the reply the
        # model actually generated is delivered here instead. One message
        # per turn, after the tool calls and any mid-turn transition have
        # settled, so the text matches what a caller would have heard.
        # Empty assistant_text still emits turn_complete so a goto-only turn
        # unlocks the chat composer.
        if self._text_only and not cancel_event.is_set():
            if assistant_text:
                any_audio = True
                yield HandlerResponse(agent_text=assistant_text, turn_complete=True)
            else:
                yield HandlerResponse(turn_complete=True)

        # Report the transition (if any) before the turn's terminal events —
        # the editor's canvas should light up the node that just spoke, even
        # on a turn that also ends the call.
        node_changed = self._take_node_changed()
        if node_changed is not None:
            yield HandlerResponse(node_changed=node_changed)

        # Reaching an `end` node ends the call the same way the [[END_CALL]]
        # token does — one teardown path, not two.
        ended_on_end_node = self._workflow.pending_end
        if ended_on_end_node:
            end_call = True
        elif end_call:
            # The model emitted [[END_CALL]] from a node that is not an end
            # node. That instruction is appended to every node's prompt on
            # purpose — it is the safety net for a caller who says goodbye
            # where no edge covers it, and without it the call would hang on
            # until max_call_duration_s. But it skips the end node that would
            # have carried the disposition, so the runner records that the
            # call left the graph instead of reporting no outcome at all.
            self._workflow.ended_off_graph = True

        # LLM [[TRANSFER]] wins over a pending escalation request this turn.
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
            # Reaching a `transfer` node hands the call to the same transfer
            # engine an LLM directive would — modelling handoff as a tool
            # instead would route around transfer_engine, the caller-ID
            # policy, and the gateway's own Transferring state.
            if transfer_request is None:
                transfer_request = await self._workflow_transfer(session_id)

            # Fall through to a pending escalation-accepted request whenever
            # the directive path produced nothing — including when a
            # directive WAS emitted but the engine rejected it as
            # already_transferring precisely BECAUSE that pending request
            # exists. Without this, the accepted pending transfer would be
            # starved for as long as the LLM keeps re-emitting directives.
            if transfer_request is None:
                state = self._session(session_id)
                transfer_request = state.pending_transfer
                state.pending_transfer = None

        # Signal the servicer to end the call once this turn's audio has
        # streamed — but not if the caller barged in (cancel_event.is_set()):
        # them talking means the agent's decision to end the call is stale.
        # Also not if a transfer is about to happen instead (see comment
        # above) — ending the call would make the transfer unreachable.
        if end_call and not cancel_event.is_set() and transfer_request is None:
            # The closing words are the end step's own — its prompt drove
            # this turn's generation (the transition swaps history[0]
            # mid-turn), so by here the goodbye is already streaming. There
            # is no second, agent-level farewell to speak on top of it; that
            # column existed to override the graph and is gone.
            if not any_audio:
                # Marker-only reply with nothing spoken (the model emitted
                # [[END_CALL]] and no words): synthesize a fallback so the
                # servicer actually has audio to key EndCall off of.
                async for response in self._speak(_FALLBACK_GOODBYE, session_id):
                    yield response
            yield HandlerResponse(
                end_call=True,
                end_call_grace_period_ms=self._goodbye_grace_period_ms,
            )

        if transfer_request is not None:
            # A fabrication-triggered transfer always gets its own specific
            # line — see that constant's comment for why a silent/generic
            # handoff right after a false "booked" claim reads as the
            # system being broken. Workflow transfer nodes / LLM-directive
            # transfers already spoke their acknowledgment via the node's
            # own prompt or the model's reply.
            if self._session(session_id).fabrication_triggered_transfer:
                self._session(session_id).fabrication_triggered_transfer = False
                async for response in self._speak(
                    _BOOKING_FABRICATION_TRANSFER_ANNOUNCEMENT, session_id,
                ):
                    yield response
            yield HandlerResponse(transfer_request=transfer_request)

    async def on_cancel(self, session_id: str) -> None:
        self._cancel_event(session_id).set()
        # Barge-in before pending_speech is consumed must not replay it next turn.
        self._workflow.pending_speech = None
        self._interrupt_workflow_background_llm()

    async def on_session_end(self, session_id: str, reason: str,
                             final_state: str | None = None) -> None:
        # Outcome must spawn before end_call() drops the write chain.
        await self._finish_workflow(session_id)
        # Requirement: transfer-failure recovery turns are recorded in
        # short-term memory (history) immediately, in on_transfer_failed(),
        # but their transcript *persistence* is deferred until now — normal
        # SessionEnd — rather than written immediately like every other
        # turn's record_turn() call.
        state = self._sessions.get(session_id)
        pending_recovery_turns = state.pending_recovery_turns if state is not None else []
        if self._transcripts is not None:
            for caller_text, ai_response, interrupted in pending_recovery_turns:
                self._transcripts.record_turn(session_id, caller_text, 1.0, ai_response, interrupted)
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
        """destination_override is a workflow transfer node's own
        destination (see docs/workflow.md §2.3) — it replaces the agent-wide
        default for that one decision and nothing else."""
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

    # ── Workflow helpers ─────────────────────────────────────────────────

    def _take_node_changed(self) -> NodeChanged | None:
        """True once when the active node id changes (for UI highlight)."""
        node = self._workflow.node
        if node.id == self._last_reported_node_id:
            return None
        self._last_reported_node_id = node.id
        return NodeChanged(
            node_id=node.id, node_name=node.name, node_type=node.type,
            via=self._workflow.last_transition,
        )

    def _refresh_node_prompt(self, history: list[ChatMessage]) -> None:
        """Refresh history[0] to the active node's rendered prompt (in place)."""
        prompt = ChatMessage(role="system", content=self._workflow.system_prompt())
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

    def record_guardrail_violation(self, session_id: str) -> TransferRequest | None:
        """Record consecutive guardrail hits; escalate when threshold crossed."""
        return self._evaluate_escalation(session_id, self._guardrail_counter.increment(session_id))

    def record_booking_fabrication(self, session_id: str) -> TransferRequest | None:
        """Escalation for fabricated booking claims (own counter; see __init__)."""
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
        """Recover after failed transfer: apologize via LLM, continue the call."""
        cancel_event = asyncio.Event()
        self._session(session_id).cancelled = cancel_event

        # Clear latch so a later ask is not "already_transferring".
        self._session(session_id).transfer_requested = False
        # Leave transfer node (no outbound edges) or recovery is dead-ended.
        self._workflow.abandon_transfer()
        # Drop early summary — call continues after failed transfer.
        self._session_finalizer.discard_pending_summary(session_id)

        history = self._get_history(session_id)
        # Recovery must use the node we reverted to.
        self._refresh_node_prompt(history)

        # Wrapped event: bare JSON confuses small local models.
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
                messages_for_llm, cancel_event, session_id, directives, store=history
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
            # LLM produced nothing usable — never leave dead air, same
            # reasoning as _FALLBACK_GOODBYE above.
            assistant_text = _TRANSFER_FAILED_FALLBACK
            async for response in self._speak(assistant_text, session_id):
                yield response

        # Persist assistant apology only; ephemeral notice stays out of history.
        if assistant_text and not cancel_event.is_set():
            history.append(ChatMessage(role="assistant", content=assistant_text))
            self._trim_history(session_id)

        # Buffer transcript write until SessionEnd (history already updated).
        self._session(session_id).pending_recovery_turns.append((
            f"[transfer_failed: {reason}]", assistant_text, cancel_event.is_set(),
        ))

    def on_transfer_cancelled(self, session_id: str) -> None:
        """Barge-in cancelled transfer before dispatch — clear latch + abandon node."""
        self._session(session_id).transfer_requested = False
        self._workflow.abandon_transfer()
        self._session_finalizer.discard_pending_summary(session_id)

    def start_finalization(self, session_id: str) -> None:
        """Kick off early summary on TransferInitiated (session_finalizer)."""
        self._session_finalizer.start_summary_early(
            session_id, self._get_history(session_id), self._llm,
        )

    async def finalize_session(
        self, session_id: str, reason: str = "transfer_completed",
    ) -> FinalizationResult:
        """Post-transfer cleanup via SessionFinalizer; returns result for gRPC."""
        return await self._session_finalizer.finalize(
            session_id,
            self._get_history(session_id),
            self._llm,
            self._cancel_event(session_id),
            reason,
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
        """llm.generate plus tool-start filler and transition bridging events."""
        if self._tool_orchestrator is None:
            async for token in self._llm.generate(history):
                yield token
            return

        # Force book_appointment only on the turn right after phone confirmation.
        just_confirmed = self._has_booking_tool and _caller_just_confirmed_phone_number(
            history, self._caller_number,
        )
        force_tool_name = "book_appointment" if just_confirmed else None
        if just_confirmed:
            self._session(session_id).phone_number_confirmed = True

        # Callables: re-read tools after mid-turn transition (orchestrator).
        local_tools = lambda: self._workflow.local_tools(history, store)  # noqa: E731
        only_tools = lambda: self._workflow.allowed_tool_names()       # noqa: E731

        async for event in self._tool_orchestrator.run_turn(
            self._agent_id or "", self._tenant_id, self._call_id, session_id, history,
            caller_number=self._caller_number, cancel_event=cancel_event,
            force_tool_name=force_tool_name,
            phone_number_confirmed=self._session(session_id).phone_number_confirmed,
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
        """Stream tokens → sentences → TTS. Directives stripped before TTS; end_call latches True for the rest of the turn."""
        stream_buf  = StreamBuffer()
        text_buffer = ""  # directive-free text awaiting a sentence boundary
        if tool_calls_made is None:
            tool_calls_made = []
        # Latch end_call across yields — filler/transition must not clear it.
        end_call = False
        try:
            async for item in self._token_stream(
                history, session_id, cancel_event, tool_calls_made, store,
            ):
                if cancel_event.is_set():
                    self._workflow.pending_speech = None
                    break
                if isinstance(item, LocalToolCompletedEvent):
                    # Bridging line during transition (barge-in-able).
                    speech = self._workflow.pending_speech
                    if speech:
                        self._workflow.pending_speech = None
                        if self._text_only:
                            # text_only: synthesis no-op — yield text or lose the line.
                            yield speech, b"", end_call
                        else:
                            # Yield text once so full_response/history/transcript
                            # record it (same as text_only above).
                            first = True
                            async for chunk in self._synthesize_sentence_stream(speech, session_id):
                                yield speech if first else "", chunk, end_call
                                first = False
                    continue
                if isinstance(item, ToolCallStartedEvent):
                    # Sized against this (tenant, agent, tool)'s calibrated
                    # average — see fillers.py's select_tool_filler and
                    # tool_latency.py — gap-suppressed by
                    # _TOOL_CALL_FILLER_MIN_GAP_S so a burst of tool calls
                    # in one turn speaks only once.
                    now = time.monotonic()
                    state = self._session(session_id)
                    last_spoken = state.tool_call_filler_last_spoken
                    if last_spoken is None or (now - last_spoken) >= _TOOL_CALL_FILLER_MIN_GAP_S:
                        state.tool_call_filler_last_spoken = now
                        # text_only: fillers would pollute assistant history.
                        if self._text_only:
                            continue
                        average_ms = self._latency_store.average_ms(
                            self._tenant_id, self._agent_id or "", item.tool_name,
                        )
                        phrase = self._fillers.select_tool_filler(
                            item.tool_name, state.tool_call_filler_last_phrase, average_ms,
                        )
                        state.tool_call_filler_last_phrase = phrase
                        any_filler_chunk = False
                        async for chunk in self._synthesize_sentence_stream(phrase, session_id):
                            if cancel_event.is_set():
                                # A barge-in during the filler itself must
                                # interrupt it, same as any other spoken
                                # text — without this check, the longest
                                # filler phrase (~2.4s) was a window where
                                # the caller's interruption went unheard.
                                break
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
                # text_only: apology must land in full_response (no TTS).
                if self._text_only:
                    yield _FALLBACK_LLM_ERROR, b"", False
                else:
                    fallback_text = _FALLBACK_LLM_ERROR
                    async for chunk in self._synthesize_sentence_stream(
                        _FALLBACK_LLM_ERROR, session_id,
                    ):
                        yield fallback_text, chunk, False
                        fallback_text = ""

        # Whatever StreamBuffer still has pending never closed into a
        # complete tag — a false-positive lookalike (the model literally
        # said "[[" in prose), not a real directive. Flush it as ordinary
        # text rather than silently dropping it.
        text_buffer += stream_buf.flush()

        end_call = any(isinstance(d, EndCallDirective) for d in directives)
        if text_buffer.strip() and not cancel_event.is_set():
            async for chunk in self._synthesize_sentence_stream(text_buffer.strip(), session_id):
                yield "", chunk, end_call

    async def _retrieve_context(self, query: str, session_id: str):
        # A retrieval failure must never fail the turn — same "degrade to
        # no context, not a broken response" contract IKnowledgeProvider
        # itself already guarantees (RepositoryUnavailableError is caught
        # inside CacheAsideKnowledgeProvider), but this is defense in depth
        # against any other exception (a bad MockKnowledgeProvider in a
        # test, an unexpected bug) reaching the caller's turn.
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
        # The one shared boundary every text source reaches TTS through —
        # the LLM-token path already ran DirectiveParser.parse() upstream,
        # but the greeting and the fixed fallback/filler strings never do
        # (see strip_markdown_chars' docstring), so this
        # is where all of them get covered instead of at every call site.
        if self._text_only:
            # text_only: never touch TTS (slow load; nobody plays audio).
            return
        text = strip_markdown_chars(text)
        # Forwards each chunk the TTS provider yields immediately — real
        # latency win only for a provider with genuine incremental
        # synthesis (Deepgram today); a no-genuine-streaming provider
        # (macOS/Kokoro/ElevenLabs) yields its one complete result once
        # (see ITTS.synthesize_stream's docstring).
        try:
            async for chunk in self._tts.synthesize_stream(text, self._sample_rate):
                yield chunk
        except Exception:
            log.exception("TTS streaming failed text=%r session=%s", text, session_id)

    async def _speak(self, text: str, session_id: str) -> AsyncGenerator[HandlerResponse, None]:
        """Speak one fixed line (TTS on call, text in chat)."""
        if self._text_only:
            if text.strip():
                yield HandlerResponse(agent_text=text)
            return
        async for chunk in self._synthesize_sentence_stream(text, session_id):
            yield HandlerResponse(tts_payloads=[chunk])

    def greeting_message(self) -> str:
        """The opening line as text, for a text_only session. Pure — the
        side effects (begin_call, the start node's delayed start) belong to
        greeting(), which runs first either way."""
        return self._workflow.greeting() or ""

    def opening_events(self) -> list[HandlerResponse]:
        """Start-node highlight + draft-fallback note for the admin UI."""
        out: list[HandlerResponse] = []
        if self._draft_fell_back:
            out.append(HandlerResponse(
                session_note=(
                    "Your draft doesn't parse — running the published (live) "
                    "flow instead."
                ),
            ))
        node_changed = self._take_node_changed()
        if node_changed is not None:
            out.append(HandlerResponse(node_changed=node_changed))
        return out

    def _session(self, session_id: str) -> _SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            state = _SessionState()
            self._sessions[session_id] = state
        return state

    def _cancel_event(self, session_id: str) -> asyncio.Event:
        return self._session(session_id).cancelled

    def _get_history(self, session_id: str) -> list[ChatMessage]:
        return self._session(session_id).history

    def _trim_history(self, session_id: str) -> None:
        history = self._session(session_id).history
        # Preserve a leading system message so it is not silently sliced away
        # when the conversation grows past max_history turns.  Without this,
        # history[-N:] drops history[0] and the `if not history` guard on the
        # next turn is never True, so the system prompt is never re-injected.
        base = 1 if (history and history[0].role == "system") else 0
        max_msgs = self._max_history * 2 + base
        if len(history) > max_msgs:
            # In-place splice: ContextSummarizer holds this list reference.
            history[base:] = history[-self._max_history * 2:]
