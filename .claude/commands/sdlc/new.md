---
description: Start a new SDLC feature from a raw requirement and produce a reviewed PRD
allowed-tools: Bash, Read, Write, Agent
---

Start a new feature: **$ARGUMENTS**

1. Pick a kebab-case slug of at most 4 words from the requirement. Create `.sdlc/<slug>/`, write the requirement verbatim to `.sdlc/<slug>/00-request.md`, and write the slug to `.sdlc/current`.
2. Run the PRD stage exactly as described in `.claude/commands/sdlc/prd.md`.

Do not read the codebase yourself — that is the agent's job. Your own tool calls in this command should be `mkdir`/`cat >` and the Agent calls, nothing more.
