# Phase 6 PRD Review

**Verdict: AMBER** — PRD is mostly ready for design with minor gaps to flag; no blocking issues, but one open question should ideally be resolved before design starts.

---

## Summary

The PRD is well-structured with 36 testable acceptance criteria, clear scope boundaries (correctly excludes Phase 7 and 8), and well-documented constraints tied to architectural lessons. The backend infrastructure (capacity.py, audit.py, readiness.py, billing seam) largely exists. However, three areas need attention before implementation starts:

1. **Billing seam incomplete**: `estimate_campaign()` raises NotImplementedError; Phase 6 needs a working version or a plan for mocked data.
2. **Open question #1 should be resolved**: Tenant-admin read visibility into their own cap and usage could significantly change the API surface.
3. **Minor ambiguities** in criteria 16, 19, and 28 that are testable but need clarification during design.

---

## Detailed Findings

### 1. **BLOCKING-ADJACENT: Billing Seam Not Ready** ⚠️
**Severity: Minor** | **Impact: Acceptance Criteria 31–32 depend on this**

**Finding:**
- `services/campaigns/billing.py:estimate_campaign()` (line 112–117) raises `NotImplementedError`.
- Acceptance criteria 31–32 require the readiness panel to display cost estimates when billing is enabled.
- The PRD states Phase 8 will replace the implementation, but Phase 6 needs to call it.

**Current State:**
```python
async def estimate_campaign(...) -> CostEstimate | None:
    if not billing.enabled:
        return None
    raise NotImplementedError("pre-start cost estimates arrive with the billing implementation")
```

**Recommendation:**
Before design starts, decide whether Phase 6 should:
- (A) Mock `estimate_campaign()` to return a dummy CostEstimate for testing criteria 31–32.
- (B) Defer billing-panel rendering to Phase 8 and test criteria 31–32 only with billing disabled.
- (C) Require Phase 8's billing implementation to land first (dependency risk).

**Action:** Clarify with product owner before design gate. Does not block design, but affects test plan.

---

### 2. **DESIGN-TIME DECISION: Tenant-Admin Visibility (Open Question #1)** ⚠️
**Severity: Minor** | **Impact: API scope and role-dependent fetch logic**

**Finding:**
Open question #1 asks whether a tenant admin can read (not write) their own tenant's outbound cap and usage. The PRD assumes "no" (superadmin reads only), but the assumption is stated as an option that "could change."

**Current state:**
- Criterion 74 (superadmin tenants page, not shown in excerpt) implies tenant cap is not visible to tenant admins.
- The readiness warning already signals when a tenant cap overrides campaign concurrency, but only as a warning, not a visible number.

**Risk:**
If decided during design or later to allow tenant-admin reads, the routes and role checks need redesign. The PRD then omits the tenant-facing read route entirely.

**Recommendation:**
Resolve this before design. Options:
- (A) **Confirmed "no"**: tenant admins see only readiness warnings; no separate cap-read route. (Lower effort, matches current assumption.)
- (B) **Confirmed "yes"**: add a tenant-admin-visible read route for their own cap and usage, gated by tenant check (higher effort, adds API surface).

**Action:** Get product owner sign-off on the assumption. If changing, update criteria 74 and add a new criterion for tenant-facing visibility.

---

### 3. **MINOR: Criterion 28 References Potentially Nonexistent Test**
**Severity: Minor** | **Impact: Test plan clarity**

**Finding:**
Criterion 28: "Given a campaign id belonging to another tenant... then status and body equal those for a random UUID on every route (existing route-walk test stays green with any new campaign route added)."

The phrase "existing route-walk test stays green" assumes a route-walk test already exists. A search found `test_app_lifespan.py` which has:
```python
paths = {getattr(r, "path", "") for r in app_module.app.routes}
```

But there is no explicit route-count test or foreign-tenant test enumeration for campaigns routes. Criterion 28 is testable, but it depends on either (A) an existing test, or (B) building one as part of Phase 6.

**Recommendation:**
Clarify in design: is the route-walk test already in place, or does it need to be added to campaigns' test suite? This is a mechanical but necessary test per lesson 12.

