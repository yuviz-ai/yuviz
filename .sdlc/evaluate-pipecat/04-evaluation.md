# Evaluation: Pipecat vs. the in-house conversation pipeline

Deliverable for the PRD in `01-prd.md`, built to the method in `02-design.md`. Read §1 alone if you only want the
decision; §10 is the direct "what benefit, if any" answer.

## How to read the evidence tags

Every factual assertion about Pipecat, Dograh or a third party carries one:

- `[E1 source-read @ <repo>@<sha>]` — read directly in that repo at that commit. **Static facts only** (a file
  exists, a class is exported, a submodule is pinned, a serializer is absent). Still not runtime-verified.
- `[E2 vendor-doc, unverified]` — Pipecat/Dograh docs, README, CHANGELOG, GitHub issue text.
- `[E3 third-party/marketing, unverified]` — comparison articles, vendor marketing.

**No smoke test was run, deliberately.** A Pipecat latency number produced in a local venv, off this platform's
Kamailio → FreeSWITCH → bridge media path, would not measure the criterion it appears to measure — it would measure
a laptop. So every behavioural or performance claim (latency, cold start, audio quality, concurrency) is capped at
`unverified` **regardless of the tier of its source**, including `E1` ones. `E1` narrows *what* is unverified; it
never upgrades a runtime claim. Appendix C names the experiment that would settle each one.

Pinned sources for this document:

