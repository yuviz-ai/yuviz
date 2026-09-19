# Telephony setup: Kamailio + FreeSWITCH in a Linux VM

The browser path (`./deployment/sh/dev.sh`) needs none of this. This doc is
only for making a real phone ring.

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

Debian's `kamailio` package installs config to /etc/kamailio, but every
script in this repo hardcodes /usr/local/etc/kamailio. Building from source
keeps scripts/update_kamailio_ip.sh working untouched.

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
    ./scripts/update_kamailio_ip.sh

Add a softphone extension. The domain must equal VM_IP — ha1/ha1b are MD5
digests with the domain baked in, and a stale domain surfaces as a generic
403 "call rejected" that looks nothing like an auth problem:

    kamctl add 1001 <password>
    kamailio -f /usr/local/etc/kamailio/kamailio.cfg -D -E

Dialable numbers are fixed by the cfg: `788` and `5000`-`5009` route to
FreeSWITCH; `1000`-`1002` are registered softphones (kamailio.cfg.tpl:665).

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

Four pieces. Every value here is pinned by host-side code — check the table
at the end before changing any of them.

**autoload_configs/modules.conf.xml** — load `mod_sofia`,
`mod_event_socket`, `mod_lua`, `mod_audio_fork`.

**autoload_configs/event_socket.conf.xml** — port 8022 (not the 8021
default), password ClueCon, and `listen-ip 0.0.0.0` so the Gateway on the
host can reach it. That last change matters for security: ESL is an
unauthenticated-by-default remote control surface for the whole switch, so
pin it to the host with an ACL and change the password:

    <param name="listen-ip" value="0.0.0.0"/>
    <param name="listen-port" value="8022"/>
    <param name="password" value="<something-not-ClueCon>"/>
    <param name="apply-inbound-acl" value="gateway_host"/>

and define `gateway_host` in autoload_configs/acl.conf.xml as HOST_IP/32.

**sip_profiles/external.xml** — `sip-port 5080`. Do not rename the profile:
`external` is baked into the ESL dial strings at
gateway/src/telephony/EslClient.cpp:318 and services/campaigns/originate.py.

**dialplan** — match `^(788|500\d)$`, answer, run the Lua below.

## 7. The Lua dialplan script

Referenced at gateway/src/core/Application.cpp:204 as start_voice_ai.lua but
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
    (gateway/src/config/Config.cpp:209). Malformed or late JSON degrades
    silently to tenant/agent `{"default","default"}` — a wrong-agent call
    that looks like a working one, not an error.
  - The frame must arrive within `metadata_wait_ms` (1500ms,
    config/gateway.yaml:11). It was raised from 300ms after two live
    warm-transfer calls landed right on that boundary.

## 8. Host-side config changes

These four are the entire cost of the VM split:

    config/gateway.yaml
      esl.host           127.0.0.1 -> VM_IP
      esl.sip_proxy_host 192.168.0.116 -> VM_IP
    services/campaigns (env)
      FREESWITCH_ESL_HOST=VM_IP
      SIP_PROXY_HOST=VM_IP

`gateway.websocket.host` is already 0.0.0.0, so it accepts the VM's
connection with no change.

## 9. Route a DID to an agent

A DID with no phone_numbers row resolves to `{"default","default"}` rather
than being rejected. Add it via Config Service so `did:5006` lands in Redis
in the shape services/config/phone_numbers.py:59-103 writes.

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

  - scripts/update_kamailio_ip.sh:44-45 hardcodes
    /usr/local/freeswitch/bin/{fs_cli,freeswitch}. Debian packages put those
    at /usr/bin, so step 3/3 silently prints "fs_cli not found — skipping"
    and never restarts FreeSWITCH after an IP change. Worth making
    overridable via env.
  - The same script's header (line 21) claims everything outside Kamailio is
    already 127.0.0.1/localhost. Not true: config/gateway.yaml:47 and
    services/campaigns/originate.py:72 both carry a literal 192.168.0.116,
    and neither is rewritten. Inbound keeps working while warm transfer and
    outbound campaigns break — an asymmetric failure that is annoying to
    diagnose.
  - scripts/start_local.sh:148-151 runs `cd $REPO && ./freeswitch`, but no
    such file is in the repo. Presumably an uncommitted local wrapper.

## Values pinned by host-side code

| What | Value | Fixed at |
|---|---|---|
| Kamailio SIP | udp:VM_IP:5060 | kamailio.cfg.tpl:221 |
| FreeSWITCH SIP | VM_IP:5080, profile `external` | dispatcher.list.tpl, EslClient.cpp:318 |
| Agent DIDs | 788, 5000-5009 | kamailio.cfg.tpl:665 |
| Softphones | 1000-1002 (MySQL subscriber) | kamailio.cfg.tpl:687 |
| ESL | 8022 (not 8021) | gateway.yaml:41-44, originate.py:71 |
| Gateway WS | ws://HOST_IP:8080/voice/<uuid> | Application.cpp:202-211 |
| Metadata | {"did","ani","direction"} | Config.cpp:209-228 |
| Audio | 16 kHz mono, 20 ms | gateway.yaml:26-29 |
