# PRD: Pipecat Evaluation — Build vs. Adopt Decision for the Conversation Pipeline

## Problem
The request asks whether `services/conversation` (STT→LLM→TTS pipeline, transfer engine, guardrails, tool orchestration,
RAG retrieval, transcript recording) and the surrounding call-handling stack (`services/vobiz`, `libs/telephony_sdk`,
`libs/vad_sdk`) should be replaced with Pipecat, an open-source Python voice-agent framework, or kept as-is. Today
that decision is undocumented judgment call territory — there is no artifact that says what was compared, on what
criteria, or why. Without one, the question will get re-litigated from scratch the next time someone hits friction
in the pipeline, and any future contributor cannot tell whether "just use Pipecat" was already considered and
rejected, or never considered at all.

This PRD scopes an **evaluation deliverable**, not a migration. It defines what "evaluate" must produce so the
resulting document is a decision the team can act on (adopt / partial-adopt / reject-and-record-why), not a survey.

Assumption: this evaluation is documentation-and-comparison only, not a runnable spike — the more practical default
for a decision-support PRD rather than an implementation one. See Scope for how an optional scratch verification,
if the evaluator judges one necessary, is bounded so it doesn't become an undeclared implementation effort.

## How this is solved elsewhere
- **Pipecat** (Daily) is a transport-agnostic Python pipeline framework: STT→LLM→TTS stages, built-in VAD/turn-taking
  and barge-in, pluggable providers (Deepgram, ElevenLabs, OpenAI, etc.), telephony via Twilio/Daily/Telnyx, and a
  managed "Pipecat Cloud" for SIP + autoscaling if you don't want to run it yourself. Pipecat also ships higher-level
  building blocks — `pipecat-flows` for scripted/branching conversation flows and framework examples for outbound
  dial-out and browser-based test/dev calling via its transports (Daily, SmallWebRTC, Twilio) — which is the
  ecosystem surface this evaluation must check against `services/campaigns` and `services/webcall` (unverified
  until the evaluator confirms feature-for-feature parity, not just conceptual overlap).
- **LiveKit Agents** converged on the same STT→LLM→TTS shape but ships its own WebRTC/SIP transport and media
  infra rather than being transport-agnostic — multiple comparisons (ThinnestAI, F22Labs, ReactifySolutions, 2026)
  call it the stronger choice for production voice platforms specifically because session affinity, scaling and
  transport reliability are solved by the platform, not left to the integrator.
- **Vocode** is telephony-first and more opinionated toward phone call flows, with a hosted option — narrower
  scope than Pipecat or LiveKit.
