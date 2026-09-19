# Security review: .sdlc/call-flow-executor/02-design.md

(Round 1 below; round 2 re-review appended at the end. The round-1 finding text is kept verbatim
as the record — see "Round 2" for per-finding status.)
VERDICT (round 1): RED — see the round-2 section at the bottom for the current verdict.

Design-stage threat model. The flow-load read path, the Redis namespacing and the `agent`-node
handoff hold up under tracing. The failures are concentrated on the two surfaces the design treats
as plumbing rather than as security boundaries: the **start voice** (`tts_config_id`) and the
**caller-entered digits**.

## Findings

1. [critical] The start voice has no server-side channel into the runner, so the only value the
   runner can read is the author-controlled `graph.start.tts_config_id` — `.sdlc/call-flow-executor/02-design.md`
   "Interfaces → SDK/Conversation" + "Node semantics" (`CallFlowRunner.__init__`, `SetVoice`)
   The design asserts `start` emits `SetVoice(resolved_tts_config_id)` and that the server-validated
   `resolved_tts_config_id` is the only honoured path. But the specified constructor is
   `CallFlowRunner(graph, *, variables=None)` — `resolved_tts_config_id` lives on the `CallFlow` DTO,
   which the runner never receives, and `open()` is a pure function of the graph. Meanwhile
   `libs/config_sdk/callflow.py:100,138,268` keeps `start.tts_config_id` inside the parsed
   `CallFlowGraph`, i.e. the unvalidated author-supplied id is the *convenient* one and the validated
   one is unreachable. `IConfigProvider.get_provider_config(provider_id)` takes no tenant argument at
   all (`libs/config_sdk/providers/cache_aside.py:173`, Redis key by id) and `ProviderConfig` carries
   no `tenant_id` (`libs/config_sdk/models.py:161-170`), so nothing downstream can catch the mistake.
   Attack: a tenant_admin in tenant A (or anyone who can author/publish a flow in A — the console
   already allows it) sets `start.tts_config_id` to a `provider_configs` UUID belonging to tenant B,
   obtained by guessing or from any prior leak. Every inbound call on A's DID then synthesizes through
   B's TTS provider config — B's engine, voice and `api_key_ref`, resolved to B's actual secret by
   the conversation-side registry — disclosing B's voice/provider configuration and billing B's
   vendor account for A's traffic.
   Fix: make `resolved_tts_config_id` a required keyword on `CallFlowRunner.__init__` (or pass the
   `CallFlow`), state in the design that the runner MUST NOT read `graph.start.tts_config_id`, and
   add the negative test: a graph whose `start.tts_config_id` names another tenant's `tts` row must
   emit `SetVoice(None)`.

2. [high] Caller-pressed digits are logged in cleartext today, and the design's only change to that
   line is to add forwarding — `services/vobiz/bridge.py:342-343`, design Changes table row
   `services/vobiz/bridge.py`
   The existing branch is `log.info("vobiz: dtmf digit=%s call=%s", event["dtmf"]["digit"], self.call_uuid)`.
   The design repurposes this branch and never says the digit must stop being logged; it additionally
   specifies "a digit outside `callflow.DTMF_KEYS` is logged and ignored" in the runner.
   Attack: a caller enters a card PAN or account PIN into a `collect` node (the node type exists for
   exactly this). Each digit is written to vobiz's INFO log with the call uuid, so anyone with log
   read access — operators, a log-shipping pipeline, a support engineer, or an attacker who gets the
   log store — reconstructs the full secret in order. Nobody needs to touch the database.
   Fix: log presence only (`dtmf received call=%s`), never the value, in `bridge.py`, in the runner's
   invalid-digit branch, and in any `DtmfDigit`/`GatewayMessage` trace logging on the servicer side.

