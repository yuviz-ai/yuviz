# Design: Tenant-level Knowledge Bases page

## Approach
The data is already there; only a second entry point is missing. So this is a Next.js route pair
(`/knowledge-bases` list, `/knowledge-bases/[tenantSlug]/[kbId]` detail) composed entirely from
calls `admin-ui/lib/knowledgeApi.ts` and `admin-ui/lib/api.ts` already expose, plus exactly one new
read endpoint — `GET /knowledge-bases/{kb_id}/agents` — because `agent_kb.list_for_agent` only
queries the `agent_knowledge_bases` edge in the agent→kb direction.

The list page composes per-tenant KBs the same way `listAllAgents`/`listAllPhoneNumbers`
(`admin-ui/lib/api.ts:300,388`) compose per-tenant rows over `listTenants()` — which is already
tenant-scoped server-side — rather than inventing an unscoped "all KBs" route. Unlike those
helpers it uses `Promise.allSettled`, not `Promise.all`, because AC15 and lesson 21 require one
tenant's or one section's failure not to blank the page.

The detail page resolves its KB out of `listKnowledgeBases(tenantId)` (the full row, lesson 33)
instead of adding a `getKnowledgeBase` client function, so the constraint "only one new client
function" holds literally: `listKbAgents` is the only addition to `knowledgeApi.ts`.

The one place this design does more than the PRD asked: the new endpoint enforces a caller-tenant
predicate. Every existing Knowledge Service route (`list_knowledge_bases`, `list_documents`,
`list_agent_knowledge_bases`) takes an id and checks only that the caller is authenticated — there
is no tenant boundary in that service today. That is a pre-existing gap this feature must not
widen: `kb_id` is an opaque unscoped string (lesson 30), so the new route checks
`is_platform_scoped(user) or str(kb["tenant_id"]) == user.tenant_id` and 404s otherwise, with the
same detail string as a genuine miss (lesson 2). It is not in this feature's scope to retrofit the
other routes; that gap is named under Risks.

## Changes
| File | Change | Why |
| --- | --- | --- |
| `services/knowledge/agent_kb.py` | Add `list_for_kb(kb_id)` — the mirror of the existing `list_for_agent`, joining `agents`/`tenants` and filtering `deleted_at IS NULL` on both. | The reverse lookup AC13 needs; belongs beside its twin, not in a new module. |
| `services/knowledge/routers/knowledge_bases.py` | Add `GET /knowledge-bases/{kb_id}/agents` to the existing `router` (no new router, no new mount in `app.py`). Resolve the KB via `kb_service.get_knowledge_base`, apply the caller-tenant predicate, then delegate. | Same handler shape as `get_knowledge_base` two functions above it; `router` is already included at `app.py:55`. |
| `services/knowledge/tests/test_agent_kb.py` | Add service-level cases for `list_for_kb`. | Existing `agent_kb` service tests live here and already have the `tenant_agent` fixture. |
| `services/knowledge/tests/test_kb_agents_api.py` (new) | HTTP-layer tests for the new route: 200 own-tenant, 404 cross-tenant, 404 unknown id, 401 no header. | Knowledge Service has no route-level test file yet; follows `services/did/tests/test_numbers_api.py` (httpx `ASGITransport`, `Bearer {test_*['token']}`). |
| `admin-ui/lib/knowledgeApi.ts` | Add `KbAgent` interface + `listKbAgents(kbId)`. Nothing else. | The one permitted client addition (Constraints, bullet 1). |
| `admin-ui/components/AppShell.tsx` | Add `{ href: "/knowledge-bases", label: "Knowledge bases", icon: "knowledge-bases" }` to `MANAGEMENT_ITEMS` (after Agents) and one `ICONS` entry. | Constraint: same Management section as Agents; active state and client-side nav come free from the existing `nav-item` + `pathname.startsWith(item.href)` markup (AC1, AC17). |
| `admin-ui/app/knowledge-bases/page.tsx` (new) | Tenant-scoped KB list: name, status, document count, attached-agent count, row link to detail, "+ Add source" button, empty state, per-section error states. | AC1–AC4, AC15. |
| `admin-ui/app/knowledge-bases/[tenantSlug]/[kbId]/page.tsx` (new) | KB detail: documents table (status/error badge, usage-mode toggle, delete, upload) and attached-agents list with attach/detach. | AC8–AC12, AC16. Route shape mirrors `app/agents/[tenantSlug]/[agentSlug]/page.tsx`. |
| `admin-ui/components/AddSourceModal.tsx` (new) | 4-card picker; only "Upload files" enabled; target = existing KB or "create new"; exports `ACCEPTED_DOC_EXTENSIONS` / `ACCEPTED_DOC_ACCEPT` / `rejectionReasonFor(file)`. | AC5–AC7. The detail page's upload control imports the same validator so there is one client-side file-type rule, not two. |

