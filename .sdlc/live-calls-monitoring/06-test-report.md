# Test report

COMMAND: `POSTGRES_DSN="postgresql://chandankumar@localhost:5432/voiceai" /Users/chandankumar/yuviz/venv/bin/python3 -m pytest services/config/tests/test_live_calls.py services/config/tests/test_console_gate.py services/config/tests/test_tenants.py services/conversation/tests/test_session_live_stage.py -q`

RESULT: 71 passed, 2 failed (both expected/known, see below)

Baseline confirmed first: `services/config/tests/ services/conversation/tests/` — not re-run in full here (out of scope for this area); the area-scoped baseline (`test_live_calls.py` + `test_console_gate.py`) was 57 passed before this session's changes.

## New tests added (5, all in `services/config/tests/test_live_calls.py`)

- `TestAuthorityMemoBounds::test_post_intervention_404_should_not_recache_the_evicted_scope_key` — pins that a 404'd scope_key must not be re-cached by `POST /interventions` — **FAIL (expected/known)**
- `TestConcurrencyEndpoint::test_admin_can_update_own_tenant_concurrency_and_next_poll_reflects_it` — AC16: admin PATCHes own tenant's cap, next poll's utilization_pct proves cache invalidation — pass
- `TestConcurrencyEndpoint::test_admin_cannot_update_another_tenants_concurrency` — AC16: admin PATCHing a foreign tenant gets the non-oracle 404 — pass
- `TestConcurrencyEndpoint::test_viewer_403s_on_concurrency_patch` — AC16: `viewer` is rejected before any write — pass
- `TestConcurrencyEndpoint::test_patch_concurrency_on_soft_deleted_tenant_should_404` — pins that PATCHing a soft-deleted tenant must not silently succeed — **FAIL (expected/known)**

## FAILURES

Both failures are pinned regressions for findings already surfaced and accepted at 05-security.md, **not new defects** — do not route these into an implementer-fix cycle the way a genuine new regression would:

1. `test_post_intervention_404_should_not_recache_the_evicted_scope_key` — KNOWN, accepted, low severity. `_resolve_scope`'s 404 branch calls `deps.forget_authority()` to evict the memo entry, but `routers/live_calls.py::request_intervention`'s except-handler then calls `deps.fresh_authority()` again with the identical `scope_key` (to attribute the denial-audit row), which re-reads the row and re-inserts exactly the entry that was just evicted — undoing the eviction bound on the POST path only (the GET path has no such re-call and stays clean, per the pre-existing `test_a_404d_slug_is_not_retained_in_the_memo`, still green). Confirmed actual behavior: the memo contains `(user_id, other_tenant_slug)` after the 404 response.
2. `test_patch_concurrency_on_soft_deleted_tenant_should_404` — KNOWN, accepted, medium severity. `PATCH /tenants/{tenant_id}/concurrency`'s own-tenant check re-reads the ACTOR's row but never checks whether the TARGET tenant is soft-deleted; `tenants_service.update_tenant()`'s `SELECT * FROM tenants WHERE id=$1 FOR UPDATE` carries no `deleted_at IS NULL` predicate — disagreeing with `_resolve_scope`'s own 403 for the identical shape elsewhere in this same feature (`TestSoftDeletedOwnTenant`, still green). Confirmed actual behavior: superadmin PATCH on a soft-deleted tenant returns 200 and writes `max_concurrent_calls=5` to the deleted row.

No other failures. No genuinely new defect was found by this session's tests.

## UNCOVERED

- **AC8 (CSV export) and AC2 (superadmin tenant picker)** — no automated frontend test exists, and none was added. Checked `admin-ui/package.json`: `@playwright/test` is listed as a devDependency but there is no `playwright.config.*`, no test npm script, and no `*.spec.*`/`*.test.*` file anywhere outside `node_modules` — the runner is not actually wired up, matching the design's own Test-plan note ("Frontend (manual, driven in a browser — lesson 23; no test runner exists for admin-ui)"). These two ACs were verified manually per T22b and remain uncovered by automated tests, as designed, not as an oversight.
- Everything else in the design's numbered Test plan (items 1–8, plus the console-gate regression item) has a matching test in `services/config/tests/test_live_calls.py` / `test_console_gate.py` / `services/conversation/tests/test_session_live_stage.py` — no other gaps found.

## Notes on what was cross-checked

- Design test-plan item 8 ("AC16 concurrency edit … same admin PATCHing another tenant → 404; a `viewer` → 403") had **no test anywhere in the suite** before this session — `services/config/tests/test_tenants.py` had zero tests for `PATCH /tenants/{tenant_id}/concurrency`. The three passing tests above close that gap; the fourth (soft-deleted tenant) surfaced the known accepted bug instead.
- T7's demotion test (`test_demotion_stops_snippet_only_after_the_shipped_60s_ttl`) and T13's soft-delete test (`test_soft_delete_403s_after_the_shipped_60s_ttl`) both use `asyncio.sleep(deps.AUTHORITY_MEMO_TTL_S + 1)` against the real shipped constant (60s), not a shortened one — confirmed present and passing (they account for most of the ~130s runtime, per lesson 25).
- `test_console_gate.py::test_live_calls_routes_match_role_allowlist` already covers the design's "console-gate regression" test-plan line (fails if `LIVE_CALLS_ROLES` gains `viewer` or a third route lands under the gate) — confirmed present and passing, no gap.
