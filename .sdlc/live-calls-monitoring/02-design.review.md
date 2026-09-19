# Review: 02-design.md (Live Calls Monitoring)
VERDICT: GREEN

No blocking or minor findings. All three round-1 items are resolved and cross-checked:

1. Selection-time re-validation is now explicit and one-time-per-selection/switch, not per-poll:
   `assert_selection_authority` is slug-keyed and memoized for 60s (`services/config/deps.py`
   interface, `_resolve_scope` docstring, Risks section), so the first poll of a selection and
   every switch hit the DB, while steady-state 5s polls of an already-validated selection do not.
   Test plan item 3 exercises both halves (soft-delete within the memo window still 200s on the
   same tenant, but 403 on switching to a different tenant, and 403 after TTL/on role change).
2. Test plan item 2 now asserts a byte-identical 404 for `GET /live-calls` with an own-tenant-
   mismatch `tenant_slug` vs. a nonexistent one (and separately for a superadmin's nonexistent
   slug), closing the gap that previously existed only for the intervention route's session_id.
3. `max_concurrent_calls` took the nullable-with-UI-prompt route, not backfill: schema is
   `INT` nullable with no default (deliberately unlike `campaigns.max_concurrent_calls`'s
   `DEFAULT 1`, confirmed at `database/schema.sql:555`), the KPI query returns
   `utilization_pct: null` / `max_concurrent_calls: null` when unset (test plan item 4 asserts no
   numeric fallback such as a `COALESCE(...,1)`), and the UI renders a "Channel cap not set"
   setup prompt rather than a number. Schema, query, and UI agree.

Spot-checked citations against the worktree: `services/conversation/session.py` transfer hooks at
lines 316/336/372/429 match the design's citation; `services/config/deps.py`'s `CONSOLE_ROLES`
and `get_authenticated_user`/`get_current_user` split match the design's description; PRD line 153
names exactly the two privileged moments ("granting Listen/Barge, selecting a tenant as a
platform-scoped account") the design's two re-validation helpers map to 1:1.

PRD's settled decisions remain honored: 5s hard refresh (Approach, page.tsx `REFRESH_MS = 5000`),
platform-scoped single-tenant-at-a-time (`_resolve_scope`'s single-slug return type, tripwire in
test plan item 3), Listen/Barge as request-and-audit-only (Scope decisions, `outcome:
"unavailable"`), sentiment explicitly deferred with a stated extension point (Scope decisions).