3. [high] Collected digits reach the conversational agent's variable dict and are persisted
   unredacted; no `sensitive`/no-log concept exists anywhere — design "Node semantics" (`collect` →
   `Store` → `update_variables`) + `services/conversation/pipeline.py:1469`,
   `services/conversation/transcript_builder.py:359-371`
   `initial_variables` is merged over `WorkflowRunner`'s `variables`; `extracted_variables()`
   (`services/conversation/workflow/runner.py:77-80`) returns every variable whose name is declared in
   the agent's workflow graph, and `transcript_builder` writes that dict into
   `conversation_sessions.extracted_variables` as jsonb. Templated prompts also render variables, so
   the value is sent to the LLM vendor and can be read back into the transcript.
   `libs/config_sdk/callflow.py:92,162-168` has no sensitivity flag on `collect`.
   Attack: an IVR asks for a 16-digit card or a PIN; the flow author names the variable the same as a
   declared workflow variable (the normal case — that is how the data is meant to be used). The PAN
   lands in plaintext in a Postgres column that is read by the transcript/session UI and any tenant
   user with transcript access, and is shipped to a third-party LLM.
   Fix: add a per-`collect` `sensitive: bool` (default false) to the SDK node model; when set, `Store`
   keeps the value only in the runner's own memory for edge matching, excludes it from
   `initial_variables` handed to the delegate and from `extracted_variables`, and any surviving copy
   is masked to the last 4 digits.

4. [high] A menu timeout and a keypress mutate the same `CallFlowRunner` from two different tasks
   with no serialization — design "Timing" (`asyncio.Task` per `Listen`) vs. "Interfaces →
   Conversation" (`push_dtmf` → `on_dtmf` from the servicer outer loop)
   The timer task is the thing that will call `runner.on_timeout()` and then execute the resulting
   actions (TTS synthesis is `await`ed inside), while `on_dtmf` runs the same action-execution loop
   from the servicer's outer loop. Both mutate `_node`, `_retries`, `_buffer`, `_transitions` across
   `await` points. The user's binding "no lock" decision is about per-flow/per-caller locks across
   calls; it does not make two tasks inside one session safe.
   Attack: a caller presses a digit in the same tick the menu timer fires (trivially reproducible on a
   short `timeout_ms`, and adversarially by keying during the prompt tail). The runner advances twice:
   the call can take the matched edge *and* the timeout edge, double-increment retries, or emit
   `Handoff` and `Hangup` for the same turn — landing the caller on the wrong node, or on an agent
   handoff that is then hung up. Bounded to one call, cheap to fix in design, invisible in unit tests
   that drive the runner from a single task.
   Fix: the timer task must only enqueue a `("timeout", node_id)` sentinel on the same channel the
   servicer loop already reads (or on `msg_q`); *all* runner mutation and action execution happens in
   exactly one task. Assert in the handler test that a digit and an expired timer in the same tick
   produce one transition, and have the sentinel carry the node id so a stale timer is dropped.

5. [medium] A `collect` node can overwrite a seeded call-context variable, and the design states this
   as intended ("last-write-wins over seeded call-context keys for free") — design "Node semantics",
   `services/conversation/pipeline.py:699-707`, `libs/config_sdk/callflow.py:162-165`
   Seeded keys are `caller_number`, `called_number`, `direction`, `agent_name`, `business_name`,
   `current_date`, `current_time`; `collect.variable` is validated only for non-emptiness.
   Attack: a flow whose `collect` writes `caller_number` lets the *caller* set it to any digits they
   key in. The conversational agent's prompt then renders the caller's own claimed number, defeating
   `_build_caller_number_context()`/`_message_reads_back_phone_number()`
   (`services/conversation/pipeline.py:137-240`), the read-back confirmation used before booking-tool
   calls — so a caller can get a booking/lookup made against someone else's number. (Verified *not*
   exploitable for transfer caller-id: `transfer_engine` uses the ctor's `self._caller_number`, not
   the variable.)
   Fix: reject `variable` names in `CALL_CONTEXT_VARIABLES` at publish-time validation in
   `libs/config_sdk/callflow.py`, and merge `initial_variables` *under* the call-context keys rather
   than over them for that reserved set.

