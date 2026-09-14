#!/usr/bin/env bash
# start_local.sh — Starts the full native macOS Voice AI stack.
# Run each block in a SEPARATE terminal tab, in order (mysql -> kamailio ->
# freeswitch is a hard dependency chain: kamailio's dispatcher/routing
# tables live in MySQL, and FreeSWITCH registers with Kamailio as its
# upstream SIP proxy).
#
# Prerequisites (already installed):
#   MySQL, Kamailio, FreeSWITCH, Redis, PostgreSQL, Ollama, faster-whisper
#
# Usage: source this file to get helper functions, or copy individual blocks.

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# ── Block 1: MySQL — Kamailio's dispatcher/routing tables live here ─────────
start_mysql() {
  brew services start mysql 2>/dev/null || true
  echo "✓ MySQL running (kamailio database)"
}

# ── Block 2: Kamailio — SIP proxy, must be up before FreeSWITCH registers ───
start_kamailio() {
  kamailio -f /usr/local/etc/kamailio/kamailio.cfg -D -E
}

# ── Block 3: Data layer (Postgres/Redis; run once, may already be running) ──
start_data() {
  brew services start postgresql@14 2>/dev/null || true
  brew services start redis         2>/dev/null || true

  # ON_ERROR_STOP=1 (lesson 13) turns a tripped guard — e.g. schema.sql's
  # case-insensitive email-collision check — into psql exit status 3, which
  # must reach the operator and stop this launcher: swallowing it (the old
  # `2>/dev/null || echo "...applied..."`) would have printed a false
  # success and started every service against a half-applied schema, with
  # no user_invites table and no invite-onboarding at all. The one case
  # that old fallback genuinely needed to catch is exit status 2 — psql's
  # own "could not connect", which is what happens on a fresh checkout
  # before `createdb voiceai` has ever been run — checked here explicitly,
  # verified empirically (`psql <missing db>` -> 2, a tripped `RAISE
  # EXCEPTION` guard under ON_ERROR_STOP=1 -> 3, not 2).
  local schema_rc=0
  psql voiceai -v ON_ERROR_STOP=1 -f "$REPO/database/schema.sql" || schema_rc=$?
  if [ "$schema_rc" -eq 2 ]; then
    echo "voiceai db missing — run: psql postgres -c 'CREATE DATABASE voiceai;'" >&2
    # return 0, not 1: this file is source-d into the operator's shell (docs/setup.md:158)
    # under `set -euo pipefail`, where a nonzero return from a sourced function
    # terminates the interactive shell. The hint above is the whole point of
    # this branch, so it must survive to be read.
    return 0
  elif [ "$schema_rc" -ne 0 ]; then
    echo "schema.sql failed to apply — see the error above; PostgreSQL/Redis were NOT started for use" >&2
    return "$schema_rc"
  fi

  psql voiceai -f "$REPO/database/knowledge_schema.sql" 2>/dev/null || true
  echo "✓ PostgreSQL + Redis running"
}

# ── Block 4: Ollama — local LLM + embedding provider ─────────────────────────
start_ollama() {
  ollama serve
}

# ── Block 5: Config Service (REST API, port 8000) ────────────────────────────
start_config_service() {
  export POSTGRES_DSN="postgresql://satish@localhost:5432/voiceai"
  # Decrypts/encrypts provider_configs.api_key_ref's enc: scheme (Vault's
  # replacement — see libs/config_sdk/secrets.py). Same "fail loudly, never
  # a hardcoded fallback" posture as CONFIG_SERVICE_PASSWORD below: generate
  # one with `./venv/bin/python3 -c "from libs.config_sdk.secrets import
  # generate_key; print(generate_key())"` and export the SAME value here
  # and in _conv_env() — a mismatch between the two just means whichever
  # service didn't encrypt a given secret can't decrypt it either.
  export SECRET_ENCRYPTION_KEY="${SECRET_ENCRYPTION_KEY:?set this in your shell — see docs/setup.md, never commit the real value}"
  # Signs the login JWTs; same value every service below must export.
  export JWT_SECRET="${JWT_SECRET:?set this in your shell — see docs/setup.md, never commit the real value}"
  cd "$REPO"
  python3 -m uvicorn services.config.app:app --host 0.0.0.0 --port 8000
}

# ── Block 6: Knowledge Service (REST API, port 8100) ──────────────────────────
start_knowledge_service() {
  export POSTGRES_DSN="postgresql://satish@localhost:5432/voiceai"
  export REDIS_URL="redis://localhost:6379/0"
  export KNOWLEDGE_STORAGE_ROOT="$REPO/data/knowledge_documents"
  export JWT_SECRET="${JWT_SECRET:?set this in your shell — see docs/setup.md, never commit the real value}"
  cd "$REPO"
  python3 -m uvicorn services.knowledge.app:app --host 0.0.0.0 --port 8100
}

# ── Block 7: Knowledge ingestion worker (background job-queue poller) ────────
start_knowledge_worker() {
  export POSTGRES_DSN="postgresql://satish@localhost:5432/voiceai"
  export KNOWLEDGE_STORAGE_ROOT="$REPO/data/knowledge_documents"
  cd "$REPO"
  python3 -m services.knowledge --log-level INFO
}

# ── Block 8: Campaigns Service (REST API, port 8400) — outbound calling ──────
# worker.py's pacing loop runs inside this same process (see its own
# docstring), not a separate process like the Knowledge worker — no extra
# block needed for it.
start_campaigns_service() {
  export POSTGRES_DSN="postgresql://satish@localhost:5432/voiceai"
  export JWT_SECRET="${JWT_SECRET:?set this in your shell — see docs/setup.md, never commit the real value}"
  cd "$REPO"
  python3 -m uvicorn services.campaigns.app:app --host 0.0.0.0 --port 8400
}

