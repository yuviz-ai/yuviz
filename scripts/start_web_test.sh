#!/usr/bin/env bash
# start_web_test.sh — Starts just enough of the stack to test an agent
# in the browser (webcall + admin-ui Test Agent), with no telephony.
#
# Prerequisites (see docs/setup.md for install steps):
#   PostgreSQL, Redis, Node.js, and (only if using local models instead of
#   a cloud STT/LLM/TTS provider) Ollama.
#
# Usage: source this file to get helper functions, or copy individual
# blocks into separate terminal tabs. Run start_data, then
# scripts/seed_default_config.py once, then the rest in any order.

set -euo pipefail
# This file is sourced (zsh or bash), so $0 is the shell, not this script.
if [ -n "${BASH_SOURCE:-}" ]; then _start_web_self="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then eval '_start_web_self=${(%):-%x}'
else _start_web_self="$0"; fi
REPO="$(cd "$(dirname "$_start_web_self")/.." && pwd)"

# Settings come from $REPO/.env (template: .env.example), same as start_local.sh.
. "$REPO/scripts/lib/env.sh"
_env_init
_load_env

# ── Block 1: Data layer (Postgres/Redis; run once, may already be running) ──
start_data() {
  brew services start postgresql@14 2>/dev/null || true
  brew services start redis         2>/dev/null || true
  psql voiceai -v ON_ERROR_STOP=1 -f "$REPO/database/schema.sql"
  psql voiceai -f "$REPO/database/knowledge_schema.sql"
  psql voiceai -f "$REPO/database/telephony_schema.sql"
  echo "✓ PostgreSQL + Redis running, schema applied"
  echo "  Next: $REPO/venv/bin/python3 $REPO/scripts/seed_default_config.py"
}

# ── Block 2: Ollama — only needed if testing with local models (the
#    seed script's default) rather than a cloud STT/LLM/TTS provider ──────
start_ollama() {
  ollama serve
}

# ── Block 3: Config Service (REST API, port 8000) ────────────────────────────
start_config_service() {
  _require SECRET_ENCRYPTION_KEY JWT_SECRET || return 0
  cd "$REPO"
  ./venv/bin/python3 -m uvicorn services.config.app:app --host 0.0.0.0 --port 8000
}

# ── Block 4: Knowledge Service (REST API, port 8100) — optional, only
#    needed if testing RAG-backed agents ──────────────────────────────────
start_knowledge_service() {
  _require JWT_SECRET || return 0
  cd "$REPO"
  ./venv/bin/python3 -m uvicorn services.knowledge.app:app --host 0.0.0.0 --port 8100
}

# ── Block 5: ConversationService (gRPC, port 50051); conv2 is optional ─────
_conv_env() {
  _require CONFIG_SERVICE_EMAIL CONFIG_SERVICE_PASSWORD SECRET_ENCRYPTION_KEY
}
start_conv1() {
  _conv_env || return 0
  cd "$REPO"
  ./venv/bin/python3 -m services.conversation --port 50051 --mode pipeline --log-level INFO
}
start_conv2() {
  _conv_env || return 0
  cd "$REPO"
  ./venv/bin/python3 -m services.conversation --port 50052 --mode pipeline --log-level INFO
}

# ── Block 6: webcall bridge (browser WebSocket <-> gRPC, port 8300) ───────
start_webcall() {
  # No Envoy here, so bypass webcall's default :10000 target.
  export CONVERSATION_SVC_TARGET="localhost:50051"
  cd "$REPO"
  ./venv/bin/python3 -m services.webcall --port 8300
}

# ── Block 7: Admin UI (Next.js, port 3000) — "Test Agent" panel ───────────
start_admin_ui() {
  cd "$REPO/admin-ui"
  npm install
  npm run dev
}

# ── Verify: check the web-testing services are up ─────────────────────────
verify() {
  echo "=== Port check ==="
  for port in 5432 6379 11434 8000 8100 50051 8300 3000; do
    nc -z localhost "$port" 2>/dev/null && echo "  :$port  OPEN" || echo "  :$port  CLOSED"
  done
  echo ""
  echo "=== Config Service health ==="
  curl -s http://localhost:8000/health || echo "  Config Service not reachable"
}

echo "start_web_test.sh loaded. Functions: start_data, start_ollama, start_config_service, start_knowledge_service, start_conv1, start_conv2, start_webcall, start_admin_ui, verify"
echo "Then open http://localhost:3000, go to an agent's page, and click 'Test Agent'."