6. [medium] `resolve_call_flow()` never re-checks that the returned `CallFlow.tenant_slug` is the
   tenant it asked for — design "Interfaces → Conversation" (`resolve_call_flow`), `_FLOW_CACHE` key
   The payload carries `tenant_slug` and the design uses it nowhere. Every layer's correctness then
   rests on one thing: that `callflow:{tenant_slug}:{id}` was written under the matching path tenant.
   Attack: anything that can write that Redis key — a Config-side bug in a future `_runtime_cache_key`
   caller, an operator replaying a dump into the wrong namespace, or an attacker with Redis access
   (the config plane already trusts Redis unauthenticated in the compose setup) — plants tenant B's
   graph under tenant A's key. A's inbound callers then hear B's prompts and walk B's menu tree. One
   comparison turns this from silent to impossible.
   Fix: in `resolve_call_flow()`, `if flow.tenant_slug != runtime_config.tenant.slug: log.error(...); return None`;
   key `_FLOW_CACHE` on the *requested* slug, never on the payload's.

7. [medium] A single caller can force ~200 TTS syntheses per call, unmetered — design Risks
   ("`_transitions` budget of `callflow.MAX_NODES` (200)"), "Node semantics" (`menu` replay)
   The budget caps transitions, not cost, and nothing caps concurrent calls per DID or caches
   synthesized prompt audio. Each replay is a fresh `ITTS` call on the tenant's paid provider config.
   Attack: an unauthenticated PSTN caller dials the DID and sits on a menu pressing an unmatched key
   (or letting it time out) until the budget trips — ~200 paid syntheses per call — then redials in a
   loop from a SIP source. This is a direct spend amplifier on the tenant's TTS vendor, reachable by
   anyone who knows the phone number.
   Fix: memoize synthesized PCM per `(tts_config_id, prompt_text)` for the life of the call (prompts
   are static strings, so a replay costs nothing after the first), and state a replay cap per node
   independent of `MAX_NODES`.

8. [low] `MAX_NODES` is reused as two different limits — `libs/config_sdk/callflow.py:44,239` vs.
   design Risks
   In the SDK it is the authoring cap on node count; the design reuses the same constant as a
   per-call transition budget. A future product decision to allow larger flows silently raises the
   runtime budget too (and vice versa).
   Attack: no attacker; this is a maintenance trap flagged only because the runtime budget is the
   stated DoS control for a looping flow and it would move without anyone deciding to move it.
   Fix: a separate `MAX_FLOW_TRANSITIONS` constant.

**Blocks the build stage:** findings 1, 2, 3 and 4. Each is a design-text change (a constructor
argument, a log line, a node field plus its propagation rule, and moving timeout handling onto the
single consuming task) — all far cheaper now than after the runner and handler exist. 5, 6 and 7
should land in the same design revision; 8 is a nit.

## Verified controls

- **Flow-load read path is genuinely Tier 2.** `tenant_scoped_router` at
  `services/config/routers/call_flows.py:29-33` really does carry
  `Depends(bind_path_tenant)` + `Depends(require_path_tenant_access)`, and `bind_path_tenant`
  (`services/config/deps.py:116-125`) sets the RLS target from the path segment before any endpoint
  dependency runs. A `call_flow_id` in hand is not sufficient to load a flow.
- **`_authorize_flow()` really is the shape the design refuses.** `services/config/routers/call_flows.py:59-70`
  fetches the row with `platform_scoped=` *first* and then `set_target_tenant(flow["tenant_id"])` —
  it would pin RLS to the row's own tenant. Not using it on the runtime route is correct, not
  cosmetic.
- **RLS is a real second layer, not a mirror of the URL.** `current_tenant()`
  (`libs/tenancy/session.py:71-80`) returns `caller` whenever the caller has a tenant of their own,
  so a tenant-B admin hitting `/tenants/A/...` is pinned to B by Postgres regardless of the path. The
  design's reliance on the platform-scoped (`tenant_id IS NULL`, `role='viewer'`) conversation
  service account for the target to be honoured is consistent with lesson 24.
- **`call_flows`, `agents` and `provider_configs` all have `ENABLE`+`FORCE` RLS with
  `tenant_id = current_setting('app.tenant_id')`** (`database/rls.sql:117-135, 463-472`), so the
  design's claim that the `agent_slugs` and voice-validation joins are scoped "by RLS, not a WHERE
  clause we could forget" holds for every table in that join.
- **No `platform_scoped` bypass is introduced.** `get_published_for_runtime()` is specified with
  `tenant_conn` only and no `platform_conn` variant; grep confirms no existing caller would need one.
