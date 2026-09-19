# Security review: Live Calls Monitoring (code) — `a06dfeb..HEAD` on `feature/live-calls-monitoring`
VERDICT: AMBER

No critical or high is open. The design's central invariant (every identity decision from a fresh DB
read, never the JWT) is genuinely enforced in the shipped code and its tripwire is load-bearing —
I re-executed T5b's own AST logic against the current source rather than trusting the test name.
Four mediums and five lows below.

## Findings

1. [medium] Denial-audit aggregation lets the prober choose which probed `session_id` survives into the
   audit trail — `services/config/live_calls.py:296-303`
   Attack: a `tenant_admin` of tenant A enumerates 20 of tenant B's session ids through
   `POST /live-calls/{sid}/interventions`. All 20 resolve to A, all 404, and all fold into one
   `audit_log` row under key `(user_id, A)`. The `UPDATE` does
   `jsonb_set(..., '{session_id}', to_jsonb($3::text))` — it *overwrites* with the newest value. The
   prober probes the 19 ids they actually care about first and one harmless id last; the surviving
   row reads `count: 20, session_id: <benign>`. AC11 gets a row, but its forensic content is
   attacker-chosen. This is the control that closed finding #7 partially undoing finding #5.
   Fix: accumulate into a bounded `session_ids` jsonb array (cap ~20, then a `truncated` flag) instead
   of overwriting the scalar — or drop `session_id` from an aggregated row rather than lying with the
   last one.

2. [medium] The mutating route has no rate limit at all — `services/config/routers/live_calls.py:114-125`
   Attack: a `supervisor` (the lowest role `LIVE_CALLS_ROLES` admits, with no console surface anywhere
   else) loops `POST /live-calls/{sid}/interventions` against any live session in their own tenant.
   T9's `live_calls_throttle.check(user.id)` is called only on the GET (`:107`). Every *success* writes
   one `live_call_interventions` row **and** one `audit_log` row with no aggregation — only the denial
   path aggregates — and burns ~4 DB round-trips (`assert_current_authority` read, `fresh_authority`
   read, the `calls` SELECT, the INSERT+audit txn) against the shared 10-connection pool. Unbounded
   table growth and pool pressure from an authenticated low-privilege actor.
   Fix: call `request.app.state.live_calls_throttle.check(user.id)` at the top of
   `request_intervention` (or a tighter write-specific bucket), before `assert_current_authority`.

3. [medium] `_mask_msisdn` does not meaningfully mask the numbers it was written for —
   `services/config/live_calls.py:43-51`
   Attack: a `supervisor` is deliberately excluded from `TRANSCRIPT_ROLES` precisely so it cannot read
   call content, then polls `GET /live-calls` every 5s and harvests caller/called numbers. For
   `+14155557788` the function returns `+1415•••7788` — 9 of 11 digits, a 1000-candidate space, and
   the hidden digits are the *least* identifying. Worse, `len(number) <= 9` returns the number
   verbatim: national-format numbers, extensions and short codes are printed in full to every
   supervisor and to the browser. Masking is the only PII control on this response and a stated AC.
   Code review flagged the `<=9` arm as a minor; the prefix width is the larger half and was not
   flagged.
   Fix: keep only the last 4 (`+1•••••7788`) and return a fixed redaction (e.g. `•••`) for anything
   shorter than the suffix length, never the raw value.

