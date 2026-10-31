---
description: Commit the feature and open a PR
allowed-tools: Bash, Read
---

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

Do this yourself — no agents, it is a few shell calls.

1. The PR base is `base_branch` from `state.json` (`state.base` is a commit SHA, not a branch). If `base_branch` is missing (runs started before `/sdlc:new` recorded it), stop and ask me which branch, even with `--yes`; never assume one. `git fetch origin <base_branch>`, then `git merge-base HEAD origin/<base_branch>`; if either fails, stop. The feature's changes are `git diff --stat <merge-base>` plus the untracked paths in `git status --short`; show me that list and stop here if anything unrelated to the feature is in it.
2. Confirm `06-test-report.md` shows zero failures outside its `## Pre-existing on base` section (lesson 54), that no test listed there is in or imports a file from the step 1 list, and that `05-review.md` has no open blocking/high findings. The independent review ran if `05-review-independent.md` exists or `state.json` has `stages.independent` or `independent_review`; then the file must exist and have no open blocking/high findings. If any of this fails, tell me and stop.
3. Create `feature/<slug>` if HEAD is the default branch or `base_branch`. Stage the code **and** the `.sdlc/<slug>/` artifacts by name, never `git add -A` (unrelated untracked files stay out).
4. With the files staged, run the repo-wide scans that read `git ls-files` (they cannot see untracked files, so the fan-in run may have missed them): `./venv/bin/python -m pytest -q tests/test_env_catalogue.py`. These need no services. A failure blocks the commit; fix it (artifacts too: an example DSN uses `<password>`, never a literal one).
5. Write the PR body to a temp file: the PRD's Problem section, the design's Approach paragraph, the acceptance criteria as a checklist, the test command with its result, and each review's verdict. Under 40 lines.
6. Commit, push, and `gh pr create --base <base_branch> --body-file <file> --assignee @me`.
7. Wait for CI: `gh pr checks <n> --watch`. A check that runs a test listed under `## Pre-existing on base` must be green, even if it is red on base; if it is red, stop and tell me. Otherwise a check is pre-existing only when it fails on the base branch on the same tests with the same errors: report it with the run link. For any other red check:
   - A fix that touches only `.sdlc/<slug>/` artifacts may be pushed directly.
   - Any other fix goes back through `/sdlc:test`, `/sdlc:review`, `/sdlc:security` (and the independent review if it ran, per step 2), then steps 2 and 4 again, before the run counts as shipped.
   - Never change a test, scan or allowlist to make a check pass; fix what it flagged.
   - After pushing, list each CI-fix commit next to the verdicts with `gh pr edit <n> --body-file <file>`.
8. Print the PR URL.
9. Once the PR is merged, tell me to run `/sdlc:qa` — the build and review stages test what we thought to test; QA is what finds the rest.

Ask me before pushing if $ARGUMENTS does not contain `--yes`.