- **Cross-tenant `agent` handoff is structurally unreachable.** The conversation side receives only
  `agent_slugs[...]`, built under the flow tenant's RLS, and resolves it via
  `resolve_handler_deps(tenant_slug, agent_slug)` (`services/conversation/agent_resolver.py:37-42`) —
  a slug-scoped, tenant-first lookup with a never-raises contract. A flow in A naming B's `agent_id`
  yields an absent key, i.e. degradation, not a handoff. (Caveat, not a finding: the handoff closure
  must take `tenant_slug` from `runtime_config.tenant.slug` and nothing else.)
- **`ProviderConfig` genuinely cannot self-validate.** No `tenant_id` field
  (`libs/config_sdk/models.py:161-170`) and `get_provider_config(provider_id)` takes no tenant
  argument — so the design is right that the check must live server-side in Config. That is finding 1's
  premise, and the premise is sound; only the plumbing is missing.
- **Redis key namespacing is collision-free for the read path.** `callflow:{tenant_slug}:{call_flow_id}`
  is derived from the path segment, distinct from `agent:{t}:{slug}`, and `did:{did}`
  (`services/config/phone_numbers.py:61-62`) is untouched in shape and TTL policy.
- **Nothing cached here is a resolved secret.** The payload is ids, slugs, status and the graph; the
  only credential-adjacent value anywhere near it is `api_key_ref` (a path, not a key), which the
  `services/config/cache.py` docstring contract explicitly permits.
- **404-for-everything on the runtime route.** Wrong tenant, unknown tenant, soft-deleted,
  unpublished and `outbound` all return an identical bare 404, with the distinguishing reason logged
  server-side only — satisfies lesson 2, no existence oracle on a `call_flow_id`. The `_parse_id()`
  400 is shape-based and tenant-independent, so it leaks nothing.
- **Per-session state isolation.** The runner holds only per-session state; test 7 asserts two runners
  over one cached `CallFlowGraph` share no mutable state, which is the right failure mode to target
  (in-place node mutation).
- **Timer lifecycle ownership.** One owning `set`, cancelled on node transition and in
  `on_session_end`, no thread pool, plus test 15 asserting `asyncio.all_tasks()` returns to its
  pre-call set — lesson 26 satisfied. (Ownership is fine; finding 4 is about *which task* the timer's
  callback mutates state from, which is a separate question.)
- **Wire change is backward compatible.** `DtmfDigit dtmf = 9` is the next free oneof arm; older peers
  fall into the existing `WhichOneof -> None` "Unexpected" path. `ServiceMessage` unchanged.
- **The `out_responses` AttributeError trap is correctly identified and fenced** (explicit class
  attribute on `echo.py` and `pipeline.py`, `getattr` default, plus a tripwire enumerated
  mechanically rather than hand-listed — lessons 19 and 29).

Count: 14 controls verified as genuinely holding.

---

# Round 2 (revised design, re-reviewed)
VERDICT (round 2): RED — one high open. Superseded by round 3 below.

Each round-1 blocker was checked against the revised design's *interfaces*, not its prose, because
the round-1 critical was precisely a control asserted in prose and unreachable in the interface.

## Status of the round-1 blockers

1. **[was critical] Start voice — materially fixed, one open gap (see finding 9).**
   Genuinely fixed: `CallFlowRunner.__init__(graph, *, tts_config_id: str | None, variables=None)`
   now carries the id as a keyword **with no default** (design "Interfaces → Conversation"), the
   `start` row reads `SetVoice(self._tts_config_id)` with "**Never** `graph.start.tts_config_id`",
   and the new paragraph states the MUST-NOT rule and the reason the required keyword *is* the
   control (a future call site cannot omit it and fall through to the graph). Test 3a's negative case
   is the right one: a graph whose `start.tts_config_id` differs from the constructor value must
   still emit `SetVoice(None)` — asserting on the emitted action, not on a comparison.
   On the tripwire's enforceability: a source grep over `runner.py` that permits `self._tts_config_id`
   and rejects any other `\.tts_config_id` is enforceable and would catch the realistic slip
   (`self._graph.start.tts_config_id`). It is evadable by `getattr(node, "tts_config_id")`, so it is
   belt, not braces — the required keyword remains the actual control. That split is fine, but the
   tripwire's *scope* is what finding 9 is about.

