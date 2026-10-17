#!/usr/bin/env bash
# Applies SIP_IP (.env) to Kamailio, FreeSWITCH, subscriber digests and
# SIP_PROXY_HOST, and restarts what still uses the old address.
#   SIP_IP=127.0.0.1 (default) | auto (LAN IP) | <fixed IPv4>
# Usage: scripts/update_kamailio_ip.sh [--if-changed]

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TPL_DIR="$REPO/scripts/kamailio"
# shellcheck source=lib/env.sh
source "$REPO/scripts/lib/env.sh"
# shellcheck source=lib/sip.sh
source "$REPO/scripts/lib/sip.sh"
MYSQL="${MYSQL:-mysql}"

# Literal .env value, never sourced; the shell's value wins.
_dotenv() { [[ -f "$REPO/.env" ]] && grep "^$1=" "$REPO/.env" | cut -d= -f2- || true; }
SIP_IP="${SIP_IP:-$(_dotenv SIP_IP)}"
FS_ESL_PORT="${FREESWITCH_ESL_PORT:-$(_dotenv FREESWITCH_ESL_PORT)}"
FS_ESL_PORT="${FS_ESL_PORT:-8022}"
FS_ESL_PASSWORD="${FREESWITCH_ESL_PASSWORD:-$(_dotenv FREESWITCH_ESL_PASSWORD)}"
KAMAILIO_DB_URL="${KAMAILIO_DB_URL:-$(_dotenv KAMAILIO_DB_URL)}"
KAMAILIO_DB_URL="${KAMAILIO_DB_URL:?set KAMAILIO_DB_URL in .env}"

export KAMAILIO_DIR YUVIZ_LOGS
mkdir -p "$KAMAILIO_DIR/run" "$YUVIZ_LOGS"
APPLIED="$KAMAILIO_DIR/.applied_ip"
ok=1

# One run at a time.
LOCK="$KAMAILIO_DIR/.sync.lock"
find "$LOCK" -maxdepth 0 -mmin +10 -exec rmdir {} \; 2>/dev/null || true
mkdir "$LOCK" 2>/dev/null || { echo "another network sync is running"; exit 0; }
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

IP="$(_sip_target_ip "$APPLIED")" || true
if [[ -z "$IP" ]]; then
  [[ "${1:-}" == --if-changed ]] && exit 0   # offline or VPN; the next change reruns this
  echo "ERROR: SIP_IP=auto but no usable address was found — is Wi-Fi/Ethernet connected (not a VPN)?" >&2
  exit 1
