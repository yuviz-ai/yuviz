# Review: 03-tasks.md (call-flow executor)
VERDICT: AMBER

1. [blocking] The design's digit-redaction tripwire (Interfaces section + Test 16) is scoped to
   three locations — "`bridge.py`, the servicer and the callflow package" — but the plan only
   delivers two of them: T10's tripwire is scoped "in this file" (bridge.py only) and T12's is
   "over this file" (servicer.py only). No task adds a `digit=%s`-shaped grep tripwire over
   `services/conversation/callflow/` (runner.py, handler.py) — the exact module that owns `collect`
   nodes and sensitive PINs. T14/T15's other tests (the exact invalid-digit log string, the 3b/14b
   redaction negative controls) still catch specific violations, but a future debug line in
   runner.py or handler.py rendering a digit would pass every check in this plan. Fix: add the
   callflow/-package grep tripwire (matching the design's Test 16 wording) to T14 or T15's
   done-when.

2. [minor] T11 and T12 are marked "must land together," but the stated symmetry is one-directional:
   T12 (servicer) landing without T11 does break the no-flow majority path, because
   `session.out_responses`/`session.push_dtmf()` wouldn't exist yet — but T11 landing alone (new
   Protocol members and attributes on session.py/echo.py/pipeline.py, unreferenced by servicer.py)
   is inert and breaks nothing. Bundling the two tasks is still the safe call, but the "shipping
   either alone" justification is inaccurate as written — fix: state the asymmetry (T12 depends on
   T11; T11 alone is a no-op) rather than claiming both directions fail identically.

Everything else held up: all 26 Changes-table files map to a task and vice versa; T9's
`_authorize_flow()` exclusion and T16's construction-site grep tripwire both carry the design's
literal expressions rather than a paraphrase (lesson 32's failure mode does not recur here); the
`collect.sensitive` property-boundary tests (3b, 14b) include the negative control lesson 12 asks
for; OQ2/OQ3/OQ4 reversal seams in T14/T16 name the exact functions/branches the design specifies;
and the seven open security findings (5-8 round 1, 11-13 round 2) appear only as one-line adjacency
notes, correctly attributed to the files/tasks the security doc says they touch, with no task
quietly doing the fix work.

VERDICT: AMBER