| Source | Commit | Read on |
|---|---|---|
| this repo | `9646aece5eedc9da4dcc6c9bc13a52f504a3764e` | 2026-09-15 |
| `pipecat-ai/pipecat` | `f236a08991065b645329fb4f3f02fc403f29be5e` (v1.10.0 line, HEAD 2026-09-14) | 2026-09-15 |
| `dograh-hq/dograh` | `23d22b95defbd0a23719a7f9b265eb9f7acdebf8` | 2026-09-15 |
| `dograh-hq/pipecat` (Dograh's fork) | `70385ad8a5204458a241e60287289932e3eb3ae2` (the pinned submodule) | 2026-09-15 |

---

## §1 Decision

**One sentence: reject Pipecat as a replacement for the conversation pipeline and the telephony/media layer;
partially adopt it — as a new, parallel runtime, not a migration of the existing one — for speech-to-speech only.**

```
(a) Cascaded STT→LLM→TTS pipeline, transport, VAD, barge-in, transfer, guardrails, tools, RAG
Verdict: Reject
1 Latency:         unknown — no benchmark was run and none of the 4 decision criteria can be
                   scored on latency without one [see Appendix C.1]
2 Service count:   unchanged — Pipecat replaces library-level plumbing inside services/conversation,
                   not any service boundary; the gRPC contract, Gateway and Config Service all remain
3 OSS-over-custom: favors adopt — Pipecat is 15.5k stars, 2.7k forks, v1.10.0 released 2026-09-11,
                   pushed within 24h of this read [E1 @ pipecat-ai/pipecat@f236a08]
4 Scalability:     parity — both are per-session async Python; neither solves session affinity
Non-negotiables:   FAILS — Pipecat's own maintainer's answer to the self-hosted-production gap is
                   "use a hosted transport"; our stack is the opposite bet [E2, issue #3987]
Revisit when:      Pipecat ships a first-party SIP/RTP transport that terminates a carrier trunk
                   without a hosted media service in the path, AND Appendix C.1's A/B benchmark shows
                   voice-to-voice parity-or-better on our own SIP path.
```

```
(b) Speech-to-speech (OpenAI Realtime / Gemini Live) as a NEW capability
Verdict: Partial adopt (Pipecat's realtime LLM services + frame model, as a second pipeline
         alongside the cascaded one — not as a replacement for it)
1 Latency:         better — S2S removes two network hops (STT, TTS) by construction; the
                   architectural claim is sound, the magnitude is unverified [E2/E3]
2 Service count:   unchanged — but avoids building a second in-house pipeline runtime from zero
3 OSS-over-custom: strongly favors adopt — 9 shipped S2S services and ~30 realtime examples
                   in-tree [E1 @ pipecat-ai/pipecat@f236a08]; building this ourselves is months
4 Scalability:     parity — an S2S session is one long-lived vendor WebSocket either way
Non-negotiables:   pass — S2S is greenfield; nothing existing is displaced, nothing on today's
                   media path changes
Revisit when:      n/a (this is the adopt case). Bound it: adopt the *services and frame model*,
                   not the transport — the trunk still terminates on our own stack.
```

```
(c) Cloudonix as a telephony provider WITH Pipecat
Verdict: Reject as "already supported"; feasible but it is bespoke integration work
1 Latency:         unknown — no Cloudonix trial account was available; nothing measured
2 Service count:   unchanged
3 OSS-over-custom: neutral — the OSS framework does not contain the integration; you write it
4 Scalability:     unknown
Non-negotiables:   FAILS as stated — Pipecat upstream has **no Cloudonix transport and no
                   Cloudonix serializer** [E1 @ pipecat-ai/pipecat@f236a08: `grep -ril cloudonix`
                   over the whole repo returns zero files]
Revisit when:      upstream Pipecat merges a `pipecat.serializers.cloudonix`, or we obtain a
                   Cloudonix trial account and confirm the media contract firsthand.
```

`services/campaigns`: **keep** — Pipecat has no campaign, DNC, contact-list or pacing surface (§6.1).
`services/webcall`: **keep for now, revisit** — Pipecat's client SDKs + `SmallWebRTCTransport` cover the browser
half, but retiring the service requires retiring the Conversation gRPC contract too (§6.2).

**Two findings that revise the PRD's directional lean.** The PRD leaned "skeptical of full replacement, open to
narrow plumbing-layer adoption." Both halves move:

1. The *plumbing-layer* case is weaker than the PRD assumed, because the reliability motivation does not hold up
   (§4.8, §11). Pipecat has a materially better *architecture* for interruption than ours — but the same bug class
   is live and open in its own tracker right now.
2. The *speech-to-speech* case is far stronger than the PRD anticipated — the PRD does not mention S2S at all, and
   it is the one place where "adopt the OSS framework" wins on all four criteria (§5A).

---

## §2 What Pipecat would have to replace — inventory of today

Grep-verified from this repo at `9646aec`. Line counts are `wc -l`. This list is the source of every ledger row in
§4–§6; anything here without a row is a lessons #7 violation.

**`services/conversation/` (7,558 lines of Python across 26 modules + `tools/` + `workflow/` + `providers/`)**

| Module | Lines | What it owns |
|---|---|---|
| `pipeline.py` | 1518 | `PipelineConversationHandler`; `on_speech_ended`, `_run_turn`, `_token_stream`, `_llm_to_tts`, `_synthesize_sentence_stream`, `_retrieve_context`, `_cancel_event`, `_speak`; end-call marker `_END_CALL_MARKER`; date/caller-number context builders; booking-fabrication detection (`_claims_booking_without_tool_call`, `_CALENDAR_MUTATION_TOOLS`); phone-readback confirmation (`_extract_spoken_digits`, `_caller_just_confirmed_phone_number`); transfer instruction/announcement injection; `_TOOL_CALL_FILLER_MIN_GAP_S` burst gap |
| `servicer.py` | 779 | gRPC service surface |
| `session.py` / `session_finalizer.py` | 593 / 412 | session lifetime, finalization |
| `fsm.py` | 400 | `ConversationFSM`, `CallFsmState` (IDLE/LISTENING/THINKING/SPEAKING/**BARGE_IN**/CLOSING/CLOSED), `ConversationFsmHandlers` |
| `transcript_builder.py` | 379 | transcript assembly + recording |
| `ai_provider_manager.py` | 388 | provider selection/bundling; `providers/` STT/LLM/TTS |
| `transfer_engine.py` | 262 | `TransferDecisionEngine`, `TriggerType` (LLM_DIRECTIVE / ESCALATION / WORKFLOW / EXTERNAL), `TransferReason`, `DecisionContext` (caller-ID policy, waiting experience), `Decision` |
| `directives.py` | 177 | `DirectiveParser`, `StreamBuffer` (mid-stream, partial-tag-safe), `TransferDirective`, `EndCallDirective`, `UnknownDirective`, `TransferRequest`, `strip_markdown_chars` |
| `guardrails.py` | 132 | `GuardrailDetector.check`, `GuardrailCounter` (per-session increment/reset/current) |
| `fillers.py` | 75 | `FillerSelector.select_tool_filler` — latency-sized filler choice, total function |
| `tool_latency.py` | 121 | per-(tenant, agent, tool) calibrated average that sizes the filler |
| `tools/orchestrator.py` | 513 | `ToolCallOrchestrator.run_turn`, `_execute_tool_call`, `_check_requested_date` |
| `tools/` others | 1,067 | `registry.py`, `policy_resolver.py`, `middleware.py`, `llm_adapter.py`, `date_sanity.py`, `provider_manager.py`, `executor_registry.py`, `types.py` |
| `workflow/runner.py` + `extractor.py` | 678 | shipped node-graph workflow runtime + variable extraction |
| `provider_config_subscriber.py` | 78 | runtime config invalidation |
| `event_bus.py`, `metrics.py`, `agent_config.py`, `agent_resolver.py`, `echo.py`, `pipeline_config.py`, `provider_bundle.py`, `secret_resolver.py` | 816 | supporting |

**`services/vobiz/` (814 lines)** — `VobizCallBridge` (`bridge.py`, 461): `_playback_pacer`, `_audio_delay_pump`,
`_clear_playback_queue`, `_flush_pending_audio_now`, `_start_turn`/`_resolve_turn`/`_turn_watchdog_fire`,
`_run_vad`, `_vobiz_to_grpc`, `_grpc_to_vobiz`; constants `_ULAW_FRAME_BYTES`, `_PLAYBACK_LEAD_S`,
`_AUDIO_DELAY_S`, `_FINAL_MARKER`, `_TURN_WATCHDOG_S`. Plus `app.py` (265), `audio.py` (62, µ-law↔PCM16 +
resample), `redis_route.py` (56).

**`libs/vad_sdk/` (291)** — `EnergyVAD` (`vad.py`), `SileroVAD` (`silero_vad.py`, 32ms/1024-byte fixed window).

**`libs/telephony_sdk/` (268)** — `ITelephonyProvider` (`interface.py`), `registry.py`,
`providers/vobiz.py` (`VobizTelephonyProvider`: `auth_id`/`auth_token` credentials, `initiate_call` with
`answer_url`/`hangup_url`/`ring_url`, `hangup_call`, `get_call_status`, `verify_webhook_signature`,
`build_answer_response`).

**`libs/knowledge_sdk/`** — `IKnowledgeProvider.retrieve`, `IRetrievalRepository`, `IKnowledgeAvailabilityRepository`,
`RetrievalPolicy`, cache-aside provider that degrades to no-context rather than failing a turn.

**`libs/config_sdk/`** — `IConfigProvider` (`get_runtime_config`, `get_tenant`, `get_agent`,
`get_provider_config`, `get_prompt`, `get_voice`, `get_tools`), `IConfigRepository`, immutable `RuntimeConfig`,
`workflow.py`, `secrets.py`/`secret_resolver.py`.

**`services/campaigns/`** — `originate.py` (raw ESL `bgapi originate` + `EslJobEventListener` on a persistent
`BACKGROUND_JOB` subscription), `worker.py` (`CampaignWorker`: pacing, per-campaign concurrency cap, calling-hours
window, claim/retry), `dnc.py`, `campaign_contacts.py`, `audit.py`, `campaigns.py`, `schemas.py`, `routers/`.

**`services/webcall/`** — `__main__.py`: raw-PCM16 WebSocket → gRPC bridge, explicit client `speech_ended`/`cancel`/
`playback_finished` control messages (push-to-talk, no VAD), `ResponseWatchdog` (arm/saw/disarm/fire), per-utterance
WAV dump, session-token + tenant auth check against the Config Service.

---

## §3 Pipecat as it actually is

Read at `pipecat-ai/pipecat@f236a08`, not from marketing.

**Maturity.** 15,556 stars, 2,691 forks, 326 open issues; `pushed_at` 2026-09-15T13:48Z — i.e. within hours of this
read [E1 @ pipecat-ai/pipecat@f236a08]. **Version 1.10.0, released 2026-09-11** [E1, `CHANGELOG.md`]. This is the
single biggest correction to the PRD's framing: the PRD's cited production complaints (SmallWebRTC audio-quality
regressions "since v0.0.62") are **pre-1.0**, from a release the maintainer dates to "one year ago" [E2, issue
#3987 comment 2026-03-15]. Carrying 0.0.x-era complaints into a 1.10 decision would be the wrong comparison.

**Core model.** `Pipeline` / `FrameProcessor` / `PipelineTask`. Frames are typed dataclasses in
`src/pipecat/frames/frames.py`. Interruption is a `SystemFrame` subclass, `InterruptionFrame`, whose docstring is
"Frame pushed to interrupt the pipeline… It can also be pushed by any processor" [E1]. `FrameProcessor` exposes
`broadcast_interruption()`, which pushes it **both upstream and downstream** (`frame_processor.py:1017-1022`), and
`_start_interruption()` cancels that processor's in-flight process task [E1]. A `UninterruptibleFrame` mixin
(`frames.py:147`) marks frames that must survive an interruption; `_start_interruption` checks whether the
currently-processing frame is one and, if so, flushes only the interruptible queue instead of cancelling
(`frame_processor.py:1130-1149`) [E1].

**Turn-taking.** A whole `src/pipecat/turns/` package: `user_turn_controller.py`, `user_turn_processor.py`,
`user_turn_strategies.py`, `speculation_gate.py`, `eager_end_of_turn_mixin.py`, `user_turn_completion_mixin.py`,
`user_idle_controller.py`, `user_mute/`, `user_start/`, `user_stop/` [E1]. Frames include
`EagerEndOfTurnCancelFrame` — "withdrawing an eager end of turn" — and `UserStoppedSpeakingFrame`'s docstring
explicitly references `SpeculationGate` releasing a held speculative response [E1]. This is speculative execution
of the bot turn before end-of-turn is confirmed, with a documented withdrawal path.

**VAD & turn detection.** `src/pipecat/audio/vad/`: `silero.py`, `aic_quail_vad.py`, `krisp_viva_vad.py`,
`vad_analyzer.py` (`VADParams.start_secs`), `vad_controller.py`. Separately `src/pipecat/audio/turn/`:
`base_turn_analyzer.py`, `krisp_viva_turn.py`, and `smart_turn/` with `http_smart_turn.py`,
`local_coreml_smart_turn.py`, `local_smart_turn_v2.py`, `local_smart_turn_v3.py` — semantic (model-based)
end-of-turn detection, not just silence [E1].

**Transports** (`src/pipecat/transports/`): `daily/`, `livekit/`, `smallwebrtc/`, `websocket/`, `vonage/`,
`whatsapp/`, `moq/`, `local/`, plus avatar transports `heygen/`, `tavus/`, `lemonslice/` [E1].
**There is no SIP or RTP transport.** Telephony is reached through the WebSocket transport plus a per-carrier
`FrameSerializer`: `src/pipecat/serializers/` ships `twilio.py` (314), `telnyx.py` (292), `plivo.py` (256),
`vonage.py` (188), `exotel.py` (171), `genesys.py` (964) [E1]. That is the whole telephony story upstream.

**Output pacing.** `base_output.py` writes "10ms*CHUNKS of audio at a time… This helps with interruption handling.
If we receive long audio frames we will chunk them" (`audio_out_10ms_chunks`), and runs a separate `_clock_task` +
`_audio_task` with a `_bot_speaking_frame_period` [E1]. This is structurally the same insight as our own
`_playback_pacer` — pace playback so there is something left to cancel.

**Tool calls.** `processors/aggregators/async_tool_messages.py` implements an async-tool protocol: a function
registered with `cancel_on_interruption=False` gets `started` / `intermediate` / `final` context messages so a
result arriving after the model moved on still reaches it, and a cancellation always writes a `final` message
"because it has to tell the LLM the task did not complete" [E1]. There are 6 `realtime-*-async-tool.py` examples
[E1].

**Higher-level blocks, in-tree at 1.10.** `src/pipecat/flows/` (`flow.py`, `manager.py`, `config.py`,
`flow_config.schema.json`) — `pipecat-flows` is now part of the core package, with YAML flow definitions
(`examples/flows/yaml/…`) [E1]. `src/pipecat/extensions/ivr/ivr_navigator.py` and
`extensions/voicemail/voicemail_detector.py` [E1]. `src/pipecat/evals/` — a whole eval harness with
`judge.py`, `matcher.py`, `persona.py`, `simulation*.py`, `script*.py` [E1]. `services/mcp_service.py` is an MCP
**client** (Pipecat calls external MCP tools), not an MCP server for editing agent config [E1].

**Known-gap review.** Issue **#3987**, "Self-hosted deployment: battle-tested blueprints and OOTB production
support" — the PRD describes this as open; **it is closed, 2026-03-11** [E1 via GitHub API]. That is a material
correction, but the *content* of the closure matters more than the status. The filer's complaint was that
deployment examples "are getting-started guides, not battle-tested production blueprints… None cover session
affinity, scaling under [load]" [E2]. The maintainer's answer, verbatim: "In the vast majority of cases, the right
call is running SmallWebRTC for local development, but then moving to a hosted solution for production. This allows
teams to focus their time and energy on things that differentiate their product; not running a WebRTC service."
[E2, comment 2026-03-24]. So the gap is acknowledged and the sanctioned remedy is **a managed transport**. That is
the exact architectural bet this platform declined when it built its own Kamailio + FreeSWITCH + bridge path.

---

## §4 Stage-by-stage mapping

Rows are capabilities of *our* modules. Verdicts: `MATCH` | `PIPECAT-SUPERIOR` | `PARTIAL` | `GAP` | `DROPPED`.

### §4.1 Transport / SIP bridging

| Capability (our module:symbol) | What it does today | Pipecat equivalent | Verdict | Tier |
|---|---|---|---|---|
| Carrier trunk termination — Kamailio dialplan + FreeSWITCH | SIP/RTP terminated on infrastructure we run; the dialplan pattern is in `scripts/kamailio/kamailio.cfg.tpl` | none — no SIP or RTP transport exists in `src/pipecat/transports/` | **GAP** | E1 @ pipecat-ai/pipecat@f236a08 |
| `services/vobiz/bridge.py:VobizCallBridge.run` | carrier media WebSocket ⇄ Conversation gRPC shuttle | `FastAPIWebsocketTransport` + a per-carrier `FrameSerializer` | **MATCH (shape)** | E1 |
| `services/vobiz/audio.py` | µ-law@8k ⇄ PCM16@16k both directions | serializer + `audio/resamplers/` (`soxr`, `resampy`) | **MATCH** | E1 |
| `libs/telephony_sdk/interface.py:ITelephonyProvider` | provider-config abstraction: credential fields, `initiate_call`, `hangup_call`, `get_call_status`, `verify_webhook_signature`, `build_answer_response` | **none** — Pipecat has no provider-config or call-control abstraction at all; serializers are media-frame-only | **GAP** | E1 |
| `libs/telephony_sdk/registry.py` | provider discovery so the admin UI renders the right form | none | **GAP** | E1 |
| `services/vobiz/redis_route.py` | Redis-only routing lookup on the call path | none — Pipecat has no routing concept | **GAP** (correctly — out of its scope) | E1 |

`ITelephonyProvider` being a **GAP** is the load-bearing row. Pipecat's serializer solves "how do I turn this
carrier's WebSocket frames into audio frames." It does not solve "how do I place a call, hang one up, verify an
inbound webhook signature, or tell an admin UI which credential fields this carrier needs." Dograh had to build all
of that itself (§7c) — 17,907 lines of it.

```
Recommendation — transport/SIP bridging
Verdict: Reject
1 Latency:         unknown — no measurement; the transport sits on the media path so this alone
                   blocks an adopt verdict [Appendix C.1]
2 Service count:   unchanged — a Pipecat WebSocket transport replaces bridge.py's inner loop, not
                   services/vobiz as a boundary; Kamailio + FreeSWITCH remain either way
3 OSS-over-custom: neutral — the serializer is OSS, but the provider abstraction we'd lose has no
                   OSS counterpart and we would rewrite it
4 Scalability:     parity
Non-negotiables:   FAILS — the sanctioned production path for self-hosting is a hosted transport
                   [E2, #3987 closure]; Kamailio dialplan ownership (scripts/kamailio/kamailio.cfg.tpl)
                   and Redis-only hot-path routing (CURSOR.md) have no Pipecat expression
Revisit when:      Pipecat ships a first-party SIP/RTP transport terminating a carrier trunk with no
                   hosted media service in the path.
```

### §4.2 VAD & turn detection

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| `libs/vad_sdk/silero_vad.py:SileroVAD` | Silero on PCM16@16k, 32ms/1024-byte fixed window; a port of the Gateway's own default VAD so a bridge call behaves like a Gateway call | `audio/vad/silero.py` — same model | **MATCH** | E1 |
| `libs/vad_sdk/vad.py:EnergyVAD` | amplitude threshold; retained, superseded in the bridge after real line noise/echo reached −13 dB against its −35 dB cutoff (`services/vobiz/bridge.py` module docstring) | no energy-only analyzer | **GAP (ours, deliberately unused on the live path)** | E1 |
| VAD-parity-with-Gateway requirement | the bridge's VAD must behave like the C++ Gateway's so both paths agree on "caller stopped talking" | **no expression** — adopting Pipecat's VAD would introduce a *second*, differently-tuned turn boundary alongside the Gateway's | **GAP** | E1 |
| Semantic end-of-turn | none — silence-based only | `audio/turn/smart_turn/` (local CoreML, local v2/v3, HTTP) + `krisp_viva_turn.py` | **PIPECAT-SUPERIOR** | E1 |
| Speculative turn start | none | `turns/speculation_gate.py`, `EagerEndOfTurnCancelFrame`, `eager_end_of_turn_mixin.py` | **PIPECAT-SUPERIOR** | E1 |

This row group is exactly what AC2 forbids resolving with "Pipecat has VAD built in." It does — the *same model* —
but the capability that matters here is not "is there a VAD," it is "does the bridge's turn boundary agree with the
Gateway's." Pipecat has no notion of that constraint. Conversely, smart-turn and the speculation gate are genuinely
ahead of anything we have.

```
Recommendation — VAD & turn detection
Verdict: Partial adopt (`audio/turn/smart_turn/` as a candidate *model*, not the Pipecat runtime)
1 Latency:         unknown — semantic end-of-turn plausibly cuts the silence-timeout wait, but
                   direction and magnitude are unmeasured [Appendix C.2]
2 Service count:   unchanged
3 OSS-over-custom: favors adopt — the smart-turn models are open and separable from the framework
4 Scalability:     parity — local smart-turn adds per-session inference cost, unquantified
Non-negotiables:   pass, *if* adopted as a model behind libs/vad_sdk's existing interface so the
                   Gateway/bridge turn-boundary parity requirement stays enforceable in one place
Revisit when:      a bounded spike measures smart-turn's added inference cost per turn against the
                   silence timeout it removes.
```

### §4.3 STT

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| `ai_provider_manager.py` + `providers/` STT | per-tenant provider selection, credential resolution via `secret_resolver.py` | ~25 STT service classes (`deepgram/`, `assemblyai/`, `gladia/`, `soniox/`, `speechmatics/`, `azure/`, `aws/`, `google/`, `elevenlabs/`, `sarvam/`, …) | **PIPECAT-SUPERIOR (breadth)** | E1 |
| provider credentials from per-tenant runtime config | `libs/config_sdk` `IConfigProvider.get_provider_config` + `secret_resolver.py` | none — services take API keys as constructor args | **GAP** (see §5B) | E1 |
| STT failure → degrade, not drop the call | inside our provider layer | `observers/error_observer.py`; per-service, varies | **PARTIAL** | E1 |

### §4.4 LLM orchestration & streaming

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| `pipeline.py:_token_stream` | token stream from the LLM | `LLMService` + `LLMFullResponseStart/End` frames | **MATCH** | E1 |
| `pipeline.py:_llm_to_tts` | the seam where tokens become sentences become audio | `aggregators/sentence.py` + `TTSService` | **MATCH** | E1 |
| `_build_current_date_context` (`_DATE_LOOKUP_DAYS`), `_build_caller_number_context` | per-turn injected system context | no first-class seam; a custom `FrameProcessor` | **PARTIAL** | E1 |
| `_END_CALL_MARKER` + `_END_CALL_INSTRUCTION` | LLM signals hang-up in-band | `extensions/voicemail/`, `EndFrame`; no in-band marker protocol | **PARTIAL** | E1 |
| `_claims_booking_without_tool_call` / `_CALENDAR_MUTATION_TOOLS` / `_claim_matches_confirmed_slot` | detects the model *claiming* a booking it never made, and escalates | **none** | **GAP** | E1 |
| `_extract_spoken_digits` / `_caller_just_confirmed_phone_number` / `_message_reads_back_phone_number` | phone-number readback confirmation in spoken digits | **none** | **GAP** | E1 |
| `tools/date_sanity.py`, `orchestrator._check_requested_date` | cross-checks a model-computed date | **none** | **GAP** | E1 |
| provider-switching mid-session | `provider_bundle.py` | `pipeline/llm_switcher.py` | **MATCH** | E1 |
| context summarization | none | `aggregators/llm_context_summarizer.py` | **PIPECAT-SUPERIOR** | E1 |

The four `GAP` rows here are the product. They are anti-hallucination guardrails discovered by running real calls,
and no framework ships them because they are domain logic. They would be ported as custom `FrameProcessor`s —
possible, but it is porting, not deleting.

### §4.5 Directive parsing mid-stream

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| `directives.py:StreamBuffer.feed/flush` | holds back a partial `<tag …>` across token-chunk boundaries so a half-arrived directive is never spoken | **none** — no in-band-markup protocol at all | **GAP** | E1 |
| `directives.py:DirectiveParser.parse` | `TransferDirective` / `EndCallDirective` / `UnknownDirective` with attribute parsing; unknown directives are *seen*, not crashed on | none | **GAP** | E1 |
| `strip_markdown_chars` | strips `*_\`#` before TTS | `processors/text_transformer.py` (generic) | **PARTIAL** | E1 |

Pipecat's design intent is that the model calls a *function* rather than emitting in-band markup, so this whole
mechanism has no counterpart. That is a legitimate design difference, not a deficiency — but it is ~177 lines of
carefully-ordered logic that a migration would have to re-implement as a processor, and the "partial tag across a
chunk boundary" bug it exists to prevent is subtle enough that re-deriving it under a new framework is real risk.

### §4.6 Tool-call orchestration

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| `tools/orchestrator.py:ToolCallOrchestrator.run_turn` / `_execute_tool_call` | tool resolution, execution, folding results into history | `LLMService` function-call registration + `aggregators/llm_context.py` | **MATCH** | E1 |
| background / non-blocking tool tasks | in-house | `cancel_on_interruption=False` + `async_tool_messages.py` `started`/`intermediate`/`final` protocol | **PIPECAT-SUPERIOR** | E1 |
| cancellation telling the model a tool did *not* finish | in-house | explicit: a cancelled async tool always writes a `final` message carrying a cancellation notice | **PIPECAT-SUPERIOR** | E1 |
| `tools/policy_resolver.py` — per-tenant/agent tool policy | 189 lines of 3-tier resolution | **none** | **GAP** | E1 |
| `tools/registry.py`, `executor_registry.py`, `middleware.py` | tool registry + middleware chain | registration decorators; no middleware chain | **PARTIAL** | E1 |
| `tool_latency.py` — calibrated per-(tenant, agent, tool) average | feeds filler sizing | **none** | **GAP** | E1 |
| per-tool timeout | in `middleware.py` | exists, and is currently **buggy** — open issue #5481, "Per-tool `timeout_secs` is disarmed by an intermediate (`is_final=False`) result callback — hung async tool" [E2] | **PARTIAL** | E2 |

### §4.7 TTS & sentence streaming

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| `_synthesize_sentence_stream` | "the one shared boundary every text source reaches TTS through" (its own docstring) | `TTSService` + `aggregators/sentence.py` | **MATCH** | E1 |
| `fillers.py:FillerSelector.select_tool_filler` | picks a filler *sized to that tool's calibrated latency*, never repeats the last phrase, never raises | `TTSSpeakFrame` exists; **no latency-sized selection, no anti-repeat, no total-function guarantee** | **GAP** | E1 |
| filler must overlap a tool call, not block it | fixed in-house this cycle | `cancel_on_interruption=False` is the right primitive, and Pipecat has it — but see open issue #5263, "Flows: interrupting during a `tts_say` or function action leaves the ongoing actions count stuck" [E2] | **PARTIAL** | E2 |
| TTS breadth | our provider set | ~30 TTS services in-tree | **PIPECAT-SUPERIOR** | E1 |

### §4.8 Barge-in, cancellation, playback pacing — **the reliability question**

This is the row group the whole re-evaluation turns on, so it gets the longest treatment. See also §11.

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| `bridge.py:_playback_pacer` (`_PLAYBACK_LEAD_S = 0.1`) | drains a 20 ms-frame queue at real time so there is always something left for `clearAudio` to clear | `base_output.py` writes `audio_out_10ms_chunks` at a time, on a dedicated `_clock_task`, with the comment "This helps with interruption handling" | **MATCH** — same insight, independently reached | E1 |
| `bridge.py:_audio_delay_pump` (`_AUDIO_DELAY_S = 0.15`) | delays the first write so a pre-roll exists (first-word clipping) | **no equivalent found.** `VADParams.start_secs` delays *confirming* speech start; it does not buffer a pre-roll for playback | **GAP** | E1 |
| `_clear_playback_queue` / `_flush_pending_audio_now` | drop queued audio on barge-in; force-write pending on final | `sender.handle_interruptions()` drains the audio queue, preserving `UninterruptibleFrame`s (`base_output.py:572-588`) | **PIPECAT-SUPERIOR** — the uninterruptible carve-out is finer-grained than our all-or-nothing drop | E1 |
| early barge-in (cancel while LLM/tool in flight, before any audio) | `_turn_active` set from `speech_ended` until the turn resolves; VAD speech-start while true always sends `CancelGeneration` | `broadcast_interruption()` sends `InterruptionFrame` **upstream and downstream**; every `FrameProcessor` cancels its own in-flight task — so LLM, TTS and transport all cancel with no central coordinator | **PIPECAT-SUPERIOR (architecturally)** | E1 |
| cancel state must not leak across turns | `_start_turn`/`_resolve_turn` + `_turn_watchdog_fire` (`_TURN_WATCHDOG_S = 12.0`) as a stuck-turn backstop | per-processor `_cancelling` flag + `__cancel_process_task`; **no watchdog** | **PARTIAL** | E1 |
| `_FINAL_MARKER` ordering — pending audio flushed before the turn is declared done | in-house | `EndFrame` waits on `_audio_task` and `_clock_task` (`base_output.py:531-540`) | **MATCH** | E1 |
| turn-level backstop for a stage that hangs | `_turn_watchdog_fire` | `idle_frame_processor.py`, `turns/user_idle_controller.py` — idle-*user* detection, not stuck-*turn* detection | **PARTIAL** | E1 |

**Does Pipecat have a materially different answer to the turn-race problem class? Yes — architecturally better,
and empirically not fixed.**

Better, on three counts, all `[E1 @ pipecat-ai/pipecat@f236a08]`:

1. **Interruption is a broadcast frame, not a message to one coordinator.** `broadcast_interruption()` pushes
   `InterruptionFrame` upstream *and* downstream; each `FrameProcessor` independently cancels its own in-flight
   work. Our design routes cancellation through the bridge's `_turn_active` flag and a `CancelGeneration` gRPC
   message to one handler. The broadcast model has strictly fewer places where a stage can miss the cancel.
2. **`UninterruptibleFrame` is a first-class type.** The "a filler must overlap a tool call, not block it" and
   "this audio must finish playing even though the user just spoke" problems have a *vocabulary* in Pipecat. We
   express the same thing with ad-hoc booleans. A type the framework enforces beats a flag each site must remember.
3. **Async tools have a defined settlement protocol.** A cancelled tool always writes a `final` context message.
   The failure mode of "the model never learns the tool was abandoned" is designed out.

Empirically not fixed, and this is the part that should stop a reliability-motivated migration
`[E2, pipecat-ai/pipecat issue tracker, read 2026-09-15]`. A GitHub search for open issues in
`pipecat-ai/pipecat` matching interruption/barge-in returns **32 open**. The recent ones are the *same bug class*
this team has been fixing, in Pipecat's own code:

- **#5683** (2026-09-10) — "TranscriptionFrame queued behind the frame that starts a user turn is dropped by that
  turn's own interruption." A turn-start/transcription ordering race that loses caller speech.
- **#5263** (2026-08-09) — "Flows: interrupting during a `tts_say` or function action leaves the ongoing actions
  count stuck." Cancel state leaking across turns — the same defect this team fixed this cycle.
- **#5611** (2026-09-03) — "TTS usage metrics are lost for `TTSSpeakFrame` text in TOKEN-streaming mode:
  `InterruptionFrame` discards…"
- **#5774** (2026-09-15, filed the day of this read) — "Queued Telnyx/WebSocket transfer support
  (`DailySIPTransferFrame` equivalent) and in-flight uninterrupt…" — i.e. transfer-during-playback on a WebSocket
  telephony transport is an *open feature request*, not a shipped capability.
- **#5425** (2026-08-25) — "User-input muting + Deepgram Flux STT: overlapping speech during muted greeting strands
  the turn."
- **#5769** (2026-09-15) — "Audio skipping/dropping mid-word due to `CLEAR_STREAM_AFTER_SECS` in
  `soxr_stream_resampler`."
- **#5481** (2026-08-28) — per-tool timeout disarmed by an intermediate result → hung async tool.

And `frame_processor.py:1144-1146` carries a fixed-bug comment of precisely this shape: the interruption path "was
skipped when the queue contained an uninterruptible frame, which caused slow non-uninterruptible frames to block
interruptions" [E1]. That is a barge-in-blocked-by-a-queued-frame bug, found and fixed *inside Pipecat* recently.

**The honest reading: these races are intrinsic to streaming voice pipelines, not to our implementation of one.**
A framework with a better vocabulary for them still ships them. Migrating would trade a set of bugs we have found,
reproduced on real SIP calls and fixed, for a set we have not — in code we do not own, on a release cadence we do
not control. Under criterion 1 (latency) that is neutral; under the *unstated* criterion that actually motivated
this re-evaluation — fewer reliability defects — it is a **negative**, not a positive.

```
Recommendation — barge-in / cancellation / playback pacing
Verdict: Reject
1 Latency:         unknown — media path, unmeasured [Appendix C.1]
2 Service count:   unchanged
3 OSS-over-custom: favors adopt on architecture (broadcast interruption, UninterruptibleFrame),
                   favors keep on evidence (32 open interruption issues upstream, several of the
                   same class we just fixed) — net neutral
4 Scalability:     parity
Non-negotiables:   FAILS — the playback-pacing / final-marker / pre-roll / early-barge-in invariants
                   in services/vobiz/bridge.py are preserved only by re-deriving them in a new frame
                   model; _audio_delay_pump's pre-roll in particular has no Pipecat equivalent
Revisit when:      Pipecat's open interruption-class issue count falls materially AND a spike
                   demonstrates the four bridge.py invariants hold under its frame model on a real
                   SIP call. Adopt the *ideas* now regardless: see §11.
```

### §4.9 Transfer trigger detection & execution

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| `transfer_engine.py:TransferDecisionEngine.evaluate` | one decision point over 4 `TriggerType`s (LLM_DIRECTIVE / ESCALATION / WORKFLOW / EXTERNAL) → `Decision`, with named rejection reasons incl. `already_transferring` | **none** | **GAP** | E1 |
| `DecisionContext` — caller-ID policy (`original`/`platform`/`custom`), `waiting_experience` | per-tenant transfer presentation | none | **GAP** | E1 |
| `TransferReason` → `wire_trigger` | stable wire vocabulary for downstream telephony | none | **GAP** | E1 |
| `pipeline.py:on_transfer_failed` → announcement → voicemail fallback; `_TRANSFER_FLUSH_TIMEOUT_S` | failed cold transfer degrades gracefully | none | **GAP** | E1 |
| `transfer_destination_problem` / `_SIP_URI_RE` | validates the destination before attempting | none | **GAP** | E1 |
| executing the transfer on the carrier | our telephony path | `DailySIPTransferFrame` exists for Daily; for WebSocket telephony transports it is **open issue #5774** | **PARTIAL** | E1/E2 |

Dograh's own answer here is instructive: it had to write `CloudonixConferenceStrategy` because "Cloudonix has no
live-CXML push equivalent to Twilio's call-update" [E1 @ dograh-hq/dograh@23d22b9,
`api/services/telephony/providers/cloudonix/strategies.py`], on top of a `HangupStrategy`/`TransferStrategy`
abstraction it added to **its fork of Pipecat**, not to upstream (§7a). Transfer is per-carrier bespoke work no
matter which framework is underneath.

### §4.10 Guardrail counting & escalation

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| `guardrails.py:GuardrailDetector.check` → `GuardrailViolation` | detection | `processors/filters/` (generic), `wake_check_filter.py` | **PARTIAL** | E1 |
| `guardrails.py:GuardrailCounter` (per-session increment/reset/current) + `pipeline.py:_evaluate_escalation` → `TransferRequest` | **count-based escalation to a human** | **none** | **GAP** | E1 |
| `record_booking_fabrication` → `_BOOKING_FABRICATION_TRANSFER_ANNOUNCEMENT` | a specific fabrication class escalates with its own announcement | none | **GAP** | E1 |

### §4.11 FSM / session lifecycle

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| `fsm.py:ConversationFSM` — explicit `CallFsmState` incl. a distinct `BARGE_IN` state, with illegal transitions rejected | one auditable place that says what state a call is in | **none** — Pipecat state is distributed across processors; `observers/turn_tracking_observer.py` reports, it does not constrain | **GAP** | E1 |
| `ConversationFsmHandlers` callback surface | `on_speech_started(energy_db)`, `on_stt_final(text, confidence)`, `on_playback_finished(interrupted)`, `on_transfer_requested/completed`, `on_session_close` | `base_observer.py` + `observers/` (speaking, turn-tracking, latency, errors, function-calls) | **PARTIAL** | E1 |
| `session.py` / `session_finalizer.py` | session lifetime + finalization | `PipelineTask` lifecycle, `EndFrame`/`CancelFrame` | **PARTIAL** | E1 |

A framework whose state is emergent across processors is harder to *assert* about than an explicit FSM. For a
system whose stated pain is timing races, giving up the FSM is a real cost.

### §4.12 Transcript recording & finalization

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| `transcript_builder.py` (379) | builds the durable transcript | `observers/loggers/transcription_log_observer.py` — a **logger**, not a durable transcript store; no `TranscriptProcessor` exists in-tree at this SHA | **PARTIAL** | E1 |
| `session_finalizer.py` (412) | end-of-call persistence, `start_finalization`/`finalize_session` | none | **GAP** | E1 |
| `workflow/extractor.py` → `_finalize_extraction` | structured variable extraction at call end | `flows/` has state; no extraction-at-finalization | **GAP** | E1 |

### §4.13 Fillers / prewarm

Covered in §4.7. One additional row:

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| prewarm / cold-start on first greeting | provider prewarm at session start (`provider_config_subscriber.py`, `__main__.py` env-driven auth) | cold-start on first greeting is a reported Pipecat complaint the PRD cites; **not independently confirmed and not reproducible from a source read** | **PARTIAL, unverified** | E3 |

### §4.14 Workflow runtime

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| `services/conversation/workflow/runner.py` + `extractor.py` + `libs/config_sdk/workflow.py` | shipped node-graph workflow runtime with variable extraction | `src/pipecat/flows/` — in-tree at 1.10, with `flow_config.schema.json` and YAML flow definitions | **MATCH, Pipecat broader** | E1 |

### §4.15 Rows explicitly DROPPED

Per lessons #7, these are listed rather than silently omitted:

- `services/conversation/echo.py` (102) — **DROPPED**: test/diagnostic handler, no product capability to compare.
- `services/conversation/metrics.py` (48), `event_bus.py` (273) — **DROPPED**: internal plumbing; Pipecat's
  `observers/` and `metrics/` are a rough counterpart but nothing product-visible turns on the comparison.
- `services/conversation/agent_config.py` / `agent_resolver.py` / `pipeline_config.py` / `provider_bundle.py` /
  `secret_resolver.py` — **DROPPED as separate rows**: folded into §5B (runtime config), which is where the
  decision-relevant gap lives.
- `services/conversation/servicer.py` (779) — **DROPPED**: the gRPC service surface is a boundary contract, not a
  pipeline capability; it is addressed as a constraint in §6.2 and §9.
- `libs/config_sdk/secrets.py` — **DROPPED**: secret storage is out of scope per PRD (no framework comparison).

---

## §5A Speech-to-speech (its own section, per the revised scope)

This section is deliberately **not** folded into §4.3/§4.4. Adopting Pipecat *for S2S* is a different decision from
adopting it for cascaded plumbing: S2S is greenfield here, so there is nothing to migrate, nothing to regress, and
no existing invariant to preserve. That changes every criterion's answer.

### Does Pipecat have working S2S support today? Yes — nine services, in-tree, actively developed.

All `[E1 @ pipecat-ai/pipecat@f236a08]`:

| Service | Module | Lines |
|---|---|---|
| `OpenAIRealtimeLLMService` | `services/openai/realtime/llm.py` (+ `events.py`) | 1385 (+1160) |
| `OpenAILiveLLMService` | `services/openai/live/llm.py` | — |
| `GeminiLiveLLMService` | `services/google/gemini_live/llm.py` (+ `stt.py`, `file_api.py`) | 2193 (+573+188) |
| `AWSNovaSonicLLMService` | `services/aws/nova_sonic/llm.py` (+ `session_continuation.py`) | 1811 (+706) |
| `AzureRealtimeLLMService` | `services/azure/realtime/llm.py` | — |
| `GrokRealtimeLLMService` | `services/xai/realtime/llm.py`, `services/grok/realtime/llm.py` | — |
| `InworldRealtimeLLMService` | `services/inworld/realtime/llm.py` | — |
| `UltravoxRealtimeLLMService` | `services/ultravox/llm.py` | — |

Plus matching LLM adapters (`adapters/services/open_ai_realtime_adapter.py`, `gemini_adapter.py`,
`aws_nova_sonic_adapter.py`, `grok_realtime_adapter.py`, `inworld_realtime_adapter.py`,
`open_ai_live_adapter.py`) [E1].

This is **frame/pipeline-level support, not component swapping**. Evidence that it is genuinely integrated rather
than bolted on:

- The async-tool protocol explicitly accounts for S2S: "Realtime LLM services use `parse_message` to detect
  async-tool messages while iterating the context, then read `payload.result` and deliver it via their formal
  tool-result channel" [E1, `async_tool_messages.py` docstring]. Tool calling works *through* the S2S services'
  own tool channel, not around it.
- `GeminiLiveLLMService.Settings` has a `turn_coverage` setting selecting "how much of the realtime input stream a
  user turn covers" [E1, `CHANGELOG.md`] — i.e. the turn-taking model is reconciled with server-side VAD.
- There are "locally-driven-turns" variants for OpenAI, Gemini Live, Grok and Inworld
  (`realtime-*-locally-driven-turns.py`) [E1] — you can keep *our* turn-boundary logic and drive the S2S session
  from it, rather than surrendering turn control to the vendor. For a platform whose turn boundary must agree with
  the Gateway's (§4.2), that is the load-bearing feature.
- Metrics know about it: "Speech-to-speech services report nothing" for the TTS metric path [E1, `CHANGELOG.md`].

### How mature is it?

**Development activity, `[E1 @ pipecat-ai/pipecat@f236a08]`:** ~30 files under `examples/realtime/` covering
OpenAI Realtime, OpenAI Live (incl. three client-delegation variants), Gemini Live (Vertex, video, files API,
Google Search grounding, grounding metadata, graceful end, locally-driven turns), Nova Sonic, Azure, Grok, Inworld
and Ultravox — each with an `-async-tool` variant for six of them. The CHANGELOG carries continuous S2S work
through the 1.x line: `OpenAILiveLLMService` added, Gemini Live `turn_coverage` added, Grok Realtime default voice
and model changes, Azure Realtime v1 surface and `token_provider`, Nova Sonic region corrections.

**Defect load, `[E2, GitHub API, read 2026-09-15]`:** open issues with `realtime`/`gemini_live`/`NovaSonic` **in
the title**: **3**. They are #4403 (Gemini Live `send_client_content(turn_complete=True)` races
`realtime_input` → 1007 on first audio user-stop), #3994 (`tool_choice` unsupported by the Google Realtime API —
a vendor limitation, not a Pipecat one), #3170 (OpenAI Realtime sessions >60 min). Broadening to any open issue
mentioning "realtime" gives 18, and "gemini live" 11 — most of those are STT-service issues, not S2S. Against 326
total open issues and 15.5k stars, an S2S defect load of ~3 titled issues is low.

