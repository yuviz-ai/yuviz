---
name: sdlc-retro
description: Turn a review miss into a permanent lesson so SDLC agents stop repeating it. Invoke with /sdlc-retro [findings-path].
disable-model-invocation: true
---

# /sdlc-retro

A stage found something the previous stage should have caught. Record it so it does not recur.

Source: $ARGUMENTS, or the newest `*.review.md` / `*-security.md` in `.sdlc/<slug>/` if I did not name one.

## Steps

1. Read the findings. For each finding rated **blocking, high or critical**, ask: was this a one-off mistake, or an instance of a *class* an agent will hit again on this codebase? Only classes become lessons — a typo does not.
2. Read `.sdlc/lessons.md`. If an existing lesson already covers the class, sharpen that entry with the new evidence instead of adding a near-duplicate. Merge aggressively; a long lessons file stops being read.
3. Append genuinely new lessons in the existing format: numbered, role tags, the rule in the imperative, then a one-line *Earned:* italic note naming the concrete miss. Two lines of rule maximum.
4. Write the rule so it is checkable. "Be careful with permissions" is useless; "trace any control through the framework's real wiring before calling it designed" is a rule an agent can apply.
5. If the file exceeds 25 lessons, drop the ones that have become codebase conventions rather than live traps — say which you dropped and why.

Write only `.sdlc/lessons.md` — that is the single shared ledger for Claude and Cursor. Do not copy lessons under `.claude/` or `.cursor/`.

Print only: the lessons added or sharpened, one line each. If nothing qualified, say so — an empty retro is a valid outcome and better than padding the file.
