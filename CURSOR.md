# yuviz — Cursor project context

Voice AI platform: real-time STT → LLM (tools, RAG, human transfer) → TTS.
Reachable over SIP (C++ Gateway + FreeSWITCH/Kamailio) or browser (webcall + Admin UI).

Repo: `yuviz-ai/yuviz`. Packaging name in `pyproject.toml` is `voiceai-services`.

## Architecture

```
Browser / SIP → (webcall | C++ Gateway) → gRPC → Conversation Service
Admin UI (Next.js) → REST → Config / Knowledge / Campaigns / DID services
PostgreSQL (source of truth) + Redis (cache / DID routing / config pub-sub)
```

| Area | Location |
|------|----------|
| Config API (tenants, agents, providers, auth, workflows) | `services/config/` |
| Real-time AI pipeline (STT/LLM/TTS, tools, transfers) | `services/conversation/` |
| Knowledge / RAG | `services/knowledge/` |
| Outbound campaigns | `services/campaigns/` |
| Custom API chains | `services/toolexec/` |
| DID / number purchase | `services/did/` |
| Browser media bridge | `services/webcall/` |
| Shared libraries | `libs/` (`config_sdk`, `knowledge_sdk`, `telephony_sdk`, `vad_sdk`) |
| C++ media gateway | `gateway/` |
| Admin UI | `admin-ui/` (Next.js 16 — read `admin-ui/AGENTS.md` before UI work) |
| Schemas | `database/*.sql` (idempotent; no Alembic) |
| Protos | `proto/` → generated under `services/conversation/generated/` |
| Deploy | `./deployment/sh/dev.sh` + `deployment/docker/` |

Routers are thin HTTP wrappers; business logic lives in sibling modules (`agents.py`, `workflows.py`, …), not in `routers/`.

## Multi-tenant model

- Every owned row carries `tenant_id` (UUID FK to `tenants`). Soft deletes via `deleted_at`.
- Roles: `superadmin` (platform; `tenant_id` null), `admin`, `viewer` (tenant-scoped).
- JWT in `Authorization: Bearer …` — identity from verified token only (`services/config/auth.py`, `deps.py`). Never trust client-supplied user/tenant identity.
- Tenant-scoped admins must only touch their own tenant; reject cross-tenant provider/agent assignment (existing tests cover this).
- Service accounts (`is_service_account`) authenticate Conversation/Knowledge to Config like real users.

## Auth & secrets

- Passwords: bcrypt. Tokens: HS256 JWT (`JWT_SECRET`).
- Provider API keys stored as `enc:…` / `k8s:…` / `env:…` refs — never plaintext in Postgres. Fernet via `SECRET_ENCRYPTION_KEY` (`libs/config_sdk/secrets.py`).
- Do not commit `deployment/.env`, real passwords, or encryption keys.

## Important workflows

1. **Inbound call**: DID → Redis `did:{did}` → agent → Conversation pipeline → optional transfer via Gateway ESL.
2. **Browser test**: Admin UI Test Agent → webcall → Conversation (no SIP).
3. **Agent config**: Admin UI → Config Service → Postgres + Redis cache; `config_version` bumps on write.
4. **Workflows**: graph on `agents.workflow` / draft; runtime in `services/conversation/workflow/`. Design: `docs/workflow.md`.
5. **Knowledge**: upload → ingestion worker → embeddings → retrieval tool during calls.
6. **Campaigns**: contact lists + DNC + originate worker.

## Database

- Apply in order: `schema.sql` → `knowledge_schema.sql` → `telephony_schema.sql`.
- Evolve schema by editing those idempotent SQL files (and one-off scripts under `scripts/` when needed). Do not invent a parallel migration tool.
- `calls.tenant_id` is a **slug string**, not a UUID FK — match existing call-path conventions.

## Commands

```bash
# Python (from repo root, with venv + POSTGRES_DSN/REDIS as tests expect)
pytest
# or narrower: pytest services/config/tests/test_agents.py

# C++ gateway
cmake -B build && cmake --build build
ctest --test-dir build --output-on-failure

# Admin UI
cd admin-ui && npm run lint && npm run build

# Full local stack
./deployment/sh/dev.sh
```

No repo-wide Python linter/typechecker is configured; do not invent one. Prefer `pytest` for Python behavior changes.

## Constraints Cursor must respect

- **Tenant isolation is an invariant** — scope queries; never cross-tenant reads/writes for convenience.
- Match existing style (docstring-heavy modules, soft delete, cache write-through patterns). Reuse `libs/*` and service helpers.
- Python runtime target is **3.11** (not 3.12+).
- Do not rewrite the Gateway media path, Redis DID cache design, or secret `enc:` scheme without a clear need.
- Do not claim tests/lint/build passed unless they were run.
- Prefer the smallest change that fixes the problem; no speculative abstractions.
- **Never commit or push** — stop and ask the user to review and do it themselves.
- **Never attribute Cursor/AI** as author, co-author, contributor, or collaborator in commits, PRs, or Git metadata.
