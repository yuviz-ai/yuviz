---
description: Write and run the tests for the current feature
allowed-tools: Bash, Read, Agent
---

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

1. Spawn `sdlc-tester` with design path `<dir>/02-design.md` and report path `<dir>/06-test-report.md`. Foreground.
2. If tests fail: the failure is the implementer's to fix, not the tester's. Spawn `sdlc-implementer` with the report path and the failing test names, told to fix the source (never the assertions). Re-run the tester once. Max 2 rounds.
3. Print only:

```
Command: <exact command>
Result: <n passed, n failed> after <n> round(s)
Still failing: <one line each, or None>
Uncovered acceptance criteria: <or None>
```

Then: `Run /sdlc:ship.`