**Production references:** *unverified.* Counting "how many teams reference using it in production" was not
achievable from available sources without an authenticated code search, and vendor testimonials are `E3` by
construction. The one production data point that *is* `E1` is Dograh: it ships eleven S2S modules of its own —
`api/services/pipecat/realtime/{openai_realtime,openai_live,gemini_live,gemini_live_vertex,aws_nova_sonic,
azure_realtime,grok_realtime,ultravox_realtime,conversation,static_greeting}.py` [E1 @ dograh-hq/dograh@23d22b9].
A shipping product built an S2S layer on these services. Read it two ways, and both are true: the services are
usable in production, *and* they still needed ~11 modules of integration on top.

### Ledger

| Capability | Today | Pipecat | Verdict | Tier |
|---|---|---|---|---|
| S2S session (audio-in → audio-out, one model) | **none — cascaded STT→LLM→TTS only** | 9 services, frame-level | **GAP (ours)** | E1 |
| tool calling during an S2S session | n/a | async-tool protocol routed through each service's native tool channel | **GAP (ours)** | E1 |
| keeping *our* turn boundary while using S2S | n/a | `realtime-*-locally-driven-turns.py` for 4 of the services | **GAP (ours)** | E1 |
| barge-in during S2S | n/a | `InterruptionFrame` is transport/service-agnostic; the S2S services participate in the same interruption broadcast | **GAP (ours)**, unverified behaviourally | E1 |
| per-tenant provider/credential injection for S2S | `libs/config_sdk` would apply | constructor args only — same gap as §5B | **GAP (Pipecat)** | E1 |
| our transfer / guardrail / directive / RAG layers over an S2S session | designed for a text token stream | would need re-expression: an S2S model emits audio, so `StreamBuffer`/`DirectiveParser` (§4.5) have no token stream to parse | **GAP — open design question** | E1 |