No change to `KnowledgeBasePanel.tsx`, `retrieval.py`, `cache.py`, `agent_kb._refresh_flag`,
`ingestion_worker.py`, or any schema file.

## Data
None. `agent_knowledge_bases` already carries every row the reverse lookup reads; this is a query
direction, not new data. No migration, no index: the existing PK on `(agent_id, kb_id)` is not
usable for a `kb_id`-leading scan, but `agent_knowledge_bases` is a small edge table read once per
KB detail view — adding an index is not justified by this feature's load.

## Interfaces

### Backend
`services/knowledge/agent_kb.py`
```python
async def list_for_kb(kb_id: Any) -> list[dict[str, Any]]: ...
```
Query (mirrors `list_for_agent`, which is the row above it):
```sql
SELECT akb.agent_id, akb.kb_id, akb.enabled, akb.created_at,
       a.slug AS agent_slug, a.name AS agent_name, a.tenant_id
FROM agent_knowledge_bases akb
JOIN agents a  ON a.id = akb.agent_id  AND a.deleted_at IS NULL
JOIN tenants t ON t.id = a.tenant_id   AND t.deleted_at IS NULL
WHERE akb.kb_id = $1
ORDER BY a.name
```
The `deleted_at` filters matter: a soft-deleted agent must not appear as a dependant when an admin
is deciding whether a KB is safe to delete.

`services/knowledge/routers/knowledge_bases.py`
```python
@router.get("/{kb_id}/agents")
async def list_knowledge_base_agents(
    kb_id: str, current_user: CurrentUser = Depends(get_current_user),
) -> list[dict]: ...
```
Behaviour, in order:
1. `kb = await kb_service.get_knowledge_base(kb_id)` (already filters `deleted_at IS NULL`).
2. If `kb is None` **or** (`not is_platform_scoped(current_user)` and
   `str(kb["tenant_id"]) != current_user.tenant_id`) → `HTTPException(404,
   f"knowledge_base {kb_id!r} not found")` — byte-identical to the existing `get_knowledge_base`
   handler's 404, so cross-tenant and nonexistent are indistinguishable (lesson 2).
3. Return `await agent_kb_service.list_for_kb(kb_id)`.

`is_platform_scoped` is imported from `services.config.deps` (lesson 24: the scoping question is
`tenant_id is None`, never `role == "superadmin"` — the NULL-tenant viewer service accounts must
keep working). Read-only, so `get_current_user` not `require_role`: AC14 gives viewers read access.
A malformed non-UUID `kb_id` must produce a 404, not a 500 — `get_knowledge_base` passes the string
to asyncpg against a `uuid` column, so wrap step 1's call and map `asyncpg`'s invalid-input error to
the same 404.

### Frontend
`admin-ui/lib/knowledgeApi.ts`
```ts
export interface KbAgent {
  agent_id: string; kb_id: string; enabled: boolean; created_at: string;
  agent_slug: string; agent_name: string; tenant_id: string;
}
export const listKbAgents = (kbId: string) =>
  request<KbAgent[]>(`/knowledge-bases/${kbId}/agents`);
```

