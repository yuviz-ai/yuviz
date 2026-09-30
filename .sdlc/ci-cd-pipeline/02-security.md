# Security review: .sdlc/ci-cd-pipeline/02-design.md (round 3, final)
VERDICT: AMBER

## Prior-round status
- [high] Publish provenance check (short `origin/main` shadowable by a tag; `--is-ancestor` accepted PR-internal commits): **closed**. The check now uses the fully qualified `refs/remotes/origin/main`. Git's first resolution rule is the literal refname, so `refs/tags/origin/main`, `refs/tags/refs/remotes/origin/main` and a branch `origin/main` (fetched as `refs/remotes/origin/origin/main`) cannot collide with it. First-parent membership rejects commits from inside a merged PR. The tag ruleset (maintainers only, all tags) and "disable rebase merging" are now **required** settings. Test plan cases (a), (b) and (c) exercise each bypass. `grep -Fx >/dev/null` under `pipefail` fails closed on no match or a `rev-list` error, and avoids the SIGPIPE false negative.
- [medium] `integration-gate` / `gate` name spoof via a side-branch PR: **still open**, now documented accurately as a residual (Finding 1).

## Findings
1. [medium] A spoofed `integration-gate` (or `gate`) check from a PR into an unreviewed side branch still satisfies branch protection on `main`. — design "Risks" (name-only matching residual), docs item 1 ("Require workflows to pass" is only *recommended*)
   Attack: A same-repo write collaborator has PR1 (feature branch into `main`, head X). They push branch `x-base` carrying a workflow whose job `integration-gate` runs `true`, then open PR2 from the same feature branch into `x-base`. The green check lands on X and satisfies PR1's required context, because Code Owners review never engages on a pushed branch. Code that skipped PR integration or unit checks can then merge into `main`.
   Bounded: `main` CI re-runs everything on push, and publish requires that run. So the attacker gets an unverified merge, not an unverified image.
   Fix: Make the "Require workflows to pass" ruleset (pinning `ci.yml`/`integration.yml` from `main`) **required**. Otherwise, restrict branch creation and PR bases to `main` for non-maintainers.

2. [low] The first-parent invariant ("only commits that were once `main`'s tip") assumes `main` changes only through PR merges. The required settings do not forbid bypass or force-push. — design "Docs" item 1/1a
   Attack: An admin, or a compromised admin token, fast-forward-pushes a multi-commit branch directly to `main`. Every intermediate commit lands on the first-parent chain without its own `main` CI run, and it becomes publishable if a tag-triggered `CI` exists at that commit. Requires admin, so the impact is small.
   Fix: Add to the required branch-protection settings: "Do not allow bypassing the above settings" and "Block force pushes" on `main`.

3. [low] Re-running an old successful `main` CI run re-fires publish and moves `$GHCR_IMAGE:main` back to an older, possibly vulnerable image. — design "Workflow `publish.yml` / Job: `publish-python`"
   Attack: A write collaborator re-runs an old `CI` push run on `main`. The SHA is first-parent, so publish passes and retags `:main` to the old build. Operators who pin SHA tags (required by the design) are unaffected. Anyone following `:main` is silently downgraded.
   Fix: Accept and document that `:main` is not a trust anchor. Optionally, publish `:main` only when `$SHA == $(git rev-parse refs/remotes/origin/main)`.

## Verified controls
- Publish provenance uses the fully qualified `refs/remotes/origin/main`. Neither a tag nor a branch named `origin/main` or `refs/remotes/origin/main` can shadow it.
- `actions/checkout` with `fetch-depth: 0` fetches `+refs/heads/*:refs/remotes/origin/*`, so the ref exists without a second credentialed fetch. `persist-credentials: false` is kept.
- The first-parent check rejects PR-internal commits (including a later-reverted backdoor commit) and side-branch or tag-only SHAs.
- The provenance step runs before the secrets check and the GHCR login. It has no `|| true`, and `grep -Fx` without `-q` avoids SIGPIPE under `pipefail`.
- The job `if` adds `head_repository.full_name == github.repository`, so fork-headed runs are refused.
- A tag-triggered `CI` at a past `main` tip is unreachable with reviewed YAML, because the current `ci.yml` triggers only on `branches: [main]` and has no `workflow_dispatch`.
- The tag ruleset (maintainers only, all tags) and "merge commit/squash only, rebase disabled" are now required settings, not defense in depth.
- The three required review settings (Code Owners, dismissal of stale approvals, approval of the most recent push) keep approval and integration in lockstep for PRs into `main`.
- `integration-gate` fails any `pull_request_review` whose base is not `main`, is main-only for `workflow_dispatch`, and fails closed on a `gh` error. The prior-run lookup is filtered by event, workflow name, PR number and base `main`.
- For fork PRs, the lookup fails closed because `pull_requests[]` is empty.
- No `pull_request_target` is used anywhere. PR-triggered runs get a read-only token and no secrets.
- `GHCR_*` values live only in Environment `ghcr-publish` (`main` only). No repo- or org-level Actions secrets are allowed, and `ci.yml` and `integration.yml` use no `environment:`, no `secrets:` and no `secrets: inherit`.
- Publish uses the bot PAT and a bot-owned `GHCR_IMAGE`, so a `packages: write` elevation of `GITHUB_TOKEN` cannot overwrite it (lesson 1).
- `gate` requires `integration == success` on push and `skipped` on `pull_request`. Nested `integration / …` checks cannot satisfy the bare `integration-gate` context.
- Untrusted event fields reach the gate script only through `env:`, never through `${{ }}` in `run:`.
- Minimal token scope: `contents: read`, plus `actions: read` only where needed. No `packages`, `contents: write` or `pull-requests: write` anywhere.
- CI uses a throwaway `JWT_SECRET`, never sets `SECRET_ENCRYPTION_KEY`, and never loads `deployment/.env`.