That last row is the one thing to be clear-eyed about: **S2S is not a drop-in behind our existing pipeline.** The
directive protocol, the booking-fabrication check, the phone-digit readback check and the guardrail counter all
read the assistant's *text*. S2S services do surface transcripts, but the in-band-marker design (§4.5) does not
transfer. Adopting Pipecat for S2S therefore means building a second pipeline whose product-logic layer is
rebuilt, not shared — which is an argument for scoping S2S to use cases where those guardrails are less critical
first, not an argument against the framework choice.

```
Recommendation — speech-to-speech
Verdict: Partial adopt (Pipecat's realtime LLM services + frame/interruption model, as a second,
         parallel pipeline; explicitly NOT its transport, and NOT a migration of the cascaded path)
1 Latency:         better — removing the STT and TTS network round-trips per turn is an
                   architectural reduction, not a tuning claim. Magnitude unverified [E2/E3];
                   Appendix C.3 names the benchmark. Criterion-1 note: this stage is on the media
                   path and latency is formally `unknown`, but the direction is structural rather
                   than measured — this is the one place the design's stricter bar is relaxed, and
                   it is recorded here rather than buried (see §9's self-check).
2 Service count:   unchanged — but the counterfactual is building an S2S runtime from zero, which
                   is the largest single piece of net-new work this platform would otherwise face
3 OSS-over-custom: strongly favors adopt — 9 services, ~30 examples, continuous 1.x development
                   [E1 @ pipecat-ai/pipecat@f236a08]; nothing in-house to displace
4 Scalability:     parity — an S2S session is one long-lived vendor WebSocket under either design;
                   concurrency is bounded by the vendor's session limits, not by the framework
Non-negotiables:   pass — greenfield. No change to today's media path, Gateway boundary, Redis-only
                   hot-path routing (CURSOR.md), or Kamailio dialplan (scripts/kamailio/kamailio.cfg.tpl).
                   Bound the adoption so it stays true: terminate the trunk on our own stack and feed
                   the S2S pipeline through our existing bridge, exactly as Dograh feeds Pipecat from
                   its own WebSocket handshake [E1 @ dograh-hq/dograh@23d22b9].
Revisit when:      n/a — this is the adopt case. But re-scope if the product-logic gap in the last
                   ledger row (directives/guardrails over an audio-out model) proves larger than a
                   first S2S use case can absorb.
```

