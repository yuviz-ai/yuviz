---
description: Write and critique the PRD for the current feature
allowed-tools: Bash, Read, Agent
---

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

1. Spawn `sdlc-prd` with: the request file path `<dir>/00-request.md`, the output path `<dir>/01-prd.md`, and any extra guidance in $ARGUMENTS. Run it in the foreground.
2. Spawn `sdlc-doc-critic` with kind `prd`, artifact `<dir>/01-prd.md`, upstream `<dir>/00-request.md`, findings path `<dir>/01-prd.review.md`.
3. If the verdict is RED or AMBER, send the findings path back to the **same** `sdlc-prd` agent with SendMessage (its context is still warm — do not spawn a fresh one) and tell it to fix only the blocking findings in place. Re-run the critic once. Stop after at most 2 rounds even if AMBER remains.
4. Print to me, and nothing else:

```
PRD ready: .sdlc/<slug>/01-prd.md
Verdict: <verdict> after <n> round(s)
Acceptance criteria: <n>
Open questions: <verbatim, or None>
Unresolved findings: <one line each, or None>
```

Then: `Review the PRD, then run /sdlc:design — or /sdlc:revise <what to change>.`

Never paste the PRD body into your reply. I will read the file.
