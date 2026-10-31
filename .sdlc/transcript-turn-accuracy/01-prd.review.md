# Review: 01-prd.md
VERDICT: AMBER

1. [minor] AC2 keeps only TRANSFER_FAILED as an always-failed zero-turn outcome, but the code also emits TRANSFER_TIMEOUT (services/conversation/session.py:345, services/config/calls.py:389 groups both as "transfer_failed"), so a flow-only Dial that times out could be designed into "clean" success — AC2 (01-prd.md:22) — fix: extend AC2 to "close_reason TRANSFER_FAILED or TRANSFER_TIMEOUT with turn_count 0 is counted as failed".
2. [minor] AC15 records a new answering-agent link from call to agent with no criterion that the answering agent belongs to the call's tenant, so the per-agent count could include another tenant's call (lesson 48) — AC15 (01-prd.md:37) — fix: add a criterion that the answering agent is recorded and counted only when it shares the call's tenant_id, enforced on write and in the per-agent count query.