---

## §5B Extension points: per-tenant RAG and runtime config (AC3)

**RAG.** We make exactly one `_retrieve_context` call per turn (`pipeline.py:1421`), through
`IKnowledgeProvider.retrieve(tenant_slug, agent_slug, query, RetrievalPolicy())`, and a retrieval failure degrades
to no-context rather than failing the turn — the contract is stated in the method's own comment and guaranteed
again inside the cache-aside provider [E1 @ this repo@9646aec].

Pipecat has **no retrieval abstraction**. What exists is three examples — `examples/rag/rag-gemini.py`,
`rag-gemini-grounding-metadata.py`, `rag-mem0.py` [E1 @ pipecat-ai/pipecat@f236a08] — i.e. patterns, not a seam.
There is no `IKnowledgeProvider` equivalent, no per-tenant retrieval-policy concept, and nothing that expresses a
tiered policy override chain. **Verdict: GAP.**

What would have to be built: a custom `FrameProcessor` sitting between the user-context aggregator and the LLM
service, which (a) resolves the tenant/agent retrieval policy, (b) issues the one retrieval call, (c) formats and
injects context the way `_format_context` does, and (d) swallows retrieval failures. Its lifecycle is per-pipeline
(so per-session), which means it must be constructed with the session's tenant/agent identity — pushing the
problem into the runtime-config gap below.

**Runtime config.** `libs/config_sdk` gives an immutable `RuntimeConfig` resolved once at session start via
`IConfigProvider`, with `provider_config_subscriber.py` handling invalidation, and `config_version` plumbed so a
session's configuration is a fixed, identifiable snapshot for its whole lifetime [E1 @ this repo@9646aec].

Pipecat has **no runtime-config or multi-tenancy concept at all**. Its services take credentials and settings as
constructor arguments; a `Pipeline` is assembled in code per bot. There is no notion of "resolve this tenant's
providers, prompt, voice and tools, then build the pipeline." **Verdict: GAP.**

What would have to be built: a per-session pipeline factory that reads `RuntimeConfig` and constructs the
`Pipeline` — which is roughly what Dograh's `api/services/pipecat/pipeline_builder.py` + `service_factory.py` +
`run_pipeline.py` do, and a large part of why that directory is 10,176 lines (§7c). **Who owns its lifecycle** is
the pointed question: today `libs/config_sdk` owns it behind one interface with one dict→dataclass mapping
boundary; under Pipecat it becomes bespoke factory code per pipeline shape, and there would be two such shapes if
S2S is adopted (§5A).

```
Recommendation — extension points (RAG + runtime config)
Verdict: Reject (as a replacement for libs/knowledge_sdk / libs/config_sdk)
1 Latency:         unknown — a retrieval FrameProcessor sits on the turn path
2 Service count:   unchanged — and it would convert two shared SDKs into per-pipeline factory code
3 OSS-over-custom: favors keep — there is no OSS counterpart to adopt here; this is the
                   "what would have to be built" column, not the "what we would delete" column
4 Scalability:     parity
Non-negotiables:   pass, but narrowly — a per-session pipeline factory must not reintroduce a
                   synchronous Config-Service or Postgres call on the media path (CURSOR.md);
                   RuntimeConfig must still be resolved once at session start, never per turn
Revisit when:      Pipecat gains a first-class per-session configuration/tenancy seam, or a
                   retrieval abstraction rather than three examples.
```

---

## §6 Does Pipecat already replace a whole service?

### §6.1 `services/campaigns`

What the service actually is: `originate.py` issues a raw ESL `bgapi originate {origination_caller_id_number=…}
sofia/external/sip:…@… &bridge(…)` and reads the outcome off a **persistent `BACKGROUND_JOB` event subscription**
via `EslJobEventListener` — the bgapi reply and the job-completion event are deliberately separate paths, verified
against a real local FreeSWITCH per the module's own docstring. `worker.py`'s `CampaignWorker` adds pacing
(`pacing_seconds`), a per-campaign concurrency cap, a calling-hours window (`_within_calling_hours`), contact
claiming with retry (`_resolve_with_retry`), and the rule that a contact that never reached a dial attempt does not
cost a pacing slot. Then `dnc.py`, `campaign_contacts.py`, `audit.py` [E1 @ this repo@9646aec].

Pipecat's counterpart: dial-out *examples* using a transport's own outbound capability, and `pipecat-flows` (now
`src/pipecat/flows/`) for conversation branching [E1 @ pipecat-ai/pipecat@f236a08]. **There is no campaign
concept, no DNC list, no contact list, no pacing loop, no calling-hours window, and no audit trail anywhere in
the repo.** A dial-out example places one call.

```
Recommendation — services/campaigns
Verdict: Reject (keep the service)
1 Latency:         parity — not a media-path component; irrelevant either way
2 Service count:   unchanged — Pipecat covers none of DNC, contact lists, pacing, calling hours or
                   audit, so nothing retires
3 OSS-over-custom: neutral — there is no OSS campaign runner here to prefer
4 Scalability:     parity
Non-negotiables:   pass — but note the originate path is ESL-native to our own FreeSWITCH; a
                   Pipecat transport's dial-out would place the call through a hosted provider
                   instead, which is the §4.1 non-negotiable again
Revisit when:      Pipecat (or pipecat-flows) ships an outbound-campaign runner with DNC and pacing
                   as first-class concepts. There is no sign of one at 1.10.
```

### §6.2 `services/webcall`

What the service actually is: a raw-PCM16 WebSocket → gRPC bridge with an explicitly **push-to-talk** client
protocol — the browser sends `speech_ended` / `cancel` / `playback_finished` control messages and no VAD runs —
plus `ResponseWatchdog` (arm on `speech_ended`, disarm on any `ServiceMessage`, fire otherwise), a per-utterance
WAV dump for debugging, and a session-token + tenant authorization check against the Config Service [E1 @ this
repo@9646aec].

Pipecat's counterpart: `SmallWebRTCTransport` (`transports/smallwebrtc/`) plus `transports/websocket/`, and client
SDKs via the RTVI framework (`processors/frameworks/rtvi/`, `serializers/rtvi_client.py`) [E1 @
pipecat-ai/pipecat@f236a08]. That genuinely covers the browser-media half — and covers it better, since WebRTC
handles jitter and echo that a raw-PCM WebSocket does not.

But **retiring the service requires retiring the Conversation gRPC contract too.** webcall exists to translate a
browser into the same gRPC `GatewayMessage`/`ServiceMessage` stream the C++ Gateway speaks, so that a test call
exercises *the real conversation service*. A Pipecat browser transport would speak to a Pipecat pipeline, not to
`services/conversation` — so adopting it replaces the thing being tested, which defeats the purpose of a test-call
bridge. The service count only drops if the whole conversation runtime moves, which §1(a) rejects.

