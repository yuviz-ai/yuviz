# Database-enforced tenant isolation (RLS) — README

Cross-checked against the code as it actually landed (Phases 1–9), not against
the design document's plan-stage description — see `.sdlc/rls-tenant-isolation/
02-design.md` for the rationale, `03-tasks.md` for the task-by-task record, and
`04-t59-test-suite-finding.md` / `05-rollout-runbook.md` for what remains open.

## Role per service

Every one of the six HTTP/worker services (`config`, `knowledge`, `campaigns`,
`toolexec`, `did`, `conversation`) connects through exactly two roles, defined
once in `database/rls.sql`:

- **`yuviz_app`** — `LOGIN NOBYPASSRLS`. The role every service's `db.py` pool
  uses today (`os.environ["POSTGRES_DSN"]`) *once the DSN cutover (T62) has
  happened* — as of this write-up, every service still connects as the
  superuser (see "Current cutover status" below).
- **`yuviz_platform`** — `NOLOGIN BYPASSRLS`, held as a Postgres role
  *attribute* (not an app-level flag), reached only via `SET LOCAL ROLE
  yuviz_platform` inside `libs.tenancy.session.platform_conn()`. `yuviz_app`
  is a member of `yuviz_platform` (membership only — `BYPASSRLS` activates on
  `SET ROLE`, it is never inherited passively).

`ALTER DEFAULT PRIVILEGES` grants `SELECT/INSERT/UPDATE/DELETE` on future
tables created by the schema-applying role automatically — this is the "table
added, forgot to grant" mitigation, and it does **not** cover a table created
by a different role.

## The four tiers

One shared predicate, `services/config/deps.py::assert_tenant_access`, backs
all four. `is_platform_scoped(user)` (`user.tenant_id is None` — lesson 24,
not `role == "superadmin"`) is always the escape hatch.

- **Tier 1 — identity resolution.** `deps.get_authenticated_user` decodes the
  JWT and calls `libs.tenancy.set_caller_tenant(user.tenant_id)` immediately
  after — the one place every authenticated request in every service records
  who the caller is, before any router-specific code runs.
- **Tier 2 — `/tenants/{tenant_slug|tenant_id}/...` routers.** Both
  `deps.bind_path_tenant` (records the path segment as the *target*, no
  authorization) and `deps.require_path_tenant_access` (403/404 depending on
  UUID-vs-slug mismatch) on the router declaration. `current_tenant()` is
  `target if caller is None else caller` — the path is honoured only for a
  genuinely platform-scoped caller, which is what makes this an independent
  second layer rather than a mirror of the URL. 12 router declarations carry
  both, verified by `tests/test_rls_coverage.py`'s route walk, not a
  hand-typed list: `services/config/routers/{agents,calls,carriers,
  phone_numbers,provider_configs,tool_provider_configs,live_calls}.py`,
  `services/campaigns/routers/campaigns.py` (both `/campaigns` and `/dnc`),
  `services/knowledge/routers/knowledge_bases.py`,
  `services/toolexec/routers/custom_apis.py`,
  `services/did/routers/numbers.py`. (`live_calls.py` takes its tenant from a
  query param, not a path segment — invisible to the route walk by
  construction; it is asserted by name in the coverage test instead, and is
  the one precedent a future query-param router must follow by hand.)
  `services/config/routers/tenants.py` is deliberately exempted — `tenants`
  is the resource these tiers key on, not a tenant-owned row.
- **Tier 3 — flat `/{id}` routers with no tenant in the path.** Fetch the row,
  call `deps.assert_tenant_access(row["tenant_id"], current_user)`, *then*
  proceed on the same resolved scope. 17 router declarations, each verified
  by a marker-substring check in `tests/test_rls_coverage.py`'s
  `_TIER3_MODULES` table (kept in sync with the design's own Tier 3 list):
  `provider_configs`, `telephony_configs`, `carriers`, `phone_numbers`,
  `tool_provider_configs`, `calls` (via `_caller_tenant_slug`, narrowed to
  `deps.is_platform_scoped` — lesson 24), `agent_tool_policies`, `users`,
  `invites`, `live_calls` (its by-id resend/revoke shape), `campaigns`,
  `knowledge_bases` (via `_authorize_kb`), `documents`, `agent_kb`,
  `custom_apis` (via `_authorize_custom_api`), `agent_apis` (via
  `agent_apis._authorize_agent_api`), `did/numbers` (assign/release). Two
  flat by-id routers are explicitly out of this design's scope, stated so a
  coverage run doesn't mis-flag them: `toolexec/routers/chain_runs.py`
  (pre-existing, unrelated authorization) and, as of Phase-4/8 follow-up
  hardening beyond the original task list, `knowledge/routers/
  retrieval_policies.py` and `knowledge/routers/agent_kb.py`'s GET list route
  — see "Bugs found and fixed beyond the task list" below; both are now
  *in* scope and covered.
- **Tier 4 — service-account body-tenant routes.** No path segment at all;
  the tenant is a field in the JSON body (or, for `has-knowledge`, a slug in
  the path exempted by name in the coverage walk). `assert_tenant_access`
  against the body's `tenant_id`/slug, then `set_target_tenant(...)`. Three
  sites: `POST /internal/chains/execute` (toolexec `executor.py`),
  `POST /internal/retrieve` (knowledge `retrieve.py`), and
  `GET /internal/agents/{tenant_slug}/{agent_slug}/has-knowledge`.

## Where the wiring lives, per service

| Service | Tier 2 routers | Tier 3 routers | Tier 4 |
|---|---|---|---|
| config | agents, calls, carriers, phone_numbers, provider_configs, tool_provider_configs, live_calls | provider_configs, telephony_configs, carriers, phone_numbers, tool_provider_configs, calls, agent_tool_policies, users, invites, live_calls | — |
| campaigns | campaigns (`/campaigns`, `/dnc`) | campaigns/dnc by-id (9 routes) | — |
| knowledge | knowledge_bases | knowledge_bases, documents, agent_kb | `POST /internal/retrieve` |
| toolexec | custom_apis | custom_apis, agent_apis | `POST /internal/chains/execute` |
| did | numbers | numbers (assign/release) | — |
| conversation | — (no HTTP router; see below) | — | `has-knowledge` (called, not owned) |

`conversation` and `knowledge`'s ingestion worker have no request path at
all — see "New service, pool, cache or table" below for how they get a
tenant scope.

## The table list

`database/rls.sql` applies in this order after `schema.sql` →
`knowledge_schema.sql` → `telephony_schema.sql` (the 4th schema file — see
"Schema apply order" below).

- **Wave A, UUID-keyed (18 tables, own `tenant_id` column):**
  `provider_configs`, `agents`, `tool_provider_configs`, `users`,
  `user_invites`, `carriers`, `phone_numbers`, `purchased_numbers`,
  `campaigns`, `dnc_numbers`, `custom_apis`, `api_chain_runs`,
  `api_side_effect_claims`, `knowledge_bases`, `kb_documents`, `kb_chunks`,
  `telephony_configs`, `audit_log`. Policy: `tenant_id =
  NULLIF(current_setting('app.tenant_id', true), '')::uuid`, `FOR ALL`,
  `USING` = `WITH CHECK`. `users`/`user_invites`/`audit_log` are nullable —
  `NULL` rows are invisible to `yuviz_app` by construction, reachable only
  through `platform_conn()`.
- **Wave A, TEXT-slug-keyed (2 tables):** `calls`, `live_call_interventions`.
  Same shape, against `app.tenant_slug` instead — these two tables key on the
  tenant's slug string, not its UUID, matching the existing call-path
  convention (`CURSOR.md`).
- **Wave B, parent-join, no tenant column of their own (10 tables):**
  `agent_tool_policies`, `agent_workflow_versions`, `agent_custom_apis`,
  `agent_knowledge_bases`, `agent_retrieval_policies` (all join through
  `agents`), `campaign_contacts` (through `campaigns`), `custom_api_params`
  (through `custom_apis`), `api_chain_steps` (through `api_chain_runs`),
  `transcript_entries` (through `calls`, by `session_id`), `kb_ingestion_jobs`
  (through `kb_documents`). Policy is `EXISTS (SELECT 1 FROM <parent> p WHERE
  p.<key> = <child>.<key>)` for both `USING` and `WITH CHECK` — the parent's
  own policy already applies inside that subquery, so these cannot drift from
  it.
- **Out of scope, stated so it is never re-litigated:** `tenants` (it IS the
  tenant; `tenant_conn()`'s own resolver reads it directly), `conversation_
  node_heartbeats` (infrastructure, not tenant-owned), `kamailio_cdr` (written
  by Kamailio directly, not by any of these six services).

`tests/test_rls_coverage.py` part (a) derives the Wave A UUID list from
`information_schema.columns` (any `public` table with a `tenant_id` column),
not from a hand-typed list — a new `tenant_id` column with no policy fails it.
Wave B has no `tenant_id` column to derive from, so it is the one place this
design keeps a fixed name list (`WAVE_B_TABLES`), matched by an identical list
in the coverage test.

## How `SET LOCAL ROLE` bypass works

`libs/tenancy/session.py::platform_conn(pool, *, reason, stamp_tenant=None)`
acquires a connection, opens a transaction, and issues `SET LOCAL ROLE
yuviz_platform` — reverted automatically at transaction end, exactly like
`SET LOCAL` on a GUC. `reason` is **mandatory** so every bypass is
greppable; `stamp_tenant`, when given, still resolves the `app.tenant_id`/
`app.tenant_slug` GUCs (under `BYPASSRLS` they gate nothing — their only
remaining consumer is `audit_log.tenant_id`'s column `DEFAULT`, so a
platform-scoped mutation on a known tenant's behalf still lands in that
tenant's own audit log).

Every `reason=` literal that exists in the codebase today is enumerated in
`tests/test_rls_coverage.py::_BYPASS_REASONS` (36 literals as of this
write-up) — an unlisted `reason=` fails the test. This is the intended-bypass
table T60's staging log-line comparison checks observed traffic against.

## The explicit-override list

`tenant_conn(pool, *, explicit_tenant=None, reason=None)` accepts an
`explicit_tenant` (a UUID or a slug) only for call sites with no request
context to read a scope from at all — `reason=` is mandatory here too, and a
caller with its own resolved tenant conflicting with `explicit_tenant` raises
`TenantScopeConflict` before yielding, so the ambient caller scope always
wins. Exactly three sites, matching `tests/test_rls_coverage.py::
_EXPLICIT_OVERRIDE_REASONS`:

- `conversation-session-write` — `services/conversation/transcript_builder.py`
  caches the call's tenant slug per `session_id`; per-session writes are
  scoped to it.
- `conversation-tool-policy` — `services/conversation/tools/policy_resolver.py`
  takes a `tenant_slug` parameter through to its resolve entry point.
- `kb-ingestion-job` — `services/knowledge/ingestion_worker.py` re-scopes to
  the claimed job's own tenant, one `platform_conn()`-claimed job at a time,
  with the poll loop kept *outside* the transaction (see the ingestion-worker
  and campaign-worker note below).

## The cache-key rule

**RLS is not a control for any cached value.** A cache is read before any
connection is opened, so a key that isn't itself tenant-scoped is a
cross-tenant leak no policy can catch. `tests/test_rls_coverage.py` part (e)
asserts all four `_cache_key`/`cache_key` builders are either UUID-bearing
(`services/config/provider_configs.py`, `services/config/
telephony_configs.py`) or tenant-slug-prefixed (`services/config/agents.py`'s
`cache_key(tenant_slug, agent_slug)`) — except `services/config/
phone_numbers.py`'s `_cache_key`, which is keyed on the E.164 number itself
and is *globally* unique by construction (a phone number belongs to exactly
one tenant), asserted as its own case rather than forced into the
tenant-prefix shape. A new cache key that is a bare slug/name/email with no
tenant component fails this test (verified: a scratch `"kb:acme-docs"`-shaped
key does fail the classifier — `test_a_bare_slug_keyed_cache_key_fails_the_
classifier`).

## What a new addition must do

- **New tenant-owned table:** add its `tenant_id` column, then a Wave A (own
  column) or Wave B (parent-join) policy block in `database/rls.sql`,
  `CREATE POLICY` before `ENABLE`/`FORCE` in the same `DO $$` block (psql -f
  has no `ON_ERROR_STOP` — lesson 13/14). `tests/test_rls_coverage.py` part
  (a) will fail loudly if you forget; it will not fail silently.
- **New `/tenants/{...}` router:** add both `deps.bind_path_tenant` and
  `deps.require_path_tenant_access` to the router's `dependencies=`. The
  route-walk tripwire catches a miss automatically *unless* the tenant comes
  from a query param rather than a path segment — follow `live_calls.py`'s
  precedent by hand in that case, and say so in the coverage test's markers.
- **New flat `/{id}` route with no tenant in the path:** fetch the row first,
  call `deps.assert_tenant_access(row["tenant_id"], current_user)`, and only
  then reuse the resolved scope for any mutation — never fetch and mutate on
  two separate connections (that gap was the original open medium finding
  this design closed for `telephony_configs`/`users`). Add the module to
  `_TIER3_MODULES` in `tests/test_rls_coverage.py`.
- **New body-tenant (Tier 4) route:** `assert_tenant_access(<body tenant>,
  current_user)` then `set_target_tenant(...)`, no path segment to bind. Add
  a case to `tests/test_cross_tenant_admin.py`'s Tier 4 parameterisation.
- **New service, pool, or background worker:** never call `pool.fetch*`/
  `pool.execute` directly — always `tenant_conn()`/`platform_conn()`, even
  for a worker with no request context (use `explicit_tenant=`/`reason=` and
  add the reason to the enumerated bypass/override table). `tests/
  test_no_bare_pool_calls.py` ASTs every module under `services/` (excluding
  `*/tests/*`) for a `.fetch/.fetchrow/.fetchval/.execute/.executemany` call
  on a `Pool`-typed name and fails on any hit.
- **A poll-loop worker** (campaigns' `worker.py`, knowledge's
  `ingestion_worker.py`): keep the loop **outside** the `tenant_conn`/
  `platform_conn` block — one transaction per scan/job, never one
  transaction holding the whole loop's snapshot. Both have a test asserting
  `pg_stat_activity.state` shows no open transaction between iterations.
- **New cache key:** must be UUID-bearing or tenant-slug-prefixed (or, like
  `phone_numbers`, globally unique by construction with that reasoning
  stated). Add it to `tests/test_rls_coverage.py` part (e).

## Schema apply order

`schema.sql` → `knowledge_schema.sql` → `telephony_schema.sql` → `rls.sql`
(4th file, `deployment/sh/init.sh`). Every service still connects on
`POSTGRES_DSN` (the superuser) as of this write-up — `rls.sql` is live
(policies exist, `FORCE ROW LEVEL SECURITY` is set) but inert, because
`SET LOCAL`/`SET ROLE` against a superuser is a no-op and the superuser
bypasses RLS unconditionally regardless. `POSTGRES_APP_DSN`/
`POSTGRES_ADMIN_DSN` exist in `deployment/.env.example`, falling back to
`POSTGRES_DSN` until the per-service cutover (T62) actually happens.

## Current cutover status (read this before assuming RLS is enforced anywhere)

**No service is running against `yuviz_app` today.** Phases 1–8 landed the
entire wiring, the policies, and the full tripwire/regression suite (T1–T58,
all green — `tests/test_rls_isolation.py`, `test_rls_coverage.py`,
`test_no_bare_pool_calls.py`, `test_cross_tenant_admin.py`,
`test_tenant_conn.py`). Phase 9 (rollout) is where this is not finished:

- **T59** (run all six services' own suites against `yuviz_app`) found real,
  quantified failures — not in this feature's own code, but in each
  service's pre-existing unit-test fixtures, which build data through a raw
  `pool.execute()` or call a service function directly with no HTTP request
  ever setting the ambient tenant scope. Full breakdown, root cause and
  per-service counts: `.sdlc/rls-tenant-isolation/04-t59-test-suite-finding.md`.
  This is real debt (hundreds of call sites across six suites) that must be
  paid down before T62 can proceed honestly.
- **T60–T62** (staging shadow verification, the pre-cutover account sweep,
  and the per-service DSN flip) have a written runbook precise enough for a
  real operator to execute — `.sdlc/rls-tenant-isolation/
  05-rollout-runbook.md` — but no staging environment exists to run them
  against here, and the DSN flip was **not** performed. Do not read
  `rls.sql`'s presence in the schema as "RLS is enforcing anything in
  production" until that document says otherwise.

## Bugs found and fixed beyond the original task list

Two real gaps were found by manual verification after the per-service Phase
4–8 builds landed, neither caught by the original T1–T58 enumeration:

1. `libs/tenancy/session.py::_split_tenant()` and `services/config/
   deps.py::assert_tenant_access()` only accepted a tenant value as a
   `str`. A fetched row's `tenant_id` (e.g. `row["tenant_id"]`) is a real
   `uuid.UUID` object from asyncpg, and `uuid.UUID(<uuid.UUID instance>)`
   raises `AttributeError` rather than round-tripping — every such caller was
   silently misrouted into the slug branch and became unresolvable down it.
   Both now accept `uuid.UUID` directly as well as `str`.
2. `database/schema.sql`'s `audit_log.tenant_id` FK defaulted to `RESTRICT`
   (no `ON DELETE` clause specified). Deleting a tenant with even one
   surviving audit-log row was then permanently blocked — including every
   test fixture's own teardown. Changed to `ON DELETE SET NULL`, matching
   `audit_log`'s own "historical record, fail-closed to NULL rather than
   leak" convention already used for its backfill.

One real, additional authorization gap — not a design-doc mismatch, a route
with literally no tenant check of any kind — was found and fixed the same
way:

3. `services/knowledge/routers/retrieval_policies.py` (both `GET` and `PUT`)
   and `services/knowledge/routers/agent_kb.py`'s `GET` list route had no
   tenant check at all. Both now use the same `_authorize_agent`/
   `assert_tenant_access` pattern their sibling routes already used.
   Regression coverage: `services/knowledge/tests/
   test_agent_scoped_routes_api.py` (5 tests, passing).