2. **[was high] Digit logging — fixed.** The `services/vobiz/bridge.py` Changes row now says the
   existing line is changed, and the design quotes the before/after
   (`"vobiz: dtmf digit=%s call=%s"` → `"vobiz: dtmf received call=%s"`) with the explicit note that
   this is a change to a line that exists today, not a new forward beneath it. The rule covers all
   three legs I named, including the one I asked about: "the servicer's `dtmf` case and any
   `GatewayMessage` trace logging log the arm name and `session_id` only". Partially-redacted forms
   are banned by name (no last-digit, no length-only running log), and the grep tripwire spans
   `bridge.py`, the servicer and the callflow package. Test 16's `caplog` assertion would genuinely
   fail against today's code — `services/vobiz/bridge.py:343` interpolates the digit at INFO, so a
   `"7"` event emits a record containing `7` right now. (Minor: scope that assertion to the branch's
   own records; a session id containing `7` would make it flaky, not wrong.)

3. **[was high] Persisted digits — fixed for flagged collects; the default is an acceptable
   documented residual.** The property split severs every path I named in round 1, and it severs
   them at a boundary rather than at each call site: `runner.variables` (the property the handler
   reads to build `initial_variables`) omits sensitive keys, so the value never reaches
   `WorkflowRunner.variables`, never reaches `extracted_variables()`
   (`services/conversation/workflow/runner.py:77-80`), and never reaches
   `conversation_sessions.extracted_variables` (`services/conversation/transcript_builder.py:359-371`).
   `flow_variables` is scoped to in-flow rendering, and in-flow rendering does **not** reach the LLM
   vendor: the IVR handler does no LLM/STT/transcript work before handoff, and rendered prompt text
   goes only to `ITTS` — verified that none of `services/conversation/providers/tts/*.py` logs the
   text it synthesizes (they log voice/speed only). Test 14b has a live negative control, which is
   what makes it able to fail.
   On the `false` default: I accept it. AC 24's whole purpose is to hand collected values to the
   agent, an always-on default would break that, and the design states the residual plainly rather
   than hiding it — plus the admin-ui checkbox means an author can actually engage the control. See
   finding 11 for the one cheap thing that would make the default safer without changing it.

4. **[was high] Timeout/keypress race — fixed.** One writer by construction: `on_dtmf()` and the
   timer task only `put_nowait` a `_FlowEvent`; all runner mutation and all action execution happen
   in the per-session driver task. It does not reintroduce the `asyncio.wait` item-loss class — the
   driver consumes a single private `asyncio.Queue` with `get()`, and the two-persistent-futures rule
   still governs the servicer loop, which is a different queue pair. The stale-`node_id` drop is the
   right shape: it makes timer cancellation best-effort rather than load-bearing, which removes the
   second race I would otherwise have flagged (a cancel that loses to an already-queued expiry).
   Teardown: the driver lives in the same owning `set` as the timers and is cancelled in
   `on_session_end`. Test 14a drives the real driver task and says so explicitly — an inline
   `on_timeout()`/`on_dtmf()` version could not fail.

## New findings (round 2)

9. [high] The construction site that must pass the validated id is never named, and the Changes table
   still gives `resolve_call_flow()` a return type that does not carry it — design Changes table row
   `services/conversation/callflow/resolver.py` (`-> CallFlowGraph | None`) vs. "Interfaces →
   Conversation" (`-> tuple[CallFlowGraph, CallFlow] | None`)
   No line anywhere in the design writes the construction expression
   (`CallFlowRunner(graph, tts_config_id=flow.resolved_tts_config_id, ...)`), and the
   `services/conversation/__main__.py` row still reads only "on a graph, return
   `CallFlowConversationHandler(...)`". The grep tripwire is scoped to the runner module, so it does
   not watch the call site. If the implementer follows the Changes table, `resolve_call_flow` hands
   back a graph and nothing else — and then the required keyword, which is the control, forces them
   to supply the one id that *is* in scope: `graph.start.tts_config_id`. The required keyword turns
   "might use the wrong value" into "must find a value", which is an improvement only if the right
   value is in scope at that point.
   Attack: identical to round-1 finding 1 — a tenant_admin in A sets `start.tts_config_id` to a
   `provider_configs` UUID in B; calls on A's DID synthesize through B's provider config and secret
   (`get_provider_config(provider_id)` is tenant-unscoped, `libs/config_sdk/providers/cache_aside.py:173`).
   Lesson 6: a fix not specified well enough to implement correctly is still open. Rated one below
   round 1 only because the authoritative Interfaces block is now right and the negative test exists.
   Fix: correct the Changes row to `-> tuple[CallFlowGraph, CallFlow] | None`, write the construction
   expression into the `__main__.py` row, and widen the grep tripwire from the runner module to
   `services/conversation/callflow/` plus the `handler_factory` block in `__main__.py`.

