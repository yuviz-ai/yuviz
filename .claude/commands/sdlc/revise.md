---
description: Apply my feedback to the current stage's artifact
allowed-tools: Bash, Read, Agent
---

My feedback: **$ARGUMENTS**

Slug: `cat .sdlc/current`. Find the latest artifact in `.sdlc/<slug>/` by filename order (`ls`), and route to the agent that owns it: `01-prd.md` → `sdlc-prd`, `02-design.md` → `sdlc-architect`, `03-tasks.md` → `sdlc-planner`, code → `sdlc-implementer`.

Prefer SendMessage to that agent if it is still listed by ListAgents — its context is warm and re-reading costs tokens. Otherwise spawn it fresh with the artifact path.

Give it my feedback verbatim and tell it to make that change only, in place. Then re-run the matching critic once.

Print only: what changed (max 5 lines), the new verdict, and the command for the next stage.