`admin-ui/components/AddSourceModal.tsx`
```ts
export const ACCEPTED_DOC_ACCEPT = ".txt,.md,text/plain,text/markdown";
export function rejectionReasonFor(file: File): string | null;   // null = acceptable
export function AddSourceModal(props: {
  tenants: Tenant[];            // from listTenants(); pre-selected when length === 1
  knowledgeBases: KnowledgeBase[];
  canManage: boolean;
  onClose: () => void;
  onCreated: () => void;        // parent re-fetches
}): React.ReactElement;
```
`rejectionReasonFor` checks the extension (`.txt`/`.md`, lower-cased) **and**, when the browser
supplies one, the MIME type against `text/plain`/`text/markdown` — extension alone is what the
`accept` attribute already filters, and AC7 requires an explicit pre-upload rejection because
`accept` is bypassable by drag-and-drop. It does not widen beyond
`ingestion_worker._SUPPORTED_CONTENT_TYPES`.

Create path reuses `createKnowledgeBase(tenantId, {slug, name, description})` then
`uploadDocument(kb.id, file, title)` — the identical pair `KnowledgeBasePanel.handleCreateKb` /
`handleUpload` use. Slug is derived from the name (lower-case, non-alphanumeric → `-`, collapse
runs, trim) into an editable field, so the user can fix a collision; a duplicate slug surfaces as
the API's own error in the modal, not as a silent retry.

### Page data flow
List page (`/knowledge-bases`), each `useEffect` branch independently try/caught:
- `listTenants()` → tenants (already tenant-scoped server-side; one tenant for a tenant user, all
  for a superadmin). Failure here is the only fatal one — render a page-level error.
- `Promise.allSettled(tenants.map(t => listKnowledgeBases(t.id)))` → rows, each tagged with
  `tenantName`/`tenantSlug`. Rejected entries add a non-blocking "couldn't load KBs for {tenant}"
  banner; fulfilled entries still render.
- `Promise.allSettled(kbs.map(kb => listDocuments(kb.id)))` → `docCountByKb`; a rejected entry
  renders "—" in that one cell, never an error page. `0` renders as `0` (AC4).
- `Promise.allSettled(kbs.map(kb => listKbAgents(kb.id)))` → `agentsByKb`; `0` renders as
  "Not used by any agent" (AC4).
- `getCurrentUser()` → `canManage = role === "superadmin" || role === "admin"`, matching
  `app/users/page.tsx:101`. `/auth/me` reads the database, so a demoted or soft-deleted user's
  stale token does not keep the write controls visible (lesson 27/35).

Detail page (`/knowledge-bases/[tenantSlug]/[kbId]`): `listTenants()` → tenant by slug → `tenantId`;
`listKnowledgeBases(tenantId)` → the KB by id (404-style "not found" state if absent);
`listDocuments(kbId)`, `listKbAgents(kbId)`, `listAgents(tenantSlug)` (for the attach picker,
minus the already-attached ids) each fetched and error-handled per section. Attach =
`assignKnowledgeBase(agentId, kbId)` (enabled defaults true, AC8); detach =
`detachKnowledgeBase(agentId, kbId)` behind a `confirm()` naming that documents are not deleted —
both the existing per-agent endpoints, so `_refresh_flag` keeps the Redis flag correct for free
(AC9, AC10). Each mutation re-fetches only `listKbAgents(kbId)`, not the page.

## Risks
- **Cross-tenant reads on the pre-existing Knowledge Service routes.** `list_knowledge_bases`,
  `list_documents` and `list_agent_knowledge_bases` accept any id from any authenticated console
  user with no tenant predicate — this page makes those routes reachable from a second surface but
  does not create the hole. *Mitigation:* the one route this feature adds carries the predicate, so
  the gap does not grow; raise the retrofit as its own ticket and name it in the handoff rather than
  silently expanding scope here. The new page itself never asks for another tenant's ids, since its
  tenant list comes from the already-scoped `GET /tenants`.
