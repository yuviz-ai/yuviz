# PRD: Tenant-level Knowledge Bases page

## Problem
Knowledge bases already support many-to-many reuse across agents at the API layer
(`assignKnowledgeBase` / `listAgentKnowledgeBases`), but the only UI surface is
`KnowledgeBasePanel.tsx`, embedded inside a single agent's page. A tenant admin who wants to see
every KB they own, reuse one across a second or third agent, or check who else depends on a KB
before editing or deleting it has no page to do that from — they have to open every agent one at a
time and cross-reference by name. This blocks the exact reuse the data model already allows.

## How this is solved elsewhere
ElevenLabs Agents documents a tenant-wide knowledge base: a document (their unit of reuse) is
uploaded once and attached to multiple agents from a shared library, with per-document usage mode
(full-context vs RAG) same as our per-document `usage_mode`. Chatbase, by contrast, keeps sources
scoped per-chatbot with less emphasis on cross-agent reuse — closer to what our per-agent panel does
today. Our data model already reuses at the coarser KB level (a KB of many documents attaches as a
unit), which is the ElevenLabs shape one level up; recommendation is to keep that granularity rather
than rebuild around per-document attach, since the schema, API, and existing panel are already built
that way and changing the unit of reuse is out of scope for a UI-only feature. The 4-card "Add
source" picker (Upload files / Website crawl / Database or warehouse / SaaS connector) mirrors a
pattern common in ETL/ingestion tools (e.g. Fivetran-style connector pickers) rather than anything
specific to conversational-AI knowledge bases — fine to adopt visually, but per the request only
"Upload files" should be wired up; the other three ship as disabled "coming soon" cards with no
backend behind them, and this PRD does not invent that backend scope.

## Scope
- In: A new top-level "Knowledge bases" nav item and page, tenant-scoped, listing every KB owned by
  the tenant with name, status (`active`/`inactive`), document count, and the list of agents
  currently attached to it. Document count is computed client-side by calling the existing
  `listDocuments(kbId)` for each KB and counting rows returned — no new backend aggregate is added
  for it (see Constraints).
- In: An "Add source" modal reachable from the new page that creates a KB (or adds a document to an
  existing one) via a 4-card picker where only "Upload files" is functional; the other three cards
  render visibly disabled with a "coming soon" label and do nothing on click.
- In: A KB detail view reachable from the list, showing the KB's documents (with existing per-document
  status, usage-mode toggle, and delete, unchanged from `KnowledgeBasePanel.tsx`) and its attached
  agents, with the ability to attach an existing agent to this KB or detach one, from the KB side
  (not just the agent side as today).
- In: The existing per-agent `KnowledgeBasePanel.tsx` view keeps working unchanged — attach/detach,
  create-KB-and-attach, per-document upload/delete/usage-mode, and retrieval-policy editing all stay
  reachable from the agent page exactly as they are today. This feature adds a second, tenant-level
  entry point onto the same underlying data; it does not replace or remove the agent-level one.
- Out: Website crawl, database/warehouse, and SaaS connector ingestion. The cards render, are visibly
  disabled, and no backend work (crawler, connector auth, warehouse credentials, scheduled sync)
  ships in this feature.
- Out: Chunk counts. `kb_chunks` exists in the schema but no endpoint anywhere in
  `services/knowledge/` returns a chunk count, per document or per KB — `documents.py`,
  `vector_repository.py`, and `retrieval.py` all read `kb_chunks` internally for retrieval, never
  expose a count. Showing chunk counts would require a new aggregate query and endpoint, which is
  more backend surface than "keep changes minimal" allows for a UI-first feature reusing document
  count as the one available proxy. If chunk-level stats are wanted later, scope that as its own
  small addition.
- Out: Accepted upload file types beyond the current `.txt` / `.md` (`text/plain`, `text/markdown`).
  `services/knowledge/ingestion_worker.py`'s `_SUPPORTED_CONTENT_TYPES` set is exactly those two
  today; there is no PDF/DOCX parser in the codebase to widen into. Assumption: the request's ask to
  "widen accepted file types to whatever the backend already parses" resolves to "no widening" once
  checked against the backend, not to adding PDF/DOCX parsing — that would be new ingestion scope,
  which contradicts the "keep backend changes minimal" instruction. If PDF/DOCX support is wanted,
  it is a separate, larger feature (new parser dependency, content-type allowlist change, and its own
  test coverage) and should be scoped separately.
