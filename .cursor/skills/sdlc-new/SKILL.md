---
name: sdlc-new
description: Start a new SDLC feature from a raw requirement and produce a reviewed PRD. Invoke with /sdlc-new <requirement>.
disable-model-invocation: true
---

# /sdlc-new

Start a new feature: **$ARGUMENTS**

## Orchestration (Cursor)

Use the Task tool for specialists. Critics always spawn fresh. Author fix rounds use `resume` with the prior agent ID.

## Steps

1. Pick a kebab-case slug of at most 4 words from the requirement. Create `.sdlc/<slug>/`, write the requirement verbatim to `.sdlc/<slug>/00-request.md`, and write the slug to `.sdlc/current`.
2. Run the PRD stage exactly as described in `.cursor/skills/sdlc-prd/SKILL.md`.

Do not read the codebase yourself — that is the agent's job. Your own tool calls in this skill should be directory/file setup and Task calls, nothing more.
