#!/usr/bin/env bash
# install_default_context.sh — replace FreeSWITCH's stock "default" dialplan
# context with the repo's deny-all one (scripts/freeswitch/default_context.xml).
#
# The stock context's eavesdrop/intercept extensions reach every tenant's calls,
# and nothing here routes to `default`. Idempotent; stock kept as default.xml.stock.
#
# Usage: scripts/freeswitch/install_default_context.sh <freeswitch-conf-dir>
#   e.g. ~/.yuviz/freeswitch/conf (macOS) or /etc/freeswitch (Debian apt)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${1:?usage: $0 <freeswitch-conf-dir>}"
DIALPLAN="$CONF/dialplan"

if [[ ! -d "$DIALPLAN" ]]; then
  echo "ERROR: $DIALPLAN not found — pass FreeSWITCH's conf dir" >&2
  exit 1
fi

if [[ -f "$DIALPLAN/default.xml" && ! -f "$DIALPLAN/default.xml.stock" ]] &&
   ! grep -q 'name="deny_all"' "$DIALPLAN/default.xml"; then
  mv "$DIALPLAN/default.xml" "$DIALPLAN/default.xml.stock"
fi
cp "$HERE/default_context.xml" "$DIALPLAN/default.xml"
echo "  dialplan: default context replaced with deny-all (stock kept as default.xml.stock)"
