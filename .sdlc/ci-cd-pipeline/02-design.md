# Design: CI/CD pipeline for yuviz

## Approach

Three workflows. `.github/workflows/ci.yml` is the unprivileged every-PR/`main` gate (Python **unit** tests with no Postgres/Redis, ctest, Admin UI lint/build, Docker build verify, aggregator `gate`). The new `.github/workflows/integration.yml` runs Python tests marked `integration` against throwaway Postgres/Redis. On a PR it runs only after an **approving review** (`pull_request_review`). On push to `main`, `ci.yml` calls it as a reusable workflow, so `CI` on `main` means "unit **and** integration passed". `publish.yml` keeps its trigger (`workflow_run` of `CI`, push, `main`, success), so it can only publish a commit that passed both. It gains two provenance checks: the head repo must be this repo, and the SHA must be on the **first-parent** chain of `refs/remotes/origin/main`. That chain holds only commits that were once the tip of `main`. Those checks close the tag-named-`main` bypass of `head_branch`. Tests are classified by an explicit module-level `pytestmark = pytest.mark.integration` on the 29 modules that need a database or Redis. The unit job points `POSTGRES_DSN`/`REDIS_URL` at a closed port, so a DB-backed test that was left unmarked fails loudly on every PR instead of silently passing. GHCR writes still use Environment `ghcr-publish` (`main` only) + `GHCR_PUSH_TOKEN` (bot PAT), pushing to a **bot-owned image name** the repo `GITHUB_TOKEN` cannot write. Do **not** treat Actions "read-only default" as blocking `packages: write` (lesson 1). No SSH deploy, no Admin UI publish, no new linters.

**PRD deviation (user decision, supersedes AC 2/5 timing):** DB/Redis-backed pytest no longer runs on every PR push. It runs once a PR has an approving review, and on every push to `main`. Merging is still blocked until it passes, because branch protection requires `integration-gate` (see Docs). Nothing is dropped: every test still runs before merge, and the full suite runs before publish.

**Why not the obvious alternatives:** A separate `workflow_run`-on-Integration trigger in `publish.yml` would need a cross-workflow "did the other one pass on this SHA" lookup plus double-publish dedupe. Calling the integration workflow from `ci.yml` on push avoids both and keeps a single publish trigger. Auto-marking by fixture name in a root `conftest.py` would miss modules that open Redis/asyncpg directly (`test_redis_repository.py`, `test_provider_config_subscriber.py`, `test_transcript_builder.py`), so it would still need manual marks. One explicit rule is simpler.

## Changes

| File | Change | Why |
|------|--------|-----|
| `pyproject.toml` | Under `[tool.pytest.ini_options]` add `markers = ["integration: needs Postgres/Redis; runs in the Integration workflow"]`. | Registers the marker so `-m` selection is unambiguous and there is no unknown-mark warning. |
| 29 test modules (list under Interfaces → Test classification) | Add `pytestmark = pytest.mark.integration` after the imports, and `import pytest` where the module lacks it. Nothing else changes: no test is moved, split, or edited. | Classification. Module-level marking keeps the rule greppable. |
| `.github/workflows/ci.yml` | Rename job `python` to `python-unit`: remove `services:`, the postgresql-client step and the schema-apply step. Set unreachable `POSTGRES_DSN`/`REDIS_URL`. Run `pytest -m "not integration"` with the 2 unit-side deselects. Add job `integration` (`if: github.event_name == 'push'`, `uses: ./.github/workflows/integration.yml`, `permissions: contents: read, actions: read`, no `secrets:`). `gate` needs `[python-unit, gateway, admin-ui, docker-build, integration]`. On `pull_request`, `integration` must be `skipped`; on `push`, it must be `success`. | Fast every-PR checks. On `main`, `CI` success now implies integration passed, which is what `publish.yml` keys on. |
| `.github/workflows/integration.yml` (new) | `name: Integration`. `on: pull_request_review (submitted)`, `workflow_call`, `workflow_dispatch`. Jobs: `python-integration` (what the old `python` job did, now with `-m integration` and the 5 integration-side deselects) and `integration-gate` (the required check; closes the "skipped counts as passed" hole). | Integration runs after approval, and on `main` via `ci.yml`. |
| `.github/workflows/publish.yml` | Job `if` adds `github.event.workflow_run.head_repository.full_name == github.repository`. Checkout gets `fetch-depth: 0`. A new step before any secret/registry step requires `$SHA` to appear in `git rev-list --first-parent refs/remotes/origin/main` and fails otherwise. The trigger, Environment, bot PAT and `GHCR_IMAGE` are unchanged. | `head_branch == 'main'` alone is satisfied by a pushed **tag** named `main`. The provenance checks bind publish to commits actually on `main` in this repo. |
| `.github/CODEOWNERS` | **Unchanged** (`.github/**` already covers `integration.yml`). | — |
| `docs/ci-cd.md` | Update the job table and the pytest-exclusions section, which now has unit and integration lists. Add an "Integration tests (approval-gated)" section, the second required check, the **required** review settings (Code Owners review, dismiss stale approvals, approval of the most recent push), the new-commit and fork behavior, local commands, the marking rule, the required tag ruleset (tag creation restricted to maintainers) and the merge-commit/squash-only merge setting, the optional "Require workflows to pass" ruleset, and the no-repo/org-secrets invariant. | Operators need to know the second required check, the settings the fences depend on, and the re-approval behavior. |
| `CURSOR.md` | In the test-commands block (line ~66), add `pytest -m "not integration"` (no DB needed) and `pytest -m integration` (needs local Postgres/Redis). | Agents and developers run the same split locally. |

