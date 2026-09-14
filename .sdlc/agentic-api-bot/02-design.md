# Design: Agentic API Task Execution (Multi-Level Tool Chains)

## Approach
A new **Tool Execution Service** (`services/toolexec/`, REST, port 8600) owns the custom-API
registry, the declared dependency graph, all outbound HTTP, and the per-call chain history —
exactly the way `services/knowledge/` owns knowledge bases, documents and retrieval rather than
Config Service owning them. On the conversation side the whole feature is **one static
`ToolDefinition` (`execute_api`) plus one executor**: enablement rides the existing
`tool_provider_configs` (`engine='toolexec'`) + `agent_tool_policies` (`tool_name='execute_api'`)
pair, so "no custom APIs enabled" is literally "no row", which is already byte-identical to today
(AC 9) with zero orchestrator changes. The only new seam inside `services/conversation/` is that
`ToolPolicyResolver` *specializes* `execute_api`'s `parameters_schema` for the agent
(`dataclasses.replace`) with the enum of that agent's enabled API names — one extra query, run only
when an `execute_api` policy row exists, on the same 30s `agent_id`-keyed cache it already has.

The obvious alternative — one LLM tool schema entry per registered API, chained by extra
orchestrator iterations — is rejected by AC 1 and by the PRD's settled decision: the graph is
admin-declared and resolved server-side in `services/toolexec/graph.py`, so the LLM supplies only
`api_name` + leaf inputs and never sees, orders or invents an upstream hop.

Chain depth is defined as **levels = APIs in the chain**: a leaf API is `chain_levels = 1`, the
platform ceiling is 4 (so at most 4 outbound calls), with a lower per-agent override in a new
`agent_tool_policies.max_chain_depth` column — the same NULL-means-framework-default shape as the
existing `timeout_ms` / `max_calls_per_turn` columns.

### Grounding correction (stated, not silently dropped — lesson 7)
The PRD refers to `knowledge_search` as an existing LLM-facing tool. It does not exist: RAG today is
a pre-turn retrieval that prefixes context onto the user message (`pipeline.py:863`
`_retrieve_context` → `libs.knowledge_sdk.CacheAsideKnowledgeProvider`), not a tool schema entry.
Nothing in AC 1 changes as a result — `execute_api` is still *exactly one* added entry, additive to
the four unchanged legacy entries in `registry.py`'s `_DEFAULT_TOOLS`; there is simply no
`knowledge_search` entry for it to sit "alongside". RAG behaviour is untouched (PRD "Out").

## Changes

| File | Change | Why |
|---|---|---|
| `database/schema.sql` | Append `custom_apis`, `custom_api_params`, `agent_custom_apis`, `api_chain_runs`, `api_chain_steps`, `api_side_effect_claims` + `ALTER TABLE agent_tool_policies ADD COLUMN IF NOT EXISTS max_chain_depth INT` | The whole feature's state; all additive so no `DO $$` guard is needed (lesson 13) |
| `services/toolexec/__init__.py` (new) | Package marker | Matches `services/knowledge/` layout |
| `services/toolexec/__main__.py` (new) | `uvicorn` entry, `PORT` default 8600 (8500 is vobiz) | Mirrors `services/vobiz/__main__.py` |
| `services/toolexec/app.py` (new) | FastAPI app, `lifespan` eager pool connect + `close_pool`, CORS `localhost:3000`, `LookupError`→404 / `ValueError`→400 / FK→400 handlers, `/health` | Copied shape of `services/knowledge/app.py` so docker/`verify` health checks behave identically |
| `services/toolexec/db.py` (new) | `get_pool()` / `close_pool()` asyncpg singleton | Same as `services/knowledge/db.py` |
| `services/toolexec/audit.py` (new) | `write_audit()` into the shared `audit_log` table, `entity_type='custom_api'` | Same duplication-over-cross-service-import call the Knowledge Service already made (`services/knowledge/audit.py` docstring) |
| `services/toolexec/schemas.py` (new) | Pydantic request/response models incl. `CustomApiCreate/Update`, `CustomApiParamSpec`, `ChainExecuteRequest/Response` | Repo convention: `schemas.py` per service |
| `services/toolexec/custom_apis.py` (new) | Registry CRUD; same-tenant validation of every `upstream_api_id`; `_validate_credential_ref()` (tenant-namespaced refs) and `_validate_endpoint_url()` (SSRF); `chain_levels`/cycle recompute in the write transaction; soft delete refused while a live API depends on the row | AC 10, 11, 17 — the FK alone cannot express "same tenant"; refs and URLs are tenant-authored input |
| `services/toolexec/agent_apis.py` (new) | Per-agent enable/disable of a custom API; `_authorize_agent_api()` rejects (404) any `(agent_id, custom_api_id)` pair where either row is missing/soft-deleted or the two do not share the **caller's** tenant, before any write; then rejects enable when `api.chain_levels > effective max_chain_depth` | AC 10 write-time gate + AC 11 enable-time gate; mirrors `services/knowledge/agent_kb.py` |
| `services/toolexec/graph.py` (new) | `MAX_CHAIN_LEVELS = 4`; `resolve_order(api, max_levels)` → post-order, dedup'd step list or an explicit `depth_limit_exceeded` / `cycle_detected` failure; `extract(response, json_path)` | AC 3, 4, 11 backstop, 13 |
| `services/toolexec/auth_schemes.py` (new) | `resolve_tenant_ref(tenant_id, ref)` — the tenant-namespaced wrapper around `libs.config_sdk.secret_resolver.CompositeSecretResolver` (see Interfaces); applies API key (header or query), static bearer, or OAuth2 client-credentials with a per-`(tenant_id, custom_api_id)` cached token refreshed 60s before `expires_in` | AC 12; a tenant-authored ref must not be able to name a platform secret |
| `services/toolexec/redaction.py` (new) | `redact(payload, paths)` → `"[redacted]"`; applied to step arguments (`custom_api_params.sensitive`) and responses (`custom_apis.sensitive_response_paths`) before persistence, before `success_template` interpolation, *and* before the response leaves the service | AC 14 + the PRD redaction constraint |
| `services/toolexec/executor.py` (new) | Chain runner: budget clamp, admission control, conditional run insert, per-step budget, pinned httpx call with explicit timeout and capped response read, upstream value extraction, HMAC argument hashing, atomic side-effect claim, step/run persistence | AC 2–7, 12, 14, 15 |
| `services/toolexec/admission.py` (new) | Per-`(tenant_id, agent_id)` concurrent + per-minute run caps, swept inline; no background task or pool, so nothing to tear down (lesson 26) | Finding 10: without it the service is an authenticated egress flood relay |
| `services/toolexec/routers/__init__.py` (new) | Package marker | — |
| `services/toolexec/routers/custom_apis.py` (new) | `/tenants/{tenant_id}/custom-apis` + `/custom-apis/{id}` CRUD behind `get_current_user` / `require_role("superadmin","admin")` + tenant guard | AC 16, 17; PRD's "not a bare `get_current_user`" constraint |
| `services/toolexec/routers/agent_apis.py` (new) | `/agents/{agent_id}/custom-apis` list + `PUT`/`DELETE` enablement, all three behind `_authorize_agent_api()` | AC 16 with AC 10 enforced at the API surface, not deferred to turn time |
| `services/toolexec/routers/chain_runs.py` (new) | `GET /calls/{session_id}/chain-runs` — redacted per-call chain history, behind `_authorize_chain_runs()` and a tenant-scoped query | AC 14 without a cross-tenant read of another tenant's business data |
| `services/toolexec/routers/execute.py` (new) | `POST /internal/chains/execute` — Conversation's only call into this service, gated on `require_execute_subject()` (a named service identity, not "any NULL-tenant viewer") | Firing a side-effecting chain is not a read; `/internal` prefix still mirrors `services/knowledge/routers/retrieve.py` |
| `services/toolexec/tests/*` (new) | See Test plan | — |
| `services/conversation/tools/registry.py` | Add one `_EXECUTE_API` `ToolDefinition` to `_DEFAULT_TOOLS` | AC 1: the single added LLM-facing entry; the four legacy entries are untouched |
| `services/conversation/tools/policy_resolver.py` | Add `max_chain_depth` **and `sensitive_arg_keys`** to `ResolvedToolPolicy` (`max_chain_depth` from the `agent_tool_policies` `SELECT`, `sensitive_arg_keys` from `p.sensitive` in the specialization query); when an `execute_api` row is present, run one query for the agent's enabled APIs and `dataclasses.replace` the definition with the `api_name` enum + leaf-input docs; drop the policy entirely if the agent has zero enabled APIs | AC 1, 9, 10 (the query joins `agents.tenant_id = custom_apis.tenant_id`) |
| `services/conversation/tools/middleware.py` | `LoggingMiddleware(redact_arg_keys=…)` + a pass-through parameter on `build_default_chain`; empty default = today's behaviour | Finding 8: `arguments=%r` at `middleware.py:44-46` is where a `sensitive` caller input would otherwise be logged in clear |
| `services/conversation/tools/orchestrator.py` | Pass `policy.sensitive_arg_keys` at the one existing `build_default_chain` call site | The only point that holds both the policy and the chain |
| `services/conversation/tools/provider_manager.py` | Add `_make_toolexec` under `engine='toolexec'`, returning the HTTP client (no `api_key_ref` required — this is internal infrastructure, not a tenant credential) | Same per-`tool_provider_config_id` caching every other tool provider gets |
| `services/conversation/tools/providers/toolexec/__init__.py`, `client.py` (new) | `ToolExecClient` — lazy service-account login against Config, one re-auth on 401, explicit `httpx` timeout | Copies `libs/knowledge_sdk/repositories/http_repository.py` verbatim in shape |
| `services/conversation/tools/executors/api_exec_executor.py` (new) | `ApiExecExecutor.execute()` — derives the chain budget from `request.context.deadline`, posts one request, maps `chain_status` → `ToolStatus`, returns only redacted payload | The single seam between the real-time turn and outbound HTTP |
| `services/conversation/__main__.py` | Register `execute_api` in `ExecutorRegistry` (~line 234); read `TOOLEXEC_SERVICE_URL` | Same wiring line as the three calendar executors |
| `services/config/routers/tool_provider_configs.py` | Exempt `engine='toolexec'` from the `api_key_ref or api_key is required` check (line ~35) | Otherwise the Admin UI cannot create the row `agent_tool_policies.tool_provider_config_id` requires |
| `admin-ui/lib/toolexecApi.ts` (new) | Typed client against `NEXT_PUBLIC_TOOLEXEC_SERVICE_URL` (default `http://localhost:8600`) | Exactly `admin-ui/lib/knowledgeApi.ts`'s pattern (its own base URL, shared `ApiError`) |
| `admin-ui/components/KnowledgeBaseTabs.tsx` (new) | Two sub-tabs: "Documents" → existing `<KnowledgeBasePanel/>` verbatim, "APIs" → `<CustomApisPanel/>` | AC 16 with `KnowledgeBasePanel.tsx` genuinely unchanged |
| `admin-ui/components/CustomApisPanel.tsx` (new) | List/create/edit an API (name, description, endpoint, method, auth scheme + ref, params with `source: literal \| caller \| upstream(api, json path)`, sensitive flags), per-agent enable toggle, and the `execute_api` master switch | AC 16, 17 |
| `admin-ui/app/agents/[tenantSlug]/[agentSlug]/page.tsx` | Line 794: render `<KnowledgeBaseTabs .../>` instead of `<KnowledgeBasePanel .../>` | Only change to the existing tab structure |
| `scripts/start_local.sh` | `start_toolexec_service()` block, `:8600` in the `verify` port loop + health curl, `portmap` line | Operators need it; every branch `return 0` on hints (lesson 15) |
| `docs/setup.md` | Env vars (`TOOLEXEC_SERVICE_URL`, `TOOLEXEC_EXECUTE_SUBJECTS`, `TOOLEXEC_TENANT_SECRET_ROOT`, `TOOLEXEC_HTTP_HOST_ALLOWLIST`, `TOOLEXEC_ARGS_HMAC_KEY_REF`/`_KEY_ID` plus the rotation rule, `TOOLEXEC_SIDE_EFFECT_CLAIM_TTL`, `TOOLEXEC_MAX_CHAIN_BUDGET_MS`, the two run caps, `TOOLEXEC_MAX_RESPONSE_BYTES`, service-account creds) + the new service in the start order | Same section that documents Knowledge Service; every new knob is a security control, so it is documented, not defaulted silently |
| `CURSOR.md` | One row in the area table: Custom API chains → `services/toolexec/` | Keeps the map true |