10. [low] The `Store`-time masking sentence contradicts the no-partial-forms rule, and "last 4" is
    the whole secret for the two shortest cases — design "Sensitive collects never leave the runner"
    ("masked to the last 4 characters at the point of `Store`") vs. "Digit values are never logged"
    ("Nothing constructs a redacted-but-recoverable form either").
    Attack: a support-visible masked copy of a 4-digit PIN or a 6-digit OTP is the credential itself,
    reachable by any tenant user with the surface that copy lands on. No consumer is named for the
    copy, so today it protects nothing and only creates the hazard.
    Fix: delete the sentence, or name the consumer and specify "at most the last 4, and only when the
    value is longer than 6 characters".

11. [low] Nothing warns an author who leaves `sensitive` unchecked on a collect that is plainly
    taking a secret — design "Sensitive collects never leave the runner" (accepted `false` default)
    The residual is documented and the default is right, but the realistic failure is a well-meaning
    author, not an attacker: a `collect` prompted "please enter your card number" with the box
    unchecked persists the PAN to `conversation_sessions.extracted_variables` and renders it into an
    LLM prompt, exactly as before. `libs/config_sdk/workflow.py:556` already has a
    `graph_warnings()`-style non-blocking warning mechanism, so this is a small, existing seam.
    Fix: a publish-time warning (not an error) when a `collect` with `sensitive=False` has a
    prompt/name matching PIN / CVV / card / OTP / passcode.

12. [low] If the driver task dies, the caller gets silence rather than a hangup — design "Timing"
    (flow driver task)
    The `try/except Exception → Hangup("flow_error")` wraps the action loop, which now runs *inside*
    the driver task; nothing is specified for the driver task itself exiting (a `BaseException`, a
    bug outside the wrapped region, or a cancellation that loses a race with session teardown). The
    queue then fills with events nobody reads and the call sits open until the gateway's own timeout.
    Attack: no attacker — a caller-visible availability hole and a channel held open per occurrence,
    which is why it is worth one line now.
    Fix: attach a done-callback that, on unexpected completion, emits the same
    `Hangup("flow_error")` egress; and extend test 15 to assert the driver task is gone too, not just
    the `Listen` timers.

13. [low] The rendered prompt is the one remaining route a sensitive value can travel, and the
    never-logged rule does not cover it — design "Digit values are never logged, anywhere on this
    path" (enumerates digit values at three sites)
    In-flow rendering via `flow_variables` puts the value into `Speak.text`, which goes to the TTS
    vendor — inherent to a confirm-back prompt and not a defect. But the ban is written against
    *digit values*, so a future `log.debug("callflow: speak=%r", text)` is not covered by the rule or
    by the `digit=%s` grep tripwire, and it would re-leak the value in full.
    Attack: an operator adds that debug line; from then on every PIN keyed into a confirm-back flow
    is in the conversation service log, readable by anyone with log access.
    Fix: state the rule as "no digit value and no rendered prompt text from a flow with a sensitive
    collect is ever logged", and note the TTS-vendor exposure in `docs/call-flows.md`.

## Findings 5-8 (round 1) — deliberately not fixed
Unchanged and still open by the user's call, which is the correct process: mediums and lows are
reported for a decision, not auto-fixed. Recorded here so the record stays complete:
5 [medium] `collect` can overwrite seeded call-context keys (`caller_number` et al.);
6 [medium] `resolve_call_flow()` does not re-assert `CallFlow.tenant_slug` against the requested
tenant; 7 [medium] ~200 unmetered TTS syntheses per call, no prompt-audio memoization;
8 [low] `MAX_NODES` serves as both the authoring cap and the runtime transition budget.
Note that finding 6 is now one line from free: the revised `resolve_call_flow()` signature returns
the `CallFlow`, so `flow.tenant_slug` is already in hand at the seam where the check belongs.

