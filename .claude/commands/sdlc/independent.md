---
description: Fresh, design-unbound review of the whole diff — the last gate before ship
allowed-tools: Bash, Read, Agent
---

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`. Base: the merge-base `/sdlc:ship` step 1 uses. Read `base_branch` from `state.json` (`/sdlc:new` records it), `git fetch origin <base_branch>`, then `git merge-base HEAD origin/<base_branch>`. If `state.json` or `base_branch` is missing (runs started before `/sdlc:new` recorded it), or either git command fails, stop and ask me for the base branch; never assume one.

Run after `/sdlc:test`. Lesson 52: this is the stage that caught what design, code review and the security audit all passed.

1. Spawn one `general-purpose` agent, foreground, with fresh context. Brief it with paths only:
   - The whole change: `git diff <merge-base>` (working tree, staged and unstaged) plus the untracked files from `git status --short`.
   - Judge it against `01-prd.md`'s acceptance criteria and the original requirement (if it names GitHub issues, read them with `gh issue view`). It is **not** bound by `02-design.md`; the code critic already checked the diff against that.
   - Look hardest at what design-scoped review misses: runtime semantics of the libraries actually called (asyncio cancellation and `wait_for`, connection pool release, retries, timeouts that bound only part of the path), concurrent callers on shared state (caches, single-flight, backoff), fail-open paths, error text reaching users, healthy-path latency, and tests that cannot fail (lesson 12).
   - Confirm each minor finding and uncovered test gap left open in `05-review.md`, `05-security.md` and `06-test-report.md` is closed, or report it.
   - Run the affected service's test suite once, summary only.
   - Write `<dir>/05-review-independent.md`: a verdict line (`GREEN`, `AMBER` or `RED`), then findings with severity `blocking`, `high` or `minor`, each with `file:line`. GREEN with honest minors is a valid outcome.
2. Blocking or high findings: fix them as `/sdlc:review` step 4 does, run the suite on the combined tree, then re-run this stage on the new diff. Max 2 rounds. If the fixes changed code after `/sdlc:security` passed, say so in `Open`.
3. Record the stage in `state.json` in the shape `/sdlc:run` uses for its stages: `stages.independent = {"status": "passed" | "failed", "verdict": "<verdict>", "rounds": <n>, "findings_path": "<dir>/05-review-independent.md"}`. `passed` means no open blocking or high finding.
4. Print only:

```
Verdict: <verdict> after <n> round(s)
Fixed: <one line per fixed finding>
Open: <one line per remaining finding, or None>
```

Then: `Run /sdlc:ship.`