## Data
Appended to `database/schema.sql` after the `agent_tool_policies` block. All statements are additive
(`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) — there is
no destructive DDL and therefore nothing that needs a `DO $$` guard (lessons 10, 13). The tables are
new, so no data-normalizing backfill exists to collide with the `lower(name)` unique index
(lesson 5).

```sql
-- ── custom_apis — a tenant-registered HTTP API, reachable only via execute_api ──
-- auth_config is REFERENCES ONLY, same discipline as tool_provider_configs.api_key_ref:
--   api_key:  {"key_ref":"env:...","location":"header"|"query","name":"X-Api-Key"}
--   bearer:   {"token_ref":"enc:..."}
--   oauth2_client_credentials:
--             {"token_url":"https://...","client_id_ref":"env:...",
--              "client_secret_ref":"enc:...","scope":"orders.write"}
-- chain_levels is the number of APIs in this API's own chain (leaf = 1); it is
-- recomputed for this row and every transitive dependent inside the same
-- transaction as any params write, and the write is rejected if it would exceed
-- graph.MAX_CHAIN_LEVELS (4) or introduce a cycle.
CREATE TABLE IF NOT EXISTS custom_apis (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL REFERENCES tenants(id),
    name                     TEXT NOT NULL,          -- LLM-facing api_name, snake_case
    description              TEXT NOT NULL,          -- surfaced in execute_api's schema
    endpoint_url             TEXT NOT NULL,
    method                   TEXT NOT NULL CHECK (method IN ('GET','POST','PUT','PATCH','DELETE')),
    body_style               TEXT NOT NULL DEFAULT 'json' CHECK (body_style IN ('json','form')),
    auth_scheme              TEXT NOT NULL DEFAULT 'none'
                                CHECK (auth_scheme IN ('none','api_key','bearer','oauth2_client_credentials')),
    auth_config              JSONB NOT NULL DEFAULT '{}'::jsonb,
    side_effecting           BOOLEAN NOT NULL DEFAULT true,   -- UI defaults false only for GET
    idempotency_header       TEXT,                   -- NULL = downstream accepts no idempotency key
    timeout_ms               INT,                    -- per-step ceiling; NULL = 6000
    sensitive_response_paths JSONB NOT NULL DEFAULT '[]'::jsonb,  -- ["$.customer.ssn", ...]
    success_template         TEXT,                   -- optional deterministic confirmation line
    chain_levels             INT  NOT NULL DEFAULT 1,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at               TIMESTAMPTZ
);
-- Tenant-SCOPED uniqueness: two tenants may both register 'lookup_account', and
-- neither can block the other's name (lesson 3). Partial on deleted_at so a
-- soft-deleted name is reusable.
CREATE UNIQUE INDEX IF NOT EXISTS custom_apis_tenant_name_key
    ON custom_apis (tenant_id, lower(name)) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_custom_apis_tenant ON custom_apis (tenant_id) WHERE deleted_at IS NULL;

-- ── custom_api_params — one row per input, and the declared dependency graph ──
-- Edges ARE the upstream params: the graph is exactly
-- {(custom_api_id -> upstream_api_id) : source='upstream'}. No second edge table,
-- so a declared dependency can never disagree with the value it produces.
-- The FK to custom_apis cannot express "same tenant" — custom_apis.py validates
-- upstream_api_id against the owning row's tenant_id inside the write
-- transaction (AC 10, AC 17).
CREATE TABLE IF NOT EXISTS custom_api_params (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    custom_api_id      UUID NOT NULL REFERENCES custom_apis(id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    location           TEXT NOT NULL CHECK (location IN ('body','query','header','path')),
    json_type          TEXT NOT NULL CHECK (json_type IN ('string','number','integer','boolean','object','array')),
    description        TEXT NOT NULL DEFAULT '',
    required           BOOLEAN NOT NULL DEFAULT true,
    source             TEXT NOT NULL CHECK (source IN ('literal','caller','upstream')),
    literal_value      JSONB,
    upstream_api_id    UUID REFERENCES custom_apis(id),
    upstream_json_path TEXT,
    sensitive          BOOLEAN NOT NULL DEFAULT false,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (custom_api_id, name),
    CONSTRAINT custom_api_params_source_shape CHECK (
        (source = 'literal'  AND literal_value IS NOT NULL AND upstream_api_id IS NULL)
     OR (source = 'caller'   AND upstream_api_id IS NULL)
     OR (source = 'upstream' AND upstream_api_id IS NOT NULL AND upstream_json_path IS NOT NULL)),
    CONSTRAINT custom_api_params_no_self_dep CHECK (upstream_api_id IS DISTINCT FROM custom_api_id)
);
CREATE INDEX IF NOT EXISTS idx_custom_api_params_api      ON custom_api_params (custom_api_id);
CREATE INDEX IF NOT EXISTS idx_custom_api_params_upstream ON custom_api_params (upstream_api_id)
    WHERE upstream_api_id IS NOT NULL;

-- ── agent_custom_apis — which of the tenant's APIs this agent may target ─────
-- Mirrors agent_knowledge_bases / agent_tool_policies: no row = not enabled,
-- not a broken default. The execute_api agent_tool_policies row is the master
-- switch and carries timeout_ms / max_chain_depth; this table is the allow-list.
CREATE TABLE IF NOT EXISTS agent_custom_apis (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      UUID NOT NULL REFERENCES agents(id),
    custom_api_id UUID NOT NULL REFERENCES custom_apis(id),
    enabled       BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_id, custom_api_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_custom_apis_agent ON agent_custom_apis (agent_id) WHERE enabled;

-- ── api_chain_runs / api_chain_steps — the operator-facing chain history ─────
-- calls.tenant_id is a slug string on the call path, but these rows are written
-- by an admin-scoped service against real UUIDs, so tenant_id is a UUID FK like
-- every other owned row. UNIQUE (tenant_id, idempotency_key) is what makes a
-- re-invoked chain return its recorded outcome instead of re-executing (AC 15).
CREATE TABLE IF NOT EXISTS api_chain_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    agent_id        UUID NOT NULL REFERENCES agents(id),
    call_id         TEXT,
    session_id      TEXT,
    turn_id         TEXT,
    tool_call_id    TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    target_api_id   UUID NOT NULL REFERENCES custom_apis(id),
    status          TEXT NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running','success','partial','failed','timeout',
                                         'invalid_argument','unavailable','rate_limited')),
    error           TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    UNIQUE (tenant_id, idempotency_key)
);
-- Tenant-first so the chain-history read (_authorize_chain_runs) is scoped by
-- tenant in the index, not only in the predicate.
CREATE INDEX IF NOT EXISTS idx_api_chain_runs_tenant_session ON api_chain_runs (tenant_id, session_id);
CREATE INDEX IF NOT EXISTS idx_api_chain_runs_tenant  ON api_chain_runs (tenant_id, started_at DESC);