## The architect's two disclosures

- **24 → 26 files is the minimum the fix required.** `libs/config_sdk/callflow.py` is unavoidable —
  the `sensitive` flag has to live on the node model that `parse_graph()`/`to_dict()` round-trip, and
  that file is where `DTMF_KEYS`/`MAX_COLLECT_DIGITS` already live. `admin-ui/components/callflow/CallFlowPanel.tsx`
  is one checkbox, and it is the difference between a control and a field: a flag no author can set
  never engages (the lesson-22 shape — check where the thing *lands*, not just where it is
  authorized). Neither addition brings a new endpoint, a new DDL statement or a new trust boundary,
  which is the growth I would have objected to.
- **The Open Question 3 correction is sound; not a finding.** Verified at
  `libs/config_sdk/callflow.py:265`: `terminator=str(d.get("terminator") or "#")`. A blank, empty or
  absent terminator is coerced to `"#"` before the graph is ever parsed, so the "no terminator" case
  the question proposed a runtime rule for cannot reach a parsed graph — the question was answered by
  existing code, and correcting the reasoning rather than shipping an unreachable runtime branch is
  the right call. The only consequence is that `"#"` is always a terminator and therefore cannot be
  collected as data; that has no security impact, and it is worth one sentence in
  `docs/call-flows.md`.

## Verified controls (cumulative)
The 14 from round 1 all still hold — nothing in the revision weakened the read path, the RLS
posture, the slug namespacing, the uniform 404, the handoff shape or the `out_responses` fence.
Newly verified in round 2:

15. **The validated voice id reaches the runner by a required keyword with no default**, so omission
    is a `TypeError` at construction rather than a silent graph fallback, and `SetVoice` carries that
    value only (design "Interfaces → Conversation", `start` node-semantics row).
16. **Presence-only digit logging**, specified as a change to the line that exists today, covering
    vobiz, the runner's invalid-digit branch and the servicer/`GatewayMessage` trace leg, with
    partially-redacted forms banned by name and a `digit=%s` grep tripwire.
17. **The sensitive/non-sensitive split is a property boundary**, not a per-call-site filter, and it
    severs all four paths named in round 1 (`initial_variables` → `WorkflowRunner.variables` →
    `extracted_variables()` → the jsonb column), with in-flow rendering confirmed not to reach the
    LLM vendor and TTS providers confirmed not to log synthesized text.
18. **One writer per runner by construction**: private `_FlowEvent` queue, driver task as sole
    mutator, stale-`node_id` timeout dropped, no `asyncio.wait` item-loss class reintroduced, driver
    in the same owning `set` and cancelled in `on_session_end`.
19. **`parse_graph()` coerces a falsy terminator to `"#"`** (`libs/config_sdk/callflow.py:265`), which
    is what makes the Open Question 3 correction correct.

Count: **19 controls verified as genuinely holding** (14 from round 1, 5 new).

## What blocks the build stage
**Finding 9 blocks.** It is the round-1 critical's path, re-reachable through a stale Changes-table
row and an unnamed construction site; the fix is correcting that row, writing the construction
expression into the `__main__.py` row, and widening the grep tripwire's scope. Findings 10-13 are
lows and do not block — 10 and 13 are single-sentence edits worth folding into the same revision.

---

# Round 3 (final)
VERDICT: GREEN — no critical or high finding is open. Nothing blocks the build stage.

## Round-2 items re-checked

