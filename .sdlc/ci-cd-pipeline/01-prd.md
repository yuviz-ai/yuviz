# PRD: CI/CD pipeline for yuviz

## Problem

yuviz has no GitHub Actions (or other CI) config, so every PR is gated only by whatever each contributor remembers to run locally. The checks that matter today — Python `pytest`, Gateway `ctest`, and Admin UI `lint`/`build` — are documented but manual, and there is no automated path to build or publish deployable images from the existing Dockerfiles. Merges can ship broken tests or unbuildable images without anyone noticing until the next person runs `./deployment/sh/dev.sh`.

## How this is solved elsewhere

Teams with multi-language monorepos converge on **GitHub Actions as the PR gate**: parallel jobs for each check surface (pytest, native tests, frontend lint/build), often with a final **aggregator “required” job** so branch protection has one stable check name (Bri, OneUptime monorepo guidance, GitHub’s own required-check docs). They also converge on **immutable image tags (commit SHA)** pushed to a registry — usually **GHCR** when the repo is already on GitHub — then `compose pull` + `up` on a host (GitHub’s publishing docs; common GHCR + SSH compose patterns).

Where they genuinely differ: (1) **selective path filters** vs always-run full CI — path filters save minutes but workflow-level `paths` leave required checks Pending and block merges (GitHub docs; dorny/paths-filter / merge-gate patterns fix this with job-level conditions + a gate job); (2) **auto-deploy on every main push** vs **tag / workflow_dispatch / environment approval** — auto-deploy needs a real host and secrets; without them, publish-only is the durable CD step.

**Recommendation for yuviz:** ship **full PR CI** matching the checks in `CURSOR.md` (no inventing repo-wide mypy/ruff — none exists). Skip monorepo path-filter optimization in this iteration — the suite is still small enough that always-on jobs are simpler and safer with branch protection. **Publish** the Python image to GHCR from `main` (SHA + `main` tags). **Do not** auto-SSH-deploy in this iteration — there is only a local/dev compose stack (`deployment/sh/dev.sh`, `deployment/docker/docker-compose.yml`), Admin UI’s Dockerfile is explicitly `next dev` (not a production image), and no production host is defined in-repo. Document a dispatch/manual “pull published image” hook as the clear next step when a host exists. Cut: full `dev.sh` with Ollama/STT/TTS in CI (heavy, not needed to gate PRs); FreeSWITCH prebuilt release pipeline (already separate).

## Scope

**In:**
- GitHub Actions workflows that run on pull requests targeting `main` and on pushes to `main`.
- Automated checks on every PR and `main` push equivalent to today’s manual gates: Python **unit** tests (every test not marked `integration`, with no Postgres/Redis available), Gateway build + `ctest`, Admin UI `lint` and production-mode `build` (host Node/npm matching `CURSOR.md` and `admin-ui/package-lock.json` — not relying on the `next dev` / pnpm container for the lint/build gate).
- Python **integration** tests (Postgres/Redis-backed, marked `integration`) against ephemeral CI Postgres (pgvector-capable where tests need it) and Redis — run on a PR only after it receives an approving review (and again after re-approval of any newer commit), and on every push to `main`.
- Every Postgres/Redis-backed test is labelled `integration`; an unlabelled one fails the every-PR unit check rather than silently passing.
- Verify that the existing service Dockerfiles (`Dockerfile.python`, `Dockerfile.adminui`) **build successfully** on every PR and on every `main` run.
- Publish the **Python** service image to GHCR on successful `main` builds, tagged with the commit SHA (and a moving `main` tag).
- Two required statuses for branch protection — `CI / gate` (every-PR checks) and `Integration / integration-gate` (integration tests) — so a PR can merge only when both are green on its head commit.
- Short operator notes: how to require both checks on `main`, the approval/re-approval behavior of integration, which secrets/permissions GHCR publish needs, and how a future host would pull the published SHA tag (documented path — not an always-on deploy).

**Out:**
- Automatic deploy over SSH (or similar) to a staging/production host in this iteration.
- A new production Admin UI image / multi-stage Next.js Dockerfile; publishing Admin UI to GHCR as a deployable artifact.
- Running the full local stack (`./deployment/sh/dev.sh` with LLM/STT/TTS) or browser/telephony e2e as a PR required check.
- Repo-wide Python lint/typecheck gates that the repo does not already define.
- Changing the FreeSWITCH prebuilt GitHub Release pipeline.

## Acceptance criteria

