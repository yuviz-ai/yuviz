# Security review: .sdlc/live-calls-monitoring/02-design.md (design stage, round 3 — final)
VERDICT: AMBER

Re-reviewed against the worktree `/Users/chandankumar/yuviz/.claude/worktrees/agent-afa560ddeac7c1ed8`.
No implementation exists yet.

**Round 2's high is closed, and closed structurally, not by a patch.** I traced the whole
`GET /live-calls` path myself rather than accepting the architect's claim (details in Verified
controls 1-3): the branch decision is now made *after* and *from* `fresh_authority`'s DB-read row,
and there is no second consultation of the token anywhere downstream in that route. This is not
whack-a-mole — the design now threads one `effective_user` from the first statement of
`_resolve_scope` and explicitly declares the token dead to the request thereafter, which is the
"single fresh identity from the top" shape rather than a per-decision-point repair.

One new **medium** on the *third* route the design adds (`PATCH /tenants/{id}/concurrency`), which
never adopted that shape. It is the same class in kind but far smaller in blast radius, and — unlike
the round-2 high — it is consistent with the PRD, which scopes its freshness constraint to granting
Listen/Barge and to tenant selection only. Round 1's mediums/lows are carried unchanged at their
original severity per the coordinator's instruction: open, tracked, not blocking.

## Findings

