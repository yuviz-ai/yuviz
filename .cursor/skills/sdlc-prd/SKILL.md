---
name: sdlc-prd
description: Write and critique the PRD for the current SDLC feature. Invoke with /sdlc-prd.
disable-model-invocation: true
---

# /sdlc-prd

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

## Orchestration (Cursor)

- Spawn specialists with Task: `subagent_type` = agent name, `run_in_background: false`.
- Record the agent ID from the Task result.
- Critic stages always spawn **fresh** (no resume).
- Author fix rounds: Task with `resume: <author-agent-id>` — do not spawn a fresh author.

## Steps

1. Task `sdlc-prd-author` with: the request file path `<dir>/00-request.md`, the output path `<dir>/01-prd.md`, and any extra guidance in $ARGUMENTS. Foreground.
2. Task `sdlc-doc-critic` (fresh) with kind `prd`, artifact `<dir>/01-prd.md`, upstream `<dir>/00-request.md`, findings path `<dir>/01-prd.review.md`.
3. If the verdict is RED or AMBER, resume the **same** `sdlc-prd-author` agent with the findings path and tell it to fix only the blocking findings in place. Re-run the critic once (fresh). Stop after at most 2 rounds even if AMBER remains.
4. Print to me, and nothing else:

```
PRD ready: .sdlc/<slug>/01-prd.md
Verdict: <verdict> after <n> round(s)
Acceptance criteria: <n>
Open questions: <verbatim, or None>
Unresolved findings: <one line each, or None>
```

Then: `Review the PRD, then run /sdlc-design — or /sdlc-revise <what to change>.`

Never paste the PRD body into your reply. I will read the file.
