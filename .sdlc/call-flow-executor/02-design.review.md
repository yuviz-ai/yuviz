# Review: Call-flow executor design

## Round 2

VERDICT: GREEN

All three round-1 findings verified fixed against the revised file, no regressions found.

1. [was blocking, now resolved] `out_responses` gap. Confirmed: Changes table rows for
   `services/conversation/pipeline.py` (line 53) and `services/conversation/echo.py` (line 54) now
   each add an explicit `out_responses: asyncio.Queue[HandlerResponse] | None = None` class
   attribute. `ConversationSession.out_responses` (lines 246-248) is `getattr(self._handler,
   "out_responses", None)` — a real second line of defence, not a mask for the missing attribute,
   since the design's own prose (lines 251-259) states plainly that without the explicit class
   attributes the no-flow path would `AttributeError`, not read `None`. The Risks bullet (lines
   382-388) no longer claims bare inertness — it now states the branch is inert *only if* both the
   class attributes and the `getattr` default land, which is accurate. Test 11 (lines 461-468) is
   rewritten to drive real `PipelineConversationHandler`/`EchoConversationHandler` instances through
   `Converse` (an `AttributeError` would abort the stream and fail the message-sequence assertion —
   a genuine failure mode, not a mock that can't observe it) plus a parametrized tripwire asserting
   `"out_responses" in vars(type(h))` per enumerated implementer, which is a real structural check
   (not vacuously true) that fails today before the attribute exists. Fixed.

2. [was blocking, now resolved] Product-decision open questions. Confirmed: OQ2-4 (lines 323-354)
   are re-framed as "design-proposed defaults pending product sign-off," each keeping its proposed
   answer, its reasoning, and a new "Reversal without rework" clause naming a single change point:
   OQ2 via `CallFlowRunner._submit_collect()`/`_invalid_attempt()`'s return value, OQ3 via the
   runner's terminator comparison, OQ4 via the single `resolve_call_flow() is None` branch in
   `handler_factory`, with the apology expressible as a synthetic one-node `hangup` graph — all
   three seams are single-point and consistent with the design's own "one branch, one place"
   posture (`__main__.py` row, line 55). This is a substantive re-framing, not relabelling: the
   language now explicitly says these are proposals awaiting the PRD's named requester, not closed
   decisions. One small note, not a new finding: `_submit_collect()`/`_invalid_attempt()` are named
   only in this disposition section and don't appear in the runner's own Interfaces stub (lines
   200-210), so the claimed seam is asserted rather than shown — acceptable for a private helper,
   but the plan stage should confirm these two functions actually exist as named. Fixed.

3. [was minor, now resolved] Confirmed: "Node semantics" gained the asymmetry passage (lines
   288-294) explicitly distinguishing collect/menu exhaustion (caller already on the line) from
   config-plane resolution failure (call never opened), stated as one rule. Fixed.

No regressions: file count is still 24 (verified by counting Changes table rows), no files added
or removed, no restructuring of sections. All round-1-verified citations —
`bind_path_tenant` (services/config/deps.py:116), `_authorize_flow` (services/config/routers/call_flows.py:59),
`record_live_stage`, `_flush_pending_audio_now` (services/vobiz/bridge.py:190), and the
`GatewayMessage.oneof payload` proto (field 9 free, 8 arms in use) — are textually unchanged from
round 1 and remain accurate.

Nothing outstanding blocks the security stage.