1. [medium] The one new *write* route in this design decides tenant ownership from the token, not the
   fresh row — design: Interfaces, `services/config/routers/tenants.py::update_tenant_concurrency`,
   `# admin: 404 "tenant not found" unless str(current_user.tenant_id) == tenant_id`, under
   `Depends(require_role("superadmin","admin"))` (token role — `deps.py:71-79` → `get_current_user`
   → `get_authenticated_user`, JWT only, no DB read).
   Attack: a tenant_admin is transferred from tenant A to tenant B (`users.py::update_user`,
   `_UPDATABLE_FIELDS = {"role","tenant_id"}` — the codebase's only confinement mechanism). Their
   unexpired token still claims tenant A for ~12h, so the own-tenant check passes for A and they
   `PATCH /tenants/<A>/concurrency` — a write into a tenant they no longer belong to, changing the
   only source of A's channel-utilization cap (PRD Constraints / AC13) and forging A's operators'
   view of their own capacity. Same shape for a demoted or soft-deleted admin inside the token
   window (lesson 27). Blast radius is one audited INT column with no call-placement enforcement
   behind it and no data disclosure, hence medium rather than critical.
   Fix: gate this route on the same fresh row the live-calls routes use —
   `assert_current_authority` (it already 403s on a re-tenanted, demoted or deleted actor) and then
   compare `effective_user.tenant_id`, not `current_user.tenant_id`. Keep the 404 (non-oracle) body.
   Also state that `tenants._UPDATABLE_FIELDS` must gain `max_concurrent_calls`, or the delegated
   `update_tenant()` raises `ValueError` on every call (`tenants.py:118-120`).

### Carried from round 1 — deliberately unfixed, unchanged severity, still open

2. [medium] Intervention tenant predicate specified against a column of the wrong type:
   `calls.tenant_id` is `TEXT` slug (`database/schema.sql:447`), not the UUID `request_intervention`
   is given. Either 500s or never matches; the security exposure is the field repair (dropping `$2`
   = cross-tenant read of any live session by id). Fix: bind the slug string, keep the UUID only for
   `audit_log.entity_id`.
3. [medium] `ACTIVE_TENANT_STORAGE_KEY` (`admin-ui/components/AppShell.tsx:9`, written at `:208` as
   `t.id`) holds a tenant UUID, not a slug, so the superadmin view (AC2) is dead and the shortest
   repair is a second server-side "slug or id" resolver the isolation reasoning does not cover.
   Fix: convert id→slug client-side; keep `_resolve_scope` single-keyed on slug.
4. [medium] Audit `ip_address` source unspecified, and this is the repo's first writer of that `INET`
   column (`services/config/audit.py:44`, no caller passes it). Raw `X-Forwarded-For` → audit forgery
   on the one route whose purpose is accountability; a non-IP value 500s the transaction. Fix:
   `request.client.host`, or one left-most XFF hop through `ipaddress.ip_address()`, NULL on failure.
5. [medium] The cross-tenant probe is the one denial that is not audited: `_resolve_scope` 404s on a
   foreign `tenant_slug` before `request_intervention` writes anything. Detection gap, not a leak.
   Fix: write the `outcome='denied'` row for that path too, attributed to the caller's own tenant
   with the same fixed `detail`.
6. [medium] No rate limit on `GET /live-calls`; the 5s interval is a client constant. One
   `supervisor` can saturate the shared 10-connection pool (`services/config/db.py:47`, no acquire
   timeout), degrading every tenant's Config traffic and `/health`. Fix: per-user token bucket sized
   to 5s (`app.state.invite_throttle` precedent, `app.py:250`) plus an explicit `acquire(timeout=…)`.
7. [medium] Per-attempt denial `audit_log` rows are unboundedly cheap to drive on an arbitrary
   200-char session id, burying real signal in a shared superadmin-read table. Fix: throttle before
   the denial write; one aggregated row per user per window.
8. [low] The authority memo has no eviction or size bound; a superadmin polling random `tenant_slug`
   values grows a per-worker dict permanently (the re-read fires before tenant resolution — still
   true, and now load-bearing, since STEP 1 memoizes on the request-supplied slug before any
   validation). Fix: sweep or bounded LRU, and only memoize slugs that resolved.
9. [low] The `agents` join for `agent_name` has no tenant predicate, in a design that claims all
   tenant predicates live in the WHERE (pattern source `services/config/calls.py:74-75`). A
   mis-tenanted `agent_id` renders another tenant's agent name. Fix: `AND a.tenant_id = <resolved
   uuid>`, plus the caller-from-a-third-tenant case in test 1.
10. [low] `_resolve_scope` still does not say what happens when the actor's own tenant is
    soft-deleted (`tenants.py:50-58` returns `None`; `routers/calls.py:24-25` 403s). Fix: name it —
    `None` → 403, never fall through to the platform branch.
11. [low] `live_call_interventions.tenant_id` relies unstated on slugs never being freed by a soft
    delete. Fix: state the dependency so a future slug-release change knows it breaks this table.

## Verified controls

1. **The branch is selected from the fresh row, and the ordering that makes that possible is
   specified.** `_resolve_scope` STEP 1 (design 201-204) computes `scope_key = tenant_slug or "self"`
   **from the request only** and calls `fresh_authority(...)` before any other decision; STEP 2
   (206-233) branches on `effective_user.tenant_id`. Because the memo key is request-derived, the
   read can precede the branch with no second query — the reason the round-2 gap existed at all is
   now removed rather than worked around, and the design states the invariant the reviewer can grep
   for ("zero hits on `user.tenant_id` after the `fresh_authority` line", 365).
2. **I independently traced the rest of `GET /live-calls`; no later consumer reads the token.**
   Route signature (235-240) takes only `request`, `tenant_slug`, `user`. Downstream: the slug and
   tenant UUID come from `_resolve_scope`'s return; `include_transcript = effective_user.role in
   TRANSCRIPT_ROLES` (293-294); `live_calls.get_live_calls(tenant_slug, *, include_transcript)`
   (292) takes no user at all; both statements carry `tenant_id = $1` in their own WHERE (301);
   response `tenant_slug` is the resolved slug; `requested_by_email` in `items[].intervention` comes
   from the stored `live_call_interventions` row, not the caller. `require_live_calls_operator`
   (239) is the only other token read and it is a coarse pre-filter that can only reject — a token
   role wider than the DB role is caught by `fresh_authority`'s 403, a token role narrower fails
   closed. The single surviving token read is the audit row's actor id/email, which records who
   called, not what they may do (216-218). The architect's claim holds.
3. **Both directions of re-tenanting are confined, with no oracle, and the tests can fail.** Fresh
   tenant NOT NULL → tenant-scoped whatever the token said, and a differing supplied `tenant_slug`
   404s identically to a nonexistent slug (219-223, lesson 2). Fresh tenant NULL → platform branch
   only if `effective_user.role == "superadmin"`, so a NULL-tenant `viewer` service account is
   refused (224-227, lesson 24 both directions). Test 4 (400-407) exercises the re-tenanted
   superadmin *and* the inverse, asserts the same confinement on `POST …/interventions`, and names
   the two mutations that must break it (branching on `user.tenant_id`; leaving `fresh_authority` in
   place but branching on the claim).
4. **Withheld means not fetched.** The snippet LATERAL is omitted from the SQL entirely when
   `include_transcript` is false (296-298) — the text never leaves Postgres.
5. **The memo TTL is a bounded, justified staleness window on a genuinely live read.** 60s with a
   stated cost argument; the underlying read is `SELECT * FROM users WHERE id = $1 AND deleted_at IS
   NULL` (`users.py:47-52`), a single indexed `fetchrow` returning the row's live `role` and
   `tenant_id` and already filtering soft-deletes. Memo lives on `app.state` per the
   `app.state.invite_throttle` precedent (`app.py:250`).
6. **The grant path is unmemoized.** `assert_current_authority` runs on every
   `POST …/interventions` (267-272), so Listen/Barge is never authorized from a cached decision.
7. **Not-found and out-of-scope are one code path on the POST route.** One query, one body, one
   audit row for foreign-tenant, nonexistent, and over-length `session_id` (274-279) — no existence
   oracle in status, text or latency.
8. **The delegated concurrency write cannot be widened into a general tenant edit.**
   `tenants.update_tenant()` allow-lists kwargs against `_UPDATABLE_FIELDS` and raises on anything
   else (`tenants.py:116-120`), and the route's body model carries the single field — so AC16 does
   not hand admins `name`, `region`, VAD or default-provider ids (the reason the design refused to
   widen the existing superadmin-only `PATCH /tenants/{id}`).
