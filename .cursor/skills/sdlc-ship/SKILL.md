---
name: sdlc-ship
description: Commit the current SDLC feature and open a PR. Invoke with /sdlc-ship [--yes]. Explicit exception to the usual never-commit rule.
disable-model-invocation: true
---

# /sdlc-ship

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

Do this yourself — no specialists, it is a few shell calls.

This skill is the **explicit exception** to the project rule “never commit or push”: the user invoked ship.

## Steps

1. `git status --short` and `git diff --stat`. Show me the file list and stop here if anything unrelated to the feature is staged.
2. Confirm `06-test-report.md` shows zero failures and `05-review.md` has no open blocking findings. If not, tell me and stop.
3. Create `feature/<slug>` if we are still on the default branch, commit the code **and** the `.sdlc/<slug>/` artifacts, then push and open a PR with `gh`.
4. PR body: the PRD's Problem section, the design's Approach paragraph, the acceptance criteria as a checklist, and the test command with its result. Under 40 lines.
5. Print the PR URL.
6. Once the PR is merged, tell me to run `/sdlc-qa` — the build and review stages test what we thought to test; QA is what finds the rest.

Ask me before pushing if $ARGUMENTS does not contain `--yes`.

Never attribute Cursor/AI as author, co-author, or collaborator in commits, PRs, or Git metadata.