Files touched: 29 test modules + `pyproject.toml` + `ci.yml` + `integration.yml` (new) + `publish.yml` + `docs/ci-cd.md` + `CURSOR.md` = **35**.

## Data

None.

## Interfaces

### Test classification

**Rule:** a test module gets module-level `pytestmark = pytest.mark.integration` if **any** test in it opens a Postgres or Redis connection. That includes connections made through the conftest `pool`-family fixtures (`pool`, `knowledge_pool`, `test_tenant`, `test_admin`, `test_superadmin`, `test_viewer`, `tenant_agent`, `test_carrier`, `test_agent`), through `services.*.db.get_pool()`, through `asyncpg.connect`/`create_pool`, or through `redis.from_url` against `REDIS_URL`. A mixed module is marked as a whole, and its few pure tests (for example `test_transcript_builder.py`'s `pool=None` case and the route-inventory tests in `test_console_gate.py`) then run only in integration. That is accepted, because nothing is split or moved.

**Exact modules (29).** This list was derived empirically: running root `pytest` (with the 7 current deselects) against `POSTGRES_DSN=postgresql://ci@127.0.0.1:1/none` and `REDIS_URL=redis://127.0.0.1:1/0` produced ERROR/FAILED results in exactly these modules and in no others:

```text
libs/config_sdk/tests/test_http_repository.py
libs/config_sdk/tests/test_redis_repository.py
libs/knowledge_sdk/tests/test_http_repository.py
services/campaigns/tests/test_campaign_contacts.py
services/campaigns/tests/test_campaigns.py
services/campaigns/tests/test_dnc.py
services/campaigns/tests/test_worker.py
services/config/tests/test_agents.py
services/config/tests/test_api.py
services/config/tests/test_bootstrap.py
services/config/tests/test_calls.py
services/config/tests/test_console_gate.py
services/config/tests/test_invite_routes.py
services/config/tests/test_invites.py
services/config/tests/test_phone_numbers.py
services/config/tests/test_provider_configs.py
services/config/tests/test_tenants.py
services/config/tests/test_users.py
services/config/tests/test_workflows.py
services/conversation/tests/test_provider_config_subscriber.py
services/conversation/tests/test_transcript_builder.py
services/did/tests/test_numbers_api.py
services/did/tests/test_purchased_numbers.py
services/knowledge/tests/test_agent_kb.py
services/knowledge/tests/test_documents.py
services/knowledge/tests/test_ingestion_worker.py
services/knowledge/tests/test_knowledge_bases.py
services/knowledge/tests/test_retrieval.py
services/knowledge/tests/test_retrieval_policies.py
```

These stay **unmarked on purpose**. `services/config/tests/test_cache_degradation.py` tests Redis-outage handling against a deliberately dead `localhost:9999`. `libs/*/tests/test_cache_aside.py` use fake repos. `services/config/tests/test_email.py` passed without a database.

**Tripwire (the unit job must be able to fail):** `python-unit` has no service containers, and its env sets
- `POSTGRES_DSN=postgresql://ci-unit@127.0.0.1:1/unit-no-db`
- `REDIS_URL=redis://127.0.0.1:1/0`

With nothing listening on port 1, asyncpg and redis raise connection-refused immediately. An unmarked DB/Redis test therefore ERRORs or FAILs in `CI / python-unit`, and `CI / gate` goes red. The failure mode was exercised as described above: all 29 modules failed loudly and none hung. Using an explicit closed port rather than the conftest `satish@localhost:5432` default keeps this deterministic, even if a runner image ever starts a local Postgres. **Limit:** a test that swallows its own connection error (the `test_cache_degradation.py` pattern) passes in unit without exercising a database. Because the integration job selects only `-m integration`, such a test would never hit a real DB. The rule above is the guard, and the test plan re-runs the unreachable-DSN sweep.

**Deselect split.** A `--deselect` of a nodeid outside the `-m` selection is a no-op, so each list goes only where the test lives:
- `python-unit`, 2 nodeids:
  ```text
  --deselect services/conversation/tests/test_ai_provider_manager.py::TestRealOllamaFactory::test_real_ollama_generates_a_token_stream
  --deselect services/conversation/tests/test_ai_provider_manager.py::TestRealMacosTtsFactory::test_get_tts_creates_real_macos_instance_and_synthesizes_audio
  ```
- `python-integration`, 5 nodeids:
  ```text
  --deselect services/knowledge/tests/test_retrieval.py::test_retrieve_end_to_end_with_real_ollama_embeddings
  --deselect services/knowledge/tests/test_retrieval.py::test_prompt_mode_document_coexists_with_vector_search_results
  --deselect services/knowledge/tests/test_retrieval.py::test_prompt_mode_document_excluded_from_ordinary_vector_search
  --deselect services/knowledge/tests/test_ingestion_worker.py::test_process_one_job_success_produces_ready_document_and_chunks
  --deselect services/knowledge/tests/test_ingestion_worker.py::test_manual_prompt_override_survives_reingestion_of_large_document
  ```
  Do **not** deselect mocked Ollama tests (`TestOllamaThinkResolution`, `test_ollama_llm.py`, constructor-only `test_get_llm_creates_real_ollama_instance`) or knowledge tests that never call `embed` / real Ollama. Prefer `--deselect` over `-k`. When a new live-Ollama/`say` test lands, add it to the list for the job that selects it, and to `docs/ci-cd.md`.

### Local dev commands

```bash
pytest -m "not integration"      # unit only; no Postgres/Redis needed
pytest -m integration            # needs local Postgres + Redis with schemas applied (docs/setup.md)
pytest                           # everything (unchanged)
# prove nothing unmarked needs a DB (same tripwire as CI):
POSTGRES_DSN=postgresql://x@127.0.0.1:1/x REDIS_URL=redis://127.0.0.1:1/0 pytest -m "not integration"
```

### Workflow triggers

**`ci.yml` (`name: CI`), unchanged triggers:**
- `on.pull_request.branches: [main]`
- `on.push.branches: [main]`
- No `paths:` filters.

**`integration.yml` (`name: Integration`), new:**
- `on.pull_request_review.types: [submitted]`. `pull_request_review` has no `branches` filter. Approved PRs to any base branch run it, which is harmless.
- `on.workflow_call` (no inputs, no secrets). `ci.yml` calls it on push to `main`.
- `on.workflow_dispatch`: manual runs are **main-only**. `integration-gate` fails a dispatch on any other ref, so a dispatch on a PR branch cannot put a green bare `integration-gate` on the PR head.
- **Not** `pull_request_target` (it runs PR code with a write token and secrets: the pwn-request risk). **Not** an Environment with required reviewers.
- Workflow YAML comes from the PR merge ref, the same trust level as `ci.yml` on `pull_request`. It has no secrets and a `contents: read` token. Fork PRs get a read-only token and no secrets, which is fine because integration needs none.

**`publish.yml` (trigger unchanged):**
- `on.workflow_run`: `workflows: ["CI"]`, `types: [completed]`. GitHub always loads this file from the **default branch**.
- Do **not** put `packages: write` or a publish job in `ci.yml` or `integration.yml`.

### Token permissions (enforceable GHCR fence) — unchanged

GitHub's model: repository/org "Workflow permissions = read" does **not** stop a workflow from setting `permissions: packages: write` on `GITHUB_TOKEN` (lesson 1).

**Enforceable fence (mandatory):**

1. **Environment `ghcr-publish`**: deployment branches **Selected → `main` only**. Environment-only secrets:
   - `GHCR_PUSH_TOKEN`: PAT for a **dedicated publish bot user** (fine-grained: `write:packages` / `read:packages` on packages that bot owns).
   - `GHCR_PUSH_USERNAME`: that bot's GitHub username.
   - `GHCR_IMAGE`: for example `ghcr.io/<bot-username>/yuviz-python` (**not** `ghcr.io/${{ github.repository_owner }}/…`).
2. **`ci.yml` and `integration.yml` never** declare `environment:`, read `GHCR_PUSH_*` / `GHCR_IMAGE`, log into `ghcr.io`, or pass `secrets:` (the `ci.yml` → `integration.yml` call has **no** `secrets: inherit`).
3. **Operators consume only SHA tags** from `GHCR_IMAGE`. `:main` is a convenience pointer only.
4. **`.github/CODEOWNERS`** requires review on `.github/**` before merge.

**`ci.yml` workflow level:** `permissions: contents: read`, unchanged. The caller job `integration` sets `permissions: { contents: read, actions: read }`. A called workflow's jobs cannot exceed the caller job's grant, and `integration-gate` requests `actions: read`, so without this grant the call fails at startup. `actions: read` is read-only run metadata.

**`integration.yml` workflow level:** `permissions: contents: read`. Job `integration-gate` adds `actions: read`. Neither ever sets `packages`, `contents: write`, or `pull-requests: write`.

**All jobs in both files** check out with `persist-credentials: false`. None uses an Environment or a registry login.

**`publish.yml` job level:** `permissions: contents: read`, `environment: ghcr-publish`, bot PAT login, pushes `$GHCR_IMAGE:${{ github.event.workflow_run.head_sha }}` and `$GHCR_IMAGE:main`, and fails loudly on missing secrets. It adds the provenance checks under the `publish-python` job below.

### Job: `python-unit` (`ci.yml`, replaces `python`)

- Runner `ubuntu-latest`, `timeout-minutes: 60` (the cold CPU-torch install dominates).
- **No `services:`.**
- Env: `POSTGRES_DSN=postgresql://ci-unit@127.0.0.1:1/unit-no-db`, `REDIS_URL=redis://127.0.0.1:1/0`, `JWT_SECRET` = the existing CI throwaway string. Do not set `SECRET_ENCRYPTION_KEY`.
- Steps:
  1. Checkout, `persist-credentials: false`.
  2. `actions/setup-python` 3.11, pip cache on `requirements.txt`.
  3. CPU torch then requirements, exactly as now (`torch==2.12.1` from `download.pytorch.org/whl/cpu`, then `pip install -r requirements.txt`).
  4. Provision the Silero VAD model exactly as now (`libs/vad_sdk/tests/test_silero_vad.py` is unit and has no skip). Keep `test -f "$src"`, with no `|| true`.
  5. `pytest -m "not integration"` + the 2 unit deselects.
- Drop the `postgresql-client` install and the schema-apply step. Nothing in this job talks to a DB.

### Job: `integration` (`ci.yml`, new caller)

```yaml
integration:
  if: github.event_name == 'push'
  permissions:
    contents: read
    actions: read
  uses: ./.github/workflows/integration.yml
```

No `with:`, no `secrets:`. Nested check names appear as `integration / python-integration` and `integration / integration-gate`. These do **not** match the bare `integration-gate` required context.

### Workflow `integration.yml` / Job: `python-integration`

- `if: github.event_name != 'pull_request_review' || github.event.review.state == 'approved'`. Actions `==` on strings is case-insensitive, so the lowercase webhook value `approved` matches either way. Under `workflow_call`, `github.event_name` is the caller's (`push`), so the job runs.
- Runner `ubuntu-latest`, `timeout-minutes: 60`.
- Service containers, **moved verbatim from today's `python` job**:
  - Postgres `pgvector/pgvector:pg14`, env `POSTGRES_USER/PASSWORD/DB=voiceai`, `ports: ["5432:5432"]`, `options: --health-cmd "pg_isready -U voiceai -d voiceai" --health-interval 5s --health-timeout 5s --health-retries 20`.
  - Redis `redis:7-alpine`, `ports: ["6379:6379"]`, `options: --health-cmd "redis-cli ping" --health-interval 5s --health-timeout 3s --health-retries 20`.
- Env: `POSTGRES_DSN=postgresql://voiceai:voiceai@localhost:5432/voiceai`, `REDIS_URL=redis://localhost:6379/0`, `JWT_SECRET` = the CI throwaway string. Do not load `deployment/.env`, and do not set `SECRET_ENCRYPTION_KEY`.
- Steps, identical to today's `python` job except the final selection:
  1. Checkout, `persist-credentials: false`. The default ref is the PR merge ref for `pull_request_review` and the pushed SHA under `workflow_call`.
  2. `actions/setup-python` 3.11, pip cache on `requirements.txt`.
  3. `sudo apt-get update && sudo apt-get install -y postgresql-client`.
  4. CPU torch then requirements.
  5. Provision the Silero VAD model (same fail-loud copy).
  6. Apply schemas with `psql "$POSTGRES_DSN" -v ON_ERROR_STOP=1 -f` for `database/schema.sql`, `database/knowledge_schema.sql` and `database/telephony_schema.sql`, in that order. No `|| true`. Skip `migrate_workflow_text.py`, service accounts and seed.
  7. `pytest -m integration` + the 5 integration deselects.

### Workflow `integration.yml` / Job: `integration-gate` (required check)

A job skipped by `if:` reports its check as **Skipped**, and GitHub counts Skipped as passing a required check. If `python-integration` were the required check, a "Comment" or "Request changes" review would create a skipped `python-integration` run on the PR head and satisfy branch protection without running anything. `integration-gate` exists to close that hole.

- `needs: [python-integration]`, `if: always()`, `runs-on: ubuntu-latest`, `permissions: { contents: read, actions: read }`.
- One step, env `GH_TOKEN: ${{ github.token }}`, `EVENT`, `STATE: ${{ github.event.review.state }}`, `RESULT: ${{ needs.python-integration.result }}`, `HEAD_SHA: ${{ github.event.pull_request.head.sha }}`, `PR_NUMBER: ${{ github.event.pull_request.number }}`, `BASE_REF: ${{ github.event.pull_request.base.ref }}`. Logic:
  - **Any `pull_request_review`:** first, exit 1 unless `BASE_REF == main` ("Integration gate only certifies PRs into main").
  - **Approving review or `push` (via `workflow_call`):** exit 0 iff `RESULT == success`.
  - **`workflow_dispatch`:** exit 0 iff `github.ref == 'refs/heads/main'` **and** `RESULT == success`. On any other ref, print "Manual Integration runs are main-only; PRs need an approving review" and exit 1.
  - **Non-approving `pull_request_review`** (`commented`, `changes_requested`): exit 0 iff a prior **successful** approval-path run exists for `HEAD_SHA` **on this PR into `main`**:
    `gh api --paginate "repos/$GITHUB_REPOSITORY/actions/runs?head_sha=$HEAD_SHA&event=pull_request_review&status=success"`, then select runs where `.name == "Integration"` and `.pull_requests[]` contains an entry with `.number == PR_NUMBER` and `.base.ref == "main"`. The count must be `> 0`. The event filter excludes dispatch runs. The PR-number and base filters stop a successful run from a **different** PR (for example a throwaway PR into another branch with the same head SHA) from being laundered into this one. The API leaves `pull_requests[]` empty for fork-headed runs, so for fork PRs this lookup never passes and fails closed (see Risks). Otherwise print "Integration runs on an approving review; none has succeeded for $HEAD_SHA yet" and exit 1. This keeps a follow-up comment review on an already-green approved PR from turning it red. Before approval, the PR shows a red `integration-gate` with that message instead of "Expected". Both block merge.
  - Print `EVENT`, `STATE` and `RESULT` for log clarity. No `continue-on-error`, no `|| true`. A failed `gh` call (non-zero exit or non-numeric output) must fail the step rather than being read as "0 runs → fail" or "pass".
- Check name users see and must require: **`Integration / integration-gate`**. The branch-protection context is the job name `integration-gate`.

### Job: `gateway` — unchanged

- Runner `ubuntu-latest`; checkout `persist-credentials: false`.
- Apt: `build-essential`, `cmake`, `pkg-config`, `libspdlog-dev`, `libyaml-cpp-dev`, `nlohmann-json3-dev`, `libwebsockets-dev`, `libgtest-dev`, `libhiredis-dev`, `libgrpc++-dev`, `protobuf-compiler-grpc`, `libprotobuf-dev`.
- onnxruntime pinned `v1.20.1` Linux x64 tarball → `/usr/local/include/onnxruntime`, `/usr/local/lib`, `ldconfig`.
- `CMAKE_PREFIX_PATH=/usr/lib/x86_64-linux-gnu/cmake`; `cmake -B build -DCMAKE_BUILD_TYPE=Release`; `cmake --build build -j"$(nproc)"`; `ctest --test-dir build --output-on-failure`.
- `SileroVADTest` `GTEST_SKIP`s without the model. Do not add a model download.

### Job: `admin-ui` — unchanged

- Node 22, npm cache on `admin-ui/package-lock.json`, `npm ci && npm run lint && npm run build` in `admin-ui/`. npm, not pnpm.

### Job: `docker-build` — unchanged

- `docker build -f deployment/docker/Dockerfile.python -t yuviz-python:ci .` and `docker build -f deployment/docker/Dockerfile.adminui -t yuviz-adminui:ci .`. No push. `timeout-minutes: 90`. No real secrets as build-args.

### Job: `gate` (`ci.yml`, aggregator / required check)

- `needs: [python-unit, gateway, admin-ui, docker-build, integration]`, `if: always()`.
- Print every `needs.*.result`. Require `success` for `python-unit`, `gateway`, `admin-ui` and `docker-build`. For `integration`:
  - `github.event_name == 'push'` → must be `success`.
  - otherwise (`pull_request`) → must be `skipped`. Any other value means the `if:` was changed and should be investigated, so fail.
- No `continue-on-error`. Check name stays **`CI / gate`**.

### Workflow `publish.yml` / Job: `publish-python`

- `if`: `workflow_run.conclusion == 'success'` && `workflow_run.event == 'push'` && `workflow_run.head_branch == 'main'` && **`github.event.workflow_run.head_repository.full_name == github.repository`**.
- `head_branch` is only the ref's short name, so a push of a **tag** named `main` also yields `head_branch == 'main'`. The `if` cannot tell a tag from a branch, so provenance is proven from git:
  - Checkout `ref: ${{ github.event.workflow_run.head_sha }}`, **`fetch-depth: 0`**, `persist-credentials: false`. The full-history checkout brings `refs/remotes/origin/main` in with the checkout's own token. It also fetches all tags, so every ref in this job is **fully qualified**; a short `origin/main` could resolve to an attacker's `refs/tags/origin/main`, because git checks tags before remotes. No separate `git fetch` is used, because credentials are removed afterwards and a private-repo fetch would fail.
  - New first step after checkout, **before** the secrets check and the GHCR login: env `SHA: ${{ github.event.workflow_run.head_sha }}`, default `bash -eo pipefail` shell, run `git rev-list --first-parent refs/remotes/origin/main | grep -Fx "$SHA" >/dev/null`.
    - **First-parent, not `--is-ancestor`:** `--is-ancestor` accepts every commit in `main`'s history, including commits inside a merged PR that never ran `main` CI. The first-parent chain holds only commits that were once the tip of `main` and so ran `CI` on push. This needs merge-commit or squash merges only (see Docs 1a); with rebase merges, every rebased PR commit lands on the first-parent chain.
    - **`grep -Fx … >/dev/null`, not `grep -Fqx`:** `-q` exits at the first match while `rev-list` is still writing. Under `pipefail`, the resulting SIGPIPE (exit 141) would fail the step on a *valid* SHA. Without `-q`, grep drains the whole input. A `rev-list` error or no match (grep exit 1) fails the job; there is no `|| true` and no `if` that swallows it.
    - Print "Refusing to publish $SHA: not on the first-parent history of refs/remotes/origin/main" on failure.
- Since `CI` on push now includes `integration` in `gate`, a `CI` success on a `main` push means unit, gateway, admin-ui, docker-build **and** integration all passed on `head_sha`. There is exactly one publish trigger per `main` commit, so there is no double-publish and no cross-workflow lookup.
- `environment: ghcr-publish`, `permissions: contents: read`. Checkout `workflow_run.head_sha`, `persist-credentials: false`. Log in with `GHCR_PUSH_USERNAME`/`GHCR_PUSH_TOKEN` (never `GITHUB_TOKEN`). Build `Dockerfile.python`, push `$GHCR_IMAGE:<sha>` and `$GHCR_IMAGE:main`. Fail loudly on missing secrets or push failure.

### Behavior: new commits after approval, re-runs, forks

- **New commit after approval:** `integration-gate` was reported on the old head SHA, and the new head SHA has none. Branch protection shows `integration-gate` as Expected, so the PR cannot merge until someone submits a new **approving** review, which triggers integration on the new head. Branch protection **requires** "Dismiss stale pull request approvals when new commits are pushed" and "Require approval of the most recent reviewable push" (see Docs item 1), so re-approval is mandatory and approval and integration stay in lockstep. "Re-run jobs" on the old run re-tests the **old** merge ref and does not satisfy the new head.
- **Re-run after a flake (AC 13):** re-running failed jobs in the approval-triggered run re-evaluates `integration-gate` normally. Re-running a red comment-triggered `integration-gate` after an approval run succeeded makes it green, because the lookup now finds the success.
- **Fork PRs:** `pull_request_review` from a fork gets a read-only token and no secrets, the same exposure as `pull_request` on `ci.yml`. Integration needs no secrets.
- **Who can trigger:** any approving review (including from a user whose approval does not count toward branch protection) starts integration. That is a compute cost, not a privilege: it runs the same code `pull_request` already runs, with the same read-only token.

### Docs: `docs/ci-cd.md`

Must state, without requiring another design:

1. Branch protection on `main`. All of these are **required**, because the fences depend on them:
   - required status checks **`CI / gate`** **and** **`Integration / integration-gate`** (context `integration-gate`);
   - **Require review from Code Owners** (CODEOWNERS on `.github/**`);
   - **Dismiss stale pull request approvals when new commits are pushed**;
   - **Require approval of the most recent reviewable push**.

   Stronger option, recommended: a repository ruleset "Require workflows to pass" pinning `ci.yml` and `integration.yml` from `main`. Required checks match only by name, and a PR can add a workflow emitting a green `gate`/`integration-gate`.
1a. **Tag ruleset (required):** restrict tag creation, update and deletion to maintainers (target all tags, `refs/tags/*`). The minimum acceptable fallback blocks tags named `main`, `origin/*` and `refs/*`. This is defense in depth for the publish `head_branch` check and for ref shadowing. **Merge methods (required):** allow merge commits and/or squash only, and disable rebase merging. The publish first-parent check relies on this.
1b. **Secrets invariant:** no repository- or organization-level Actions secrets for this repo. Any secret lives in an Environment with a `main`-only deployment rule (today only `ghcr-publish`).
2. Job table: `python-unit` (no DB; `-m "not integration"`; closed-port tripwire), `gateway`, `admin-ui`, `docker-build`, `integration` (push only, calls `integration.yml`), `gate`. A separate Integration table: `python-integration`, `integration-gate`.
3. "Integration tests (approval-gated)": triggers, the skipped-counts-as-passed reason for `integration-gate`, new-commit/re-approval behavior, fork behavior, that `main` runs integration on every push through `CI`, and that manual `workflow_dispatch` runs are main-only (the gate fails them on any other ref).
4. Marking rule and tripwire: the module-level `pytestmark` rule verbatim, the fact that an unmarked DB/Redis test fails `CI / python-unit`, and the limit (a test that swallows its own connection error is not caught).
5. Local commands (block above).
6. Pytest exclusions split into unit (2) and integration (5) lists.
7. The unchanged sections stay unchanged: Environment `ghcr-publish` + `GHCR_*` secrets + bot-owned `GHCR_IMAGE` rationale, no false read-only claim, SHA-pin consume path, out-of-scope list. Update the publish sentence to say that `CI` success on `main` includes integration.

### Secrets / logging invariants

- Never `cat`, `echo` or commit `deployment/.env`, `SECRET_ENCRYPTION_KEY`, real JWT secrets, API keys or `GHCR_PUSH_TOKEN`.
- `GHCR_PUSH_TOKEN` lives only in Environment `ghcr-publish`. It never appears in `ci.yml` or `integration.yml`, and it is never passed via `secrets:` to a called workflow.
- **No repository- or organization-level Actions secrets.** Every secret lives in an Environment whose deployment rule is `main` only. PR-triggered runs (`pull_request`, `pull_request_review`, PR-mutated YAML) can therefore reach no secret, whatever their YAML says.

## Risks

- A DB/Redis-backed test left unmarked would silently pass in unit if the unit job had a database. Mitigation: `python-unit` has no services and points `POSTGRES_DSN`/`REDIS_URL` at closed port 1, so such a test ERRORs and reddens `CI / gate`. This was exercised: all 29 modules fail loudly under that env.
- A test that swallows its own connection error (the `test_cache_degradation.py` pattern) passes in unit without a DB and never runs in `-m integration`, so its real-DB coverage is silently lost. Mitigation: the marking rule is "any Postgres/Redis connection", reviewers apply it, and the test plan re-runs the closed-port sweep.
- A skipped job counts as passing for branch protection, so a comment review would satisfy a `python-integration` required check without running anything. Mitigation: require `integration-gate` (`if: always()`). It passes a non-approving-review run only when a prior successful `pull_request_review` Integration run exists for the PR head SHA **and** lists this PR number with base `main`. It fails any PR whose base is not `main`.
- The prior-run lookup could launder a green run from another PR that shares the head SHA. Mitigation: the `gh api …/actions/runs?head_sha=&event=pull_request_review&status=success` result is filtered to runs whose `pull_requests[]` contains this PR's number with `base.ref == "main"`.
- For fork PRs, the runs API returns an empty `pull_requests[]`, so the comment-review lookup never matches. Mitigation: it fails closed. A comment review on an approved fork PR turns `integration-gate` red until the PR is re-approved; that is accepted as a UX cost, not a hole.
- Required status checks match by **name only**, so a PR that adds a workflow with a job named `integration-gate` or `gate` can post a green check on its head SHA. `CI / gate` has shared this since the first design. Mitigation, partial: Code Owners review on `.github/**` covers workflows added in PRs into `main`. **Residual (open, medium):** it does **not** cover a spoof workflow pushed on an unreviewed side branch and triggered by a second PR into that branch at the same head SHA. The runs API's `pull_requests[]` may also list PR1 for a run triggered by PR2, which is unverified against the live API. The outcome is that code which skipped PR integration or unit checks can merge. It is bounded because `main` CI re-runs everything on push and publish requires that run. Docs recommend a repository ruleset "Require workflows to pass" (pinning `ci.yml`/`integration.yml` from `main`) as the control that closes it.
- `publish.yml`'s `head_branch == 'main'` is also satisfied by a pushed **tag** named `main`, which could publish an unreviewed SHA under the trusted namespace. Mitigation: the job `if` requires `workflow_run.head_repository.full_name == github.repository`; a pre-login `git rev-list --first-parent refs/remotes/origin/main | grep -Fx "$SHA"` (with a `fetch-depth: 0` checkout) fails the job for any SHA that was never the tip of `main`; and docs require a tag ruleset restricting tag creation to maintainers.
- The short name `origin/main` can be shadowed by a pushed tag `refs/tags/origin/main`, which the `fetch-depth: 0` checkout fetches and git resolves before remotes. Mitigation: only the fully qualified `refs/remotes/origin/main` is used, and the required tag ruleset restricts tag creation to maintainers.
- An intermediate commit from inside a merged PR (for example a backdoor commit reverted later in the same PR) is in `main`'s history but never ran `main` CI. Mitigation: first-parent membership rejects it. Rebase merging is disabled in docs as a required setting, because rebased PR commits would otherwise all be first-parent.
- A malicious first-parent commit that was once `main`'s tip can be republished later via a tag-triggered `CI`. Mitigation: such a commit already passed review and `main` CI when it was the tip, and the tag ruleset blocks non-maintainer tags. Accepted.
- A repository- or org-level Actions secret would be readable by PR-mutated YAML. Mitigation: invariant, with no repo/org Actions secrets; every secret lives in a `main`-only Environment (today only `ghcr-publish`).
- A comment review that lands while the approval run is still in progress produces a red `integration-gate` that may be the latest check on that SHA. Mitigation: re-run that gate job after the approval run finishes (the lookup then passes) or re-approve. No concurrency group is used, because a queued approval run could be cancelled by a later event.
- Integration results go stale when commits land after approval. Mitigation: the new head has no `integration-gate`, so merge is blocked until re-approval. Branch protection **requires** "Dismiss stale pull request approvals when new commits are pushed" and "Require approval of the most recent reviewable push", so the old approval is also gone.
- A same-repo PR can edit `integration.yml` or `ci.yml` so that its own gate passes, because PR YAML runs from the merge ref. Mitigation: **required** Code Owners review on `.github/**` means an owner must approve the edit. The token is `contents: read`/`actions: read` with no secrets. Publish loads only default-branch YAML and runs only after `main` CI.
- The `ci.yml` → `integration.yml` call fails at startup if the caller job does not grant what the nested `integration-gate` requests. Mitigation: the caller job sets `permissions: { contents: read, actions: read }`. It passes no `secrets:`.
- A nested reusable-workflow check could collide with the required context name and let a PR's skipped `integration` job satisfy it. Mitigation: nested checks are prefixed with the caller job name (`integration / integration-gate`), which does not equal `integration-gate`. The test plan verifies no bare `integration-gate` check appears on an unapproved PR head.
- Any approving review, even one that does not count toward protection, can start integration runs. Mitigation: this is compute cost only. It runs the same code with the same read-only token and no secrets as `pull_request`, and `pull_request_target` and Environments are deliberately not used.
- Gateway apt/onnxruntime install can drift from developer laptops. Mitigation: onnxruntime is pinned to v1.20.1 in the workflow, the build is Release, and `CMAKE_PREFIX_PATH` is set once rather than vendoring gRPC.
- The Python `libs/vad_sdk` Silero tests fail without `models/silero_vad.onnx` (no skip). Mitigation: both Python jobs copy `faster_whisper`'s `silero_vad_v6.onnx` into that path, fail-loud. Gateway ctest may still skip Silero.
- Live Ollama / macOS `say` tests have no in-tree `skipif`. Mitigation: fixed `--deselect` lists split per job (2 unit, 5 integration). New live-infra tests must be added to the list for the job that selects them.
- Plain `pip install -r requirements.txt` would pull CUDA torch. Mitigation: install CPU `torch==2.12.1` first in both Python jobs.
- Python and Docker jobs are slow. Mitigation: explicit `timeout-minutes` (60 for `python-unit`, 60 for `python-integration`, 90 for docker-build and publish). The torch install is now paid twice per approved PR, which is accepted for the simpler separation.
- `Dockerfile.adminui` uses pnpm without a committed lockfile. Mitigation: accept for verify-build only, and do not touch the Dockerfile unless the build fails.
- Publish rebuilds the Python image after `docker-build` already built it. Mitigation: accept the duplicate minutes for the no-artifact handoff.
- A missing Environment, wrong branch restriction, or bad `GHCR_*` fails publish loudly, which is intended. `CI / gate` still goes green on PRs.
- The repo `GITHUB_TOKEN` can still gain `packages: write` via PR-mutated YAML. Mitigation: publish goes only under a bot-owned `GHCR_IMAGE` that token cannot write, and operators pin SHA tags.

## Test plan

- **Classification tripwire (local, before PR):** with `POSTGRES_DSN=postgresql://x@127.0.0.1:1/x REDIS_URL=redis://127.0.0.1:1/0`, run `pytest -m "not integration"` plus the 2 unit deselects: it must be fully green. What would make it fail: any unmarked module from the list of 29. Temporarily remove `pytestmark` from one module (for example `test_tenants.py`) and confirm that the command goes red. That proves the tripwire can trip.
- **Selection sanity:** `pytest -m integration --co -q` collects tests only from the 29 listed modules. `pytest --co -q` totals equal unit + integration, so nothing falls through both selections.
- **PR dry-run (unit):** open a PR. `CI / python-unit` runs with no service containers, and `CI / integration` shows Skipped. `CI / gate` succeeds. No bare `integration-gate` check exists on the head SHA, and branch protection shows `integration-gate` as Expected.
- **Approval path:** a "Comment" review produces a red `integration-gate` ("none has succeeded yet") and a skipped `python-integration`. An "Approve" review runs `python-integration` against service containers (the logs show the CI DSN, `ON_ERROR_STOP` schema apply, CPU torch, the 5 deselects), and `integration-gate` goes green and satisfies protection. A further "Comment" review keeps `integration-gate` green through the prior-success lookup. On a second PR with the same head SHA but a base other than `main`, an approval makes `integration-gate` fail on the base check. A comment review on the first PR does not count that run.
- **Stale approval (required settings):** with the three required review settings enabled, approve a PR and let `integration-gate` go green, then push a new commit. Confirm that the approval shows as **dismissed**, that the new head SHA has **no** `integration-gate` check (Expected), and that merge is blocked. Re-approve and confirm that integration runs on the new head and `integration-gate` goes green. Confirm that a `.github/**` change cannot merge without a Code Owner approval.
- **Failure aggregation:** force-fail one unit test and confirm that `CI / python-unit` and `CI / gate` fail. Force-fail one integration test and confirm that `python-integration` and `integration-gate` fail on approval. Re-run after the fix and confirm green (AC 13).
- **Main + publish:** merge to `main`. `CI` runs `python-unit` **and** `integration / python-integration`, and `gate` requires both. `publish.yml` fires only after that `CI` success and pushes `$GHCR_IMAGE:<sha>` with the Environment token. Force-fail an integration test on a `main` push (throwaway branch protection off, or a revert-ready commit) and confirm that `CI / gate` fails and publish does not run. The existing GHCR checks still hold: a PR elevating `packages: write` cannot overwrite `$GHCR_IMAGE`, and a missing Environment or token fails loudly.
- **Publish provenance:** the job log shows the first-parent check on `refs/remotes/origin/main` passing before the GHCR login, for a SHA well behind the tip as well. That confirms the no-`-q` pipe does not SIGPIPE under `pipefail` on a long history. What would make it fail, run in a scratch clone with `set -eo pipefail`, confirming a non-zero exit (and in CI that no login or push step runs) for each case:
  - (a) a SHA reachable only from a side branch or tag;
  - (b) a commit from **inside a merged PR's history**, which is an ancestor of `main` but not first-parent (use a merge-commit merge of a 2-commit PR and take the first PR commit);
  - (c) a local tag `origin/main` pointing at an unrelated commit, which must not change the result because the check uses `refs/remotes/origin/main`.

  With the tag ruleset on, confirm that a non-maintainer cannot push tags named `main` or `origin/main`. Confirm that the repo and org have no Actions secrets outside Environments (Settings → Secrets → Actions shows only Environment secrets).
