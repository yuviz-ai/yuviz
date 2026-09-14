---
name: sdlc-status
description: Show where the current SDLC feature stands. Invoke with /sdlc-status.
disable-model-invocation: true
---

# /sdlc-status

`cat .sdlc/current`, then `ls .sdlc/<slug>/`, then for each artifact print one line: name, and its VERDICT line if the matching `.review.md` exists (`grep -h VERDICT`). Print unchecked task count from `03-tasks.md` with `grep -c '\- \[ \]'`.

Then name the next skill to run (`/sdlc-design`, `/sdlc-build`, …). Keep the whole reply under 12 lines. Do not open the artifacts.
