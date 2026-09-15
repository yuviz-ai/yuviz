# Code review

VERDICT: AMBER

## Prior round — all 6 confirmed closed

1. (blocking) `documents.py` never converted — CLOSED. All five functions now open
   `tenant_conn()`/`platform_conn()`; no bare `pool.fetchrow/fetch/acquire` remains in the file.
2. (blocking) `POST /agents/{agent_id}/knowledge-bases` missing a tenant check — CLOSED.
   `routers/agent_kb.py:27` calls `_authorize_agent` before `assign()`; all four routes on that
   router are now covered.
3. (blocking) `_authorize_*` never calling `set_target_tenant` — CLOSED in all four instances:
   `routers/documents.py:26,36`, `routers/knowledge_bases.py:69`, `routers/agent_kb.py:39`, plus
   the proactively-found `routers/retrieval_policies.py:29`. Verified the downstream service calls
   (`list_documents`, `list_for_agent`, `upsert_policy`, `assign`) rely on that ambient target and
   now resolve for a NULL-tenant caller instead of raising `TenantUnresolved`.
4. (blocking) `get_document` missing `platform_scoped` — CLOSED (`documents.py:26`), and the
   matching `stamp_tenant=` is threaded through `update_document`/`soft_delete_document` so
   `audit_log.tenant_id` still stamps on the bypass branch.
5. (minor) `deps.py:183` raw un-normalized UUID compare — CLOSED. `assert_tenant_access` now has an
   explicit `isinstance(tenant, uuid.UUID)` branch and compares `str(parsed)` on the string branch.
6. (minor) `libs/tenancy/session.py:157` UUID-vs-str log noise — CLOSED. `_split_tenant` accepts
   `uuid.UUID`, and both warning arms compare `str(...)` on each side.

Also spot-checked for collateral breakage: `PgVectorRepository` lost its `pool` constructor arg and
gained a `conn` first parameter — `runtime.py:28` and `retrieval.py:233` are the only non-test call
sites and both match the new signature.

## New findings

1. [minor] Cross-tenant `kb_id` accepted on assignment — `services/knowledge/routers/agent_kb.py:27`
   — fails when: tenant B's admin POSTs `/agents/{B_agent}/knowledge-bases` with `kb_id` belonging
   to tenant A. `_authorize_agent` only validates the *agent's* tenant, and
   `agent_knowledge_bases_tenant_isolation` (`database/rls.sql:428`) checks only the `agents` side,
   so the junction row is written (the `kb_id` FK check runs as system and bypasses RLS).
   `_refresh_flag` then caches `has_enabled_kb=true` for an agent whose only KB retrieves nothing —
   a persistent cross-tenant reference plus a silently empty retrieval path. No content leak:
   `kb_chunks` RLS filters the foreign chunks at retrieve time. — fix: resolve `body.kb_id` through
   `kb_service.get_knowledge_base` in `_authorize_agent`'s caller and 404 if its `tenant_id` differs
   from the agent's.

2. [minor] Two different `_authorize_kb` functions with different failure shapes —
   `services/knowledge/routers/documents.py:20` vs `services/knowledge/routers/knowledge_bases.py:50`
   — fails when: RLS is not yet enforcing (the design's own shadow-verification rollout step, where
   the app check is the only live layer). The `knowledge_bases.py` copy deliberately returns 404 for
   both "absent" and "another tenant's", with a comment citing lesson 2; the `documents.py` copy
   delegates to `assert_tenant_access`, which returns 403 on a UUID mismatch — so tenant B probing
   `/documents/{A_doc_id}` gets 403 (exists) vs 404 (does not), an existence oracle. Once RLS is
   active the 403 branch is unreachable (the `tenant_conn` fetch already returned None), which makes
   the divergence invisible in tests and easy for the next reader to misjudge. — fix: have
   `documents.py`'s helpers raise the same 404 as the `knowledge_bases.py` helper, or share one.