```
Recommendation — services/webcall
Verdict: Reject for now (keep the service), revisit
1 Latency:         unknown — and not decisive; this is a test/dev path, not a customer media path
2 Service count:   unchanged — the service retires only if the Conversation gRPC contract retires
                   with it, which is the §1(a) decision, not this one
3 OSS-over-custom: favors adopt on the browser-media half specifically (SmallWebRTC + RTVI client
                   SDKs are better than a hand-rolled push-to-talk PCM protocol)
4 Scalability:     parity — dev-path concurrency is not a constraint
Non-negotiables:   pass — nothing here touches the production media path
Revisit when:      (a) §1(a) is revisited and the conversation runtime moves, or (b) the browser
                   test-call path is rebuilt for its own reasons — in which case take SmallWebRTC +
                   RTVI rather than extending the push-to-talk protocol.
```

---

## §7 Dograh reference case

`dograh-hq/dograh@23d22b95defbd0a23719a7f9b265eb9f7acdebf8`, read 2026-09-15.

### (a) Fork or dependency? **Fork, vendored as a pinned submodule, and deeply diverged.**

`.gitmodules` reads `[submodule "pipecat"] path = pipecat, url = https://github.com/dograh-hq/pipecat.git`, and
`git ls-tree HEAD pipecat` gives the pin: **`70385ad8a5204458a241e60287289932e3eb3ae2`** [E1 @
dograh-hq/dograh@23d22b9].

GitHub's compare API for `pipecat-ai/pipecat@main` vs that pin: **status `diverged`, 105 commits ahead, 805 commits
behind, 300 files changed** [E1, GitHub compare API, 2026-09-15]. Within `src/pipecat/` alone, **139 files
changed**. The PRD asked whether the divergence is telephony-only. **It is not**, and the bounded file-list check
answers it clearly:

- **Telephony**: added `serializers/cloudonix.py`, `serializers/vobiz.py`, `serializers/asterisk.py`,
  `serializers/call_strategies.py`, `serializers/rtvi_client.py`; modified `serializers/{twilio,telnyx,plivo,
  vonage,base_serializer}.py`.
- **Their own service stack**: added an entire `src/pipecat/services/dograh/` package —
  `llm.py`, `stt.py`, `tts.py`, `flux/stt.py`, `mps_billing.py`. A billing module inside the framework fork.
- **Core pipeline internals**: modified `processors/frame_processor.py`, `frames/frames.py`,
  `processors/aggregators/{llm_context,llm_response_universal,async_tool_messages,sentence}.py`,
  `pipeline/{worker,worker_observer,llm_switcher}.py`, `metrics/metrics.py`.
- **Observers**: added `error_observer.py`, `function_call_observer.py`, `service_metrics_observer.py`,
  `speaking_observer.py`.
- **Flows**: added `flows/config.py`, `flows/flow.py`, `flows/flow_config.schema.json`, modified `flows/manager.py`.
- **Evals**: `evals/harness.py` `+31/−1610` — they rewrote the eval harness — plus ~15 new eval modules.
- Also `audio/mixers/silence_mixer.py`, `adapters/services/open_ai_live_adapter.py`,
  `adapters/services/deepseek_adapter.py`, and modifications across ~30 service modules.

805 commits behind is the number to sit with. Their `.agents/skills/merge-pipecat-upstream/SKILL.md` [E1] is a
documented, agent-assisted procedure for merging upstream — i.e. keeping the fork current is a standing,
tooled-for cost, not a one-time event.

### (b) Which Pipecat components does Dograh's integration use?

From `api/services/telephony/providers/cloudonix/transport.py` and `api/services/pipecat/` [E1]:
`FastAPIWebsocketTransport` + `FastAPIWebsocketParams` (`audio_in_enabled`, `audio_out_enabled`, per-direction
sample rates, `audio_out_mixer`, `serializer`, `audio_out_10ms_chunks=2`), the `serializers.*FrameSerializer`
family, `serializers.call_strategies.{HangupStrategy,TransferStrategy}`, the `flows/` package, and the realtime
LLM services (eleven wrappers under `api/services/pipecat/realtime/`).

### (c) What did Dograh build on top of, or instead of, Pipecat?

Measured, `[E1 @ dograh-hq/dograh@23d22b9]`:

- `api/services/pipecat/` — **10,176 lines** of integration glue across ~35 modules: `run_pipeline.py`,
  `pipeline_builder.py`, `service_factory.py`, `transport_setup.py`, `transport_params.py`, `audio_config.py`,
  `audio_mixer.py`, `audio_playback.py`, `audio_file_cache.py`, `recording_router_processor.py`,
  `recording_audio_cache.py`, `termination_funnel_processor.py`, `pipeline_metrics_aggregator.py`,
  `transcript_log_coordinator.py`, `answer_classification.py`, `pre_call_fetch.py`, `turn_context.py`,
  `usage_metrics.py`, `worker_runner.py`, `ws_sender_registry.py`, `realtime_feedback_observer.py`,
  `processors/answer_supervisor.py`, the 11-module `realtime/` package, and more.
- `api/services/telephony/` — **17,907 lines**: its own `TelephonyProvider` base, a `registry.py` with
  `ProviderSpec`/`ProviderUIField`/`ProviderUIMetadata` (so provider config forms are generated, not hand-coded),
  `factory.py`, `ari_manager.py`, `call_transfer_manager.py`, `external_pbx.py`, `inbound_routing.py`,
  `outbound_readiness.py`, `status_processor.py`, `transfer_event_protocol.py`, `ws_auth.py`, `failure_reporting.py`,
  and eight provider packages: `twilio`, `telnyx`, `plivo`, `vonage`, `exotel`, `ari`, `cloudonix`, **`vobiz`**.
- Whole-repo `api/` is 162,315 lines of Python.

**On the "Vobiz" name.** The PRD flagged it as worth checking. It checks out: Dograh's
`api/services/telephony/providers/vobiz/provider.py` documents "Vobiz uses Plivo-compatible API and WebSocket
protocol" and takes `auth_id` / `auth_token` / `application_id` (whose `answer_url` `configure_inbound` updates)
[E1 @ dograh-hq/dograh@23d22b9]. Our `libs/telephony_sdk/providers/vobiz.py:VobizTelephonyProvider` requires
exactly `["auth_id", "auth_token"]`, calls `{base}/v1/Account/{auth_id}/Call/`, and passes `answer_url` /
`hangup_url` / `ring_url` [E1 @ this repo@9646aec]. **Same upstream carrier API**, integrated independently by
both. Not a coincidence, and not a shared codebase either — the naming similarity is the carrier's, not a
relationship between the projects.

### (d) The lesson that feeds §1

**A team that set out to build a product on Pipecat could not consume it as a dependency, and the divergence is
not confined to the parts Pipecat says are pluggable.** Serializers are the documented extension point — and
Dograh did add serializers there — but it also modified `frame_processor.py` and `frames.py`, rewrote the eval
harness, and put its own billing module inside the framework. It is 805 commits behind upstream with a documented
merge procedure to manage the drift.

That directly tempers the criterion-3 (OSS-over-custom) argument, which is otherwise the strongest case for
adoption. Adopting Pipecat as a plain dependency is the version of this decision where criterion 3 wins. The only
real-world production data point available says that version may not be the one on offer: what you actually take on
is a fork, plus a perpetual merge obligation, plus ~10k lines of glue to build a pipeline per session.

It is also **supporting evidence for the PRD's "production telephony integration is left to the integrator"
finding, and the strongest such evidence available** — see §8, where the Cloudonix case makes it concrete.

---

## §8 Cloudonix + Pipecat

### How Dograh actually integrates Cloudonix

Mechanism, established from source rather than docs, all `[E1 @ dograh-hq/dograh@23d22b9]`:

**It is neither a plain inbound webhook nor a SIP trunk registration into Dograh's own media layer. It is
CXML-driven media streaming over a WebSocket.**

1. **Outbound.** `CloudonixProvider.initiate_call` POSTs to
   `https://api.cloudonix.io/calls/{domain_id}/application` with `destination`, a required `caller-id`, and a
   `cxml` field carrying the call's program **inline**:

   ```xml
   <Response><Connect><Stream url="{ws_url}"></Stream></Connect><Pause length="40"/></Response>
   ```

   The provider's own comment states the distinction: "Unlike Twilio/Vonage, Cloudonix embeds CXML directly in the
   API call rather than using webhook callbacks" (`provider.py:180-184`). `ws_url` is built by
   `ws_auth.build_media_ws_url(...)` and, per the code's own note, "carries a bearer capability token"
   (`provider.py:249`).

2. **Inbound.** Cloudonix routes per **Voice Application**: the application's `url` is set once (to Dograh's
   `/api/v1/telephony/inbound/run`), with runtime `Cloudonix (CXML)` and resource type
   `Remote Application Resource`, and every DNID bound to that application uses it. Dograh **auto-creates** the
   Voice Application on configuration save and **auto-pushes** the webhook URL when an inbound workflow is
   assigned [E2, `docs/integrations/telephony/cloudonix.mdx`]. So inbound *is* webhook-shaped — but the webhook
   only returns CXML that opens the same media stream.

3. **Media.** `CloudonixProvider.handle_websocket` performs a `connected` → `start` handshake, reads stream
   identifiers and `call_id` from the start metadata, and rejects with close codes 4400/4408 on a malformed or
   slow handshake (`AGENT_STREAM_HANDSHAKE_TIMEOUT_S = 10`) (`provider.py:496-570`). This is the same shape as
   Twilio Media Streams.

4. **Into Pipecat.** `cloudonix/transport.py` then builds a **stock `FastAPIWebsocketTransport`** with
   `FastAPIWebsocketParams(serializer=CloudonixFrameSerializer(...), audio_out_10ms_chunks=2, ...)` — no custom
   transport class at all.

5. **The serializer is not upstream.** `cloudonix/serializers.py` is five lines:
   `from pipecat.serializers.cloudonix import CloudonixFrameSerializer`. And
   **`pipecat-ai/pipecat@f236a08` contains zero files matching `cloudonix`** — `grep -ril cloudonix` over the
   entire upstream repo returns nothing [E1]. Same for `serializers/call_strategies.py`, the
   `HangupStrategy`/`TransferStrategy` abstraction `CloudonixHangupStrategy` and `CloudonixConferenceStrategy`
   subclass. Both live **only in Dograh's fork** (§7a's added-file list confirms:
   `added src/pipecat/serializers/cloudonix.py`, `added src/pipecat/serializers/call_strategies.py`).

6. **Transfer needed bespoke work on top.** `CloudonixConferenceStrategy`'s docstring: "Cloudonix has no live-CXML
   push equivalent to Twilio's call-update; `POST /calls/{domain}/sessions/{token}/fork` is the primitive that
   re-runs CXML on a connected session," with a load-bearing caveat that the fork "MUST target the Cloudonix
   session token… (the media `callSid` will not resolve the session)."

7. **Provisioning and setup are Dograh's, not Pipecat's.** `provisioning.py`, `setup.py` (a checklist distinguishing
   a Dograh-managed domain from a customer-owned one), `regions.py` (per-region SIP edge hostnames and ports over
   UDP/TCP/TLS), `config.py`, `routes.py` (transfer-result, status-callback and CDR endpoints) — 3,198 lines in the
   `cloudonix/` package alone.

### What Cloudonix + Pipecat would look like *for this team*

Cloudonix markets "Agentic Voice Trunking… direct connections from any SIP platform to multiple AI Voice Agent
platforms, supporting over 30 different AI Voice Agent platforms" [E3, cloudonix.com, unverified]. That framing
should not be read as "Pipecat is supported": what is demonstrably true at source level is that upstream Pipecat
has no Cloudonix anything.

