#!/usr/bin/env bash
# Opt-in: run supervisord as a macOS LaunchAgent (starts at login, restarted if it dies).
# Usage: deployment/autostart.sh install | uninstall | status | print
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="ai.yuviz.supervisord"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CONF="$REPO/deployment/supervisord.conf"
SOCK="$HOME/.yuviz/supervisor.sock"

[ "$(uname)" = Darwin ] || { echo "autostart is macOS only" >&2; exit 1; }

plist() {
  local supervisord
  supervisord="$(command -v supervisord)" || { echo "supervisord not found: brew install supervisor" >&2; exit 1; }
  # launchd jobs don't inherit the shell PATH; capture it (Homebrew, func-e, the venv) at install time.
  local path="$REPO/venv/bin:$PATH"
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$supervisord</string><string>-c</string><string>$CONF</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$path</string><key>HOME</key><string>$HOME</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- An untyped job is throttled by macOS, which slows call audio. -->
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME/.yuviz/logs/supervisord.launchd.log</string>
  <key>StandardErrorPath</key><string>$HOME/.yuviz/logs/supervisord.launchd.log</string>
</dict>
</plist>
EOF
}

running_elsewhere() {
  [ -S "$SOCK" ] && supervisorctl -c "$CONF" pid >/dev/null 2>&1 && ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1
}

case "${1:-status}" in
  print)
    plist
    ;;
  install)
    if running_elsewhere; then
      echo "supervisord is already running (probably in a terminal tab). Stop it with Ctrl-C there, then re-run install." >&2
      exit 1
    fi
    mkdir -p "$HOME/.yuviz/logs" "$(dirname "$PLIST")"
    plist > "$PLIST"
    plutil -lint "$PLIST" >/dev/null
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "installed $PLIST; supervisord now starts at login. Check: deployment/svc status"
    ;;
  uninstall)
    # Stops supervisord and every service it runs.
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $LABEL"
    ;;
  status)
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
      launchctl print "gui/$(id -u)/$LABEL" | grep -E "^\s+(state|pid|last exit code) =" || true
    else
      echo "not installed"
    fi
    ;;
  *)
    echo "usage: $0 install | uninstall | status | print" >&2
    exit 2
    ;;
esac