4. [medium] The new `record_live_stage` write is a cross-tenant write primitive: no tenant predicate on
   a client-supplied `session_id` — `services/conversation/transcript_builder.py:354-360`
   `UPDATE calls SET live_stage = $2 WHERE session_id = $1` carries no `tenant_id`, although `calls`
   has the column and the handler holds `ctx.tenant_id`. `ctx.session_id` and `ctx.tenant_id` both come
   straight off the gRPC `SessionOpenRequest` with no validation that they belong together
   (`services/conversation/servicer.py:134-145`), on a server bound with `add_insecure_port` and zero
   interceptors (`services/conversation/__main__.py:359`).
   Attack: anyone with network reach to the Conversation gRPC port opens a `Converse` stream declaring
   their own tenant but `session_id` = a currently-live call of tenant B, drives a transfer, and flips
   tenant B's `calls.live_stage` to `human_connected` / `waiting_for_human` — falsifying tenant B's
   Live Calls dashboard and its `waiting_for_human` / `human_connected` KPIs, the exact signals the
   feature exists to make operators act on. The unauthenticated gRPC listener predates this diff; the
   unscoped write does not, and the transfer hooks are the first writer where the dashboard is the
   consumer.
   Fix: `WHERE session_id = $1 AND tenant_id = $3` with `ctx.tenant_id`. (Separately: the Conversation
   gRPC listener has no authentication — out of scope for this diff, but it is the root cause.)

5. [low] The denial path re-populates the authority memo entry `_resolve_scope` just evicted —
   `services/config/routers/live_calls.py:140` vs `deps.forget_authority` at `:83`/`:93`
   The comment asserts this `fresh_authority` call "is a memo hit". It is not: `_resolve_scope` called
   `forget_authority` on that exact `(user_id, scope_key)` immediately before raising, so this is a
   full DB re-read that re-inserts the entry.
   Attack: any live-calls operator loops POST with distinct nonexistent `tenant_slug` values. Each
   denial re-adds a memo entry, so finding #8's eviction half is undone and only the
   `AUTHORITY_MEMO_MAX_ENTRIES = 10_000` LRU cap remains. The flood evicts every legitimate user's
   entry, forcing a `users` read on every live-calls request platform-wide. Memory stays bounded, so
   this is degradation, not exhaustion.
   Fix: reuse the identity `_resolve_scope` already resolved (pass it out on the exception path), or
   call `deps.forget_authority` again after the audit write.

6. [low] A platform-scoped actor's cross-tenant probes are still unaudited —
   `services/config/routers/live_calls.py:141`
   `record_denied_intervention` runs only `if effective_user.tenant_id is not None`. A `superadmin`
   enumerating tenant slugs through the intervention route leaves **no** audit row at all. The `403
   "account tenant is no longer active"` branch (`:77`) is likewise never audited — only 404 is caught.
   Attack: a superadmin (or any future NULL-tenant actor inside `LIVE_CALLS_ROLES`) maps which tenant
   slugs exist, with zero trace, on the one route the design added audit to for exactly this purpose.
   Fix: on the platform-scoped 404, audit against the *requested* slug with a NULL/placeholder
   entity_id; audit the 403 branch too.

