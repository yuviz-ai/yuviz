#!/usr/bin/env bash
# Realigns the whole local stack with the machine's current LAN IP after a
# network change (new Wi-Fi, switching to a phone hotspot, etc).
#
# Updates Kamailio config, the MySQL subscriber auth domain, FreeSWITCH's
# local_ip_v4, and .env's SIP_PROXY_HOST. Idempotent.
#
# Run as your normal user, NOT via sudo: only the Kamailio steps escalate, and
# FreeSWITCH must keep running as you or its runtime files get root-owned.
#
# Usage: scripts/update_kamailio_ip.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TPL_DIR="$REPO_ROOT/scripts/kamailio"
KAMAILIO_ETC="/usr/local/etc/kamailio"
KAMAILIO_CFG="$KAMAILIO_ETC/kamailio.cfg"
# Homebrew FreeSWITCH when present, else the source-install prefix.
FS_PREFIX="$(brew --prefix freeswitch 2>/dev/null || echo /usr/local/freeswitch)"
FS_CLI="${FS_CLI:-$FS_PREFIX/bin/fs_cli}"
FS_BIN="${FS_BIN:-$FS_PREFIX/bin/freeswitch}"
FS_HOME="${FS_HOME:-$HOME/.yuviz/freeswitch}"
# Read .env as literal KEY=value, never sourced. A value already in the shell wins.
_dotenv() { [[ -f "$REPO_ROOT/.env" ]] && grep "^$1=" "$REPO_ROOT/.env" | cut -d= -f2- || true; }
FREESWITCH_ESL_PORT="${FREESWITCH_ESL_PORT:-$(_dotenv FREESWITCH_ESL_PORT)}"
FREESWITCH_ESL_PASSWORD="${FREESWITCH_ESL_PASSWORD:-$(_dotenv FREESWITCH_ESL_PASSWORD)}"
KAMAILIO_DB_URL="${KAMAILIO_DB_URL:-$(_dotenv KAMAILIO_DB_URL)}"
FS_ESL_PORT="${FREESWITCH_ESL_PORT:-8022}"
FS_ESL_PASSWORD="${FREESWITCH_ESL_PASSWORD:?set FREESWITCH_ESL_PASSWORD in .env}"
KAMAILIO_DB_URL="${KAMAILIO_DB_URL:?set KAMAILIO_DB_URL in .env}"

# Escapes a value for the replacement side of sed "s|...|...|".
sed_escape() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }

# UDP connect() sends nothing; it just picks the outbound interface's IP.
detect_lan_ip() {
  python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
finally:
    s.close()
"
}

fs_cli() {
  "$FS_CLI" -H 127.0.0.1 -P "$FS_ESL_PORT" -p "$FS_ESL_PASSWORD" -x "$1" 2>/dev/null
}

# SIP_IP overrides detection; use 127.0.0.1 for a local softphone (VPN
# interfaces can drop traffic to their own address).
if [[ -n "${SIP_IP:-}" ]]; then
  LAN_IP="$SIP_IP"
  echo "Using SIP_IP: $LAN_IP"
else
  LAN_IP="$(detect_lan_ip)"
  if [[ -z "$LAN_IP" || "$LAN_IP" == "127.0.0.1" ]]; then
    echo "ERROR: could not detect a real LAN IP (got '$LAN_IP') — is Wi-Fi/Ethernet connected?" >&2
    exit 1
  fi
  echo "Detected LAN IP: $LAN_IP"
fi
echo ""

# Gateway transfers and Campaigns outbound dial Kamailio at SIP_PROXY_HOST.
REPO="$REPO_ROOT"
# shellcheck source=lib/env.sh
source "$REPO_ROOT/scripts/lib/env.sh"
if [[ ! -f "$REPO_ROOT/.env" ]]; then
  echo "  WARNING: $REPO_ROOT/.env not found — SIP_PROXY_HOST NOT written. Source" >&2
  echo "  scripts/start_local.sh once (it creates .env), then rerun this script." >&2
elif [[ "$(_env_get SIP_PROXY_HOST || true)" == "$LAN_IP" ]]; then
  echo "  .env: SIP_PROXY_HOST already $LAN_IP"
elif _env_put SIP_PROXY_HOST "$LAN_IP" && written="$(_env_get SIP_PROXY_HOST)" && [[ "$written" == "$LAN_IP" ]]; then
  echo "  .env: SIP_PROXY_HOST=$written (restart the Gateway and Campaigns from a new tab)"
else
  echo "ERROR: could not write SIP_PROXY_HOST=$LAN_IP to $REPO_ROOT/.env" >&2
  exit 1
fi
if [[ -f "$REPO_ROOT/.env" ]]; then
  _warn_env_drift SIP_PROXY_HOST
fi
echo ""

# ── Step 1: regenerate Kamailio config from templates ───────────────────────
echo "=== Step 1/3: Kamailio config ==="

