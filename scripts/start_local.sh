#!/usr/bin/env bash
# start_local.sh — Starts the full native macOS Voice AI stack.
# Run each block in a SEPARATE terminal tab, in order (mysql -> kamailio ->
# freeswitch is a hard dependency chain).
#
# Prerequisites (already installed):
#   MySQL, Kamailio, FreeSWITCH, Redis, PostgreSQL, Ollama, faster-whisper
#
# Usage: source this file to get helper functions, or copy individual blocks.

set -euo pipefail
# This file is sourced (zsh or bash), so $0 is the shell, not this script.
if [ -n "${BASH_SOURCE:-}" ]; then _start_local_self="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then eval '_start_local_self=${(%):-%x}'
else _start_local_self="$0"; fi
REPO="$(cd "$(dirname "$_start_local_self")/.." && pwd)"

# ── Settings: everything comes from $REPO/.env (template: .env.example) ──────
. "$REPO/scripts/lib/env.sh"
_env_init
_load_env

# ── Block 1: MySQL — Kamailio's dispatcher/routing tables live here ─────────
start_mysql() {
  brew services start mysql 2>/dev/null || true
  echo "✓ MySQL running (kamailio database)"
}

# ── Block 2: Kamailio — SIP proxy, must be up before FreeSWITCH registers ───
start_kamailio() {
  # -Y: user-owned runtime dir so this runs without sudo.
  mkdir -p "$HOME/.yuviz/kamailio/run"
  kamailio -f /usr/local/etc/kamailio/kamailio.cfg -D -E -Y "$HOME/.yuviz/kamailio/run"
}

# ── Block 3: Data layer (Postgres/Redis; run once, may already be running) ──
start_data() {
  brew services start postgresql@14 2>/dev/null || true
  brew services start redis         2>/dev/null || true

  # psql exits 2 when it can't connect (db missing) and 3 when a schema guard
  # trips under ON_ERROR_STOP; the latter must stop the launcher.
  local schema_rc=0
  psql voiceai -v ON_ERROR_STOP=1 -f "$REPO/database/schema.sql" || schema_rc=$?
  if [ "$schema_rc" -eq 2 ]; then
    echo "voiceai db missing — run: psql postgres -c 'CREATE DATABASE voiceai;'" >&2
    # return 0: a nonzero return under `set -e` in a sourced file closes the shell.
    return 0
  elif [ "$schema_rc" -ne 0 ]; then
    echo "schema.sql failed to apply — see the error above; PostgreSQL/Redis were NOT started for use" >&2
    return "$schema_rc"
  fi

  psql voiceai -f "$REPO/database/knowledge_schema.sql" 2>/dev/null || true
  psql voiceai -f "$REPO/database/telephony_schema.sql" 2>/dev/null || true
  # RLS is inert here: every start_* block uses the superuser POSTGRES_DSN.
  _require YUVIZ_APP_PASSWORD || return 0
  psql voiceai -v yuviz_app_password="$YUVIZ_APP_PASSWORD" -f "$REPO/database/rls.sql" 2>/dev/null || true
  echo "✓ PostgreSQL + Redis running"
}

# ── Block 4: Ollama — local LLM + embedding provider ─────────────────────────
start_ollama() {
  ollama serve
}

# ── Block 5: Config Service (REST API, port 8000) ────────────────────────────
start_config_service() {
  # TELEPHONY_PUBLIC_BASE_URL is optional here: without it, new Vobiz numbers
  # are saved with a "not synced" warning (number_sync.py).
  _require SECRET_ENCRYPTION_KEY JWT_SECRET || return 0
  cd "$REPO"
  python3 -m uvicorn services.config.app:app --host 0.0.0.0 --port 8000
}

# ── Block 6: Knowledge Service (REST API, port 8100) ──────────────────────────
start_knowledge_service() {
  _require JWT_SECRET || return 0
  cd "$REPO"
  python3 -m uvicorn services.knowledge.app:app --host 0.0.0.0 --port 8100
}

# ── Block 7: Knowledge ingestion worker (background job-queue poller) ────────
start_knowledge_worker() {
  cd "$REPO"
  python3 -m services.knowledge --log-level INFO
}