1. **[was high, finding 9] Fixed — all three parts landed, and the answer to the question that
   matters is no.**
   - Changes row, `resolver.py` (design:52) now reads
     `resolve_call_flow(runtime_config, config) -> tuple[CallFlowGraph, CallFlow] | None`, matching the
     Interfaces block (design:193-196), with the clause "the `CallFlow` is what carries
     `resolved_tts_config_id` to the construction site" — so the row that disagreed no longer
     disagrees, and it says *why* the second element exists.
   - Changes row, `__main__.py` (design:57) now spells out `resolved = await resolve_call_flow(...)`,
     `graph, flow = resolved`, and construct **exactly**
     `CallFlowRunner(graph, tts_config_id=flow.resolved_tts_config_id, variables=<call-context dict>)`.
     `flow` is therefore in scope at the one site the required keyword must be satisfied, and the row
     ends "the only place the validated voice id is passed, written out so there is nothing to infer".
   - Tripwire scope (design:117-119) now greps **all of `services/conversation/callflow/` and
     `__main__.py`'s `handler_factory` block** for any `tts_config_id` read off a node or graph
     object, with the reason stated: "the call site is where the wrong value is in scope, so watching
     only the runner would miss it."
   Can an implementer following only the Changes table still end up with `graph.start.tts_config_id`
   as the only id in scope? No. The table now hands them the `CallFlow` in the resolver row and the
   literal construction expression in the `__main__.py` row; the validated value is the one in scope
   and the unvalidated one is what the tripwire watches for. The round-1 critical is closed at the
   interface, at the call site, and at the tripwire.

2. **[was low, finding 10] Fixed, and fixed the way I asked rather than qualified.** The masking
   sentence is gone; design:353-357 now reads "**No masked copy is kept.**… a sensitive value exists
   in the runner's `_vars` for the life of the call and nowhere else, in no form, partial or whole. A
   support surface that later wants 'did the caller enter something' gets the boolean, never
   characters." That is consistent with the logging rule's "nothing constructs a
   redacted-but-recoverable form either" — one rule now, stated once, with no exception clause for a
   consumer that does not exist.

3. **[was minor, test 16] Fixed.** design:593-596: the assertion is "filtered to the bridge logger and
   the assertion made against each record's own `getMessage()`, so a session/call uuid containing a
   `7` cannot make it flaky". It still fails against today's code — `services/vobiz/bridge.py:343`
   emits `"vobiz: dtmf digit=%s call=%s"` on the bridge logger, whose `getMessage()` contains the
   digit — so the test retains the property that makes it worth having (lesson 12: it can fail).

## Regression check

- **File count: 26, unchanged.** Mechanically counted from the Changes table (`26` rows). The two
  round-2 additions (`libs/config_sdk/callflow.py`, `admin-ui/components/callflow/CallFlowPanel.tsx`)
  are still there; nothing new was added to carry these three edits, which is right — all three were
  text corrections to rows and rules that already existed.
- **Test plan: 21 enumerated cases, unchanged in membership** — `1, 2, 3, 3a, 3b, 4, 5, 6, 7, 8, 9,
  10, 11, 12, 13, 14, 14a, 14b, 15, 16, 17`. No case added, none removed, and the three edits landed
  inside existing cases (3a, 16) rather than as new ones. Bookkeeping note, not a defect: the count
  relayed to me was "24"; the design's actual enumerated total is 21. It is the lesson-29 shape — a
  hand-carried count of a list nobody derived mechanically — and worth correcting in whatever tracks
  it, but it changes no control.
- **The 19 verified controls all still hold.** The three edits are strictly additive in strength: a
  Changes-table signature corrected toward the Interfaces block, a construction expression written
  out, a tripwire widened, an exception clause deleted, and a log assertion narrowed to the emitting
  logger. Nothing was loosened, no dependency order changed, no new surface introduced.
- **Findings 5-8 and 11-13 remain recorded as open** in this file (rounds 1 and 2 above), untouched by
  the architect and untouched here. They are reported to the user for a decision, not auto-fixed.

## Final tally

Fixed across the three rounds: round-1 findings 1 (critical), 2, 3, 4 (highs), round-2 findings 9
(high) and 10 (low), plus the test-16 flakiness note.
Open: round-1 findings 5, 6, 7 (medium), 8 (low); round-2 findings 11, 12, 13 (low). No critical, no
high.

Controls verified as genuinely holding: **19** (unchanged from round 2 — the round-3 edits
strengthened controls 15 and 17 rather than adding new ones).

**Nothing blocks the build stage.** The seven open findings are mediums and lows, each with a named
fix in this file; finding 6 in particular is now a one-line addition at a seam the revised
`resolve_call_flow()` signature already exposes, and would be cheap to fold into the build.
