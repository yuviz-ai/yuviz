---
name: sdlc-test
description: Write and run the tests for the current SDLC feature. Invoke with /sdlc-test.
disable-model-invocation: true
---

# /sdlc-test

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

## Orchestration (Cursor)

Task `sdlc-tester` foreground. On failures, Task `sdlc-implementer` to fix source (never assertions), then re-run tester once. Max 2 rounds.

## Steps

1. Task `sdlc-tester` with design path `<dir>/02-design.md` and report path `<dir>/06-test-report.md`. Foreground.
2. If tests fail: the failure is the implementer's to fix, not the tester's. Task `sdlc-implementer` with the report path and the failing test names, told to fix the source (never the assertions). Re-run the tester once. Max 2 rounds.
3. Print only:

```
Command: <exact command>
Result: <n passed, n failed> after <n> round(s)
Still failing: <one line each, or None>
Uncovered acceptance criteria: <or None>
```

Then: `Run /sdlc-ship.`
