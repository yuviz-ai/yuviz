# Review: 01-prd.md (re-review)
VERDICT: GREEN

Prior blocking findings: both resolved.
- cal.com: criteria 35-38 now define its capabilities (availability, booking, failure, connect/audit), marked contingent on open question 2, with a delete-if-out rule in Scope.
- Scope vs Problem: Scope now has a "committed vs contingent" section. Write-back criteria 27-31 are labelled contingent on open question 3, and the Problem section says the PRD carries both possibilities.

1. [minor] Criterion 38 requires a booking audit "same as criterion 30 regardless of the answer to open question 3", but criterion 30 is contingent on open question 3 and would move out if v1 is read-only. Cal.com's audit requirement would then point at a deleted criterion. — Acceptance criteria 30 and 38 — fix: state the audit-entry fields inside 38 itself, or take 30 out of the contingent set.
2. [minor] Criterion 31 is listed as contingent but says it still applies if v1 is read-only. Criteria 8 and 12 also mention write-back or create/log while being committed. — Scope contingency list, criteria 8, 12, 31 — fix: move 31 to committed, and word 8 and 12 as "if write-back exists".