- **Stale tenant claim in the token.** The new route's boundary check reads `current_user.tenant_id`
  from the JWT, which `services/config/deps.py` never re-validates against the database (lesson 27).
  *Mitigation:* the route returns strictly less than `GET /tenants/{tid}/knowledge-bases` already
  returns to that same stale token, so it adds no new exposure; and the UI's write controls gate on
  `/auth/me`, which does hit the database. A structural `effective_user` re-read is a service-wide
  change, not a one-route patch.
- **N+1 fetches on the list page** (one `listDocuments` + one `listKbAgents` per KB) — a tenant with
  50 KBs issues ~100 requests. *Mitigation:* the PRD explicitly forbids an aggregate endpoint;
  `allSettled` keeps them independent and non-blocking, counts render as they resolve, and 50 KBs
  per tenant is far outside current data volumes. If it becomes real, the fix is one aggregate
  endpoint, not a redesign of this page.
- **Two client-side file-type rules drifting apart** (modal vs. the panel's `accept` attribute at
  `KnowledgeBasePanel.tsx:487`). *Mitigation:* the new modal and the detail page share one exported
  validator; the panel is deliberately untouched per Scope, and the backend's
  `_SUPPORTED_CONTENT_TYPES` remains the only enforcing gate.
- **Superadmin sees every tenant's KBs on one page**, which is correct (matches `/agents`) but makes
  a wrong-tenant delete easier. *Mitigation:* the table shows a Tenant column and the detail route
  is tenant-slug-addressed, so the tenant is visible at both the list and the confirm step.
- **`listTenants()` failing blanks the page**, since every other fetch depends on it. *Mitigation:*
  this is a genuine dependency, not a `Promise.all` bundling of independents; it renders an explicit
  "couldn't load accounts" error with a retry, never an empty table (AC2's empty state is reserved
  for a successful fetch returning zero KBs).

## Test plan

**Backend, service level** (`services/knowledge/tests/test_agent_kb.py`, DB-backed, `tenant_agent`
fixture):
1. `list_for_kb` returns both agents when a KB is attached to two — and after detaching one,
   returns exactly the other, with the KB row and its documents still present (AC9, AC10).
2. `list_for_kb` returns `[]` for a KB with no attachments, and excludes an agent whose
   `deleted_at` is set.

**Backend, HTTP level** (`services/knowledge/tests/test_kb_agents_api.py`, new, httpx
`ASGITransport` against `services.knowledge.app:app`):
3. Tenant admin gets 200 with their own KB's agents; a **different tenant's** admin gets 404 with
   the exact same detail string as an unknown `kb_id` (lesson 2). This is the case the query's own
   joins cannot reach — the KB and its agent share a tenant and the *caller* is elsewhere — so
   deleting the predicate from the handler must turn this test red and nothing else (lesson 12).
4. A NULL-tenant `viewer` service account gets 200 (lesson 24 — the predicate must not be
   role-based); a tenant `viewer` gets 200 for their own KB (AC14: reads are not admin-gated); no
   `Authorization` header is 401; a malformed non-UUID `kb_id` is 404, not 500.

**Frontend** (existing admin-ui test setup; unit where one exists, otherwise manual per lesson 23):
5. `rejectionReasonFor` rejects `notes.pdf` and `x.txt.exe`, accepts `readme.md` and `a.TXT`, and
   the modal sends no request when a rejected file is chosen (AC7).
6. List page with one tenant failing `listKnowledgeBases` and another succeeding: the succeeding
   tenant's rows render alongside a scoped error banner. Deleting the `allSettled` and restoring
   `Promise.all` must fail this test (AC15, lesson 21).

**Manual, in a browser** (lesson 23, and the only way to catch the class of bug that keeps landing
here): log in as a tenant `viewer` and confirm the nav item appears, the page loads, and no
create/attach/detach/delete control is clickable; log in as a tenant admin and run add-source →
create KB → upload `.md` → open detail → attach a second agent → detach it → confirm the KB still
lists with "0 agents"; then open that agent's own `KnowledgeBasePanel` tab and confirm it is
unchanged and consistent.
