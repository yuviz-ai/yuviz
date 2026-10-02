#!/usr/bin/env bash
# verify_setup.sh — checks the start_web_test.sh stack is healthy, not just listening.
#
# Usage: ./scripts/verify_setup.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PASS="✓"
FAIL="✗"
ok=true

check_port() {
  local name="$1" port="$2"
  if nc -z localhost "$port" 2>/dev/null; then
    echo "  $PASS $name (:$port) — port open"
  else
    echo "  $FAIL $name (:$port) — port closed"
    ok=false
  fi
}

check_http_health() {
  local name="$1" url="$2"
  local body
  body=$(curl -s -f "$url" 2>/dev/null)
  if [ -n "$body" ]; then
    echo "  $PASS $name — $url responded: $body"
  else
    echo "  $FAIL $name — $url not responding"
    ok=false
  fi
}

echo "=== Data layer ==="
check_port "PostgreSQL" 5432
check_port "Redis" 6379

echo ""
echo "=== HTTP services ==="
check_http_health "Config Service" "http://localhost:8000/health"
check_http_health "Knowledge Service" "http://localhost:8100/health"

echo ""
echo "=== Conversation Service (gRPC health check) ==="
if "$REPO/venv/bin/python3" - <<'PYEOF'
import sys
import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

try:
    channel = grpc.insecure_channel("localhost:50051")
    stub = health_pb2_grpc.HealthStub(channel)
    resp = stub.Check(health_pb2.HealthCheckRequest(service="voiceai.v1.ConversationService"), timeout=3)
    if resp.status == health_pb2.HealthCheckResponse.SERVING:
        print("  ✓ Conversation Service (:50051) — SERVING")
        sys.exit(0)
    print(f"  ✗ Conversation Service (:50051) — status={resp.status}")
    sys.exit(1)
except grpc.RpcError as e:
    print(f"  ✗ Conversation Service (:50051) — {e.code()}: {e.details()}")
    sys.exit(1)
PYEOF
then :; else ok=false; fi

echo ""
echo "=== Other ==="
check_port "webcall bridge" 8300
check_port "admin-ui" 3000

echo ""
if $ok; then
  echo "All checks passed."
  exit 0
else
  echo "Some checks failed — see docs/setup.md for the expected startup order."
  exit 1
fi
