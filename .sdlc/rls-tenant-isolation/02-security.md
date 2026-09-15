# Security review: .sdlc/rls-tenant-isolation/02-design.md (round 4, design mode — targeted re-check)
VERDICT: AMBER

Scope: re-verified the five round-3 `users`-module findings against the current draft and against the
real code they cite. **All five are genuinely closed** (details under "Verified controls"). One new
gap is reported below; it is not enumeration — it is a control the Tier 3 rule specifies for the
fetch and leaves unspecified for the write that follows it.

## Findings

1. [medium] Tier 3's rule scopes the **resolver** only; the **mutation** that follows it on the same
   handler has no specified way to reach the platform branch — design "Tier 3 — the complete list of
   flat by-id routers" (the uniform rule, §lines 167-183 and the signature block at 694-706), vs.
   risk register line 1046 ("adds a `platform_scoped: bool` parameter to **five service functions**")
   - The rule says: resolver takes `platform_scoped`, `True` ⇒ `platform_conn`, `False` ⇒
     `tenant_conn`, then `assert_tenant_access(row["tenant_id"], user)`. But on every flat router the
     write is a *second*, independent service call that acquires its own connection —
     `telephony_configs_service.update_telephony_config` / `set_default_outbound` /
     `soft_delete_telephony_config` (`services/config/routers/telephony_configs.py:62,72,80`, each
     doing its own `await db.get_pool()` inside), and the same shape on `carriers`, `phone_numbers`,
     `tool_provider_configs`, `agent_tool_policies`, `campaigns`, `dnc`, `documents`, `agent_kb`,
     `did/numbers`. Nothing in the design gives those functions a scope signal.
   - The `users` module is the *only* place the platform-branch write is specified, and only in prose
     (line 845-847: "The platform branch's mutation opens `platform_conn(reason=…,
     stamp_tenant=row["tenant_id"])`") — with no corresponding parameter on `update_user` /
     `soft_delete_user` in the conversion inventory (line 280 mentions only `list_users`). Tripwire
     (f) already presumes platform-branch mutations exist; nothing says how they are reached.
   - Attack/impact, two branches, both real:
     (a) The implementer follows the blanket conversion rule and makes the mutation `tenant_conn()`.
     A platform superadmin (`tenant_id IS NULL` — the only account type that can administer across
     tenants) has `caller is None` and no target on a flat route, so `tenant_conn` raises
     `TenantUnresolved` → **500 on every by-id PATCH/POST/DELETE across all 17 flat routers**,
     including `PATCH/DELETE /users/{user_id}`. Fail-closed, so no leak, but it removes the
     platform operator's entire write surface at step 5 of the rollout.
     (b) The implementer avoids (a) the obvious way and makes those mutation functions
     unconditionally `platform_conn`. Then every flat-router write — including `update_user`, where
     `_UPDATABLE_FIELDS = {"role", "tenant_id"}` — runs with RLS bypassed for *tenant-scoped*
     superadmins too, and the post-fetch `assert_tenant_access` is again the only layer. That
     directly contradicts the design's own load-bearing sentence at line 841-843, which names
     `users`' `WITH CHECK (tenant_id = <GUC>)` as the independent second layer that rejects
     `SET tenant_id = NULL`. This is the same shape as round 3's finding 3, arriving through the
     write path instead of the read path.
   - Fix: state the mutation-side rule once, uniformly, in the Tier 3 section — after the fetch and
     `assert_tenant_access`, the handler calls `deps.set_target_tenant(row["tenant_id"])` and the
     mutation stays on a plain `tenant_conn()` (correct for both actor kinds: a platform actor has
     `caller is None` so the target wins and RLS *still applies* to the write; a tenant-scoped actor
     is pinned to its own tenant by `current_tenant()`), reserving `platform_conn(stamp_tenant=…)`
     for the case where `row["tenant_id"] is None`. Then update line 1046's "five service functions"
     and the `users` paragraph to match, and extend tripwire (c) to assert every Tier 3 mutating
     route calls `set_target_tenant` as well as `assert_tenant_access`.

## Verified controls

- **Round-3 finding 1 (self-promotion via `PATCH`/`DELETE /users/{user_id}`) — closed.** The design
  now specifies a real resolver (`users.get_user_for_admin(user_id, *, platform_scoped)`), the
  post-fetch `assert_tenant_access(row["tenant_id"], …)`, **and** the second
  `assert_tenant_access(fields["tenant_id"], …)` on the destination tenant before the write. Checked
  against `assert_tenant_access`'s stated contract (line 674-691): `tenant is None` is admitted only
  for a platform-scoped caller, so `{"tenant_id": null}` is a 403 for a tenant-scoped superadmin and
  a no-op privilege-wise for one that is already platform-scoped. The `role` carve-out is
  correctly argued: the scoping predicate is `tenant_id IS NULL`, never the role (lesson 24), so
  granting `superadmin` to a row that keeps a non-NULL tenant grants no scope — and `admin` cannot
  reach the route at all (`require_role("superadmin")`, `routers/users.py:41,56`).
- **Round-3 finding 2 (identity-resolution bypass list) — closed.** The bypass row is now
  `users.py:47 get_user_by_id` with one `platform_conn(reason="identity-resolution")` inside the
  function, explicitly naming all four callers (`deps.py:144`, `deps.py:192`, `routers/auth.py:52`,
  `routers/tenants.py:83`) and retracting the wrong `deps.py:133` anchor. Tripwire (d) is
  correspondingly re-specified as a set of `reason=` *literals* rather than call sites, so the
  one-literal/four-callers shape passes instead of failing the design's own test.
- **Round-3 finding 3 (`get_user_by_id` on two lists) — closed.** The two questions are now two
  functions with disjoint caller sets: `get_user_by_id` (identity only, unconditional bypass, four
  `deps`/`auth`/`tenants` callers) and `get_user_for_admin` (authorization target, `tenant_conn`
  unless `is_platform_scoped`, two callers in `routers/users.py`). No shared boolean; neither can sit
  on both lists.
- **Round-3 finding 4 (`audit_log.tenant_id` NULL on platform-branch writes) — closed.**
  `platform_conn(stamp_tenant=…)` is added with an explicit README rule ("every `platform_conn()`
  that mutates on behalf of a known tenant passes `stamp_tenant=`"), its only legal source is the
  already-fetched `row["tenant_id"]`, and tripwire (f) fails any `platform_conn` whose function body
  also executes an INSERT/UPDATE/DELETE without it, with the genuinely-tenantless sites exempted by
  `reason=` literal. Blast radius correctly bounded to one column (under BYPASSRLS the GUC gates no
  read and no write). Test 4b is extended with the Tier 3 case that was missing.
- **Round-3 finding 5 (`GET /users` 500 for NULL-tenant service accounts) — closed.** `list_users`
  now selects `platform_conn(reason="users-null-tenant-listing")` whenever its `tenant_id` argument
  is `None`, *regardless* of `is_platform_scoped` — which is the branch a `role="viewer"`,
  NULL-tenant Conversation/vobiz account actually lands on given `routers/users.py:31`'s
  `is_platform_scoped(...) and role == "superadmin"`. The router's authorization logic is untouched,
  the SQL is unchanged (`role != 'superadmin'` / `NOT is_service_account` exclusions still apply), so
  nothing widens, and the test asserts against the pre-cutover response rather than a bare 200.
- `assert_tenant_access` remains one predicate shared by Tier 2/3/4 with the 403-on-UUID /
  404-on-slug split preserved from the existing `custom_apis.py:38` and `agents.py:24-30` precedents
  — no new existence oracle across a tenant boundary (lesson 2), and `get_user_for_admin`'s
  tenant-scoped miss yields today's 404 from an empty policy result.
- `current_tenant()` precedence (target honoured only when `caller is None`) is unchanged from the
  round-3 verification: a tenant-scoped caller cannot move the GUC via path, query or body.
- The users-module integration tests carry a real failure mode: the self-promotion case has the
  platform-scoped counter-arm so it cannot pass on a blanket rejection, the cross-tenant-move case
  has the DB-layer `InsufficientPrivilegeError` counter-check, and the four `get_user_by_id` callers
  are exercised individually rather than through one shared assertion (lesson 12).
- Mechanical enumeration (lesson 29) is in place and not regressed: `app.routes` walks for Tier 2/3,
  the AST no-bare-pool-calls tripwire for the 183 sites, `information_schema` for the table set, and
  `reason=`-literal collection for the bypass/override tables.
