---
description: Adversarially review the implementation and fix blocking findings
allowed-tools: Bash, Read, Agent
---

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

1. Spawn `sdlc-code-critic` with design path `<dir>/02-design.md` and findings path `<dir>/05-review.md`. Foreground.
2. For blocking findings only: SendMessage them to the implementer agent from the build stage if it is still listed by ListAgents; otherwise spawn a fresh `sdlc-implementer` and give it the findings path plus the design path. Tell it to fix exactly those findings and nothing else.
3. Re-run the critic once after fixes. Stop after 2 rounds — report anything still open rather than looping.
4. Print only:

```
Verdict: <verdict> after <n> round(s)
Fixed: <one line per fixed finding>
Open: <one line per remaining finding, or None>
```

Then: `Run /sdlc:security, then /sdlc:test.`

Minor findings are reported, not fixed, unless I ask.

5. If a blocking finding was a class of mistake that will recur, run the `/sdlc:retro` steps against the findings file and mention which lessons you added.
