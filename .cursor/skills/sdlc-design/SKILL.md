---
name: sdlc-design
description: Produce and critique the technical design from the approved PRD. Invoke with /sdlc-design.
disable-model-invocation: true
---

# /sdlc-design

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

## Orchestration (Cursor)

Task for specialists; critics always fresh; author fix rounds via Task `resume` with the architect's agent ID.

## Steps

1. Task `sdlc-architect` with PRD path `<dir>/01-prd.md`, output `<dir>/02-design.md`, plus $ARGUMENTS as extra constraints. Foreground.
2. Task `sdlc-doc-critic` (fresh) with kind `design`, artifact `<dir>/02-design.md`, upstream `<dir>/01-prd.md`, findings `<dir>/02-design.review.md`.
3. On RED or AMBER, resume the same architect with the findings to fix blocking findings in place, then re-run the critic (fresh). Max 2 rounds.
4. Print only:

```
Design ready: .sdlc/<slug>/02-design.md
Verdict: <verdict> after <n> round(s)
Files to touch: <n>
Risks: <verbatim lines>
Unresolved findings: <one line each, or None>
```

Then: `Review the design, then run /sdlc-security — or /sdlc-revise <what to change>.`

Do not read the design or the codebase yourself. Do not paste the design back.