1. Given a pull request that changes application code against `main`, when CI runs, then the workflow starts without any existing `.github` config having been present before this feature.
2. Given CI runs on a PR, when the Python unit job executes, then every test not marked `integration` passes with no Postgres or Redis reachable from the job.
3. Given CI runs on a PR, when the Gateway job executes, then the C++ project configures, builds, and `ctest --test-dir build --output-on-failure` passes.
4. Given CI runs on a PR, when the Admin UI job executes, then `lint` and `build` both pass using the Admin UI package manager and lockfile already used in that tree.
5. Given a PR where any of the Python unit tests, Gateway ctest, Admin UI lint/build, or Docker build verification fails, when `CI / gate` is evaluated, then it fails.
6. Given a PR where all of those checks pass, when `CI / gate` is evaluated, then it succeeds without waiting for integration tests.
7. Given a push to `main` that passes CI (including integration tests), when the publish job runs, then a Python image built from `deployment/docker/Dockerfile.python` is pushed to GHCR tagged with the commit SHA.
8. Given GHCR publish succeeds, when an operator inspects the package, then the SHA-tagged image is pullable with the repo’s GitHub Packages permissions (no plaintext registry password committed in the repo).
9. Given a PR or `main` run, when the Docker build verification runs, then both `Dockerfile.python` and `Dockerfile.adminui` build without error (Admin UI image need not be published).
10. Given CI secrets and logs, when a job fails or succeeds, then no decrypted API keys, JWT secrets, `SECRET_ENCRYPTION_KEY`, or `.env` contents from `deployment/.env` appear in workflow logs or committed workflow files.
11. Given the operator notes shipped with this feature, when an operator configures branch protection on `main`, then the docs name both exact checks they must require — `CI / gate` and `Integration / integration-gate` — and state that a new commit after approval needs re-approval before integration re-runs.
12. Given the CD path docs shipped with this feature, when an operator has a host that can `docker pull` the published SHA tag, then the docs state the pull/tag/compose steps without requiring a second design pass for “how do we get an artifact.”
13. Given a workflow is re-run on the same commit after a flake, when checks pass, then the re-run check (`CI / gate` or `Integration / integration-gate`) reports success (re-runs are supported; no one-shot-only gate).
14. Given Actions is disabled or GHCR publish lacks `packages: write` (or equivalent), when publish is attempted on `main`, then the job fails loudly rather than reporting a false “deployed/published” success.
15. Given a PR with no approving review, when its checks are listed, then no integration tests have run and `Integration / integration-gate` is not green.
16. Given a PR receives an approving review, when integration runs, then every test marked `integration` runs against CI-provided Postgres and Redis (not a hardcoded developer laptop DSN) and `Integration / integration-gate` passes only if they all pass.
17. Given a PR with no successful integration run on its head commit, when a comment-only (or request-changes) review is submitted, then no integration tests run and `Integration / integration-gate` does not turn green.
18. Given an approved PR whose integration already passed on its head commit, when a later comment-only review is submitted, then `Integration / integration-gate` stays green.
19. Given an approved PR with green integration, when a new commit is pushed, then `Integration / integration-gate` is not green for the new head commit until a new approving review triggers integration on it.
20. Given a push to `main`, when CI runs, then integration tests run against CI-provided Postgres and Redis and `CI / gate` fails if any of them fails.
21. Given branch protection configured per the docs, when either `CI / gate` or `Integration / integration-gate` is not green on the PR head commit, then GitHub blocks the merge; when both are green, merge is allowed.
22. Given a test that opens a Postgres or Redis connection but is not marked `integration`, when the unit job runs on a PR, then that test fails or errors and `CI / gate` fails.

## Constraints

- Match the commands and toolchain already documented for humans: `CURSOR.md` (pytest; `cmake` + `ctest`; Admin UI lint/build); Python runtime target **3.11**.
- Do not invent a repo-wide mypy/ruff gate (`CURSOR.md`, `.cursor/rules/testing.mdc`).
- Local/dev compose remains the source of truth for how services run together (`deployment/sh/dev.sh`, `deployment/docker/docker-compose.yml`); CI must not require committing `deployment/.env` or real secrets (tenant-isolation / secrets rules).
- Many pytest modules expect Postgres + Redis (`services/*/tests/conftest.py`, `libs/*/tests/conftest.py`); CI must supply them for integration runs only. Schema apply must follow existing SQL apply conventions if tests need a loaded schema (`database/*.sql`, `docs/setup.md` / init scripts) — do not invent a parallel migration system.
- Admin UI Dockerfile is explicitly hot-reload/`next dev` (`docs/docker-startup.md` §10); CI lint/build is the Admin UI quality gate, not that image’s runtime mode.
- Prefer GitHub-native auth for GHCR (`GITHUB_TOKEN` + least privilege) over long-lived personal tokens in repo secrets.
- No commit/push of secrets; workflows must not echo secret values.

Assumption: default branch is `main` (current `origin`/`upstream` HEAD).
Assumption: “CD as appropriate” for this iteration means **build + publish Python image + documented pull path**, not live auto-deploy.
Assumption: unit plus integration together cover the full repo-root `pytest` suite (same as `CURSOR.md`), not a narrowed subset, unless a test is already marked skip/xfail in-tree. Per user decision, the integration half runs after approval and on `main` pushes rather than on every PR push; nothing is dropped, since merge still requires it green.

## Open questions

1. Is there a concrete staging/production host (and compose layout) that should be wired for automated deploy in a follow-up, or is registry publish the lasting CD boundary for now?
