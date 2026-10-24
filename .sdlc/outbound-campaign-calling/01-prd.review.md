# Review: 01-prd.md (outbound campaign calling), final check
VERDICT: AMBER

Resolved: the state paragraph (line 32) and criterion 40 now state the same single exception (deadline-closed `failed` with no call record may be upgraded to `answered` or `ended` by a late event for the same contact:attempt), matching criterion 12. No new contradiction. Orato.ai is dropped with a stated reason (line 8) and Strada is removed.

1. [minor] Recovery by state leaves Path A `sent` with no Redis claim and no call record undefined. — criteria 16-18 — fix: state the action (no re-send under the same key; resolve as failed at the deadline; retry with attempt+1).
2. [minor] Criterion 11 treats "no call record" as failed at the deadline, but on Path A a ringing call may have no call record yet, so concurrency can exceed N until criterion 12 corrects it. — criteria 8, 11 — fix: define the live-call signal for Path A.
3. [minor] Criterion 12 says the outcome is corrected to `answered` only, while the state paragraph and criterion 40 also allow `ended`. — criterion 12 — fix: say "answered or ended".
4. [minor] Carried from round 1: criterion 30 has no numeric latency threshold or named benchmark. There is no CSV phone validation or dedupe criterion. Readiness does not check for a cross-tenant agent. Pause semantics for in-flight attempts are unspecified. — criteria 1, 30, Scope — fix: add a latency delta, E.164 validation and dedupe, a blocking readiness check for an agent not in the tenant, and the pause rule for in-flight attempts.