# ── Block 8b: Tool Execution Service (REST API, port 8600) — custom API chains ─
start_toolexec_service() {
  export POSTGRES_DSN="postgresql://satish@localhost:5432/voiceai"
  export JWT_SECRET="${JWT_SECRET:?set this in your shell — see docs/setup.md, never commit the real value}"
  # Tenant-namespaced credential refs resolve under this root, NOT the
  # platform k8s secret mount — see services/toolexec/auth_schemes.py.
  export TOOLEXEC_TENANT_SECRET_ROOT="${TOOLEXEC_TENANT_SECRET_ROOT:-$REPO/data/toolexec_tenant_secrets}"
  mkdir -p "$TOOLEXEC_TENANT_SECRET_ROOT"
  cd "$REPO"
  python3 -m services.toolexec
}

# ── Block 9/10: Python ConversationService instances (ports 50051/50052) ─────
_conv_env() {
  export POSTGRES_DSN="postgresql://satish@localhost:5432/voiceai"
  export REDIS_URL="redis://localhost:6379/0"
  export CONFIG_SERVICE_URL="http://localhost:8000"
  export CONFIG_SERVICE_EMAIL="conversation-service@internal.yuviz.ai"
  export CONFIG_SERVICE_PASSWORD="${CONFIG_SERVICE_PASSWORD:?set this in your shell — see docs/setup.md §4, never commit the real value}"
  export KNOWLEDGE_SERVICE_URL="http://localhost:8100"
  # Must be the SAME value start_config_service() exports — see its own
  # comment on this variable.
  export SECRET_ENCRYPTION_KEY="${SECRET_ENCRYPTION_KEY:?set this in your shell — see docs/setup.md, never commit the real value}"
}
start_conv1() {
  _conv_env
  cd "$REPO"
  python3 -m services.conversation --port 50051 --mode pipeline --log-level INFO
}
start_conv2() {
  _conv_env
  cd "$REPO"
  python3 -m services.conversation --port 50052 --mode pipeline --log-level INFO
}

# ── Block 11: Envoy gRPC proxy ────────────────────────────────────────────────
start_envoy() {
  # Install func-e if missing: brew install func-e
  func-e run -c "$REPO/config/envoy.yaml"
}

# ── Block 12: C++ Gateway ──────────────────────────────────────────────────────
start_gateway() {
  cd "$REPO"
  ./build/gateway/voice_ai_gateway config/gateway.yaml
}

# ── Block 13: FreeSWITCH (registers with Kamailio from Block 2) ──────────────
start_freeswitch() {
  cd "$REPO"
  ./freeswitch
}

# ── Block 14: Admin UI (Next.js, port 3000) ───────────────────────────────────
start_admin_ui() {
  cd "$REPO/admin-ui"
  npm run dev
}

# ── Verify: check all services are healthy ───────────────────────────────────
verify() {
  echo "=== Port check ==="
  for port in 3306 5060 5080 5432 6379 11434 8000 8100 8400 8600 50051 50052 10000 8080 3000; do
    nc -z localhost "$port" 2>/dev/null && echo "  :$port  OPEN" || echo "  :$port  CLOSED"
  done

  echo ""
  echo "=== Config / Knowledge / Campaigns Service health ==="
  curl -s http://localhost:8000/health || echo "  Config Service not reachable"
  echo ""
  curl -s http://localhost:8100/health || echo "  Knowledge Service not reachable"
  echo ""
  curl -sf http://localhost:8400/health || echo "  Campaigns Service not reachable"
  echo ""
  curl -s http://localhost:8600/health || echo "  Tool Execution Service not reachable"

  echo ""
  echo "=== Envoy upstream health ==="
  curl -s http://localhost:9901/clusters | grep conversation_svc | grep health || \
    echo "  Envoy admin not reachable — is Envoy running?"

  echo ""
  echo "=== Recent call records ==="
  psql voiceai -c "SELECT session_id, duration_ms, turn_count, close_reason FROM calls ORDER BY started_at DESC LIMIT 5;" 2>/dev/null || \
    echo "  (no call records yet)"
}

# ── Port map ─────────────────────────────────────────────────────────────────
portmap() {
  cat <<'EOF'
  :3306   MySQL      — Kamailio dispatcher/routing tables
  :5060   Kamailio   — SIP proxy (start before FreeSWITCH)
  :5080   FreeSWITCH — SIP UA (registers with Kamailio)
  :6379   Redis      — session state + config cache-aside
  :5432   PostgreSQL — CDR, transcripts, config, knowledge base
  :11434  Ollama     — local LLM + embedding provider
  :8000   Config Service    — REST API
  :8100   Knowledge Service — REST API (RAG)
  :8400   Campaigns Service — REST API (outbound calling)
  :8600   Tool Execution Service — REST API (custom API chains)
  :50051  ConvSvc-1  — gRPC ConversationService
  :50052  ConvSvc-2  — gRPC ConversationService
  :10000  Envoy      — gRPC load balancer (upstream -> 50051, 50052)
  :9901   Envoy admin — http://localhost:9901
  :8080   C++ Gateway — WebSocket (FreeSWITCH mod_audio_fork -> here)
  :9090   Gateway metrics — http://localhost:9090/metrics
  :3000   Admin UI   — Next.js
EOF
}

echo "start_local.sh loaded. Functions: start_mysql, start_kamailio, start_data, start_ollama, start_config_service, start_knowledge_service, start_knowledge_worker, start_campaigns_service, start_toolexec_service, start_conv1, start_conv2, start_envoy, start_gateway, start_freeswitch, start_admin_ui, verify, portmap"