# ── Block 8: Campaigns Service (REST API, port 8400) — outbound calling ──────
# The pacing worker runs in-process; no separate block.
start_campaigns_service() {
  _require JWT_SECRET FREESWITCH_ESL_PASSWORD || return 0
  _warn_env_drift SIP_PROXY_HOST
  cd "$REPO"
  python3 -m uvicorn services.campaigns.app:app --host 0.0.0.0 --port 8400
}

# ── Block 8b: Tool Execution Service (REST API, port 8600) — custom API chains ─
start_toolexec_service() {
  # Tenant credential refs resolve under TOOLEXEC_TENANT_SECRET_ROOT, not the platform mount.
  _require JWT_SECRET TOOLEXEC_TENANT_SECRET_ROOT TOOLEXEC_ARGS_HMAC_KEY_REF || return 0
  cd "$REPO"
  mkdir -p "$TOOLEXEC_TENANT_SECRET_ROOT"
  python3 -m services.toolexec
}

# ── Block 8c: Telephony Service (REST API, port 8750) — Vobiz/Cloudonix webhooks + outbound ─
start_telephony_service() {
  # TELEPHONY_PUBLIC_BASE_URL: your public tunnel URL — see docs/telephony.md.
  _require JWT_SECRET SECRET_ENCRYPTION_KEY TELEPHONY_PUBLIC_BASE_URL || return 0
  cd "$REPO"
  python3 -m services.telephony
}

# ── Block 9/10: Python ConversationService instances (ports 50051/50052) ─────
_conv_env() {
  _require CONFIG_SERVICE_EMAIL CONFIG_SERVICE_PASSWORD SECRET_ENCRYPTION_KEY
}
start_conv1() {
  _conv_env || return 0
  cd "$REPO"
  python3 -m services.conversation --port 50051 --mode pipeline --log-level INFO
}
start_conv2() {
  _conv_env || return 0
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
  _require FREESWITCH_ESL_PASSWORD || return 0
  _warn_env_drift SIP_PROXY_HOST
  cd "$REPO"
  ./build/gateway/voice_ai_gateway config/gateway.yaml
}

# ── Block 13: FreeSWITCH (registers with Kamailio from Block 2) ──────────────
start_freeswitch() {
  # Homebrew FreeSWITCH, configured by scripts/freeswitch/setup_macos.sh.
  # -scripts points at the repo so start_voice_ai.lua is never a stale copy.
  local fs_home="${FS_HOME:-$HOME/.yuviz/freeswitch}"
  "$(brew --prefix freeswitch)/bin/freeswitch" -nonat \
    -conf "$fs_home/conf" -log "$fs_home/log" -db "$fs_home/db" -run "$fs_home/run" \
    -scripts "$REPO/scripts/freeswitch"
}

# ── Block 14: Admin UI (Next.js, port 3000) ───────────────────────────────────
start_admin_ui() {
  cd "$REPO/admin-ui"
  npm run dev
}

# ── Verify: check all services are healthy ───────────────────────────────────
verify() {
  echo "=== Port check ==="
  for port in 3306 5060 5080 5432 6379 11434 8000 8100 8400 8600 8750 50051 50052 10000 8080 3000; do
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
  :8750   Telephony Service — REST API (Vobiz/Cloudonix webhooks + outbound)
  :50051  ConvSvc-1  — gRPC ConversationService
  :50052  ConvSvc-2  — gRPC ConversationService
  :10000  Envoy      — gRPC load balancer (upstream -> 50051, 50052)
  :9901   Envoy admin — http://localhost:9901
  :8080   C++ Gateway — WebSocket (FreeSWITCH mod_audio_fork -> here)
  :9090   Gateway metrics — http://localhost:9090/metrics
  :3000   Admin UI   — Next.js
EOF
}

echo "start_local.sh loaded. Functions: start_mysql, start_kamailio, start_data, start_ollama, start_config_service, start_knowledge_service, start_knowledge_worker, start_campaigns_service, start_toolexec_service, start_telephony_service, start_conv1, start_conv2, start_envoy, start_gateway, start_freeswitch, start_admin_ui, verify, portmap"