7. [low] `PATCH /tenants/{tenant_id}/concurrency` writes soft-deleted tenants —
   `services/config/routers/tenants.py:62-97` → `services/config/tenants.py:127` (`SELECT * FROM
   tenants WHERE id = $1 FOR UPDATE`, no `deleted_at IS NULL`)
   Attack: an `admin` whose tenant was soft-deleted still has a matching `users.tenant_id`, so the
   own-tenant equality check passes and they mutate the dead tenant row plus write an `audit_log`
   entry. This is precisely the case `_resolve_scope` 403s on the live-calls side (finding #10) — the
   two new surfaces disagree about what a soft-deleted own-tenant means. Blast radius is one
   display-only column, so low, not medium.
   Fix: 403 when `tenants_service.get_tenant_by_id(tenant_id)` returns None, mirroring
   `_resolve_scope`'s own-tenant branch.

8. [low] T5b's invariant is directional and name-bound — `services/config/tests/test_live_calls.py:259-283`
   It flags `.tenant_id`/`.role` only on an `ast.Name` literally spelled `user`, and only on lines
   *after* the first `fresh_authority(` call. A helper added above `_resolve_scope`, a rebind
   (`u = user`), an unpack, or a read inside a `Depends(...)` default all pass. The file is clean
   today — I verified the only `user.<attr>` read anywhere in it is `user.id` at `:107` — so this is a
   durability gap, not a live bug.
   Fix: assert the *complete* set of `user.<attr>` reads in the module equals `{"id"}` plus an explicit
   allowlist, rather than only those below a line number.

9. [low] Throttle, denial-aggregation and authority-memo state are all per-process —
   `services/config/app.py:212-240`, `services/config/live_calls.py:275`, `services/config/deps.py:159`
   Under N uvicorn workers the GET throttle's real bound is 4N per 5s, not 4, and the denial
   aggregation emits up to N `audit_log` rows per window instead of one. Neither caches an
   authorization *grant* (the memo only holds an identity the DB confirmed, and 60s staleness is the
   design's accepted revocation latency), so this is not an authorization bypass — but the throttle's
   documented bound is not its deployed bound. Worth stating in ops notes or moving to Redis, which
   this service already runs.

### Assessed and not findings
- `interventions_pending` never decrements: the KPI is an `EXISTS` over all history, so it over-counts.
  It reveals nothing cross-tenant, feeds no authorization decision, and is already in the review's
  open minors. No security consequence.
- `ge=1, le=10_000` bypass: there is no path to the column that skips the Pydantic model (see verified
  controls #14/#15). Not a finding.

### Design-stage carried-open findings — closure check
Closed as claimed: #1 (concurrency fresh re-read), #2 (type-mismatch predicate, `tenant_id = $2` bound
to the TEXT slug), #3 (`ACTIVE_TENANT_STORAGE_KEY` slug on both writer and reader), #4 (ip validation),
#9 (agents-join `a.tenant_id = $2`), #10 (soft-deleted own tenant → 403, no fall-through to the
platform branch), #11 (slug-reuse assumption holds — soft delete keeps the row, so `tenants.slug`'s
UNIQUE still blocks reissue), acquire timeout on the poll, rate limiting on the poll.
**Partially closed:** #5 (finding 1 above: content attacker-controlled; finding 6: platform-scoped
probes still unaudited), #7/#8 (finding 1 and finding 5 above).

## Verified controls
1. T5b tripwire present and load-bearing — I re-ran its AST logic against current source: the only
   `user.<attr>` read in `routers/live_calls.py` is `user.id` at `:107`, and the mutation test really
   does detect an injected `user.tenant_id`. T9/T12/T14/T25 and the round-2 fix did not weaken it.
2. Branch selection reads `effective_user.tenant_id` (`routers/live_calls.py:66`) and the transcript
   gate reads `effective_user.role in deps.TRANSCRIPT_ROLES` (`:110`) — both from the same fresh row.
3. `fresh_authority` and `assert_current_authority` both go through `users_service.get_user_by_id`,
   which is `WHERE id = $1 AND deleted_at IS NULL` (`users.py:47-52`) — lesson 27's soft-delete
   revocation gap is genuinely closed on this surface, not just named.
4. Ordering on the write path is sound: `assert_current_authority` (uncached) runs *before*
   `_resolve_scope`, so the memoized identity the write actually uses cannot disagree with the fresh
   row — assert already proved fresh tenant == token tenant, and the memo entry was written under the
   same tenant.
5. Both live-calls statements carry their own `c.tenant_id = $1` (`live_calls.py:65`, `:103`) rather
   than relying on the agents JOIN's incidental exclusion (lesson 12); the join additionally carries
   `a.tenant_id = $2`.
6. No SQL injection anywhere in the new code. `_ROWS_SQL_TEMPLATE.format()` interpolates only module
   constants (`MAX_LIVE_ROWS`, two fixed SQL fragments chosen by a bool). Every attacker-influenced
   value — tenant_slug, session_id, action, ip, user id/email, count — is a bind parameter, including
   the nested `jsonb_set` UPDATE and the `audit_log` INSERT.
7. The transcript LATERAL joins `transcript_entries` on `session_id` with no tenant predicate; that is
   safe because `calls.session_id` is the table's PRIMARY KEY (`database/schema.sql:446`), so a
   session_id is globally unique and the tenant-filtered `c` anchors it. Verified, not assumed.
