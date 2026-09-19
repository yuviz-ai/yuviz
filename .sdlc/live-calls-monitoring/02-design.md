# Design: Live Calls Monitoring

## Approach
One tenant-scoped read endpoint (`GET /live-calls`) serves the whole screen — KPI row, live rows,
per-call intervention badge — in a single request built from two statements on one pooled
connection, polled client-side every 5s. No server-side cache: at 5s per session a partial index
over `ended_at IS NULL` rows makes the poll cheaper than the staleness a cache would add on top of
the interval (AC5 gives the whole budget to the poll, so a 2s TTL would put the worst case at 7s).
The "stage" KPIs (AI-only / waiting for human / human connected) have no source in the schema
today — `close_reason` is written only at hangup — so this adds one `calls.live_stage` column
written by the Conversation Service at its four existing transfer hooks; that is the smallest
honest way to answer AC4/AC13's stage split rather than deriving it from end-of-call data that
does not exist yet mid-call. Listen/Barge is a request recorder: authorize → re-validate authority
against the DB → write `live_call_interventions` + `audit_log` in one transaction → return
`outcome: "unavailable"`, which the next poll surfaces to every other operator of that tenant as a
badge (this, not a fabricated "human connected", is how AC14 is met — see Scope decisions).
Authorization does **not** go through `get_current_user`: `CONSOLE_ROLES` excludes `supervisor`
(`services/config/deps.py:36`), and adding it there would grant supervisors every route that is
guarded only by "is authenticated" (lesson 4). Instead a new `require_live_calls_operator`
dependency, built on `get_authenticated_user`, admits exactly `superadmin | admin | supervisor` on
exactly these two routes.

## Scope decisions (explicit, per PRD constraints)
- **Sentiment indicator is OUT of the first build.** Nothing sentiment-like exists:
  `transcript_entries` has `intent`, `entities`, `tool_calls`, `metadata` and latency columns and no
  sentiment column (`database/schema.sql`), and `services/conversation/guardrails.py:14` states in
  so many words that there is "no sentiment scoring". A keyword heuristic invented here would render
  a confident-looking indicator beside real telemetry that operators would act on, so the column is
  not shipped. Extension point: `live_calls.py`'s snippet lateral already selects the latest
  `transcript_entries` row; when Conversation begins writing `metadata->>'sentiment'`, the field is a
  one-line addition to that SELECT and one column in the table. AC4's sentiment clause is therefore
  knowingly deferred, not silently assumed.
- **AC14 is met by an intervention badge, not by a stage flip.** Requesting Barge does not connect a
  human (no audio path exists), so flipping `live_stage` to `human_connected` would be a lie in the
  KPI row. What propagates within one 5s cycle to every other operator of that tenant is
  `intervention: {action, outcome, requested_by_email, requested_at}` on the call's row plus an
  `interventions_pending` KPI. AC14's literal "reflects that a human is now connected" is
  unsatisfiable in a build with no audio join; it becomes satisfiable, unchanged in shape, when the
  telephony work lands and sets `live_stage='human_connected'`.
- **Transcript snippet is withheld from `supervisor`** (AC15). `GET /calls/{id}/transcript` is
  reachable today by `superadmin/admin/viewer` only (`get_current_user`), so serving a snippet to
  supervisors would make this screen a wider transcript surface than the existing endpoint. Rows for
  supervisors carry `transcript_snippet: null, transcript_withheld: true` and the UI renders "—".
  The inclusion test is `deps.TRANSCRIPT_ROLES` against the role `fresh_authority()` read from the
  database, not the role in the token: a demotion out of `admin` stops the snippet within the 60s
  memo TTL instead of at token expiry (~12h).
- **Supervisors get a console landing page.** They currently log in to `/no-access`
  (`admin-ui/app/login/page.tsx:74`). Granting API access without fixing where the role lands is
  lesson 22, so `/live-calls` becomes the supervisor's post-login destination and the only nav item
  it sees; every other admin route still redirects it to `/no-access`.

