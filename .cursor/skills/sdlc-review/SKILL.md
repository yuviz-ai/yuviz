---
name: sdlc-review
description: Adversarially review the implementation and fix blocking findings. Invoke with /sdlc-review.
disable-model-invocation: true
---

# /sdlc-review

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

## Orchestration (Cursor)

Task `sdlc-code-critic` always fresh. Blocking fixes: resume the build-stage implementer if you have its agent ID; else Task a fresh `sdlc-implementer` with findings + design paths.

## Steps

1. Task `sdlc-code-critic` with design path `<dir>/02-design.md` and findings path `<dir>/05-review.md`. Foreground.
2. For blocking findings only: resume the implementer from the build stage if you still have its agent ID; otherwise Task a fresh `sdlc-implementer` with the findings path plus the design path. Tell it to fix exactly those findings and nothing else.
3. Re-run the critic once after fixes (fresh). Stop after 2 rounds — report anything still open rather than looping.
4. Print only:

```
Verdict: <verdict> after <n> round(s)
Fixed: <one line per fixed finding>
Open: <one line per remaining finding, or None>
```

Then: `Run /sdlc-security, then /sdlc-test.`

Minor findings are reported, not fixed, unless I ask.

5. If a blocking finding was a class of mistake that will recur, run the `/sdlc-retro` steps against the findings file and mention which lessons you added.
