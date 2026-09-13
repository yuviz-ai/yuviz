# Review: SIP Trunk Onboarding — Purchase-to-Live-Call Connectivity PRD
VERDICT: AMBER

1. [blocking] AC5 and AC7 require the inbound test call to be judged pass/fail against "the stated timeout," but no timeout value is stated anywhere in the PRD — it exists only as unresolved Open Question 3 — so these two acceptance criteria are not testable as written. — .sdlc/sip-trunk-onboarding/01-prd.md:31,33,57 — fix: pick a default timeout value (even a placeholder like "5 minutes, tunable") and state it in Constraints or the AC itself, or explicitly mark AC5/AC7 as blocked-on-OQ3 until answered.