- Out: API/tool connections. They remain the existing separate per-agent tab; no tenant-level
  "API connections" page is built now, per the request.
- Out: Any change to the KB/document/agent-KB data model, retrieval policy behavior, or the
  Redis-backed `has_enabled_kb` cache flag in `services/knowledge/agent_kb.py` — this feature only
  adds a read surface and reuses existing write paths.

## Acceptance criteria
1. Given a logged-in tenant user with any role, when they open the console, then a "Knowledge bases"
   nav item is visible in the same nav section as "Agents", and navigating to it loads a page scoped
   to their current tenant only (no cross-tenant KBs ever render).
2. Given a tenant with zero knowledge bases, when the Knowledge bases page loads, then it renders an
   empty state with a call to action to add the first source, not an error or a blank table.
3. Given a tenant with N knowledge bases, when the page loads, then it lists all N via the existing
   `listKnowledgeBases(tenantId)` call, each row showing name, status, a document count derived
   client-side from `listDocuments(kbId)` for that row's KB, and the count (and names, on expand or
   in detail view) of agents currently attached. No chunk count is shown on this page.
4. Given a KB with zero attached agents, when its row is rendered, then it shows "0 agents" / "Not
   used by any agent" rather than omitting the field or erroring; given a KB with zero documents, its
   document count shows "0" the same way.
5. Given the "Add source" modal is opened, when it renders, then exactly one of the four cards
   ("Upload files") is clickable/enabled; "Website crawl", "Database or warehouse", and "SaaS
   connector" are visibly present, visually disabled, and labeled "coming soon", and clicking them
   does nothing (no network call, no navigation).
6. Given "Upload files" is selected in the Add source modal, when the user picks a target (an
   existing KB, or "create new"), then the resulting create/upload flow uses the existing
   `createKnowledgeBase` / `uploadDocument` endpoints and enforces the same `.txt`/`.md` file-type
   restriction as today's panel — the modal is a new entry point onto the same validated flow, not a
   new validation path.
7. Given a file outside the accepted types is dropped or selected, when the user attempts to
   proceed, then the UI rejects it client-side with a clear message before any upload request is
   sent (mirrors current `accept=".txt,.md,text/plain,text/markdown"` behavior).
8. Given a tenant user opens a KB's detail view, when they select an agent from an "attach" control,
   then the agent becomes attached (enabled by default) via the existing per-agent assign endpoint,
   and the detail view's agent list refreshes to include it without a full page reload.
9. Given a KB is attached to 2+ agents, when the user detaches one agent from the KB detail view,
   then only that agent's `agent_knowledge_bases` row is removed, the KB itself and its documents are
   unaffected, and the other attached agent(s) keep functioning (retrieval unaffected for them).
10. Given a user detaches the last remaining agent from a KB, when the detach completes, then the KB
    itself is not deleted and still appears in the tenant-level list with "0 agents".
11. Given a KB detail view is opened for a KB with documents, when documents are listed, then each
    document shows the same status, error, and "always include in prompt" (`usage_mode`) controls
    that `KnowledgeBasePanel.tsx` already exposes per-agent, backed by the same `listDocuments`,
    `updateDocument`, and `deleteDocument` calls.
12. Given a document upload is in a `processing` or `failed` state, when the tenant-level detail view
    renders it, then the status badge and any `error` text are visible, matching current per-agent
    behavior, not hidden behind the new page.
13. Given the tenant-level page needs "agents using this KB" and no existing endpoint returns agents
    by `kb_id` (`listAgentKnowledgeBases` only queries by `agent_id`; confirmed no reverse lookup
    exists in `services/knowledge/agent_kb.py` or its router), when this feature ships, then exactly
    one new read-only endpoint is added to serve that reverse lookup, scoped to the KB's own tenant,
    and no other new backend endpoint is introduced (document counts are satisfied by the existing
    `listDocuments` endpoint called per KB, not a new one; chunk counts are out of scope per Scope).
