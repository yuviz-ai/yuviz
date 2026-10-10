---
description: Run the rest of the pipeline unattended — sdlc-approver stands in for me at every gate
allowed-tools: Bash, Read, Agent
---

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`. If `.sdlc/<slug>/worktree` exists, it holds the path of the feature's git worktree. All stage work happens there, and the worktree's `.sdlc/<slug>/` is the source of truth.

Run every remaining stage in order, starting from the first whose artifact is missing or whose tasks are unchecked:
prd → design → security (design) → plan → build → review → test → ship. Pass $ARGUMENTS through as extra guidance.

Run each stage exactly as its own command file describes (`.claude/commands/sdlc/<stage>.md`), with one change: **wherever a stage would stop for me, hand the gate to `sdlc-approver` instead.** That covers the end of every stage ("Review …, then run …"), an AMBER or RED verdict left unresolved after the stage's max rounds, open questions, a build agent reporting a deviation or a blocked task, and a failed combined-tree check. Give the approver the gate name, the feature dir, the artifact and findings paths, and every agent report verbatim.

Act on the approver's report:
- **PROCEED** — start the next stage.
- **PROCEED_AFTER_FIXES** — send each fix instruction to the owning agent (SendMessage if still listed, else spawn fresh), respecting file ownership and the one-test-runner rule. Re-run that stage's critic (or, for build, the combined-tree check), then take the gate back to the approver. At most 2 approver rounds per gate; a third is ESCALATE.
- **ESCALATE** — stop and print the escalations. Do not work around them.

**Ship** is outward-facing: push and open the PR only if $ARGUMENTS contains `--ship`. Otherwise stop after test with the branch committed locally, and tell me the ship command.

Between stages, print exactly one line: `<stage>: <verdict> — <approver overall> (<n> fixes, <n> decisions)`. Do not ask me anything else.

At the end, print:

```
Reached: <last completed stage>
Approvals: .sdlc/<slug>/approvals.md (<n> approved, <n> fixed, <n> decided)
Escalations: <verbatim, or None>
Next: <command, or None>
```
