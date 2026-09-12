# Review: 01-prd.md
VERDICT: GREEN

No blocking or minor findings. Prior AMBER (document/chunk counts) is resolved: chunk counts are
dropped from scope with a cited reason (no endpoint in services/knowledge/ exposes kb_chunks
counts), and document count is now explicitly specified as client-side derived from the existing
`listDocuments(kbId)` call per row, with no new aggregate endpoint. Verified against the codebase:
`_SUPPORTED_CONTENT_TYPES = {"text/plain", "text/markdown"}` in
services/knowledge/ingestion_worker.py, `require_role("superadmin", "admin")` gates on all
knowledge_bases/agent_kb/documents mutation routes, the tenant-scoped list route uses
`get_current_user` only (matches AC1/AC14's "any role can view" claim), `list_for_agent`'s JOIN
does filter on `kb.deleted_at IS NULL` (matches AC16's claim), and `knowledgeApi.ts` already
exports `listKnowledgeBases`, `listDocuments`, `createKnowledgeBase`, `uploadDocument`. Nav
`matches()` in AppShell.tsx is a search filter, not a role gate, consistent with AC1.
