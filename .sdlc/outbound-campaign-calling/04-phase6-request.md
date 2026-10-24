# Phase 6: Capacity API and campaign UI

Build the platform outbound limits API, per-tenant capacity fields, and the campaign management UI for capacity, readiness, and contact attempts.

## Deliverables

1. **Capacity API routes**: `GET|PUT /platform/outbound-limits` with CPS routes and per-provider settings; `PUT|GET /tenants/{id}/outbound-cap` gated to superadmin only; all writes audited with old/new values
2. **Admin UI capacity card**: show live concurrent call usage, CPS usage, platform limits and tenant limits side by side
3. **Campaign create/edit forms**: retry gap field (min 1, default 60), enforce max_attempts>=1, caller-ID select with provider badge (native/Vobiz/Cloudonix)
4. **Campaign detail page**: readiness panel with blocks (red), warnings (amber), and live wait_reason with waiting_count; Start/Resume buttons disabled when blocks exist, return 409 with checks when blocked
5. **Live attempts list**: show attempts under each contact with outcome, disposition (for answered calls), masked phone number (no full digits), retry count, attempt timestamp
6. **Estimated cost preview** (when billing enabled): show per-contact and per-campaign cost estimate

## Acceptance Criteria

- All routes: 403 to non-superadmin, same 404 as random UUID to foreign-tenant calls
- Schema constraints enforced with 400 response
- UI builds without errors
- UI renders live usage, readiness panel, and contact attempts correctly
- No response contains full callee number; masked form only
- Every write creates an audit row with old/new values
- Tests cover all readiness blocks and foreign-tenant cases