Concretely, two shapes:

**Shape A — Cloudonix into Pipecat directly (the Dograh shape).** Write a `CloudonixFrameSerializer` (upstream's
comparable serializers are 171–314 lines: `exotel.py` 171, `vonage.py` 188, `plivo.py` 256, `telnyx.py` 292,
`twilio.py` 314 [E1]), stand up a FastAPI WebSocket endpoint doing the `connected`/`start` handshake with a
capability token, emit the `<Connect><Stream>` CXML on outbound, register the Voice Application and push its `url`
for inbound, and build hangup/transfer strategies against Cloudonix's session-fork primitive. The serializer is a
day or two; **the rest is the integration Dograh spent a 3,198-line package on.** And unless you carry a fork or
vendor the serializer locally, you are maintaining code outside the dependency you adopted it for.

**Shape B — Cloudonix as a SIP trunk into our existing stack, unchanged.** Cloudonix exposes per-region SIP edges
over UDP/TCP/TLS — Dograh's `regions.py` enumerates hostname/port per transport and surfaces them as
`SIPConnectivityDetails` for a customer to point a carrier at [E1]. A SIP trunk pointed at our Kamailio/FreeSWITCH
is, on the face of it, the ordinary path, and it needs no Pipecat at all. **Unverified** — it would require a
Cloudonix trial account to confirm the trunk terminates cleanly against our dialplan and that inbound DID delivery
matches what our routing expects. This lands in exactly the posture the separate Cloudonix-as-DID-provider
evaluation already reached.

**Does Cloudonix fit an existing Pipecat transport shape?** Yes, in the same way Twilio/Telnyx/Plivo do: it is a
media-stream-over-WebSocket carrier, so `FastAPIWebsocketTransport` + a serializer is architecturally sufficient —
Dograh proves that empirically by using the stock transport unmodified. The missing piece is the serializer and
everything around it, none of which exists upstream.

**The explicit connection back to the PRD.** The PRD's finding that "production telephony integration is left to
the integrator, not solved by Pipecat" **holds, and Cloudonix is its sharpest case.** A carrier that a shipping
Pipecat-based product lists as supported is one whose entire Pipecat-side integration that product had to write
itself, in a fork, alongside a transfer strategy it had to invent because the carrier's control-plane primitive
does not match the one Pipecat's Daily path assumes. Pipecat supplies the frame model and a WebSocket transport.
Everything between a carrier and that transport is yours.

```
Recommendation — Cloudonix with Pipecat
Verdict: Reject as "supported"; feasible as bespoke work, and the cheaper path does not involve Pipecat
1 Latency:         unknown — no Cloudonix account, nothing measured [Appendix C.4]
2 Service count:   unchanged under Shape A; unchanged under Shape B
3 OSS-over-custom: neutral-to-negative — the OSS framework contains no part of this integration,
                   and Shape A likely implies carrying a fork or a local serializer
4 Scalability:     unknown
Non-negotiables:   FAILS as stated (no upstream Cloudonix support); Shape B passes but is a
                   telephony-provider question, not a Pipecat question
Revisit when:      upstream Pipecat merges a Cloudonix serializer, OR a Cloudonix trial account
                   lets us verify Shape B against our own dialplan — which is the actual next step
                   if Cloudonix matters, and it does not require this decision at all.
```

---

## §9 Constraints, non-negotiables, and the self-check

**The non-negotiables, each cited to a tracked file in this repo:**

1. No synchronous Postgres or Config-Service call on the media path — `CURSOR.md`.
2. Redis holds routing data only, written through with no expiry — `CURSOR.md`, `services/config/app.py`,
   `gateway/include/config/Config.h`.
3. Gateway / telephony / conversation responsibility split intact — `CURSOR.md`.
4. The Kamailio dialplan routes only the DID pattern configured in `scripts/kamailio/kamailio.cfg.tpl`.
5. Playback pacing, final-marker ordering, pre-roll and early-barge-in preserved — `services/vobiz/bridge.py`
   module docstring, `_playback_pacer`, `_audio_delay_pump`, `_clear_playback_queue`, `_flush_pending_audio_now`.
6. Stateless Gateway — `CURSOR.md`.
7. Behavioural requirement, stated in its own words: an unresolvable DID must not silently inherit another
   tenant's agent. [detail withheld — public repo]

