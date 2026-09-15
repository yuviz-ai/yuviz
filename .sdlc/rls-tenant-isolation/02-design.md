# Design: Database-Enforced Row-Level Security for Tenant Isolation

## Approach

One GUC pair (`app.tenant_id` UUID + `app.tenant_slug`) is set by a single shared context manager,
`libs/tenancy.tenant_conn()`, which replaces the 67 existing `async with (await get_pool()).acquire()`
sites across six services. The tenant value is not threaded through 67 function signatures — it is
read from a `ContextVar` that answers **"which tenant is this request operating on"**, not "who is
calling". That distinction is the design's load-bearing detail: on this codebase 12 routers are
mounted under a `/tenants/{tenant_slug|tenant_id}/...` prefix and legitimately let a platform-scoped
actor operate on a tenant that is not their own, so a GUC derived from the JWT alone would return
zero rows for every superadmin action. The ContextVar therefore holds a two-field scope record whose
reader resolves **`target if caller is None else caller`** — the client-supplied path segment steers
the GUC *only* for a caller who is themselves platform-scoped (`tenant_id IS NULL`), so on those 11
routers RLS is a genuine second layer rather than a mirror of the URL. It is written in exactly two
places: `deps.py`'s existing decode point, and one new FastAPI dependency attached to those 12 router
declarations — paired there with a second dependency that enforces the *same* predicate at the app
layer, because RLS must never be the only check.
`tenant_conn()` opens a transaction, resolves both GUCs from `tenants` in one statement (so a caller
holding only a slug — Conversation always does — and a caller holding only a UUID both work, and no
table's policy needs a subquery), then yields the connection; `SET LOCAL` therefore always shares a
transaction with the query it guards.

Platform bypass is `SET LOCAL ROLE yuviz_platform` inside a second context manager `platform_conn()`,
not an `app.is_platform` flag in every policy. The trust boundary is identical either way (the same
process holds both capabilities), but `SET LOCAL ROLE` costs zero extra policy clauses on the 30
policies, reverts automatically at transaction end exactly like `SET LOCAL`, needs no second pool or
second DSN, and makes every bypass a single greppable call site rather than a boolean that any future
code path can set. Rollout is ordered so the database can never end up in the one state that causes
an outage — RLS enabled with no policy — and so the whole change is revertible by a `POSTGRES_DSN`
edit, per service, with no schema change.

Four surfaces sit outside the `tenant_conn()` boundary and are each given their own tier and their
own tripwire, because RLS cannot reach any of them: **(a)** 116 **pool-level**
`pool.fetch/fetchrow/fetchval/execute` calls across 30 production modules that never go through
`acquire()` at all and would run with *no GUC set* after cutover — counted per module in
"Conversion inventory" below, alongside the 67 `acquire()` sites, for **183 call sites** total;
**(b)** four read-through **Redis caches** that return a row before Postgres is ever touched, so for
any value served from cache RLS is explicitly *not* the control and the route must carry its own
post-fetch tenant check; **(c)** 53 **flat by-id route declarations across 17 flat routers** whose
tenant is only knowable after the read (Tier 3), fixed per flat *router* rather than per route;
**(d)** two **service-account routes that carry the tenant in the request body** and one that
carries it in a non-`/tenants/` path (Tier 4), where neither a path param nor the caller's JWT names
the tenant. The Tier 2 route-walk tripwire cannot see (c) or (d), so each gets a tripwire keyed to
its own shape.

## Open questions — resolved

**Q1. Platform bypass: `SET LOCAL ROLE yuviz_platform` (a second, NOLOGIN, `BYPASSRLS` role that
`yuviz_app` is a member of), not a policy branch and not a second pool.** A policy branch
(`OR current_setting('app.is_platform', true) = 'true'`) adds a clause to **every one of the 30
policies this design ships** (20 Wave A + 10 Wave B — the parent-join policies need the clause too,
otherwise a bypassing read of `campaigns` still can't reach `campaign_contacts`) — 30 more places a
typo fails open — and makes bypass a value that any code path can set, including one that sets it
accidentally as a default. A second connection pool on a second DSN doubles connection lifecycle in
six services and doubles the credential surface. `SET LOCAL ROLE` gets the operational profile of the
policy branch (one pool, one DSN, one credential) with the trust profile of the second role (bypass
is a Postgres role attribute, visible in `current_user`/`pg_stat_activity`, not an application
assertion the policy has to believe), and it reverts at transaction end by the same mechanism as
`SET LOCAL`. Role *attributes* are not inherited through `GRANT yuviz_platform TO yuviz_app` — only
an explicit `SET ROLE` activates `BYPASSRLS` — so the default posture of every `yuviz_app` connection
is "policies apply".

**Q2. TEXT-slug tables: a second GUC, `app.tenant_slug`, not a subquery in the policy.** Both GUCs
are set by one statement in `tenant_conn()`, so the "non-uniform shape" the PRD worried about exists
only inside that one function; every policy stays a bare column comparison. A subquery-per-policy on
`calls` (the highest-write table on the platform, written by Conversation on every call) was
rejected because it adds a lookup to every statement on the hottest table and makes the policy
depend on `tenants` being readable, and because Conversation *only ever has the slug* — a UUID-only
GUC would have forced a slug→UUID lookup in Conversation anyway, just in a worse place. Doing the
resolution once per transaction in the helper is strictly cheaper and has one failure point.
`calls` and `live_call_interventions` are the only two TEXT-slug tables.

**Q3. `yuviz_app` does not own the tables. Existing owner (the superuser that applies
`database/*.sql`) stays the owner; `yuviz_app` gets DML grants only.** The PRD scopes the new role
as having "no DDL rights", and a table's owner can always `ALTER`/`DROP` it — making `yuviz_app` the
owner would contradict that directly. `FORCE ROW LEVEL SECURITY` is only *needed* for the owner to
be subject to policies; `yuviz_app` as a non-owner, non-`BYPASSRLS` role is subject to them under
plain `ENABLE`. `FORCE` still ships on every table (AC 2) because it is free, it is correct if
ownership ever moves, and it makes the guarantee independent of who owns what. Note honestly: with
a *superuser* owner, `FORCE` changes nothing for that owner — superusers bypass RLS regardless. No
`ALTER TABLE ... OWNER TO` ships in this change.

**Q4. The table list is enumerated below in three waves** (Wave A: 20 tables with a real tenant
column across `schema.sql`, `knowledge_schema.sql` and `telephony_schema.sql` — 19 that have one
today plus `audit_log`, which gains one in this change; Wave B: 10 child tables with no tenant column
that the app layer already treats as tenant-scoped; Out: 3 tables that are genuinely platform-global
or unowned). Read from both schema files in full, not inferred.
20 + 10 = **30 `CREATE POLICY` statements**, the number used in Q1.

**Q5. Conversation does *not* match the other services' shape — confirmed by reading the code.** It
has no `db.py` and no request scope. It holds **two independent, long-lived asyncpg pools** created
directly: `services/conversation/transcript_builder.py:65` (`create_pool(min_size=1, max_size=5)`)
and `services/conversation/tools/policy_resolver.py:101` (same). Both then *do* acquire per
operation (`async with self._pool.acquire()` — 5 sites in TranscriptBuilder, 2 in ToolPolicyResolver),
so no connection is held across a call session; the actual difference is that the tenant is not in a
request context, it is a per-session value the service already carries (`session.py:154 tenant_id`,
`servicer.py:138`, always the **slug**, `""` on the degraded fallback path at
`agent_config.py:169`). So Conversation uses the same `tenant_conn()` helper with the tenant passed
as an explicit argument rather than from the ContextVar. Two of its sites are genuinely
cross-tenant maintenance sweeps and use `platform_conn()` (listed below).

**Q6. The GUC is set from the tenant the *request targets*, not from the caller's JWT — and the two
differ on 12 routers.** This is the correction that the rest of the design hangs on. Four tiers,
in precedence order, resolved by a single `current_scope()` reader so the answer does not depend on
FastAPI dependency evaluation order:

- **Tier 1 — caller tenant (default, and the ceiling).** `deps.get_authenticated_user` calls
  `set_caller_tenant(user.tenant_id)` after decode. Correct for every route whose data belongs to
  the caller's own tenant, and correct as the value a platform-scoped actor gets: `None`.
- **Tier 2 — path target, honoured *only* when the caller is platform-scoped.** Every router mounted
  under `/tenants/{tenant_slug}/…` or `/tenants/{tenant_id}/…` gains
  `dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)]` on its
  `APIRouter(...)` declaration. `bind_path_tenant` reads `request.path_params` for `tenant_slug` or
  `tenant_id`, whichever is present, and calls `set_target_tenant(...)`; it performs **no**
  authorization and raises **no** HTTPException — it is purely "record what tenant this URL is
  about". The target is not a free choice, because `current_tenant()` returns
  **`target if caller is None else caller`**: a caller who has a tenant of their own can never point
  the GUC at another tenant, whatever the URL says. So `bind_path_tenant` being unauthenticated and
  order-independent is safe by construction, and the GUC is never client-controlled for a
  tenant-scoped actor. Router-level dependencies run *before* endpoint dependencies in FastAPI, i.e.
  before `get_authenticated_user` — which is why precedence is a property of the reader, not of call
  order (lesson 1: trace it through the real wiring).

  `require_path_tenant_access` is the app-layer half, and it exists because **9 of the 12 Tier 2
  router declarations have no caller-tenant check at all today** — verified by reading them, not inferred:
  `calls.py:29,41,51` (`get_or_404(get_tenant(slug))` proves the tenant *exists*, never that the
  caller belongs to it), `knowledge_bases.py:17,22`, `campaigns.py:21,26` and `campaigns.py:106,112`
  (`dnc`) take `tenant_id` straight off the path under a bare `get_current_user` /
  `require_role("superadmin","admin")` — a role every tenant admin holds — and `carriers.py:15`,
  `phone_numbers.py:17`, `telephony_configs.py:16`, `tool_provider_configs.py:15`'s
  `_resolve_tenant_id` is a `validate_id_exists` existence check only. **`services/did/routers/numbers.py:34`
  is the worst of the set and was missed by the previous enumeration:** its
  `/tenants/{tenant_id}/numbers` router has no tenant helper of *any* kind — not even an existence
  check — so `search`, `purchase` and the tenant listing take `tenant_id` straight off the path under
  `require_role("superadmin","admin")`. Any tenant admin can therefore **purchase a DID, a billable
  carrier action, into another tenant**, and read that tenant's number inventory; after cutover
  without `bind_path_tenant` the same routes return an empty 200 instead, which hides the hole rather
  than closing it. It gets exactly the same two dependencies as the other 11. Only `agents.py:23`,
  `provider_configs.py:68` and `toolexec/custom_apis.py:38` check the caller. Attaching one
  dependency at each of the 12 router declarations closes all of them in one place and puts the check
  where the next router author will see it. It is a lift-and-reuse of the existing
  `services/toolexec/routers/custom_apis.py:38 _require_tenant_access` — same predicate
  (`deps.is_platform_scoped`), same 403, same detail string — promoted into `deps.py` next to
  `require_role`; `custom_apis.py` and `provider_configs.py` then import it instead of keeping local
  copies.

  **The one deliberate behaviour change in this design, named explicitly:** the exemption predicate
  is `deps.is_platform_scoped` (`tenant_id IS NULL`, lesson 24) — *not* `role == "superadmin"` and
  *not* `is_service_account`, which is what `provider_configs.py:72`, `calls.py:20` and
  `agents.py:28` write today. The two layers must agree on who may cross a tenant boundary, or the
  disagreement resurfaces as a silent empty 200 instead of a 403, which is strictly worse. So an
  account with `role = 'superadmin'` **and** a non-NULL `tenant_id` (`provider_configs.py:81`
  records that these exist as "a leftover default from account creation") loses cross-tenant access
  and gets today's cross-tenant 403/404 instead. Those accounts are enumerated and NULLed out as a
  named pre-cutover step in "Migration and rollout"; it is a one-row operator action, and doing it
  is what keeps AC 6 true for them. The handler-level checks in `agents.py`/`provider_configs.py`/
  `custom_apis.py` are **not** removed — the router dependency runs first and is the effective one,
  and the redundant second check is left as written (defense in depth, and a smaller diff).
- **Tier 3 — flat by-id routes with no tenant anywhere in the request.** The row's tenant is unknown
  until it is read, so there is nothing to put in the GUC before the read, and a platform-scoped
  actor would otherwise hit `TenantUnresolved` on *every* by-id route. The previous draft named only
  four helpers; the real surface is **53 by-id route declarations across 17 flat routers**, and the
  rule below is stated so that it answers "how does a platform-scoped actor reach **any** by-id
  resource", not just the named ones. Enumerated in full in the Tier 3 table under "Interfaces".

  **The rule, uniform across all 17:** a flat router's by-id fetch is performed by exactly one
  resolver per router, and that resolver takes the existing `platform_scoped: bool` keyword that
  `services/config/users.py:55 list_users` already uses (the codebase's convention for "the router
  decided scope, the service obeys"), derived only from `deps.is_platform_scoped(current_user)`
  (`tenant_id IS NULL`, lesson 24 — never `role == "superadmin"`). `True` ⇒ the fetch runs under
  `platform_conn(reason=…)`; `False` ⇒ under `tenant_conn()`, where the policy makes another tenant's
  row invisible and the route's existing `get_or_404` turns that into today's 404 with no new status
  code (lesson 2). **The post-fetch app-layer check is not optional and not supplied by RLS**: after
  the fetch every one of the 17 calls the shared
  `deps.assert_tenant_access(row_tenant_id, current_user)` — the same predicate and the same 403/404
  shapes as `require_path_tenant_access`, extracted so the two tiers cannot drift. Six routers
  already perform this check and keep their code, merely importing the shared predicate
  (`provider_configs._authorize_provider`, `custom_apis._authorize_custom_api`,
  `knowledge_bases._authorize_kb`, `calls._caller_tenant_slug`, `agent_apis`' `tenant_filter`,
  `invites.may_invite`/`_same_tenant`). **Eleven have no post-fetch tenant check today** and gain
  one; that is a tightening, and it is required — a cached read (see "Caches and RLS") can return a
  row that RLS never saw, so on those routes the app-layer check is the *only* control.

- **Tier 4 — service-account routes carrying the tenant in the request BODY (or in a non-`/tenants/`
  path).** No tier covered this before and the Tier 2 route-walk tripwire is structurally blind to
  it, because the path contains no `/tenants/{`. Three sites, all on `/internal` prefixes:
  `services/toolexec/routers/execute.py:49` (`POST /internal/chains/execute`, tenant in
  `body.tenant_id`, gated by `require_execute_subject`'s named-identity allow-list),
  `services/knowledge/routers/retrieve.py:35` (`POST /internal/retrieve`, tenant in
  `body.tenant_slug`, gated by a **bare `get_current_user`** — so today any authenticated account can
  read any tenant's retrieved KB context by supplying another tenant's slug), and
  `services/knowledge/routers/retrieve.py:27`
  (`GET /internal/agents/{tenant_slug}/{agent_slug}/has-knowledge`, tenant in a path segment that is
  not under a `/tenants/` prefix). Handling, following the `live_calls._resolve_scope` precedent
  exactly — an explicit call rather than a router dependency, because only the handler has the body:
  each handler calls `deps.assert_tenant_access(<body or path tenant>, current_user)` and then
  `set_target_tenant(<same value>)`, in that order, immediately after its auth gate. Both are safe
  and correct for the callers that exist: Conversation and the SDK service accounts are
  `tenant_id IS NULL` (lesson 24), so `assert_tenant_access` admits them and `current_tenant()`
  honours the target; a tenant-scoped account gets 403 from the assertion and, even if the assertion
  were removed, is pinned to its own tenant by `current_tenant()`. `execute.py:53-56`'s existing
  body/identity comparison and `require_execute_subject` are both kept unchanged — Tier 4 adds the
  GUC binding and the missing check on `retrieve.py`, and removes nothing.

Under Tier 2, `agents.py`'s `is_unscoped` branch — the site the review flagged — needs *no* code
change beyond the two router-level dependencies: `/tenants/{tenant_slug}/agents` already carries the
target in the URL, so for a platform-scoped superadmin (`caller is None`) `current_tenant()` returns
the target and `tenant_conn()` resolves to tenant X, and AC 6 holds. For a tenant-scoped caller the
GUC is their own tenant and `require_path_tenant_access` has already returned the 404, so the two
layers agree. The same is true of `calls`, `carriers`, `phone_numbers`, `provider_configs`,
`telephony_configs`, `tool_provider_configs`, `campaigns`, `dnc`, `knowledge_bases`,
`custom_apis` and `did`/`numbers`' tenant-scoped routers. `live_calls.py` is the one exception: its tenant arrives as a
`?tenant_slug=` **query** param and is resolved by `_resolve_scope`, which already returns
`(slug, tenant_uuid, effective_user)` — so it calls `set_target_tenant(uuid)` at that return, after
its 403 branches, rather than via `bind_path_tenant`.

## Changes

| File | Change | Why |
|---|---|---|
| `database/rls.sql` (new) | Role creation, GRANTs, `ALTER DEFAULT PRIVILEGES`, per-table `CREATE POLICY` then `ENABLE`/`FORCE`, each table in its own `DO $$` block | 4th schema file; `psql -f` has no `ON_ERROR_STOP` (lesson 13), so each table's DDL must be atomic within its own block |
| `libs/tenancy/__init__.py` (new) | Re-exports `tenant_conn`, `platform_conn`, `set_caller_tenant`, `set_target_tenant`, `current_scope`, `TenantUnresolved` | Framework-free shared module; the six services must not import each other's `db.py` (each `db.py` docstring states it owns only its own pool lifecycle) |
| `libs/tenancy/session.py` (new) | The two context managers, the scope `ContextVar` + precedence reader, the one-statement GUC resolver, bypass counter/log | All the logic lives in exactly one place so a policy change is a one-file change |
| `services/config/deps.py` | `get_authenticated_user` calls `set_caller_tenant(user.tenant_id)` after decode, before the console-role gate; add `bind_path_tenant` and `require_path_tenant_access` next to `require_role` (the latter lifted verbatim from `toolexec/routers/custom_apis.py:38`) | The single decode point every authenticated route in all five HTTP services flows through (lesson 9); `bind_path_tenant` lives with the other shared FastAPI deps for the same reason |
| `services/config/routers/{agents,calls,carriers,phone_numbers,provider_configs,telephony_configs,tool_provider_configs}.py` | Add `dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)]` to the 7 `/tenants/{…}/…` router declarations (`agents.py:16`, `calls.py:10`, `carriers.py:11`, `phone_numbers.py:13`, `provider_configs.py:14`, `telephony_configs.py:11`, `tool_provider_configs.py:11`); `provider_configs._require_tenant_access` (`:68`) becomes an import of the shared one | Tier 2 — without the first, every superadmin-on-tenant-X action returns zero rows; without the second, `calls`, `carriers`, `phone_numbers`, `telephony_configs` and `tool_provider_configs` have no caller-tenant check at all |
| `services/knowledge/routers/knowledge_bases.py` | Same two dependencies on `tenant_scoped_router` (`:13`); `_authorize_kb` (`:58`) gains Tier 3 `platform_scoped` | Tier 2 + Tier 3; `list_knowledge_bases` (`:17`) and `create_knowledge_base` (`:22`) have **no** caller-tenant check today |
| `services/toolexec/routers/custom_apis.py` | Same two on `tenant_scoped_router` (`:32`); local `_require_tenant_access` (`:38`) becomes an import of the shared one; `_authorize_custom_api` (`:47`) gains Tier 3 `platform_scoped` | Tier 2 + Tier 3; this file's helper is the one being promoted |
| `services/campaigns/routers/campaigns.py` | Same two dependencies on `tenant_scoped_router` (`:17`) and `dnc_tenant_router` (`:103`); the 9 flat by-id routes get Tier 3 | Tier 2; `list/create_campaign` (`:21,26`) and `list/add_dnc_number` (`:106,112`) have **no** caller-tenant check today |
| `services/did/routers/numbers.py` | Same two dependencies on `tenant_scoped_router` (`:34`); the 2 flat by-id routes (`:103,121`) get Tier 3 | **Tier 2, missed by the first enumeration.** No caller-tenant check on any route today — cross-tenant DID *purchase* (a billable carrier action) and cross-tenant inventory listing |
| `services/config/routers/{telephony_configs,carriers,phone_numbers,tool_provider_configs,agent_tool_policies}.py` | Tier 3 on the flat routers: resolver gains `platform_scoped`, handler gains `deps.assert_tenant_access(row, user)` | 11 of the 17 flat routers have no post-fetch tenant check; `telephony_configs` is also a **cached** read, where the route check is the only possible control |
| `services/config/routers/users.py` + `services/config/users.py` | New `get_user_for_admin(user_id, *, platform_scoped)`; `PATCH`/`DELETE /users/{user_id}` fetch → `assert_tenant_access(row)` → mutate, and `PATCH` additionally asserts the **new** `tenant_id` before writing; `get_user_by_id` keeps its four identity-resolution callers and holds the single `platform_conn(reason="identity-resolution")`; `list_users` uses `platform_conn` whenever `tenant_id is None` | The only Tier 3 router with no fetch and no row check today, and the only one whose updatable fields are `role`/`tenant_id` — `{"tenant_id": null}` is self-promotion into the platform scope this design keys on |
| `services/knowledge/routers/{documents,agent_kb}.py` | Same Tier 3 treatment | By-id routes keyed on `document_id`/`agent_id` with no tenant check |
| `services/knowledge/routers/retrieve.py` | Tier 4: `POST /internal/retrieve` (`:35`) and `GET /internal/agents/{tenant_slug}/…` (`:27`) call `assert_tenant_access(body.tenant_slug / tenant_slug, user)` then `set_target_tenant(...)` | Tenant is in the body/a non-`/tenants/` path, so Tier 2's route walk is blind to it; `/internal/retrieve` is gated on bare `get_current_user` today |
| `services/toolexec/routers/execute.py` | Tier 4: after `require_execute_subject` (`:33`) and the existing body check (`:53`), call `set_target_tenant(body.tenant_id)` | Service-account route with the tenant in the body; without this, `executor`'s 11 pool-level calls run with no GUC |
| `services/config/routers/invites.py` | `POST /invites` becomes Tier 4 (`assert_tenant_access` + `set_target_tenant` on `body.tenant_id`); `resend`/`revoke` become Tier 3 | Narrows the `invites.py` bypass off `create_invite`, the module's one high-privilege write |
| `services/config/routers/live_calls.py` | `_resolve_scope` calls `set_target_tenant(tenant["id"])` immediately before its successful return | Tenant is a query param, not a path param; must land after the existing 403 branches, not before |
| `services/config/routers/provider_configs.py` | `_authorize_provider` (`:77`) + its service call gain `platform_scoped` | Tier 3 — by-id route with an `is_unscoped` branch at `:106-107` |
| `services/config/routers/calls.py` | `_caller_tenant_slug` returning `None` selects `platform_conn` for the flat `/calls` router's queries; its `is_unscoped` (`:20`) narrows to `is_platform_scoped` | Tier 3 — the existing `is_unscoped` branch at `:20`; the narrowing keeps the bypass predicate and the Tier 2 predicate identical (lesson 24) |
| `database/schema.sql` | `audit_log` gains `tenant_id UUID REFERENCES tenants(id)` (nullable) defaulted from the RLS GUC, plus `audit_log_tenant_idx` | `audit_log` holds `old_value`/`new_value` **full-row snapshots** of each tenant's agents, provider configs, campaigns and users — it is tenant data that merely lacked a tenant column |
| `services/config/routers/audit_log.py` + `services/config/audit.py` | `list_audit_log` takes `tenant_id` + `platform_scoped` (the `users.py:55 list_users` convention); route derives them from `deps.is_platform_scoped(current_user)`, service adds `AND tenant_id = $n` on the scoped branch and picks `platform_conn`/`tenant_conn` | `:20` gates on role only, so a superadmin scoped to tenant A reads every tenant's mutation history (lesson 24 inverted: role checked, scope not) |
| `services/toolexec/agent_apis.py` | `tenant_filter is None` branch (`:182`) → `platform_conn`; the by-id check at `:66` gains `platform_scoped` | Tier 3; predicate already in the code |
| `services/config/*.py` (agents, provider_configs, tenants, phone_numbers, carriers, telephony_configs, users, invites, workflows, live_calls, agent_tool_policies, tool_provider_configs, audit, cache, **calls**) | Both patterns: `(await get_pool()).acquire()` → `tenant_conn()`, **and** bare `pool.fetch*/execute` → the same context manager; pre-auth/platform paths → `platform_conn()` | **32 `acquire()` + 53 pool-level = 85 sites.** `calls.py` (11 pool-level, 0 `acquire`) was absent from the first file list entirely |
| `services/campaigns/{campaigns,campaign_contacts,audit,**dnc**,**worker**}.py` | Same, both patterns | 3 `acquire()` + 9 pool-level = 12 sites. `dnc.py` (4) and `worker.py` (1) were absent from the first list |
| `services/toolexec/{custom_apis,admission,executor,audit,auth_schemes,agent_apis}.py` | Same, both patterns | 5 `acquire()` + 20 pool-level = 25 sites; `executor.py` alone has 11 pool-level calls |
| `services/knowledge/{knowledge_bases,documents,retrieval_policies,retrieval,vector_repository,audit,**agent_kb**}.py` | Same, both patterns; `retrieval.py`/`vector_repository.py` receive the pool as an argument, so the **caller** (`routers/retrieve.py`, Tier 4) opens the scope and passes the connection | 5 `acquire()` + 27 pool-level = 32 sites. `agent_kb.py` (8 pool-level) was absent from the first list |
| `services/knowledge/ingestion_worker.py` | 1 `acquire()` + 7 pool-level → `platform_conn()` per job; the poll loop stays **outside** the context manager | A worker loop inside a transaction pins a snapshot and holds locks for the loop's lifetime |
| `services/did/{purchased_numbers,carriers,audit}.py` | Same, both patterns | 2 `acquire()` + 4 pool-level. **did is not in the PRD's list of five but connects on the same `POSTGRES_DSN` and queries `purchased_numbers`/`carriers`/`phone_numbers`** — unwired, it returns zero rows after cutover |
| `services/conversation/transcript_builder.py` | Cache the call's tenant slug per `session_id` alongside the existing `_turn_counts`/`_barge_in_counts` dicts; per-session writes use `tenant_conn(explicit_tenant=<slug>, reason="conversation-session-write")`; the inactive-call reconcile sweep uses `platform_conn()` | Slug is available at `begin_call`; the sweep is cross-tenant by definition |
| `services/conversation/tools/policy_resolver.py` | Add `tenant_slug` to the resolve entry point and to the cache key; both acquire sites use `tenant_conn(explicit_tenant=<slug>, reason="conversation-tool-policy")` | Its queries hit UUID-keyed `agents`/`custom_apis`/`agent_custom_apis` with no tenant in scope today; unwired it silently resolves zero tools |
| `scripts/{create_superadmin,create_service_account,seed_default_config}.py` | Read `POSTGRES_ADMIN_DSN`, falling back to `POSTGRES_DSN` | PRD constraint: admin scripts write `tenant_id IS NULL` rows and must keep bypassing RLS |
| `deployment/docker/*` + `deployment/sh/dev.sh` + `scripts/start_local.sh` | Apply `database/rls.sql` as the 4th schema file; per-service `POSTGRES_DSN` now the `yuviz_app` DSN; `POSTGRES_ADMIN_DSN` for scripts | Cutover and rollback are both a DSN edit |
| `tests/test_rls_isolation.py` (new) | Negative suite, connects as `yuviz_app` directly (no app layer) | AC 3/4/5/10 |
| `tests/test_rls_coverage.py` (new) | Two tripwires: every `tenant_id` table has RLS + FORCE + ≥1 policy; every `/tenants/{…}` router has **both** `bind_path_tenant` and `require_path_tenant_access` | Catches all three fail-open shapes: a new table without a policy, a new tenant-scoped router without the GUC, and one without the app-layer check |
| `tests/test_tenant_conn.py` (new) | Precedence, pool-reuse leak (`min_size=1`), fail-closed-on-unresolved | AC 8/9 |
| `tests/test_cross_tenant_admin.py` (new) | AC 6 matrix: superadmin acting on tenant X through each of the 12 Tier 2 routers, the 17 Tier 3 flat routers and the 3 Tier 4 sites | The review's blocking findings, named by site |
| `tests/test_no_bare_pool_calls.py` (new) | AST tripwire: no module under `services/` may call `.fetch/.fetchrow/.fetchval/.execute/.executemany` on a `Pool`-typed/`get_pool()`-derived name | The 116 pool-level sites are the failure mode a per-module checklist demonstrably misses; this is the only check that trips when the 117th is added (lesson 12) |
| `CURSOR.md` | Schema apply order gains `rls.sql`; Multi-tenant model section gains the caller-vs-target rule, the four tiers, and the "never call `pool.fetch*` directly" rule | It is the file an agent reads first |
| `docs/rls-tenant-isolation.md` (new) | **README deliverable — belongs in the plan, not written here.** Must be a checklist: role per service, the **four** tiers, exact wiring location per service, table list, how bypass works, the explicit-override list, the cache-key rule and the statement that **RLS is not a control for any value served from cache**, and what a new table / tenant-path router / flat by-id route / body-tenant route / service / pool / cache must do | AC 11 |

## Conversion inventory — all 183 call sites

The first draft counted only the 67 `async with (await get_pool()).acquire()` sites. That is 37% of
the real surface. **116 further production calls go straight to the pool** —
`pool.fetch/fetchrow/fetchval/execute/executemany`, no `acquire()`, no transaction — across 30
modules. asyncpg runs each of those on an arbitrary pooled connection with **no GUC set**, so after
cutover every one of them reads zero rows (or fails `WITH CHECK` on a write) while looking, from the
application's side, exactly like "this tenant has no data". They are not a smaller version of the
`acquire()` problem; they are the larger half of it. Counts are `grep -rn` over `services/`,
excluding `*/tests/*`:

| Module | `acquire()` | pool-level | Notes |
|---|---:|---:|---|
| `services/toolexec/executor.py` | 0 | 11 | Entered only via Tier 4 `POST /internal/chains/execute` |
| `services/config/calls.py` | 0 | 11 | **Absent from the first file list.** Tier 2 `/tenants/{slug}/calls` + Tier 3 `/calls/{session_id}` |
| `services/knowledge/agent_kb.py` | 0 | 8 | **Absent from the first file list.** Tier 3 + Tier 4 (`has-knowledge`) |
| `services/knowledge/ingestion_worker.py` | 1 | 7 | `platform_conn` per job |
| `services/config/workflows.py` | 1 | 7 | Under Tier 2 `agents` routes |
| `services/knowledge/retrieval.py` | 0 | 6 | Pool passed in by `routers/retrieve.py` (Tier 4) |
| `services/config/users.py` | 5 | 6 | `list_users` already carries `is_platform_scoped` |
| `services/toolexec/agent_apis.py` | 2 | 5 | Tier 3 |
| `services/conversation/transcript_builder.py` | 5 | 4 | Own pool; explicit-tenant call sites |
| `services/toolexec/custom_apis.py` | 3 | 4 | Tier 2 + Tier 3 |
| `services/config/tenants.py` | 3 | 4 | Out of RLS scope → `platform_conn` |
| `services/config/phone_numbers.py` | 3 | 4 | `prewarm()` is a bypass; rest Tier 2/3 |
| `services/campaigns/dnc.py` | 0 | 4 | **Absent from the first file list** |
| `services/campaigns/campaigns.py` | 2 | 4 | |
| `services/campaigns/campaign_contacts.py` | 1 | 4 | Wave B child table |
| `services/config/invites.py` | 4 | 3 | Bypass narrowed — see "Bypass sites" |
| `services/config/telephony_configs.py` | 4 | 3 | Includes the **cached** `get_telephony_config` fill |
| `services/config/agents.py` | 3 | 3 | |
| `services/knowledge/knowledge_bases.py` | 3 | 3 | |
| `services/did/purchased_numbers.py` | 2 | 3 | |
| `services/config/provider_configs.py` | 3 | 2 | Includes the **cached** `get_provider_config` fill |
| `services/config/tool_provider_configs.py` | 3 | 2 | |
| `services/config/carriers.py` | 3 | 2 | |
| `services/config/agent_tool_policies.py` | 3 | 2 | |
| `services/config/audit.py` | 0 | 2 | Must use the mutation's own connection, never its own |
| `services/knowledge/documents.py` | 3 | 2 | |
| `services/config/live_calls.py` | 4 | 0 | Tier 2-equivalent via `_resolve_scope` |
| `services/toolexec/admission.py` | 2 | 0 | |
| `services/conversation/tools/policy_resolver.py` | 2 | 0 | Own pool; explicit-tenant |
| `services/knowledge/retrieval_policies.py` | 1 | 0 | |
| `services/knowledge/vector_repository.py` | 0 | 1 | Pool passed in |
| `services/did/carriers.py` | 0 | 1 | |
| `services/campaigns/worker.py` | 0 | 1 | `platform_conn` — cross-tenant due-campaign scan |
| `services/config/db.py` | 1 | 0 | The pool factory itself; unchanged |
| **Total** | **67** | **116** | **183** |

`scripts/create_service_account.py` has 1 further pool-level call and is **not** converted — scripts
move to `POSTGRES_ADMIN_DSN` and keep bypassing RLS (PRD constraint).

Conversion rule, identical for both patterns: `pool.fetchrow(q, …)` becomes
`async with tenant_conn(pool) as conn: await conn.fetchrow(q, …)`. Bypass and explicit-tenant sites
are chosen from the enumerated lists in this document, not per call site. The planner must task this
**per module with its two counts**, not as "convert the acquire sites"; and
`tests/test_no_bare_pool_calls.py` is what keeps the count at zero afterwards, because a written
inventory has now been wrong twice.

## Data

New file `database/rls.sql`, applied after `telephony_schema.sql`. Run as the current superuser.

### Roles and grants

```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yuviz_platform') THEN
        CREATE ROLE yuviz_platform NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yuviz_app') THEN
        CREATE ROLE yuviz_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
            PASSWORD :'yuviz_app_password';
    END IF;
END $$;

-- Idempotent re-assertion: AC 1 must hold even if the role pre-existed.
ALTER ROLE yuviz_app     NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
ALTER ROLE yuviz_platform NOSUPERUSER BYPASSRLS  NOCREATEDB NOCREATEROLE NOLOGIN;

GRANT yuviz_platform TO yuviz_app;   -- membership only; BYPASSRLS activates on SET ROLE, never inherited

GRANT USAGE ON SCHEMA public TO yuviz_app, yuviz_platform;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO yuviz_app, yuviz_platform;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA public TO yuviz_app, yuviz_platform;
REVOKE CREATE ON SCHEMA public FROM yuviz_app, yuviz_platform;

-- New tables created by the schema-applying role get grants automatically; this is
-- the mitigation for "someone adds a table and forgets to GRANT", which fails closed.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO yuviz_app, yuviz_platform;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO yuviz_app, yuviz_platform;
```

The password is supplied by the operator (`psql -v yuviz_app_password=...`); it is never committed.
`deployment/.env` gains `POSTGRES_APP_DSN` and `POSTGRES_ADMIN_DSN`.

### `audit_log` gains a tenant column

The only schema change outside `rls.sql`, appended to `database/schema.sql` beside the existing
`audit_log` definition (`schema.sql:348`) so the table keeps one home:

```sql
ALTER TABLE audit_log
    ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id)
        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid;
CREATE INDEX IF NOT EXISTS audit_log_tenant_idx ON audit_log(tenant_id, changed_at DESC);
```

Nullable, exactly like `users.tenant_id` / `user_invites.tenant_id`: NULL means platform scope. The
GUC-valued `DEFAULT` is what makes this a small change — `audit.write_audit(conn, …)` is always
called **on the mutation's own connection, inside the mutation's own transaction** (its module
docstring says so, and every caller passes `conn`), so the row is stamped with the same tenant the
surrounding `tenant_conn()` resolved, in all six services, with **zero changes to any `audit.py`
writer or any of its ~40 call sites**. It cannot drift from the policy because it *is* the policy's
expression; and `WITH CHECK` rejects any explicit value that disagrees. A write performed under `platform_conn()` sets no GUC, so the
default yields `NULL`. That is correct for a mutation that genuinely has no tenant (redemption of a
platform invite, NULL-tenant user CRUD) and **wrong for a Tier 3 platform-branch mutation**, where a
platform superadmin edits a row that *does* belong to a tenant — the edit would then be missing from
that tenant's own newly-scoped audit log. So `platform_conn` takes `stamp_tenant=` (signature under
"Interfaces"): when given, it issues the same one-statement
`set_config('app.tenant_id'/'app.tenant_slug', …, true)` resolver `tenant_conn` uses, *in addition
to* `SET LOCAL ROLE yuviz_platform`. Under `BYPASSRLS` the GUC restricts nothing, so
`audit_log.tenant_id`'s `DEFAULT` is its only consumer: this changes what the audit row records and
nothing else, with no change to `write_audit` or its ~40 call sites. **The rule, stated in the
README: every `platform_conn()` that mutates on behalf of a known tenant passes `stamp_tenant=`;
only a mutation with genuinely no tenant omits it.** On Tier 3 the value is `row["tenant_id"]` from
the fetch that already precedes the mutation — and is legitimately `None` when that row is itself
platform-scoped (a NULL-tenant user, a platform invite), which is the correct NULL. On the
cross-tenant workers (`ingestion_worker`, `campaigns/worker`, `transcript_builder`'s sweep) each
per-row unit of work passes the tenant it just read; only the scan that *selects* the work omits it. `ADD COLUMN` with a non-volatile default evaluates once at
DDL time with no GUC set, so every pre-existing row starts NULL and is then backfilled below.

Backfill, in its own `DO` block in `database/rls.sql` **before** the `audit_log` policy block. It is
guarded by `a.tenant_id IS NULL` so it is idempotent and re-runnable, and if it fails the rows stay
NULL — invisible to `yuviz_app`, i.e. fail-closed, no leak and no outage (lessons 10/13):

```sql
DO $$
DECLARE m RECORD;
BEGIN
    FOR m IN SELECT * FROM (VALUES
        ('agent','agents'), ('campaign','campaigns'), ('carrier','carriers'),
        ('custom_api','custom_apis'), ('invite','user_invites'), ('kb_document','kb_documents'),
        ('knowledge_base','knowledge_bases'), ('phone_number','phone_numbers'),
        ('provider_config','provider_configs'), ('purchased_number','purchased_numbers'),
        ('telephony_config','telephony_configs'), ('tool_provider_config','tool_provider_configs'),
        ('user','users')
    ) AS v(entity_type, tbl) LOOP
        EXECUTE format(
            'UPDATE audit_log a SET tenant_id = t.tenant_id FROM %I t '
            'WHERE t.id = a.entity_id AND a.entity_type = $1 AND a.tenant_id IS NULL', m.tbl)
        USING m.entity_type;
    END LOOP;

    -- entity_id IS the tenant for 'tenant' rows.
    UPDATE audit_log a SET tenant_id = a.entity_id
     WHERE a.entity_type = 'tenant' AND a.tenant_id IS NULL;

    -- Three child entities have no tenant column of their own; they inherit via agents.
    UPDATE audit_log a SET tenant_id = ag.tenant_id
      FROM agent_tool_policies p JOIN agents ag ON ag.id = p.agent_id
     WHERE p.id = a.entity_id AND a.entity_type = 'agent_tool_policy' AND a.tenant_id IS NULL;
    UPDATE audit_log a SET tenant_id = ag.tenant_id
      FROM agent_retrieval_policies p JOIN agents ag ON ag.id = p.agent_id
     WHERE p.id = a.entity_id AND a.entity_type = 'agent_retrieval_policy' AND a.tenant_id IS NULL;
    UPDATE audit_log a SET tenant_id = ag.tenant_id
      FROM agent_workflow_versions v JOIN agents ag ON ag.id = v.agent_id
     WHERE v.id = a.entity_id AND a.entity_type = 'agent_workflow' AND a.tenant_id IS NULL;

    -- live_call_interventions.tenant_id is a TEXT slug, so it joins through tenants.slug.
    UPDATE audit_log a SET tenant_id = t.id
      FROM live_call_interventions i JOIN tenants t ON t.slug = i.tenant_id
     WHERE i.id = a.entity_id AND a.entity_type = 'live_call_intervention' AND a.tenant_id IS NULL;
END $$;
```

Rows whose entity was hard-deleted match nothing and stay NULL — platform-readable only. That is the
correct failure direction, and it is stated in the README so nobody reads an incomplete tenant
history as a bug in the policy.

### Policy templates

Order inside each block is load-bearing: **the policy is created while RLS is still disabled, and
RLS is enabled only after the policy exists.** A failure at any point therefore leaves the table
either with no RLS (today's behaviour, fail-open, no outage) or with RLS and a correct policy —
never RLS with no policy, which denies everything to `yuviz_app`. `DROP POLICY IF EXISTS` +
`CREATE POLICY` sit in the same `DO` block as the `ENABLE`, so a failed `CREATE` rolls the `DROP`
back (lesson 13: `psql -f` is autocommit-per-statement, so a block is the only atomic unit).

UUID-keyed template (Wave A, 18 tables):

```sql
DO $$
BEGIN
    DROP POLICY IF EXISTS <table>_tenant_isolation ON <table>;
    CREATE POLICY <table>_tenant_isolation ON <table>
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
    ALTER TABLE <table> FORCE  ROW LEVEL SECURITY;
END $$;
```

`NULLIF(..., '')` is what makes AC 4 an empty result rather than an error: `current_setting(x, true)`
returns `''` when unset, `''::uuid` would raise `invalid input syntax for type uuid`, and
`NULL = tenant_id` is NULL → zero rows, and a `WITH CHECK` of NULL rejects the write. `FOR ALL TO
yuviz_app` scopes the policy to the app role only, so `yuviz_platform` is never even evaluated.

TEXT-slug template (Wave A, 2 tables — `calls`, `live_call_interventions`):

```sql
        USING      (tenant_id = NULLIF(current_setting('app.tenant_slug', true), ''))
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_slug', true), ''))
```

Parent-join template (Wave B, 10 tables with no tenant column). The parent's own policy applies
inside the subquery, so this needs no GUC of its own and cannot drift from the parent:

```sql
        USING      (EXISTS (SELECT 1 FROM <parent> p WHERE p.<pk> = <table>.<fk>))
        WITH CHECK (EXISTS (SELECT 1 FROM <parent> p WHERE p.<pk> = <table>.<fk>))
```

### Wave A — tables with their own tenant column (20)

| Table | Column type | Source file |
|---|---|---|
| `provider_configs` | UUID | schema.sql:37 |
| `agents` | UUID | schema.sql:69 |
| `tool_provider_configs` | UUID | schema.sql:190 |
| `users` | UUID **nullable** | schema.sql:226 |
| `user_invites` | UUID **nullable** | schema.sql:306 |
| `carriers` | UUID | schema.sql:363 |
| `phone_numbers` | UUID | schema.sql:387 |
| `purchased_numbers` | UUID | schema.sql:432 |
| `campaigns` | UUID | schema.sql:547 |
| `dnc_numbers` | UUID | schema.sql:598 |
| `custom_apis` | UUID | schema.sql:808 |
| `api_chain_runs` | UUID | schema.sql:895 |
| `api_side_effect_claims` | UUID | schema.sql:959 |
| `calls` | **TEXT slug** | schema.sql:445 |
| `live_call_interventions` | **TEXT slug** | schema.sql:1015 |
| `knowledge_bases` | UUID | knowledge_schema.sql:21 |
| `kb_documents` | UUID | knowledge_schema.sql:46 |
| `kb_chunks` | UUID | knowledge_schema.sql:97 |
| `telephony_configs` | UUID | telephony_schema.sql:13 |
| `audit_log` | UUID **nullable**, added by this change | schema.sql:348 |
| *(18 UUID + 2 TEXT)* | | |

`users`, `user_invites` and `audit_log` are nullable by design (`NULL = platform-scoped`). Under the
UUID template a NULL-tenant row matches nothing, so platform accounts and platform-scoped audit rows
are invisible to `yuviz_app` and reachable only through `platform_conn()` — which is correct, and is
exactly why the pre-auth paths below must be bypass sites.

### Wave B — child tables, no tenant column, app already treats them as tenant-scoped (10)

These are in scope under the PRD's own carve-out ("unless a service's existing app-layer code
already treats them as tenant-scoped"). A leak here is not cosmetic: `campaign_contacts` is a
contact list of phone numbers and `custom_api_params.literal_value` holds per-tenant API values.

| Table | Parent / FK | Source |
|---|---|---|
| `agent_tool_policies` | `agents(id)` via `agent_id` | schema.sql:210 |
| `agent_workflow_versions` | `agents(id)` via `agent_id` | schema.sql:335 |
| `campaign_contacts` | `campaigns(id)` via `campaign_id` | schema.sql:572 |
| `custom_api_params` | `custom_apis(id)` via `custom_api_id` | schema.sql:843 |
| `agent_custom_apis` | `agents(id)` via `agent_id` | schema.sql:873 |
| `api_chain_steps` | `api_chain_runs(id)` via `run_id` | schema.sql:918 |
| `transcript_entries` | `calls(session_id)` via `session_id` | schema.sql:510 |
| `agent_knowledge_bases` | `agents(id)` via `agent_id` | knowledge_schema.sql:129 |
| `agent_retrieval_policies` | `agents(id)` via `agent_id` | knowledge_schema.sql:148 |
| `kb_ingestion_jobs` | `kb_documents(id)` via `document_id` | knowledge_schema.sql:163 |

### Out of scope (no tenant column, genuinely platform-global or unowned)

`tenants` (it *is* the tenant, and `tenant_conn()`'s own resolver reads it),
`conversation_node_heartbeats` (infrastructure), `kamailio_cdr` (written by Kamailio, not by any of
these services). Three tables. Each is listed in the README with the reason, so the next reader does
not re-litigate it. `audit_log` is **not** on this list: it looked platform-global because it lacks a
tenant column, but it stores per-tenant before/after row snapshots, so it is Wave A and gains the
column above.

### Indexes

No index changes. Every Wave A table already has a `tenant_id`-leading index
(`idx_agents_tenant`, `idx_calls_tenant`, `idx_campaigns_tenant`, `idx_kb_chunks_tenant`,
`idx_telephony_configs_tenant`, …), which is what the policy predicate needs. Wave B's `EXISTS`
subqueries resolve on the parent's primary key. If a plan regression appears in staging, it is
reported explicitly rather than fixed silently (PRD scope).

## Interfaces

`libs/tenancy/session.py`:

```python
class TenantUnresolved(RuntimeError): ...

@dataclass(frozen=True)
class TenantScope:
    caller: str | None = None   # tenant UUID from the JWT; None = platform-scoped actor
    target: str | None = None   # tenant UUID or slug taken from the request's own path/query

_scope: ContextVar[TenantScope]          # default TenantScope()

def set_caller_tenant(tenant_id: str | None) -> None: ...   # replaces .caller, keeps .target
def set_target_tenant(tenant: str | None) -> None: ...      # replaces .target, keeps .caller
def current_scope() -> TenantScope: ...
def current_tenant() -> str | None:
    """`target if caller is None else caller`.

    A caller who has a tenant of their own is pinned to it; the path/query
    target is honoured only for a genuinely platform-scoped actor
    (`tenant_id IS NULL`, deps.is_platform_scoped — lesson 24). This is what
    makes RLS an independent second layer instead of a mirror of the URL.
    """

class TenantScopeConflict(RuntimeError): ...

@asynccontextmanager
async def tenant_conn(
    pool: asyncpg.Pool,
    *,
    explicit_tenant: str | None = None,  # UUID *or* slug; only for the enumerated no-request-context sites
    reason: str | None = None,           # mandatory whenever explicit_tenant is given
) -> AsyncIterator[asyncpg.Connection]: ...

@asynccontextmanager
async def platform_conn(
    pool: asyncpg.Pool,
    *,
    reason: str,
    stamp_tenant: str | None = None,  # UUID or slug; sets the GUCs for audit_log's DEFAULT only
) -> AsyncIterator[asyncpg.Connection]: ...
```

The separate `tenant_id=` / `slug=` keywords of the first draft are **removed**: they reintroduced
the "whatever the caller passes wins" shape that removing `target`-wins from `current_tenant()`
exists to eliminate, and an ad-hoc `tenant_conn(pool, tenant_id=<anything>)` would have been a silent
per-call-site bypass with none of `platform_conn`'s greppability. The replacement has three
properties that make an explicit tenant the *same* kind of enumerated exception a bypass is:

- **`reason=` is mandatory with `explicit_tenant`** (`ValueError` otherwise), exactly like
  `platform_conn(reason=…)`, so every override is greppable and logged.
- **The caller still wins.** If `current_scope().caller is not None` and `explicit_tenant` does not
  resolve to it, `tenant_conn` raises `TenantScopeConflict` before yielding. So even inside a request
  an override cannot move the GUC off the caller's own tenant — the override is usable only where
  there is genuinely no caller scope, which is precisely the legitimate set.
- **The legitimate set is enumerated and asserted**, in the same table as the bypass sites and by the
  same tripwire (`tests/test_rls_coverage.py` collects the `reason=` string literals passed to both
  helpers across `services/` and asserts the set equals the union of the two tables below). Adding an
  override site without adding its row fails the test.

The complete explicit-override list — three sites, all with no request context and all Conversation-
or worker-side:

| Site | `reason=` | Why there is no ContextVar to read |
|---|---|---|
| `services/conversation/transcript_builder.py` per-session writes | `"conversation-session-write"` | gRPC session, not an HTTP request; tenant is the per-session slug (`session.py:154`) |
| `services/conversation/tools/policy_resolver.py` (2 sites, `:101`) | `"conversation-tool-policy"` | Same; slug threaded in from the resolve entry point |
| `services/knowledge/ingestion_worker.py` per-job body | `"kb-ingestion-job"` | Job claimed under `platform_conn`; the per-job work then re-scopes to the job's own tenant so the job body is policy-checked |

`current_tenant()`'s precedence — **the target is honoured only when `caller is None`** — is the
whole fix for the review's blocking finding, and it is a property of the reader, not of the order two
setters happen to run in. A tenant-scoped caller therefore cannot move the GUC off their own tenant
by any URL, query string or body, which is what makes the policies a second layer: with this reader,
`GET /tenants/<B-slug>/calls` as a viewer of A reads zero rows and
`POST /tenants/<B-uuid>/knowledge-bases` as an admin of A is rejected by `WITH CHECK`, *even before*
`require_path_tenant_access` 403s it. AC 6 is unaffected: a platform actor has `caller is None` by
definition, so the target still wins for exactly the case AC 6 describes.
Both setters are idempotent within a request; nothing clears the other's field. Because
`set_target_tenant` accepts either a UUID or a slug (the 12 routers split 2/10 between
`{tenant_slug}` and `{tenant_id}`), `tenant_conn` passes an ambiguous value as **both** `$1` and
`$2` to the resolver below, which disambiguates by shape — a non-UUID string simply fails the
`$1::uuid` cast branch. `set_target_tenant` therefore stores the raw string and `tenant_conn`
attempts a `uuid.UUID(...)` parse to decide which argument slot it fills.

`services/config/deps.py` (alongside `require_role`, same module, same import surface):

```python
async def bind_path_tenant(request: Request) -> None:
    """Router-level dependency for /tenants/{tenant_slug|tenant_id}/… routers.
    Records the target tenant for RLS. Performs no authorization and raises
    nothing. Safe unauthenticated because current_tenant() ignores the target
    for any caller that has a tenant of their own."""


async def require_path_tenant_access(
    request: Request, current_user: CurrentUser = Depends(get_current_user),
) -> None:
    """Router-level dependency, paired with bind_path_tenant on all 12
    tenant-path routers. Delegates to assert_tenant_access below, which is
    lifted from toolexec/routers/custom_apis.py:38 — same predicate, same
    detail string:

        is_platform_scoped(current_user) -> allowed (tenant_id IS NULL only)
        path {tenant_id}   != current_user.tenant_id -> 403
                              "tenant_id does not match the caller's tenant"
        path {tenant_slug} -> resolve by slug; mismatch -> 404
                              f"tenant {slug!r} not found"

    The 403/404 split is not new: it reproduces the two existing precedents
    exactly. A {tenant_id} path already carries the UUID the caller supplied,
    so there is no existence to leak (custom_apis.py:38-42); a {tenant_slug}
    path must 404 identically for 'missing' and 'not yours' or it becomes a
    slug oracle (agents.py:24-30, lesson 2). Runs before bind_path_tenant's
    effect can matter because no tenant-scoped query executes in between.
    """


def assert_tenant_access(tenant: str | None, current_user: CurrentUser) -> None:
    """The one predicate, shared by Tier 2 (a path segment), Tier 3 (a
    fetched row's tenant_id) and Tier 4 (a request body field), so the
    three tiers cannot drift apart:

        is_platform_scoped(current_user) -> allowed (tenant_id IS NULL only)
        UUID argument, mismatch -> 403 "tenant_id does not match the caller's tenant"
        slug argument, mismatch or unknown -> 404 f"tenant {slug!r} not found"

    `tenant is None` means a platform-scoped row (a NULL-tenant user,
    invite or audit row) and is allowed only for a platform-scoped
    caller. require_path_tenant_access is a Request-reading wrapper over
    this; Tier 3 and Tier 4 call it directly.

    It is authorization only. It never sets a GUC, so it is safe to call
    after a fetch that a cache satisfied without touching Postgres --
    which is exactly why the cached reads depend on it.
    """
```

Tier 3 signature change on every one of the 17 flat routers' resolvers, matching
`services/config/users.py:55 list_users(*, tenant_id, is_platform_scoped)`. The six already-named
ones are illustrative, not the whole list — the whole list is the table above:

```python
async def get_provider_config(config_id: str, *, platform_scoped: bool = False) -> dict | None: ...
async def get_custom_api(custom_api_id: str, *, platform_scoped: bool = False) -> dict | None: ...
async def get_knowledge_base(kb_id: str, *, platform_scoped: bool = False) -> dict | None: ...
async def list_calls(tenant_slug: str | None, *, ...) -> list[dict]:   # None ⇒ platform_conn
async def list_audit_log(*, tenant_id: str | None, platform_scoped: bool = False, ...) -> dict:
    # platform_scoped ⇒ platform_conn + no tenant predicate; otherwise tenant_conn
    # plus an explicit `AND tenant_id = $n` — app layer and RLS both, never RLS alone.
```

`tenant_conn` acquires, opens `conn.transaction()`, then issues exactly one resolving statement:

```sql
SELECT set_config('app.tenant_id',   t.id::text, true),
       set_config('app.tenant_slug', t.slug,     true)
  FROM tenants t
 WHERE ($1::uuid IS NOT NULL AND t.id = $1::uuid)
    OR ($1::uuid IS NULL AND $2::text IS NOT NULL AND t.slug = $2)
```

Zero rows (tenant unknown, both arguments NULL, or identity resolution failed upstream) → raise
`TenantUnresolved` **before** yielding the connection. This is AC 9: the caller gets an explicit
error, never an unset GUC that silently reads as "no data". `set_config(..., true)` is `SET LOCAL`
by another name and is reverted when the transaction ends, which satisfies AC 8 on a reused pooled
connection without a `RESET` in a `finally`.

`platform_conn` acquires, opens a transaction, issues `SET LOCAL ROLE yuviz_platform`, logs at
INFO with `reason=` and increments a counter. `reason` is mandatory so every bypass is
self-documenting and greppable, and so staging logs enumerate the real bypass set before cutover.

### Tier 2 — the complete list of routers getting `bind_path_tenant` (12)

| Router declaration | Prefix | Cross-tenant branch it makes work |
|---|---|---|
| `services/config/routers/agents.py:16` | `/tenants/{tenant_slug}/agents` | `_resolve_tenant`'s `is_unscoped` (`:28`) — 12 routes |
| `services/config/routers/calls.py:10` | `/tenants/{tenant_slug}/calls` | path-scoped listing for any actor |
| `services/config/routers/carriers.py:11` | `/tenants/{tenant_id}/carriers` | `_resolve_tenant_id` (`:15`) |
| `services/config/routers/phone_numbers.py:13` | `/tenants/{tenant_id}/phone-numbers` | `_resolve_tenant_id` (`:17`) |
| `services/config/routers/provider_configs.py:14` | `/tenants/{tenant_id}/providers` | `_require_tenant_access` (`:68`) |
| `services/config/routers/telephony_configs.py:11` | `/tenants/{tenant_id}/telephony-configs` | `_resolve_tenant_id` (`:16`) |
| `services/config/routers/tool_provider_configs.py:11` | `/tenants/{tenant_id}/tool-providers` | `_resolve_tenant_id` (`:15`) |
| `services/campaigns/routers/campaigns.py:17` | `/tenants/{tenant_id}/campaigns` | tenant-scoped listing/create |
| `services/campaigns/routers/campaigns.py:103` | `/tenants/{tenant_id}/dnc` | same |
| `services/knowledge/routers/knowledge_bases.py:13` | `/tenants/{tenant_id}/knowledge-bases` | `list/create` for a platform actor |
| `services/toolexec/routers/custom_apis.py:32` | `/tenants/{tenant_id}/custom-apis` | `_require_tenant_access` (`:38`) |
| `services/did/routers/numbers.py:34` | `/tenants/{tenant_id}/numbers` | **nothing today** — `search`/`purchase`/`list` read `tenant_id` off the path with no check at all |

All 12 get **both** dependencies. The "cross-tenant branch it makes work" column above describes
`bind_path_tenant`; `require_path_tenant_access` is new authorization on the 9 declarations whose existing
helper is an existence check or absent entirely — `calls`, `carriers`, `phone_numbers`,
`telephony_configs`, `tool_provider_configs`, `campaigns`/`dnc`, `knowledge_bases` and
`did`/`numbers` — and is a duplicate of the handler's own check on `agents`, `provider_configs` and
`custom_apis`. The route-walk tripwire is what makes this list self-maintaining: the `did` router was
missed by hand-enumeration twice, which is the argument for deriving the list from `app.routes`
rather than from this table.

Plus one non-router site: `services/config/routers/live_calls.py::_resolve_scope` calls
`set_target_tenant(tenant["id"])` at its successful return (`~:78`), after the 403 branches — its
tenant is a `?tenant_slug=` query param, not a path param. It needs no
`require_path_tenant_access`: `_resolve_scope`'s own 403/404 branches already are that check, and
they use the fresh DB row.

`tests/test_rls_coverage.py` asserts this list mechanically: walk every mounted route whose path
contains `/tenants/{`, assert both `bind_path_tenant` and `require_path_tenant_access` are in its
dependency chain. A new tenant-scoped
router added without it fails the test (lesson 12 — the tripwire trips on the exact new-code shape).
The walk covers **all six** apps including `did`, which is how `numbers.py` would have been caught.

### Tier 3 — the complete list of flat by-id routers (17 routers, 53 route declarations)

Every row gets the same treatment: resolver takes `platform_scoped: bool` (source:
`deps.is_platform_scoped(current_user)` only), selects `platform_conn(reason=…)` / `tenant_conn()`,
then calls `deps.assert_tenant_access(row["tenant_id"], current_user)` on the fetched row. "Check
today" = does a post-fetch caller-tenant comparison already exist.

| Flat router (prefix) | By-id routes | Resolver gaining `platform_scoped` | Check today |
|---|---|---|---|
| `config/routers/provider_configs.py` `/providers` | 4 (`:112,117,137,147`) | `provider_configs.get_provider_config` (**cached**) | yes — `_authorize_provider` (`:77`) |
| `config/routers/telephony_configs.py` `/telephony-configs` | 4 (`:45,53,67,76`) | `telephony_configs.get_telephony_config` (**cached**) | **no** |
| `config/routers/carriers.py` `/carriers` | 3 (`:43,51,65`) | `carriers.get_carrier` | **no** |
| `config/routers/phone_numbers.py` `/phone-numbers` | 3 (`:60,68,88`) | `phone_numbers.get_phone_number` | **no** |
| `config/routers/tool_provider_configs.py` `/tool-providers` | 3 (`:53,63,82`) | `tool_provider_configs.get_tool_provider_config` | **no** |
| `config/routers/calls.py` `/calls` | 2 (`:87,96`) | `calls.get_call` / `get_transcript` via `_caller_tenant_slug` (`:14`) | yes |
| `config/routers/agent_tool_policies.py` `/agents/{agent_id}/tool-policies` | 2 (`:51,69`) | `agent_tool_policies.*` keyed on `agent_id` | **no** (`validate_id_exists` only, `:14`) |
| `config/routers/users.py` `/users/{user_id}` | 2 (`:38,53`) | `users.get_user_for_admin` (**new** — see "The `users` module" below) | **no** — `require_role("superadmin")` only, and no fetch of any kind before the write |
| `config/routers/invites.py` `/invites/{invite_id}` | 2 (`:94,116`) | `invites.resend_invite` (`:256`) / `revoke_invite` (`:305`) | yes — `may_invite`/`_same_tenant` |
| `config/routers/live_calls.py` `/live-calls/{session_id}/interventions` | 1 (`:114`) | `_resolve_scope` (`:47`) | yes — and it re-reads via `fresh_authority` |
| `campaigns/routers/campaigns.py` `/campaigns` + `/dnc` | 9 (`:49,54,64,70,79,127,135,143`, `:123`) | `campaigns.get_campaign`, `dnc.get_dnc_number` | **no** |
| `knowledge/routers/knowledge_bases.py` `/knowledge-bases` | 4 (`:39,47,63,77`) | `knowledge_bases.get_knowledge_base` | yes — `_authorize_kb` (`:58`) |
| `knowledge/routers/documents.py` `/documents` + `/knowledge-bases/{kb_id}/documents` | 5 (`:19,24,53,61,75`) | `documents.get_document`, `knowledge_bases.get_knowledge_base` | **no** |
| `knowledge/routers/agent_kb.py` `/agents/{agent_id}/knowledge-bases` | 2 (`:28,41`) | `agent_kb.*` keyed on `agent_id` | **no** |
| `toolexec/routers/custom_apis.py` `/custom-apis` | 3 (`:93,98,114`) | `custom_apis.get_custom_api` | yes — `_authorize_custom_api` (`:47`) |
| `toolexec/routers/agent_apis.py` `/agents/{agent_id}/custom-apis` | 2 (`:34,47`) | `agent_apis` `tenant_filter` (`:66,182`) | yes |
| `did/routers/numbers.py` `/numbers/{id}/assign|release` | 2 (`:103,121`) | `purchased_numbers.get_purchased_number` | **no** |

**Not Tier 3, stated so it is not re-litigated:** `config/routers/tenants.py`'s 4 by-id routes
(`:38,50,62,102`). `tenants` is out of RLS scope entirely (it is the table `tenant_conn()`'s own
resolver reads), so the whole module is on the bypass list; its authorization is unchanged.
`config/routers/agents.py`'s 10 by-id routes are *not* flat — they live under
`/tenants/{tenant_slug}/agents` and are covered by Tier 2.

Note the predicate correction these sites inherit: `provider_configs.py:106` and `calls.py:20`
currently write `role == "superadmin" or tenant_id is None`. The **bypass** decision uses only the
`tenant_id is None` half (`deps.is_platform_scoped`, lesson 24) — a superadmin *with* a tenant is
scoped to that tenant for data access. Where a post-fetch check already exists its 403/404 shape is
left exactly as written; the eleven routers that gain one adopt `assert_tenant_access`'s shape, which
is `custom_apis.py:38`'s existing shape verbatim.

#### The `users` module — the one Tier 3 router with no fetch, and the two functions not to confuse

`PATCH /users/{user_id}` (`services/config/routers/users.py:38`) and `DELETE /users/{user_id}` (`:53`)
are the only Tier 3 routes that never read the row before writing it: both pass the path id straight
into `users_service.update_user` / `soft_delete_user` under `require_role("superadmin")` alone, and
there is no `users.get_user` for the previous draft's resolver column to have named. That matters
more here than on any other flat router, because `_UPDATABLE_FIELDS = {"role", "tenant_id"}`
(`services/config/users.py:24`) makes the *written value* a tenancy decision: `PATCH {"tenant_id":
null}` moves an account into the platform scope that `deps.is_platform_scoped` — and therefore every
bypass, every Tier 2 exemption and every Tier 3 platform branch in this design — keys on. Three
changes, giving these two handlers the same fetch-then-check shape as the other sixteen routers:

1. **A real by-id resolver.** `users.get_user_for_admin(user_id, *, platform_scoped: bool = False)
   -> dict | None`, running the same `SELECT * FROM users WHERE id = $1 AND deleted_at IS NULL` that
   `get_user_by_id` (`:47`) runs today and differing only in how it opens its connection:
   `platform_conn(reason="users-admin-by-id")` when `platform_scoped`, `tenant_conn()` otherwise
   (source of the flag: `deps.is_platform_scoped(current_user)` only, lesson 24). Both handlers call
   it first and `get_or_404` the result — so a tenant-scoped actor asking for another tenant's user
   id gets today's 404 from an empty policy result, with no new status code (lesson 2).
2. **Check the row that was fetched.** `deps.assert_tenant_access(row["tenant_id"], current_user)`
   immediately after the fetch, identical to the other sixteen routers. A superadmin who still
   carries a tenant (the leftover-default accounts named under Tier 2) therefore gets a 403/404 on
   another tenant's user instead of today's unconditional 200.
3. **Check the value being written — this is what closes the self-promotion path.** When
   `"tenant_id"` is present in `fields`, the handler calls
   `deps.assert_tenant_access(fields["tenant_id"], current_user)` a **second** time, before the
   service call: the destination tenant is authorized exactly like the source. `assert_tenant_access`
   already admits `None` only for a platform-scoped caller, so `{"tenant_id": null}` is a 403 for
   anyone who is not already platform-scoped — and a privilege no-op for anyone who is — and the same
   call rejects a move into a third tenant. `"role"` needs no extra rule: after (2) a tenant-scoped
   superadmin can only reach rows inside its own tenant, and granting `role = 'superadmin'` to a row
   that keeps a non-NULL `tenant_id` grants no scope, because the scoping predicate is
   `tenant_id IS NULL` and never the role (lesson 24). `PATCH` and `DELETE` both take (1) and (2);
   only `PATCH` takes (3).

RLS is the independent second layer here rather than the control: a tenant-scoped actor's write runs
under `tenant_conn()`, and `users`' `WITH CHECK (tenant_id = <GUC>)` rejects `SET tenant_id = NULL`
(`NULL = <GUC>` is NULL, not true) and any other-tenant value with an `InsufficientPrivilegeError`.
The app-layer checks above are what turn that into a 403 rather than a 500, and they are the **only**
control on the platform branch, where the policy is bypassed. The platform branch's mutation opens
`platform_conn(reason=…, stamp_tenant=row["tenant_id"])`, so the tenant whose user was edited still
sees the row in its own audit log (see "`audit_log` gains a tenant column").

**`get_user_by_id` vs `get_user_for_admin` — the ambiguity that put one name on two lists.**
`get_user_by_id` (`:47`) is unchanged and is **identity resolution only**: it answers "who is the
actor on this request", and every one of its four callers re-reads the *caller's own* row —
`deps.assert_current_authority` (`deps.py:144`), `deps.fresh_authority` (`deps.py:192`),
`routers/auth.py:52` (`GET /auth/me`) and `routers/tenants.py:83`. All four run **before** the
request's tenant is known, and all four must be able to see a NULL-tenant platform account, which no
policy can ever return. So the bypass lives *inside* `get_user_by_id`, once, as
`platform_conn(reason="identity-resolution")`, and the four callers inherit it with no flag and no
call-site change. `get_user_for_admin` is the opposite question — "may this actor touch *that* user"
— has exactly two callers, both in `routers/users.py`, and takes the normal tenant-scoped path unless
`deps.is_platform_scoped(current_user)` says otherwise. Two functions, two contracts, no shared
boolean; that separation is why neither can sit on both lists again.

### Caches and RLS — the read-through Redis path

**A value that has left Postgres is outside every policy.** `services/config/cache.py` is a
read-through Redis cache and four call sites use it; two of them return a row *before Postgres is
ever touched*, so RLS is not merely a second layer there, it is **not a layer at all**:
`telephony_configs.py:56-58` and `provider_configs.py:71-74` both `return cached` on a hit. This
design does not try to make RLS cover them. It states the boundary and puts the control where it can
actually run:

1. **The route, not the policy, is the control for any cached read.** Every cached getter is on the
   Tier 3 list above, so its flat router performs `deps.assert_tenant_access(row["tenant_id"], …)`
   **after** the getter returns, on the cached and uncached paths identically — the check is on the
   returned row, not on the query, so a cache hit and a cache miss are indistinguishable to it. This
   is what closes the named gap: the flat `GET /telephony-configs/{config_id}` (`:45`) and its
   `PATCH` (`:53`), `POST /set-default-outbound` (`:67`) and `DELETE` (`:76`) siblings have **no
   tenant check of any kind today** and are reachable by any `superadmin`/`admin` of any tenant with
   a known config UUID; `provider_configs`' equivalent routes already do this via
   `_authorize_provider` and are the shape being copied.
2. **The miss path still sets the GUC.** The cached getters gain `platform_scoped: bool` like every
   other Tier 3 resolver, so the fill query runs under `platform_conn`/`tenant_conn` rather than a
   bare pool call (they are `pool.fetchrow` sites today — items in the conversion inventory below).
   A tenant-scoped caller's miss therefore reads nothing for a foreign id, and the 404 happens
   before anything is cached.
3. **Cache-key rule, stated in the README and enforced by a tripwire:** a cache key must be either
   (a) globally unique across tenants by construction, or (b) tenant-prefixed. All four existing keys
   already comply and are the precedents — `provider:{uuid}` and `telephony:{uuid}` are (a) on a
   primary key, `did:{e164}` (`phone_numbers.py:59`) is (a) on a globally unique number,
   `agent:{tenant_slug}:{agent_slug}` (`agents.py:43`) is (b) because an agent slug is unique only
   *within* a tenant, and `toolexec/auth_schemes.py:127`'s in-process OAuth2 token cache keyed
   `(tenant_id, custom_api_id)` is (b). A new cache keyed on a per-tenant-unique value (`slug`,
   `name`, `email`) without the tenant in the key is a cross-tenant serve, and no policy can catch
   it. `tenants.py:30`'s `tenant:{slug}` is exempt: `tenants` is out of RLS scope and slugs are
   global.
4. **Invalidation is unchanged.** All four caches invalidate by the same key on write, and writes now
   run under `tenant_conn()`, so a tenant can only invalidate keys for rows it could write.

Nothing here caches a *list*; every cached value is a single row keyed by its own identifier, which
is why point 1 is sufficient and no per-tenant cache namespace is needed.

### Bypass sites (`platform_conn`) — the complete intended list

| Site | Reason |
|---|---|
| `services/config/auth.py` / `routers/auth.py` — login by email, `/auth/bootstrap` | Pre-auth: there is no tenant yet, and the row may be a NULL-tenant platform admin |
| `services/config/invites.py` — `accept_invite` (`:374`), `get_invite_for_accept` (`:473`), `_check_context_live` (`:340`) | Pre-auth by design (lesson 1: these routes have no `Authorization` header) |
| `services/config/invites.py` — `list_invites` (`:455`) platform branch, and `create_invite` (`:158`) **only when the invite's target tenant is NULL** | Narrowed from module granularity — see below |
| `services/config/users.py:47 get_user_by_id` — one `platform_conn(reason="identity-resolution")` call site inside the function, shared by its **four** callers: `deps.py:144 assert_current_authority`, `deps.py:192 fresh_authority`, `routers/auth.py:52` (`GET /auth/me`), `routers/tenants.py:83` (the concurrency route's fresh-actor read) | Identity resolution runs before the request's tenant is known and must reach NULL-tenant platform accounts, which no policy can return. (`deps.py:133` in the previous draft was `_row_to_effective_user`, a pure function that touches no database — these four are the real sites.) |
| `services/config/users.py:55 list_users` — **whenever its effective `tenant_id` argument is `None`**: the `is_platform_scoped=True` cross-tenant branch *and* the scoped branch of a NULL-tenant non-superadmin service account | Both read only rows a policy can never return — the first is cross-tenant by design (gated on `tenant_id is None`, lesson 24), the second filters `tenant_id IS NOT DISTINCT FROM NULL`, i.e. exclusively platform-scoped rows. See "`GET /users` for a NULL-tenant service account" below |
| `services/config/routers/invites.py:86` — platform-scoped invite listing | Same |
| `services/config/tenants.py` — all of it | `tenants` is out of RLS scope, but NULL-tenant reads still need a connection that resolves |
| `services/config/phone_numbers.py` — `prewarm()` | Startup, cross-tenant, no request context |
| Tier 3's 17 flat routers, on their platform branch only (`platform_scoped=True`) | Enumerated in the Tier 3 table; the only legal source of that flag is `deps.is_platform_scoped(current_user)` |
| Tier 4's `POST /internal/*` sites, never — they are **not** bypasses | Listed here only to say so: their tenant is known (body/path), so they set the GUC like any other request |
| `services/knowledge/ingestion_worker.py` — job claim/poll | Cross-tenant queue, no request context |
| `services/campaigns/worker.py` — originate worker's due-campaign scan | Cross-tenant, no request context |
| `services/conversation/transcript_builder.py` — inactive-call reconcile sweep | Cross-tenant maintenance UPDATE by definition |
| `services/config/routers/audit_log.py` — `list_audit_log(platform_scoped=True)` | Platform-scoped superadmin reading every tenant's history; the tenant-scoped branch uses `tenant_conn()` and an explicit `tenant_id` predicate |
| `scripts/*.py` | Not via `platform_conn` — these connect on `POSTGRES_ADMIN_DSN` as the superuser |

**`create_invite` is deliberately not bypass-listed at module granularity.** It is the highest-privilege
write in `invites.py` — it mints a bearer-equivalent grant (lesson 16) — and sweeping it into
`platform_conn` alongside the low-risk pre-auth reads would exempt the one function that most needs
the policy. Its target tenant arrives in the **request body** (`body.tenant_id`), which makes
`POST /invites` a Tier 4 site: the route calls `deps.assert_tenant_access(body.tenant_id, current_user)`
(the existing `may_invite`/`_same_tenant` logic at `:112,132` is kept and this runs beside it, not
instead of it) and then `set_target_tenant(body.tenant_id)`, after which `create_invite` uses a plain
`tenant_conn()` — so the `user_invites` row is written under the target tenant's GUC and `WITH CHECK`
rejects any mismatch. The **only** bypassing branch is `body.tenant_id is None`, i.e. a platform
superadmin inviting another platform admin, whose row is `tenant_id IS NULL` and therefore invisible
to every policy by construction. `resend_invite` and `revoke_invite` are Tier 3 (by-id, table above),
not bypasses.

Note what is **not** on this list any more: `audit.py`. Audit writes ride the mutation's own
connection (`write_audit(conn, …)`), so they inherit whatever scope that mutation resolved and their
`tenant_id` is stamped by the column default — a mutation under `tenant_conn()` writes a tenant-owned
audit row, a mutation under `platform_conn()` writes a NULL-tenant one. Any `audit.py` site that
acquires its own connection rather than receiving one must use the *same* helper as the mutation it
records, never `platform_conn()` unconditionally.

**`GET /users` for a NULL-tenant, non-superadmin service account.** `routers/users.py:13-35`
deliberately computes `platform_scoped = is_platform_scoped(...) and role == "superadmin"`, so
Conversation/vobiz (`role="viewer"`, `tenant_id IS NULL`) land on `list_users(tenant_id=None,
is_platform_scoped=False)` — the `tenant_id IS NOT DISTINCT FROM NULL AND role != 'superadmin'`
branch. A naive conversion opens `tenant_conn()` there with neither caller nor target and raises
`TenantUnresolved` → 500: AC 9 firing correctly on a request that is not actually an error. The
specified behaviour is **today's response, unchanged**: `list_users` selects
`platform_conn(reason="users-null-tenant-listing")` whenever its `tenant_id` argument is `None`,
regardless of `is_platform_scoped`, because a NULL-tenant listing is by construction a set of rows no
policy returns. No SQL changes, so the `role != 'superadmin'` and `NOT is_service_account` exclusions
still apply and nothing widens — the caller sees exactly the rows it sees today, and a *tenant*-scoped
caller still takes `tenant_conn()`. The router's authorization logic at `:13-35` is untouched.

Every other site is `tenant_conn()`. The rule the README states: **if you reach for `platform_conn`,
the predicate that justifies it must be `tenant_id IS NULL` (lesson 24 / `deps.is_platform_scoped`),
never `role == "superadmin"`, and never "this route has no tenant in its path" — that case is Tier 2
or Tier 3, not a bypass.** Note that `routers/users.py` and `routers/invites.py` deliberately
combine `is_platform_scoped(...) and role == "superadmin"` for *authorization*; the bypass decision
uses only the scoping half, matching what those handlers' queries actually need.

## What must not change

Every existing `WHERE tenant_id = $1` clause stays exactly as written. RLS is a second, independent
layer; removing an app-layer filter because "the database now enforces it" would make the app
correct only as long as the GUC wiring is correct, which is the single-point-of-failure this design
exists to eliminate. No app-layer tenant check, `is_platform_scoped` call, 403/404 shape, or query
predicate is removed or relaxed by this change, and nothing is widened. `bind_path_tenant` in
particular raises nothing: a nonexistent or foreign tenant slug in the path still produces exactly
the status code the handler's own `_resolve_tenant` produces today. Identity resolution in `deps.py`
is unchanged apart from one added `set_caller_tenant(...)` call; no new place decides *who* a request
is, only a recorded fact about *what* it targets.

Three exceptions, stated here so they are not discovered as surprises. (i) `require_path_tenant_access`
**adds** a 403/404 on the 9 tenant-path router declarations that have no caller-tenant check today
(`did`/`numbers` among them, where the missing check permits a billable cross-tenant DID purchase),
and on the 3 that do it narrows the exemption from `role == "superadmin"`/`is_service_account` to
`tenant_id IS NULL`. (ii) `assert_tenant_access` **adds** a post-fetch 403/404 on the 11 flat by-id
routers that have none — existing cross-tenant IDOR holes, including the cached
`GET /telephony-configs/{id}`. (iii) Tier 4 **adds** a tenant check to `POST /internal/retrieve`,
which is gated on bare `get_current_user` today. Both directions are tightenings, both are required for the app layer and the
policies to agree on the same predicate, and both are reproductions of an existing shape in this
codebase rather than a new one (`custom_apis.py:38` for the 403, `agents.py:24` for the 404). No
check anywhere is loosened.

## Migration and rollout

The change ships in five independently-revertible steps. At no point is there a big-bang cutover.

1. **Wiring only.** `libs/tenancy` lands, **all 183 call sites are converted (67 `acquire()` + 116
   pool-level)**, `deps.py` sets the caller scope and gains `assert_tenant_access`, the **12**
   Tier 2 routers get both dependencies, the **17** Tier 3 flat routers get `platform_scoped` +
   the post-fetch check, and the **3** Tier 4 sites get their explicit assert + `set_target_tenant`.
   Services still connect on the *superuser* DSN, and `database/rls.sql` has not been applied.
   `SET LOCAL`/`set_config` against a superuser is a no-op, so behaviour is unchanged except for one
   thing that is deliberately observable: `tenant_conn()` raises `TenantUnresolved` for an
   unresolvable tenant, and every operation is now transactional. Both are covered by tests in this
   step, before any policy can turn a wiring bug into an outage.
2. **Shadow verification.** Run staging on step 1 for one deploy cycle. `platform_conn(reason=...)`
   logs give the real, observed bypass set; compare it against the intended list above. Any site
   logging a bypass that is not on that list is a finding — it means a tenant-scoped query is
   running unscoped today. Add a second staging-only log line in `tenant_conn` recording
   `scope.target is not None and scope.target != scope.caller` — the cross-tenant admin path — so
   step 3 can confirm AC 6 against real traffic, not only against tests. Add a third staging-only
   line in `tenant_conn` logging at WARNING when `current_tenant()` is `None` **and** no
   `explicit_tenant` was given — that is a call site the conversion missed, and it is the only signal
   that distinguishes "converted but unscoped" from "correctly scoped to a tenant with no rows". Log the
   `caller is not None and target not in (None, caller)` sub-case at **WARNING**: that is a request
   the app layer allows today and `current_tenant()` will pin to the caller's tenant, i.e. exactly
   the account class step 4 sweeps. Zero such warnings for a full deploy cycle is the gate on step 5. Simultaneously, CI runs all
   six services' existing suites against a database where the app role is `yuviz_app` and `rls.sql`
   *is* applied (AC 7). This is the real gate: the suites pass as `yuviz_app` before any production
   DSN moves. A suite that needs a workaround is a finding, not a pass.
3. **Apply `database/rls.sql` in production.** Policies and RLS go live while every service is still
   connecting as the superuser, so they are inert — zero production risk from the DDL itself. Verify
   AC 1 and AC 2 with the `pg_roles` / `pg_class` queries, and run `tests/test_rls_isolation.py`
   against production's `yuviz_app` before any service moves.
4. **Pre-cutover account sweep (one query, blocking).** The Tier 2 predicate is `tenant_id IS NULL`,
   so any account that relies on `role = 'superadmin'` while holding a leftover `tenant_id` loses
   cross-tenant access at cutover — an empty table in the UI, not an error. Enumerate them in *step 2's* staging cycle, before any policy is live:
   `SELECT id, email, role, tenant_id FROM users WHERE tenant_id IS NOT NULL AND (role = 'superadmin' OR is_service_account) AND deleted_at IS NULL;`
   Every row is either a genuine platform actor, whose `tenant_id` is set to NULL, or a
   correctly-scoped tenant account that keeps its tenant. A non-empty, unreviewed result blocks
   step 5. Pair it with the shadow log line below.
5. **Flip `POSTGRES_DSN` per service, in ascending blast radius:** did → campaigns → toolexec →
   knowledge → conversation → config. Config last because a failure there takes down authentication
   for everything. Each flip is independently observable and independently revertible.

**Rollback (AC 12):** revert that service's `POSTGRES_DSN` to the superuser DSN and restart. The
superuser bypasses RLS unconditionally, so the policies and the `yuviz_app` role stay in place and
stay inert — rollback is never a schema change, never requires dropping a policy, and can be done
one service at a time under time pressure. `database/rls.sql` is idempotent and re-runnable.

## Risks

- **A tenant-scoped router is added later without `bind_path_tenant`, so every platform-actor request through it silently returns zero rows — or without `require_path_tenant_access`, so it has no caller-tenant check at all.** Both are live failures today, not hypotheses: 9 of the 12 existing router declarations shipped without the second one, and `did`/`numbers.py:34` shipped without any tenant check at all and was itself missed by the first hand-written enumeration of this very list. Mitigated by the route-walk tripwire in `tests/test_rls_coverage.py` (assert every mounted path containing `/tenants/{` has *both* in its dependency chain), which trips on the new-router shape rather than on a hardcoded list, and by the README checklist. Residual: a router that takes the tenant from a query param, as `live_calls.py` does, is invisible to the walk and must be added by hand — the README says so and names `live_calls` as the precedent.
- **`bind_path_tenant` sets the target tenant *before* any authorization check runs, so between the two the GUC names a tenant the caller may not be entitled to.** Not exploitable, and no longer relied on: `current_tenant()` discards the target entirely unless the caller is platform-scoped, so the GUC never names a tenant the caller is not entitled to in the first place; additionally no tenant-scoped query executes in that window (the only query is the `tenants` lookup, an out-of-scope table). `bind_path_tenant` still raises nothing, so it cannot introduce a status code that differs across the tenant boundary (lesson 2).
- **`require_path_tenant_access` narrows the cross-tenant exemption from `role == "superadmin"`/`is_service_account` to `tenant_id IS NULL`, so a superadmin with a leftover tenant_id loses cross-tenant access — and the symptom is a 403/404 on a page that worked yesterday.** Accepted deliberately: the alternative is the two layers disagreeing, which turns the same request into a silent empty 200, and `provider_configs.py:81` already documents that leftover tenant_id as an artifact of account creation rather than intent. Mitigated by the step-4 account sweep (which finds every affected row before cutover and is a blocking gate), by the step-2 WARNING log that counts real occurrences in staging traffic, and by an explicit test case asserting the 403 for that account shape so the behaviour is pinned rather than incidental.
- **`audit_log` rows written before this change, and rows whose entity was hard-deleted, backfill to NULL and become invisible to a tenant-scoped reader.** Fail-closed by choice — the alternative direction leaks. Mitigated by the backfill covering all 18 `entity_type` values in use (enumerated from the code, not guessed) including the three agent-child entities and the slug-keyed `live_call_intervention`, and by the README stating that an incomplete pre-cutover history is expected, not a policy bug.
- **`audit_log.tenant_id` defaults from the RLS GUC, so the audit row's tenant is set implicitly rather than passed by the writer.** Justified as the smallest correct change: `write_audit` already runs on the mutation's connection inside the mutation's transaction, so the default is read from the same resolved scope the mutation used and cannot disagree with it; passing it explicitly would touch ~40 call sites in six services for the same value. `WITH CHECK` rejects any row whose `tenant_id` disagrees with the GUC, so a writer that ever sets it explicitly and wrongly fails loudly instead of misfiling.
- **A future service, pool, or background worker is added without `tenant_conn()` — its own rows become invisible to itself, and the symptom looks like "no data", not "permission denied".** Mitigated by making `libs.tenancy` the only documented way to get a connection, by `TenantUnresolved` being a loud error rather than an empty set, and by the README checklist (AC 11) that a new service/pool must follow. Conversation's two ad-hoc `asyncpg.create_pool` calls are the existing precedent for exactly this mistake and are both converted here.
- **A new tenant-scoped table ships without a policy — it fails *open*, silently readable across tenants, and nothing breaks so nobody notices.** Mitigated by `tests/test_rls_coverage.py`, which enumerates `information_schema.columns` for a `tenant_id` column and asserts RLS + FORCE + ≥1 policy for each. This tripwire genuinely trips: adding a `tenant_id` column without a policy fails it (lesson 12).
- **An admin adds a table and forgets to grant DML to `yuviz_app` — that table fails *closed* and breaks outright.** Mitigated by `ALTER DEFAULT PRIVILEGES`, which grants automatically for tables created by the schema-applying role. It does *not* cover a table created by a different role; the README says so explicitly.
- **`SET LOCAL ROLE yuviz_platform` is an in-process escalation `yuviz_app` can perform at will, so a code path that calls `platform_conn()` wrongly is a full bypass.** Mitigated by the mandatory `reason=` argument (every call is greppable and logged), by the enumerated intended bypass list above, by the Tier 2/Tier 3 rule that keeps "no tenant in the path" from becoming a bypass excuse, and by step 2's shadow comparison of observed vs. intended bypasses. Accepted honestly: this is equal to, not worse than, the policy-branch alternative the PRD offered — both require the process to hold the capability.
- **`platform_conn(stamp_tenant=…)` is a second way to set the tenant GUCs, and a wrong value would mislabel an audit row.** It is the one added parameter in this design; it exists because a platform superadmin's edit to a tenant's row would otherwise be absent from that tenant's own audit log, and no other mechanism can stamp it. Mitigated by its blast radius being exactly one column — under `BYPASSRLS` the GUC gates no read and no write, so a wrong value cannot leak or corrupt a row, only misfile the audit entry — by its only legal source being the `row["tenant_id"]` of a fetch that already happened, and by tripwire (f), which fails any platform-branch mutation that omits it.
- **`PATCH /users/{user_id}` can rewrite the very predicate every tier keys on (`tenant_id IS NULL`), so a missed check there is a self-service escalation into platform scope, not a data leak.** Mitigated by authorizing the destination tenant with the same `assert_tenant_access` as the source before the write, by `users`' `WITH CHECK` rejecting the same `UPDATE` at the database for a tenant-scoped connection, and by the self-promotion test case with its platform-scoped counter-arm (lesson 12).
- **Tier 3 adds a `platform_scoped: bool` parameter to five service functions, and a caller that passes `True` wrongly gets a full-table read.** Mitigated by making the only legal source of that boolean `deps.is_platform_scoped(current_user)` — grep-checkable, and the same convention `services/config/users.py:55` already uses — and by an AC 6 test per site that asserts a *tenant-scoped* actor gets `False` and 404s on another tenant's id.
- **Wrapping 183 call sites in transactions changes failure atomicity and lock duration**, and the 116 pool-level ones gain a transaction they never had. Multi-statement operations become atomic, which is a correctness improvement. The real hazard is a long-running loop inside a transaction pinning a snapshot: `services/knowledge/ingestion_worker.py`'s poll loop and `services/campaigns/worker.py` must keep the loop *outside* the context manager, one transaction per job. Called out as a specific implementation constraint, with a test asserting the ingestion worker holds no transaction between jobs.
- **`transcript_entries` under the Wave B parent-join policy depends on the `calls` row existing.** Conversation's writes are fire-and-forget and ordered `begin_call` then `record_turn`, but a failed/racing `begin_call` would turn transcript writes from "insert an orphan" into "rejected by `WITH CHECK`". Mitigated by keeping the TranscriptBuilder tests that cover out-of-order writes and by the existing `ON CONFLICT (session_id) DO NOTHING` insert; if the test shows real loss, `transcript_entries` drops to a follow-up rather than blocking Wave A.
- **The degraded agent-config fallback (`agent_config.py:169`) supplies `tenant.id = ""`.** The slug is still populated there, which is why `tenant_conn()` accepts a slug and resolves the UUID itself. If the slug is also empty, `TenantUnresolved` fires and the call's persistence fails loudly instead of writing a row under the wrong tenant.
- **A read-through cache returns a row before Postgres is consulted, so RLS is not a control for cached values.** Structural, not fixable by any policy — mitigated by making the *route's* `assert_tenant_access` the control on all four cached getters (it runs identically on hit and miss because it inspects the returned row, not the query), by putting all four on the Tier 3 list so the miss path still sets the GUC, and by the cache-key rule + tripwire. Residual, stated plainly: if someone adds a cached getter whose route skips `assert_tenant_access`, RLS will not save it. The Tier 3 route-walk tripwire is written against flat routers generally, so it covers the four cached routes too.
- **Hand-enumerated lists in this document have been wrong twice** (the `did` router missing from the Tier 2 list of "11"; the 116 pool-level calls missing from the "67 call sites"). Mitigated by making every list in this design mechanically asserted rather than trusted: the Tier 2 list from an `app.routes` walk over all six apps, the Tier 3 list from a walk of flat routers' by-id routes, the conversion inventory from the AST tripwire in `tests/test_no_bare_pool_calls.py`, and the bypass/override lists from collected `reason=` literals. A list a test derives cannot silently omit a member (lesson 12).
- **Adding `assert_tenant_access` to 11 flat routers that have no post-fetch tenant check today turns some cross-tenant 200s into 403/404s.** These are existing IDOR holes (a known config UUID reaches another tenant's telephony credentials, DID inventory, campaign contacts or KB documents), so the change is a tightening and closing them is the point — but it is a visible behaviour change on routes an operator's script might rely on. Mitigated by the step-2 staging log counting real cross-tenant by-id hits before cutover, by every one of the 11 reproducing `custom_apis.py:38`'s existing 403/404 shapes rather than inventing new ones (lesson 2), and by the Tier 3 rows in the AC 6 matrix pinning both directions.
- **`create_invite` moves off the module-wide `platform_conn` bypass onto Tier 4.** A platform superadmin inviting into a tenant now writes under that tenant's GUC, so a resolver bug turns invite creation into a `WITH CHECK` failure rather than a silent misfile — loud, and preferable. Mitigated by keeping `may_invite`/`_same_tenant` exactly as written alongside the new assert, and by an explicit test for all four combinations (platform actor into a tenant, platform actor into NULL, tenant admin into own tenant, tenant admin into another tenant).
- **`did` is not one of the PRD's "five services" but connects on the same DSN and queries three Wave A tables.** Omitting it from the plan would break DID entirely at cutover. It is in the file table above; the planner must not drop it back out.
- **New config knob `POSTGRES_ADMIN_DSN`.** Justified because PRD constraint (line 54) requires `scripts/create_superadmin.py` and `seed_default_config.py` to keep bypassing RLS while services do not, and those scripts read `POSTGRES_DSN` today; it defaults to `POSTGRES_DSN` so no existing environment breaks.
- **The GUC's caller tenant comes from a JWT claim, so it is as stale as the token — up to ~12h (lesson 27).** `services/config/deps.py` resolves identity purely from the decoded token, so a de-provisioned or re-tenanted user keeps the old `tenant_id` claim until expiry, and RLS will faithfully scope them to the tenant the token names. **Stated as a known limitation rather than fixed here, and this design does not make it worse:** the GUC reads the *same* claim the app-layer `WHERE tenant_id = $1` filters already read, so the two layers are stale together and RLS never widens access beyond what the app already grants. One free narrowing is taken where a fresh row already exists: `live_calls._resolve_scope` calls `deps.fresh_authority` (`:58`, `:140`) and re-reads the user from the database, so it calls `set_caller_tenant(effective_user.tenant_id)` from that fresh row — not from the token — making the live-calls routes strictly tighter than the rest. A general fix is a DB read on every request in `get_authenticated_user`; that is lesson 27's open item and belongs to its own change, not this one. Named in the README so nobody reads RLS as revocation.
- **Slug reuse on `calls` / `live_call_interventions`.** The existing schema comment already notes that a soft-deleted tenant's reissued slug misattributes history. RLS inherits that weakness rather than fixing it — this design does not make it worse and does not claim to fix it. Flagged so it is not mistaken for a new guarantee.

## Test plan

**Integration — cross-tenant admin matrix (`tests/test_cross_tenant_admin.py`).** The direct answer
to AC 6 and to the review's blocking finding, run through the real FastAPI apps against a
`yuviz_app` connection with `rls.sql` applied. Fixture: tenants A and B, a superadmin with
`tenant_id = NULL`, and a tenant_admin scoped to A. Parameterised over **all 12 Tier 2 routers by
name** (including `did`/`numbers`), **all 17 Tier 3 flat routers by name** and **all 3 Tier 4
sites by name**:

1. Superadmin `GET /tenants/{B}/…` returns B's rows — the same rows the same request returns today
   on the superuser DSN, compared row-for-row against a superuser-connected baseline. This is the
   assertion that fails if `bind_path_tenant` is missing from a router, and it is the one that would
   have caught `agents.py`.
2. Superadmin `POST`/`PATCH`/`DELETE` under `/tenants/{B}/…` succeeds and the row lands with
   `tenant_id = B`, verified by a superuser-connected read — proving `WITH CHECK` passed against the
   *target*, not the caller.
3. Tenant_admin of A on `/tenants/{B}/…` gets 404 on the `{tenant_slug}` routers and 403 on the
   `{tenant_id}` routers — never an empty 200 — for **read and write**, parameterised over all 12
   router declarations including the 9 that have no such check today (`calls`, `carriers`, `phone_numbers`,
   `telephony_configs`, `tool_provider_configs`, `campaigns`/`dnc`, `knowledge_bases`,
   `did`/`numbers` — where the write case is a billable carrier purchase). The
   regression cases named by the security review are explicit rows: `viewer` of A on
   `GET /tenants/{B-slug}/calls` (and `/latency-stats`, `/dashboard-stats`), and `admin` of A on
   `POST /tenants/{B}/knowledge-bases`, `POST /tenants/{B}/campaigns`, `POST /tenants/{B}/dnc`.
3b. **RLS alone, app layer removed.** Repeat the writes from case 3 with
   `require_path_tenant_access` monkeypatched to a no-op, asserting zero rows on read and
   `asyncpg.InsufficientPrivilegeError` on write. This is the case that proves the database is an
   *independent* second layer rather than a mirror of the URL — under the old `target`-wins reader it
   would have succeeded — and it is the only test that distinguishes the two readers.
3c. **Superadmin holding a tenant_id.** A `role='superadmin'` account with `tenant_id = A` gets 403/404
   on `/tenants/{B}/…` and still succeeds on `/tenants/{A}/…`, pinning the deliberate narrowing.
4. Tier 3 by-id, parameterised over all 17 flat routers: superadmin with `tenant_id = NULL` on
   `GET /<flat>/{B's id}` returns the row (this is the assertion that fails with `TenantUnresolved`
   if a resolver did not get `platform_scoped`, and it is why the list must be all 17, not five);
   tenant_admin of A on the same id gets 404/403; tenant_admin of A on A's own id gets 200. The
   third case is the counter-check that stops case two passing because the route is simply broken
   (lesson 12).
4a. **Cached reads specifically.** `GET /telephony-configs/{B's id}` and `GET /providers/{B's id}`
   as tenant_admin of A, run **twice**: once cold, once after a platform superadmin has read the
   same id so the row is certainly in Redis. Both must 404. Without `assert_tenant_access` the
   second call returns B's credentials from cache with no query at all, so a test that only runs
   cold cannot fail — state that explicitly and prime the cache.
4c. **Tier 4.** `POST /internal/retrieve` with `body.tenant_slug = B` as a tenant-scoped account of
   A gets 403; as Conversation's NULL-tenant service account it returns B's context and the audit
   trail shows the GUC was B. `POST /internal/chains/execute` with `body.tenant_id = B` as a
   non-allow-listed service account still 403s (`require_execute_subject` unchanged), and as
   Conversation writes `api_chain_runs` with `tenant_id = B`. `GET /internal/agents/{B-slug}/…/has-knowledge`
   as a tenant-scoped account of A gets 404.
4d. **DID purchase specifically.** `POST /tenants/{B}/numbers/purchase` as admin of A gets 403 and
   **no carrier call is made** — asserted on the mocked provider, because the billable side effect
   happens before any DB write and RLS cannot reach it.
4b. `GET /audit-log`: a superadmin with `tenant_id = A` sees only entity ids belonging to A — asserted
   against a fixture that contains at least one B row of the same `entity_type`, so the assertion
   cannot pass by B having no rows (lesson 12) — while a NULL-tenant superadmin sees both. Plus one
   write-then-read: mutate an agent in tenant B as a platform superadmin, assert the new `audit_log`
   row carries `tenant_id = B` (the column default picked up the GUC), and assert the equivalent
   mutation under `platform_conn` (an invite redemption) carries NULL.
5. Negative control: temporarily remove `bind_path_tenant` from one router in-test and assert case 1
   now fails; and remove `require_path_tenant_access` and assert case 3b is what still stops the
   write. Without this the matrix could pass for the wrong reason (lesson 12 — state what makes
   the check fail, and prove it can).

**Integration — cross-tenant negative suite (`tests/test_rls_isolation.py`).** Connects to Postgres
directly as `yuviz_app`, with no application code in the path, so it proves the database layer holds
independently of the app layer. Fixture: seed tenants A and B as the superuser, plus one row per
Wave A table for each, and one Wave B child row per tenant. Then, per table, parameterised:

1. **Cross-tenant read is empty (AC 3).** In one transaction, `set_config('app.tenant_id', A, true)`
   + the slug, then `SELECT * FROM <table> WHERE <pk> = <B's row pk>` → zero rows. Asserts zero
   rows, *not* an error — a `permission denied` here would mean the grant is wrong, not that
   isolation works. Counter-check in the same test: the same query for A's own pk returns exactly
   one row, so the assertion cannot pass by the fixture simply being empty (lesson 12).
2. **No GUC at all is empty, not everything (AC 4).** Fresh transaction, nothing set,
   `SELECT count(*) FROM <table>` → 0, while the same count as the superuser is ≥ 2.
3. **Cross-tenant write is rejected (AC 5).** GUC = A, `INSERT ... (tenant_id) VALUES (B)` and
   `UPDATE <table> SET tenant_id = B WHERE tenant_id = A` → both raise
   `asyncpg.InsufficientPrivilegeError` (`new row violates row-level security policy`). Asserting
   the specific exception class, not "an exception" (lesson 12).
4. **`calls` slug path specifically (AC 10).** `app.tenant_slug` = A's slug reaches A's calls and
   not B's, with no change to the column type and using the unmodified existing query text from
   `transcript_builder.py`.
5. **Platform bypass restores today's answers (AC 6).** In one transaction,
   `SET LOCAL ROLE yuviz_platform`, run the actual prewarm query from
   `services/config/phone_numbers.py` and the unscoped `provider_configs` listing, assert the row
   set is identical to the superuser's. Then `COMMIT` and re-query on the same connection without
   the `SET ROLE` → back to zero rows, proving the bypass does not survive the transaction.

**Integration — coverage tripwires (`tests/test_rls_coverage.py`).** (a) Query `information_schema`
for every table in `public` having a column named `tenant_id`; assert `pg_class.relrowsecurity` and
`relforcerowsecurity` are true and `pg_policies` has ≥ 1 row (AC 2); separately assert the Wave B
list by name. (b) Walk each of the six FastAPI apps' `app.routes`; for every route whose path
contains `/tenants/{`, assert **both** `bind_path_tenant` and `require_path_tenant_access` appear in
its resolved dependency chain — across all six apps, `did` included. (c) For every route **not**
under `/tenants/{` whose path has a `{...}` id segment, assert the endpoint's module is on the Tier 3
table and its source calls `assert_tenant_access`; routes on the Tier 4 list are exempted by name and
asserted to call `set_target_tenant` instead. (d) Collect every `reason=` string literal passed to
`platform_conn` / `tenant_conn` across `services/` and assert the set equals the union of the bypass
and explicit-override tables — so a new bypass or a new explicit tenant cannot be added without
appearing in this document. **The unit of comparison is the `reason=` literal, not the call site or
the caller**: one table row may cover several callers that share one literal, which is exactly the
shape of identity resolution (four callers — `deps.py:144`, `deps.py:192`, `routers/auth.py:52`,
`routers/tenants.py:83` — reaching one `platform_conn(reason="identity-resolution")` inside
`users.get_user_by_id`). A per-call-site comparison would reject that correct fix, so it is specified
as a set of literals. (f) For every `platform_conn(...)` call inside a function whose body also
executes an `INSERT`/`UPDATE`/`DELETE`, assert `stamp_tenant=` is passed; sites that mutate with
genuinely no tenant are exempted by `reason=` literal (`identity-resolution`,
`users-null-tenant-listing`, the pre-auth invite reasons), so adding a platform-branch mutation
without a tenant stamp fails rather than silently writing a NULL-tenant audit row. (e) Assert each of the four `_cache_key` builders produces a key that is
either UUID-bearing or tenant-prefixed. Failure modes caught: a new table or column without a policy;
a new tenant-scoped router without Tier 2; a new flat by-id route without Tier 3; an ad-hoc bypass or
tenant override; a cache key that collides across tenants.

**Integration — the `users` module (`tests/test_cross_tenant_admin.py`).** The router whose updatable
fields are the tenancy itself gets its own cases, all through the real app:
- **Self-promotion is refused.** A superadmin with a non-NULL `tenant_id` sends
  `PATCH /users/{own_id}` with `{"tenant_id": null}` → 403, and the row's `tenant_id` is re-read and
  asserted unchanged. Same actor, same body, against *another user in its own tenant* → also 403.
  Counter-case so the test can fail for the right reason: a platform-scoped superadmin sending the
  identical body → 200 (lesson 12 — without this arm the assertion passes on any blanket rejection).
- **Cross-tenant edit and delete are refused.** Tenant A's superadmin `PATCH`es and `DELETE`s tenant
  B's user id → 404 (not 403, not 200), and B's row is asserted still present and unmodified.
- **Cross-tenant move is refused.** Tenant A's superadmin `PATCH {"tenant_id": <B>}` on its own
  user → 403 from the second `assert_tenant_access`, with the DB-layer counter-check that the same
  `UPDATE` issued directly under A's GUC raises `InsufficientPrivilegeError`.
- **Identity resolution still works for a platform account.** `GET /auth/me`, the live-calls
  intervention route and `PATCH /tenants/{id}` concurrency all succeed for a NULL-tenant actor after
  cutover — the four `get_user_by_id` callers, exercised individually, since a single shared bypass
  that regresses would otherwise 401/403 all four at once.
- **`GET /users` for a NULL-tenant viewer service account returns 200 with today's row set**, not a
  500 — asserted against the pre-cutover response captured from the same fixture, so an empty list
  cannot pass as success.

**Static — no bare pool calls (`tests/test_no_bare_pool_calls.py`).** AST-walk every module under
`services/` (excluding `*/tests/*`) and fail on any `.fetch/.fetchrow/.fetchval/.execute/.executemany`
attribute call whose receiver is a name bound from `get_pool()`/`create_pool()` or annotated
`asyncpg.Pool`. Baseline at zero after the conversion. This is the only check that catches call site
117; the two hand-written inventories that preceded it both missed dozens (lesson 12 — it trips on
the exact shape new code takes).

**Unit/integration — the helper (`tests/test_tenant_conn.py`).**
- **Precedence.** `set_caller_tenant(None)` then `set_target_tenant(B)` → `current_tenant() == B`,
  and the *reverse* call order → still `B` (order-independence, the claim that would break if
  precedence were implemented by overwriting a single variable). Then `set_caller_tenant(A)` with
  `set_target_tenant(B)` → `current_tenant() == A` in **both** call orders: a tenant-scoped caller is
  pinned to their own tenant and a client-supplied target cannot move them. These two cases together
  are the unit-level statement of the critical finding's fix.
- **Slug/UUID ambiguity.** `set_target_tenant(<slug>)` and `set_target_tenant(<uuid str>)` both
  resolve to the same row through `tenant_conn`.
- **Pool-reuse leak (AC 8).** One pool at `min_size=1, max_size=1` so the same physical connection
  is guaranteed reused. Request 1 under tenant A, request 2 under tenant B; request 2 asserts
  `current_setting('app.tenant_id', true)` equals B before its query and never A. A third
  acquisition using the raw pool (no helper) asserts the setting is `''` — proving the revert
  happens at transaction end, not by the next call overwriting it.
- **Fail-closed on unresolved identity (AC 9).** `tenant_conn(pool)` with an empty scope and
  `tenant_conn(pool, explicit_tenant=<uuid of a nonexistent tenant>, reason=...)` both raise `TenantUnresolved` and the
  connection is returned to the pool with no GUC set. Explicitly asserts the error, not an empty
  result — the whole point is that "no tenant" must not read as "no data".
- **Explicit override cannot be used ad hoc.** `tenant_conn(pool, explicit_tenant=B)` with no
  `reason=` raises `ValueError`; with `reason=` but inside a request whose `caller` is A raises
  `TenantScopeConflict` **before** yielding, and `pg_stat_activity` shows no transaction left open.
  This is the unit-level statement that the override is not a second `target`-wins path.
- **Ingestion worker holds no transaction between jobs**, asserted via `pg_stat_activity.state`.

**Regression (AC 7).** All six services' existing suites run unmodified against `yuviz_app` with
`rls.sql` applied, in CI, as step 2's gate. Any test needing a workaround is filed as a finding.

**Manual (lesson 23).** Drive the real Admin UI after cutover **as a superadmin with no tenant, then
as a tenant admin**: switch to another tenant from the tenant selector and list its agents, open a
knowledge base, run a campaign, place a test call through webcall and confirm the transcript
persists. The superadmin-on-another-tenant path is listed first deliberately — it is the one the
GUC-from-JWT design would have broken, and every failure mode of this change looks like an empty
table in the UI, which is exactly what a green suite will not show.