## Changes
| File | Change | Why |
| --- | --- | --- |
| `database/schema.sql` | Append: `tenants.max_concurrent_calls`, `calls.live_stage`, `live_call_interventions` table, partial live index, transcript `(session_id, turn_number DESC)` index. All `IF NOT EXISTS`/`ADD COLUMN IF NOT EXISTS`, no destructive DDL, so nothing needs a `DO $$` guard (lessons 10/13). | Schema for the utilization KPI, the stage split, and the intervention record. |
| `services/config/live_calls.py` (new) | `get_live_calls()`, `request_intervention()`, `_mask_msisdn()`, `LIVE_STAGES`. | `calls.py`'s docstring commits it to read-only reporting; the intervention writer would falsify that. Keeps the poll query and its writer in one module. |
| `services/config/routers/live_calls.py` (new) | `GET /live-calls`, `POST /live-calls/{session_id}/interventions`, `_resolve_scope()`. | Its own auth model (`require_live_calls_operator`) — mixing it into `routers/calls.py` would leave two gates in one file and blur the console-gate tripwire. |
| `services/config/deps.py` | Add `LIVE_CALLS_ROLES`, `TRANSCRIPT_ROLES`, `AUTHORITY_MEMO_TTL_S` + `require_live_calls_operator`; add `assert_current_authority()` (DB re-read on every intervention) and `fresh_authority()` (the same read, memoized 60s per `(user_id, scope_key)`, returning the row's current role/tenant). | The role grant belongs beside `require_role` in the module every service imports, so `tests/test_console_gate.py` covers it (lesson 9). The two helpers close lesson 27 / AC9 for every privileged decision this feature makes: granting Listen/Barge, a platform-scoped account's tenant selection, and transcript-snippet inclusion (AC15) — none of which may rest on a JWT claim. |
| `services/config/app.py` | Mount `live_calls.router`. | Same pattern as the other routers. |
| `services/config/schemas.py` | `TenantUpdate.max_concurrent_calls: int \| None = Field(default=None, ge=1, le=10_000)` (None = field absent, per `exclude_unset`; clearing the cap back to NULL is not offered); new `TenantConcurrencyUpdate`; new `InterventionRequest`. | Bounds validated at config time, matching `transfer_timeout_ms`'s stance (`schemas.py:59`). |
| `services/config/tenants.py` | Add `max_concurrent_calls` to `_UPDATABLE_FIELDS`. | `update_tenant()` rejects unknown fields; it already audits and invalidates `_cache_key(slug)`, which is what makes AC16 land on the next poll rather than after a 60s TTL. |
| `services/config/routers/tenants.py` | New `PATCH /tenants/{tenant_id}/concurrency`, `require_role("superadmin","admin")` + explicit own-tenant check for `admin`. | AC16 needs a tenant admin to edit this. Widening the existing `PATCH /tenants/{tenant_id}` (superadmin-only) would hand admins `name`, `region`, VAD and default-provider ids as well. |
| `services/conversation/transcript_builder.py` | New `record_live_stage(session_id, stage)` → `_spawn(...)` → `UPDATE calls SET live_stage=$2 WHERE session_id=$1`. | Reuses the existing fire-and-forget ordered-write path (`_spawn`, `record_workflow_outcome`); never blocks the call. |
| `services/conversation/session.py` | Call `record_live_stage` from `on_transfer_initiated` (`waiting_for_human`), `on_transfer_completed` (`human_connected`), `on_transfer_failed` / `on_transfer_cancelled` (`ai`). | The only four places transfer state is known mid-call (lines 316/336/372/429). |
| `admin-ui/app/live-calls/page.tsx` (new) | KPI row, live table, pause/resume, CSV export, Listen/Barge buttons, superadmin tenant guard. | The screen. |
| `admin-ui/lib/api.ts` | `LiveCallsSnapshot`/`LiveCall` types, `getLiveCalls(tenantSlug?)`, `requestIntervention()`, `updateTenantConcurrency()`, `max_concurrent_calls` on `Tenant`. | Single typed client, per the file's own header. |
| `admin-ui/components/AppShell.tsx` | Export `ACTIVE_TENANT_STORAGE_KEY`; add `{ href: "/live-calls", label: "Live Calls" }` to `CALLING_ITEMS`; in the auth guard, allow `supervisor` on `/live-calls` and render only that nav item for it. | Lesson 22: the role must land somewhere it can use. |
| `admin-ui/app/login/page.tsx` | `supervisor` → `/live-calls`; other non-console roles unchanged → `/no-access`. | Same. |
| `admin-ui/app/no-access/page.tsx` | Copy: it is now the `agent`-role page; drop "supervisor" from both message strings. | The screen would otherwise tell a supervisor it has no access while it does. |
| `admin-ui/app/tenants/page.tsx` | `max_concurrent_calls` number input in the tenant edit form → `updateTenantConcurrency()`. | AC16's admin-editable path. |
| `services/config/tests/test_live_calls.py` (new) | See Test plan. | |
| `services/config/tests/test_console_gate.py` | Update `test_exactly_two_routes_depend_on_get_authenticated_user` into a named allowlist that also asserts each live-calls route's role set; keep `supervisor` 403 on `/users`, `/calls/{id}`, `/audit-log`. | That tripwire fails the moment a new `get_authenticated_user` route appears — by design. It must be widened deliberately, and stay able to trip (lesson 12). |

## Data
Appended to `database/schema.sql` (applied by plain `psql -f`; every statement is idempotent and
non-destructive, so no statement depends on a prior one succeeding — lesson 13):

```sql
-- Per-tenant channel cap — the only source for the Live Calls utilization KPI.
-- INT + CHECK mirror campaigns.max_concurrent_calls (schema.sql:555), but the
-- column is NULLABLE with NO DEFAULT, deliberately unlike campaigns': a
-- platform-wide DEFAULT 1 would BE the inferred global cap AC13 forbids, just
-- moved from the query into the column. NULL means "not configured yet" and is
-- rendered as a setup prompt, never as a number (see get_live_calls below).
-- No backfill: there is no honest value to backfill from — calls history has no
-- provisioned-channel record, so a computed "observed peak" would be the same
-- fabricated cap under a busier name.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS max_concurrent_calls INT;
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_max_concurrent_calls_check;
ALTER TABLE tenants ADD  CONSTRAINT tenants_max_concurrent_calls_check
    CHECK (max_concurrent_calls IS NULL OR max_concurrent_calls >= 1);

-- Mid-call stage. NULL = never transferred; readers COALESCE to 'ai' so no
-- backfill is needed and no existing writer has to change (lesson 32: every
-- write path, including Conversation's abort paths, already satisfies this).
ALTER TABLE calls ADD COLUMN IF NOT EXISTS live_stage TEXT;
ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_live_stage_check;
ALTER TABLE calls ADD  CONSTRAINT calls_live_stage_check
    CHECK (live_stage IS NULL OR live_stage IN ('ai', 'waiting_for_human', 'human_connected'));

-- Serves both the KPI aggregate and the row list: only live rows are indexed,
-- so the index stays bounded by concurrent calls, not by call history.
CREATE INDEX IF NOT EXISTS idx_calls_live_tenant
    ON calls (tenant_id, started_at DESC) WHERE ended_at IS NULL;

-- Latest-turn snippet lookup per live row. idx_transcript_entries_session
-- (session_id only) would still sort every turn of the call.
CREATE INDEX IF NOT EXISTS idx_transcript_entries_session_turn
    ON transcript_entries (session_id, turn_number DESC);

-- Listen/Barge requests. tenant_id is the slug (matching calls.tenant_id's own
-- "slug reference, not a hard FK" note), and session_id deliberately has NO FK:
-- AC11 requires recording a denial, and a denial's requested session may not
-- exist at all — an FK would turn the audit requirement into a 500 on exactly
-- the path it exists for.
CREATE TABLE IF NOT EXISTS live_call_interventions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     TEXT NOT NULL,
    session_id    TEXT NOT NULL CHECK (length(session_id) <= 200),
    action        TEXT NOT NULL CHECK (action IN ('listen', 'barge')),
    outcome       TEXT NOT NULL CHECK (outcome IN ('granted', 'denied', 'unavailable')),
    detail        TEXT,
    user_id       UUID REFERENCES users(id),
    user_email    TEXT NOT NULL,
    ip_address    INET,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_live_call_interventions_lookup
    ON live_call_interventions (tenant_id, session_id, created_at DESC);
```

`audit_log` is **unchanged**: `entity_id` is `UUID NOT NULL` and `action` is
`CHECK (action IN ('created','updated','deleted'))`, so an intervention audits as
`entity_type='live_call_intervention'`, `entity_id=<resolved tenant UUID>`, `action='created'`,
`new_value={"session_id":…,"requested_action":…,"outcome":…,"detail":…}`. Written via
`audit.write_audit(conn, …)` inside the same transaction as the `live_call_interventions` INSERT,
per that module's "commit or roll back together" doctrine.

## Interfaces

### `services/config/deps.py`
```python
LIVE_CALLS_ROLES = frozenset({"superadmin", "admin", "supervisor"})

def require_live_calls_operator():   # Depends(get_authenticated_user) → CurrentUser | 401 | 403
    """403 for viewer/agent and any future role. Built on get_authenticated_user,
    NOT get_current_user: supervisor is deliberately outside CONSOLE_ROLES and
    must stay outside it (lesson 4)."""

async def assert_current_authority(user: CurrentUser) -> CurrentUser:
    """Re-read users WHERE id=$1 AND deleted_at IS NULL (users_service.get_user_by_id).
    403 if the row is gone, if role has changed out of LIVE_CALLS_ROLES, or if
    tenant_id no longer matches the token's. Closes lesson 27 / AC9."""

AUTHORITY_MEMO_TTL_S = 60
TRANSCRIPT_ROLES = frozenset({"superadmin", "admin"})   # matches who reaches
                                                        # GET /calls/{id}/transcript today

async def fresh_authority(
    app_state, user: CurrentUser, scope_key: str, *, ttl_s: int = AUTHORITY_MEMO_TTL_S,
) -> CurrentUser:
    """assert_current_authority()'s database read (users WHERE id=$1 AND deleted_at
    IS NULL), returning a CurrentUser rebuilt from the ROW's current role and
    tenant_id rather than from the token's claims — memoized per
    (user.id, scope_key) for ttl_s in an in-process dict on app.state (same
    placement convention as app.state.invite_throttle, services/config/app.py:250).

    Every privileged decision GET /live-calls makes reads off this returned
    identity, never off the JWT: (a) a platform-scoped account's choice of WHICH
    tenant to view — the selection-time re-validation the PRD's Constraints demand
    alongside granting Listen/Barge; and (b) whether the transcript snippet is
    included (role in TRANSCRIPT_ROLES — AC15), so an admin demoted to
    supervisor/agent stops receiving transcript text without waiting out the
    ~12h token.

    scope_key is derived from the REQUEST only — the `tenant_slug` query parameter,
    or "self" when it is absent — never from the caller's identity. That ordering is
    deliberate: the memo has to be readable BEFORE any branch decision, because the
    branch decision is itself made from the row this call returns. A tenant SWITCH
    therefore always re-reads rather than inheriting another selection's validation.

    TTL is 60s, not 5s: it is a cached authorization DECISION, so it is set as low
    as the poll cost allows and no lower — at 60s an open operator session adds one
    users read per minute (one per twelve 5s polls) instead of one per poll, which
    is what keeps the steady-state poll at two statements. Polls inside the window
    do NOT hit the database. The exposure after a demotion or deactivation is
    therefore bounded by this 60s TTL, not by the token's ~12h life (lesson 27), and
    the memo holds only a timestamp and the row-derived role/tenant — never a grant
    the database did not confirm (lesson 16)."""
```

### `services/config/routers/live_calls.py`
```python
router = APIRouter(prefix="/live-calls", tags=["live-calls"])

async def _resolve_scope(
    request, user, tenant_slug: str | None,
) -> tuple[str, uuid.UUID, CurrentUser]:
    """Returns (slug, tenant_uuid, effective_user) or raises.

    STEP 1, before any other decision:
        scope_key       = tenant_slug or "self"          # from the request, not the actor
        effective_user  = await deps.fresh_authority(request.app.state, user, scope_key)
    403 if the row is gone or its role has left LIVE_CALLS_ROLES.

    STEP 2: BRANCH ON effective_user.tenant_id — never on user.tenant_id. Branch
    selection and the privilege check inside the branch therefore derive from the
    SAME fresh row and cannot disagree. This is the load-bearing detail: this
    codebase confines by scope, not by role (users.tenant_id is mutable —
    users.py:194's update_user — and users.py:76-90 narrows a superadmin by
    re-tenanting them), so trusting the token's NULL/non-NULL claim to pick the
    branch would route a just-re-tenanted superadmin down the platform-scoped path
    and hand it every tenant's calls and transcripts for the token's remaining ~12h
    — the same gap the transcript decision closes, one level up. Once a DB read is
    already paid for at this exact point, no claim about scope is consulted again.
    After this line `user` (the token) is dead to the request: the only permitted
    reads of it are the audit row's actor id/email, which record WHO called, not
    what they may do. Two separate predicates (lesson 24):
      • effective_user.tenant_id is NOT NULL → tenant-scoped, whatever the token
        said: slug = that tenant's slug via tenants_service.get_tenant_by_id; a
        supplied tenant_slug that differs → 404 "tenant not found" — identical
        status/body/path to a nonexistent slug. A token that claimed NULL lands here
        the moment the row is re-tenanted, and is confined to the row's tenant.
      • effective_user.tenant_id IS NULL → platform-scoped, whatever the token said:
        AUTHORITY first — effective_user.role must be "superadmin" (403 otherwise, so
        a NULL-tenant viewer service account, and any account demoted after login,
        cannot read any tenant's live calls); then tenant_slug is REQUIRED (400
        "tenant_slug is required", a fixed response that reveals nothing about any
        tenant); then the tenant is resolved via the Redis-cached
        tenants_service.get_tenant. Because scope_key is the requested slug, the
        re-read fires on the first poll of a selection and on every switch;
        subsequent polls of the SAME already-validated selection do not re-read
        users, which is what keeps the 5s poll at two statements."""

@router.get("")
async def get_live_calls(
    request: Request,                    # app.state, for the fresh-authority memo
    tenant_slug: str | None = Query(default=None),
    user: CurrentUser = Depends(require_live_calls_operator()),
) -> LiveCallsSnapshot
```
Response:
```json
{
  "tenant_slug": "acme", "generated_at": "…", "refresh_seconds": 5, "truncated": false,
  "kpis": {"live_calls": 7, "ai_only": 5, "waiting_for_human": 1, "human_connected": 1,
           "interventions_pending": 1, "max_concurrent_calls": 20, "utilization_pct": 35.0},
  // max_concurrent_calls: null and utilization_pct: null when the cap is unset —
  // the server never substitutes a fallback (AC13), the UI shows a setup prompt.
  "items": [{"session_id": "…", "agent_name": "Reception", "direction": "inbound",
             "caller_number_masked": "+1415•••7788", "called_number_masked": "+1415•••0100",
             "live_stage": "ai", "started_at": "…", "elapsed_ms": 42000,
             "transcript_snippet": "…", "transcript_withheld": false,
             "intervention": {"action": "barge", "outcome": "unavailable",
                              "requested_by_email": "sup@acme.com", "requested_at": "…"}}]
}
```
```python
@router.post("/{session_id}/interventions", status_code=202)
async def request_intervention(
    session_id: str,
    body: InterventionRequest,           # {"action": "listen"|"barge", "tenant_slug": str|None}
    request: Request,                    # client IP for the audit row
    user: CurrentUser = Depends(require_live_calls_operator()),
)
```
Order of operations, per route (lesson 30 — these are the only two new routes and each names its
own guard): `require_live_calls_operator` (401/403 on the token's role — a coarse gate that can
only reject, never widen) → `assert_current_authority` (403 on a demoted, deleted or re-tenanted
actor; it compares the fresh row's `tenant_id` to the token's claim) → `_resolve_scope`, whose
returned `effective_user` — not the token — is what is passed to
`live_calls.request_intervention(...)` for the scope predicate and the audit row's actor fields. Success →
`202 {"outcome": "unavailable", "detail": "audio join not yet available", "requested_at": …}`.
Foreign-tenant or nonexistent `session_id`, or `len(session_id) > 200` → one `audit_log` row
(`outcome: "denied"`, `detail: "not_found_or_out_of_scope"`, session id truncated to 200 chars)
and `404 {"detail": "call not found"}` — one identical code path, one identical body, one
identical query for both cases, so there is no existence oracle in status, text or latency
(lesson 2). No `live_call_interventions` row on that path: the table drives in-tenant badges, and
letting an arbitrary string insert into it would make it a growth vector.

### `services/config/live_calls.py`
```python
LIVE_STAGES = ("ai", "waiting_for_human", "human_connected")
MAX_LIVE_ROWS = 200

def _mask_msisdn(number: str | None) -> str | None:
    """'+14155557788' → '+1415•••7788' (country/area prefix + last 4). No masking
    convention exists in this repo today (grepped admin-ui/ and services/config/ —
    the only 'mask' references are provider_configs.py's secret notes), so this is
    the one definition, applied SERVER-SIDE so the raw MSISDN never leaves the API."""

async def get_live_calls(tenant_slug: str, *, include_transcript: bool) -> dict
    # include_transcript = effective_user.role in deps.TRANSCRIPT_ROLES, where
    # effective_user is _resolve_scope's fresh_authority() result — never the JWT
    # role (AC15). Withheld rows are built with transcript_snippet=None and
    # transcript_withheld=True; the snippet LATERAL is omitted from the SQL
    # entirely in that case, so the text is never fetched, not merely dropped
    # before serialization.
    # One pool.acquire(); statement 1 = KPI aggregate; statement 2 = rows + LATERAL
    # latest transcript_entries turn + LATERAL latest live_call_interventions row.
    # Both carry `tenant_id = $1` explicitly in their own WHERE (PRD constraint /
    # lesson 12: not relying on a JOIN's incidental exclusion). ended_at IS NULL is
    # the single definition of live, reused from calls.py::_status_of.
    # LIMIT MAX_LIVE_ROWS; `truncated` set from the KPI count, not from len(rows).

async def request_intervention(
    *, tenant_slug: str, tenant_id: uuid.UUID, session_id: str, action: str,
    user: CurrentUser, ip_address: str | None,
) -> dict | None
    # Returns None when `SELECT 1 FROM calls WHERE session_id=$1 AND tenant_id=$2
    # AND ended_at IS NULL` finds nothing (router turns that into the 404 + denial
    # audit). Otherwise one transaction: INSERT live_call_interventions
    # (outcome='unavailable') + audit.write_audit(...). Outcome is a module constant
    # today; when the telephony join exists it becomes 'granted'/'denied' with no
    # change to the route, the table or the audit shape.
```

### `services/config/routers/tenants.py`
```python
@router.patch("/{tenant_id}/concurrency")
async def update_tenant_concurrency(
    tenant_id: str, body: TenantConcurrencyUpdate,          # {"max_concurrent_calls": int}
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
)
# admin: 404 "tenant not found" unless str(current_user.tenant_id) == tenant_id —
# same non-oracle 404 GET /tenants/{slug} already returns for a foreign tenant.
# superadmin: any tenant. Delegates to tenants_service.update_tenant(), so the
# change is audited and cache-invalidated exactly like every other tenant edit.
```

### `admin-ui/lib/api.ts`
```ts
export function getLiveCalls(tenantSlug?: string): Promise<LiveCallsSnapshot>;
export function requestIntervention(
  sessionId: string, action: "listen" | "barge", tenantSlug?: string,
): Promise<InterventionResult>;
export function updateTenantConcurrency(tenantId: string, maxConcurrentCalls: number): Promise<Tenant>;
```

### `admin-ui/app/live-calls/page.tsx`
`REFRESH_MS = 5000`. One `useEffect` owns a `setInterval` (the same primitive
`admin-ui/app/users/page.tsx:77` already uses; there is no SSE/WebSocket convention for console
list data — the one WebSocket, `TestAgentPanel.tsx`, is the webcall mic bridge — and none is
introduced). Pause clears the interval and stops applying responses; resume fires an immediate
fetch **before** restarting the interval (AC7 — otherwise the first post-resume paint is up to 5s
stale). An in-flight response that lands after pause is discarded via a generation counter, so a
request issued pre-pause cannot overwrite the frozen table (AC6). Fetch failures set a banner and
keep the last good snapshot rather than blanking the table (lesson 21's sibling failure mode); a
403 mid-session (the operator was demoted or a selection re-validation failed) stops the interval
and shows the reason instead of retrying every 5s. A `null` `max_concurrent_calls` renders the
utilization tile as a "Channel cap not set" prompt linking to the Accounts page, never a number.
`supervisor` and `admin` call `getLiveCalls()` with no slug; `superadmin` passes the slug from
`ACTIVE_TENANT_STORAGE_KEY`, and with nothing selected renders an in-page tenant picker instead of
a table (AC2 — no aggregated view exists in this feature, and `listAllCalls`'s cross-tenant fanout
in `app/calls/page.tsx` is deliberately not reused). Export builds a CSV client-side from the
rendered `items` state — the exact rows and columns on screen, no re-query (AC8).

## Risks
- **Every existing tenant starts with `max_concurrent_calls IS NULL`, so the utilization KPI has nothing to show until an admin sets it.** The column is nullable with no default precisely so the feature cannot invent a cap (AC13); `utilization_pct` is `null`, and the KPI tile renders "Channel cap not set — set it on the Accounts page" with a link, so the gap is visible and actionable rather than a plausible-looking 700%. Above 100% (a real cap genuinely exceeded) the bar clamps at 100% and shows an over-cap badge.
- **`live_stage` is written by a different service than the one that reads it (lesson 11).** The column is nullable and every reader `COALESCE(live_stage,'ai')`s, so the schema half is inert until Conversation ships its four hook calls; if Conversation lags, the stage KPIs read "all AI-only" (honest for a build with no transfer telemetry) instead of erroring.
- **`record_live_stage` is fire-and-forget, so a dropped write leaves a call showing a stale stage.** It rides the existing `_spawn` chain, which serializes per session (a later `ai` cannot overtake an earlier `human_connected`) and logs failures; a missed write self-corrects at the next transfer event and is bounded by the call's own lifetime — the row disappears from the view when `ended_at` is set regardless.
- **The 5s poll shares a 10-connection pool (`services/config/db.py:47`) with every other Config route.** One request = one `acquire()` and two indexed statements over only live rows, and the tenant lookup is the already-Redis-cached `get_tenant`, so a 20-operator floor is ~8 queries/s; `MAX_LIVE_ROWS = 200` bounds the payload and the per-row lateral count so one runaway tenant cannot turn a poll into a large scan. Load must be measured at the shipped 5000ms and shipped LIMIT, never at a slacker interval (lesson 25).
- **`POST /live-calls/{session_id}/interventions` accepts an arbitrary opaque `session_id`.** It is never concatenated into SQL (parameterized), is length-capped at 200 before use, and its tenant predicate lives in the query itself rather than in a JOIN, because the path segment carries no tenant (lesson 30).
- **A supervisor now reaches a Config route for the first time.** The grant is a dedicated `require_live_calls_operator` on two enumerated routes; `CONSOLE_ROLES` is untouched, so the ~28 bare `get_current_user` routes stay closed, and `test_console_gate.py` keeps asserting `supervisor` → 403 on `/users`, `/calls/{id}` and `/audit-log`.
- **Branch selection is a privileged decision too, so it reads the same fresh row as the privilege check.** `_resolve_scope` calls `fresh_authority` first and only then decides platform-scoped vs tenant-scoped from `effective_user.tenant_id`; because scope_key comes from the request rather than from the actor, that ordering is possible with no second read, and the two decisions cannot disagree. A design or review diff for this route should grep for `user.tenant_id` after the `fresh_authority` line and expect zero hits (lesson 19).
- **No privileged decision in this feature rests on a JWT claim, and none of them cost a query per poll.** Granting Listen/Barge re-reads the actor on every request (`assert_current_authority`). Everything `GET /live-calls` decides — the tenant scope for a platform-scoped selection, and transcript-snippet inclusion (AC15) — reads the role/tenant `fresh_authority()` took from the `users` row, memoized 60s per `(user_id, scope_key)`. Polls inside that window do not touch the database, so an open session costs one extra read per minute rather than one per 5s cycle, and a demotion or deactivation is bounded by 60s instead of the token's ~12h.
- **The authority memo is an authorization decision held in process memory.** It is keyed by `(user_id, scope_key)` so a tenant switch cannot reuse another selection's validation, expires in 60s, is per-worker (a second worker simply re-reads), and stores only a timestamp plus the row-derived role/tenant — so a stale entry can delay a revocation by at most 60s and can never grant a scope or a transcript the database did not confirm. 60s is the floor the poll cost allows: at 5s it would be one `users` read per poll, which is the load this design exists to avoid.
- **A denial writes an `audit_log` row per attempt, so a scripted prober can grow that table.** No new rate-limit knob is added (the existing throttles in `app.py` are invite-specific); the row is small, the `session_id` is truncated to 200 chars, and every audited action in this repo already carries this property — flagged so a future global throttle covers this route too.
- **The audit row's `entity_id` is the tenant UUID, not the call.** `audit_log.entity_id` is `UUID NOT NULL` and `session_id` is TEXT; widening that column would touch every existing audit writer and reader, so the session id lives in `new_value` and `entity_type='live_call_intervention'` makes the pairing unambiguous.

## Test plan
Unit / module (`services/config/tests/test_live_calls.py`, pytest + `AsyncClient` against the real
Config app, following `test_calls.py` and `test_console_gate.py`; fixtures `test_tenant`,
`test_viewer`, `pool`):
1. **Tenant isolation with the JOIN unable to help (AC1/AC10, lesson 12).** Two tenants each with a
   live call. Tenant A's admin polls → exactly A's row and A's counts. Then the case a JOIN cannot
   reach: call and agent both belong to tenant B, caller is tenant A's admin → `POST
   …/interventions` returns 404 and inserts no `live_call_interventions` row. Prove the guard by
   deleting the `tenant_id = $2` predicate and watching this test — not the others — fail.
2. **No existence oracle (AC3/AC10).** On `GET /live-calls`: a tenant-scoped admin supplying a
   `tenant_slug` for another (real, populated) tenant and one supplying a slug that exists nowhere
   must produce byte-identical status and body (`404 {"detail": "tenant not found"}`), through the
   same single cached lookup so neither reveals existence by timing; assert the same for a
   superadmin's nonexistent slug. On `POST …/interventions`: byte-identical status and body for
   foreign-tenant live session, nonexistent session id, and a 300-char session id. `viewer` and `agent` get 403 on
   both routes before any tenant/call lookup runs; unauthenticated gets 401.
3. **Platform-scoped access (AC2, lesson 24).** NULL-tenant `viewer` service account (the exact
   shape `scripts/create_service_account.py` mints) → 403 on both routes. NULL-tenant `superadmin`
   with no `tenant_slug` → 400; with a slug → that tenant only. There is no code path returning rows
   for more than one tenant — asserted by a tripwire on `_resolve_scope`'s return type being a single
   slug, which fails if an "all tenants" branch is ever added.
   **Selection-time re-validation (blocking half of AC9/AC2).** NULL-tenant superadmin polls tenant A
   → 200. Soft-delete that superadmin, then replay the same token: polling tenant A again within the
   60s memo still returns 200 (the documented, bounded window), while selecting tenant B → 403
   because the memo is slug-keyed. Clear the memo (or advance past its TTL with the shipped 60s
   value, never a shortened one — lesson 25) and tenant A → 403 too. Demote the account to `viewer`
   and assert the same, so the check fails for role change as well as deletion. Deleting the
   `fresh_authority` call must make the tenant-B case fail; deleting only the slug from the memo
   key must make it fail too.
   **Re-tenanted superadmin (branch selection).** Mint a token for a NULL-tenant superadmin, then
   `UPDATE users SET tenant_id = <tenant A>` while the token still claims NULL. With that unchanged
   token: `GET /live-calls?tenant_slug=<B>` → 404 (confined to A by the fresh row, not routed down
   the platform-scoped branch by the stale claim), `?tenant_slug=<A>` → 200 with A's rows only, and
   omitting `tenant_slug` → 200 for A rather than the platform-scoped 400. Same matrix on
   `POST …/interventions`. Then the inverse: a token claiming tenant A whose row is now NULL and
   role `superadmin` → treated as platform-scoped (400 without a slug). Reverting the branch to
   `user.tenant_id` must fail the first case; leaving `fresh_authority` in place but branching on the
   claim is exactly the regression this asserts, so it must not pass on the role check alone.
4. **Liveness and the KPI split (AC4/AC5/AC13).** Seed live calls across all three `live_stage`
   values plus one `live_stage IS NULL` (must count as `ai_only`); assert counts, masked numbers
   (raw MSISDN absent from the whole serialized body), `elapsed_ms` from `started_at`, and
   `utilization_pct = live/max_concurrent_calls*100` against that tenant's own value while a second
   tenant has a different cap. A tenant with `max_concurrent_calls IS NULL` returns
   `utilization_pct: null` and `max_concurrent_calls: null` — assert no numeric fallback appears
   anywhere in the body, which is what would fail if a `COALESCE(...,1)` were ever added. Then set
   `ended_at` and re-poll: row gone, counts decremented.
5. **AC9 stale-token re-validation.** Supervisor requests Barge → 202. Soft-delete the user
   (`deleted_at`) and replay the same token → 403, no new intervention row. Repeat with the role
   changed to `viewer` → 403. This fails if `assert_current_authority` is removed, which is the point.
6. **Audit completeness (AC11/AC12).** In-tenant request → one `live_call_interventions` row
   (`outcome='unavailable'`) **and** one `audit_log` row with actor id/email, tenant, session id and
   outcome; response says `unavailable`, never a success shape. Out-of-scope request → audit row
   with `outcome='denied'` and no intervention row. Also assert the pair is transactional by
   forcing the `write_audit` call to raise and checking no intervention row survives.
7. **AC15 snippet authority, decided on the DB role not the token.** Same call, three callers:
   `admin` gets a snippet; `supervisor` gets `transcript_snippet: null, transcript_withheld: true`
   and its whole body contains no substring of the seeded transcript text. Then the demotion case:
   the admin polls once (200, snippet present), its `users.role` is updated to `supervisor` in the
   database while its token stays valid, and after the shipped 60s `AUTHORITY_MEMO_TTL_S` — never a
   shortened one (lesson 25) — the same token's next poll returns `transcript_withheld: true` and no
   transcript text. Repeat with `deleted_at` set → 403. Replacing `fresh_authority`'s role with
   `user.role` must make this test fail; the first (pre-TTL) poll still returning the snippet is the
   documented 60s bound, asserted explicitly so the window cannot silently grow.
8. **AC16 concurrency edit.** Tenant admin PATCHes its own tenant's concurrency → 200, next poll
   shows the new `utilization_pct` (proves `cache.invalidate` fires); the same admin PATCHing
   another tenant → 404; a `viewer` → 403.

Console-gate regression: the widened `get_authenticated_user` allowlist test must still fail when a
third route is added to that dependency, and must fail if `LIVE_CALLS_ROLES` gains `viewer`.

Frontend (manual, driven in a browser — lesson 23; no test runner exists for `admin-ui`):
pause holds the table across three refresh cycles while calls start and end; resume repaints
immediately, with no pre-pause rows left; CSV row/column count matches the screen exactly; a
supervisor logs in and lands on `/live-calls` with only that nav item and no 403 banner; a
superadmin with no tenant selected sees the picker, not an empty table; two browser windows on one
tenant both show the barge badge within one 5s cycle.