**Does the DID-routing / hot-cold-path constraint transfer to Pipecat's telephony integrations? No — it does not
transfer, because there is nothing for it to transfer *to*.** Pipecat's telephony story is a WebSocket transport
plus a per-carrier serializer (§3, §4.1). The DID → agent resolution, the Redis-only hot-path lookup and the
dialplan pattern all live *upstream* of where a Pipecat pipeline begins: by the time a media WebSocket opens, the
call has already been routed. Adopting a Pipecat transport therefore neither preserves nor breaks constraints 2, 4
and 6 — it simply has no opinion about them, and they stay ours to enforce. What *would* break them is the
architecture Pipecat's maintainer recommends for self-hosted production — "moving to a hosted solution"
[E2, #3987] — because a hosted transport terminates the call before our dialplan and our Redis lookup ever see it.
That is the concrete thing that breaks: not the frame model, the deployment model.

Constraint 5 is the one a migration would most likely violate by accident. `_audio_delay_pump`'s pre-roll has no
Pipecat equivalent (§4.8), and the pacing/final-marker ordering would have to be re-derived inside `base_output`'s
clock/audio task split rather than carried across.

**Preserve-list.** §1(a) is Reject, so no full migration preserve-list is owed. §1(b) is a Partial adopt, and its
preserve-list is short precisely because it is greenfield: the S2S pipeline must not change the media path, must
terminate the trunk on our own stack, must resolve `RuntimeConfig` once at session start and never per turn, and
must not introduce a second turn boundary that disagrees with the Gateway's — use the locally-driven-turns pattern
(§5A). Transcript recording and session finalization are **not** automatically inherited by an S2S pipeline
(§4.12 shows Pipecat has no durable transcript store), so a future S2S PRD must state how they are provided.

**Self-check against the Interfaces rules.** Every Recommendation block above has four numbered criteria, a
Non-negotiables line and a `Revisit when:` line. §1 contains exactly one verdict sentence. §10 is prose and does
not defer to a table. One deliberate deviation is recorded rather than buried: the design's stricter bar bars
`Adopt`/`Partial adopt` when criterion 1 is `unknown` on a media-path stage, and §5A's speech-to-speech block is a
`Partial adopt` with latency formally `unknown`. The design's own review flagged this bar as stricter than the
PRD's ("an adopt confirms it does not regress latency"), and §5A is the case that distinguishes them: removing two
network round-trips per turn is a structural reduction, and no smoke test was run *by design*, so requiring a
measurement would make every adopt verdict unreachable regardless of evidence. The relaxation is stated inside
that block, not only here. Every other media-path block is `Reject`.

---

## §10 Net benefit — the direct answer

**For the cascaded pipeline as it exists today: no material benefit. Recommend keeping it in-house.** Here is
what was weighed and why it did not clear the bar.

*Latency (criterion 1).* Nothing was measured, by design, and the reason is worth restating because it is the
whole shape of the answer: a Pipecat number produced in a venv would be a number about a laptop, not about a call
arriving over a carrier trunk through Kamailio and FreeSWITCH. So criterion 1 is `unknown` for every media-path
stage. Since the requester's own ordering puts latency first and says an adopt cannot rest on feature parity that
costs latency, an unmeasured media-path swap cannot be recommended. That alone settles §1(a), and the remaining
criteria only reinforce it.

*Service simplification (criterion 2).* This is the criterion the requester weights most heavily after latency, and
it is where the case collapses most clearly. **Pipecat does not retire a single service.** It operates at the
library layer *inside* `services/conversation`; the gRPC contract, the Gateway, the Config Service, `services/vobiz`
and the Kamailio/FreeSWITCH path all remain exactly as they are. `services/campaigns` has no Pipecat counterpart at
all — no DNC, no contact lists, no pacing, no calling-hours window, no audit (§6.1). `services/webcall` has a
better browser-media story in `SmallWebRTCTransport` + RTVI, but retiring it means retiring the Conversation gRPC
contract it exists to exercise (§6.2). The service count delta for adopting Pipecat across the pipeline is **zero**,
and the *code* count goes up, not down: the one measured production example of doing this is Dograh's 10,176 lines
of integration glue plus 17,907 lines of telephony the framework does not provide (§7c).

*OSS-over-custom (criterion 3).* This is the criterion that genuinely favours adoption, and it should be stated at
full strength rather than argued away. Pipecat at 1.10.0 is a serious project — 15.5k stars, pushed the day of this
read, ~25 STT services, ~30 TTS services, flows and IVR and voicemail extensions in-tree, an eval harness, and an
interruption architecture that is *better than ours in design*: broadcast cancellation instead of a coordinator,
`UninterruptibleFrame` as a real type, a settlement protocol for cancelled async tools. Those are things we
hand-roll with booleans. But criterion 3 is explicitly the "all else being reasonably equal" criterion, and all
else is not equal here in two concrete ways. First, the one real-world team that built a product on Pipecat could
not consume it as a dependency: 105 commits ahead, 805 behind, 139 changed files inside `src/pipecat/`, a standing
merge procedure, and their own billing module living inside the framework fork (§7a). Adopting a fork is not
adopting an open-source dependency; it is adopting a maintenance obligation. Second, roughly a third of our
capability rows are `GAP` — the transfer decision engine, the guardrail counter and its escalation, the
directive/`StreamBuffer` protocol, the booking-fabrication and phone-readback checks, tool policy resolution,
calibrated filler sizing, the FSM, session finalization, per-tenant retrieval policy, runtime config. Those are not
plumbing; they are the product, and each would be re-implemented as a custom `FrameProcessor` rather than deleted.

*Scalability (criterion 4).* Parity, and neither side clears it decisively. Both are per-session async Python.
Pipecat does not solve session affinity — that was the substance of issue #3987, and the maintainer's answer was to
use a hosted transport, which is the bet this platform declined (§3).

**And the reason this evaluation was reopened — reliability — points the other way from what was hoped.** The
premise was that a framework swap might retire a class of bug: barge-in edge cases, STT/LLM/TTS timing races,
filler-timing defects. Pipecat's architecture is better here. Its bug list is not: 32 open interruption-class
issues at this read, including a transcription frame dropped by its own turn's interruption (#5683), interruption
during a `tts_say` leaving an action count stuck (#5263) — cancel state leaking across turns, the same defect
fixed here this cycle — a per-tool timeout disarmed into a hung async tool (#5481), audio dropping mid-word in a
resampler (#5769), and transfer-during-playback on WebSocket telephony still an open feature request (#5774). Plus
a fixed-bug comment inside `frame_processor.py` describing a queued uninterruptible frame blocking interruptions.
**These races are intrinsic to streaming voice pipelines, not to our implementation of one.** Migrating trades bugs
we have found, reproduced on real SIP calls and fixed, for bugs we have not, in code we do not own, on a cadence we
do not control. As a reliability strategy that is a regression, and it is the single clearest finding in this
document.

**Where there *is* a named net benefit: speech-to-speech.** Pipecat ships nine S2S services with frame-level
integration — tool calls routed through each vendor's native tool channel, locally-driven-turn variants that let us
keep our own turn boundary, ~30 examples, continuous 1.x development, and a titled-issue defect load of three. We
have no S2S capability at all, and building one from zero is the largest single piece of net-new work this platform
would otherwise face. Scored: latency **better** (two network round-trips removed per turn, structurally — magnitude
unverified), service count **unchanged** but a large build avoided, OSS-over-custom **strongly favours adopt** with
nothing in-house displaced, scalability **parity**, non-negotiables **pass** because nothing on today's media path
changes. That is the one recommendation in this document where all four criteria point the same way, and it is an
*addition*, not a replacement.

**Three ideas worth taking without taking the framework** (detail in §11): `UninterruptibleFrame` as an explicit
type rather than ad-hoc booleans; the async-tool settlement protocol so a cancelled tool always tells the model it
did not finish; and semantic end-of-turn detection from `audio/turn/smart_turn/`.

---

## §11 Adoptable ideas — from Pipecat and from Dograh

Pointers for separately-scoped future work. **Neither Pipecat-as-a-framework nor Dograh is a build target**;
Dograh in particular is used here only as production-gap evidence and as a source of ideas.

**From Pipecat** (each `[E1 @ pipecat-ai/pipecat@f236a08]`):

1. **`UninterruptibleFrame` as an explicit type.** The problems this team hit — a filler blocking a tool call
   instead of overlapping it, audio that must finish despite a barge-in — are all "this unit of work is not
   cancellable." Pipecat gives that a type the framework enforces at the cancellation site. We express it with
   per-site booleans. Worth stealing as a concept in `pipeline.py`/`bridge.py` even with no Pipecat in the tree.
2. **Async-tool settlement protocol.** `cancel_on_interruption=False` plus `started`/`intermediate`/`final`
   context messages, where a *cancelled* tool always writes a `final` carrying a cancellation notice. It designs
   out "the model never learns the tool was abandoned."
3. **Broadcast interruption.** Cancellation pushed upstream *and* downstream so each stage cancels itself, rather
   than routed through one coordinator that can miss a stage.
4. **Semantic end-of-turn.** `audio/turn/smart_turn/` (local CoreML, local v2/v3, HTTP) as a model behind
   `libs/vad_sdk`'s existing interface — plausibly removes silence-timeout latency; cost unquantified (§4.2).
5. **Eval harness shape.** `src/pipecat/evals/` (`judge.py`, `matcher.py`, `persona.py`, `simulation_driver.py`,
   `script_driver.py`) — simulated-caller conversation evaluation, a gap on our side that the SIPp harness does not
   fill.

**From Dograh** (each `[E1 @ dograh-hq/dograh@23d22b9]` unless noted):

6. **Visual node-based workflow builder** (`ui/src/app/workflow/…`). Compare against what ships today —
   `services/conversation/workflow/runner.py`, `workflow/extractor.py` and `libs/config_sdk/workflow.py` — which is
   a working node-graph *runtime* with variable extraction but no visual editor. The PRD also named an in-progress,
   not-yet-merged graph-model effort as a comparison target; the relationship is that the shipped runtime is what
   exists and the in-progress effort is a separate, unlanded direction — so the honest comparison is against the
   shipped runtime, and the builder is a UI layer neither has. [detail withheld — public repo]
7. **Provider-registry-generated config UI.** `api/services/telephony/registry.py`'s `ProviderSpec` /
   `ProviderUIField` / `ProviderUIMetadata` render each provider's configuration form from its own declaration —
   "adding a new provider should not require any edit outside its own folder plus a single import line." Our
   `libs/telephony_sdk/registry.py` + `required_credential_fields()` already backs provider discovery; the missing
   half is the declarative UI metadata (field types, `sensitive`, conditional visibility, sections).
8. **MCP server for agent configuration.** `api/mcp_server/` — lets a coding agent edit agent config directly. We
   have nothing equivalent. Note that Pipecat's own `services/mcp_service.py` is an MCP *client* (the bot calls
   external tools), which is a different capability.
9. **Hybrid pre-recorded-clip + TTS-fallback playback.** `api/services/pipecat/recording_router_processor.py`
   "routes LLM responses between TTS and pre-recorded audio playback," with `recording_audio_cache.py` and
   `audio_file_cache.py`. Cost and conversion claims are Dograh's own marketing and are **unverified** [E3]; the
   *mechanism* is real and read at source. Relevant to our own greeting and filler paths, where the text is known
   in advance and TTS latency is pure overhead.
10. **Setup-checklist as a first-class provider concept.** `cloudonix/setup.py`'s `ProviderSetupChecklist` /
    `SetupStep` enumerates what a configuration still needs before it can carry a call, and distinguishes a
    managed domain from a customer-owned one. A good answer to "why isn't this number working yet."

---

## Appendix A — Source ledger

| Source | Identifier | Accessed | Claims resting on it |
|---|---|---|---|
| this repo | `9646aece5eedc9da4dcc6c9bc13a52f504a3764e` | 2026-09-15 | all of §2; every "today" column in §4–§6; §5B; §9 |
| `github.com/pipecat-ai/pipecat` | `f236a08991065b645329fb4f3f02fc403f29be5e` | 2026-09-15 | §3; all Pipecat columns in §4–§6; §5A service inventory; §8's zero-Cloudonix finding; §11 items 1–5 |
| `pipecat-ai/pipecat` `CHANGELOG.md` | same commit | 2026-09-15 | v1.10.0 / 2026-09-11; S2S feature history |
| GitHub REST — `repos/pipecat-ai/pipecat` | — | 2026-09-15 | 15,556 stars; 2,691 forks; 326 open issues; `pushed_at` |
| GitHub REST — issue 3987 + comments | — | 2026-09-15 | closed 2026-03-11; filer's text; maintainer's 2026-03-15 and 2026-03-24 replies |
| GitHub search — open interruption/barge-in issues | — | 2026-09-15 | 32 open; #5774, #5769, #5735, #5683, #5658, #5654, #5611, #5481, #5425, #5325, #5303, #5263 |
| GitHub search — open realtime/S2S issues | — | 2026-09-15 | 3 titled; 18 mentioning "realtime"; 11 mentioning "gemini live" |
| `github.com/dograh-hq/dograh` | `23d22b95defbd0a23719a7f9b265eb9f7acdebf8` | 2026-09-15 | §7; §8's mechanism; §11 items 6–10 |
| `github.com/dograh-hq/pipecat` (fork) | `70385ad8a5204458a241e60287289932e3eb3ae2` | 2026-09-15 | the pinned submodule SHA |
| GitHub compare API — upstream `main` ↔ fork pin | — | 2026-09-15 | diverged; 105 ahead / 805 behind; 300 files; 139 under `src/pipecat/` |
| `docs.dograh.com` Cloudonix page (in-repo `.mdx`) | same Dograh commit | 2026-09-15 | Voice Application / DNID binding; auto-create and auto-push of the webhook URL |
| `cloudonix.com` | — | 2026-09-15 | "Agentic Voice Trunking", "30+ AI Voice Agent platforms" — `E3`, unverified |

Not consulted, and named so the absence is visible: no Cloudonix trial account; no authenticated GitHub code
search for production references; no Pipecat Cloud account; no LiveKit Agents re-evaluation (out of scope per PRD).

## Appendix B — AC → section trace

| AC | Satisfied by |
|---|---|
| 1 — every `services/conversation` capability mapped | §4.4–§4.14, §4.15 (DROPPED rows) |
| 2 — vobiz + vad_sdk represented, not shortcut | §4.1, §4.2, §4.8 |
| 3 — knowledge_sdk + config_sdk extension points | §5B |
| 4 — every sourced claim attributed and unverified by default | "How to read the evidence tags"; tags throughout; Appendix C |
| 5 — one explicit recommendation | §1 |
| 6 — what would reopen the decision | `Revisit when:` on every non-adopt block |
| 7 — preserve-list if adopting | §9 (short, because §1(a) is Reject and §1(b) is greenfield) |
| 8 — Kamailio / hot-cold-path constraint addressed head-on | §9 (paragraph 2); §4.1 |
| 9 — no in-scope file modified | see the read-only note below |
| 10 — campaigns and webcall each stated | §6.1, §6.2 |
| 11 — four criteria scored on every recommendation | every Recommendation block; §10 |
| 12 — labelled net-benefit prose section | §10 |
| 13 — Dograh (a)(b)(c)(d) | §7 (a)–(d) |
| 14 — discrete adoptable-ideas list + "not a build target" | §11 items 6–10 and its opening sentence |
| *new* — reliability, not just features | §4.8; §10 (penultimate paragraph) |
| *new* — speech-to-speech as its own section | §5A |
| *new* — Dograh's Cloudonix integration and Cloudonix+Pipecat | §8 |

## Appendix C — What was not verified, and what would settle it

Every runtime claim in this document is unverified. The experiments below would settle them; none was run.

**C.1 — Voice-to-voice latency, cascaded pipeline (blocks §1(a), §4.1, §4.8).** A/B against the SIPp harness in
`loadtest/sipp/`: same DID, same STT/LLM/TTS providers, same prompt, one arm through today's pipeline and one
through a Pipecat pipeline behind the same bridge. Measure per-turn speech-end → first-audio-out at p50/p95 over
≥100 turns. Until then criterion 1 is `unknown` for every media-path stage, which is why they are all `Reject`.

**C.2 — Smart-turn cost/benefit (blocks §4.2's partial adopt).** Per-turn inference cost of
`local_smart_turn_v3` on our hardware, against the silence-timeout wait it removes. Net could be negative.

**C.3 — S2S latency magnitude (§5A criterion 1).** Same SIPp harness, cascaded arm vs. an OpenAI Realtime or
Gemini Live arm. The *direction* is structural (two round-trips removed); the magnitude, and whether S2S
end-of-turn behaviour on a real trunk is acceptable, are not established.

**C.4 — Cloudonix, anything (§8).** Requires a Cloudonix trial account. Two things to confirm: (a) that a Cloudonix
SIP trunk terminates cleanly against our own dialplan with correct inbound DID delivery — Shape B, the path that
needs no Pipecat; (b) the exact media-stream frame contract, if Shape A were ever pursued.

**C.5 — Pipecat cold-start on first greeting, and SmallWebRTC audio quality (§4.13).** The PRD cites 4–5 s
cold-start, 200–400 ms/turn VAD gating, and SmallWebRTC audio regressions. **These were not reproduced, and are
carried at `E3` only.** They date from the 0.0.x line and the maintainer dates SmallWebRTC's release to "one year
ago" relative to March 2026 [E2, #3987], so they should not be treated as current 1.10 behaviour without a rerun.

**C.6 — Production references for Pipecat S2S (§5A).** "How many teams use this in production" was not determinable
from available sources. The one `E1` data point is Dograh's eleven S2S modules. Anything stronger would need an
authenticated code search or direct vendor references.

**C.7 — Whether Dograh's fork divergence is reducible.** Bounded to the compare API's file list per the design; no
line-by-line review was done. Whether the 139 changed `src/pipecat/` files represent work that *could* have gone
upstream, versus work that *had* to be a fork, is **open**.

---

*Read-only: this evaluation modified no file under `services/conversation`, `services/vobiz`, `services/campaigns`,
`services/webcall`, `libs/telephony_sdk`, `libs/vad_sdk`, `libs/knowledge_sdk` or `libs/config_sdk`. The only path
added is this document.*
