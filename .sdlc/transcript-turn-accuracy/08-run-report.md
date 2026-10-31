# Run report: transcript-turn-accuracy
Status: shipped (CI green: all 10 checks pass)
PR: https://github.com/yuviz-ai/yuviz/pull/232 — "Fixes #204" and "Fixes #182" commented. #205 (item 7 is on fix/tts-failure-handling) and #203 (only item 2 in scope) remain open.

| Stage | Verdict | Rounds | Output tokens (exact) | Est. cost |
|---|---|---|---|---|
| prd | AMBER | 2 | sonnet 9,930 · opus 7,130 | $0.49 |
| security_design | GREEN | 1 | opus 6,035 | $0.24 |
| design | AMBER | 1 | opus 33,837 | $1.33 |
| plan | GREEN | 2 | opus 22,512 | $0.88 |
| build | GREEN | 1 | haiku 18,228 · sonnet 81,241 | $1.91 |
| review | GREEN | 1 | opus 75,867 · sonnet 14,629 | $3.28 |
| security_code | GREEN | 1 | (billed to review) | — |
| test | GREEN | 1 | sonnet 4,431 | $0.09 |
| independent | GREEN | 1 | opus 20,445 | $0.80 |
| retro | — | 0 | sonnet 650 | $0.01 |
| _state_writes | — | — | haiku 101,051 | $1.07 |
| **Total** | | | 395,986 | **≈ $10.10 est.** |

Cost is estimated: input taken as 20× output with 80% of it cache reads. Haiku input is priced at $1/M because the configured rate was unreadable. Three direct implementer agents dispatched after halts (stale tests, review round-1 fixes, AMBER fixes) ran outside the workflow, so their tokens are not counted here.

Halts: build ×3 (a test file outside the agent's ownership, 4 stale tests, test files outside ownership) and review ×1 (the AC14 fix needed servicer.py/session.py). Each was retried with your decision. Gate B: review was AMBER, so you chose to fix the findings; the re-run came back all GREEN.

Open findings — decide:
- [medium] test — pipeline.py:1309: the transfer-refused apology nested in _run_turn is not aclosed on hang-up, so its [transfer_failed] row can be dropped. Not a regression; listed as a follow-up in the PR.
- [minor] independent — servicer.py:587: "spoken" means sent to the gateway, not played. Stated in the PR body.
- [low] review — pipeline.py:1133 and session.py:70: merge overlap with fix/tts-failure-handling (CJK sentence joining, HandlerResponse, tts_chunk path). Stated in the PR body.
Lessons added: None
Workflow run: wf_939192ca-08d (resume within this session with resumeFromRunId)
