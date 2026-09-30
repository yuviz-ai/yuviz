# Review: 03-tasks.md (Phase 4, T9–T19)
VERDICT: AMBER

1. [minor] T16's gate-script check lists only failing cases (PR+`success`, push+`skipped`, non-success unit job), so a script that always exits 1 satisfies "done when" (lesson 12) — T16 — fix: add "exits 0 for PR with four `success` + `integration`=`skipped`, and for push with all five `success`".
2. [minor] T14's gate-script matrix omits `workflow_dispatch` on `refs/heads/main` + `success` → 0 and approved + `failure` → 1, so the main-only dispatch pass path and the RESULT check on the approval path are never exercised — T14 — fix: add those two stubbed cases to the done-when list.
3. [minor] T15 deletes job `python` while `gate` still has `needs: [python, …]` (ci.yml:163) until T16, so after T15 alone the workflow is invalid even though "YAML parses" passes — T15 — fix: have T15 change `gate.needs` to `python-unit`, or merge T15 and T16 into one task with `actionlint` (or an equivalent needs-graph check) in done-when.