---

### 4. **MINOR: Criterion 19 Requires 10 Block-Check Test Cases (Not 9)**
**Severity: Minor** | **Impact: Test coverage and effort estimation**

**Finding:**
Criterion 19 lists 9 block check IDs to test:
> "agent_unavailable, caller_id_missing, caller_id_not_owned, caller_id_not_routed_to_agent, no_pending_contacts, timezone_invalid, provider_not_ready, provider_not_campaign_capable, no_healthy_sip_node"

However, `services/campaigns/readiness.py:CheckId` defines 10 check IDs, including `campaign_cap_above_tenant_cap` (a warning, not a block):

```python
CheckId = Literal[
    "agent_unavailable", "caller_id_missing", "caller_id_not_owned", "caller_id_not_routed_to_agent",
    "no_pending_contacts", "timezone_invalid", "provider_not_ready", "provider_not_campaign_capable",
    "no_healthy_sip_node", "campaign_cap_above_tenant_cap",
]
```

**Issue:**
The criterion explicitly tests "every block check id" but omits `campaign_cap_above_tenant_cap`. This check is actually a warning (level="warn"), not a block, and it's already referenced in the "Out" section's scope and in the constraints (lesson 10).

**Recommendation:**
Update criterion 19 to clarify that it tests all block check IDs (excluding warnings), or explicitly list the 9 block IDs to avoid ambiguity. The test data setup for 9 separate block conditions is labor-intensive; product/design should confirm this level of coverage is necessary.

---

### 5. **MINOR: Criterion 16 Semantics — No-Op Audit Trail**
**Severity: Minor** | **Impact: Clarification only**

**Finding:**
Criterion 16: "Given a saved campaign, when the edit form loads, then retry gap, max attempts and caller ID show the stored values, and saving unchanged values changes nothing other than an audit-trailed no-op update."

This explicitly requires that unchanged saves generate an audit row. This is unusual (most systems skip audit if nothing changed) but it's a deliberate design choice. The criterion is testable, but the phrase "nothing other than an audit-trailed no-op" could confuse implementers.

**Recommendation:**
This is correct as written, but verify during design that the intent is truly "audit every save, even no-ops" vs. "skip audit if nothing changed." The constraint section (line 79) references lesson 8 (atomic updates) but doesn't clarify the no-op case.

---

### 6. **MINOR: Log and Audit Sanitization Guidance**
**Severity: Minor** | **Impact: Implementation clarity**

**Finding:**
Criterion 30: "Given the logs produced by these routes and the audit rows, then neither contains a full callee number."

This constraint is broader than just API responses (criterion 27). The audit module must sanitize `old_value` and `new_value` JSONb fields if they contain callee numbers. However, `audit.py:write_audit()` accepts `old_value` and `new_value` as dicts with no sanitization.

**Current code:**
```python
async def write_audit(
    conn: asyncpg.Connection,
    *,
    ...
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    ...
) -> None:
    await conn.execute(
        "INSERT INTO audit_log ... VALUES (..., $6::jsonb, $7::jsonb, ...)",
        ...
        json.dumps(old_value, default=str) if old_value is not None else None,
        json.dumps(new_value, default=str) if new_value is not None else None,
        ...
    )
```

**Recommendation:**
Clarify in design whether:
- (A) Callee numbers should never be written to audit (no one should call write_audit with contact data).
- (B) The write_audit function should sanitize callee/caller IDs in old_value and new_value before writing.
- (C) Callee/caller IDs are never touched by Phase 6 routes (contacts list changes are Phase 7, updates to campaign fields only).

Presumed answer: (C) — Phase 6 routes touch campaign fields (cap, gap, attempts, caller_id), not contact data, so no callee in audit. But make this explicit.

---

### 7. **MINOR: Criterion 24 — Partial-Failure Error Handling Specificity**
**Severity: Minor** | **Impact: UI implementation detail**

**Finding:**
Criterion 24: "Given the readiness, progress and attempts requests are independent, when one returns 403 or 500, then the other sections still render and the failed section shows its own error."

