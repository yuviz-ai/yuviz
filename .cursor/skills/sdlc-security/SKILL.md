---
name: sdlc-security
description: Security review of the current design or implementation (multi-tenant isolation, privilege escalation, tokens). Invoke with /sdlc-security.
disable-model-invocation: true
---

# /sdlc-security

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

Mode: `code` if there is an uncommitted diff (`git status --short`), otherwise `design`. $ARGUMENTS overrides the mode.

## Orchestration (Cursor)

Task for `sdlc-security-auditor` (always fresh each round). Fix critical/high via Task `resume` on the owning author if you still have their agent ID; otherwise spawn fresh `sdlc-architect` (design) or `sdlc-implementer` (code) with the findings path.

## Steps

1. Task `sdlc-security-auditor` with the mode, the artifact (`<dir>/02-design.md` for design mode; the working diff plus `<dir>/02-design.md` for code mode), the PRD path `<dir>/01-prd.md`, and findings path `<dir>/0X-security.md` (`02-security.md` for design mode, `05-security.md` for code mode). Foreground.
2. For **critical** and **high** findings: resume the owning agent — `sdlc-architect` in design mode, `sdlc-implementer` in code mode — if you still have their agent ID; else Task them fresh with the findings path. Fix those only.
3. Re-run `sdlc-security-auditor` (fresh) after fixes. Max 3 rounds — security gets one more than the other stages, because a design-stage fix is far cheaper than the same bug found after it ships.
4. Print only:

```
Security: <verdict> after <n> round(s)
Fixed: <one line per fixed finding>
Open: <severity + one line each, or None>
Controls verified: <n>
```

Medium and low findings are reported, not auto-fixed, unless I ask.

5. If any critical or high finding was a class of mistake that will recur — not a one-off slip — run the `/sdlc-retro` steps against the findings file before printing, and mention which lessons you added.
