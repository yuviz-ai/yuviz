# Design: Outbound campaigns Phase 6, capacity API and campaign UI

## Approach
Everything the UI needs is already computed server-side: limits and their validation (`capacity.py`), readiness (`readiness.py`), wait reason, waiting count and effective limit (`campaigns.get_progress`), and attempt history (`attempts.list_for_campaign`). This phase adds one superadmin-only router in Campaigns over `capacity.py`. It extends exactly two existing reads: attempts gain a server-masked callee and disposition, and a caller-ID listing reports each number's path from the same join `resolve_outbound_route` uses. It also builds the console surfaces: a capacity card, a shared dialing-fields form block, a readiness panel and an attempts list. The obvious alternative is to compute the path badge, mask or wait state in the browser. We reject it because each of those values would then have a second source of truth that drifts from what the worker actually does.

Open-question resolutions (binding for this phase):
- **OQ1, tenant-admin cap visibility: no.** No tenant-facing capacity route is built. The `campaign_cap_above_tenant_cap` readiness warning and `progress.effective_limit` stay the only tenant-visible signals. Both already ship.
- **OQ2, contacts list: unchanged.** `GET /campaigns/{id}/contacts` still returns `phone_number` to the tenant that uploaded it. The new attempt rows show only the server mask.
- **OQ3, disposition:** `calls.disposition` for attempts with `answered_at IS NOT NULL`, null otherwise. Phase 7 outcome fields are not read.
- **Billing seam: render-if-present, no mock in product code.** `estimate_campaign` keeps raising `NotImplementedError` when billing is on. Phase 6 does three things: guards the call so it can never fail readiness or Start, renders `readiness.estimate` when it is non-null, and proves AC31 with a test stub. Production runs `NullBillingAdmission`, so no cost line renders until Phase 8 (AC32).
- **PRD-review item 6, audit PII:** option (C). Phase 6 audit writes carry only limit rows, tenant caps and campaign rows. None of them contains a callee. `write_audit` is not changed.

