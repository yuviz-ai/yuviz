---
description: Implement the approved tasks (all, or only the task IDs given) — file-disjoint groups run in parallel
allowed-tools: Bash, Read, Agent
---

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

Tasks to build: $ARGUMENTS, or every unchecked task in `<dir>/03-tasks.md` if no IDs were given.

1. If we are on the default branch, create `feature/<slug>` first.

2. **Read `.claude/sdlc/parallelism.md`.** Group the tasks by the files they touch, then dispatch one `sdlc-implementer` per **file-disjoint** group — all in a single message, so they genuinely run at once rather than one after another. Give each the design path, the task file path, its task IDs, and an explicit list of the files it owns. Two agents must never own the same file: split by file, not by topic.

   Serialise only against a real blocker: a task needing a file another task is still creating, or a phase boundary. Keep genuinely dependent tasks in one agent — context reuse across them is what keeps that cheap.

   **Only one agent in the batch runs the test suite.** Concurrent pytest against the same database interleaves and produces flaky failures that look like real regressions and cost more than the parallelism saved. Tell the others to report failures in files they do not own rather than fixing them.

3. If any agent reports a deviation or a blocked task, do not improvise a different design. Hand the reports verbatim to `sdlc-approver` (gate `build-batch`) and act on its verdict: PROCEED → next batch; PROCEED_AFTER_FIXES → send each fix to the owning agent, then back to the approver; ESCALATE → stop and tell me.

4. **Fan in before reporting.** Run the full suite yourself on the combined tree: each agent verified against a tree that did not contain the others' work, and that is not the tree we are shipping. Reconcile the reports for contradiction, not just for a union.

5. Print only:

```
Built: <task IDs>   (<n> agents, <n> serialised because <reason>)
Files: <paths>
Check: <command> — <passed|FAILED>  (combined tree)
Deviations: <one line each, or None>
Remaining tasks: <IDs, or None>
```

Then: `Run /sdlc:review.`

Do not write or read code yourself in this command.
