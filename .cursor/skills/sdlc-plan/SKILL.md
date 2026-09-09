---
name: sdlc-plan
description: Split the approved design into build tasks. Invoke with /sdlc-plan.
disable-model-invocation: true
---

# /sdlc-plan

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

## Orchestration (Cursor)

Task for `sdlc-planner`; critic always fresh; on RED resume planner once.

## Steps

1. Task `sdlc-planner` with design path `<dir>/02-design.md`, output `<dir>/03-tasks.md`. Foreground.
2. Task `sdlc-doc-critic` (fresh) with kind `plan`, artifact `<dir>/03-tasks.md`, upstream `<dir>/02-design.md`, findings `<dir>/03-tasks.review.md`.
3. On RED, resume the same planner and re-run the critic once. AMBER on a plan is not worth a round — report it and move on.
4. `cat .sdlc/<slug>/03-tasks.md` and show me the task list verbatim (it is short, and I need it to approve the build).

Then: `Approve, then run /sdlc-build — or /sdlc-build T1 T2 to do part of it.`
