# Telephony setup: Kamailio + FreeSWITCH in a Linux VM

The browser path (`./deployment/sh/dev.sh`) needs none of this. This doc is
only for making a real phone ring.

**Try the native setup first:** [telephony-local-setup.md](telephony-local-setup.md)
runs everything on macOS or Linux with no VM. This doc is the fallback.

Whichever route you take, the Gateway only plays the agent's voice if
`mod_audio_fork` carries `scripts/freeswitch/mod_audio_fork-playback.patch`
(upstream discards the audio), and the Lua script must play
`silence_stream://-1` rather than sleep. Use the repo's
`scripts/freeswitch/start_voice_ai.lua` instead of the copy in section 7.

## Why a VM

Both daemons are source/apt installs whose config has never been in version
control (see docs/setup.md:230). Building FreeSWITCH natively on Apple
Silicon is a known-bad path — the bundled libvpx hardcodes x86_64 detection
(signalwire/freeswitch#1450) and Homebrew's /opt/homebrew prefix breaks more
of the build. Debian arm64 packages exist (FreeSWITCH >= 1.10.12), so the VM
is the cheaper route by a wide margin.

Split: **SIP + media in the VM, everything else stays native on the host.**

    Mac host                      Debian VM (routable IP)
    ────────────────────          ───────────────────────────
    C++ Gateway    :8080  <─────  FreeSWITCH mod_audio_fork
    ConvSvc/Envoy  :10000 ─────>  FreeSWITCH ESL :8022
    Config/Redis/PG               FreeSWITCH sofia "external" :5080
    Admin UI       :3000          Kamailio :5060  <──  Zoiper (on the Mac)
                                  MySQL :3306 (Kamailio tables only)

Kamailio and FreeSWITCH stay on the *same* host as each other, which is what
scripts/kamailio/*.tpl already assume (`listen=udp:__LAN_IP__:5060` and a
dispatcher target of `__LAN_IP__:5080` resolve to the same VM IP).

## 1. Create the VM

Needs an IP reachable from the host — SIP and RTP will not survive Docker
Desktop-style port publishing. OrbStack machines and Lima with vmnet both
give you one; plain Docker Desktop does not.

    # OrbStack (simplest on Apple Silicon)
    orb create debian:bookworm sip
    orb -m sip
    ip -4 addr show   # note this: VM_IP

A physical desk phone on your Wi-Fi needs *bridged* networking instead
(UTM's Bridged mode) — a host-only VM IP is not reachable from the LAN.

Record two addresses now; nearly every step below needs one of them:
  - `VM_IP`   — the VM as seen from the Mac
  - `HOST_IP` — the Mac as seen from inside the VM (`ip route | grep default`)

## 2. Kamailio (build from source, prefix /usr/local)

Debian's `kamailio` package lacks modules the config needs, so build from
source. The repo renders its own config into ~/.yuviz/kamailio, so the
installed one is unused.

    apt update && apt install -y git build-essential cmake bison flex \
      libssl-dev libmariadb-dev libmariadb-dev-compat default-mysql-server \
      pkg-config
    git clone --depth 1 -b master https://github.com/kamailio/kamailio
    cd kamailio
    make cfg include_modules="db_mysql dispatcher auth_db htable"
    make all && make install        # -> /usr/local/sbin/kamailio, /usr/local/etc/kamailio

    systemctl enable --now mariadb
    kamdbctl create                 # creates the `kamailio` database

The four modules above are the ones the cfg actually needs: db_mysql +
auth_db for the subscriber table, dispatcher for the FreeSWITCH target,
htable for the antiflood block.

## 3. Deploy the Kamailio config

Mount the repo into the VM (OrbStack exposes /Users automatically; Lima
mounts $HOME), then run the generator **inside the VM** so it picks up the
VM's IP rather than the Mac's:

    cd /Users/<you>/yuviz
    SIP_IP=VM_IP ./scripts/update_kamailio_ip.sh   # or SIP_IP=VM_IP in .env

Add a softphone extension. The domain must equal VM_IP — ha1/ha1b are MD5
digests with the domain baked in, and a stale domain surfaces as a generic
403 "call rejected" that looks nothing like an auth problem:

    kamctl add 1001 <password>
    kamailio -f ~/.yuviz/kamailio/kamailio.cfg -D -E -Y ~/.yuviz/kamailio/run

Dialable numbers are fixed by the cfg: `788` and `5000`-`5009` route to
FreeSWITCH; `1000`-`1002` are registered softphones (kamailio.cfg.tpl).

## 4. FreeSWITCH (apt, needs a free SignalWire token)

Create a Personal Access Token first — the repo is authenticated:
https://developer.signalwire.com/freeswitch/FreeSWITCH-Explained/Installation/HOWTO-Create-a-SignalWire-Personal-Access-Token_67240087

    TOKEN=<your PAT>
    apt install -y gnupg2 wget
    wget --http-user=signalwire --http-password=$TOKEN -O /usr/share/keyrings/signalwire-freeswitch-repo.gpg \
      https://freeswitch.signalwire.com/repo/deb/debian-release/signalwire-freeswitch-repo.gpg
    printf 'machine freeswitch.signalwire.com login signalwire password %s\n' "$TOKEN" > /etc/apt/auth.conf
    chmod 600 /etc/apt/auth.conf
    echo "deb [signed-by=/usr/share/keyrings/signalwire-freeswitch-repo.gpg] https://freeswitch.signalwire.com/repo/deb/debian-release/ bookworm main" \
      > /etc/apt/sources.list.d/freeswitch.list
    apt update && apt install -y freeswitch-meta-all freeswitch-dev

Verify you got >= 1.10.12; earlier releases predate arm64 support.

## 5. mod_audio_fork (the fiddly bit)

Not a FreeSWITCH module — it is third-party, and **the canonical
drachtio-freeswitch-modules repo has been taken down**. Use a mirror:

    apt install -y libwebsockets-dev
    git clone https://github.com/mdslaney/drachtio-freeswitch-modules
    cd drachtio-freeswitch-modules/modules/mod_audio_fork
    # build against the installed FreeSWITCH headers (freeswitch-dev), then:
    #   cp mod_audio_fork.so /usr/lib/freeswitch/mod/

Without this module there is no media path to the Gateway at all — the call
will connect and sit in silence. If the build fights you, check whether
`drachtio/drachtio-freeswitch-mrf:*-full` publishes an arm64 image with the
module prebuilt; do NOT run it under amd64 emulation, real-time audio and
RTP will not tolerate it.

## 6. FreeSWITCH config

Five pieces. Every value here is pinned by host-side code — check the table
at the end before changing any of them.

**autoload_configs/modules.conf.xml** — load `mod_sofia`,
`mod_event_socket`, `mod_lua`, `mod_audio_fork`.

**autoload_configs/event_socket.conf.xml** — port 8022 (not the 8021
default), a random password (the same value as `FREESWITCH_ESL_PASSWORD` in
`.env`), and `listen-ip VM_IP` so the Gateway on the host can reach it. That last change matters for security: ESL is a
remote control surface for the whole switch, so bind it to the one interface
the host uses (never `0.0.0.0`), pin it to the host with an ACL, and change
the password:

    <param name="listen-ip" value="VM_IP"/>
    <param name="listen-port" value="8022"/>
    <param name="password" value="<FREESWITCH_ESL_PASSWORD from .env>"/>
    <param name="apply-inbound-acl" value="gateway_host"/>

and define `gateway_host` in autoload_configs/acl.conf.xml as HOST_IP/32.

**sip_profiles/external.xml** — `sip-port 5080`. Do not rename the profile:
`external` is baked into the ESL dial strings in
gateway/src/telephony/EslClient.cpp.

**dialplan** — match `^(788|500\d)$`, answer, run the Lua below.

**dialplan/default.xml** — replace the stock `default` context with the
repo's deny-all one, exactly as `scripts/freeswitch/setup_macos.sh` does on
macOS. Do not skip this. The stock context is a demo dialplan: `779` is
eavesdrop-all, `886`/`*8`/`**<ext>` intercept another call, and the `35xx`
conference rooms are shared by every tenant on the switch. The stock
`public` context hands `10xx`, `35xx`-`38xx` and `5551212` on to it, so any
call that reaches the `external` profile can end up there:

    sudo /Users/<you>/yuviz/scripts/freeswitch/install_default_context.sh /etc/freeswitch
    fs_cli -P 8022 -p <password> -x reloadxml

The stock file is kept once as `dialplan/default.xml.stock`. Rerun it after
any package upgrade that rewrites `/etc/freeswitch/dialplan/default.xml`.

## 7. The Lua dialplan script

Referenced in gateway/src/core/Application.cpp as start_voice_ai.lua but
never committed. It has exactly three jobs: answer, start the fork, park.

    local uuid = session:get_uuid()
    session:answer()
    local meta = string.format('{"did":"%s","ani":"%s","direction":"inbound"}',
        session:getVariable("destination_number"),
        session:getVariable("caller_id_number"))
    freeswitch.API():execute("uuid_audio_fork",
        uuid .. " start ws://HOST_IP:8080/voice/" .. uuid .. " mono 16000 " .. meta)
    session:sleep(3600000)   -- Gateway drives hangup over ESL

Note `ws://HOST_IP:8080` — not 127.0.0.1, which would resolve to the VM
itself. This is the one line that differs from a single-host install.

Three details are load-bearing:
  - The channel UUID in the URL path becomes `session_id`, the calls-table
    PK. It replaced a per-process counter that collided across Gateway
    restarts and silently appended one call's transcript onto another's.
  - The metadata JSON keys are exactly `did`/`ani`/`direction`
    (gateway/src/config/Config.cpp). Malformed or late JSON degrades
    silently to tenant/agent `{"default","default"}` — a wrong-agent call
    that looks like a working one, not an error.
  - The frame must arrive within `metadata_wait_ms` (1500ms,
    config/gateway.yaml). It was raised from 300ms after two live
    warm-transfer calls landed right on that boundary.

## 8. Host-side config changes

These two are the entire cost of the VM split:

    .env (repo root; read by both the Gateway and Campaigns)
      FREESWITCH_ESL_HOST=VM_IP
      SIP_PROXY_HOST=VM_IP   (written by update_kamailio_ip.sh in step 3,
                              since the repo is mounted; check it)

These live only in `.env`; `config/gateway.yaml` no longer carries any
address.

After `update_kamailio_ip.sh` changes `SIP_PROXY_HOST`, start the Gateway and
Campaigns from a new terminal tab on the host (or `unset SIP_PROXY_HOST` and
re-source `scripts/start_local.sh`). A tab that sourced it earlier keeps the
old exported value, because `_load_env` never overrides the shell;
`start_gateway` and `start_campaigns_service` warn when the two differ.

The Gateway's audio WebSocket listens on loopback by default. In this split
FreeSWITCH connects from the VM, so set the host's VM-facing address in
`.env`:

      GATEWAY_LISTEN_HOST=HOST_IP

The WebSocket is unauthenticated: whoever connects can stream a call as any
DID. Use the host-only network address rather than `0.0.0.0`, and allow only
the VM's IP to port 8080 in the host firewall.

## 9. Route a DID to an agent

A DID with no phone_numbers row resolves to `{"default","default"}` rather
than being rejected. Add it via Config Service so `did:5006` lands in Redis
in the shape services/config/phone_numbers.py writes.

## 10. Start order and first call

Hard-ordered: MySQL -> Kamailio -> FreeSWITCH (Kamailio's tables live in
MySQL; FreeSWITCH registers upstream to Kamailio). On the host,
`source scripts/start_local.sh` then `portmap`.

Register Zoiper on the Mac as `1001@VM_IP` and dial `5006`.

Triage order when it fails:
  - phone never rings         -> Kamailio: `kamailio -c -f <cfg>`, then its log
  - rings, then silence       -> mod_audio_fork not loaded, or ws:// unreachable
  - audio works, wrong agent  -> metadata frame late/malformed, or no DID row
  - agent can't end the call  -> ESL unreachable from host (ACL, or port 8022)

## Known repo gaps this setup runs into

  - SIP_PROXY_HOST in .env must be VM_IP. update_kamailio_ip.sh writes it;
    while it is blank the Gateway refuses transfers to numbers with an
    explicit error. An old
    .env holding 127.0.0.1 is wrong here: rerun the script, then restart
    from a new tab (see section 8).

## Values pinned by host-side code

| What | Value | Fixed at |
|---|---|---|
| Kamailio SIP | udp:VM_IP:5060 | kamailio.cfg.tpl |
| FreeSWITCH SIP | VM_IP:5080, profile `external` | dispatcher.list.tpl, EslClient.cpp |
| Agent DIDs | 788, 5000-5009 | kamailio.cfg.tpl |
| Softphones | 1000-1002 (MySQL subscriber) | kamailio.cfg.tpl |
| ESL | 8022 (not 8021) | `.env` FREESWITCH_ESL_PORT |
| Gateway WS | ws://HOST_IP:8080/voice/<uuid> | Application.cpp |
| Metadata | {"did","ani","direction"} | Config.cpp |
| Audio | 16 kHz mono, 20 ms | gateway.yaml |