CREATE TABLE IF NOT EXISTS api_chain_steps (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             UUID NOT NULL REFERENCES api_chain_runs(id) ON DELETE CASCADE,
    step_index         INT  NOT NULL,          -- 0 = shallowest upstream
    custom_api_id      UUID NOT NULL REFERENCES custom_apis(id),
    api_name           TEXT NOT NULL,          -- denormalized: survives a rename/delete
    level              INT  NOT NULL,
    session_id         TEXT,                   -- denormalized from the run, for history reads only
    status             TEXT NOT NULL CHECK (status IN ('claimed','success','failed','timeout',
                                                       'skipped','invalid_argument','unavailable')),
    http_status        INT,
    error              TEXT,
    arguments_redacted JSONB,                  -- redaction.py applied BEFORE insert
    response_redacted  JSONB,
    argument_sources   JSONB,                  -- {"order_id":"lookup_order:$.data.id","name":"caller"}
    arguments_hash     TEXT,                   -- '<kid>:<hmac>' — see api_side_effect_claims; it is
                                               -- an HMAC, never a plain hash, because it sits in the
                                               -- same row as arguments_redacted (finding 7)
    side_effecting     BOOLEAN NOT NULL,
    idempotency_key    TEXT,
    duration_ms        INT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, step_index),
    -- A NULL is distinct from every other NULL in a unique index, so a side-
    -- effecting step with a NULL hash would silently escape the claim below
    -- (finding 5). Make it unrepresentable rather than merely unwritten.
    CONSTRAINT api_chain_steps_side_effect_keyed
        CHECK (NOT side_effecting OR arguments_hash IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_api_chain_steps_run ON api_chain_steps (run_id);

-- ── api_side_effect_claims — the AC 15 arbiter, deliberately NOT a partial ──
-- index on api_chain_steps. Two reasons the claim needs its own row:
--   (1) api_chain_steps is append-only history (AC 14) — a legitimate repeat of
--       an identical mutation after the window must not overwrite the earlier
--       step's record, which a unique index on the history table would force.
--   (2) Every claim column can be NOT NULL here, so there is no NULL row that
--       slips past uniqueness (finding 5).
-- Scope is (tenant_id, custom_api_id, arguments_hash) — NOT session_id: the
-- caller whose refund succeeded, hung up and redialled arrives with a brand new
-- session, and that is exactly the duplicate AC 15 exists to stop.
CREATE TABLE IF NOT EXISTS api_side_effect_claims (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id),
    custom_api_id  UUID NOT NULL REFERENCES custom_apis(id),
    arguments_hash TEXT NOT NULL,           -- '<kid>:<hmac>', see executor.py step 6
    run_id         UUID NOT NULL REFERENCES api_chain_runs(id) ON DELETE CASCADE,
    session_id     TEXT NOT NULL,           -- who won it, for the refusal message
    status         TEXT NOT NULL CHECK (status IN ('claimed','success','timeout','released')),
    claimed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, custom_api_id, arguments_hash)
);
CREATE INDEX IF NOT EXISTS idx_api_side_effect_claims_run ON api_side_effect_claims (run_id);

-- ── per-agent chain-depth override ──────────────────────────────────────────
-- NULL = use the platform ceiling (graph.MAX_CHAIN_LEVELS = 4), never 0/disabled
-- — the same contract timeout_ms / max_calls_per_turn already document above.
ALTER TABLE agent_tool_policies ADD COLUMN IF NOT EXISTS max_chain_depth INT;
```

## Interfaces

### Tool Execution Service — admin surface
Auth on every route: reads `Depends(get_current_user)`, writes
`Depends(require_role("superadmin","admin"))` — the same `services/config/deps.py` dependencies
`services/knowledge/routers/knowledge_bases.py` uses, never a bare authenticated check. Tenant
scope is enforced by `_require_tenant_access(tenant_id, current_user)` (copied from
`services/config/routers/provider_configs.py:68`, using `deps.is_platform_scoped` rather than a
role comparison — lesson 24) for `/tenants/{tenant_id}/…` routes, by an
`_authorize_custom_api(id, current_user)` helper for the id-addressed `/custom-apis/{id}` routes,
by `_authorize_agent_api(agent_id, custom_api_id, current_user)` for every
`/agents/{agent_id}/custom-apis…` route, and by
`_authorize_chain_runs(session_id, current_user)` for the chain-history route.

**All three helpers return 404 with the identical detail string for "no such id" and "not your
tenant"** — deliberately diverging from `provider_configs.py`'s 403, which lets a tenant admin
probe whether an opaque id exists elsewhere on the platform (lesson 2).

```python
# services/toolexec/agent_apis.py — the AC 10 write-time gate. Runs INSIDE the
# PUT/DELETE/GET handlers, before any INSERT/UPDATE, and is the reason a
# tenant-A admin who guesses tenant B's custom_api_id gets a rejection at the
# API surface rather than a row that merely fails to resolve at turn time.
async def _authorize_agent_api(agent_id: str, custom_api_id: str | None,
                               current_user: CurrentUser) -> tuple[dict, dict | None]:
    """One query joining both sides to the same tenant:

        SELECT a.id AS agent_id, a.tenant_id, ca.id AS custom_api_id, ca.chain_levels
        FROM agents a
        LEFT JOIN custom_apis ca
               ON ca.id = $2 AND ca.tenant_id = a.tenant_id AND ca.deleted_at IS NULL
        WHERE a.id = $1 AND a.deleted_at IS NULL

    Raises LookupError (-> 404, identical detail text in every case) when the
    agent does not exist, when custom_api_id was given but did not join (absent,
    soft-deleted, or a DIFFERENT tenant's API — AC 10), or when the agent's
    tenant is not the caller's and the caller is not platform-scoped
    (`deps.is_platform_scoped`, lesson 24). custom_api_id=None is the list
    route, which checks only the agent side."""


# services/toolexec/agent_apis.py — the chain-history gate (finding 2).
# api_chain_runs.session_id is unscoped opaque TEXT, so the tenant predicate
# must be in the query itself, not derived from the path.
async def _authorize_chain_runs(session_id: str, current_user: CurrentUser) -> list[dict]:
    """Reads runs + steps for one session, with

        WHERE r.session_id = $1
          AND ($2::uuid IS NULL OR r.tenant_id = $2)   -- $2 = current_user.tenant_id

    so a tenant-scoped caller sees only their own tenant's runs and a
    platform-scoped caller (`deps.is_platform_scoped`, lesson 24) sees the
    session unfiltered. An empty result raises LookupError -> 404 with the
    SAME detail text an unknown session_id produces, so a foreign session_id
    is not an existence oracle (lesson 2). Indexed by
    idx_api_chain_runs_tenant_session."""
```

```
GET    /tenants/{tenant_id}/custom-apis                       -> CustomApi[]
POST   /tenants/{tenant_id}/custom-apis                       -> CustomApi          201
GET    /custom-apis/{custom_api_id}                           -> CustomApi (with params[])
PATCH  /custom-apis/{custom_api_id}                           -> CustomApi
DELETE /custom-apis/{custom_api_id}                           -> 204  (soft delete; 409 if a live
                                                                 API still declares it upstream)
GET    /agents/{agent_id}/custom-apis                         -> AgentCustomApi[]   (id, name,
                                                                 chain_levels, enabled)
PUT    /agents/{agent_id}/custom-apis/{custom_api_id}         -> AgentCustomApi     {enabled: bool}
DELETE /agents/{agent_id}/custom-apis/{custom_api_id}         -> 204
GET    /calls/{session_id}/chain-runs                         -> ChainRun[] with steps[]  (AC 14)
                                                              (tenant-scoped: see _authorize_chain_runs)
GET    /health                                                -> {"status":"ok"}
```

`CustomApiCreate` carries `params: CustomApiParamSpec[]` and is written in one transaction:

```python
# services/toolexec/custom_apis.py
async def create_custom_api(*, tenant_id, name, description, endpoint_url, method, body_style,
                            auth_scheme, auth_config, side_effecting, idempotency_header,
                            timeout_ms, sensitive_response_paths, success_template,
                            params: list[dict], user_id, user_email) -> dict: ...
# Raises ValueError (-> 400 via app.py) for, with the offending name/id in the message:
#   unknown_upstream_api          upstream_api_id not found, soft-deleted, or another tenant's (AC 17)
#   credential_ref_not_a_reference  auth_config value is not env:/enc:/k8s:
#   credential_ref_outside_tenant_namespace
#                                 auth_schemes.validate_tenant_ref() rejected the ref: env: refs
#                                 must be env:TENANT_<this tenant's uuid hex>_*, k8s: refs must be
#                                 k8s:tenants/<this tenant_id>/<name> and stay under
#                                 TOOLEXEC_TENANT_SECRET_ROOT after .resolve(); enc: always ok
#   invalid_endpoint_url          _validate_endpoint_url() rejected the URL — see its contract
#                                 below (scheme, absolute deny-list over EVERY resolved A/AAAA
#                                 record, no userinfo/fragment)
#   chain_depth_exceeded          this row or a transitive dependent would exceed 4 levels (AC 11)
#   dependency_cycle              the edge closes a cycle
```

`chain_levels` maintenance and both rejections happen in the same transaction as the params write,
via one recursive CTE over `custom_api_params` that computes levels for the row *and every
transitive dependent* (a mid-graph edit can push a dependent over the ceiling, not just the edited
row) with a path array for cycle detection. Serialized per tenant with
`SELECT pg_advisory_xact_lock(hashtext('custom_apis:' || tenant_id))` so two concurrent edits cannot
each individually pass the depth check and jointly break it.

### Tool Execution Service — internal surface (the service boundary)
```
POST /internal/chains/execute
```
Auth: `Depends(require_execute_subject)` — **not** a bare `get_current_user`, and **not**
`is_platform_scoped` alone. Firing a side-effecting chain in a tenant is a write, and every service
account on this platform is a `role="viewer"`, `tenant_id=NULL` identity (Conversation, vobiz, the
Config/Knowledge SDK accounts), so "platform-scoped" would let any one of them move another
tenant's money:

```python
# services/toolexec/routers/execute.py
_EXECUTE_SUBJECTS = frozenset(
    e.strip().lower() for e in os.environ.get(
        "TOOLEXEC_EXECUTE_SUBJECTS", "conversation-service@internal.yuviz.ai",
    ).split(",") if e.strip()
)

