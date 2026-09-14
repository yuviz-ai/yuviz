# Review: 02-design.md (Agentic API Task Execution)
VERDICT: GREEN

Both prior findings are resolved:

1. [was blocking, now closed] `_authorize_agent_api(agent_id, custom_api_id, current_user)` now gates every `/agents/{agent_id}/custom-apis…` route (list/PUT/DELETE) before any read or write. It joins `custom_apis ca ON ca.id = $2 AND ca.tenant_id = a.tenant_id` (rejects a wrong-tenant `custom_api_id`) and separately checks the resolved agent's `tenant_id` against the caller's (rejects a wrong-tenant `agent_id`), both collapsing to an identical-detail 404 (lesson 2). The test plan (`test_routes_auth.py`) explicitly asserts the write is rejected with **zero rows added to `agent_custom_apis`** for a wrong-tenant `custom_api_id`, a wrong-tenant `agent_id`, and on the `GET` list route — this is a real row-count assertion, not just a runtime-resolution-filter check, so it would catch a regression back to "fails silently at turn time only."

2. [was minor, now closed] SSRF validation is now a named code path, not just Risks prose: `custom_apis.py`'s `_validate_endpoint_url()` is cited in the `ValueError` catalogue as `invalid_endpoint_url` (https-only unless `TOOLEXEC_ALLOW_HTTP=true` for RFC1918 hosts, rejects loopback/link-local/metadata), and `executor.py` step 4 explicitly re-invokes the same function at call time, resolves the host itself, and pins the dialed IP with an explicit `Host` header — closing the DNS-rebind gap between registration-time and call-time checks. `test_custom_apis.py` adds concrete cases (`http://`, `169.254.169.254`, a hostname resolving to loopback).

No new defects introduced by the revision: the added `_authorize_agent_api` helper is consistent with the existing `_authorize_custom_api` / `_require_tenant_access` pattern (same 404-collapsing, same `is_platform_scoped` use per lesson 24), the SSRF re-check at call time is correctly placed before the request is built (no request sent on failure, per the existing "fail before HTTP" convention used elsewhere in the executor), and none of the previously-verified citations (schema columns, `types.py::__post_init__`, `agent_tool_policies` columns, `pipeline.py`'s fabrication guard) were altered in ways that break their earlier verification.

No unresolved findings.
