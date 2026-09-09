---
description: Produce and critique the technical design from the approved PRD
allowed-tools: Bash, Read, Agent
---

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

1. Spawn `sdlc-architect` with PRD path `<dir>/01-prd.md`, output `<dir>/02-design.md`, plus $ARGUMENTS as extra constraints. Foreground.
2. Spawn `sdlc-doc-critic` with kind `design`, artifact `<dir>/02-design.md`, upstream `<dir>/01-prd.md`, findings `<dir>/02-design.review.md`.
3. On RED or AMBER, SendMessage the findings back to the same architect agent to fix blocking findings in place, then re-run the critic. Max 2 rounds.
4. Print only:

```
Design ready: .sdlc/<slug>/02-design.md
Verdict: <verdict> after <n> round(s)
Files to touch: <n>
Risks: <verbatim lines>
Unresolved findings: <one line each, or None>
```

Then: `Review the design, then run /sdlc:security — or /sdlc:revise <what to change>.`

Do not read the design or the codebase yourself. Do not paste the design back.
