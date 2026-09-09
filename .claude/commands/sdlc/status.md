---
description: Show where the current feature stands
allowed-tools: Bash
---

`cat .sdlc/current`, then `ls .sdlc/<slug>/`, then for each artifact print one line: name, and its VERDICT line if the matching `.review.md` exists (`grep -h VERDICT`). Print unchecked task count from `03-tasks.md` with `grep -c '\- \[ \]'`.

Then name the next command to run. Keep the whole reply under 12 lines. Do not open the artifacts.