fi
# One interface only; the value lands in config, SQL and vars.xml.
if [[ ! "$IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ || "$IP" == 0.0.0.0 ]]; then
  echo "ERROR: SIP_IP must be 127.0.0.1, auto or one IPv4 address (not 0.0.0.0); got '$IP'" >&2
  exit 1
fi
# Also re-run when vars.xml still has a literal "auto" (or other stale pin) from
# setup_macos.sh before it resolved SIP_IP.
_fs_local_ip_pin_matches() {
  local vars
  vars="$(_fs_vars_xml)" || return 0
  grep -qF "data=\"local_ip_v4=$1\"" "$vars"
}
if [[ "${1:-}" == --if-changed && -f "$KAMAILIO_DIR/kamailio.cfg" \
      && "$(cat "$APPLIED" 2>/dev/null)" == "$IP" ]] \
   && _fs_local_ip_pin_matches "$IP"; then
  exit 0
fi
echo "[$(date '+%F %T')] SIP_IP=${SIP_IP:-127.0.0.1} -> $IP"
# Restarted services inherit this priority.
own_nice="$(ps -o nice= -p $$ | tr -d ' ')"
[[ "${own_nice:-0}" -gt 0 ]] && echo "  WARNING: running at nice $own_nice; services restarted from here inherit it" >&2

# ── 1. SIP_PROXY_HOST ────────────────────────────────────────────────────────
if [[ ! -f "$REPO/.env" ]]; then
  echo "  WARNING: $REPO/.env not found — SIP_PROXY_HOST NOT written. Source" >&2
  echo "  scripts/start_local.sh once (it creates .env), then rerun this script." >&2
elif [[ "$(_env_get SIP_PROXY_HOST || true)" == "$IP" ]]; then
  echo "  .env: SIP_PROXY_HOST already $IP"
elif _env_put SIP_PROXY_HOST "$IP" && [[ "$(_env_get SIP_PROXY_HOST)" == "$IP" ]]; then
  echo "  .env: SIP_PROXY_HOST=$IP"
else
  echo "ERROR: could not write SIP_PROXY_HOST=$IP to $REPO/.env" >&2
  exit 1
fi
[[ -f "$REPO/.env" ]] && _warn_env_drift SIP_PROXY_HOST

# Stops pids with SIGTERM, then SIGKILL after $1 seconds.
_stop() {
  local wait_s=$1; shift
  [[ $# -gt 0 ]] || return 0
  kill -TERM "$@" 2>/dev/null || true
  for _ in $(seq 1 $((wait_s * 2))); do
    kill -0 "$@" 2>/dev/null || return 0
    sleep 0.5
  done
  kill -KILL "$@" 2>/dev/null || true
}

# Restarts go through supervisord when it runs the program, else a detached copy fights the supervised one.
_sv() { supervisorctl -c "$REPO/deployment/supervisord.conf" "$@"; }
_supervised() {
  command -v supervisorctl >/dev/null 2>&1 &&
    _sv status "$1" 2>/dev/null | grep -qE "^$1[[:space:]]+(RUNNING|STARTING|BACKOFF)"
}

# ── 2. Kamailio config ───────────────────────────────────────────────────────
sed_escape() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }
changed=0
for name in kamailio.cfg dispatcher.list; do
  tmp="$(mktemp)"
  sed -e "s/__LAN_IP__/$IP/g" \
      -e "s|__KAMAILIO_DIR__|$(sed_escape "$KAMAILIO_DIR")|g" \
      -e "s|__KAMAILIO_DB_URL__|$(sed_escape "$KAMAILIO_DB_URL")|g" "$TPL_DIR/$name.tpl" > "$tmp"
  if cmp -s "$tmp" "$KAMAILIO_DIR/$name"; then
    rm -f "$tmp"
  else
    (umask 077; mv -f "$tmp" "$KAMAILIO_DIR/$name")
    changed=1
  fi
done
read -r -a kam_pids <<< "$(_kamailio_pids | tr '\n' ' ')"
if [[ ${#kam_pids[@]} -gt 0 ]] && ! pgrep -u "$(id -u)" -f "^([^ ]*/)?kamailio -f $KAMAILIO_DIR/kamailio.cfg" >/dev/null; then
  changed=1   # still running from the legacy path
fi
if [[ "$changed" == 0 ]]; then
  echo "  kamailio: config already for $IP"
elif [[ ${#kam_pids[@]} -eq 0 ]]; then
  echo "  kamailio: config rendered for $IP (not running)"
elif ! kamailio -c -f "$KAMAILIO_DIR/kamailio.cfg" -Y "$KAMAILIO_DIR/run" >/dev/null 2>&1; then
  echo "  WARNING: new kamailio config does not check; the running one is left alone." >&2
  echo "    kamailio -c -f $KAMAILIO_DIR/kamailio.cfg -Y $KAMAILIO_DIR/run" >&2
  ok=0
elif _supervised kamailio; then
  # Its start reruns this script, which finds $LOCK held and starts Kamailio; this run does steps 3-5.
  if _sv restart kamailio >/dev/null; then
    echo "  kamailio: restarted by supervisord on $IP"
  else
    echo "  WARNING: supervisorctl restart kamailio failed; see supervisorctl status kamailio" >&2
    ok=0
  fi
else
  _stop 10 "${kam_pids[@]}"
  (cd "$REPO" && nohup bash -c '. scripts/lib/sip.sh; _kamailio_start' >>"$YUVIZ_LOGS/kamailio.log" 2>&1 &)
  echo "  kamailio: restarted on $IP (log $YUVIZ_LOGS/kamailio.log)"
fi

# ── 3. Subscriber digests ────────────────────────────────────────────────────
if ! command -v "$MYSQL" > /dev/null 2>&1; then
  echo "  mysql: client not found — skipped"
elif ! "$MYSQL" -u root kamailio -e "SELECT 1" > /dev/null 2>&1; then
  echo "  mysql: kamailio database unreachable — skipped"
else
  stale_rows="$("$MYSQL" -u root kamailio -N -e "SELECT username, password FROM subscriber WHERE domain != '$IP';")"
  if [[ -z "$stale_rows" ]]; then
    echo "  mysql: subscribers already on $IP"
  else
    # MySQL 9 has no MD5().
    STALE_ROWS="$stale_rows" NEW_DOMAIN="$IP" python3 -c "
import hashlib, os
d = os.environ['NEW_DOMAIN']
for row in os.environ['STALE_ROWS'].strip().splitlines():
    u, p = row.split('\t')
    ha1 = hashlib.md5(f'{u}:{d}:{p}'.encode()).hexdigest()
    ha1b = hashlib.md5(f'{u}@{d}:{d}:{p}'.encode()).hexdigest()
    print(f\"UPDATE subscriber SET domain='{d}', ha1='{ha1}', ha1b='{ha1b}' WHERE username='{u}';\")
" | "$MYSQL" -u root kamailio
    echo "  mysql: $(grep -c . <<< "$stale_rows") subscriber(s) moved to $IP"
  fi
fi

# ── 4. FreeSWITCH local_ip_v4 ────────────────────────────────────────────────
if ! vars="$(_fs_vars_xml)"; then
  echo "  freeswitch: not installed — skipped"
else
  pin="<X-PRE-PROCESS cmd=\"set\" data=\"local_ip_v4=$IP\"/>"
  if ! grep -qF "$pin" "$vars"; then
    sed -i.bak -e '/data="local_ip_v4=/d' "$vars" && rm -f "$vars.bak"
    sed -i.bak -e "s|^<include>|<include>\\
  $pin|" "$vars" && rm -f "$vars.bak"
    echo "  freeswitch: local_ip_v4 pinned to $IP in $vars"
  fi
  fs_cli="$(_fs_prefix)/bin/fs_cli"
  _fs() { "$fs_cli" -H 127.0.0.1 -P "$FS_ESL_PORT" -p "$FS_ESL_PASSWORD" -x "$1" 2>/dev/null || true; }
  running="$(_fs 'eval ${local_ip_v4}')"
  if [[ -z "$running" ]]; then
    echo "  freeswitch: not running"
  elif [[ "$running" == "$IP" ]]; then
    echo "  freeswitch: already on $IP"
  else
    if _supervised freeswitch; then
      _sv restart freeswitch >/dev/null || { echo "  WARNING: supervisorctl restart freeswitch failed" >&2; ok=0; }
    else
      _fs 'fsctl shutdown' >/dev/null
      _stop 20 $(pgrep -u "$(id -u)" -x freeswitch || true)
      (cd "$REPO" && _fs_start -nc >>"$YUVIZ_LOGS/freeswitch.log" 2>&1) || true
    fi
    for _ in $(seq 1 30); do [[ "$(_fs 'eval ${local_ip_v4}')" == "$IP" ]] && break; sleep 1; done
    echo "  freeswitch: restarted ($running -> $IP)"
  fi
  # A new address refuses binds briefly and sofia gives up after 3 tries.
  if [[ -n "$running" ]]; then
    for f in "$(dirname "$vars")"/sip_profiles/*.xml; do
      p="$(basename "$f" .xml)"
      [[ "$p" == *ipv6* ]] && continue
      for _ in $(seq 1 15); do
        _fs 'sofia status' | grep -qE "^ +$p[[:space:]]+profile[[:space:]]+sip:[^ ]*@$IP:" && break
        _fs "sofia profile $p start" >/dev/null
        sleep 2
      done
      if _fs 'sofia status' | grep -qE "^ +$p[[:space:]]+profile[[:space:]]+sip:[^ ]*@$IP:"; then
        echo "  freeswitch: profile $p up on $IP"
      else
        echo "  WARNING: freeswitch profile $p is not up on $IP; the next sync retries" >&2
        ok=0
      fi
    done
  fi
fi

# ── 5. Gateway and Campaigns ─────────────────────────────────────────────────
# Restarts this checkout's <label> processes with a stale SIP_PROXY_HOST.
_restart_stale() {
  local label=$1 pattern=$2 start=$3 pid cwd current stale=()
  for pid in $(pgrep -u "$(id -u)" -f "$pattern" || true); do
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
    [[ "$cwd" == "$REPO" ]] || continue
    current="$(ps eww -o command= -p "$pid" | tr ' ' '\n' | sed -n 's/^SIP_PROXY_HOST=//p' | head -1)"
    [[ "$current" == "$IP" ]] || stale+=("$pid")
  done
  [[ ${#stale[@]} -gt 0 ]] || return 0
  if _supervised "$label"; then
    if _sv restart "$label" >/dev/null; then
      echo "  $label: restarted by supervisord for SIP_PROXY_HOST=$IP"
    else
      echo "  WARNING: supervisorctl restart $label failed; it still has the old SIP_PROXY_HOST" >&2
      ok=0
    fi
    return 0
  fi
  _stop 40 "${stale[@]}"
  # env -u: else this shell's old value wins over .env.
  (cd "$REPO" && env -u SIP_PROXY_HOST nohup bash -c ". scripts/start_local.sh >/dev/null && $start" \
    >>"$YUVIZ_LOGS/$label.log" 2>&1 &)
  echo "  $label: restarted for SIP_PROXY_HOST=$IP (log $YUVIZ_LOGS/$label.log)"
}
# Anchored, so a shell merely mentioning the program never matches.
_restart_stale gateway '^[^ ]*build/gateway/voice_ai_gateway( |$)' start_gateway
_restart_stale campaigns '^[^ ]*[Pp]ython[0-9.]* -m uvicorn services\.campaigns\.app:app' start_campaigns_service

if [[ "$ok" == 1 ]]; then echo "$IP" > "$APPLIED"; echo "  done"; else rm -f "$APPLIED"; fi
