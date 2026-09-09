---
description: Split the approved design into build tasks
allowed-tools: Bash, Read, Agent
---

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

1. Spawn `sdlc-planner` with design path `<dir>/02-design.md`, output `<dir>/03-tasks.md`. Foreground.
2. Spawn `sdlc-doc-critic` with kind `plan`, artifact `<dir>/03-tasks.md`, upstream `<dir>/02-design.md`, findings `<dir>/03-tasks.review.md`.
3. On RED, SendMessage the findings back to the same planner and re-run the critic once. AMBER on a plan is not worth a round — report it and move on.
4. `cat .sdlc/<slug>/03-tasks.md` and show me the task list verbatim (it is short, and I need it to approve the build).

Then: `Approve, then run /sdlc:build — or /sdlc:build T1 T2 to do part of it.`
