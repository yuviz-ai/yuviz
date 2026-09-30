# CI/CD

GitHub Actions gates PRs and `main` pushes. Python tests are split: unit tests
(no Postgres/Redis) run on every PR push; tests marked `integration` run once a
PR has an approving review, and on every push to `main`. Publish of the Python
image to GHCR runs only after a successful CI run on `main` (which includes
integration), via a separate workflow loaded from the default branch.

## Prerequisite: Actions enabled for fork PRs

Contributors open PRs from forks. In **Settings → Actions → General** (repo or
org admin):

- Actions permissions: allow all actions, or at least GitHub-authored actions
  plus `docker/login-action`.
- Fork pull request workflows: run workflows from fork PRs of collaborators
  without extra approval (or approve each run from the PR's Checks tab).

If a PR shows no `CI` checks at all, this setting is the first thing to check.

## Branch protection

All of the following are **required** on `main` — the fences below depend on them.

Required status checks, named exactly:

```text
CI / gate
Integration / integration-gate
```

`CI / gate` is the aggregator job in `.github/workflows/ci.yml`.
`Integration / integration-gate` is the gate job in
`.github/workflows/integration.yml`; its branch-protection context is the job
name `integration-gate`.

Required review settings:

- **Require review from Code Owners** (`.github/CODEOWNERS` covers `.github/**`,
  including every workflow)
- **Dismiss stale pull request approvals when new commits are pushed**
- **Require approval of the most recent reviewable push**

Code Owners review is merge-time friction for workflow edits; it does **not**
stop unmerged PRs from running mutated workflow YAML from the PR head.

Recommended (stronger): a repository ruleset **"Require workflows to pass"**
pinning `ci.yml` and `integration.yml` from `main`. Required checks match by
name only, so a PR can add its own workflow that emits a green `gate` or
`integration-gate`; the ruleset closes that.

### Tag ruleset and merge methods (required)

- Tag ruleset targeting all tags (`refs/tags/*`): restrict creation, update and
  deletion to maintainers. Minimum fallback: block tags named `main`,
  `origin/*` and `refs/*`. Publish's `head_branch == 'main'` is also satisfied
  by a pushed **tag** named `main`, and a tag `origin/main` shadows the short
  remote-branch name.
- Merge methods: allow merge commits and/or squash only; **disable rebase
  merging**. Publish's first-parent check relies on this.

### Secrets invariant

No repository- or organization-level Actions secrets for this repo. Every
secret lives in an Environment with a `main`-only deployment rule (today only
`ghcr-publish`). PR-triggered runs can then reach no secret, whatever their YAML
says.

## What CI runs

Workflow: `.github/workflows/ci.yml` (`name: CI`), on `pull_request` and `push`
to `main`.

| Job | What |
|-----|------|
| `python-unit` | `pytest -m "not integration"` with **no** Postgres/Redis; `POSTGRES_DSN`/`REDIS_URL` point at closed port 1 (tripwire) |
| `gateway` | `cmake` Release build + `ctest --test-dir build --output-on-failure`; provisions `build/models/silero_vad.onnx` from the pinned faster-whisper wheel and **fails if any test is skipped** (the Silero VAD tests `GTEST_SKIP` without the model) |
| `admin-ui` | `npm ci` / `npm run lint` / `npm run build` in `admin-ui/` (npm + `package-lock.json`, not pnpm) |
| `docker-build` | Build `Dockerfile.python` and `Dockerfile.adminui` as `yuviz-*:ci` — **no push** |
| `actionlint` | Lints every workflow (pinned, checksum-verified actionlint release; shellcheck at warning level) |
| `integration` | Push only: calls `integration.yml` as a reusable workflow (no `secrets:`) |
| `gate` | Requires `success` from the five leaf jobs; `integration` must be `success` on push and `skipped` on PRs |

A new push to a PR cancels that PR's in-flight `CI` run. `main` pushes are never
cancelled, because publish keys off each one.

Third-party actions are pinned to commit SHAs (tag in a trailing comment), and
the onnxruntime tarball is checked against a SHA-256. When bumping a version,
update the SHA too.

The torch and faster-whisper versions come from `requirements.txt` (the CPU
torch wheel is installed first, and passed to `Dockerfile.python` as
`TORCH_VERSION`), so bumping them there is enough.

Workflow: `.github/workflows/integration.yml` (`name: Integration`).

| Job | What |
|-----|------|
| `python-integration` | `pytest -m integration` against ephemeral Postgres (`pgvector/pgvector:pg14`) + Redis; schemas applied with `psql -v ON_ERROR_STOP=1` |
| `integration-gate` | The required check; see below |

Top-level permissions in both workflows are `contents: read`; the gate jobs add
`actions: read`. Neither workflow uses an Environment, logs into GHCR, or reads
`GHCR_PUSH_*` / `GHCR_IMAGE`.

## Integration tests (approval-gated)

Triggers of `integration.yml`:

- `pull_request_review` (submitted). `python-integration` runs only when the
  review is an approval. Not `pull_request_target`, no Environment.
- `workflow_call`: `ci.yml` calls it on every push to `main`, so `main` runs
  integration through `CI`.
- `workflow_dispatch`: **main-only**. `integration-gate` fails a manual run on
  any other ref, so a dispatch on a PR branch cannot put a green gate on it.

Why `integration-gate` and not `python-integration` is required: a job skipped
by `if:` reports **Skipped**, and GitHub counts Skipped as passing a required
check. A "Comment" review would otherwise satisfy protection without running
anything. The gate:

- fails any review event on a PR whose base is not `main`;
- on an approval (or a `main` push) passes only if `python-integration` succeeded;
- on a comment / changes-requested review passes only if a prior successful
  approval-triggered Integration run exists for the PR head SHA **on this PR
  into `main`**; otherwise it is red with "none has succeeded … yet".

Before approval the PR shows a red `integration-gate` (or Expected); both block
merge.

**New commits after approval:** the new head SHA has no `integration-gate`, so
merge is blocked until someone approves again, which runs integration on the
new head. The required stale-approval settings make re-approval mandatory.
"Re-run jobs" on the old run re-tests the old merge ref and does not satisfy
the new head.

**Flakes:** re-run failed jobs in the approval-triggered run. A red
comment-triggered gate turns green on re-run once an approval run has succeeded.

**Forks:** fork PRs get a read-only token and no secrets, which integration does
not need. The runs API leaves `pull_requests[]` empty for fork-headed runs, so
for those the gate matches a prior approval run by head SHA **and** the PR's
head repository instead. Without that, every bot or comment review (e.g.
CodeRabbit) after approval would turn an approved fork PR red.

## Marking rule and tripwire

A test module gets module-level `pytestmark = pytest.mark.integration` if
**any** test in it opens a Postgres or Redis connection — through the conftest
`pool`-family fixtures, `services.*.db.get_pool()`, `asyncpg.connect` /
`create_pool`, or `redis.from_url` against `REDIS_URL`. A mixed module is marked
as a whole.

`python-unit` has no service containers and points `POSTGRES_DSN` /
`REDIS_URL` at `127.0.0.1:1`, so an unmarked DB/Redis test fails
`CI / python-unit` (and `CI / gate`) with connection refused instead of passing
silently.

**Limit:** a test that swallows its own connection error (the
`test_cache_degradation.py` pattern) passes in unit without touching a database
and is never selected by `-m integration`. Reviewers must apply the rule.

## Local commands

```bash
pytest -m "not integration"      # unit only; no Postgres/Redis needed
pytest -m integration            # needs local Postgres + Redis with schemas applied (docs/setup.md)
pytest                           # everything
# prove nothing unmarked needs a DB (same tripwire as CI):
POSTGRES_DSN=postgresql://x@127.0.0.1:1/x REDIS_URL=redis://127.0.0.1:1/0 pytest -m "not integration"
```

## Pytest exclusions (live Ollama / macOS `say`)

No Ollama/STT/TTS in CI this iteration. A `--deselect` outside a job's `-m`
selection is a no-op, so each nodeid lives only in the job that selects it.
When a new live-infra test lands, add it to that job **and** here.

Unit (`python-unit` in `ci.yml`):

```text
services/conversation/tests/test_ai_provider_manager.py::TestRealOllamaFactory::test_real_ollama_generates_a_token_stream
services/conversation/tests/test_ai_provider_manager.py::TestRealMacosTtsFactory::test_get_tts_creates_real_macos_instance_and_synthesizes_audio
```

Integration (`python-integration` in `integration.yml`):

```text
services/knowledge/tests/test_retrieval.py::test_retrieve_end_to_end_with_real_ollama_embeddings
services/knowledge/tests/test_retrieval.py::test_prompt_mode_document_coexists_with_vector_search_results
services/knowledge/tests/test_retrieval.py::test_prompt_mode_document_excluded_from_ordinary_vector_search
services/knowledge/tests/test_ingestion_worker.py::test_process_one_job_success_produces_ready_document_and_chunks
services/knowledge/tests/test_ingestion_worker.py::test_manual_prompt_override_survives_reingestion_of_large_document
```

## GHCR publish (main only)

Workflow: `.github/workflows/publish.yml`. Triggered by `workflow_run` of `CI`
completed. GitHub always loads this file from the **default branch**, so a PR
cannot rewrite the publish control. `CI` success on a `main` push includes
integration (`gate` requires it), so publish only ships commits that passed
unit **and** integration tests.

Provenance checks before any secret or registry step:

- the `CI` run's head repository must be this repository;
- the SHA must be on the first-parent history of `refs/remotes/origin/main`
  (fully qualified, so a tag `origin/main` cannot shadow it). First-parent
  commits are those that were once the tip of `main`; commits inside a merged
  PR, side branches and tag-only commits are refused.

### Required Environment: `ghcr-publish`

Deployment branches: **Selected → `main` only**.

Environment secrets (never repository secrets, never in `ci.yml` or `integration.yml`):

| Secret | Purpose |
|--------|---------|
| `GHCR_PUSH_USERNAME` | Dedicated publish bot GitHub username |
| `GHCR_PUSH_TOKEN` | Bot PAT with `write:packages` / `read:packages` on packages the bot owns |
| `GHCR_IMAGE` | Full image name, e.g. `ghcr.io/<bot-username>/yuviz-python` |

Use a **bot-owned** `GHCR_IMAGE` (`ghcr.io/<bot>/yuviz-python`), **not**
`ghcr.io/${{ github.repository_owner }}/…`. A same-repo PR can still elevate
`GITHUB_TOKEN` with `permissions: packages: write` and push to namespaces that
token may write (org/repo-linked packages). It cannot push to another user’s
package namespace — that is the enforceable fence.

The Actions read-only default does **not** block `packages: write`. Workflows
can still elevate via a job/workflow `permissions:` key and push to packages
that `GITHUB_TOKEN` can write. Do not design around that false control.

After the first successful push, in GHCR package settings for that bot-owned
package: do not grant this repository’s Actions / `GITHUB_TOKEN` write access.
Only the bot PAT should write.

Publish tags: `$GHCR_IMAGE:<commit-sha>` always, and `$GHCR_IMAGE:main`
(convenience pointer only) only when that SHA is still the tip of `main`,
re-fetched right before the push. A re-run of an older CI run, or an older
publish that finishes after a newer one, cannot move `:main` backwards. Missing Environment / secrets / `GHCR_IMAGE` or a
failed push fails the job loudly (no `continue-on-error`).

## Consume a published image (manual)

SHA tags are the trust anchor for deploy. Do **not** treat `:main` as the pin.

```bash
# Replace <bot> and <commit-sha> with the bot GHCR namespace and the CI commit.
docker pull ghcr.io/<bot>/yuviz-python:<commit-sha>
docker tag  ghcr.io/<bot>/yuviz-python:<commit-sha> yuviz-python:dev
# then compose up using existing deployment/docker/docker-compose.yml image name
```

Automatic SSH deploy to a host is out of scope for this iteration.

## Out of scope (this iteration)

- Full `./deployment/sh/dev.sh` stack in CI (Ollama / STT / TTS)
- Admin UI image publish to GHCR (Dockerfile is `next dev`, not a production artifact)
- FreeSWITCH prebuilt release pipeline changes
- Repo-wide mypy / ruff gates (none configured in-tree)
- Auto-SSH deploy to staging/production
