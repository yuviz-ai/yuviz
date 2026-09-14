---
name: sdlc-build
description: Implement the approved SDLC tasks (all, or only the task IDs given). Invoke with /sdlc-build [T1 T2].
disable-model-invocation: true
---

# /sdlc-build

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

Tasks to build: $ARGUMENTS, or every unchecked task in `<dir>/03-tasks.md` if no IDs were given.

## Orchestration (Cursor)

Spawn **one** Task `sdlc-implementer` for the whole batch. Keep the agent ID for later `/sdlc-review` / `/sdlc-security` fix rounds. Split into a second implementer only if a task depends on a file another task is still creating and the batch exceeds ~8 tasks.

## Steps

1. If we are on the default branch, create `feature/<slug>` first.
2. Task one `sdlc-implementer` with the design path, the task file path, and the task IDs. Do not spawn one agent per task; the context reuse across dependent tasks is what keeps this cheap.
3. If it reports a deviation or a blocked task, stop and tell me — do not improvise a different design.
4. Print only:

```
Built: <task IDs>
Files: <paths>
Check: <command> — <passed|FAILED>
Deviations: <one line each, or None>
Remaining tasks: <IDs, or None>
```

Then: `Run /sdlc-review.`

Do not write or read code yourself in this skill.
