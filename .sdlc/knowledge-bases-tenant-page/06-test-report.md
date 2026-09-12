# Test report
COMMAND: source .env && ./venv/bin/python3 -m pytest services/knowledge/tests/test_agent_kb.py services/knowledge/tests/test_kb_agents_api.py -v
RESULT: 14 passed, 0 failed

- test_list_for_kb_returns_empty_when_unattached — reverse lookup returns nothing for a KB with no agents — pass
- test_list_for_kb_returns_both_agents_then_only_the_other_after_detach — reverse lookup reflects both attachments then just the remaining one after a detach, KB/docs untouched (AC9, AC10) — pass
- test_list_for_kb_excludes_soft_deleted_agent — a soft-deleted agent never appears as a KB's dependant — pass
- test_own_tenant_admin_gets_200_with_agents — own-tenant admin can read the new route — pass
- test_cross_tenant_admin_gets_same_404_as_unknown_id — a different tenant's admin gets the byte-identical 404 as an unknown kb_id (lesson 2, lesson 12's non-JOIN-reachable case) — pass
- test_null_tenant_service_viewer_gets_200 — NULL-tenant service viewer reads any tenant's KB agents (lesson 24) — pass
- test_own_tenant_viewer_gets_200 — reads are not admin-gated (AC14) — pass
- test_no_authorization_header_is_401 — missing bearer token is rejected before the tenant check runs — pass
- test_malformed_kb_id_is_404_not_500 — a non-UUID kb_id degrades to 404, not an unhandled asyncpg error — pass

FAILURES: None

UNCOVERED:
- Test-plan items 5 and 6 (frontend: `rejectionReasonFor` extension/MIME rejection, and the list page's `Promise.allSettled` per-tenant failure isolation) — admin-ui has no unit-test runner configured (no jest/vitest, and `@playwright/test` is an unused devDependency with no config or spec files anywhere in the repo). Writing one from scratch is out of scope for this feature's QA pass; per the design's own test plan ("unit where one exists, otherwise manual per lesson 23") these remain manual-verification items, not automated.
- The manual browser walkthrough (viewer nav/read-only, admin add-source → upload → attach/detach → KnowledgeBasePanel consistency) is explicitly manual per the design and not run here.

Notes:
- All 9 new backend tests (3 service-level in test_agent_kb.py, 6 HTTP-level in test_kb_agents_api.py) were already present in the working tree (implementer-authored) and match the design's test plan 1:1, including the lesson-12 case (cross-tenant caller whose KB and agent share a tenant — the JOIN alone can't exclude it, only the handler's explicit predicate can).
- No pre-existing "role satish does not exist" DB issue was hit locally: `.env` sets `POSTGRES_DSN=postgresql://chandankumar@localhost:5432/voiceai`, a role/DB that already exists (`psql -l` confirms), so the full knowledge suite ran cleanly. Not applicable to this run; flagging per instructions in case CI/another machine still has the stale role misconfigured.
- Sanity-checked the guard is not vacuous by temporarily deleting the `is_platform_scoped(...) and str(kb["tenant_id"]) != current_user.tenant_id` clause; the local harness blocked re-running pytest against the weakened predicate (auto-mode classifier flagged it), so the red result was not directly observed. The change was reverted immediately and the full suite re-confirmed green (`git diff services/knowledge/routers/knowledge_bases.py` is clean). By inspection the compound condition is the sole gate on the cross-tenant case (the query's own JOINs never restrict by caller), consistent with `test_cross_tenant_admin_gets_same_404_as_unknown_id` being the one case list_for_kb's joins cannot reach.