- **Dograh** (`dograh-hq/dograh`, GitHub) is the most directly relevant data point of all four, because it is not a
  framework to compare against Pipecat — it is itself a shipping, self-hosted voice AI platform *built on* Pipecat,
  i.e. one real team's already-taken answer to the exact question this PRD asks. Per its README and GitHub
  description (unverified beyond these sources until the evaluator reads the actual repo): Dograh vendors Pipecat
  as a **git submodule pinned to a specific commit** and describes itself as maintaining a **custom fork** of it
  for telephony integrations — i.e. it did not consume upstream Pipecat as a plain dependency, which is itself a
  signal worth weighing (see the fork/vendor discussion below). It is a monorepo (`/ui`, `/api`, `/sdk`, `/deploy`)
  positioned as a self-hosted alternative to Vapi/Retell, and ships several concrete capabilities this codebase
  does not currently have: (a) a **visual node-based workflow builder** (start/agent/tool/webhook nodes with
  transitions) — directly relevant prior art for this codebase's own in-progress, not-yet-merged
  `future_workflow_graph_model.md` effort, worth a side-by-side comparison regardless of the Pipecat
  replace/continue decision; (b) an **MCP server** that lets coding agents (Claude Code, Cursor, Codex) edit agent
  configuration directly — a capability this platform does not have; (c) a **unified telephony-provider
  abstraction** spanning Twilio, Vonage, Telnyx, Plivo, Cloudonix, Asterisk (ARI), and a provider literally named
  "Vobiz" in its provider list (unverified whether this is the same integration surface as this codebase's
  `services/vobiz` or an unrelated, coincidentally-named provider — the evaluator should check, since a name this
  specific is unlikely to be pure coincidence and may point at a shared upstream telephony API both integrated
  against); and (d) a **hybrid pre-recorded-clip + TTS-fallback** playback technique (claimed cost and conversion
  improvements, unverified beyond Dograh's own marketing material) that this codebase's TTS layer does not use.
  Dograh's own documentation describes "minimal explicit detail on production scaling" and names session affinity
  and call-transfer/human-handoff as open concerns rather than solved problems (per its README, unverified beyond
  that source) — independent, third-party corroboration that the "self-hosted Pipecat production is a known gap"
  finding below is real, not just an issue-tracker complaint, since a team that forked Pipecat to build a product
  on it still had to solve (or is still solving) the same problem this codebase already solved with Kamailio +
  Redis hot-path routing + ESL wiring.
- Where they genuinely fork: **transport-agnostic + flexible pipeline (Pipecat)** vs. **integrated transport +
  managed scaling (LiveKit Agents)**. Pipecat's own GitHub issue tracker (#3987, open as of 2026) confirms
  self-hosted production deployment blueprints are a known, acknowledged gap — teams report the jump from "runs
  locally" to "runs in production" requires building session affinity, scaling and transport reliability
  themselves, which is exactly the layer this codebase already built and hardened (Kamailio DID routing, Redis
  hot-path routing, barge-in pacing, ESL wiring — see `did_management_platform_architecture.md`,
  `kamailio_did_routing_gotcha.md`, `project_bargein_playback_design.md`). Dograh's own choice to fork/vendor
  Pipecat rather than depend on it directly is independent corroboration of that same gap from a real production
  deployment, not just from an issue tracker. Documented Pipecat production issues as of 2026 (unverified beyond
  the cited sources, not independently reproduced) include 4-5s cold-start on first greeting, VAD-gated response
  start costing 200-400ms/turn, and audio-quality regressions in its no-Daily-dependency WebRTC transport
  (SmallWebRTCTransport, since v0.0.62).
- **Recommendation going into the evaluation**: do not treat this as "rip out and replace." The parts of this
  codebase that are hard-won and specific to this product — the transfer engine, guardrails, tool-call
  orchestration, per-tenant RAG retrieval policy, the DID-routing and hot/cold-path split, barge-in pacing tuned
  against real SIP calls — are not things Pipecat provides; they are the actual product. What Pipecat (or LiveKit
  Agents) could plausibly replace is narrower: the STT/LLM/TTS stage-sequencing plumbing inside `pipeline.py` and
  possibly the transport/VAD layer in `services/vobiz` + `libs/vad_sdk`. Scope the evaluation to that narrower
  question — "does adopting a framework at the plumbing layer save more than it costs to bend it around our
  transfer/guardrail/RAG/tool logic" — rather than an all-or-nothing rewrite, and say so explicitly in the
  evaluation's conclusion even if the answer ends up being "reject." The one place this default should be
  overridden is where Pipecat's own ecosystem already does the whole job of a standalone in-house service
  (campaigns, webcall) — there, the bar is not "is the plumbing better" but "do we need to run this service at
  all," and the requester's explicit priority on fewer in-house services and open-source-over-custom should carry
  real weight, not just be one factor among many. Dograh's decision to fork/vendor Pipecat rather than depend on
  it cleanly is a data point that should temper an "adopt as a plain dependency" conclusion if the evaluator finds
  the same forking pressure applies here. **Do not treat Dograh itself as an adoption option** — running a
  third-party Pipecat-based product on top of our already-hardened telephony/routing layer would mean re-solving
  the same integration problem the in-house stack solves today, plus inheriting Dograh's own release cadence and
  its acknowledged scaling/session-affinity gaps. The correct use of Dograh in this evaluation is narrower: as
  evidence for the Pipecat production-gap finding, and as a source of specific, adoptable ideas (workflow builder,
  telephony abstraction, MCP server, hybrid TTS) to record even if the overall Pipecat recommendation is "reject."

## Decision criteria (standing weighting for every recommendation in this evaluation)
Every per-stage and per-service recommendation this evaluation produces (conversation pipeline stages, and the
campaigns/webcall comparisons added below) must be explicitly weighed against these four criteria, in this
priority order the requester stated:
1. **Latency** — does Pipecat achieve voice-to-voice latency equal to or better than the current pipeline for the
   capability being compared? A recommendation to adopt cannot rest on feature parity alone if it costs latency.
2. **Service simplification** — does adopting Pipecat for this capability reduce the number of custom in-house
   services that must be built, run, and maintained (e.g., retiring `services/campaigns` or `services/webcall`
   entirely, not just changing what they call internally)?
3. **Preference for globally-accepted open source over custom-written services** — all else being reasonably
   equal, adopting a widely-used open-source framework over maintaining bespoke code is the preferred outcome.
4. **Scalability** — can the approach handle increasing concurrent call volume without a fundamental architectural
   redesign (e.g., horizontal scaling of conversation workers, no single-process or single-instance bottleneck)?
   A capability that matches today's latency and feature set at low concurrency but requires a rearchitecture to
   scale up does not clear this criterion.
A "reject" recommendation must say which of these four criteria it fails on (or that it fails none but a
disqualifying non-negotiable — e.g., the Kamailio routing constraint — overrides them); an "adopt" recommendation
must say it satisfies at least the latency criterion, since simplification, OSS-preference, and scalability cannot
justify a regression in call quality.

## Scope
- In: Produce a written evaluation document (`.sdlc/evaluate-pipecat/`) that:
  - Inventories what `services/conversation`, `services/vobiz`, `libs/telephony_sdk`, and `libs/vad_sdk` do today,
    citing the actual modules (not a generic description).
  - Maps each Pipecat capability against the equivalent home-grown piece, stage by stage (transport, VAD/turn
    detection, STT, LLM orchestration, TTS, barge-in, telephony/SIP bridging).
  - Names, for each mapped stage, whether Pipecat matches, is superior to, or cannot reach parity with what exists
    today — and why, citing the specific behavior (e.g., transfer-on-trigger, tool-call mid-stream interruption,
    per-tenant RAG retrieval policy, the no-TTL Redis DID cache, the hot/cold path split).
  - Examines `dograh-hq/dograh` on GitHub as a concrete reference case for a production system built on Pipecat:
    how it structures its Pipecat integration (fork vs. dependency, submodule pinning), which Pipecat
    components/transports/services it uses, what it built on top of or instead of Pipecat (its own provider
    abstraction layer, telephony integrations), and what lessons from its architecture are relevant to this
    evaluation's recommendation and to the four Decision criteria above — in particular whether Dograh's choice to
    fork/vendor Pipecat rather than consume it as a plain dependency is evidence for or against this codebase doing
    the same. Separately, and independent of the overall Pipecat recommendation, records a short discrete list of
    Dograh features or techniques worth considering for adoption on their own merits — its visual workflow builder
    (compare against `future_workflow_graph_model.md`), its unified multi-provider telephony abstraction (compare
    against `libs/telephony_sdk`'s registry approach), its MCP server, and its hybrid pre-recorded+TTS playback —
    each with one line on why it's worth a look. This list is pointers for future, separately-scoped work, not a
    design or a commitment to build any of them.
  - Separately evaluates, for each of `services/campaigns` (outbound calling campaigns) and `services/webcall`
    (browser-based test-call bridge for a newly created agent), whether Pipecat's own framework/ecosystem
    (including `pipecat-flows` and its example dial-out / browser-transport patterns) already provides equivalent
    functionality natively — and if so, states this as a candidate for replacing the custom service outright, not
    merely as a plumbing-layer optimization. This comparison is scoped to "does Pipecat already do this" — it is
    not a request to design the replacement.
  - States a recommendation: full adoption, partial adoption (name which stage or service), or reject — with the
    reasoning a reader can act on without re-doing the research, and with each recommendation scored against the
    four Decision criteria above.
  - Is built from Pipecat's public docs, GitHub issues, and third-party comparisons, per the assumption above — no
    running Pipecat instance is required. If the evaluator judges a specific claim (e.g. a latency number) needs
    firsthand confirmation, a throwaway smoke test may be run in a scratch directory outside `services/` and outside
    version control; it is not a project deliverable, is not vendored or committed into the codebase, and does not
    change what counts as a deliverable under this PRD.
  - Records what was NOT independently verified (e.g., any claim taken from Pipecat's docs or third-party
    comparisons without running it against this codebase's actual call volume/latency budget) as explicitly
    unverified, not asserted as fact.
  - If the recommendation is "adopt" (full or partial), states the concrete migration boundary: which files/
    services would be touched, and which existing behaviors (transfer, guardrails, RAG, transcript recording,
    barge-in pacing) must be preserved across the change — as a scoping input for a future migration PRD, not a
    migration plan itself.
- Out:
  - Actually building, forking, or vendoring Pipecat into the codebase. This PRD is the evaluation only; a
    migration (if recommended) is a separate PRD with its own acceptance criteria. This bars any Pipecat code from
    entering `services/` or being committed to the repo — it does not bar the bounded, uncommitted scratch smoke
    test described in Scope, which if it happens at all lives outside the repo and outside `services/`.
  - A cost/pricing comparison of Pipecat Cloud vs. self-hosting — the request is about capability fit, not billing.
  - Re-evaluating LiveKit Agents, Vocode, or other frameworks in depth. They may be named for contrast (as above)
    but are not the subject of this evaluation. Dograh is the one exception named above, and only for the narrower
    purposes stated (production-gap evidence and an adoptable-ideas list) — not a full independent evaluation of
    Dograh as an adoption candidate in its own right, since Dograh itself is explicitly not being considered for
    adoption (see Recommendation above).
  - Any change to `services/conversation`'s transfer engine, guardrail logic, RAG retrieval policy, or DID routing
    as part of this work — those are read, not modified.
  - Designing what a `services/campaigns` or `services/webcall` replacement would look like if Pipecat is found to
    cover them; this evaluation states whether native coverage exists and recommends retire-or-keep, a follow-on
    migration PRD designs the actual replacement.
  - Designing or scoping any of the Dograh-sourced adoptable ideas (workflow builder, telephony abstraction, MCP
    server, hybrid TTS). Naming them and their rationale is in scope; specifying how to build them is not.

## Acceptance criteria
1. Given the evaluation document is complete, when a reader checks it against `services/conversation/pipeline.py`,
   `session.py`, `transfer_engine.py`, `guardrails.py`, `tools/orchestrator.py`, and `directives.py`, then every
   capability those modules provide today (STT→LLM→TTS sequencing, cancellation on barge-in, transfer trigger
   detection, guardrail counting, tool-call orchestration, directive parsing) appears in the stage-by-stage mapping
   as either matched, exceeded, or explicitly called out as a Pipecat gap.
2. Given the evaluation document is complete, when a reader checks it against `services/vobiz` and
   `libs/vad_sdk`, then the telephony bridging and VAD responsibilities those own today are represented in the
   mapping, not silently omitted because Pipecat's docs describe VAD/turn-detection as "built in."
3. Given the evaluation document is complete, when a reader checks it against `libs/knowledge_sdk` (RAG) and
   `libs/config_sdk` (per-tenant runtime config), then the document states explicitly whether Pipecat has an
   equivalent extension point for per-tenant retrieval policy and runtime config injection, or states that it does
   not and what would have to be built to add one.
4. Given the evaluation document makes a claim sourced from Pipecat's public docs, GitHub issues, or third-party
   comparison articles, then that claim is attributed inline (product/source named) and marked "unverified" unless
   the evaluator ran the bounded scratch smoke test described in Scope specifically to confirm it — per this PRD's
   documentation-and-comparison-only assumption, the default for every claim is "unverified."
5. Given the evaluation document reaches a recommendation, when a reader looks for the decision, then it is a
   single explicit statement — full adoption / partial adoption (naming the stage) / reject — not left implicit
   across multiple paragraphs.
6. Given the recommendation is "reject" or "partial adoption," when a reader checks the document, then it states
   what would have to change (in Pipecat, or in this codebase's requirements) for the decision to be revisited,
   so the question is not re-opened from zero next time.
7. Given the recommendation is "full adoption" or "partial adoption," when a reader checks the document, then it
   names every existing behavior (transfer engine triggers, guardrail thresholds, RAG retrieval policy, transcript
   recording, barge-in pacing per `project_bargein_playback_design.md`, Redis-only hot-path DID lookups) that a
   follow-on migration must preserve, so a migration PRD cannot silently drop one.
8. Given the market-research section, when a reader checks it against the request's framing ("replace our
   services"), then it explicitly addresses the DID-routing/hot-cold-path split and Kamailio 5000-5009 routing
   constraint (`kamailio_did_routing_gotcha.md`) as a reason Pipecat's own telephony integrations (Twilio/Daily/
   Telnyx-managed) may not be a drop-in replacement for the current Kamailio+FreeSWITCH+vobiz bridge — either
   confirming this constraint transfers cleanly or naming what would break.
9. Given the evaluation is delivered, when checked against the repo, then it does not modify any file under
   `services/conversation`, `services/vobiz`, `libs/telephony_sdk`, or `libs/vad_sdk` — the evaluation is
   read-only research output.
10. Given the evaluation document is complete, when a reader checks it against `services/campaigns` (outbound
    calling campaigns) and `services/webcall` (browser test-call bridge for a newly created agent), then the
    document states explicitly, for each service, whether Pipecat's framework/ecosystem already provides
    equivalent functionality natively — and if it does, the document recommends retiring the custom service as a
    candidate outcome rather than treating campaigns/webcall as out of scope or as a plumbing-only comparison.
11. Given any recommendation in the evaluation — per pipeline stage, or for `services/campaigns`/`services/webcall`
    — when a reader checks it, then the recommendation states explicitly how it scores against each of the four
    Decision criteria (latency, service-count reduction, open-source-over-custom preference, scalability — can the
    approach handle increasing concurrent call volume without a fundamental architectural redesign), not just a
    general narrative judgment; a "reject" states which criterion it fails or names the overriding non-negotiable
    constraint, and an "adopt" confirms it does not regress latency.
12. Given the evaluation document is complete, when a reader looks for the answer to "what concrete benefit(s), if
    any, does adopting Pipecat provide over our existing in-house services," then the document contains a single,
    clearly labeled summary section (not a table, not left for the reader to infer from the stage-by-stage mapping)
    that states this directly: either the specific, named net benefit(s) — scored against each of the four
    Decision criteria (latency, service simplification, open-source-over-custom preference, scalability) — or, if
    none of the mapped stages or services clears a net benefit once weighed against those criteria, an explicit
    statement of "no material benefit, recommend keeping in-house" naming which criteria were considered and why
    they did not justify adoption. A feature-parity or capability-comparison table alone does not satisfy this
    criterion.
13. Given the evaluation document is complete, when a reader checks it for a dedicated `dograh-hq/dograh` reference
    case, then the document names: (a) whether Dograh consumes Pipecat as a plain dependency or forks/vendors it
    (and how — e.g. pinned submodule), (b) which Pipecat components/transports/services Dograh's integration uses,
    (c) what Dograh built on top of or instead of Pipecat (its own abstraction layers, telephony integrations), and
    (d) at least one explicit lesson drawn from Dograh's architecture that feeds into this evaluation's
    recommendation or its scoring against the four Decision criteria — not a passing mention or a footnote.
14. Given the evaluation document is complete, when a reader checks the Dograh analysis specifically, then it
    contains a discrete, named list of Dograh features or techniques flagged as worth considering for adoption
    independent of the overall Pipecat recommendation (at minimum: the visual workflow builder relative to
    `future_workflow_graph_model.md`, its telephony-provider abstraction relative to `libs/telephony_sdk`, its MCP
    server, and its hybrid pre-recorded+TTS playback technique), each with one line on why — and states explicitly
    that Dograh itself is not a build target, only a source of ideas and of production-gap evidence.

## Constraints
- This is a research/decision deliverable, not a code change — no schema, API, or service-boundary conventions
  apply to the deliverable itself. The constraint is on what the evaluation must account for: the existing
  hot/cold-path split and per-service responsibility boundaries documented in `architecture_decisions_voiceai.md`
  and enforced by `phase5_coding_rules.md` are the bar any "replace" recommendation must be measured against —
  a framework swap that reintroduces synchronous DB/Config-Service calls on the media path, or collapses the
  Gateway/telephony/conversation-service boundary, fails that bar regardless of Pipecat's own merits.
- The DID routing constraint in `did_redis_cache_no_ttl_design.md` (no-TTL Redis cache, write-through on
  create/update) and `kamailio_did_routing_gotcha.md` (5000-5009 routing) are existing product constraints that
  any telephony-layer replacement must either preserve or explicitly propose changing.
- The requester's standing decision criteria — latency parity-or-better, fewer in-house services to maintain,
  preference for globally-accepted open source over custom-written services, and scalability to growing concurrent
  call volume without a fundamental redesign (see Decision criteria above) — are the lens every recommendation in
  this document is judged through, not one input among several unweighted ones.
- Follow `.sdlc/lessons.md` #7: if a requirement or capability comparison is dropped to keep the document short,
  say what was dropped and why — do not silently omit a stage from the mapping.

## Open questions
None. (Prior open question — whether `services/campaigns`/`services/webcall` are in scope — is resolved: they are
in scope for the narrower "does Pipecat already cover this natively" comparison described in Scope and Acceptance
Criteria 10, distinct from the full stage-by-stage mapping done for the conversation pipeline.)
</content>
