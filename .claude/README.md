# SDLC agent pipeline

A staged workflow for building features in this repo with Claude Code. Each stage has an expert
agent and, where it matters, an **adversarial critic that runs with fresh context** — so the critic
does not inherit the author's blind spots. You review and approve between stages; nothing advances
on its own.

## The flow

| Command | Does | You get |
|---|---|---|
| `/sdlc:new <requirement>` | Researches how competitors solve it, writes a PRD, critiques it | `01-prd.md` — a decision to approve |
| `/sdlc:design` | Technical design grounded in this codebase, then critiqued | `02-design.md` |
| `/sdlc:security` | Threat-models the design for tenant isolation and privilege escalation | `02-security.md` |
| `/sdlc:plan` | Splits it into ordered, independently-shippable phases | `03-tasks.md` |
| `/sdlc:build [T1 T2]` | Implements the tasks — all of them, or just the ones you name | code + ticked tasks |
| `/sdlc:review` | Adversarial code review of the diff | `05-review.md` |
| `/sdlc:security` | Audits the implementation, not the intention | `05-security.md` |
| `/sdlc:test` | Writes the tests the design called for and runs them | `06-test-report.md` |
| `/sdlc:ship` | Commits, pushes, opens a PR | PR URL |
| `/sdlc:qa` | **After merge** — runs the real app and hunts edge cases | `07-qa-report.md` |

Also: `/sdlc:status` (where am I), `/sdlc:revise <feedback>` (change the current artifact),
`/sdlc:retro` (turn a review miss into a permanent lesson).

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

## `.sdlc/lessons.md` — the part that compounds

Every rule in **`.sdlc/lessons.md`** (repo-root, shared with Cursor) was earned from a real miss on
this codebase: a `DROP CONSTRAINT` that would have aborted a schema apply half-finished, a
console-role gate that would have taken `/health` down with docker-compose, tests that could not
fail. Every agent reads it before starting and must comply or say why a lesson does not apply.

When a review catches something a previous stage should have, `/sdlc:retro` turns it into a lesson —
but only if it is a *class* of mistake, not a one-off. Merge aggressively; a long lessons file stops
being read. Write only that path — do not keep a second copy under `.claude/`.

## Using it

Start with `/sdlc:new` and a plain-language requirement. Review each artifact before running the
next stage — the gates are the point. If an artifact is wrong, `/sdlc:revise <what to change>`
rather than editing it by hand, so the agent's context stays in sync with the file.

Two things worth knowing:

- **Run the app.** `/sdlc:qa` after merge found 17 defects on a feature that had three review rounds,
  a green security audit and 330 passing tests. The suite tests what someone thought to test.
- **A clean verdict is a real outcome.** Critics are told not to manufacture findings. GREEN with an
  honest list of accepted lows is the expected good result, not a sign the critic was lazy.

New agents need a Claude Code restart before they register.
