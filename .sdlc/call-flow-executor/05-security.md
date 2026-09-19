# Security review: call-flow executor implementation (uncommitted working-tree diff, branch `redesign`)
VERDICT: RED

Scope audited: the feature's 26 files. Excluded per instruction: `admin-ui/` except
`components/callflow/CallFlowPanel.tsx` + `lib/callFlowApi.ts`, `services/knowledge/embedding_manager.py`,
`services/toolexec/schemas.py`.

## Findings

1. [high] The runtime flow read carries no tenant predicate at all, so RLS is the *sole* control —
   and for the only production caller the app-level layer above it is a no-op, while the assertion
   that RLS holds has never been executed — `services/config/call_flows.py:104-145`
   (`get_published_for_runtime`), `database/schema.sql:1105`, `services/config/tests/test_call_flows.py:110,158,208`
   Three statements read cross-tenant-capable tables with no `tenant_id` in the WHERE clause:
   `SELECT ... FROM call_flows WHERE id = $1 AND deleted_at IS NULL AND graph IS NOT NULL AND direction = 'inbound'`,
   `SELECT id, slug FROM agents WHERE id = ANY($1::uuid[]) ...`, and
   `SELECT id FROM provider_configs WHERE id = $1 AND role = 'tts'`. The router's
   `require_path_tenant_access` is a real second layer for a console JWT, but the only production
   caller of this route is the Conversation service account, which is platform-scoped
   (`tenant_id IS NULL`) and therefore passes `assert_tenant_access` for *every* tenant slug. So for
   the caller that matters, nothing but the Postgres policy separates tenant A's request from tenant
   B's row. Two things make that load-bearing single control unsafe to merge on:
   (a) the three tests that assert it (`test_payload_resolves_..._omits_cross_tenant_or_inactive`,
   `test_resolved_tts_config_id_null_for_cross_tenant_or_non_tts_provider`,
   `test_published_route_404s_for_wrong_tenant_path_and_admin_jwt`) cannot execute, because the local
   role is `BYPASSRLS`. Under a bypassing role every one of them fails — they are not merely
   unexercised, they are *known to be red* in the only environment anyone has run them in. By
   lesson 12 this check currently proves nothing, and by lesson 19 the control has no other witness.
   (b) `agents.call_flow_id UUID REFERENCES call_flows(id) ON DELETE SET NULL` has no same-tenant
   constraint, and `call_flows` is a tenant-owned row named by id from another tenant-owned row —
   exactly lesson 31's shape, in the one place the feature did *not* apply lesson 31's remedy.
   Attack: an operator (or the first Config/UI write path for this field — grep finds none today,
   which is the only reason this is high and not critical) sets tenant A's agent `call_flow_id` to
   the UUID of tenant B's published flow. On any deployment whose `POSTGRES_DSN` authenticates as the
   schema owner or a superuser rather than `yuviz_app` — which is how every local and CI run works
   today — `get_published_for_runtime("tenant-a", <B-flow-id>)` returns B's whole graph, B's
   `agent_slugs`, and B's `resolved_tts_config_id`. Tenant A's inbound PSTN caller then hears tenant
   B's prompts, keys DTMF into B's `collect` nodes, and every syllable is synthesized through B's TTS
   `provider_config` and its `api_key_ref` secret. The Redis path leaks it further: the payload is
   written to `callflow:tenant-a:<B-flow-id>` with a 60s TTL, so the Conversation service keeps
   serving it from `RedisConfigRepository` even after the DSN is fixed.
   Fix: (i) add an explicit predicate to all three statements — pass the tenant id and append
   `AND tenant_id = $2` to the `call_flows` and `agents` reads and to the `provider_configs` read, so
   RLS is defence-in-depth rather than the whole defence; (ii) add a same-tenant CHECK/trigger on
   `agents.call_flow_id`; (iii) run those three tests as `yuviz_app` and report them green before
   merge — a superuser-only run is not a run.