14. Given a non-superadmin/non-admin tenant user (matching the existing `require_role("superadmin",
    "admin")` gate on KB create/update/delete and the agent-KB assign/detach routes), when they open
    the new page, then they can view KBs, documents, and attached-agent lists, but create, delete,
    attach, and detach controls are disabled or hidden for them exactly as the existing per-agent
    panel already restricts those actions today.
15. Given the KB listing, document listing, or attached-agents fetch on the new page fails
    independently (e.g. one fetch 403s or 500s while the others succeed), when the page renders, then
    the failing section shows its own error state and the sections that succeeded still render their
    data — no single failed call blanks the whole page (no `Promise.all`-style all-or-nothing fetch
    across independent tenant-scoped calls).
16. Given a KB is deleted from the tenant-level page, when the delete completes, then it is removed
    (soft-deleted per `soft_delete_knowledge_base`) from the tenant list, and any agent still showing
    it as attached in the agent-level `KnowledgeBasePanel` no longer lists it after that panel's next
    refresh (since `list_for_agent`'s existing JOIN already filters on `kb.deleted_at IS NULL`).
17. Given the Knowledge bases nav item, when clicked by a user on any other page, then the app
    navigates client-side (no full reload) and the item shows an active state, consistent with the
    other `nav-item`s in `AppShell.tsx`.

## Constraints
- Tenancy: every read and write must go through the tenant-scoped or agent-scoped paths that already
  exist (`GET /tenants/{tenant_id}/knowledge-bases`, `/agents/{agent_id}/knowledge-bases`,
  `/knowledge-bases/{kb_id}`, `/knowledge-bases/{kb_id}/documents`) — do not introduce an
  un-tenant-scoped "list all KBs" route.
- Auth/roles: mutation routes already gate on `require_role("superadmin", "admin")`
  (`services/knowledge/routers/knowledge_bases.py`, `routers/agent_kb.py`); the new page must respect
  the same gates client-side and must not assume a role the API will reject.
- Exactly two new pieces of backend surface are introduced by this feature, and no others:
  1. A new `GET` returning agents attached to a `kb_id`, scoped by the KB's own `tenant_id`, following
     the existing handler shape in `services/knowledge/routers/knowledge_bases.py` and
     `knowledge_bases.py`. No schema change — `agent_knowledge_bases` already has the rows this
     needs; it is a query direction, not new data.
  2. Nothing else. Document counts are derived client-side from the existing `listDocuments(kbId)`
     response (its length), one call per KB rendered — no new count/aggregate endpoint. Chunk counts
     are dropped from scope entirely rather than adding a second new endpoint; see Scope.
- Nav: add "Knowledge bases" alongside "Agents" in the "Management" nav section of
  `admin-ui/components/AppShell.tsx`, following the existing `nav-item` markup and active-state
  pattern (`pathname.startsWith(item.href)`); do not invent a new nav section for it.
- File types: do not widen `accept` beyond `.txt,.md,text/plain,text/markdown` anywhere in this
  feature — `services/knowledge/ingestion_worker.py:_SUPPORTED_CONTENT_TYPES` is the backend source
  of truth and it only supports those two today.
- Reuse `admin-ui/lib/knowledgeApi.ts` as-is for every capability it already exposes; only add a
  client function for the one new reverse-lookup endpoint (agents-by-kb_id).
- Do not touch `services/knowledge/retrieval.py`, the Redis `has_enabled_kb` cache, or
  `agent_kb.py`'s `_refresh_flag` — this feature is additive UI plus one additive read endpoint, not
  a change to what gets retrieved at conversation time.
- Fetch independent tenant-scoped calls independently (per-section try/catch), not via a single
  `Promise.all` that fails the whole page on one 403 — same failure shape already called out for this
  codebase.

## Open questions
None — the request, the existing `knowledgeApi.ts` contract, and the backend's actual supported
content types fully determine the scope above.
