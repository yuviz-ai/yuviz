# SDLC agent pipeline (Cursor)

A staged workflow for building features in this repo with Cursor Agent. Each stage has an expert
subagent and, where it matters, an **adversarial critic that runs with fresh context** — so the critic
does not inherit the author's blind spots. You review and approve between stages; nothing advances
on its own.

Claude Code has the same pipeline under `.claude/`. This tree is the Cursor-native adaptation:
skills instead of slash-command markdown, `.cursor/agents/` subagents, and Task/`resume` instead of
Claude's Agent/SendMessage.

## The flow

| Skill | Does | You get |
|---|---|---|
| `/sdlc-new <requirement>` | Researches how competitors solve it, writes a PRD, critiques it | `01-prd.md` — a decision to approve |
| `/sdlc-design` | Technical design grounded in this codebase, then critiqued | `02-design.md` |
| `/sdlc-security` | Threat-models the design for tenant isolation and privilege escalation | `02-security.md` |
| `/sdlc-plan` | Splits it into ordered, independently-shippable phases | `03-tasks.md` |
| `/sdlc-build [T1 T2]` | Implements the tasks — all of them, or just the ones you name | code + ticked tasks |
| `/sdlc-review` | Adversarial code review of the diff | `05-review.md` |
| `/sdlc-security` | Audits the implementation, not the intention | `05-security.md` |
| `/sdlc-test` | Writes the tests the design called for and runs them | `06-test-report.md` |
| `/sdlc-ship` | Commits, pushes, opens a PR | PR URL |
| `/sdlc-qa` | **After merge** — runs the real app and hunts edge cases | `07-qa-report.md` |

Also: `/sdlc-status` (where am I), `/sdlc-revise <feedback>` (change the current artifact),
`/sdlc-retro` (turn a review miss into a permanent lesson), `/review-pr <PR>` (GitHub PR review).

Artifacts land in `.sdlc/<feature-slug>/` and are committed with the PR, so a reviewer can see what
was decided and why — not just what changed.

## Why it is shaped this way

**Artifacts, not conversation.** Each stage reads one file from the stage before it. The architect
never sees the PRD's critique; the tester never sees the PRD. That keeps context small and stops
early noise contaminating later work.

**Critics run fresh.** A reviewer sharing the author's context inherits the author's assumptions.
Every critic starts cold and verifies claims against the real code — a design citing a file or
function that does not exist is its highest-value finding.

**Loops are capped.** Two rounds per stage, three for security. Anything still open is reported to
you rather than ground on.

## Cursor orchestration

Stage skills are the orchestrators. They must use the **Task** tool:

1. Launch a specialist with `subagent_type` equal to the agent `name` in `.cursor/agents/`
   (e.g. `sdlc-prd-author`, `sdlc-architect`), `run_in_background: false`, and a prompt that
   includes every path and input.
2. Record the returned agent ID.
3. Author fix rounds: Task again with `resume: <agent-id>` and the findings path — warm context,
   do not spawn fresh.
4. Critics always spawn **fresh** (no `resume`) so they do not inherit the author's blind spots.

Slash skills (`/sdlc-prd`) and Task agents (`sdlc-prd-author`) use different names on purpose so
they do not collide in the `/` menu.

Do not do the specialist's work in the parent chat. Do not paste artifact bodies back to the user.

## `.sdlc/lessons.md` — the part that compounds

Every rule in **`.sdlc/lessons.md`** (repo-root, shared with Claude) was earned from a real miss
on this codebase. Every agent reads it before starting and must comply or say why a lesson does
not apply.

When a review catches something a previous stage should have, `/sdlc-retro` turns it into a lesson —
but only if it is a *class* of mistake, not a one-off. Merge aggressively; a long lessons file stops
being read. There is one file — no cross-tool mirroring.

## Layout

```
.cursor/
  README.md           # this file
  rules/              # always-on / scoped engineering rules
  agents/             # specialist subagents (Task targets)
  skills/             # /sdlc-* and /review-pr stage orchestrators

.sdlc/lessons.md      # earned rules (canonical; Claude + Cursor)
```

Specialist Task names (when they differ from the skill): `sdlc-prd-author`,
`sdlc-security-auditor`, `sdlc-qa-hunter`. Others match Claude (`sdlc-architect`,
`sdlc-implementer`, `sdlc-code-critic`, …).

## Using it

Start with `/sdlc-new` and a plain-language requirement. Review each artifact before running the
next stage — the gates are the point. If an artifact is wrong, `/sdlc-revise <what to change>`
rather than editing it by hand, so the agent's context stays in sync with the file.

Two things worth knowing:

- **Run the app.** `/sdlc-qa` after merge found 17 defects on a feature that had three review rounds,
  a green security audit and 330 passing tests. The suite tests what someone thought to test.
- **A clean verdict is a real outcome.** Critics are told not to manufacture findings. GREEN with an
  honest list of accepted lows is the expected good result, not a sign the critic was lazy.

After adding or renaming agents/skills, start a new Agent chat so Cursor rediscovers them.