2. [medium] `status` is read out of the database, carried across the wire, and then dropped by the
   SDK, so no consumer can ever enforce it — an archived flow keeps driving live calls —
   `services/config/call_flows.py:120` (no `status` predicate), `libs/config_sdk/models.py:294-310`
   (`CallFlow` has no `status` field), `libs/config_sdk/providers/cache_aside.py:133-141`
   The reviewer's round-2 finding stands, and the implementation made it slightly harder to close
   than the review described: the HTTP payload *does* include `"status": row["status"]`, but
   `_call_flow_from_dict` silently discards it, so `resolve_call_flow`/`CallFlowRunner` have no way to
   check it even defensively. Attack: a tenant admin discovers a published flow's `collect` node is
   capturing card digits into a non-`sensitive` variable and archives it to stop the bleeding; the
   flow keeps answering calls and keeps collecting, for as long as the flow row is not soft-deleted.
   Fix: add `AND status = 'published'` to the runtime SELECT, and add `status` to `CallFlow` so the
   runtime can assert it independently of the query.

3. [medium] `docs/call-flows.md` tells an operator that a wrong-tenant flow id is rejected by the
   code, when the code contains no such comparison — `docs/call-flows.md` (Resolution order step 3-4,
   Degradation table)
   The degradation table lists "`get_call_flow()` misses (deleted, unpublished, non-inbound, wrong
   tenant)" as a property of the resolution path, and step 4 says a cross-tenant `agent_id` is
   "structurally absent from that map, not rejected by a comparison." The first claim is true only
   while the DB connection role is non-`BYPASSRLS`, and the doc never names that precondition; the
   second is true of `agent_slugs` only *because* the `agents` SELECT was filtered by RLS, which the
   doc also does not say. "unpublished" is also wrong — an archived flow is not a miss (finding 2).
   Attack: an operator troubleshooting a leak reads this table, concludes cross-tenant is impossible
   by construction, and does not check which Postgres role the service authenticates as — which is
   precisely the condition finding 1 depends on.
   Fix: state the precondition explicitly ("wrong-tenant isolation here is RLS on `yuviz_app`; the
   query carries no tenant predicate — a `BYPASSRLS` DSN removes it"), and correct the archived case.

4. [medium] A `collect` node marked `sensitive` is still rendered into a spoken prompt and shipped to
   the third-party TTS vendor — `services/conversation/callflow/runner.py:_render` /
   `callflow/handler.py:_run_actions` (`Speak` branch)
   `_render` deliberately uses `flow_variables`, which retains sensitive keys, so a confirmation
   prompt like "you entered {{pin}}, press 1 to confirm" interpolates the PIN into `Speak.text`,
   which the handler passes to `self._active_tts.synthesize(...)`. I verified the value is not logged
   and not persisted (see verified controls 5-11) — but a keyed-in PIN leaving the platform in the
   body of an HTTP request to a cloud TTS provider is a disclosure to a fourth party, and the
   `sensitive` flag's own docstring promises "the collected value never leaves the runtime", which is
   not what the code does.
   Attack: caller keys a card PIN into a `collect` node whose author added a confirmation prompt; the
   PIN is transmitted to, and potentially request-logged by, the tenant's TTS vendor. Nobody
   configuring `sensitive: true` expects that.
   Fix: either reject `{{var}}` references to a `sensitive` variable at publish-time validation, or
   amend `callflow.py`'s `sensitive` docstring and `docs/call-flows.md` to say the value does reach
   the TTS vendor when a prompt interpolates it.

5. [low] The "digit values are never logged" rule has no mechanical enforcement anywhere in the diff —
   `services/conversation/servicer.py:569-571`, `services/vobiz/bridge.py:343-346`
   Every current call site is correct (verified by grep, see verified controls 5-8), but the rule is
   held up by three code comments. There is a tripwire for the TTS control and one for
   `out_responses`; there is none for digits. The code review's observation about a `digit=%s`-shaped
   grep missing an f-string is the milder version of the problem — the grep does not exist, so
   `log.info(f"dtmf {digit}")` added to `bridge.py` next month passes the whole suite.
   Attack: a future debugging line reintroduces lesson 33's exact leak — keyed PINs reconstructable
   from application logs by anyone with log access, across every tenant at once.
   Fix: a module-scoped tripwire (lesson 9 — scope it to the file set, not to one service) asserting
   that in `bridge.py`, `servicer.py` and `callflow/*.py` no logging call's arguments or f-string
   parts contain a digit-bearing identifier (`digit`, `dtmf`, `_buffer`, `flow_variables`).

6. [low] The TTS tripwire is evadable in four ways and does not cover the whole reachable surface —
   `services/conversation/tests/test_callflow_tts_tripwire.py`
   It regexes `(\w+)\.tts_config_id` with an allowlist of `{"action"}`, over `callflow/*.py` plus
   `__main__.py`. It misses `getattr(node, "tts_config_id")`, `node.__dict__[...]`,
   `(n.get("data") or {})["tts_config_id"]` (the dict form the graph actually arrives in — the most
   likely accident, since that is how `call_flows.py` itself reads it), and is defeated outright by
   naming a loop variable `action`. It also does not cover `agent_resolver.py` or
   `provider_bundle.py`, either of which could grow a flow-aware path.
   Attack: a later change reads the author-supplied `start.tts_config_id` in dict form and reopens
   lesson 31/32's hole with a green tripwire above it.
   Fix: assert positively instead — that `CallFlowRunner(...)`'s `tts_config_id=` argument expression
   is literally `flow.resolved_tts_config_id` (AST-walk the single call site), and keep the negative
   grep as a secondary net. It is a real control today; it is not a durable one.

7. [low] The failed-handoff path leaves the flow driver alive on a call that has already been told to
   hang up — `services/conversation/callflow/handler.py:_handoff_to`
   Both failure branches (`agent_slugs` miss, `handoff()` returns `None`) emit
   `HandlerResponse(end_call=True)` but set neither `self._ended` nor `self._delegate`, so
   `_drive()`'s `while not self._ended and self._delegate is None` keeps looping and keeps calling
   `self._runner.on_digit(...)` for events that arrive after the end-call. Today the runner is parked
   on an `agent` node, whose `on_digit` returns `[]`, so nothing happens and the leak is bounded by
   `on_session_end`. It is the single place in this handler where the otherwise-correct `_ended`
   discipline (lesson 34) is not applied, and the next node type or retry edge reachable there turns
   it into a post-hangup advance.
   Attack: caller keys digits during the grace period after an unresolvable handoff; a future runner
   change makes those digits act on a dead session.
   Fix: `self._ended = True` before the `out_responses.put` in both failure branches.

## Judgement calls you asked for explicitly

- **Would the code hold under a non-bypassing role?** Yes, mechanically, and for a clean reason:
  `libs/tenancy/session.py`'s `_RESOLVE_SQL` sets `app.tenant_id` from the `tenants` row, and
  `database/rls.sql:466-471` compares `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`
  with `FORCE ROW LEVEL SECURITY`. An unknown slug resolves no row, so the GUC stays empty, the
  comparison is against NULL, and zero rows come back — fail-closed, not fail-open. The three reads
  are inside one `tenant_conn` transaction, so the `SET LOCAL` precedes all of them. `current_tenant()`
  pins a tenant-scoped caller to its own tenant and honours the path target only for
  `tenant_id IS NULL`, so a tenant-B admin cannot aim RLS at tenant A even by editing the URL.
- **Is anything other than RLS standing between tenant B and tenant A's flow?** For a console JWT,
  yes: `require_path_tenant_access` on the router 404s a wrong-tenant slug before the handler runs.
  For the Conversation service account — the only caller in production — **no**. That is finding 1,
  and it is why I will not call this green on an untested policy.
- **Is the `tenant_conn` "one join" → "three statements" deviation a problem?** No. One transaction,
  GUCs set once up front, `SET LOCAL` reverted at commit; every statement is covered. Accept it.
- **Is `Depends(get_current_user)` instead of a narrower role guard a problem?** No. It admits a
  same-tenant `viewer`, but `GET /tenants/{slug}/call-flows` and the by-id editor reads are already
  bare `get_current_user`, so a viewer gains nothing it could not already read. The payload's only
  non-graph content is `resolved_tts_config_id`, an internal id with no secret attached. Not a
  finding — but note lesson 4: adding a role below `viewer` later would inherit this route.
- **Existence oracle?** None found. `_parse_id` returns 400 only for a malformed UUID (a format
  error, not an existence signal); unknown, wrong-tenant, unpublished, outbound and soft-deleted all
  return the identical bare `404 call_flow '<id>' not found`. Lesson 2 satisfied.
- **Items already open by your decision** — I re-read each against the code. None was made worse, with
  the one exception noted in finding 2 (`status` now exists in the payload but is discarded by the
  SDK, so the fix is a two-file change rather than one). The `collect`-overwrites-seeded-key merge
  direction, the missing `resolve_call_flow()` tenant assert, unmetered TTS spend, `MAX_NODES` double
  duty, the missing publish-time `sensitive` warning and the missing driver-task done-callback are all
  implemented exactly as decided and are not re-raised here.

## Verified controls

1. `CallFlowRunner.__init__(self, graph, *, tts_config_id, variables=None)` — `tts_config_id` is a required keyword-only argument with no default; a call site cannot omit it.
2. No statement in `services/conversation/callflow/*.py` or `__main__.py` reads `.tts_config_id` off a node or graph object — grepped independently of the tripwire; the only occurrences are `action.tts_config_id` (SetVoice's own field) and `self._tts_config_id`.
3. `__main__.py`'s `handler_factory` passes literally `CallFlowRunner(graph, tts_config_id=flow.resolved_tts_config_id, variables=call_context_variables)` — lesson 31/32's required call expression is present at the one site that must satisfy it.
4. `voice_for()` is only ever reached from `_set_voice(action.tts_config_id)`, whose `SetVoice` is constructed solely from `self._tts_config_id` in `runner.open()`; no unvalidated id has a path to `config.get_provider_config`.
5. `services/vobiz/bridge.py`'s dtmf branch logs `"vobiz: dtmf received call=%s"` — presence only, no digit, no partial redaction, and the empty-digit guard drops before logging.
6. `servicer.py`'s `dtmf` case logs `"Converse: dtmf session=%s"` only; no `GatewayMessage` trace logging carries the payload.
7. The runner's invalid-digit branch logs `"callflow: non-DTMF input ignored node=%s"` — node id only, in all three call sites (`on_digit`, `_menu_digit`, `_collect_digit`).
8. Grepped every logging call in `bridge.py`, `servicer.py`, `pipeline.py` and `callflow/*.py` for a digit- or variable-bearing argument: none. No f-string leak present today.
9. `runner.variables` omits `self._sensitive_keys`; `runner.flow_variables` retains them; `_store` adds/discards the key in step with the node's `sensitive` flag — the redaction is a property boundary, not a call-site filter.
10. `handler._handoff_to` passes `self._runner.variables` (the redacting property) into `handoff()` → `_build_pipeline_handler(initial_variables=...)`. Traced end to end: a `sensitive` value cannot reach `WorkflowRunner.variables` (`pipeline.py:705`), therefore not `extracted_variables()` (`pipeline.py:1484`), therefore not `conversation_sessions.extracted_variables` (`transcript_builder.py:367`), and not the LLM prompt suffix.
11. The `Store` action's value is never consumed by the handler (`pass` — already applied inside the runner), so the sensitive value has no egress via the action list either.
12. `GET /{call_flow_id}/published` does not call `_authorize_flow()`; the RLS target is the DID-resolved path tenant, bound by `Depends(bind_path_tenant)` at router construction, i.e. before the endpoint body and before any read.
13. All three reads run inside a single `tenant_conn` transaction — the GUCs are set once, before the first of them, and revert at commit (`SET LOCAL`), safe on a pooled connection.
14. An unknown or non-existent tenant slug resolves no `tenants` row, leaves `app.tenant_id` empty, and yields zero rows via the NULL comparison — fail-closed.
15. A wrong-tenant path segment with a console JWT is stopped by `require_path_tenant_access` at the router, returning the same 404 shape as an unknown slug (no slug oracle).
16. The conversation side receives only `flow.agent_slugs`; no raw `agent_id` crosses the boundary, and an `agent` node naming an id absent from the map ends the call rather than guessing.
17. The handoff resolves via `resolve_handler_deps(runtime_config.tenant.slug, agent_slug, ...)` — the tenant comes from the runtime config, never from the flow payload.
18. `_FLOW_CACHE` is keyed `(flow.tenant_slug, flow.id)`, versioned by `config_version`, bounded at 256 with FIFO eviction, and holds only a parsed `CallFlowGraph` — no secret, no cross-tenant collision possible on the key.
19. The Redis runtime key is `callflow:{tenant_slug}:{call_flow_id}` on both the writer (`call_flows._runtime_cache_key`) and the reader (`redis_repository.fetch_call_flow`) — tenant-prefixed on both sides, 60s TTL, and invalidated by `update`/`delete`/`publish`.
20. Single-writer discipline: `_listen_generation` is bumped in `_arm_timer`, which is the only place a timer is armed; `_run_actions` calls `_cancel_timers()` before every action list; a `("timeout", gen)` event with a stale generation is dropped; `_ended` drops events queued behind a `Hangup`/`Dial`. I found no path that arms a timer without bumping.
21. Exactly one task mutates the runner: `on_dtmf` and `_fire` only `put_nowait` onto the private queue; every `runner.*` call is inside `_drive()`.
22. Lesson 35 checked: `pipeline.py`'s `on_dtmf` (1174) is its own method — `on_cancel` (1168) retains its trailing `self._interrupt_workflow_background_llm()` at 1172. `echo.py`'s `on_dtmf` and `out_responses` are likewise correctly placed class-level members.
23. The driver's `except Exception` logs and ends the call; no stack trace, exception text or tenant-identifying detail reaches the gateway or the caller.
24. `on_session_end` cancels and `gather`s the driver task and every armed timer before delegating — lesson 26's lifecycle question is answered.
25. Every new SQL statement is parameterized (`$1`/`$2`, `ANY($1::uuid[])`); no string-built query anywhere in the diff.
26. The runtime payload contains no secret material — `resolved_tts_config_id` is an id, and `api_key_ref` resolution stays behind `SecretResolver` on the Conversation side.

## What blocks a merge

Finding 1, in two parts, both required:
1. Run `test_payload_resolves_..._omits_cross_tenant_or_inactive`,
   `test_resolved_tts_config_id_null_for_cross_tenant_or_non_tts_provider` and
   `test_published_route_404s_for_wrong_tenant_path_and_admin_jwt` against a connection
   authenticating as `yuviz_app` (non-`BYPASSRLS`), and report them green. The feature's single most
   important assertion is currently unexecuted, and is red under the role it was written for.
2. Add an explicit `tenant_id` predicate to the three reads in `get_published_for_runtime`, and a
   same-tenant constraint on `agents.call_flow_id`, so the isolation of a PSTN caller's IVR does not
   rest on one Postgres policy plus the deployment's choice of DB role.

Findings 2 and 3 should land in the same change (both are one-line); 4-7 can follow.

---

# Round 2 — re-audit of the dispatched fixes
VERDICT: AMBER (round 1 was RED; the high is closed)

Verified against the working tree, not the implementer's report.

## Round-1 findings closed

**R1-1 [was high] — CLOSED.** Both parts land, and both hold.

*Part 1, the explicit predicates.* `services/config/call_flows.py:108-163` now carries a literal
`tenant_id = $2` on all three reads, exactly as claimed:
`call_flows ... WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL AND graph IS NOT NULL AND direction = 'inbound'`,
`agents ... WHERE id = ANY($1::uuid[]) AND tenant_id = $2 AND deleted_at IS NULL AND status = 'active'`,
`provider_configs WHERE id = $1 AND tenant_id = $2 AND role = 'tts'`. All three still run inside the
same `tenant_conn` transaction, so the predicate is *additional* to the policy rather than instead of
it — which was the point. Confirmed defence-in-depth and not a swap: `git status` shows
`database/rls.sql` and `libs/tenancy/` unmodified, so `call_flows_tenant_isolation`,
`FORCE ROW LEVEL SECURITY` and `current_tenant()`'s caller-pinning are all exactly as audited in
round 1.

*The new `get_tenant()` lookup, assessed on its own.* `services/config/tenants.py:36-51` runs on
`platform_conn(reason="tenants-out-of-rls-scope")` — a deliberate `BYPASSRLS` connection — and is
Redis-cached. That is the correct call here and does not undo the fix: the lookup is a
`slug → id` resolution whose result is then used *as the restricting predicate*, so it can only
narrow the three reads, never widen them. It cannot return a different tenant's id for a given slug,
and the slug itself is the path segment RLS is already bound to. Two consequences worth recording,
both benign or better:
- `get_tenant()` filters `deleted_at IS NULL`, so a soft-deleted tenant's slug now resolves to
  `None` and the route returns its bare 404. That is an unintended *improvement* — RLS alone never
  consulted `tenants.deleted_at`, so before this change an offboarded tenant's published flow would
  still have been served to the runtime (lesson 16's shape). Now it is not.
- A miss returns `None` before any flow read, which the route renders as the same
  `404 call_flow '<id>' not found` every other negative case returns — so the new lookup adds no
  tenant-existence oracle.
- The only cost: one extra `platform_conn bypass reason=...` INFO line per uncached runtime read.
  Noise, not exposure.

*Part 2, the database-tier constraint.* `database/schema.sql:1107-1152` adds
`UNIQUE (id, tenant_id)` on `call_flows` and replaces `agents.call_flow_id`'s plain
`REFERENCES call_flows(id)` with `FOREIGN KEY (call_flow_id, tenant_id) REFERENCES call_flows(id, tenant_id) ON DELETE SET NULL`.
The cross-tenant pin that round 1's attack path depended on is now impossible at the database tier,
not merely absent from the application. Judged against lessons 10 and 13:
- **Atomicity holds.** The violation guard, both FK drops, the UNIQUE drop/add and the FK add are all
  `EXECUTE`s inside one `DO $$ ... END $$`, which is a single statement to `psql`. Under
  autocommit-per-statement that block is its own transaction, so the `RAISE EXCEPTION` rolls back all
  three changes together. Lesson 13's trap — a guard that protects only the statements inside its own
  block, with sequencing protecting nothing — does not apply, because there is nothing outside the
  block to protect. I could find no way to leave the table with the UNIQUE added and the permissive
  FK still in place.
- **A failure mid-block fails closed.** If the final FK `ADD` were to fail on something the guard's
  `JOIN` cannot see (a dangling `call_flow_id` — reachable only on a database where the column exists
  without ever having had the original FK), the rollback restores the *old* permissive FK. Weaker
  than intended, never absent, and noisy rather than silent (next point).
- **The guard actually reaches the operator, which is lesson 14's half of this.** I checked the whole
  line and the caller, not just the flag: `scripts/start_local.sh:44` is
  `psql voiceai -v ON_ERROR_STOP=1 -f "$REPO/database/schema.sql" || schema_rc=$?`, and the launcher
  distinguishes psql's exit 2 ("could not connect", `return 0` with a hint — lesson 15) from any
  other nonzero, which it propagates with `return "$schema_rc"` and an explicit failure message.
  Nothing swallows it: no `2>/dev/null`, no `|| true`, no abort-into-a-success-message.
  `docs/setup.md:74` documents the same `-v ON_ERROR_STOP=1` invocation, so the second documented
  apply path behaves identically. The implementer's reported exit 3 is consistent with
  `ON_ERROR_STOP=1` and could not have been produced without it — the verification was real.
- **MATCH SIMPLE** exempts a NULL `call_flow_id` from the composite check, so the no-flow majority
  path is untouched. Confirmed against the block's own comment.
- The ordering fix is genuinely necessary and genuinely present: both `DROP CONSTRAINT IF EXISTS` on
  `agents` precede the `call_flows` UNIQUE drop/add, so the re-apply does not fail with "other
  objects depend on it". This is the lesson-10 shape — a check that only fails on the *second* apply
  against a non-empty database — and it was found the only way it can be found.

**R1-3 [was medium, the doc] — CLOSED, with one word left over.** `docs/call-flows.md:50-69` now
states both layers explicitly, names the platform-scoped Conversation service account as the reason
RLS alone is insufficient on this route, names the `yuviz_app` / `NOBYPASSRLS` precondition, and
describes the superuser-bypass case the predicate covers. It does **not** overstate in the other
direction: it says in as many words "**This is defence in depth, not a replacement for RLS**" and
"the explicit predicate is what still holds in that case, not a substitute for connecting correctly
in the first place." Both degradation-table rows now name the explicit predicate instead of implying
an unspecified code-level rejection. The one residue: the first of those rows still lists
"unpublished" among the misses, which remains wrong for an *archived* flow (R1-2, still open) — and
the row now carries more authority than it did, so the stale word misleads slightly harder than
before. One word, to be fixed with R1-2.

## The failing test — my judgement

`test_published_route_404s_for_wrong_tenant_path_and_admin_jwt`: **(b) — not an information leak.**
The test's byte-identical-body assertion is over-specified, and it should be narrowed. I am putting
the reasoning on the record rather than the conclusion alone, because the implementer's "pre-existing
gap between two 404 sources" framing is right about the mechanism and silent on why that is safe.

Lesson 2's predicate is *"any response that differs **across a tenant boundary** is an information
leak"* — that is, a response that varies with the existence or state of a resource the caller is not
entitled to know about. The correct test is whether a **fixed caller** sees a **varying** response.
Enumerating the caller classes that can reach this route:

- **Tenant-B admin (or viewer) hitting `/tenants/A/call-flows/{id}/published`.** Stopped at the
  router by `require_path_tenant_access` → `assert_tenant_access`, which returns
  `404 {"detail": "tenant 'A' not found"}`. That string is constant for this caller **regardless of
  whether tenant A exists, whether the flow id exists, whether it is published, archived, outbound or
  soft-deleted** — the rejection happens before any flow is looked at, and `assert_tenant_access`
  deliberately folds "no such tenant" and "exists, but not yours" into one 404 (its own docstring
  cites lesson 2 and agents.py's precedent). Zero bits leak.
- **Platform-scoped service account (`tenant_id IS NULL`).** Reaches the route body and gets
  `404 {"detail": "call_flow '<id>' not found"}`. This caller is *entitled* to every tenant, so
  nothing it learns is across a boundary for it. Within its own view the message is still constant
  across unknown-tenant, wrong-tenant, unpublished, outbound and soft-deleted — I re-verified that
  the new `get_tenant() is None` branch returns through the same 404, so the fix did not introduce a
  tenant-existence oracle here.
- **Superadmin with a non-NULL `tenant_id`.** Not platform-scoped per lesson 24, so it takes the
  first branch — same constant string.

So the two differing bodies are never observable by the same principal, and neither varies with the
target's existence. There is no actor who can write the sentence "starting from X access, I probe Y
and learn Z." By my own round-1 rule, that is not a finding. My round-1 wording — "wrong-tenant,
unpublished, outbound and soft-deleted all return the identical bare 404" — was scoped to the
route-tier cases reachable by the service account, which is still true, and I did note the router 404
separately as "the same 404 *shape*". Saying "shape" and "identical" in the same paragraph invited
exactly this reading, and I should have written per-caller invariance explicitly. The assessment was
right; the phrasing was loose. It is not (a) and not (c).

**Do not weaken the assertion — replace it with one that can fail for the right reason** (lesson 12:
state what would make it fail). The current assertion cannot fail on a real leak and cannot pass on a
correct implementation, which is the worst of both. It should assert **per-caller invariance**:

1. For the tenant-B admin JWT, assert the response is `404` and the body is byte-identical across
   three targets: (i) tenant A's slug with a real published flow id, (ii) tenant A's slug with a
   random UUID, (iii) a slug that does not exist at all. That is the assertion that fails the moment
   anyone makes `assert_tenant_access` distinguish "no such tenant" from "not yours", or moves the
   flow lookup above the router guard.
2. Separately, for the service-account JWT, assert `404` and a byte-identical body across
   wrong-tenant, unknown-tenant, unpublished, outbound and soft-deleted targets.
3. Assert both callers get status `404` (never 403), which is the cross-caller property that *does*
   matter and is the only one worth comparing between them.

Test-hygiene only, not a security blocker: [low].

## Round-2 findings

R2-1. [low] `test_published_route_404s_for_wrong_tenant_path_and_admin_jwt` asserts byte-identical
   bodies across two *different* principals, which is neither what lesson 2 requires nor something
   the code should satisfy — `services/config/tests/test_call_flows.py:208`
   Attack: none — this is the absence of an attack path, which is the finding. The risk is
   downstream: a red test in the suite gets "fixed" by whoever touches it next, and the cheapest fix
   is to delete the assertion, taking the genuine per-caller-invariance coverage with it.
   Fix: replace with the three per-caller assertions above; keep it red until then rather than
   loosening it.

## Still open from round 1 (unchanged, deliberately left alone)

- R1-2 [medium] `status` read, sent over the wire, then discarded by `_call_flow_from_dict` — an
  archived flow keeps driving live calls. Plus the one stale "unpublished" word in the doc.
- R1-4 [medium] A `sensitive` collect value is still interpolated into a `Speak` prompt and sent to
  the third-party TTS vendor, contradicting the flag's own docstring.
- R1-5 [low] No mechanical tripwire for the "digit values are never logged" rule; every call site
  correct today, enforced by comments alone.
- R1-6 [low] TTS tripwire evadable four ways (`getattr`, `__dict__`, the dict form
  `data["tts_config_id"]`, a local named `action`) and does not cover `agent_resolver.py` /
  `provider_bundle.py`.
- R1-7 [low] `_handoff_to`'s two failure branches set neither `_ended` nor `_delegate`, leaving the
  driver consuming events on a call already told to hang up.
- The seven items open by the user's explicit decision: unchanged, and none re-litigated.

**Did anything this round make any of them worse?** Only R1-2, and only cosmetically: the
degradation-table row that still says "unpublished" now reads as an authoritative statement about
the new predicate, so the stale word carries more weight. No new risk was introduced by either fix.

## Residual risk on the closed high

Materially lower than round 1, and for a reason I can name. Two of the three tests I flagged as
never-executed — `test_payload_resolves_same_tenant_active_agent_and_omits_cross_tenant_or_inactive`
and `test_resolved_tts_config_id_null_for_cross_tenant_or_non_tts_provider` — now pass **under the
superuser `BYPASSRLS` role**, on the strength of the explicit predicates alone. That is the opposite
of round 1's position, where they were red under the only role anyone ran them with. Lesson 12's
question is now answerable: delete `AND tenant_id = $2` from any of the three reads and those tests
go red immediately, with no RLS involvement required. The cross-tenant assertion is genuinely
exercised for the first time.

What remains unexercised is **RLS itself** on this path — no test proves the policy would have caught
what the predicate now catches. That is acceptable residual risk rather than a blocker, because the
two layers are independent, the predicate is the one that is now tested, and `database/rls.sql` is
unmodified from the state a prior feature verified. It stays on the record as the reason to run the
config suite against `yuviz_app` when the DSN cutover happens (`scripts/start_local.sh:59-62` notes
every service still uses the superuser DSN — "RLS is live but inert").

## Controls verified as genuinely holding: 35

The 26 from round 1 all still hold — I re-checked that the fix touched neither the runner, the
handler, the redaction properties, nor the RLS/tenancy layer. Nine added:

27. All three reads in `get_published_for_runtime` carry a literal `tenant_id = $2`, verified in the tree.
28. RLS and `libs/tenancy/` are unmodified (`git status` clean) — the predicate is additional to the policy, inside the same `tenant_conn` transaction, not a replacement for it.
29. The new `tenants.get_tenant(tenant_slug)` is a slug→id resolution only; it runs on `platform_conn` by design (tenants is out of RLS scope) and can only narrow the three reads, never widen them.
30. `get_tenant()`'s `deleted_at IS NULL` filter means a soft-deleted tenant's flow now 404s instead of being served — a control RLS alone never provided.
31. A `get_tenant()` miss returns through the same bare `404 call_flow '<id>' not found` as every other negative case — no tenant-existence oracle added.
32. Composite FK `agents(call_flow_id, tenant_id) → call_flows(id, tenant_id)` plus its supporting `UNIQUE (id, tenant_id)`: the cross-tenant pin round 1's attack depended on is now impossible at the database tier.
33. Guard + both FK drops + UNIQUE drop/add + FK add are one `DO $$` statement — atomic under autocommit, so the `RAISE` rolls all three back; no half-apply path found. FK drops correctly precede the UNIQUE they depend on, so re-apply is idempotent.
34. `scripts/start_local.sh:44` and `docs/setup.md:74` both apply `schema.sql` with `-v ON_ERROR_STOP=1`, and the launcher propagates the nonzero rc (distinguishing psql's exit 2 per lesson 15) rather than swallowing it — a tripped guard genuinely stops the operator (lessons 13, 14).
35. MATCH SIMPLE exempts a NULL `call_flow_id` from the composite FK — the no-flow majority path is unaffected.

## What still blocks a merge

Nothing critical and nothing high. Round 1's blocker is closed: the isolation of a PSTN caller's IVR
no longer rests on one Postgres policy plus the deployment's choice of DB role, and the assertion
that it holds is now executed.

Before merge I would want, in descending order:
1. **R1-2** — one predicate (`AND status = 'published'`) and one field on `CallFlow`. An archived flow
   driving live calls is the only open item with a live effect on callers. Plus the one stale
   "unpublished" word in the degradation table.
2. **R2-1** — narrow the failing test to per-caller invariance so the suite goes green for the right
   reason. A red test merged is a test deleted later.
3. **R1-4** — either forbid `{{sensitive_var}}` at publish validation or correct the `sensitive`
   docstring and the doc. Today the flag promises more than it delivers.

R1-5, R1-6 and R1-7 are durability and hygiene; they can follow. AMBER, not GREEN, on items 1-3.