async def require_execute_subject(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Named-identity gate: the caller must be a service account whose email is
    in the operator-configured allow-list. `is_service_account` is carried in the
    JWT (services/config/auth.py:50), so this needs no DB read and cannot be
    satisfied by a human console account. A tenant-scoped service account (should
    one ever exist) must additionally match the body's tenant_id — checked in the
    handler, since only it has the body:

        if user.tenant_id is not None and user.tenant_id != body.tenant_id: 403

    403 (not 404) is correct here: the caller is a platform service, not a tenant
    actor, so there is no tenant boundary to leak existence across."""
    if not (user.is_service_account and user.email.lower() in _EXECUTE_SUBJECTS):
        raise HTTPException(status_code=403, detail="identity may not execute API chains")
    return user
```

`docs/setup.md` and `scripts/start_local.sh` export `TOOLEXEC_EXECUTE_SUBJECTS` alongside the
service's other env vars.

```python
class ChainExecuteRequest(BaseModel):
    tenant_id: str; agent_id: str
    call_id: str; session_id: str; turn_id: str
    tool_call_id: str; idempotency_key: str
    api_name: str
    caller_arguments: dict[str, Any] = {}
    chain_budget_ms: int          # whole-chain wall clock, derived from the turn's deadline;
                                  # server clamps to TOOLEXEC_MAX_CHAIN_BUDGET_MS (can only lower)
    max_chain_depth: int          # effective per-agent ceiling, already min'd with 4

class ChainStepReport(BaseModel):
    api_name: str; level: int
    status: Literal["success","failed","timeout","skipped","invalid_argument","unavailable"]
    from_prior_step: list[str] = []      # param names sourced from an upstream response

class ChainExecuteResponse(BaseModel):
    run_id: str
    chain_status: Literal["success","partial","failed","timeout","invalid_argument",
                          "unavailable","rate_limited"]
    steps: list[ChainStepReport]
    completed_steps: list[str] = []      # AC 6: what DID happen, never dropped
    failed_step: ChainStepReport | None = None
    data: dict[str, Any] = {}            # redacted projection of the FINAL step only
    missing_fields: list[dict] = []      # same shape CalendarExecutor already returns
    deterministic_response: str | None = None
    error: str | None = None
```

`data` is populated **only** for `chain_status == "success"`. A failed or partial chain returns no
success-shaped fields at all, so there is nothing for the LLM to echo as a completion — that, plus
`completed_steps`/`failed_step`, is how AC 5/6 are met without inventing a second fabrication
classifier. `deterministic_response` is set only when the final step succeeded *and* the API has a
`success_template` — the same `ToolResult.deterministic_response` escape hatch
`CalendarExecutor` already uses for real booking successes, which `ToolCallOrchestrator` speaks
verbatim. **Because it is spoken verbatim and lands in the transcript, template interpolation is
constrained on both ends (finding 9):**

- *At registration*, `custom_apis._validate_success_template()` parses every `{{$.path}}`
  placeholder and rejects the save with `invalid_success_template` (naming the placeholder) unless
  the path is a well-formed JSON path into this API's own response **and** is neither equal to, nor
  a descendant or ancestor of, any entry in `sensitive_response_paths`, and does not name a
  `custom_api_params` row marked `sensitive`. `PATCH`ing `sensitive_response_paths` re-runs the same
  validation against the stored template, so an admin cannot add a path *after* the template already
  references it.
- *At runtime*, interpolation reads **only** the already-redacted response projection
  `redaction.redact()` produced — never the raw response — so a path that became sensitive between
  registration and the call renders `[redacted]` rather than the value. Each substituted value is
  coerced to `str`, stripped of control characters, and truncated to 120 characters (it is about to
  be spoken by TTS). If any placeholder is unresolved, `deterministic_response` is left `None` and
  the LLM narrates from `data` instead — a literal `{{…}}` is never spoken. `pipeline.py`'s `_claims_booking_without_tool_call` is unchanged and still guards bookings.

### Conversation side
```python
# services/conversation/tools/registry.py — the ONE added entry
_EXECUTE_API = ToolDefinition(
    name="execute_api", category="custom_api",
    description="Call one of this business's own systems. Pick api_name from the list below and "
                "supply only the inputs it says come from the caller; anything a prior system must "
                "provide is fetched automatically — never ask the caller for it and never claim a "
                "result this function did not return. <per-agent list appended at resolve time>",
    parameters_schema={
        "type": "object",
        "properties": {
            "api_name": {"type": "string", "enum": []},           # filled per agent
            "inputs":   {"type": "object", "description": "..."},  # leaf inputs only
        },
        "required": ["api_name"],
    },
)
```
`inputs` is a flat free-form object rather than a per-API `oneOf`: conditional-schema support is
uneven across the OpenAI/Ollama/Gemini function-calling shapes `llm_adapter.py` bridges. Per-API
required leaves are documented in the description and validated server-side, returning
`INVALID_ARGUMENT` + `missing_fields` — the existing convention `book_appointment` already relies on.

```python
# services/conversation/tools/policy_resolver.py
@dataclass(frozen=True)
class ResolvedToolPolicy:      # + two fields
    max_chain_depth:     int | None
    # Union of custom_api_params.name WHERE sensitive AND source='caller', across
    # the agent's enabled APIs. Empty frozenset for every legacy tool, so their
    # logging is byte-identical to today. Finding 8: LoggingMiddleware logs
    # `arguments=%r` verbatim (middleware.py:44-46), which is the LLM's inputs
    # object — so a value toolexec's own redaction.py scrubs out of the step rows
    # an operator reads would otherwise sit in clear in the conversation
    # service's application log, a wider reader set with different retention.
    # Over-redaction across two APIs sharing a key name is accepted: it fails safe.
    sensitive_arg_keys:  frozenset[str] = frozenset()

async def _specialize_execute_api(self, defn: ToolDefinition, agent_id: str) -> ToolDefinition | None:
    """Returns defn with api_name.enum + per-API leaf-input docs, or None when the
    agent has zero enabled custom APIs (in which case the policy is dropped, so
    the LLM never sees an execute_api with an empty enum)."""
```
The backing query is the runtime tenant fence for AC 10 — an `agent_custom_apis` row can only
resolve if the agent and the API share a tenant, independent of the write-time check:
```sql
SELECT ca.id, ca.name, ca.description, ca.chain_levels,
       p.name AS param_name, p.description AS param_description, p.json_type, p.required,
       p.sensitive AS param_sensitive
FROM agent_custom_apis aca
JOIN custom_apis ca ON ca.id = aca.custom_api_id AND ca.deleted_at IS NULL
JOIN agents      a  ON a.id  = aca.agent_id AND a.tenant_id = ca.tenant_id
LEFT JOIN custom_api_params p ON p.custom_api_id = ca.id AND p.source = 'caller'
WHERE aca.agent_id = $1 AND aca.enabled
ORDER BY ca.name, p.name
```
`param_sensitive` is what populates `sensitive_arg_keys`: `_specialize_execute_api` returns
`(ToolDefinition, frozenset[str])` — the definition for the LLM and the union of
`param_name WHERE param_sensitive` across the agent's enabled APIs — and `enabled_tools()` puts the
frozenset on the `ResolvedToolPolicy` it builds for `execute_api` (`frozenset()` for every other
tool). It runs only when the agent's
`agent_tool_policies` rows contain `execute_api`, and its result is cached in the existing 30s
`agent_id` cache — so an agent with no custom APIs issues **zero** extra queries and gets zero added
latency (AC 9).

```python
# services/conversation/tools/middleware.py — finding 8, kept generic (no tool
# name appears here; the orchestrator supplies the keys from the resolved policy).
class LoggingMiddleware:
    def __init__(self, redact_arg_keys: frozenset[str] = frozenset()) -> None: ...
    # Before each log call, any dict key whose name is in redact_arg_keys is
    # replaced with "[redacted]" at any depth of `request.arguments` and of
    # `result.payload`. The empty default — every existing caller — is today's
    # behaviour unchanged.

def build_default_chain(executor, timeout_ms: int = 6000, metrics: IMetrics | None = None,
                        redact_arg_keys: frozenset[str] = frozenset()) -> MiddlewareChain: ...

# services/conversation/tools/orchestrator.py — one argument added at the single
# existing build_default_chain call site in _execute_tool_call:
#     chain = build_default_chain(executor, timeout_ms=timeout_ms, metrics=self._metrics,
#                                 redact_arg_keys=policy.sensitive_arg_keys)
```

**How the set reaches the log line, stated because it is not incidental:** `LoggingMiddleware` is
handed a `ToolExecutionRequest`, never a `ResolvedToolPolicy`, so the keys are injected at
**construction** time. That works without any new plumbing because `build_default_chain` is already
called *per tool call*, inside `_execute_tool_call`, at the one place that holds both `policy` and
the chain (`orchestrator.py:194-196`) — the middleware instances are not long-lived or shared, so a
per-agent key set is legitimate state on them. Two alternatives were rejected: putting the set on
`ToolExecutionContext` (a wider blast radius — `types.py` is imported by every executor and
explicitly documented as the seam that must stay dependency-free — for no gain), and scrubbing
inside `ApiExecExecutor` (too late: `LoggingMiddleware` logs `request.arguments` *before* calling
the executor, so the executor cannot pre-empt it). **`types.py` is therefore unchanged.**

```python
# services/conversation/tools/executors/api_exec_executor.py
class ApiExecExecutor:
    def __init__(self, client: ToolExecClient, max_chain_depth: int | None) -> None: ...
    async def execute(self, request: ToolExecutionRequest) -> ToolResult: ...
```
`chain_budget_ms = int((request.context.deadline - time.monotonic()) * 1000)` — the whole chain is
bounded by the *existing* `agent_tool_policies.timeout_ms` budget that `TimeoutMiddleware` already
enforces for this tool call, so the new ceiling composes with the old ones instead of bypassing them
(PRD constraint). For `execute_api`, `timeout_ms` therefore means *whole-chain* budget; the Admin UI
defaults it to 20000 when creating the policy row and says so inline. `chain_status` maps 1:1 onto
`ToolStatus` (including `rate_limited` → the enum's existing `ToolStatus.RATE_LIMITED`) except
`partial` → `ToolStatus.FAILED` with `payload["partial"] = True`.

### Chain execution semantics (`services/toolexec/executor.py`)
1. **Resolve and verify ownership in one query** — `tenant_id`, `agent_id` and `api_name` arrive as
   independent body fields and none of them is trusted:
   ```sql
   SELECT ca.*, atp.timeout_ms, atp.max_chain_depth
   FROM agents a
   JOIN custom_apis ca        ON ca.tenant_id = a.tenant_id AND lower(ca.name) = lower($3)
                              AND ca.deleted_at IS NULL
   JOIN agent_custom_apis aca ON aca.agent_id = a.id AND aca.custom_api_id = ca.id AND aca.enabled
   LEFT JOIN agent_tool_policies atp ON atp.agent_id = a.id AND atp.tool_name = 'execute_api'
                                    AND atp.enabled
   WHERE a.id = $2 AND a.tenant_id = $1 AND a.deleted_at IS NULL
   ```
   Zero rows ⇒ `invalid_argument` / `api_not_enabled_for_agent`, before the run row is claimed and
   before any HTTP call. `a.tenant_id = $1` is what stops a caller from pairing one tenant's
   `tenant_id` with another's `agent_id`; `ca.tenant_id = a.tenant_id` is the same fence
   `policy_resolver`'s query applies. `api_chain_runs.tenant_id`/`agent_id` are written from this
   verified row, never from the request body. An API disabled or soft-deleted since the schema was
   cached stops working on the very next turn (lesson 16: the grant is re-validated at redemption,
   not cached).
2. **Order** via `graph.resolve_order(target, max_levels=min(request.max_chain_depth, 4))`.
   Post-order DFS, dedup'd by api id so a diamond runs a shared upstream once; a chain longer than
   the ceiling or containing a cycle returns `failed` with `error="depth_limit_exceeded"` /
   `"dependency_cycle"` **before any HTTP call** — never truncated, never looped (AC 11 backstop).
   APIs with no declared edge between them never appear in each other's order (AC 13).
2b. **Admit or refuse** (finding 10). `chain_budget_ms` arrives in the body and is clamped —
   `min(body.chain_budget_ms, TOOLEXEC_MAX_CHAIN_BUDGET_MS)`, default 30000 — so a client-supplied
   value can only ever *lower* the budget, exactly as `max_chain_depth` can only lower the depth
   ceiling. Then `admission.acquire(tenant_id, agent_id)` enforces
   `TOOLEXEC_MAX_CONCURRENT_RUNS_PER_AGENT` (default 4) and
   `TOOLEXEC_MAX_RUNS_PER_MINUTE_PER_AGENT` (default 60); over either ⇒ `chain_status="rate_limited"`
   with **no run row and no HTTP call**, which `ApiExecExecutor` maps to the existing
   `ToolStatus.RATE_LIMITED`. Counters are per-`(tenant_id, agent_id)` in-process dicts swept inline
   on each check (no background task and no executor, so there is no lifecycle to shut down —
   lesson 26); they are per-replica, so the effective ceiling is `replicas × limit`. That is stated,
   not hidden: this is an abuse brake against a single tenant turning the platform's egress into an
   authenticated flood relay, not a billing quota, and the per-replica bound still caps it to a
   known multiple. Redis is deliberately not introduced for it. The concurrency slot is released in a `finally` when the run reaches a terminal status — including the barge-in case, where the chain keeps running server-side and so must keep holding its slot until it actually finishes.
3. **Claim the run** with `INSERT INTO api_chain_runs … ON CONFLICT (tenant_id, idempotency_key) DO
   NOTHING RETURNING id`. Zero rows = the loser's path (lesson 8): load the existing run — if
   terminal, return its recorded outcome and steps with **no new HTTP calls**; if still `running`,
   return `failed` / `chain_already_running`.
4. **Per step**: re-validate and pin the destination (finding 6 — a registration-time-only check
   loses to DNS rebinding, and IP+`Host` pinning would break TLS):
   ```python
   # services/toolexec/custom_apis.py — used at registration AND before every call
   _DENIED_NETS = [ipaddress.ip_network(n) for n in (
       "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
       "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4",
       "240.0.0.0/4", "255.255.255.255/32",
       "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "64:ff9b::/96",
   )]

   async def resolve_and_validate_endpoint(url: str) -> tuple[str, list[str]]:
       """Returns (hostname, allowed_ips) or raises ValueError('invalid_endpoint_url').

         - scheme must be 'https', unless the hostname is in the operator-configured
           TOOLEXEC_HTTP_HOST_ALLOWLIST (an explicit host list, NOT "any private
           address" — the previous TOOLEXEC_ALLOW_HTTP knob is removed: it both
           contradicted this deny-list and made the dev default the unsafe one).
         - no userinfo, no fragment, no non-default port unless allow-listed.
         - getaddrinfo() the host for BOTH families and check EVERY returned
           record through ipaddress.ip_address() — which normalizes decimal/octal/
           hex IPv4, '0', and IPv4-mapped IPv6 ('::ffff:127.0.0.1') to their real
           value. If ANY record falls in _DENIED_NETS (or is not global per
           .is_global), reject the whole URL — never dial "the good one".
       """
   ```
   The request then goes through **one shared `httpx.AsyncClient` per step configured with
   `transport=PinnedResolverTransport(allowed_ips)`** — an `httpx.AsyncHTTPTransport` subclass that
   connects to a pre-validated address while leaving the URL, SNI and certificate verification on
   the **hostname**, so TLS still validates and `verify=False` is never needed (and is explicitly
   forbidden). `follow_redirects=False` is set explicitly: a 3xx is returned to the chain as that
   step's result, not followed, so a `302 → 169.254.169.254` cannot defeat the check. A failure here
   is `failed` / `invalid_endpoint_url` with no request sent. Then
   `step_timeout_ms = min(api.timeout_ms or 6000, remaining chain budget)`, passed
   explicitly to `httpx.AsyncClient(timeout=…)` (lesson 18/19 — never an implicit default). Exhausted
   budget or a step timeout ⇒ that step is `timeout`, all deeper steps are recorded `skipped`, and the
   run is `timeout` (AC 7). No retries: `side_effecting` steps must never be retried, and the
   framework's `RetryMiddleware` is off by default for every other tool too.

   The response body is read with `client.stream(...)` and abandoned past
   `TOOLEXEC_MAX_RESPONSE_BYTES` (default 1 MiB) — short-circuited on `Content-Length` when present
   — ⇒ that step is `failed` / `response_too_large` (finding 10: an unbounded read is both a memory
   hazard and free egress amplification).
5. **Arguments**: `literal` from `literal_value`, `caller` from `caller_arguments` (missing +
   `required` ⇒ `invalid_argument` + `missing_fields`, no call made), `upstream` from
   `graph.extract(prior_response, json_path)` (missing ⇒ that step is `failed` /
   `upstream_value_missing`; the LLM is never asked for it — AC 3).

   Every value is then coerced to its declared `json_type` and **placed, never concatenated**
   (finding 4 — an unauthenticated phone caller is the ultimate source of a `caller` value, via the
   LLM's free-form `inputs`):
   - `location='header'`: value must be a `str` matching `^[\x20-\x7E]*$` — any CR, LF, NUL or
     other control character ⇒ `invalid_argument` / `illegal_header_value` (the param name only,
     never the value, in the error), so a spoken `"\r\nAuthorization: …"` cannot inject a header.
   - `location='path'`: substituted as a **single URL segment** — `quote(value, safe="")`, which
     percent-encodes `/ ? # % .` — and rejected outright if the raw value contains `..` or a control
     character. A path param can therefore never re-target the request to another path or bolt on a
     query string.
   - `location='query'`: passed through `httpx`'s `params=` dict (which percent-encodes), never
     interpolated into the URL string.
   - `location='body'`: serialized by `json=`/`data=` per `body_style`; no template substitution
     anywhere in the request path.
6. **Side-effect fail-closed** (AC 15) — one **derived key**, used as both the claim key and the
   downstream idempotency value so the two can never drift, plus an **atomic claim taken before the
   outbound call** (lesson 8, not a read-then-write):

   ```python
   # services/toolexec/executor.py — the ONE derivation. HMAC, not a bare
   # sha256: this value sits in api_chain_steps beside arguments_redacted, so a
   # plain digest of a 9-digit id, a phone number or an order number is an
   # offline-brute-forceable verifier of exactly the value redaction removed
   # (finding 7). Redaction that ships a verifier is not redaction.
   #   key      = await platform_secret_resolver.resolve(TOOLEXEC_ARGS_HMAC_KEY_REF)
   #              — a REFERENCE ('env:'/'enc:'/'k8s:'), resolved once at startup
   #              through the platform CompositeSecretResolver (NOT the
   #              tenant-namespaced resolver: this is a platform secret, not
   #              tenant-authored input). Absent ⇒ the service fails to start,
   #              the same fail-loud posture JWT_SECRET already has.
   #   kid      = TOOLEXEC_ARGS_HMAC_KEY_ID (default 'k1') — stored inline so a
   #              rotated key produces a visibly different namespace of hashes
   #              rather than silently colliding with the old one.
   def _derive(tag: str, tenant_id, custom_api_id, resolved_arguments) -> str:
       """ONE derivation function, TWO domain-separation tags. The stored value
       and the exported value must not be equal: the idempotency header is sent
       in plaintext to an endpoint the tenant admin registered and controls, so a
       single derivation would hand that admin an HMAC oracle over its own
       argument space — invoke with a guessed value for a field it sees
       '[redacted]' in its own chain history, then compare the header its own
       server received against the stored hash. That does not make exhaustive
       search practical (each guess costs a real call and burns a claim) but it
       makes a shortlist — a suspected order id, a known date of birth —
       entirely practical, which is exactly what the HMAC was added to prevent.
       Same key, same canonical input, one function, so the two values cannot
       drift; different tag, so neither reveals the other."""
       return f"{kid}:" + hmac.new(
           key,
           (tag + "|" + canonical_json(
               {"t": tenant_id, "a": custom_api_id, "args": resolved_arguments})).encode(),
           hashlib.sha256,
       ).hexdigest()

   arguments_hash  = _derive("claim", ...)   # STORED  (api_chain_steps, api_side_effect_claims)
   idempotency_key = _derive("idem",  ...)   # EXPORTED (custom_apis.idempotency_header only)
   ```
   **Rotation:** changing the key (or `kid`) makes every pre-rotation claim
   unmatchable, so an identical mutation claimed before the rotation could fire once more. That is
   bounded by the claim window below, so `docs/setup.md` states the rule plainly: rotate no more
   often than the window, and treat a rotation as "one window during which redialled duplicates are
   possible". Old rows are never re-hashed (the plaintext is gone by design).

   **The claim**, one statement, conflict target `UNIQUE (tenant_id, custom_api_id, arguments_hash)`:
   ```sql
   INSERT INTO api_side_effect_claims
       (tenant_id, custom_api_id, arguments_hash, run_id, session_id, status)
   VALUES ($1, $2, $3, $4, $5, 'claimed')
   ON CONFLICT (tenant_id, custom_api_id, arguments_hash) DO UPDATE
       SET run_id = EXCLUDED.run_id, session_id = EXCLUDED.session_id,
           status = 'claimed', claimed_at = now()
       WHERE api_side_effect_claims.status = 'released'
          OR api_side_effect_claims.claimed_at < now() - $6::interval
   RETURNING id
   ```
   Zero rows returned is the **loser's path**: a live claim exists, so this step is recorded (in the
   losing run) as `failed` / `side_effecting_step_already_completed`, naming when it happened so the
   agent says "that refund already went through" instead of issuing a second one. `DO UPDATE …
   WHERE` is what expresses the window without a non-immutable index predicate, and it takes over a
   stale claim in the same statement rather than reading first.

   **Window** — `TOOLEXEC_SIDE_EFFECT_CLAIM_TTL`, default **24 hours**, chosen not assumed: the
   window must outlive the human retry loop AC 15 actually targets (a caller who is cut off mid-chain
   and redials, is transferred and repeats themselves, or calls back the same day — minutes to
   hours), and must be short enough that a *legitimate* repeat of a byte-identical mutation on a
   later day is still possible. It only ever gates mutations whose resolved arguments are identical:
   a second refund for a different order, or a re-order with any differing field, has a different
   `arguments_hash` and never touches the claim. A **caller-identity key was considered and
   rejected**: a webcall/browser session has no ANI at all (`ToolExecutionContext.caller_number` is
   `""`), so the key would fail open exactly where it matters, and a SIP ANI is attacker-supplied —
   it would narrow nothing `arguments_hash` does not already cover while adding a spoofable input.

   **Release:** the claim is set `released` only on proof that no mutation occurred — a 4xx other
   than 409 — which frees an immediate legitimate retry. A `timeout` or any 5xx leaves it `claimed`
   (a timed-out mutation may well have landed): fail closed. Success sets it `success`, which the
   window governs.

   **The downstream header** (`custom_apis.idempotency_header`, when configured) is sent as
   `_derive("idem", …)` — **not** `sha256(run.idempotency_key:custom_api_id)`: `run.idempotency_key`
   defaults to the per-turn `tool_call_id`, so the previous derivation handed the downstream API a
   fresh key every turn and deduped nothing (finding 5). Being a function of `(tenant_id,
   custom_api_id, resolved arguments)`, it is stable across turns, sessions and redials — the
   downstream API dedupes exactly the requests our own claim does, by construction — while being a
   *different* value from the stored `arguments_hash`, so nothing the tenant's own endpoint receives
   can be compared against what chain history stores. `api_chain_steps.idempotency_key` records the
   exported value (it is what the downstream API saw); the claim tables record only the `claim`-tagged
   one.

   Non-side-effecting steps take no claim and carry no idempotency header.

7. **Auth**: `auth_schemes.apply(api, request_kwargs)` resolves every ref through
   `resolve_tenant_ref(api.tenant_id, ref)` (below) at call time — **never** the bare
   `CompositeSecretResolver`. An unresolvable, absent or out-of-namespace ref raises before the
   request is built ⇒ `unavailable` / `credential_unavailable`, with **no** outbound request and the
   ref itself never in the error, the step row, or the log line (AC 12). OAuth2 tokens are cached
   in-process under `(tenant_id, custom_api_id)` — a tenant-scoped cache key by construction — with
   an `expires_at - 60s` refresh; the token is never persisted.

   ```python
   # services/toolexec/auth_schemes.py — finding 1. A tenant admin authors these
   # refs, so scheme-shape validation is not enough: EnvResolver returns ANY
   # process env var (libs/config_sdk/secret_resolver.py:36-44) and
   # K8sFileResolver does an unguarded `Path(mount_root) / ref` join, so
   # `env:JWT_SECRET` or `k8s:../../proc/self/environ` would otherwise exfiltrate
   # the platform's signing key / encryption key / arbitrary files to a
   # tenant-chosen endpoint_url.
   TENANT_ENV_PREFIX = "TENANT_"          # env:TENANT_<uuid-hex-upper>_<NAME>
   TENANT_SECRET_ROOT = os.environ["TOOLEXEC_TENANT_SECRET_ROOT"]  # NOT the platform k8s mount

   def validate_tenant_ref(tenant_id: str, ref: str) -> None:
       """Raises ValueError('credential_ref_outside_tenant_namespace') unless the
       ref resolves inside a namespace provably owned by tenant_id:

         enc:  always allowed — the ciphertext IS the secret, it names nothing
               (libs/config_sdk/secrets.py Fernet); no namespace to escape.
         env:  must fullmatch  r'env:TENANT_{hex}_[A-Z0-9_]+'  where
               hex = uuid.UUID(tenant_id).hex.upper(). No platform variable
               (JWT_SECRET, SECRET_ENCRYPTION_KEY, POSTGRES_DSN, SMTP_PASSWORD,
               CONFIG_SERVICE_PASSWORD) can match, and one tenant cannot name
               another's because the hex is fixed by the row's own tenant_id.
         k8s:  must fullmatch  r'k8s:tenants/{tenant_id}/[A-Za-z0-9._-]+'  — no
               '/' or '.' sequences beyond that, so '..' is unrepresentable — AND
               (Path(TENANT_SECRET_ROOT) / rel).resolve() must be relative_to
               (Path(TENANT_SECRET_ROOT).resolve() / 'tenants' / tenant_id).
               The .resolve() check is belt to the regex's braces: a symlink
               planted in the mount cannot escape either.
         anything else (including a literal): rejected.
       """

   async def resolve_tenant_ref(tenant_id: str, ref: str) -> str:
       validate_tenant_ref(tenant_id, ref)     # re-checked at RESOLUTION, with the
       ...                                     # tenant_id read from the custom_apis
                                               # row — not only at registration, so a
                                               # future bulk-import/script path cannot
                                               # bypass it (lesson 16, lesson 19)
   ```
   `custom_apis._validate_credential_ref()` calls the same `validate_tenant_ref` at registration, so
   a bad ref is a 400 at save time as well as a refusal at call time.
8. **Persist** each step (redacted) as it completes and finalize the run: `success` if every step
   succeeded, `partial` if at least one succeeded before the failure, else the failing status. The
   partial state is in both the response and the row (AC 6, 14).
9. **Barge-in** (AC 8): unchanged. `ToolCallOrchestrator._execute_tool_call`'s `asyncio.wait` race
   against `cancel_event` already abandons this HTTP call exactly as it abandons a Cal.com call; the
   chain finishes server-side and is recorded, and there is deliberately **no** resume/poll endpoint,
   so a mid-flight chain can never be resurrected into a later turn. A repeat request in a new turn
   arrives with a new `tool_call_id`, hits rule 6, and fails closed rather than duplicating a mutation.

## Risks
- **A tenant-registered endpoint URL is an SSRF primitive against internal networks** — mitigated by
  a specified code path, not a posture: `resolve_and_validate_endpoint()` runs at registration *and*
  before every call, normalizes and checks **every** A/AAAA record against an absolute deny-list
  (loopback, link-local/metadata, ULA, all RFC1918, CGNAT, `0.0.0.0/8`, IPv4-mapped IPv6,
  multicast/reserved), plaintext `http` requires an operator host allow-list rather than a
  "private addresses are fine" switch, `follow_redirects=False` so no hop escapes the check, and the
  connection is pinned via a resolver transport that keeps SNI and certificate verification on the
  hostname — `verify=False` is forbidden outright.
- **A tenant-authored credential reference can name a platform secret** (`env:JWT_SECRET`,
  `k8s:../../proc/self/environ`) and ship it to a tenant-chosen endpoint — mitigated by
  `auth_schemes.validate_tenant_ref()`, which confines `env:` refs to `TENANT_<this tenant's uuid
  hex>_*`, confines `k8s:` refs to `tenants/<this tenant_id>/<name>` under a dedicated
  `TOOLEXEC_TENANT_SECRET_ROOT` with a `.resolve()` containment check, allows `enc:` freely (the
  ciphertext names nothing), and is enforced at resolution as well as registration so no future
  write path can bypass it.
- **`/internal/chains/execute` fires side-effecting calls, so "any authenticated service account" is
  too broad a gate** — restricted to `is_service_account` identities named in
  `TOOLEXEC_EXECUTE_SUBJECTS`, and the executor re-derives tenant/agent/API ownership in one joined
  query rather than trusting the body's `tenant_id`/`agent_id` pair.
- **`LoggingMiddleware` in the conversation service logs `request.arguments` and `result.payload`
  verbatim, which is exactly where a sensitive chained value would leak** — the response half is
  covered because the executor only ever receives a redacted `data` projection; the *request* half
  is not covered by that argument (a `sensitive` caller input is scrubbed from the step rows an
  operator reads, so leaving it in clear in the application log would put it in the wider-reader,
  different-retention place the redaction constraint forbids), so `LoggingMiddleware` now takes
  `redact_arg_keys` and the orchestrator passes `policy.sensitive_arg_keys`.
- **The argument digest stored beside the redacted arguments is a verifier of the redacted value** —
  it is an HMAC under a platform key held by reference (`TOOLEXEC_ARGS_HMAC_KEY_REF`, resolved once
  at startup, absent ⇒ fail to start), prefixed with its key id, so a reader of chain history cannot
  brute-force a low-entropy order id or phone number out of it; the key's rotation cost is one claim
  window of possible redialled duplicates, stated in `docs/setup.md`.
- **The idempotency header exports a value derived from the same secret to an endpoint the tenant
  admin controls, which would otherwise be an HMAC oracle over that tenant's own redacted
  arguments** — the stored and exported values are domain-separated (`_derive("claim", …)` vs
  `_derive("idem", …)`) through a single derivation function, so the exported value is never
  comparable to the stored one and the two still cannot drift apart.
- **The side-effect claim window (24h) is a policy judgement, and a wrong value is either a duplicate
  mutation or a blocked legitimate repeat** — it is bounded on both sides by construction: only
  byte-identical resolved arguments are ever gated (any real difference changes the HMAC), and the
  window is an operator knob (`TOOLEXEC_SIDE_EFFECT_CLAIM_TTL`) rather than a constant, so a tenant
  whose business genuinely repeats identical mutations within a day is an operator change, not a
  code change.
- **A tenant (or a compromised service account) can turn the platform's egress into an authenticated
  flood relay against a third party, and the platform owns the bill and the abuse complaint** —
  `chain_budget_ms` is clamped server-side, runs are capped per `(tenant_id, agent_id)` for
  concurrency and per minute, and each step's response read is capped at
  `TOOLEXEC_MAX_RESPONSE_BYTES`; the caps are per-replica, which bounds rather than eliminates the
  ceiling and is stated as such.
- **`success_template` is spoken verbatim, so it is an admin-authored path into the caller's ear and
  the transcript** — placeholders are whitelisted against the API's own non-sensitive response paths
  at registration (and re-validated when `sensitive_response_paths` changes), interpolated only from
  the already-redacted projection, control-stripped and length-capped, and an unresolved placeholder
  suppresses `deterministic_response` entirely rather than speaking a literal `{{…}}`.
- **A four-step chain can exceed the conversational patience window even inside its budget** — the
  chain is bounded by the existing per-tool `timeout_ms` (default 20000 for `execute_api`), each step
  by `min(api.timeout_ms, remaining)`, and the UI shows the worst-case total while editing so an
  admin sees a 4×6s chain cannot fit a 20s budget.
- **`chain_levels` is denormalized and could drift from the graph** — it is only ever written in the
  same transaction as the params it summarizes, under a per-tenant advisory lock, and
  `graph.resolve_order` recomputes the real depth at runtime as an independent backstop, so drift
  degrades to a clean runtime refusal rather than an unbounded chain.
- **Revocation lag: `deps.py` resolves identity purely from the JWT, so a demoted or soft-deleted
  admin keeps write access to the API registry for up to the token TTL (~12h)** (lesson 27) — this
  design adds no new long-lived grant and does not fix the platform-wide issue, but the *execution*
  path re-reads `custom_apis`/`agent_custom_apis` (both `deleted_at`/`enabled` filtered) on every
  turn, so disabling an API takes effect within the 30s policy cache regardless of any token.
- **Adding `engine='toolexec'` to `tool_provider_configs` without an `api_key_ref` widens that
  table's "always credentialed" invariant** — the exemption is a single engine-name condition in
  `routers/tool_provider_configs.py` and `_make_toolexec` is the only factory that accepts a null
  key; every tenant-credentialed engine still fails loudly without one.
- **A new service means a new operational surface (port, health check, service account, launcher
  block)** — justified by the PRD's settled constraint (outbound third-party HTTP must not occupy
  real-time conversation workers, and tenant API credentials must not live in the conversation
  process); it reuses the Knowledge Service's app/db/audit/auth wiring wholesale and adds **no new
  dependency** (`httpx`, `asyncpg`, `fastapi` are all already in `requirements.txt`).
- **The `inputs` free-form object lets the LLM send junk or extra keys** — unknown keys are dropped
  and missing required leaves return `INVALID_ARGUMENT` + `missing_fields` before any HTTP call, the
  same loop `book_appointment` already runs the model through.
- **An in-process OAuth2 token cache multiplies token fetches by replica count** — bounded and
  intentional: tokens are short-lived derived artifacts that must not be persisted, and the miss cost
  is one extra HTTPS call per replica per expiry window.

## Test plan

**Unit — `services/toolexec/tests/test_graph.py`** (no DB, no network): post-order for a 4-level
chain; diamond dedupe (shared upstream appears once); `depth_limit_exceeded` at 5 levels and at a
per-agent override of 2; `dependency_cycle`; `extract()` on a missing path returns a miss, not an
exception.

**Unit — `services/toolexec/tests/test_auth_schemes.py`**: API key placed in header vs query;
static bearer; OAuth2 fetches once, reuses within expiry, re-fetches after; unresolvable ref raises
before any request is constructed (asserted by a transport that fails the test if called), and the
ref string appears in neither the exception nor the caplog output. **Namespace containment
(finding 1)**, each asserted to raise `credential_ref_outside_tenant_namespace` *and* to leave a
sentinel `os.environ["JWT_SECRET"]` / on-disk platform secret unread: `env:JWT_SECRET`,
`env:SECRET_ENCRYPTION_KEY`, `env:TENANT_<other tenant's hex>_TOKEN`, `k8s:../../etc/passwd`,
`k8s:tenants/<other tenant_id>/token`, and a symlink inside
`TOOLEXEC_TENANT_SECRET_ROOT/tenants/<id>/` pointing outside it (the `.resolve()` case — this one
fails if only the regex is implemented); `env:TENANT_<own hex>_TOKEN` and any `enc:` ref resolve.

**Unit — `services/toolexec/tests/test_endpoint_validation.py`** (finding 6): reject `http://` for a
non-allow-listed host, `http://169.254.169.254/…`, `https://[::1]/`, `https://[::ffff:127.0.0.1]/`,
`https://2130706433/` (decimal), `https://0x7f.1/`, `https://100.64.0.1/`, a host with two A records
where only one is private (asserts the whole URL is rejected, not "the good one" dialed), and a URL
with userinfo; accept a normal public host. Assert the pinned transport is constructed with
`verify` untouched and `follow_redirects=False`, and that a `302` response is returned as the step's
result rather than followed.

**Integration — `services/toolexec/tests/test_custom_apis.py`** (real Postgres, as the existing
suites use): reject an upstream id belonging to another tenant and a nonexistent one, each naming the
dependency (AC 17); accept the same API `name` in two tenants and confirm neither blocks the other
(lesson 3); reject a 5-level chain at save time and reject an *edit* to a mid-graph API that pushes a
dependent to 5; reject a cycle; soft delete refused while a dependent is live; `auth_config` with a
literal secret rejected; `invalid_endpoint_url` for `http://`, for `http://169.254.169.254/…`, and
for a hostname resolving to loopback.

**Integration — `services/toolexec/tests/test_chain_execution.py`** (`httpx.MockTransport` +
Postgres) — the cases that actually matter:
1. 4-level chain succeeds; `api_chain_steps` shows four rows in declared order with
   `argument_sources` attributing each param to `literal` / `caller` / `upstream:$.path`, and the
   response's `data` reflects the final call (AC 3, 4, 14).
2. Level-2 of a 4-level chain returns 500 ⇒ `chain_status="partial"`, `completed_steps=["a"]`,
   `failed_step.api_name=="b"`, levels 3–4 recorded `skipped`, and `data == {}` so there is no
   success field to fabricate from (AC 5, 6).
3. Level-2 hangs past its step timeout ⇒ run `timeout`, exactly one outbound call made after the
   hanging one is abandoned (asserted on the mock transport's call log), no retry (AC 7).
4. Re-POST with the same run `idempotency_key` returns the recorded outcome with **zero** new
   transport calls; re-POST under a *new* `tool_call_id` after a successful side-effecting step is
   refused with `side_effecting_step_already_completed` and zero new calls — asserted for a new
   `tool_call_id` in the **same** session *and* for one in a **new** `session_id` (the redial), since
   the claim is deliberately not session-scoped (AC 15).
4b. **Concurrency and claim scope (findings 5, 7):** two `asyncio.gather`-ed executes of the same
   side-effecting chain **in one session**, against a transport whose handler blocks until both have
   entered, result in **exactly one** outbound call to that step (the transport counts them, so the
   test fails under a SELECT-then-POST design) and one `side_effecting_step_already_completed`. Then
   the scope and window cases, all asserted on the transport's call count:
   *(i)* the same mutation replayed under a **different `session_id`** (the hang-up-and-redial case)
   is still refused — this fails if the claim is scoped by session;
   *(ii)* with `TOOLEXEC_SIDE_EFFECT_CLAIM_TTL` at its **deployed default** and the claim row's
   `claimed_at` back-dated past it, the same mutation **is** allowed and takes over the claim in one
   statement, and the earlier `api_chain_steps` row still exists (history is not overwritten) — the
   test moves the row's timestamp rather than shortening the TTL, so it exercises the shipped value
   (lesson 25);
   *(iii)* one differing argument yields a different `arguments_hash` and is never gated;
   *(iv)* a 422 releases the claim and an immediate retry does call again, while a timeout and a 500
   both keep it;
   *(v)* the value sent in `custom_apis.idempotency_header` is **identical across two different
   turns/sessions** for the same resolved arguments (this fails under a `tool_call_id`-derived key)
   and differs when an argument differs;
   *(vi)* **the hash is keyed and domain-separated (findings 7 + low 4)** — four assertions, each of
   which a bare `sha256(canonical_json(...))` implementation would fail: the stored value `!=`
   `sha256` of the canonical args; the stored value differs for the same arguments under a second
   `TOOLEXEC_ARGS_HMAC_KEY_REF` (two fixtures, same args, different keys ⇒ different hashes); the
   stored value `!=` the value sent in `custom_apis.idempotency_header` for the same call (the
   `claim`/`idem` tag separation — this fails under a single derivation); and changing
   `TOOLEXEC_ARGS_HMAC_KEY_ID` changes the stored prefix while the same key still dedupes;
   *(vii)* inserting a side-effecting `api_chain_steps` row with a NULL `arguments_hash` raises the
   `api_chain_steps_side_effect_keyed` CHECK (the NULL-bypass guard — this is a DB-level assertion,
   so it fails if the constraint is dropped).
4c. **Injection (finding 4):** a `caller` value of `"a\r\nAuthorization: Bearer x"` on a `header`
   param yields `invalid_argument` / `illegal_header_value` with no request sent and the value absent
   from the error; a `path` value of `"../../admin/refund"` and of `"x?admin=1"` produce a request
   whose path is a single percent-encoded segment under the registered path (asserted on the URL the
   transport received).
5. Two APIs with no declared edge, invoked in one turn, each produce a single-step run (AC 13).
6. A param marked `sensitive` and a path in `sensitive_response_paths` appear as `"[redacted]"` in
   both the step rows and the response, while `api_name`/`status`/`argument_sources` remain visible
   (AC 14 + redaction constraint).
7. **Admission (finding 10) — each assertion names what would make it fail (lesson 12):**
   *(a)* `chain_budget_ms=3600000` in the body: the step's effective deadline is
   `TOOLEXEC_MAX_CHAIN_BUDGET_MS`, not the body's value — asserted on the deadline the transport
   observes, so it fails if the clamp is absent (a log-line assertion would not);
   *(b)* `TOOLEXEC_MAX_CONCURRENT_RUNS_PER_AGENT + 1` runs gathered against a blocking transport: the
   last returns `rate_limited` with **no `api_chain_runs` row and zero transport calls**, while a
   *different* agent in the same tenant is admitted concurrently — so the test fails both if the cap
   is missing and if it is applied per tenant instead of per `(tenant_id, agent_id)`;
   *(c)* the per-minute cap tripped at the deployed value (the test drives real calls, it does not
   lower the constant — lesson 25), and the slot is proven released by admitting a further run after
   the first batch completes, which fails if the `finally` release is dropped;
   *(d)* a transport that streams more than `TOOLEXEC_MAX_RESPONSE_BYTES` and counts the bytes
   actually pulled: result is `failed` / `response_too_large` and the pulled count is at most the
   cap plus one chunk — it fails if the body is read to completion first.
8. **`success_template` (finding 9) — the validator must be exercised, not assumed:** registration
   is rejected with `invalid_success_template` naming the placeholder for *(a)* a path listed in
   `sensitive_response_paths`, *(b)* a path that is a descendant of one (`$.customer.ssn.last4` under
   `$.customer.ssn`), *(c)* a path that is an ancestor of one, and *(d)* a placeholder naming a
   `sensitive` param; a template with only non-sensitive paths saves and renders its values.
   *(e)* **PATCH-after-the-fact:** with that template already stored, `PATCH`ing the same path into
   `sensitive_response_paths` is rejected — this is the case that fails if re-validation on update
   was never implemented. *(f)* At runtime a path redacted by `redaction.py` renders `[redacted]`,
   not the value, and *(g)* an unresolved placeholder leaves `deterministic_response` as `None`, with
   the spoken text asserted to contain no `{{`.

*What would make these fail:* case 2 fails if the executor returns any success-shaped `data` on a
partial chain; case 3 fails if the abandoned step's successor is still called; case 4 fails if the
mock transport records any call at all.

**Integration — `services/toolexec/tests/test_routes_auth.py`**: every route 401s with no header;
`viewer` 403s on all writes; a tenant-scoped admin gets **404 with byte-identical detail** for
another tenant's `custom_api_id` and for a random UUID (lesson 2 — this test fails if either the
status code or the message differs). **AC 10 write-time:** a tenant-A admin `PUT`ing tenant B's `custom_api_id` onto their
own agent gets 404 **and `agent_custom_apis` gains no row** (the test asserts the row count, so it
fails if the guard is only a runtime-resolution filter); same for `DELETE`, for a tenant-A admin
targeting tenant B's `agent_id` with their own API, and for the `GET` list route; the 404 detail
text is byte-identical to the one a random UUID produces (lesson 2). **Chain history (finding 2):**
a tenant-A admin `GET`ing a tenant-B `session_id` gets 404 with detail byte-identical to an unknown
session_id's, and the platform-scoped caller gets the rows — the test fails if the tenant predicate
is missing, because the tenant-A caller would receive B's steps. **Execute identity (finding 3):** a
`role="viewer"`, `tenant_id=NULL` service account whose email is *not* in
`TOOLEXEC_EXECUTE_SUBJECTS` gets 403 on `/internal/chains/execute` (this is the vobiz/SDK
service-account case, so the test fails under an `is_platform_scoped`-only gate); a human
`superadmin` also gets 403; the allow-listed Conversation account succeeds; and a body pairing
tenant A's `tenant_id` with tenant B's `agent_id` returns `api_not_enabled_for_agent` with zero
transport calls and **no `api_chain_runs` row**.

**Unit — `services/conversation/tests/test_execute_api_tool.py`**: with 7 custom APIs enabled the
schema list contains exactly one `execute_api` entry whose `api_name.enum` has 7 values, *plus* the
unchanged legacy entries for the tools that agent also has enabled — the assertion counts entries by
name, so it fails if a per-API entry is ever added (AC 1); with zero enabled APIs, no `execute_api`
entry is offered and the resolver issues no second query (asserted on a recording pool, so it fails
if the query becomes unconditional — AC 9); `chain_status` → `ToolStatus` mapping including
`partial` → `FAILED`; a `cancel_event` set mid-call yields `FAILED/"cancelled"` and the abandoned
task's result is never folded into history (AC 8). **Logging (finding 8):** three assertions with
`caplog` at INFO, driving the real `build_default_chain` (not a hand-built middleware), so the test
fails if the `redact_arg_keys` argument is never passed at the orchestrator's call site:
*(a)* with a policy whose `sensitive_arg_keys == {"national_id"}`, the sentinel value appears in
**no** record's message or args — asserted over `caplog.records` rather than `caplog.text`, so a
`%r`-deferred value cannot hide from it; *(b)* `api_name` and the non-sensitive inputs *do* still
appear, so the test also fails if everything is redacted wholesale; *(c)* with the field's default
empty `frozenset()` (every legacy tool), the log line is byte-identical to today's.
Additionally `test_policy_resolver.py` asserts `sensitive_arg_keys == {"national_id"}` for an agent
with one API carrying that `sensitive` param and `frozenset()` for an agent with none — the unit
that fails if `p.sensitive` never made it into the SELECT.

**Tripwire — `services/conversation/tests/test_execute_api_tool.py`**: assert
`{d.name for d in ToolRegistry().all()} == {"book_appointment","cancel_appointment",
"reschedule_appointment","send_sms","execute_api"}`, so any future registry addition must be
consciously accounted for against AC 1.

**Manual (lesson 23, required before sign-off):** run the stack via `scripts/start_local.sh`, drive
the Knowledge Base → APIs sub-tab in a browser as both `admin` and `viewer`, register a real 3-level
chain against a local stub, place a webcall, and confirm the agent narrates level-1 lookup + level-3
action, then confirm the failure path speaks no success. Per lesson 21, verify the APIs sub-tab still
renders its list for a `viewer` whose sibling write-scoped fetches fail.
- **Chain steps run strictly sequentially, even where the DAG permits parallelism** — accepted
  limitation for v1. `resolve_order` returns a flat post-order list, so a diamond's two independent
  upstreams are dialed one after the other and each burns from the same shared `timeout_ms` budget;
  wall-clock is the sum of every call rather than the sum of the slowest call per level. The DAG
  data model already supports level batching (Kahn-style waves executed with `asyncio.gather`), and
  the change is localized to `resolve_order`'s return type plus the executor loop — deliberately
  deferred because no tenant topology exists yet to show fan-out is common, a linear chain gains
  nothing from it, and in-flight concurrency lands on the partial-failure/idempotency bookkeeping
  where a bug means a duplicated side effect. Revisit if QA or production shows chains timing out;
  the interim mitigation is the Admin UI showing worst-case chain total while an admin edits.
