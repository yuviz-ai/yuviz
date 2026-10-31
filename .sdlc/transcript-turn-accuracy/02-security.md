# Security review: .sdlc/transcript-turn-accuracy/02-design.md
VERDICT: GREEN

## Findings
None. No cross-tenant, privilege, IDOR, token, injection, exposure or abuse path was found that the design introduces or leaves open.

## Verified controls
- `answering_agent_id` provenance: the handoff agent comes only from `agent_slugs`, built server-side by `services/config/call_flows.py:208-214` with `WHERE id = ANY($1) AND tenant_id = $2`. `callflow/handler.py:177` refuses any agent not in that map, so a foreign-tenant agent id cannot reach `_begin_call`. The unscoped `LEFT JOIN agents aa` in `get_call` therefore cannot surface another tenant's agent name.
- `_begin_call` upsert (`transcript_builder.py:297-318`): the new `answering_agent_id` SET stays behind the existing `WHERE calls.tenant_id = EXCLUDED.tenant_id`, so a session_id collision with another tenant's call cannot change that row.
- `record_summary`: it uses the same `tenant_conn(explicit_tenant=...)` plus `AND tenant_id = $3` shape as `_record_detected_languages` (`transcript_builder.py:339-351`). The tenant slug comes from the server-side `_tenant_slugs` map, not from client input.
- `record_live_stage` / `record_workflow_outcome`, now called from the call-flow handler, go through the same tenant-guarded `UPDATE ... AND tenant_id = $3` writers (`transcript_builder.py:353-363`).
- Reconcile `turn_count` subquery: it runs inside the existing node-scoped `platform_conn` UPDATEs (`transcript_builder.py:86-171`) and correlates on `calls.session_id`, the same correlation `reconcile_inactive_calls` already uses. No tenant input, and node scoping is unchanged.
- `list_calls` agent filter (`services/config/calls.py:63-67`): the new `OR c.answering_agent_id = $n` is ANDed with `c.tenant_id = $1` under `tenant_conn`. A foreign agent_id in `?agent_id=` returns nothing.
- `get_call` and `get_transcript` (`calls.py:100-148`): both keep `c.tenant_id = $2` with the tenant taken from the verified token (`routers/calls.py:26-34`). Moving the summary from transcript rows to `calls.session_summary` does not widen who can read it, because both endpoints use the same `get_current_user` gate (`routers/calls.py:149-165`).
- Dashboard predicate: the new `$3`/`$4` arrays are bound parameters from server constants, with no string interpolation. `WHERE tenant_id = $1` under `tenant_conn` is kept.
- `tool_calls` sink (AC21): only `name` plus the `ToolStatus.value` status are serialized, and arguments, results and errors are never passed. A test checks that card/PIN fixtures are absent from the stored JSON.
- Export (`calls.py:554-558`) uses an explicit column list and does not pick up `session_summary`, so the summary does not leak through CSV export.
- Schema change: additive `ADD COLUMN IF NOT EXISTS` plus a partial index, with no unique constraint, so no cross-tenant uniqueness DoS (lesson 3) and no destructive DDL (lesson 10).
