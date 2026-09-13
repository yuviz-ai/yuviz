# Design: Pipecat evaluation — method, evidence rules, and document structure

## Approach
The deliverable is one self-contained document, `.sdlc/evaluate-pipecat/04-evaluation.md`, built from a **fixed
capability ledger derived from the modules that exist**, not from Pipecat's feature list. The obvious alternative —
walk Pipecat's docs and note what we also have — produces a survey that silently omits every home-grown behavior
Pipecat has no vocabulary for (directive parsing, guardrail-count escalation, `_AUDIO_DELAY_S` playback pacing,
no-TTL DID cache), which is exactly the failure `.sdlc/lessons.md` #7 and AC1/AC2/AC3 forbid. So the ledger rows are
enumerated **first**, from `grep`-verified module inventories, and each row must carry a verdict before the document
is complete; a dropped row must be listed as dropped with a reason. Second decision: GitHub is reachable from this
environment (verified — `api.github.com/repos/pipecat-ai/pipecat` → 200, `dograh-hq/dograh` → 200, and
`dograh/.gitmodules` already reads `[submodule "pipecat"] url = https://github.com/dograh-hq/pipecat.git`), so
Dograh's fork-vs-dependency question (AC13a) is answered by **reading the pinned submodule SHA and diffing the fork
against upstream**, not by quoting its README. Third: **no scratch smoke test will be run**, and the document says so
— a Pipecat latency number produced in a local venv, off our Kamailio→FreeSWITCH→vobiz path, would not measure the
criterion it appears to measure, so every latency claim stays explicitly unverified with a named experiment that
would settle it. Fourth: the four Decision criteria are applied as a **scoring block with fixed vocabulary appended
to every recommendation**, so AC11 is satisfied structurally rather than by narrative that has to be re-read.
Fifth, and it constrains all four: **this whole feature directory is written for publication.** `origin` is
`https://github.com/yuviz-ai/yuviz.git`, `api.github.com/repos/yuviz-ai/yuviz` returns `"private": false`, `.sdlc/` is
tracked (`git ls-files .sdlc/`) and `git check-ignore .sdlc/evaluate-pipecat/` exits 1 — so the moment this
directory is committed and pushed it is world-readable, permanently, in history, and the PR diff additionally goes to
CodeRabbit. `.sdlc/evaluate-pipecat/` is committed **as a unit**, so the publication boundary is a property of the
directory, not of the deliverable: `01-prd.md`, `00-request.md`, this design, `02-security.md` and the `*.review.md`
artifacts are published on exactly the same terms as `04-evaluation.md`, and a redaction rule scoped to the
deliverable alone leaks through the sibling that was written before the rule existed. The obvious alternative — keep
the whole feature in scratch and publish only a summary — throws away the artifact's entire purpose (a future "just
use Pipecat" challenger has to be able to read the reasoning). So instead every file in the directory is written or
retrofitted against a **publication boundary rule** (Interfaces #5) rather than redacted after a push: it may state
operational facts that are *already* in a tracked file of this repo, and must express everything sourced from private
operational memory by its **effect** rather than by its private filename, identifier, or constant.

## Changes
| File | Change | Why |
|---|---|---|
| `.sdlc/evaluate-pipecat/04-evaluation.md` (new) | The entire deliverable: sections §1–§10 below, including Appendix A (source ledger) and Appendix B (AC→section trace). **Public artifact** — written to Interfaces #5's publication boundary and gated by Test plan check 6 before it is committed | Matches the `.sdlc/<feature>/NN-*.md` numbering already used by `user-onboarding-hierarchy/` (00-request → 01-prd → 02-design → 03-tasks → 05-review); `04-` is the free implementation-output slot. That directory is tracked and not ignored, so this file is world-readable on push — it is treated as an external publication, not an internal note |
| `.sdlc/evaluate-pipecat/01-prd.md` | **Redaction pass, blocking precondition of check 6.** It carries **7 distinct citations to private memory docs (10 occurrences)** plus one live-DID literal. Each is rewritten the way Interfaces #4 was: cite the tracked file that carries the rule, or state the requirement in the PRD's own words with the `[detail withheld — public repo]` marker. **The private-name → tracked-citation mapping is not reproduced here** — it exists only in the scratch evidence folder (row 3), because this design is committed to the same public repo and naming a file *because* it is being redacted republishes it (Interfaces #5, last bullet). Requirements, ACs and scope are unchanged — only the citation form (lessons #7) | It is committed in the same PR and same directory as the deliverable and is not ignored, so it publishes both the filenames and an inventory of what other private docs exist — the exact category Interfaces #5 excludes. A boundary enforced on one file in a directory that ships as a unit is not enforced |
| scratchpad `pipecat-evidence/` (outside repo, uncommitted) | Working notes: fetched `pyproject.toml`/`.gitmodules`/file listings, `git compare` output for the Dograh fork, URL+SHA+access-date per source, **plus `redaction-map.md`: the single authoritative private-name → tracked-citation mapping** behind each non-negotiable and each redacted `01-prd.md` line, and the deny-sweep filename patterns check 6(b) deliberately does not spell out. This file is the only place those names appear, and nothing under `.sdlc/evaluate-pipecat/` may quote it | PRD Scope permits uncommitted scratch work; keeps raw fetch output out of the deliverable and out of `git status`, and gives a reviewer with memory access a way to re-derive a redacted claim without that mapping being published |

No file under `services/conversation`, `services/vobiz`, `services/campaigns`, `services/webcall`,
`libs/telephony_sdk`, `libs/vad_sdk`, `libs/knowledge_sdk`, `libs/config_sdk` is opened for write. AC9 is satisfied by
construction and verified by the check in Test plan.

## Data
None. No schema, no migration, no `database/*.sql` change.

## Interfaces
Four fixed shapes. Every occurrence in the deliverable uses them verbatim; a reviewer greps for them.

**1. Capability ledger row** (§3, §4, §5) — one row per capability, never per Pipecat feature:

```
| Capability (our module:symbol) | What it does today | Pipecat equivalent (named component) | Verdict | Evidence tier |
```
`Verdict` ∈ `MATCH` | `PIPECAT-SUPERIOR` | `PARTIAL (what's missing)` | `GAP (nothing equivalent)` | `DROPPED (why)`.
`DROPPED` rows are still rows — lessons #7.

**2. Claim tag** — appended inline to every factual assertion about Pipecat, Dograh, or third-party sources:

- `[E1 source-read @ <repo>@<sha>]` — read directly in the source repo at a pinned commit. Static facts only
  (a file exists, a submodule is pinned, a class is exported). **Still not runtime-verified.**
- `[E2 vendor-doc, unverified]` — Pipecat docs, README, changelog, GitHub issue number.
- `[E3 third-party/marketing, unverified]` — comparison articles, Dograh marketing claims.

Any behavioral or performance claim (latency, cold start, audio quality, "handles N concurrent calls") is capped at
`[E2 …, unverified]` or `[E3 …, unverified]` regardless of tier of the source, because nothing was executed. AC4's
default of "unverified" therefore holds everywhere; `E1` narrows *what* is unverified rather than claiming otherwise.

**3. Recommendation block** — closes every stage row group in §4, and each of §6's two services:

```
Verdict: Adopt | Partial adopt (<named stage/component>) | Reject
1 Latency:         better | parity | worse | unknown — <one line, with claim tag>
2 Service count:   -N services | unchanged | +N — <one line>
3 OSS-over-custom: favors adopt | neutral | favors keep — <one line>
4 Scalability:     better | parity | worse | unknown — <one line>
Non-negotiables:   pass | FAILS <named constraint>
Revisit when:      <the specific change in Pipecat or in our requirements that reopens this>
```
Rules enforced by the document's own §9 self-check: `Adopt`/`Partial adopt` requires criterion 1 ≠ `worse` **and**
≠ `unknown` where the stage sits on the media path; `Reject` must name the failing criterion **or** the overriding
non-negotiable. `Revisit when:` is mandatory on every non-adopt block (AC6).

**4. Non-negotiables gate** — the named list every adopt candidate is run through *before* scoring. Each is cited to a
**tracked file in this repo**, never to a private memory doc (Interfaces #5): no synchronous Postgres/Config-Service
call on the media path (`CURSOR.md`); Redis holds routing data only, `did:{did}` written through with no expiry
(`services/config/app.py`, `gateway/include/config/Config.h`, `CURSOR.md`); Gateway/telephony/conversation
responsibility split intact (`CURSOR.md`); the Kamailio dialplan routes only the DID pattern in
`scripts/kamailio/kamailio.cfg.tpl` — cite the template, do not restate the range or any live DID; playback pacing +
final-marker + onset/capture preserved (`services/vobiz/bridge.py` module docstring, `_playback_pacer`,
`_audio_delay_pump`); stateless Gateway (`CURSOR.md`). Where the operative rule exists only in private memory, it is
stated as a behavioral requirement in its own words with no filename — e.g. "an unresolvable DID must not silently
inherit another tenant's agent" — and the private citation stays in scratch.

**5. Publication boundary** — a rule on the *content of every file committed under `.sdlc/evaluate-pipecat/`*, not on
the deliverable alone. The directory ships as one unit in one PR, so the in-scope set is: `00-request.md`,
`01-prd.md`, `01-prd.review.md`, `02-design.md` (this file), `02-design.review.md`, `02-security.md`,
`04-evaluation.md`, and **any artifact a later stage adds to the directory** (`03-tasks.md`, `05-review.md`, further
`*.review.md`). Each is subject to the identical test and the identical literal deny-sweep; no file is exempt for
being an "input" artifact rather than the deliverable. The rule is applied while drafting and enforced by Test plan
check 6.

The test is mechanical: **a fact may be published only if it is already in a tracked file of this repo**
(`git grep -q` finds it) **or in a public external source cited in Appendix A** (for non-deliverable files: cited
inline). Verified examples of each side, so the rule is not abstract: `_AUDIO_DELAY_S` (`services/vobiz/bridge.py`),
the `did:{did}` key (`CURSOR.md`) and the Kamailio dialplan pattern (`scripts/kamailio/kamailio.cfg.tpl`) are already
tracked and may be discussed; `sunrise-hospital` and the demo tenant→DID mappings appear in **zero** tracked files
(`git grep` returns nothing) and may not. Specifically excluded from every file in the directory regardless of how
useful it would be to the argument:

- Tenant identifiers and any tenant→DID mapping, customer names, live DID numbers, phone numbers, emails.
- Any credential, secret, token, key material, connection string, internal hostname, IP or non-public port.
- Routing *fallback* behavior on a cache/DB miss — what happens to an unresolvable or unprovisioned DID. State the
  requirement ("must not silently inherit another tenant's agent"), never the current fallback's effect.
- Failure modes stated as exploitable recipes: pacing, turn-watchdog, barge-in and guardrail behavior are described as
  *invariants to preserve*, not as "here is what breaks and how to trigger it".
- Tuning constants, thresholds and limits that are not already in a tracked file.
- Titles or filenames of private memory docs, and any phrasing that inventories what other private docs exist —
  **including in a passage whose purpose is to redact them.** A file that is itself committed may never name a
  redacted-because-private filename, quote the private naming patterns, or pair a private name with its replacement;
  it refers to the redaction by **count and pointer** only ("N citations redacted — mapping in scratch
  `pipecat-evidence/redaction-map.md`"). This bullet is self-applying: it binds this design, `02-security.md` and
  every `*.review.md` exactly as it binds `01-prd.md`, and it is what makes the fix land once instead of migrating
  one file over each round (lessons #31).

That last bullet is the one `01-prd.md` currently violates, in 10 places across 7 distinct private citations, each
with an operational gloss — which both names the docs and inventories the private set. The list of which files those
are is deliberately absent from this design and from every other committed artifact; it is in scratch
`pipecat-evidence/redaction-map.md`, and the mechanical test in check 6(a0) finds them without anyone having to write
them down. Remediation is specified in Changes and gated as precondition (a0) of check 6 — the boundary is not
satisfied by this design's own compliance while a sibling in the same commit fails it, and equally not satisfied by a
clean `01-prd.md` next to a design that spelled the names out while explaining the cleanup.

Nothing here weakens AC1/AC2/AC3 or lessons #7: a capability whose public description would be too thin still gets its
ledger row, its verdict, and an evidence tier — the row cites the tracked module and symbol, which is where a reader
should look anyway. Likewise a redacted PRD line keeps its requirement, its AC number and its force; only the citation
form changes. If a row or requirement genuinely cannot be argued without a non-publishable fact, it is published
unchanged plus the marker `[detail withheld — public repo]`, and the withheld reasoning goes to scratch. A withheld
detail is never a dropped row and never a dropped requirement.

## Document structure
Sections are the deliverable's table of contents. Each names what gets examined and where the evidence comes from.

**§1 Decision (½ page, first page).** The single explicit statement required by AC5, in the Recommendation-block
shape above, for the pipeline as a whole plus one line each for campaigns and webcall. Nothing else.

**§2 What Pipecat would have to replace — inventory of today.** Cited per module, not generic (Scope bullet 1):
`services/conversation/pipeline.py` (1758 lines — `PipelineConversationHandler`, `on_speech_ended`, `_token_stream`,
`_llm_to_tts`, `_synthesize_sentence_stream`, `_retrieve_context`, `_cancel_event`), `session.py`,
`session_finalizer.py`, `transcript_builder.py`, `fsm.py` (`ConversationFSM`), `transfer_engine.py`
(`TransferDecisionEngine`, `TriggerType`, `DecisionContext`), `guardrails.py` (`GuardrailDetector`,
`GuardrailCounter`), `directives.py` (`DirectiveParser`, `StreamBuffer`, `TransferRequest`), `tools/orchestrator.py`
(`ToolCallOrchestrator`, `_fold_tool_result_into_history`, background tool tasks), `tools/policy_resolver.py`,
`ai_provider_manager.py` + `providers/{stt,llm,tts}`, `fillers.py`, `workflow/runner.py` + `workflow/extractor.py`
(+ `libs/config_sdk/workflow.py`, 675 lines); `services/vobiz/{app,bridge,audio,redis_route}.py` (`VobizCallBridge`,
`_playback_pacer`, `_audio_delay_pump`, `_clear_playback_queue`, `_flush_pending_audio_now`, turn watchdog);
`libs/vad_sdk/{vad,silero_vad}.py` (`EnergyVAD`, `SileroVAD`); `libs/telephony_sdk/{interface,registry,providers/
vobiz.py}`; `libs/knowledge_sdk`; `libs/config_sdk`. This section is the source of the ledger rows — anything listed
here that has no row in §4/§5 is a lessons-#7 violation.

**§3 Pipecat as it actually is.** Read from `pipecat-ai/pipecat` at a pinned SHA, not from marketing: pipeline/frame
model (`Pipeline`, `FrameProcessor`, `PipelineTask`, interruption frames), transports (Daily, SmallWebRTC, Twilio
WebSocket, Telnyx), `SileroVADAnalyzer` + smart-turn, STT/LLM/TTS service classes, `pipecat-flows`, Pipecat Cloud's
SIP story. Includes the known-gap review: issue #3987 (self-hosted production blueprint) and the
cold-start/VAD-gating/SmallWebRTC audio-quality reports named in the PRD — each carried with its tag, none asserted.

**§4 Stage-by-stage mapping (AC1, AC2).** Ledger rows grouped into: transport/SIP bridging · VAD & turn detection ·
STT · LLM orchestration & streaming · directive parsing mid-stream · tool-call orchestration (incl. background tools
and mid-stream interruption) · TTS & sentence streaming · barge-in/cancellation/playback pacing · transfer trigger
detection & execution · guardrail counting & escalation · FSM/session lifecycle · transcript recording &
finalization · filler/prewarm. Every row ends in a verdict; each group ends in a Recommendation block. The
vobiz/vad_sdk rows are mandatory and must not be resolved with "Pipecat has VAD built in" — AC2 explicitly targets
that shortcut, so those rows compare against the specific behaviors in `bridge.py`'s module docstring (pacing,
pre-roll flush ordering, early barge-in, stale-flag failure mode).

**§5 Extension points: per-tenant RAG and runtime config (AC3).** Whether Pipecat has an equivalent seam for
`libs/knowledge_sdk` retrieval (our one `_retrieve_context` call per turn, 3-tier `agent_retrieval_policies`
override) and for `libs/config_sdk`'s immutable `RuntimeConfig` + `config_version` injection at session start
(`provider_config_subscriber.py`). If none: name the concrete thing that would have to be built as a custom
`FrameProcessor` or context aggregator, and who owns its lifecycle.

**§6 Does Pipecat already replace a whole service? (AC10).** Two subsections, each ending in a Recommendation block
whose criterion 2 is stated as an actual service count delta. `services/campaigns`: compared against `pipecat-flows`
and Pipecat's dial-out examples — but the comparison must be against what `originate.py` really does (raw ESL
`bgapi originate` + a persistent `BACKGROUND_JOB` listener mirroring the Gateway's `EslClient`/`EslEventListener`
split) plus `worker.py`, `dnc.py`, `campaign_contacts.py`, since a dial-out example does not cover DNC, contact
lists, pacing, or audit. `services/webcall`: compared against Daily/SmallWebRTC transports and Pipecat client SDKs —
against what `__main__.py` really is (raw-PCM16 WebSocket → gRPC bridge, `ResponseWatchdog`, WAV dump), noting that
adopting a Pipecat transport here only retires the service if the Conversation gRPC contract goes too.

**§7 Dograh reference case (AC13).** Four labeled subsections (a)–(d) matching AC13's four items, each `E1` where
the repo answers it: (a) fork/vendor mechanism — `.gitmodules` names `path = pipecat`, `url =
github.com/dograh-hq/pipecat.git`; record the pinned submodule SHA, the fork's divergence from `pipecat-ai/pipecat`
via GitHub's compare API (commits ahead/behind, files touched), and whether the divergence is telephony-only;
(b) which Pipecat components/transports/services its `/api` imports; (c) what it built instead — its provider
abstraction and telephony integrations, including the specific check the PRD flags: whether its "Vobiz" provider is
the same upstream API as `libs/telephony_sdk/providers/vobiz.py` (compare credential fields, call-initiate signature,
answer-response/WebSocket shape against our `ITelephonyProvider`); (d) at least one lesson feeding §1's scoring —
the fork-pressure question stated as evidence for or against consuming Pipecat as a plain dependency here.

**§8 Adoptable ideas from Dograh (AC14).** A discrete named list — visual workflow builder vs. our
`services/conversation/workflow/` + `libs/config_sdk/workflow.py` (note: this repo already has a shipped workflow
runtime per `CURSOR.md`, so the comparison is against what exists, not against a deferred roadmap doc); its
telephony-provider abstraction vs. `libs/telephony_sdk`'s registry; its MCP server; its hybrid pre-recorded-clip +
TTS-fallback playback. One line each on why it's worth a look, plus the explicit sentence that Dograh is not a build
target — only a source of ideas and production-gap evidence.

**§9 Constraints, non-negotiables, and what a migration would have to preserve (AC7, AC8).** The Kamailio dialplan
constraint (cited to `scripts/kamailio/kamailio.cfg.tpl`) and the hot/cold-path split addressed head-on against
Pipecat's Twilio/Daily/Telnyx-managed telephony: state whether the constraint transfers or name what breaks. If §1 is
Adopt or Partial adopt, the preserve-list (transfer triggers, guardrail thresholds, RAG retrieval policy, transcript
recording, barge-in pacing, Redis-only hot-path DID lookups) is enumerated here as scoping input for a future
migration PRD. Also the self-check: every Recommendation block re-read against the Interfaces rules above.

**§10 Net benefit — the direct answer (AC12).** A prose section, explicitly labeled, no table: the named net
benefit(s) scored against all four criteria, or the explicit "no material benefit, recommend keeping in-house"
naming which criteria were considered and why they did not justify adoption. Placed last but referenced from §1.

**Appendix A — source ledger.** Every source: URL, repo@SHA or doc version, access date, and which claims rest on
it. **Appendix B — AC→section trace.** 14 rows, one per acceptance criterion, naming the section that satisfies it.
**Appendix C — what was not verified and what would settle it.** Every runtime claim, with the specific experiment.

## Risks
- **The ledger is derived from `grep` of module symbols, so a capability implemented inline (not as a named class) can be missed** — mitigate by seeding §2 from the *file* list of the four in-scope areas, requiring every file to appear in at least one row, and listing files with no row as explicit `DROPPED` entries.
- **Third-party performance claims get repeated until they read as fact** — mitigate with the mandatory claim tag: any behavioral/perf claim is capped at `unverified` even when sourced from a pinned repo read, and Appendix C restates each one with its settling experiment.
- **Deciding not to run the smoke test leaves criterion 1 (latency) `unknown` for stages where it is decisive, which the Interfaces rule then blocks from an `Adopt` verdict** — this is intended, not a gap: `unknown` on the media path yields `Reject` or `Partial adopt` with `Revisit when: <named benchmark>`, and Appendix C names the benchmark (A/B against the SIPp harness in `loadtest/sipp/`, same DID and providers) rather than leaving a future reader to invent one.
- **The Dograh fork diff can be large enough that "telephony-only?" is not answerable cheaply** — bound it: compare API only (files-changed list and commits-ahead count), no line-by-line review; if the shape is not evident from the file list, record that as an open item in Appendix C instead of guessing.
- **Network access to GitHub is available now but is not guaranteed for the reader** — every `E1` claim records repo@SHA so it is re-checkable later, and raw fetch output is kept in scratch so the deliverable never depends on a live fetch to be read.
- **The whole feature directory lands in a public repo, so anything drawn from private operational memory in *any* of its files becomes world-readable and unretractable** (git history keeps it even after a later delete, and the PR diff also reaches CodeRabbit) — mitigate with Interfaces #5's already-tracked-or-public test applied across every file in `.sdlc/evaluate-pipecat/`, and Test plan check 6 as a hard pre-commit gate over the directory; the deliverable stays in scratch and `01-prd.md`'s redaction pass must land before check 6 can pass, so the redaction decision happens before the first commit rather than after a push.
- **Redacting `01-prd.md` after review could be read as re-litigating an approved artifact, or could silently soften a requirement** — mitigate by bounding the pass to citation form only: no requirement, AC or scope bullet is removed or reworded in force, each redacted citation is replaced by a tracked-file citation or `[detail withheld — public repo]`, and the original text is kept in scratch so the diff is auditable (lessons #7 — nothing is dropped to satisfy a rule).
- **The publication boundary can be over-applied and hollow out the documents** — an evaluator who redacts defensively produces a survey again — mitigate with the `[detail withheld — public repo]` marker: the row, verdict and evidence tier (or the requirement and its AC number) are always published, only the specific non-publishable fact is withheld, and check 2 (ledger completeness) still fails if the row is absent. Withheld ≠ dropped.
- **The documents are discoverable only if someone looks in `.sdlc/`** — mitigate by making §1 self-contained (verdict + revisit-conditions readable without the other nine sections), which is what a future "just use Pipecat" challenger needs; no new index file is added. Note this cuts against the risk above and is deliberately the weaker concern: obscurity is not a control, so §1 is written to be read by anyone, including a reader outside the company.

## Test plan
Not software — the checks are document reviews, each with a stated failure mode (lessons #12).

1. **Read-only proof (AC9).** `git status --porcelain -- services/conversation services/vobiz services/campaigns services/webcall libs/ database/ gateway/` must be empty, and the only new or modified paths anywhere are under `.sdlc/evaluate-pipecat/`. Fails if any in-scope file was touched — including an accidental `__pycache__` write, which this check will surface.
2. **Ledger completeness (AC1, AC2, AC3).** For each file listed in §2, grep §4/§5 for its name: zero hits is a fail unless it appears as `DROPPED (<reason>)`. Specifically fails if `bridge.py`'s pacing/pre-roll behaviors, `guardrails.py`'s counting, `directives.py`'s mid-stream parsing, `libs/vad_sdk`, `libs/knowledge_sdk` or `libs/config_sdk` have no row. This check can fail — the first draft of a stage-by-stage mapping written from Pipecat's docs would fail it on vad_sdk and config_sdk.
3. **Untagged-claim sweep (AC4).** Every sentence asserting a Pipecat/Dograh/third-party fact must end in `[E1|E2|E3 …]`. Fails on any bare assertion, and on any perf/behavioral claim tagged `E1` without `unverified`.
4. **Recommendation-block conformance (AC5, AC6, AC11, AC12).** Every block has all four numbered criteria, a Non-negotiables line, and — if not `Adopt` — a `Revisit when:`. `Adopt`/`Partial adopt` with criterion 1 = `worse` or `unknown` on a media-path stage is a fail. §1 must contain exactly one verdict sentence; §10 must exist, be prose, and not defer to a table.
5. **Dograh coverage (AC13, AC14).** §7 has subsections labeled (a)–(d) with the submodule SHA and fork-divergence numbers present as `E1`; §8 has all four named items plus the "not a build target" sentence. Fails if any item is only a passing mention.
6. **Publication-boundary sweep (pre-commit gate, blocking).** **Scope: every file that will be committed under
   `.sdlc/evaluate-pipecat/` — enumerated as `git status --porcelain -- .sdlc/evaluate-pipecat/` plus
   `git ls-files .sdlc/evaluate-pipecat/`, i.e. `00-request.md`, `01-prd.md`, `01-prd.review.md`, `02-design.md`,
   `02-design.review.md`, `02-security.md`, `04-evaluation.md` and anything a later stage adds.** Not the deliverable
   alone: the directory is pushed as a unit, so a clean `04-evaluation.md` next to an unredacted `01-prd.md` publishes
   the same secrets. Run against the finished drafts *before* `git add`, over the whole file set:

   - **(a0) Remediation precondition — checked, not assumed.** `01-prd.md`'s redaction pass (Changes, row 2) must
     already be applied. Verify mechanically: for every `` `*.md` `` token in `01-prd.md`, `git ls-files --error-unmatch`
     must resolve it (so `.sdlc/lessons.md` passes and a private memory-doc name does not). Two exemptions, both
     mechanical: a token naming a sibling in `.sdlc/evaluate-pipecat/` (tracked as of the same commit), and a token
     naming a scratch path under `pipecat-evidence/` (never committed, and pointing at it is the sanctioned way to
     refer to a redaction). Any other unresolved token is a blocking fail. Sibling artifacts (`00-request.md`, `02-security.md`, the `*.review.md` files) get the same
     treatment if the sweep finds hits in them. This precondition can fail today and does — `01-prd.md` currently
     carries **10 such tokens across 7 distinct private docs**, none of which `git ls-files` resolves. Which docs
     they are is not recorded here or in any other committed file (Interfaces #5, last bullet); the check finds them
     mechanically, and the mapping to their replacements is in scratch `pipecat-evidence/redaction-map.md`.
   - **(a) Already-tracked-or-public-or-withheld.** In every in-scope file, each operational fact asserted about our
     own system is either found by `git grep -q` in a tracked file, or carries a public source citation, or is marked
     `[detail withheld — public repo]`.
   - **(b) Literal deny-sweep**, run identically over every in-scope file. The private-doc arm is stated as the
     *test*, never as a pattern inventory (Interfaces #5, last bullet): every backticked `*.md` token must resolve
     under `git ls-files --error-unmatch`. This is strictly stronger than a glob list — it also catches a private doc
     whose name fits no known shape — and it is why the glob list itself lives in scratch
     `pipecat-evidence/redaction-map.md` rather than here. Remaining arms: tenant slugs, four-digit DID literals,
     `@`-addresses, IPs, and any `KEY=`/`token`/`secret`/`password` fragment — each hit either resolves to a tracked
     file or is removed.
   - **(c)** No sentence in any in-scope file describes what happens on a routing miss.

   This check can fail, and has failed twice in different files: the Interfaces #4 non-negotiables list previously
   cited six private memory-doc filenames, and this design's own Changes row and Interfaces #5 previously spelled out
   the very names `01-prd.md` is being cleaned of — a passage that explains a redaction is in scope for that
   redaction. `01-prd.md` still fails (a0)/(b) until its pass lands. Because the sweep is scoped to *every* file in
   the directory including this one, running it in file order is what stops the finding from relocating instead of
   closing. Only after all four parts pass for all in-scope files does `04-evaluation.md` move into
   `.sdlc/evaluate-pipecat/` and the directory get committed.
