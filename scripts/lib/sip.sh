# SIP helpers for start_local.sh and update_kamailio_ip.sh. Needs REPO.

KAMAILIO_DIR="${KAMAILIO_DIR:-$HOME/.yuviz/kamailio}"
YUVIZ_LOGS="${YUVIZ_LOGS:-$HOME/.yuviz/logs}"
# Pre-KAMAILIO_DIR config; a Kamailio still using it gets moved over.
KAMAILIO_LEGACY_CFG="${KAMAILIO_LEGACY_CFG-/usr/local/etc/kamailio/kamailio.cfg}"
NETWORK_SYNC_LABEL=ai.yuviz.network-sync

# Default-route address; a UDP connect sends nothing.
_detect_lan_ip() {
  python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
except OSError:
    pass
finally:
    s.close()
"
}

# Interface the default route egresses on (empty if unknown).
# YUVIZ_TEST_EGRESS_IFACE overrides for tests.
_egress_iface() {
  if [ -n "${YUVIZ_TEST_EGRESS_IFACE+x}" ]; then
    echo "$YUVIZ_TEST_EGRESS_IFACE"
    return 0
  fi
  if command -v route >/dev/null 2>&1; then
    # macOS: `route -n get 8.8.8.8`
    route -n get 8.8.8.8 2>/dev/null | awk '/interface:/{print $2; exit}'
  elif command -v ip >/dev/null 2>&1; then
    ip route get 8.8.8.8 2>/dev/null \
      | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit }}'
  fi
}

# Full-tunnel VPN ifaces drop packets a host sends to their own address.
_is_vpn_iface() {
  case "$1" in
    utun*|ppp*|ipsec*) return 0 ;;
    *) return 1 ;;
  esac
}

# SIP_IP: 127.0.0.1 (default), auto (LAN IP) or a fixed address.
# Optional $1: path to the last applied IP. With auto over a VPN tunnel, print
# that applied address (or nothing) instead of the tunnel address.
_sip_target_ip() {
  local mode="${SIP_IP:-127.0.0.1}" iface applied_file="${1:-}" applied
  if [ "$mode" != auto ]; then
    echo "$mode"
    return 0
  fi
  iface=$(_egress_iface)
  if _is_vpn_iface "$iface"; then
    if [ -n "$applied_file" ] && [ -s "$applied_file" ]; then
      applied=$(cat "$applied_file")
      echo "WARNING: default route is on VPN interface $iface; keeping SIP on $applied" >&2
      echo "$applied"
      return 0
    fi
    echo "WARNING: default route is on VPN interface $iface; not moving SIP onto it" >&2
    return 1
  fi
  _detect_lan_ip
}

# FS_PREFIX if set; else Homebrew, source install, then /usr.
_fs_prefix() {
  if [ -n "${FS_PREFIX:-}" ]; then
    [ -x "$FS_PREFIX/bin/freeswitch" ] && echo "$FS_PREFIX"
    return
  fi
  local p
  for p in "$(brew --prefix freeswitch 2>/dev/null)" /usr/local/freeswitch /usr; do
    [ -n "$p" ] && [ -x "$p/bin/freeswitch" ] && { echo "$p"; return 0; }
  done
  return 1
}

_fs_home() { echo "${FS_HOME:-$HOME/.yuviz/freeswitch}"; }

# The vars.xml of the install _fs_start runs.
_fs_vars_xml() {
  local prefix f
  prefix=$(_fs_prefix) || return 1
  for f in "$(_fs_home)/conf/vars.xml" "$prefix/etc/freeswitch/vars.xml" /etc/freeswitch/vars.xml; do
    [ -f "$f" ] && { echo "$f"; return 0; }
  done
  return 1
}

# Runs FreeSWITCH in the foreground; pass -nc to daemonize.
_fs_start() {
  local prefix fs_home
  prefix=$(_fs_prefix) || { echo "FreeSWITCH not found — run scripts/freeswitch/setup_macos.sh" >&2; return 1; }
  fs_home=$(_fs_home)
  if [ -d "$fs_home/conf" ]; then
    "$prefix/bin/freeswitch" "$@" -nonat -conf "$fs_home/conf" -log "$fs_home/log" \
      -db "$fs_home/db" -run "$fs_home/run" -scripts "$REPO/scripts/freeswitch"
  else
    # An install without our config dir reads Lua from its own scripts directory; keep those copies current.
    "$REPO/scripts/freeswitch/install_campaign_files.sh" "$prefix" || return 1
    "$prefix/bin/freeswitch" "$@" -nonat
  fi
}

# Runs Kamailio in the foreground from the rendered config.
_kamailio_start() {
  mkdir -p "$KAMAILIO_DIR/run"
  kamailio -f "$KAMAILIO_DIR/kamailio.cfg" -DD -E -Y "$KAMAILIO_DIR/run"
}

_kamailio_pids() {
  pgrep -u "$(id -u)" -f "^([^ ]*/)?kamailio -f $KAMAILIO_DIR/kamailio.cfg"
  [ -n "$KAMAILIO_LEGACY_CFG" ] && pgrep -u "$(id -u)" -f "^([^ ]*/)?kamailio -f $KAMAILIO_LEGACY_CFG"
  return 0
}

# launchd agent that reruns update_kamailio_ip.sh whenever the network changes.
_network_sync_plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$NETWORK_SYNC_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$REPO/scripts/update_kamailio_ip.sh</string>
    <string>--if-changed</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$REPO/venv/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>WatchPaths</key>
  <array>
    <string>/var/run/resolv.conf</string>
    <string>/Library/Preferences/SystemConfiguration</string>
  </array>
  <!-- Else launchd throttles the CPU of everything it restarts. -->
  <key>ProcessType</key><string>Interactive</string>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>AbandonProcessGroup</key><true/>
  <key>StandardOutPath</key><string>$YUVIZ_LOGS/network-sync.log</string>
  <key>StandardErrorPath</key><string>$YUVIZ_LOGS/network-sync.log</string>
</dict>
</plist>
EOF
}
