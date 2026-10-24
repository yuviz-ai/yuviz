#!/usr/bin/env bash
# Installs the outbound-campaign files into an existing FreeSWITCH install. Safe to re-run.
#
#   install_campaign_files.sh <freeswitch-prefix> [conf-dir] [scripts-dir]
#
# Copies the campaign Lua scripts into the install's scripts directory, the dialplan and
# egress profile into its conf directory, writes mod_hiredis's connection (from
# REDIS_FREESWITCH_PASSWORD in .env) and loads it. Re-run after editing any file here:
# FreeSWITCH reads its Lua from the installed copy, not from the repo.

set -euo pipefail

PREFIX="${1:?usage: $0 <freeswitch-prefix> [conf-dir]}"
CONF="${2:-$PREFIX/etc/freeswitch}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
SCRIPTS="${3:-$PREFIX/share/freeswitch/scripts}"
MOD="$PREFIX/lib/freeswitch/mod"

[[ -d "$CONF/autoload_configs" ]] || { echo "ERROR: no FreeSWITCH config at $CONF" >&2; exit 1; }
[[ -f "$MOD/mod_hiredis.so" ]] || { echo "ERROR: $MOD/mod_hiredis.so missing; build it first (see mod_hiredis-username.patch)" >&2; exit 1; }

redis_pw=$(grep -E '^REDIS_FREESWITCH_PASSWORD=' "$REPO_ROOT/.env" 2>/dev/null | cut -d= -f2- || true)
[[ "$redis_pw" =~ ^[A-Za-z0-9_.~-]+$ ]] || { echo "ERROR: REDIS_FREESWITCH_PASSWORD must be set in .env (A-Za-z0-9_.~- only)" >&2; exit 1; }

if [[ ! "$SCRIPTS" -ef "$HERE" ]]; then
  for f in start_voice_ai.lua start_campaign_call.lua campaign_event.lua; do
    install -m 644 "$HERE/$f" "$SCRIPTS/$f"
  done
fi
install -m 644 "$HERE/000_campaign_trigger.xml" "$CONF/dialplan/public/000_campaign_trigger.xml"
install -m 644 "$HERE/campaign_egress_refuse.xml" "$CONF/dialplan/campaign_egress_refuse.xml"
install -m 644 "$HERE/kamailio_egress.xml" "$CONF/sip_profiles/kamailio_egress.xml"
echo "  copied campaign scripts, dialplan and kamailio_egress profile"

"$HERE/install_campaign_config.sh" "$CONF"

hiredis="$CONF/autoload_configs/hiredis.conf.xml"
( umask 077; cat > "$hiredis" <<XML
<configuration name="hiredis.conf" description="mod_hiredis">
  <profiles>
    <profile name="default">
      <connections>
        <connection name="primary">
          <param name="hostname" value="127.0.0.1"/>
          <param name="port" value="6379"/>
          <param name="username" value="freeswitch"/>
          <param name="password" value="$redis_pw"/>
        </connection>
      </connections>
    </profile>
  </profiles>
</configuration>
XML
)
echo "  hiredis.conf: 127.0.0.1:6379 as ACL user freeswitch"

modules="$CONF/autoload_configs/modules.conf.xml"
if ! grep -q 'mod_hiredis' "$modules"; then
  sed -i.bak 's|\(.*<load module="mod_lua"/>.*\)|\1\n    <load module="mod_hiredis"/>|' "$modules" && rm -f "$modules.bak"
  echo "  modules.conf: + mod_hiredis"
fi
echo "campaign files installed in $PREFIX; restart FreeSWITCH to load them"
