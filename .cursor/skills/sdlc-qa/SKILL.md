---
name: sdlc-qa
description: Post-merge QA — run the real app and hunt edge cases the build and review missed. Invoke with /sdlc-qa [slug].
disable-model-invocation: true
---

# /sdlc-qa

Slug: `cat .sdlc/current`, unless $ARGUMENTS names a different feature. Feature dir: `.sdlc/<slug>/`.

## Orchestration (Cursor)

Task `sdlc-qa-hunter` foreground. Do not fix defects in this skill.

## Steps

1. Confirm what we are testing: `git log --oneline -1` and whether the feature has actually merged (`git branch --contains` / `gh pr view`). QA runs against merged code — if it has not merged, tell me and ask before continuing.
2. Task `sdlc-qa-hunter` with the feature dir, the PRD path (it needs the acceptance criteria and the Out section), and the report path `<dir>/07-qa-report.md`. Foreground.
3. Do NOT fix anything found. Print the defects and stop — I decide what gets fixed and whether it goes back through `/sdlc-build` or becomes its own feature.
4. Print only:

```
QA: <n> defects (<n> critical, <n> high, <n> medium, <n> low)
<one line per defect, most severe first>
Verified working: <n> scenarios
Not covered: <one line each, or None>
Report: .sdlc/<slug>/07-qa-report.md
```

5. If a critical or high defect is a class of mistake that will recur, run the `/sdlc-retro` steps against the report and mention which lessons you added.