8. Transcript withholding is structural, not cosmetic: when `include_transcript` is False the LATERAL
   is omitted from the SQL entirely (`live_calls.py:153-157`) — the text is never fetched.
9. No existence oracle on GET: a foreign slug, a nonexistent slug, and a slug mismatched against the
   caller's own tenant all return an identical `404 {"detail": "tenant not found"}` on identical code
   paths (`routers/live_calls.py:78-84`, `:91-94`).
10. No existence oracle on POST: a foreign-tenant session_id and a nonexistent one both return
    `404 "call not found"` — the predicate is `session_id = $1 AND tenant_id = $2` with the DB-resolved
    slug, so a foreign session can never match (`live_calls.py:221-226`).
11. `require_live_calls_operator` is built on `get_authenticated_user`, not `get_current_user` —
    `CONSOLE_ROLES` is untouched, so `supervisor` gained nothing on the 28 bare-authenticated routes
    (lesson 4).
12. `test_live_calls_routes_match_role_allowlist` keys on the gate's `__code__`, so a *new* route
    mounted under the same dependency fails it, and it asserts `viewer not in LIVE_CALLS_ROLES`.
13. `test_exactly_two_routes_depend_on_get_authenticated_user` still resolves to exactly
    `{me, change_password}` — the new gate did not add a third bare-authenticated route.
14. `PATCH /tenants/{id}/concurrency` really does achieve the fresh-read property it claims: it calls
    `users_service.get_user_by_id(current_user.id)` and scopes on `row["tenant_id"]`, never
    `current_user.tenant_id`; a non-matching tenant gets the same 404 body as a nonexistent one
    (lesson 2); superadmin bypass is explicit, not implied by `tenant_id IS NULL` (lesson 24).
15. `ge=1, le=10_000` is enforced server-side by FastAPI's validation of `TenantConcurrencyUpdate` —
    there is no alternate route to the column (`PATCH /tenants/{id}` is superadmin-only and carries the
    same bound), `_UPDATABLE_FIELDS` is an allowlist so the route reaches no other column, and the DB
    CHECK independently enforces `>= 1`.
16. `tenants.max_concurrent_calls` is display-only — grepped every service; campaigns' concurrency
    limiter uses `campaigns.max_concurrent_calls`, a different column. The new admin-editable route
    escalates no runtime capability.
17. `_extract_client_ip` validates the left-most XFF hop through `ipaddress.ip_address()` and stores
    NULL on a spoofed/malformed value — no unvalidated header text into the INET column, no 500.
18. The denial path writes `audit_log` only, never `live_call_interventions` — an arbitrary
    out-of-scope `session_id` cannot insert into the tenant-badge table (lesson 30), and the audited
    session_id is truncated to the same 200-char bound as the table's CHECK.
19. The success path wraps the INSERT and `write_audit` in one transaction — no intervention row can
    exist without its audit row.
20. Round-2's denial-aggregation key fix is correct as far as the tenant dimension goes: the key is
    `(user.id, str(tenant_id))` (`live_calls.py:290`), so a superadmin probing tenant A then tenant B
    inside the window gets two distinct rows — finding #5's per-tenant accountability is preserved.
    (The session_id dimension is finding 1 above.)
21. The poll throttle reads `user.id` off the token before the identity re-read — sound, because it is
    a gate that can only reject, never widen, and `user.id` is signature-verified and immutable.
    `limit=4/5s` gives real multi-tab headroom (lesson 36).
22. The authority memo is bounded (10k, LRU) and keyed on `(user.id, scope_key)` where `scope_key`
    derives from the *request* (`tenant_slug` or `"self"`), never from identity — so a tenant switch
    re-validates rather than inheriting another selection's result.
23. `pool.acquire(timeout=ACQUIRE_TIMEOUT_S=5.0)` on the poll — pool exhaustion surfaces as a
    retryable error rather than queueing behind the shared 10-connection pool.
