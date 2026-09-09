---
name: sdlc-revise
description: Apply user feedback to the current SDLC stage's artifact. Invoke with /sdlc-revise <feedback>.
disable-model-invocation: true
---

# /sdlc-revise

My feedback: **$ARGUMENTS**

Slug: `cat .sdlc/current`. Find the latest artifact in `.sdlc/<slug>/` by filename order (`ls`), and route to the agent that owns it: `01-prd.md` → `sdlc-prd-author`, `02-design.md` → `sdlc-architect`, `03-tasks.md` → `sdlc-planner`, code → `sdlc-implementer`.

## Orchestration (Cursor)

Prefer Task `resume` with that agent's prior ID if you still have it — warm context. Otherwise Task it fresh with the artifact path.

## Steps

Give it my feedback verbatim and tell it to make that change only, in place. Then re-run the matching critic once (fresh Task — never resume a critic).

Print only: what changed (max 5 lines), the new verdict, and the skill for the next stage.
