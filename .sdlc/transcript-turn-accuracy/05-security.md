# Security review: transcript-turn-accuracy (code, base a841bf1)
VERDICT: GREEN

## Findings
None. No carried item is tagged security_design. The carried items are correctness, persistence-accuracy and test-coverage issues with no attacker or tenant-boundary angle. The code critics own them.

## Verified controls
- `get_call`, `get_transcript` and `list_calls` take the tenant from the verified token via `_caller_tenant_slug(current_user)` (`services/config/routers/calls.py:150-160`), never from the request. The new `answering_agent_id` filter in `list_calls` is ANDed with the existing `c.tenant_id` predicate and is parameterized (`services/config/calls.py:64-67`).
- The tenant `get_call` query keeps `WHERE c.session_id = $1 AND c.tenant_id = $2`, so the new `aa` join cannot return a row from another tenant's call.
- `answering_agent_id` can only hold an agent from the same tenant. `agent_slugs` is filtered by `agents.tenant_id = $2` (`services/config/call_flows.py:208-214`), and `handoff` resolves the delegate by slug under `runtime_config.tenant.slug` (`services/conversation/__main__.py:499-507`). This means the `aa.name` join cannot leak a foreign agent's name.
- The `_begin_call` upsert keeps `WHERE calls.tenant_id = EXCLUDED.tenant_id`, so a session_id collision from another tenant cannot set `answering_agent_id` (`services/conversation/transcript_builder.py:330-333`).
- `get_agent_call_stats` is scoped by `c.tenant_id = $1`. As defence in depth, the lateral join counts an answering agent only when its tenant's slug matches the call's tenant (`services/config/calls.py:343-356`).
- `get_dashboard_stats` keeps `WHERE tenant_id = $1` inside the new subquery. The new disposition and reason arrays are bound parameters, not interpolated SQL (`services/config/calls.py:245-253`).
- `_record_summary` writes under `tenant_conn(explicit_tenant=...)` with `AND tenant_id = $3` (`services/conversation/transcript_builder.py:378-388`). The `transcript_entries` insert keeps its `EXISTS ... tenant_id = $14` guard.
- `tool_calls` persistence holds only the tool name and status, never arguments (`services/conversation/transcript_builder.py:216-219`). The pipeline builds entries only from `event.tool_name` and `ToolCallFinishedEvent.status`, so card or PIN arguments never reach `transcript_entries`.
- The reconcile sweeps' `turn_count` subquery is correlated on `calls.session_id`, so it cannot count another session's or another tenant's rows (`services/conversation/transcript_builder.py:93-157`).
- The admin UI renders `session_summary` and `answering_agent_name` as React text nodes with no `dangerouslySetInnerHTML`, so LLM-generated summary text cannot cause XSS (`admin-ui/app/(console)/calls/[sessionId]/page.tsx:157-162,241-246`).
- `session_summary` is served by `GET /calls/{id}` and `GET /calls`, which have the same `get_current_user` guard as the transcript route it came from. No role sees content it could not see before.
- The new log lines in pipeline and servicer log session ids and dispositions only. No caller text, tool arguments or summary text is logged.