This is correct and testable, but the criterion doesn't specify error-handling detail:
- Should the three requests be fetched in parallel with Promise.all, or individually with fallback?
- Lesson 21 says "Promise.all must not be used across role-dependent fetches"; readiness and progress are role-dependent (viewer sees no write buttons).

**Recommendation:**
Design should address this: separate the role-dependent fetch (progress, which drives Start/Resume buttons) from the data-only fetch (attempts). This is already covered by lesson 21, but the constraint section (line 82) should explicitly call it out for Phase 6.

---

## Scope Assessment ✓

**Phase 7 and 8 boundaries are correct:**
- Phase 7 (deferred): business outcomes, DNC management UI, per-status retry rules, callbacks, compliance, CRM sync.
- Phase 8 (deferred): billing implementation (charging, balances, credit checks).
- Phase 6 scope: display estimates, not charge them. ✓

**Tenant-admin visibility boundary is documented but not finalized:** Open question #1 defines the boundary assumption (superadmin-only). Recommend resolving before design.

---

## Constraints Assessment ✓

Constraints are clear and well-referenced to architectural lessons:
- Lesson 2 (tenant isolation): invariant status/body, no IDs echoed. ✓
- Lesson 8 (atomic updates): single conditional write with audit. ✓
- Lesson 12 & 29 (mechanical route enumeration): guards awaited, routes walkable. ✓
- Lesson 17 (caller search): grep across admin-ui, scripts, docs. ✓
- Lesson 21 (Promise.all separation): not across role-dependent fetches. ✓
- Lesson 36 (tenant_id predicate): use tenant_id, not RLS, for new joins. ✓
- Lesson 38 (guard await): all awaited, none fire-and-forget. ✓

All constraints are actionable. Recommend adding explicit guidance on lesson 21 for Phase 6's multi-section fetch pattern (criterion 24).

---

## Acceptance Criteria Assessment

**Testable:** 35 of 36 are clearly testable with concrete inputs and outputs.
- Criterion 19 needs clarification (9 vs. 10 checks, and test data complexity).
- Criterion 28 needs confirmation of existing test.
- All others are straightforward.

**Complete:** Covers API (12 criteria), forms (5), detail page (13), cost (3), console (3). No obvious gaps in the scope.

**Potential coverage gaps:**
- Callee masking in attempts (criterion 27) is explicit; callee masking in audit (criterion 30) is not.
- Role-based UI rendering (superadmin sees capacity card, tenant admin doesn't) is criterion 74 (superadmin tenants page not in excerpt) but not explicitly covered for campaign detail. Recommend adding a criterion for campaign-detail role rendering (viewer sees no buttons, admin sees all).

---

## Recommendation Summary

| Item | Status | Action |
|------|--------|--------|
| Billing seam (estimate_campaign) | ⚠️ Not ready | Clarify mocking vs. dependency before design gate |
| Open question #1 (tenant-admin visibility) | ⚠️ Unresolved | Get sign-off on "superadmin-only" assumption |
| Criterion 28 (route-walk test) | ✓ Clarifiable | Confirm existing test in design |
| Criterion 19 (block checks) | ✓ Clarifiable | Confirm all 10 checks vs. 9 blocks in design |
| Log/audit sanitization | ✓ Clarifiable | Confirm no callee in audit writes in design |
| Promise.all pattern (lesson 21) | ✓ Clarifiable | Document fetch pattern in design |
| **Overall readiness** | **AMBER** | **Design phase: minor gaps to flag, not blocking** |

---

## Verdict

**GREEN for design with AMBER flags.** The PRD is well-written, acceptance criteria are mostly testable and complete, and scope is correctly drawn. The three minor gaps (billing seam, open question, test clarity) can be resolved during design without rework. Recommend:

1. **Before design gate:** Confirm billing seam plan and tenant-admin visibility assumption.
2. **During design:** Clarify route-walk test, block-check coverage, and fetch patterns.
3. **During build:** Implement audit sanitization and role-based UI per constraints.

No changes required to PRD text for design to proceed; these are design-time clarifications, not PRD bugs.
