# Tasks: Live Calls Monitoring

## Phase 1: Schema

- [x] T1 Append `tenants.max_concurrent_calls`, `calls.live_stage`, `live_call_interventions` table,
  `idx_calls_live_tenant`, `idx_transcript_entries_session_turn` and
  `idx_live_call_interventions_lookup` to `database/schema.sql`, exactly as specified (nullable
  `max_concurrent_calls` with NO DEFAULT and no backfill; `live_call_interventions.tenant_id` bound
  as TEXT slug, matching `calls.tenant_id`'s type — closes security finding #2) — `database/schema.sql`
  — done when: `psql -f database/schema.sql` applies twice cleanly against a populated dev DB (idempotent,
  lesson 10/13), `\d tenants` shows `max_concurrent_calls INT NULL`, `\d calls` shows `live_stage TEXT`
  with its CHECK, `\d live_call_interventions` shows `tenant_id TEXT NOT NULL`.

## Phase 2: Authority and auth surface

- [x] T2 Add `LIVE_CALLS_ROLES`, `TRANSCRIPT_ROLES`, `AUTHORITY_MEMO_TTL_S = 60`,
  `require_live_calls_operator()`, `assert_current_authority()` and `fresh_authority()` to
  `services/config/deps.py`, per the design's Interfaces section (memo keyed on `(user_id, scope_key)`
  on `app.state`, scope_key derived from the request's `tenant_slug` or `"self"`, never from the
  caller's identity) — `services/config/deps.py` — done when: a unit test calls `fresh_authority` twice
  within 60s for the same `(user_id, scope_key)` and observes exactly one DB read (mock/count the
  `get_user_by_id` call), a third call after a role change and TTL expiry re-reads and returns the new
  role, and `assert_current_authority` 403s when the user row is soft-deleted or its `tenant_id` no
  longer matches the token's.
- [x] T3 Update `services/config/tests/test_console_gate.py`'s
  `test_exactly_two_routes_depend_on_get_authenticated_user` into a named allowlist mechanism, keeping
  it green against the CURRENT (pre-live-calls) route set and `supervisor` → 403 on `/users`,
  `/calls/{id}`, `/audit-log`. Structure the allowlist so a new entry (route → expected role set) can be
  added without rewriting the assertion shape — this task does not yet add `/live-calls` entries, since
  those routes don't exist until T5 (see T5a) — `services/config/tests/test_console_gate.py` — done
  when: the test passes today, and fails if any third route starts depending on
  `get_authenticated_user` without an allowlist entry.

## Phase 3: `GET /live-calls`

- [x] T4 Create `services/config/live_calls.py` with `LIVE_STAGES`, `MAX_LIVE_ROWS = 200`,
  `_mask_msisdn()` and `get_live_calls(tenant_slug, *, include_transcript)`: two statements on one
  `pool.acquire()`, both carrying `tenant_id = $1` in their own WHERE (never relying on a JOIN, lesson
  12), `ended_at IS NULL` as the sole liveness predicate, `LIMIT MAX_LIVE_ROWS` with `truncated` set
  from the KPI count, the `agents` join carrying an explicit `AND a.tenant_id = $1` predicate (closes
  security finding #9), and the transcript LATERAL entirely omitted from the SQL when
  `include_transcript` is false rather than fetched-then-stripped (closes security finding: withheld
  means not fetched) — `services/config/live_calls.py` — done when: a unit test seeds a live call with a
  transcript row and asserts that with `include_transcript=False` the returned dict has
  `transcript_snippet: None, transcript_withheld: True` AND the raw transcript text is absent from the
  query plan/SQL executed (assert via a query-capturing fixture that no transcript_entries JOIN/LATERAL
  is present in the statement when withheld), and a second test seeds agents in two tenants sharing one
  `agent_id`-shaped collision and asserts only the caller-tenant agent name is ever returned.
- [x] T5 Create `services/config/routers/live_calls.py` with `_resolve_scope()` and
  `GET /live-calls`, wired exactly per the design's order of operations:
  `require_live_calls_operator` → `_resolve_scope` computes `scope_key` from the request's
  `tenant_slug` (or `"self"`) and calls `fresh_authority()` as its first and only identity read →
  branches STEP 2 on `effective_user.tenant_id` (never `user.tenant_id`) → passes `effective_user` (not
  `user`) to `live_calls.get_live_calls(..., include_transcript=effective_user.role in
  deps.TRANSCRIPT_ROLES)` — `services/config/routers/live_calls.py` — done when:
  `grep -n "user.tenant_id\|user.role" services/config/routers/live_calls.py` after the
  `fresh_authority` call line returns zero hits (spot-checked now; T5b makes this an enforced,
  permanent test rather than a one-time check), and
  `services/config/tests/test_live_calls.py::test_platform_scoped_access` and
  `test_retenanted_superadmin` (added in T6) pass.
- [x] T5a Extend `services/config/tests/test_console_gate.py`'s allowlist (from T3) with entries for
  `GET /live-calls` and `POST /live-calls/{session_id}/interventions`, asserting each route's role set
  is exactly `LIVE_CALLS_ROLES` — `services/config/tests/test_console_gate.py` — done when: the test
  fails if a route not in the allowlist depends on `get_authenticated_user`, fails if either live-calls
  entry's role set gains `viewer`, and the pre-existing `supervisor` → 403 assertions on `/users`,
  `/calls/{id}`, `/audit-log` still pass.
- [x] T5b Add an automated, permanent invariant test to `services/config/tests/test_live_calls.py` that
  reads the source of `services/config/routers/live_calls.py` at test time and asserts — via an AST walk
  (preferred, since it survives reformatting) or, if AST proves impractical, a regex over the file with
  comments/strings stripped — that no reference to `user.tenant_id` or `user.role` appears anywhere
  after the line containing the `fresh_authority(` call. This test is the structural enforcement of the
  design's central closed-high invariant and MUST be run in the normal test suite (not a one-time
  reviewer grep), so any later edit to this file — including ones from tasks not yet written — fails
  loudly if it reintroduces a stale-token read — `services/config/tests/test_live_calls.py` — done when:
  the test passes against T5's implementation, and temporarily inserting a line reading `user.tenant_id`
  or `user.role` after the `fresh_authority` call (anywhere in the file, including inside helper
  functions defined in the same module) makes the test fail; revert the insertion and confirm it passes
  again. Every later task in this build that edits `services/config/routers/live_calls.py` (T9, T12,
  T14) must keep this test green.
- [x] T6 Add `services/config/tests/test_live_calls.py` covering tenant isolation with a JOIN-proof
  case (test 1), the no-existence-oracle byte-identical 404s for GET (test 2's GET half), platform-scoped
  access including the NULL-tenant `viewer` service-account 403 (test 3's platform half), and the
  re-tenanted-superadmin branch-selection matrix that fails if the branch reverts to `user.tenant_id`
  (test 3's re-tenanted half — the load-bearing regression test for the fixed high) —
  `services/config/tests/test_live_calls.py` — done when: all listed cases pass, and manually reverting
  `_resolve_scope`'s branch to `user.tenant_id` makes the re-tenanted-superadmin test fail (prove the
  guard per lesson 12).
- [x] T7 Add the AC15 snippet-authority tests: same call, `admin` gets a snippet, `supervisor` gets
  `transcript_snippet: null, transcript_withheld: true` with no substring of the transcript text
  anywhere in the response body; the demotion case (role changed in DB, token unchanged, snippet still
  served within the 60s memo TTL, withheld after TTL expiry using the shipped 60s value, never a
  shortened one per lesson 25) — `services/config/tests/test_live_calls.py` — done when: all pass, and
  replacing `fresh_authority`'s role source with `user.role` (the token) makes the demotion-case test
  fail.
- [x] T8 Add the AC4/AC5/AC13 KPI tests: stage split across all three `live_stage` values plus one
  `NULL` (counts as `ai_only`), masked MSISDN (raw number absent from the whole serialized body),
  `utilization_pct` computed against the tenant's own `max_concurrent_calls`, and the nullable-cap case
  — `max_concurrent_calls IS NULL` tenant returns `utilization_pct: null` and `max_concurrent_calls:
  null` with no numeric fallback anywhere in the body — `services/config/tests/test_live_calls.py` —
  done when: all pass, and introducing a `COALESCE(max_concurrent_calls, 1)` anywhere in the query or
  serializer makes the nullable-cap test fail.
- [x] T9 Add an `acquire(timeout=...)` on the pool checkout in `get_live_calls` and a per-user token
  bucket rate limit on `GET /live-calls`, sized to the 5s poll interval, following the
  `app.state.invite_throttle` precedent (closes security finding #6 — no rate limit / no acquire
  timeout on the poll) — `services/config/live_calls.py`, `services/config/routers/live_calls.py` —
  done when: a test driving requests faster than one per 5s from one user gets 429 after the bucket is
  exhausted, a test with the pool saturated by other connections observes the acquire raise/time
  out rather than hang indefinitely, and T5b's invariant test still passes (the throttle logic must not
  read `user.tenant_id`/`user.role` post-`fresh_authority`).

## Phase 4: `POST /live-calls/{session_id}/interventions`

- [x] T10 Add `InterventionRequest` and `request_intervention()` to `services/config/live_calls.py`:
  looks up `SELECT 1 FROM calls WHERE session_id=$1 AND tenant_id=$2 AND ended_at IS NULL` binding the
  TEXT slug (not a UUID) against `calls.tenant_id` (closes security finding #2 — the type-mismatch
  predicate), returns `None` on no match, otherwise one transaction inserting
  `live_call_interventions` (`outcome='unavailable'`) and `audit.write_audit(...)` together —
  `services/config/live_calls.py` — done when: a test with a cross-tenant call (agent and call in
  tenant B, caller in tenant A) confirms the query binds the slug string and returns `None` rather than
  raising a type error or matching; a test forcing `write_audit` to raise confirms no
  `live_call_interventions` row survives (transactional).
- [x] T11 Determine `ip_address` from `request.client.host` (or the left-most `X-Forwarded-For` hop
  validated through `ipaddress.ip_address()`, NULL on failure — never an unvalidated raw header value)
  and pass it into `request_intervention`'s audit write (closes security finding #4) —
  `services/config/live_calls.py`, `services/config/routers/live_calls.py` — done when: a test posting
  a spoofed non-IP `X-Forwarded-For` header does not raise and the audit row's `ip_address` is NULL,
  and a test with a well-formed XFF hop stores that address.
- [x] T12 Add `services/config/routers/live_calls.py::request_intervention` wired
  `require_live_calls_operator` → `assert_current_authority` → `_resolve_scope` →
  `live_calls.request_intervention(...)` using `effective_user` throughout for the scope predicate and
  the audit actor fields (never `user`); on no match (foreign-tenant, nonexistent, or
  `len(session_id) > 200`), write one `outcome='denied'` audit row attributed to the caller's own
  resolved tenant with a fixed `detail` — including the branch-selection 404 case in `_resolve_scope`
  itself, which the design left unaudited (closes security finding #5) — and return one identical
  `404 {"detail": "call not found"}` for all three cases, with no `live_call_interventions` row on that
  path — `services/config/routers/live_calls.py` — done when: a test asserts byte-identical status and
  body across foreign-tenant, nonexistent-session and 300-char-session-id requests, each producing
  exactly one `audit_log` row with `outcome='denied'`; a test hitting `_resolve_scope`'s own tenant-slug
  mismatch 404 (not `get_live_calls`'s query miss) also produces a denial audit row, closing the
  previously-unaudited branch; and T5b's invariant test still passes (this route's wiring must keep
  using `effective_user`, never fall back to reading `user.tenant_id`/`user.role`).
- [x] T13 Add the AC9 stale-token re-validation test and audit-completeness test: soft-delete or demote
  the user after a successful intervention request, replay the same token → 403, no new intervention
  row; in-tenant request produces exactly one `live_call_interventions` row and one paired `audit_log`
  row with actor id/email, tenant, session id, outcome — `services/config/tests/test_live_calls.py` —
  done when: both pass, and removing `assert_current_authority` from the route makes the stale-token
  test fail.
- [x] T14 Add a per-user throttle before the denial-audit write in `request_intervention`, aggregating
  repeated denials from the same user within a window into one row rather than one row per attempt
  (closes security finding #7 — unboundedly cheap per-attempt denial rows) — `services/config/live_calls.py`
  — done when: a test firing 20 rapid denied requests from one user observes fewer than 20 new
  `audit_log` rows (one aggregated row, count/last-seen updated), while a granted/in-tenant request is
  never throttled, and T5b's invariant test still passes (throttle bookkeeping must not read
  `user.tenant_id`/`user.role` post-`fresh_authority` in the router file).
- [x] T15 Add a bounded eviction (LRU or periodic sweep) to the `fresh_authority` memo dict on
  `app.state`, and memoize only slugs that resolved successfully — never a slug that 404'd (closes
  security finding #8) — `services/config/deps.py` — done when: a test driving `fresh_authority` with
  many distinct `scope_key`s beyond the bound observes the memo's size stay capped, and a resolved-then-
  invalidated slug does not appear as a cached entry.

## Phase 5: `PATCH /tenants/{id}/concurrency`

- [x] T16 Add `max_concurrent_calls` to `services/config/tenants.py::_UPDATABLE_FIELDS`, and add
  `TenantConcurrencyUpdate` to `services/config/schemas.py` (`max_concurrent_calls: int`, `ge=1,
  le=10_000`) and `TenantUpdate.max_concurrent_calls: int | None` per the design — `services/config/tenants.py`,
  `services/config/schemas.py` — done when: `tenants_service.update_tenant(tenant_id,
  max_concurrent_calls=5)` succeeds without raising `ValueError` and the column updates.
- [x] T17 Add `PATCH /tenants/{tenant_id}/concurrency` to `services/config/routers/tenants.py`, gated on
  `require_role("superadmin","admin")` PLUS `assert_current_authority()` to re-read the actor, then
  comparing `effective_user.tenant_id` (never `current_user.tenant_id` from the token) for the admin
  own-tenant check — closing security finding #1, the medium on this exact route — with the same
  non-oracle 404 body `GET /tenants/{slug}` already returns for a foreign tenant —
  `services/config/routers/tenants.py` — done when: a test transfers an admin from tenant A to tenant B
  via `update_user`, replays the admin's still-valid (stale) token, and confirms `PATCH
  /tenants/{A}/concurrency` now 404s (fresh row says B, not the token's stale A) while `PATCH
  /tenants/{B}/concurrency` succeeds; a superadmin PATCHes any tenant successfully.

## Phase 6: Conversation Service live_stage writes

- [x] T18 Add `record_live_stage(session_id, stage)` to `services/conversation/transcript_builder.py`
  using the existing `_spawn(...)` fire-and-forget ordered-write path (`UPDATE calls SET
  live_stage=$2 WHERE session_id=$1`) — `services/conversation/transcript_builder.py` — done when: a
  unit test calls `record_live_stage` and observes the `calls.live_stage` column update without
  blocking the caller (mocked `_spawn`), and a second call for the same session with an earlier stage
  does not overtake a later one already applied (serialization is per-session).
- [x] T19 Call `record_live_stage` from `on_transfer_initiated` (`waiting_for_human`),
  `on_transfer_completed` (`human_connected`), and `on_transfer_failed` / `on_transfer_cancelled`
  (`ai`) in `services/conversation/session.py` — `services/conversation/session.py` — done when: an
  integration test driving each of the four hooks observes `calls.live_stage` set to the corresponding
  value for that session.

## Phase 7: Admin UI

- [x] T20 Add `LiveCallsSnapshot`/`LiveCall` types, `getLiveCalls(tenantSlug?)`,
  `requestIntervention(sessionId, action, tenantSlug?)`, `updateTenantConcurrency(tenantId,
  maxConcurrentCalls)`, and `max_concurrent_calls` on the `Tenant` type — `admin-ui/lib/api.ts` — done
  when: the admin-ui TypeScript build passes with these exported and typed.
- [x] T21 Export `ACTIVE_TENANT_STORAGE_KEY` from `admin-ui/components/AppShell.tsx`, add `{ href:
  "/live-calls", label: "Live Calls" }` to `CALLING_ITEMS`, allow `supervisor` past the auth guard on
  `/live-calls` and render only that nav item for it; update `admin-ui/app/login/page.tsx` to send
  `supervisor` to `/live-calls`; update `admin-ui/app/no-access/page.tsx` copy to drop "supervisor"
  from both message strings — `admin-ui/components/AppShell.tsx`, `admin-ui/app/login/page.tsx`,
  `admin-ui/app/no-access/page.tsx` — done when: a supervisor test login lands on `/live-calls`, sees
  only the Live Calls nav item, is redirected away from `/users` and every other admin route to
  `/no-access`, and `/no-access`'s copy no longer mentions supervisor.
- [x] T22 Build `admin-ui/app/live-calls/page.tsx`'s polling lifecycle: 5s poll via `setInterval` with a
  generation counter discarding stale in-flight responses on pause (AC6), and immediate
  fetch-then-restart on resume (AC7) — `admin-ui/app/live-calls/page.tsx` — done when: manually driven
  in a browser (lesson 23): pause holds the table across three refresh cycles while calls start/end
  server-side, an in-flight request issued just before pause never overwrites the frozen table, and
  resume repaints immediately with no stale (pre-pause) rows before the next interval tick.
- [x] T22a Add `admin-ui/app/live-calls/page.tsx`'s failure and edge states: a fetch-failure banner that
  preserves the last good snapshot rather than blanking the table (per lesson 21's sibling failure
  mode); a 403 mid-session stops polling and shows the reason instead of retrying every 5s; and a `null
  max_concurrent_calls` renders a "Channel cap not set" prompt linking to the Accounts page rather than
  any number (never a fabricated fallback, per the design's explicit scope decision) —
  `admin-ui/app/live-calls/page.tsx` — done when: manually driven in a browser: killing the API mid-poll
  shows the banner while the last good table stays visible; demoting/soft-deleting the operator mid-
  session (triggering a 403) stops further polling and shows the reason, not a retry loop; a tenant with
  no `max_concurrent_calls` set shows the setup prompt, never a number or a computed value.
- [x] T22b Add `admin-ui/app/live-calls/page.tsx`'s CSV export (built client-side from the exact
  rendered `items` rows, no re-query — AC8) and the superadmin tenant picker rendered when
  `ACTIVE_TENANT_STORAGE_KEY` resolves to nothing selected (AC2) — `admin-ui/app/live-calls/page.tsx` —
  done when: manually driven in a browser: CSV row/column count matches the on-screen table exactly for
  a paused (frozen) table, and a superadmin with no tenant selected sees the picker, not an empty table.
- [x] T23 Convert `ACTIVE_TENANT_STORAGE_KEY` to store the tenant slug instead of `t.id` at the write
  site (`admin-ui/components/AppShell.tsx:208`), and update every reader of that key (the superadmin
  tenant picker in T22b, and any other consumer) to expect a slug — closes security finding #3, which
  the design itself flagged as making the superadmin tenant-selection path dead code —
  `admin-ui/components/AppShell.tsx`, `admin-ui/app/live-calls/page.tsx` — done when: a manual
  superadmin session selects a tenant, `localStorage`'s `yuviz.activeTenantId` holds the slug (not a
  UUID), and `getLiveCalls(tenantSlug)` is called with that slug and returns that tenant's rows.
- [x] T24 Add `max_concurrent_calls` number input to the tenant edit form in
  `admin-ui/app/tenants/page.tsx`, calling `updateTenantConcurrency()` on save —
  `admin-ui/app/tenants/page.tsx` — done when: editing a tenant's concurrency in the UI persists the
  value and the next Live Calls poll for that tenant reflects the new `utilization_pct` (cache
  invalidation from T16/T17 firing correctly end to end).

## Phase 8: Scope-out documentation and soft-delete edge case

- [x] T25 Add an explicit `None`-tenant branch to `_resolve_scope`: when the actor's own (non-NULL)
  tenant has been soft-deleted (`tenants_service.get_tenant_by_id` returns `None`), 403 rather than
  falling through to the platform-scoped branch — closes security finding #10 — and add a one-line
  code comment on `live_call_interventions.tenant_id` stating its dependency on tenant slugs never
  being reused after a soft delete — closes security finding #11 — `services/config/routers/live_calls.py`,
  `database/schema.sql` — done when: a test soft-deletes a tenant-scoped operator's own tenant and
  confirms `GET /live-calls` and `POST .../interventions` both 403 rather than routing to the
  platform-scoped branch or 500ing, and T5b's invariant test still passes.
- [x] T26 Confirm sentiment stays out of scope and the nullable-cap "no fabricated fallback" decision
  is visible in the shipped code: grep `services/config/live_calls.py` and
  `admin-ui/app/live-calls/page.tsx` for any sentiment field or `COALESCE`/default substitution on
  `max_concurrent_calls`/`utilization_pct`, and add a one-line comment at the transcript LATERAL noting
  it is the extension point for a future `metadata->>'sentiment'` column, per the design's Scope
  decisions — `services/config/live_calls.py` — done when: `grep -ri sentiment
  services/config/live_calls.py admin-ui/app/live-calls/page.tsx` returns only the extension-point
  comment (no sentiment field or logic shipped), and T8's nullable-cap test remains the enforcement
  point that a numeric fallback was never introduced.