## Changes
| File | Change | Why |
|---|---|---|
| `services/campaigns/capacity.py` | Add the `RECENT_SEND` predicate constant, which `_recent_sends` now uses. Add a `for_update` kwarg to `get_platform_limits`. Add `list_cps_limits`, `get_tenant_cap`, `list_tenant_caps` and `usage`. | Lets the capacity routes read old values under lock, and counts usage with the same predicates as admission (`IN_FLIGHT`, one-second send window), so AC11 holds by construction |
| `services/campaigns/routers/capacity.py` (new) | `capacity_router`, prefix `/capacity`, router-level `dependencies=[Depends(require_platform_operator)]`, 7 routes. `require_platform_operator` is a local `async def` dependency: `user.role == "superadmin" and is_platform_scoped(user)`, else 403. | AC1–12. A tenant-scoped superadmin is refused. The gate runs before path and schema validation. |
| `services/campaigns/schemas.py` | `PlatformLimitsUpdate`, `CpsLimitUpdate`, `TenantCapUpdate` (strict ints, required keys) | AC6: rejects `"5"`, `5.5`, `true` and a missing key |
| `services/campaigns/app.py` | `app.include_router(capacity.capacity_router)`. Add a `RequestValidationError` handler that returns 400 with `{"detail": [{"loc", "msg"}]}` (drops pydantic's `input`/`ctx`). | AC6/13/14 require 400, not FastAPI's 422. Dropping `input` stops request values being echoed back. |
| `services/campaigns/attempts.py` | `list_for_campaign` adds `callee_masked` and `disposition` (join below). Add private `_mask_callee`. | AC26/27 |
| `services/campaigns/readiness.py` | Move `estimate_campaign` out of the existing `platform_conn` block into `_estimate(campaign, billing) -> CostEstimate \| None`. `_estimate` opens its own `platform_conn(pool, reason="campaign-by-id")` inside `asyncio.wait_for(..., BILLING_TIMEOUT_S)`, and `except Exception` gives `None` + one `log.warning("cost estimate unavailable")`. A timeout then cancels only the estimate's own connection; the context manager rolls it back and releases it. | AC33. Without this, any billing-enabled deploy before Phase 8 makes readiness, Start and Resume 500 on `NotImplementedError`. |
| `services/campaigns/campaigns.py` | `list_caller_ids(tenant_id)`. `get_progress` adds `c.status` to its existing `state` SELECT, returned as `status`. | Path badge source (AC15), same join as `resolve_outbound_route`. Each poll tick sees status changes made by the worker or another user (AC25). |
| `services/campaigns/routers/campaigns.py` | `GET /tenants/{tenant_id}/campaigns/caller-ids` on `tenant_scoped_router` | Inherits `bind_path_tenant` + `require_path_tenant_access`. Adds no `{campaign_id}` route, so the walk in `test_campaign_tenancy.py` stays at 10. |
| `tests/test_rls_coverage.py` | Add `"capacity-admin"` to `_BYPASS_REASONS` | New `platform_conn` reason; the walker fails otherwise |
| `tests/test_cross_tenant_admin.py` | Extend `test_tier3_campaigns` with `/tenants/{b}/campaigns/caller-ids` as tenant-admin-A → 403. A superadmin gets 200. | Covers the new tenant-scoped route the way the file covers its siblings |
| `services/campaigns/tests/test_capacity_routes.py` (new) | Route walk, role and scope, invariance, validation, audit, concurrency, usage | AC1–12 |
| `services/campaigns/tests/test_campaign_routes.py` | Block-id 409s, warn-only start, attempts mask and disposition, validation 400, no-op audit, viewer 403s, log scan | AC14, 16, 19–21, 26–27, 29–30 |
| `services/campaigns/tests/test_readiness.py` | Estimate stub renders; raising or slow estimate yields `estimate=None` and `ready` unchanged | AC31–33 |
| `admin-ui/lib/api.ts` | `campaignsRequest` parses list `detail` like `request()` and passes the parsed body to `ApiError`. New types and functions are listed under Interfaces. `Campaign`/`CampaignCreate`/`CampaignUpdate` gain `retry_gap_minutes`; `Campaign` gains `wait_reason`, `wait_since`; `CampaignProgress` gains `status`. | The 409 checks body and the 400 `loc` must reach the UI (AC17, AC20) |
| `admin-ui/components/campaigns/CampaignDialingFields.tsx` (new) | Shared caller-ID select with path badge, max attempts, retry gap, max concurrent calls | One copy for the create and edit forms (AC13–17) |
| `admin-ui/components/campaigns/ReadinessPanel.tsx` (new) | Blocks, warnings, wait state, cost line | AC18, 22, 31–32 |
| `admin-ui/components/campaigns/AttemptsList.tsx` (new) | Attempt rows for one contact | AC26 |
| `admin-ui/components/CapacityCard.tsx` (new) | Platform limits, CPS, per-tenant cap and usage, inline save errors | AC34–35 |
| `admin-ui/app/(console)/campaigns/new/page.tsx` | Replace the inline caller-ID, max-attempts and concurrency inputs with `CampaignDialingFields`, and add retry gap | AC13–15 |
| `admin-ui/app/(console)/campaigns/[id]/page.tsx` | Independent section fetches (no `Promise.all`), readiness panel, Settings (edit) section, attempts under each contact, polling, role gating | AC16–18, 22–25, 29 |
| `admin-ui/lib/useConnectionAccess.ts` | Also keep `tenant_id` from `getCurrentUser()` and return `isPlatformOperator: role === "superadmin" && tenantId === null`. Additive; existing fields are unchanged. | The UI gate must ask the same question as the server gate |
| `admin-ui/app/(console)/tenants/page.tsx` | Mount `<CapacityCard/>` only when `useConnectionAccess()` reports `checked && isPlatformOperator` | AC34. The card owns its fetches, so no request is made otherwise. |

## Data
None. No DDL. `platform_outbound_limits`, `outbound_cps_limits`, `tenants.max_outbound_concurrent_calls`, `campaigns.wait_reason/wait_since`, `campaign_attempts.callee/call_session_id/answered_at` and `calls.disposition` all exist in `database/schema.sql`.

`audit_log.entity_id` is `UUID NOT NULL`, so non-UUID entities get these fixed ids:
- Platform limits: `entity_type="platform_outbound_limits"`, `entity_id=UUID(int=0)`.
- CPS limit: `entity_type="outbound_cps_limit"`, `entity_id=uuid5(NAMESPACE_URL, "outbound_cps_limits:" + scope)`, with `scope` inside `old_value` and `new_value`.
- Tenant cap: `entity_type="tenant"`, `entity_id=tenant_id`, `old_value/new_value={"max_outbound_concurrent_calls": n}`.
- Action is always `"updated"`. Audit rows carry `user_id=current_user.id` and `user_email=current_user.email`, matching `campaigns.update_campaign`.

## Interfaces

### Capacity router (`services/campaigns/routers/capacity.py`)
Auth: router dependency `require_platform_operator`, defined in `routers/capacity.py`:
```
async def require_platform_operator(user: CurrentUser = Depends(get_current_user)) -> CurrentUser
    # 403 "only the platform operator can manage capacity" unless user.role == "superadmin" and is_platform_scoped(user)
```
- Privilege and scope are separate questions, so both are checked (lesson 24). A superadmin row with a non-NULL `tenant_id` gets 403. So do service accounts (`viewer`, NULL tenant); no service needs these routes.
- The function is defined locally, not in `services/config/deps.py`. The shared module's consumers are therefore not widened (lesson 9). It reuses `get_current_user` (fresh user-row re-read, lesson 27) and `is_platform_scoped`, the codebase's canonical `tenant_id IS NULL` predicate.
- The 403 text is constant, so for one principal the response is identical for its own tenant id, another tenant's id and a random UUID (AC3, lesson 2).
- Path params are typed `str`, never `UUID`, so no path validation error can come before the gate. FastAPI resolves router dependencies before it validates the body against the pydantic schema, so a well-formed body with bad values from a non-operator gets 403, not 400.
- Exception: a body that is not parseable JSON fails in FastAPI's body read, which runs before dependencies, so it gets 400 regardless of role. That response is caller-invariant and changes nothing, so it leaks nothing.
- The dependency is declared in `APIRouter(dependencies=[...])` and so is awaited by FastAPI, never called by hand (lesson 38).

Every write runs in a single `platform_conn(pool, reason="capacity-admin")` transaction. The transaction does four things in order:
1. Takes `SELECT ... FROM platform_outbound_limits FOR UPDATE`. This is the one mutex for all capacity writes.
2. Reads the old value.
3. Calls the existing setter, with these literal calls:
   - `await capacity.set_platform_limits(conn, total_call_capacity=body.total_call_capacity, inbound_reserve=body.inbound_reserve, max_outbound_concurrent_calls=body.max_outbound_concurrent_calls, updated_by=user.id)`
   - `await capacity.set_cps_limit(conn, scope, body.cps, updated_by=user.id)`
   - `await capacity.set_tenant_cap(conn, tenant_uuid, body.max_outbound_concurrent_calls)`. `tenants` has no `updated_by`; the audit row carries the actor.
4. Calls `write_audit` on the same connection.

A `LimitError` raised anywhere rolls the whole transaction back, so the row is unchanged and no audit row exists (AC5, AC9). The app's existing `ValueError` handler turns `LimitError` into a 400 with its safe message. Admission (`capacity.admit`) reads these rows with plain `SELECT`, so the lock never blocks dialing.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/capacity/platform` | none | `{total_call_capacity, inbound_reserve, max_outbound_concurrent_calls, updated_at, updated_by, cps_limits: [{scope, cps, updated_at}]}` (AC1) |
| PUT | `/capacity/platform` | `PlatformLimitsUpdate` | same shape as GET |
| PUT | `/capacity/cps/{scope}` | `CpsLimitUpdate` | `{scope, cps, updated_at}`. An unknown scope gets 400 `"unknown CPS scope"` from `set_cps_limit`. |
| GET | `/capacity/usage` | none | `{platform_in_flight: int, cps: [{scope, cps: int \| null, sends_last_second: int}]}`. Every configured scope plus any scope with sends in the window (AC11). |
| GET | `/capacity/tenants` | none | `[{tenant_id, name, slug, max_outbound_concurrent_calls, in_flight}]` |
| GET | `/capacity/tenants/{tenant_id}` | none | `{tenant_id, max_outbound_concurrent_calls, in_flight}`. 404 `"tenant not found"` for a non-UUID or unknown id (AC4). |
| PUT | `/capacity/tenants/{tenant_id}` | `TenantCapUpdate` | same as GET. Old value is read with `get_tenant_cap(conn, id, for_update=True)`; `None` gives 404 with no write and no audit. `null` clears the cap (AC7). |

Route walk (AC12): `len(capacity_router.routes) == 7`.

### Schemas (`services/campaigns/schemas.py`)
All three use `model_config = ConfigDict(strict=True)`, so `"5"`, `5.0` and `true` are rejected. Every key is required, even when its value may be null.

```
class PlatformLimitsUpdate(BaseModel):
    total_call_capacity:           int | None = Field(..., ge=1)
    inbound_reserve:               int        = Field(..., ge=0)
    max_outbound_concurrent_calls: int | None = Field(..., ge=1)

class CpsLimitUpdate(BaseModel):
    cps: int = Field(..., ge=1)

class TenantCapUpdate(BaseModel):
    max_outbound_concurrent_calls: int | None = Field(..., ge=1)
```

### capacity.py additions
```
RECENT_SEND = "last_send_at > clock_timestamp() - interval '1 second'"
async def get_platform_limits(conn, *, for_update: bool = False) -> dict[str, Any]
async def list_cps_limits(conn) -> list[dict[str, Any]]
async def get_tenant_cap(conn, tenant_id: UUID, *, for_update: bool = False) -> dict[str, Any] | None   # {"max_outbound_concurrent_calls": ...} or None if no tenant
async def list_tenant_caps(conn) -> list[dict[str, Any]]      # tenants LEFT JOIN in-flight counts per tenant_id (IN_FLIGHT)
async def usage(conn) -> dict[str, Any]
```
- The old CPS value is read with `SELECT cps FROM outbound_cps_limits WHERE scope=$1` after the singleton lock is held. Two concurrent first inserts of one scope are therefore serialized, and each audit `old_value` is exact (AC10).
- Concurrent `PUT /capacity/platform` calls cannot store a mix of their fields: each is one `UPDATE` that sets all three fields, and the singleton lock orders them (AC10).

### Attempts (`attempts.list_for_campaign`, same signature)
The SELECT gains:
```
FROM campaign_attempts a
JOIN tenants t ON t.id = a.tenant_id
LEFT JOIN calls k ON k.session_id = a.call_session_id AND k.tenant_id = t.slug
WHERE a.campaign_id = $1 AND a.tenant_id = $2
```
- New output columns are `a.callee` (raw, internal only) and `CASE WHEN a.answered_at IS NOT NULL THEN k.disposition END AS disposition`.
- `calls.tenant_id` is the tenant slug, so the `k.tenant_id = t.slug` predicate is the explicit tenant predicate (lesson 36). Only `disposition` is read from `calls`.
- In Python each row does `row["callee_masked"] = _mask_callee(row.pop("callee"))`. The raw callee never leaves the function.

`_mask_callee(value: str | None) -> str | None`:
- Takes the digits only.
- More than 4 digits gives `"•••" + digits[-4:]`.
- 1 to 4 digits gives `"•••"`, so a short test DID is never shown whole.
- `None` gives `None`.

### Caller IDs
`campaigns.list_caller_ids(tenant_id) -> list[dict]` runs on `tenant_conn`. It returns `phone_numbers pn LEFT JOIN telephony_configs tc ON tc.id = pn.telephony_config_id AND tc.tenant_id = pn.tenant_id AND tc.deleted_at IS NULL WHERE pn.tenant_id = $1 AND pn.deleted_at IS NULL`. Each row is `{did, path}` with `path = tc.provider or "native"`.

Route: `GET /tenants/{tenant_id}/campaigns/caller-ids`, any console role (read). Ownership is still enforced on write by the existing `caller_id_owned_by_tenant` 422 (AC15 unchanged).

### Admin UI (`admin-ui/lib/api.ts`)
```
interface PlatformLimits { total_call_capacity: number|null; inbound_reserve: number; max_outbound_concurrent_calls: number|null; updated_at: string; cps_limits: CpsLimit[] }
interface CpsLimit { scope: string; cps: number; updated_at: string }
interface CapacityUsage { platform_in_flight: number; cps: { scope: string; cps: number|null; sends_last_second: number }[] }
interface TenantCap { tenant_id: string; name: string; slug: string; max_outbound_concurrent_calls: number|null; in_flight: number }
interface ReadinessCheck { id: string; level: "block"|"warn"; field: string; message: string; count: number|null }
interface CostEstimate { currency: string; calls: number; per_call_minor: number; total_minor: number; avg_billable_s: number; avg_source: string }
interface CampaignReadiness { ready: boolean; checks: ReadinessCheck[]; estimate: CostEstimate|null }
interface CampaignAttempt { id; contact_id; attempt; outcome: string|null; state; disposition: string|null; callee_masked: string|null; created_at; answered_at: string|null; ended_at: string|null; failure_reason: string|null }
interface CallerIdOption { did: string; path: string }
CampaignProgress += { busy; declined; wait_reason: string|null; wait_since: string|null; tenant_cap: number|null; effective_limit: number; waiting_count: number }

getPlatformLimits(); updatePlatformLimits(body); updateCpsLimit(scope, cps); getCapacityUsage();
listTenantCaps(); updateTenantCap(tenantId, cap: number|null);
getCampaignReadiness(id); listCampaignAttempts(id); listCampaignCallerIds(tenantId)
```
`check.id` is typed `string`, not a closed union, so an unknown id still renders.

### Component hierarchy and data flow
```
TenantsPage
└── CapacityCard                         (rendered only if checked && isPlatformOperator)
    ├── PlatformLimitsForm               fetch getPlatformLimits      → own {data,error}
    ├── CpsLimitsTable                   (from PlatformLimits.cps_limits + CapacityUsage.cps)
    ├── UsageStrip                       fetch getCapacityUsage, 10 s poll while visible
    └── TenantCapsTable                  fetch listTenantCaps         → own {data,error}
        └── row: stored value (display) | draft input | Save | inline error

CampaignDetailPage
├── header + StatusActions               canManage only; Start/Resume disabled iff readiness has a block
├── PausedNote                           status === "paused"
├── ReadinessPanel(readiness, progress)  blocks red, warns amber, wait state, cost line
├── SettingsSection (edit form)          canManage only; uses CampaignDialingFields + name, timezone
├── ProgressCounts
└── ContactsTable
    └── per contact (expand) → AttemptsList(attempts.filter(contact_id))

CampaignNewPage → CampaignDialingFields
```

**Role-based fetch pattern (lesson 21).**
- `getCampaign(id)` runs first. Its failure is the only page-level error (404 renders "campaign not found").
- `getCampaignReadiness`, `getCampaignProgress`, `listCampaignAttempts`, `listCampaignContacts` and the tenant/agent lookup then run as **separate promises**, each with its own `.then(set).catch(setSectionError)`. One returning 403 or 500 leaves the others rendered (AC24).
- `useConnectionAccess()` decides which write controls render. Before `checked`, write controls are hidden (fail closed).
- `listCampaignCallerIds` is called only inside `SettingsSection`, which mounts only for `canManage`.
- The tenants page today calls `Promise.all([listAgents, listPhoneNumbers])`. That call is per-tenant and admin-level, so it stays. The card never joins it.

**Readiness panel.**
- Field map: `caller_id`, `max_concurrent_calls` and `calling_hours_timezone` map to `#campaign-field-<field>` in SettingsSection; `contacts` maps to the Upload CSV button; `agent_id` maps to the existing agent link in the header.
- A link calls `document.getElementById(...)?.focus()`.
- An unknown id or field, or a viewer (no SettingsSection), gets the message without a link.

**Start/Resume.**
- A 409 `ApiError.body` (`{detail, ready, checks, estimate}`) replaces the readiness state, so the panel shows exactly the checks that blocked the request (AC20).
- If readiness failed to load, Start stays enabled and the server's 409 is the gate.

**Wait state (AC22).**
- Shown only when `progress.wait_reason` is non-null. It shows a plain-words sentence, `waiting_count`, the time since `wait_since` and `effective_limit`.
- Plain words come from a map over the 11 `capacity.WaitReason` values; an unknown reason renders as-is.

**Polling (AC25).**
- A 5 s `setInterval` refetches progress and attempts only, never readiness, which probes the telephony service and SIP.
- Each tick writes `progress.status` into the page's `campaign.status`.
- The interval effect depends on `campaign.status`. When a tick observes anything other than `"running"`, the effect's cleanup clears the interval. This covers a worker pause or completion and a pause by another user.
- It also stops while `document.hidden`; a `visibilitychange` listener starts and stops it.
- A Resume or Start from this page sets `campaign.status` from the action response, which re-arms the interval.

**Cost line (AC31–33).**
- Rendered only when `readiness.estimate` is non-null.
- Format: `Intl.NumberFormat(undefined, {style: "currency", currency})`. Minor units are divided by `10 ** resolvedOptions().maximumFractionDigits`.
- It shows per contact (`per_call_minor`) and per campaign (`total_minor`). It never contributes to the disabled state.

**Forms.**
- `CampaignDialingFields` mirrors `schemas.py`: attempts `min=1`, gap `min=1`, a blank gap submits `60`. A value below 1 blocks submit with an inline message.
- Stored values load verbatim, including out-of-range legacy values (no clamping).
- The caller-ID `<option>` label is `"<did> · Native SIP" | "· Vobiz" | "· Cloudonix" | "· <provider>"`. A badge next to the select shows the chosen path. The existing "Custom…" free-text entry stays, and the server 422 rejects an unowned number.
- The edit form always PATCHes its full field set, so an unchanged save still runs `update_campaign` and writes one audit row (AC16; existing behaviour).

**Server errors (AC17).**
- A list `detail` is mapped to a field by `loc[loc.length-1]`.
- A string `detail` is mapped by its leading token (`caller_id …`, `agent_id …`, which is how both existing 422 messages begin).
- Otherwise it shows at the form top. User input is never reset on error.

**CapacityCard save (AC35).** The input is a draft state separate from the displayed stored value. The stored value is replaced only from a successful PUT response. On error `err.detail` shows inline and the draft is kept.

### Acceptance-criteria trace
| AC | Where |
|---|---|
| 1, 11 | `GET /capacity/platform`, `GET /capacity/usage` (`IN_FLIGHT`, `RECENT_SEND`) |
| 2, 3, 12 | router-level `require_platform_operator` (superadmin AND platform-scoped), `str` path params, route walk = 7 |
| 4, 7 | `get_tenant_cap(..., for_update=True)` → 404; nullable `TenantCapUpdate` |
| 5, 6 | `LimitError` → 400; strict schemas + `RequestValidationError` → 400 |
| 8, 9, 10 | single-transaction lock → old → set → audit; rollback on any raise |
| 13–17 | `CampaignDialingFields`, SettingsSection, 400 handler, `ApiError.body` |
| 18–21 | `ReadinessPanel`; existing `_start_or_409`; new block-id tests |
| 22, 23 | `progress` fields already returned; PausedNote |
| 24, 25 | independent section fetches; visibility-gated poll |
| 26, 27, 30 | `list_for_campaign` join + `_mask_callee`; no new log lines carry callee |
| 28 | no new `{campaign_id}` route; `test_campaign_tenancy.py` walk stays 10 and green |
| 29 | `canManage` gating; existing `require_role("superadmin","admin")` on writes |
| 31–33 | guarded `estimate_campaign`; cost line render-if-present |
| 34, 35 | tenants-page `isPlatformOperator` gate; card-owned fetches; draft vs stored value |
| 36 | `npm run typecheck` + `npm run build` |

## Risks
- Turning `RequestValidationError` from 422 into 400 changes every Campaigns validation status. Mitigation: the only HTTP consumer is `admin-ui` (`campaignsRequest`), which is updated in the same change. `scripts/start_local.sh` only launches the service. The list-shaped `detail` is preserved, and `test_schemas.py` uses its own bare FastAPI app, so it is unaffected.
- With billing enabled before Phase 8, `estimate_campaign` raises `NotImplementedError` and would 500 readiness, Start and Resume. Mitigation: the `readiness.check` guard (`wait_for(BILLING_TIMEOUT_S)` + `except Exception` → `estimate=None`, one warning log) is the named recovering line. `test_readiness.py` covers both a raising stub and a slow one.
- `audit_log.entity_id` is `UUID NOT NULL` but platform and CPS limits have no UUID. Mitigation: fixed `UUID(int=0)` for the singleton and `uuid5` per scope, documented under Data, with `scope` repeated in the values.
- The singleton `FOR UPDATE` serializes all capacity writes. Mitigation: writes are operator-only and rare. `admit()` and `cps_exhausted()` use plain reads, which MVCC never blocks; a test asserts admission completes while a write transaction holds the lock.
- The tenant-cap write locks the `tenants` row and briefly blocks a concurrent Config tenant update. Mitigation: it is a single-row update with no external I/O inside the transaction.
- There is no route to delete a CPS limit (`capacity.py` has no delete). Mitigation: out of PRD scope. The operator can set a high value, and the card says "no row = no limit" for scopes absent from the list.
- `calls.tenant_id` is a slug, so the disposition join depends on slug equality. Mitigation: a mismatch (for example a renamed tenant) yields `disposition = null`, never another tenant's call, because the join also keys on `session_id` from the attempt's own row.
- Short DIDs (for example `5003`) would be fully revealed by a "last 4" mask. Mitigation: `_mask_callee` shows digits only when there are more than 4.
- The contacts list still returns full `phone_number` (OQ2). Mitigation: accepted per the PRD assumption. Attempt rows and capacity responses carry only the server mask, and AC27's test is scoped to those.
- The new platform bypass reason `capacity-admin` would fail `test_rls_coverage.py`. Mitigation: it is registered in `_BYPASS_REASONS` in the same change.
- Network: no new listener, port or bind. The routes ride the existing Campaigns `:8400` app behind the same JWT, so LAN-peer exposure is unchanged and the routes are reachable only with a superadmin token.

## Test plan
Integration tests (pytest against the scratch DB via the existing `scratch_pool` and `world` fixtures) prove the server contract. The UI is proven by type-check and build plus a manual drive as the three roles (lesson 23).

1. **Capacity gate and invariance (AC2, 3, 9, 12)**, `test_capacity_routes.py`.
   - Walk `capacity_router.routes` and assert the count is 7.
   - For each route, use principals admin-A, viewer-A, service-account (`viewer`, NULL tenant) and a tenant-scoped superadmin (`superadmin`, tenant A). Assert 403. Bodies are well-formed and schema-valid, so the gate, not validation, is what is tested. For the tenant routes, also assert byte-identical status and body for own-id, B-id and random UUID with each principal fixed.
   - Snapshot `platform_outbound_limits`, `outbound_cps_limits`, `tenants` and `count(audit_log)` before and after: all unchanged.
2. **Validation and atomic audit (AC4–8, 10).**
   - Over-capacity cap: 400 with the `LimitError` text, row unchanged, no audit.
   - Each of `0`, `-1`, `"5"`, `5.5`, `true` and a missing key for every limit body: 400, row unchanged.
   - Bad CPS scope: 400. Unknown and non-UUID tenant: 404, no audit. A `null` tenant cap round-trips.
   - Each successful write produces exactly one audit row with `user_id`, `entity_type` and the correct old and new values.
   - Two concurrent `PUT /capacity/platform` calls (via `asyncio.gather` on two connections): the final row equals one request exactly, and audit row 2's `old_value` equals audit row 1's `new_value`.
   - Mutation check: removing the `FOR UPDATE` must make the concurrency test fail at least once in 50 iterations. If it cannot, the test proves nothing (lesson 12).
3. **Readiness gate (AC19–21, 33)**, `test_campaign_routes.py`.
   - Parametrize over the 9 block ids. Each fixture produces that check, and both `/start` and `/resume` return 409 with `{"id": <id>, "level": "block"}` in `checks` and an unchanged `status`.
   - A campaign whose only check is `campaign_cap_above_tenant_cap`: start returns 200 and status becomes `running`.
   - With `estimate_campaign` stubbed to raise, then to sleep past `BILLING_TIMEOUT_S`: readiness is 200, `estimate` is null, `ready` is unchanged, and the pool's free-connection count returns to its starting value.
   - With it stubbed to return a `CostEstimate`: the readiness JSON carries `currency`, `per_call_minor` and `total_minor` (AC31).
4. **Attempts PII and disposition (AC26, 27, 30).**
   - Seed one answered attempt with a `calls` row carrying a disposition, one `no_answer` attempt with a `calls` row carrying a disposition (it must come back null), and one attempt whose `calls` row has the same `session_id` under another tenant's slug (it must come back null).
   - Assert no response value contains the full seeded callee or its digit string. `callee_masked` ends in the last 4 digits.
   - Capture `caplog` at DEBUG across every Campaigns route call and the audit rows written in the test, and assert neither contains the callee digits.
5. **Forms and roles (AC14, 16, 29, 34, 36).**
   - PATCH and POST with `max_attempts=0` or `retry_gap_minutes=0` return 400.
   - PATCH with identical values writes exactly one `campaign` audit row and changes no other column but `updated_at`.
   - A viewer gets 403 on start, pause, resume and PATCH.
   - `GET /progress` returns `status`.
   - The tier-3 tenant-B admin gets 403 on `/tenants/{A}/campaigns/caller-ids`.
   - `test_campaign_tenancy.py` stays green unchanged at 10 routes.
   - Admin UI `typecheck` and `build` pass.
   - Manual drive on the running stack as superadmin, tenant admin and viewer:
     - The browser network tab shows no `/capacity` request for the two non-superadmin roles.
     - Over-cap save keeps the draft and shows the server text.
     - A blocked campaign has Start disabled and red links that focus the field.
     - The viewer sees no write controls.
     - Polling stops on tab hide, on pause from another tab and on a worker-driven status change (set `campaigns.status='paused'` in psql while the page polls).