kamailio_changed=0
for name in kamailio.cfg dispatcher.list; do
  tpl="$TPL_DIR/$name.tpl"
  target="$KAMAILIO_ETC/$name"

  if [[ ! -f "$tpl" ]]; then
    echo "ERROR: template not found: $tpl" >&2
    exit 1
  fi

  # Temp file, not $(...): command substitution strips trailing newlines.
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT
  sed -e "s/__LAN_IP__/$LAN_IP/g" -e "s|__KAMAILIO_DB_URL__|$(sed_escape "$KAMAILIO_DB_URL")|g" "$tpl" > "$tmp"

  if sudo diff -q "$tmp" "$target" > /dev/null 2>&1; then
    echo "  $name: already up to date (IP unchanged)"
    rm -f "$tmp"
    trap - EXIT
    continue
  fi

  # Timestamped backup so hand-made backups are never clobbered.
  if sudo test -f "$target"; then
    backup="$target.bak.$(date +%Y%m%d%H%M%S)"
    sudo cp "$target" "$backup"
    echo "  $name: backed up existing file to $(basename "$backup")"
  fi

  sudo mv "$tmp" "$target"
  trap - EXIT
  echo "  $name: regenerated with IP=$LAN_IP"
  kamailio_changed=1
done

if [[ "$kamailio_changed" == "1" ]] && pgrep -x kamailio > /dev/null 2>&1; then
  echo "  restarting kamailio to pick up the new config..."
  sudo pkill -x kamailio || true
  for _ in $(seq 1 20); do
    pgrep -x kamailio > /dev/null 2>&1 || break
    sleep 0.5
  done
  sudo kamailio -DD -E -f "$KAMAILIO_CFG"
  sleep 1
  if pgrep -x kamailio > /dev/null 2>&1; then
    echo "  kamailio restarted"
  else
    echo "  WARNING: kamailio did not come back up — check its config with:" >&2
    echo "    sudo kamailio -c -f $KAMAILIO_CFG" >&2
  fi
elif [[ "$kamailio_changed" == "1" ]]; then
  echo "  kamailio isn't currently running — nothing to restart"
fi
echo ""

# ── Step 2: fix up the MySQL subscriber table's auth domain ─────────────────
echo "=== Step 2/3: MySQL subscriber auth domain ==="

if ! command -v mysql > /dev/null 2>&1; then
  echo "  mysql client not found on PATH — skipping"
elif ! mysql -u root kamailio -e "SELECT 1" > /dev/null 2>&1; then
  echo "  can't reach the kamailio MySQL database — skipping"
else
  stale_rows="$(mysql -u root kamailio -N -e \
    "SELECT username, password FROM subscriber WHERE domain != '$LAN_IP';")"

  if [[ -z "$stale_rows" ]]; then
    echo "  all subscriber rows already use domain=$LAN_IP"
  else
    sql_file="$(mktemp)"
    trap 'rm -f "$sql_file"' EXIT
    # ha1/ha1b digests embed the domain, so update them together. Python, not
    # MySQL MD5(), which MySQL 9.x removed.
    python3 -c "
import sys

new_domain = '$LAN_IP'
rows = '''$stale_rows'''.strip().splitlines()
for row in rows:
    username, password = row.split('\t')
    import hashlib
    ha1 = hashlib.md5(f'{username}:{new_domain}:{password}'.encode()).hexdigest()
    ha1b = hashlib.md5(f'{username}@{new_domain}:{new_domain}:{password}'.encode()).hexdigest()
    print(f\"UPDATE subscriber SET domain='{new_domain}', ha1='{ha1}', ha1b='{ha1b}' WHERE username='{username}';\")
" > "$sql_file"
    mysql -u root kamailio < "$sql_file"
    rm -f "$sql_file"
    trap - EXIT
    n="$(echo "$stale_rows" | wc -l | tr -d ' ')"
    echo "  updated $n subscriber row(s) to domain=$LAN_IP"
  fi
fi
echo ""

# ── Step 3: restart FreeSWITCH if it's still bound to a stale IP ────────────
echo "=== Step 3/3: FreeSWITCH local_ip_v4 ==="

if [[ ! -x "$FS_CLI" ]]; then
  echo "  fs_cli not found — skipping"
elif ! fs_running_ip="$(fs_cli 'eval ${local_ip_v4}')" || [[ -z "$fs_running_ip" ]]; then
  echo "  FreeSWITCH isn't running (or ESL isn't reachable) — nothing to restart"
elif [[ "$fs_running_ip" == "$LAN_IP" ]]; then
  echo "  FreeSWITCH already bound to $LAN_IP"
else
  echo "  FreeSWITCH is still bound to stale IP $fs_running_ip — restarting"
  # local_ip_v4 is resolved only at process startup; reloadxml won't update it.
  fs_cli shutdown || true
  for _ in $(seq 1 20); do
    pgrep -x freeswitch > /dev/null 2>&1 || break
    sleep 0.5
  done
  if [[ -d "$FS_HOME/conf" ]]; then
    # Same flags as start_freeswitch in scripts/start_local.sh.
    "$FS_BIN" -nc -nonat -conf "$FS_HOME/conf" -log "$FS_HOME/log" -db "$FS_HOME/db" \
      -run "$FS_HOME/run" -scripts "$REPO_ROOT/scripts/freeswitch"
  else
    ( cd /usr/local/freeswitch && "$FS_BIN" -nc )
  fi
  sleep 2
  new_ip="$(fs_cli 'eval ${local_ip_v4}' || true)"
  if [[ "$new_ip" == "$LAN_IP" ]]; then
    echo "  FreeSWITCH restarted, now bound to $LAN_IP"
  else
    echo "  WARNING: FreeSWITCH restarted but reports local_ip_v4=$new_ip (expected $LAN_IP)" >&2
  fi
fi

echo ""
echo "Done."
