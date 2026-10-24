#!/usr/bin/env bash
# install_campaign_config.sh — FreeSWITCH config the campaign Path A needs, applied to an installed conf dir:
#   - lua.conf.xml: campaign_event.lua as a global event hook for CHANNEL_ANSWER and CHANNEL_HANGUP_COMPLETE
#   - sip_profiles/external.xml: enable-3pcc=proxy, so Kamailio's SDP-less trigger INVITE reaches the dialplan
# Idempotent.
#
# Usage: scripts/freeswitch/install_campaign_config.sh <freeswitch-conf-dir>

set -euo pipefail

CONF="${1:?usage: $0 <freeswitch-conf-dir>}"

_insert_before_settings_end() {
  local file="$1" line="$2"
  [[ "$(grep -c '</settings>' "$file")" == 1 ]] || { echo "ERROR: $file must contain exactly one </settings>" >&2; exit 1; }
  sed -i.bak "s|</settings>|$line\n  </settings>|" "$file"
  rm -f "$file.bak"
}

lua_conf="$CONF/autoload_configs/lua.conf.xml"
for event in CHANNEL_ANSWER CHANNEL_HANGUP_COMPLETE; do
  if ! grep -q "<hook event=\"$event\" script=\"campaign_event.lua\"/>" "$lua_conf"; then
    _insert_before_settings_end "$lua_conf" "    <hook event=\"$event\" script=\"campaign_event.lua\"/>"
    echo "  lua.conf: + campaign_event.lua on $event"
  fi
done

external="$CONF/sip_profiles/external.xml"
if ! grep -q '^[[:space:]]*<param name="enable-3pcc" value="proxy"/>' "$external" ||
   [[ "$(grep -c '^[[:space:]]*<param name="enable-3pcc"' "$external")" != 1 ]]; then
  sed -i.bak '/^[[:space:]]*<param name="enable-3pcc"/d' "$external"
  rm -f "$external.bak"
  _insert_before_settings_end "$external" '    <param name="enable-3pcc" value="proxy"/>'
  echo "  sip_profiles: external enable-3pcc = proxy"
fi
