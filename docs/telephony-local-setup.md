# Local telephony setup (macOS and Linux, no VM)

This guide gets a softphone on your machine calling a Yuviz agent over real
SIP, with every piece running natively on the same host. You need it to test
anything telephony-specific: DTMF and IVR call flows, ESL hangup and transfer,
or the Gateway's media path. To just talk to an agent, use the browser path
(`./deployment/sh/dev.sh`), which needs none of this.

| Platform | Status |
|---|---|
| macOS (Apple Silicon) | Tested end to end: inbound call, agent audio, Cal.com booking, IVR with DTMF |
| Linux (Debian 12 / Ubuntu 22.04+) | Same components and config; the commands below have **not** been run yet |

If the native build fights you, the older [VM guide](telephony-vm-setup.md)
is still the fallback.

## How a call flows

```
Softphone (Zoiper)            udp 127.0.0.1:5060
   │  SIP REGISTER / INVITE
   ▼
Kamailio        :5060  ── routes 788, 5000-5009 ──▶  FreeSWITCH "external" :5080
                                                         │ dialplan → start_voice_ai.lua
                                                         │ mod_audio_fork (patched)
                                                         ▼
                                  C++ Gateway  ws://127.0.0.1:8080/voice/<uuid>
                                                         │ gRPC
                                                         ▼
                                  Envoy :10000 → Conversation service :50051
                                  Config service :8000 (DID → agent, via Redis)
```

FreeSWITCH forks the caller's audio to the Gateway. The Gateway sends the
agent's speech back as binary 16 kHz L16 frames on the same WebSocket, and the
patched `mod_audio_fork` plays them into the call. The Gateway also connects
to FreeSWITCH's event socket (ESL, `127.0.0.1:8022`) to receive DTMF and
hangups and to send hangup and transfer commands.

Everything binds to **127.0.0.1** by default (`SIP_IP` in `.env`), which never
changes with your Wi-Fi or VPN. Only a softphone on this Mac can call in; for
a phone on your LAN, see [Using your LAN IP instead](#using-your-lan-ip-instead).

## What's in the repo

| Path | What it is |
|---|---|
| `scripts/freeswitch/setup_macos.sh` | macOS: installs FreeSWITCH, builds and patches `mod_audio_fork`, writes the FreeSWITCH config. Safe to rerun. |
| `scripts/freeswitch/mod_audio_fork-playback.patch` | Makes `mod_audio_fork` play the Gateway's binary audio into the call. Upstream discards it, so without the patch the caller hears nothing. |
| `scripts/freeswitch/start_voice_ai.lua` | Dialplan script: answers, starts the fork with `{"did","ani","direction"}` metadata, then plays endless silence while the Gateway drives the call. |
| `scripts/freeswitch/00_voice_ai.xml` | Dialplan entry: routes `788` and `5000`-`5009` to the Lua script. |
| `scripts/kamailio/*.tpl` | Kamailio config templates (`__LAN_IP__` gets replaced). |
| `scripts/update_kamailio_ip.sh` | Applies `SIP_IP` to Kamailio, FreeSWITCH and `.env`, and restarts what is stale. |
| `scripts/start_local.sh` | `start_*` helpers for every service. |

## Before you start

The application side has to work first: Postgres, Redis, the Config service
and the Conversation service, as described in [setup.md](setup.md). In
particular, `JWT_SECRET` and `SECRET_ENCRYPTION_KEY` must be exported in each
terminal that starts a Python service, with the same values everywhere.

You also need MySQL for Kamailio's tables, `cmake`, and a C/C++ toolchain.

---

## macOS

### 1. FreeSWITCH and mod_audio_fork

```bash
SIP_IP=127.0.0.1 scripts/freeswitch/setup_macos.sh
```

This script:
- installs Homebrew's prebuilt arm64 FreeSWITCH;
- clones the `mod_audio_fork` source mirror into `~/src/drachtio-freeswitch-modules` (the original drachtio repo was taken down), applies the playback patch, builds it and installs it;
- copies the stock config into `~/.yuviz/freeswitch/conf`, because Homebrew's `etc/freeswitch` is symlinks into the versioned install directory and edits there are lost on `brew upgrade`;
- edits that copy:
  - loads `mod_audio_fork`;
  - puts ESL on `127.0.0.1:8022`;
  - disables the stock `internal` SIP profile, which would bind 5060, Kamailio's port;
  - sets `local_ip_v4` and `external_{rtp,sip}_ip` to `127.0.0.1` (the default STUN lookup puts your public IP into the call setup and the call goes silent);
  - installs the dialplan entry;
  - replaces the stock `default` dialplan context with a deny-all one (`scripts/freeswitch/install_default_context.sh`). The stock context is a demo whose `779`/`886` extensions eavesdrop on or intercept any call on the switch, and the stock `public` context hands calls on to it.

Rerun it after every `brew upgrade freeswitch`, which deletes the built module.

### 2. Kamailio

Build Kamailio from source into `/usr/local` (its config is rendered into
`~/.yuviz/kamailio`). It needs `db_mysql` and `http_client`,
and **both are excluded from a default build**. Build them explicitly:

```bash
git clone --depth 1 https://github.com/kamailio/kamailio ~/kamailio
cd ~/kamailio
make cfg && make all && sudo make install          # skip if already installed

# the two modules the repo config needs
PATH="$(brew --prefix mysql)/bin:$PATH" LIBRARY_PATH="$(brew --prefix)/lib" \
  make -C src modules modules="modules/db_mysql modules/http_client"
sudo cp src/modules/db_mysql/db_mysql.so src/modules/http_client/http_client.so \
  /usr/local/lib64/kamailio/modules/
```

`LIBRARY_PATH` is needed because the linker can't otherwise find Homebrew's
`zstd`.

Then continue with [Kamailio database and extensions](#kamailio-database-and-extensions).

### 3. Build the Gateway

```bash
brew install spdlog yaml-cpp nlohmann-json grpc hiredis onnxruntime googletest libwebsockets func-e
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(sysctl -n hw.logicalcpu)" --target voice_ai_gateway
```

`func-e` runs Envoy (`start_envoy`).

---

## Linux (Debian / Ubuntu) — untested

These are the same components with Linux paths. `setup_macos.sh` uses macOS
`sed -i ''` and Homebrew paths, so on Linux you apply its steps by hand.

### 1. FreeSWITCH

Install it from SignalWire's apt repo. That requires a free SignalWire
Personal Access Token; the steps are in section 4 of the
[VM guide](telephony-vm-setup.md#4-freeswitch-apt-needs-a-free-signalwire-token).
You want `freeswitch-meta-all` and `freeswitch-dev`, version 1.10.12 or
later.

Debian's paths: config in `/etc/freeswitch`, modules in
`/usr/lib/freeswitch/mod`, `fs_cli` in `/usr/bin`, Lua scripts in
`/usr/share/freeswitch/scripts`.

### 2. mod_audio_fork, patched

```bash
sudo apt install -y git build-essential pkg-config libwebsockets-dev libspeexdsp-dev libssl-dev
git clone --depth 1 https://github.com/mdslaney/drachtio-freeswitch-modules ~/src/drachtio-freeswitch-modules
git -C ~/src/drachtio-freeswitch-modules apply "$PWD/scripts/freeswitch/mod_audio_fork-playback.patch"

cd ~/src/drachtio-freeswitch-modules/modules/mod_audio_fork
CFLAGS="$(pkg-config --cflags freeswitch libwebsockets speexdsp) -fPIC -O2"
LIBS="$(pkg-config --libs freeswitch libwebsockets speexdsp)"
gcc -c $CFLAGS mod_audio_fork.c -o mod_audio_fork.o
for f in lws_glue parser audio_pipe; do g++ -std=c++14 -c $CFLAGS $f.cpp -o $f.o; done
g++ -shared -o mod_audio_fork.so *.o $LIBS
sudo cp mod_audio_fork.so /usr/lib/freeswitch/mod/
```

If `pkg-config` can't find `freeswitch`, look for `freeswitch.pc` under
`/usr/lib/pkgconfig` (or `/usr/lib/*/pkgconfig`) and add that directory to
`PKG_CONFIG_PATH`.

### 3. FreeSWITCH config

These are the same edits `setup_macos.sh` makes, applied to `/etc/freeswitch`:

```bash
C=/etc/freeswitch
REPO="$PWD"   # run from the repo root

# load the module
sudo sed -i 's|\(.*<load module="mod_lua"/>.*\)|\1\n    <load module="mod_audio_fork"/>|' $C/autoload_configs/modules.conf.xml

# ESL on loopback, port 8022 (FREESWITCH_ESL_PORT in .env)
sudo sed -i -e 's|name="listen-ip" value="[^"]*"|name="listen-ip" value="127.0.0.1"|' \
            -e 's|name="listen-port" value="[^"]*"|name="listen-port" value="8022"|' \
            $C/autoload_configs/event_socket.conf.xml

# free port 5060 for Kamailio
for p in internal.xml internal-ipv6.xml external-ipv6.xml; do
  [ -f $C/sip_profiles/$p ] && sudo mv $C/sip_profiles/$p $C/sip_profiles/$p.disabled
done

# no STUN; pin everything to 127.0.0.1
sudo sed -i -e 's|data="external_rtp_ip=stun:[^"]*"|data="external_rtp_ip=$${local_ip_v4}"|' \
            -e 's|data="external_sip_ip=stun:[^"]*"|data="external_sip_ip=$${local_ip_v4}"|' $C/vars.xml
sudo sed -i 's|^<include>|<include>\n  <X-PRE-PROCESS cmd="set" data="local_ip_v4=127.0.0.1"/>|' $C/vars.xml

# dialplan + Lua script (symlinked, so edits in the repo apply on the next call)
sudo cp "$REPO/scripts/freeswitch/00_voice_ai.xml" $C/dialplan/public/
sudo ln -sf "$REPO/scripts/freeswitch/start_voice_ai.lua" /usr/share/freeswitch/scripts/start_voice_ai.lua

# deny-all "default" context: the stock one eavesdrops on/intercepts other calls (779, 886)
sudo "$REPO/scripts/freeswitch/install_default_context.sh" $C

sudo systemctl restart freeswitch
```

Set the ESL password to a random value in `event_socket.conf.xml`, and the
same value as `FREESWITCH_ESL_PASSWORD` in `.env`; the Gateway and Campaigns
read it from there. Don't leave FreeSWITCH's built-in default: anything that
can reach port 8022 can control every call.

### 4. Kamailio

Build it from source into `/usr/local`, including the modules the repo config
needs:

```bash
sudo apt install -y bison flex libssl-dev libmariadb-dev libmariadb-dev-compat libcurl4-openssl-dev default-mysql-server
git clone --depth 1 https://github.com/kamailio/kamailio ~/kamailio && cd ~/kamailio
make cfg include_modules="db_mysql dispatcher auth_db htable http_client"
make all && sudo make install
```

The scripts find FreeSWITCH under Homebrew, `/usr/local/freeswitch` or `/usr`
(apt), in that order. Set `FS_PREFIX` to choose one.

### 5. Build the Gateway

```bash
sudo apt install -y cmake libspdlog-dev libyaml-cpp-dev nlohmann-json3-dev \
  libgrpc++-dev protobuf-compiler-grpc libhiredis-dev libgtest-dev libwebsockets-dev
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)" --target voice_ai_gateway
```

ONNX Runtime usually isn't packaged by distros. Download the Linux release
from github.com/microsoft/onnxruntime and put its headers in
`/usr/local/include/onnxruntime` and `libonnxruntime.so` in `/usr/local/lib`,
which are the paths `gateway/CMakeLists.txt` searches. For Envoy, install
`func-e` from func-e.io, or any Envoy binary on `PATH`.

---

## Kamailio database and extensions

This part is the same on both platforms.

**Create the `kamailio` database** from the schema files in the Kamailio
source tree. This avoids depending on `kamdbctl` having MySQL support built in:

```bash
cd ~/kamailio/utils/kamctl/mysql
mysql -uroot -e "CREATE DATABASE IF NOT EXISTS kamailio;
  CREATE USER IF NOT EXISTS 'kamailio'@'localhost' IDENTIFIED BY 'kamailiorw';
  GRANT ALL PRIVILEGES ON kamailio.* TO 'kamailio'@'localhost';"
mysql -uroot kamailio < standard-create.sql
for f in *-create.sql; do [ "$f" = standard-create.sql ] || mysql -uroot kamailio < "$f"; done
```

The user and password must match `DBURL` in `scripts/kamailio/kamailio.cfg.tpl`.

**Add softphone extensions.** `ha1` and `ha1b` are MD5 digests with the SIP
domain baked in, so the domain must be exactly the IP Kamailio listens on:

```bash
python3 - <<'EOF' | mysql -uroot kamailio
import hashlib
domain, password = "127.0.0.1", "change-me"
for user in ("1001", "1002"):
    ha1  = hashlib.md5(f"{user}:{domain}:{password}".encode()).hexdigest()
    ha1b = hashlib.md5(f"{user}@{domain}:{domain}:{password}".encode()).hexdigest()
    print(f"DELETE FROM subscriber WHERE username='{user}'; "
          f"INSERT INTO subscriber(username,domain,password,ha1,ha1b) "
          f"VALUES('{user}','{domain}','{password}','{ha1}','{ha1b}');")
EOF
```

Only `1000`-`1002` are routed as softphones, a limit set in `kamailio.cfg.tpl`.

**Render and deploy the config:**

```bash
./scripts/update_kamailio_ip.sh     # start_kamailio also runs it
kamailio -c -f ~/.yuviz/kamailio/kamailio.cfg -Y ~/.yuviz/kamailio/run   # should print "config file ok"
```

## Route a number to an agent

The dialable numbers are `788` and `5000`-`5009`. A number with no mapping
still connects, but goes to the `default` agent, with no error. Map one in
the admin UI's **Telephony** page (`localhost:3000/telephony`). That path goes
through the Config service, which writes Postgres and warms `did:<number>` in
Redis, where the Gateway reads the route. A row inserted straight into
Postgres won't be picked up until the Config service restarts and prewarms.

Check a mapping:

```bash
redis-cli get did:5006
# {"tenant_slug": "...", "agent_slug": "...", "version": N}
```

To test an IVR, map the number to an agent that has a published call flow
(`agents.call_flow_id`). The conversation service runs the flow first, then
hands over to the agent the flow's `agent` node names.

## Start everything

Order matters for the SIP pieces: MySQL, then Kamailio, then FreeSWITCH. Run
each service in its own terminal:

```bash
source scripts/start_local.sh
start_mysql
start_kamailio          # runs without sudo (-Y ~/.yuviz/kamailio/run)
start_freeswitch        # macOS; on Linux: sudo systemctl start freeswitch
start_config_service    # :8000
start_conv1             # :50051
start_envoy             # :10000
start_gateway           # :8080
portmap                 # shows what's listening
```

A few gotchas with `start_local.sh`:
- The Python helpers call `python3`, so **activate the repo venv first** (`source venv/bin/activate`), otherwise you get `No module named uvicorn`.
- Settings come from the repo's `.env` (template `.env.example`), which sourcing the script creates and fills with generated secrets on first run. Each `start_*` names any setting it still needs, such as `FREESWITCH_ESL_PASSWORD`.
- After rebuilding `mod_audio_fork`, **restart FreeSWITCH fully**. `reload mod_audio_fork` breaks the module's WebSocket layer, and FreeSWITCH exits on the next call.

### Or: one tab with supervisord (macOS)

`deployment/supervisord.conf` runs every service above, plus Ollama, Knowledge, Campaigns, Toolexec, Telephony, Webcall and the Admin UI, from one terminal tab. It restarts any service that crashes. It calls the same `start_*` helpers, so settings still come from `.env`, and a `.env` edit takes effect on the next `restart` even if the tab you started supervisord from had exported an older value. A key left blank in `.env` still comes from that tab. MySQL, PostgreSQL and Redis stay under `brew services` (`start_mysql`, `start_data`).

```bash
brew install supervisor
mkdir -p ~/.yuviz/logs
supervisord -c deployment/supervisord.conf    # stays in the foreground; Ctrl-C stops everything
```

From any other tab:

```bash
supervisorctl -c deployment/supervisord.conf status
supervisorctl -c deployment/supervisord.conf restart gateway
tail -f ~/.yuviz/logs/gateway.log
```

- **Run it in a normal foreground tab.** Every service inherits supervisord's CPU priority, so starting it with `&` or from a background launchd job slows call audio.
- **Stop it with Ctrl-C, not by closing the tab.** Closing the tab sends SIGHUP, which supervisord treats as "reload and restart every service".
- **Check for live calls before restarting** FreeSWITCH, the Gateway or the Conversation Services. `restart` doesn't check.
- **After editing the config,** `supervisorctl -c deployment/supervisord.conf update` restarts only the services whose section changed.
- **Network sync** (`install_network_sync`, `SIP_IP=auto`) works alongside it: `update_kamailio_ip.sh` restarts Kamailio, FreeSWITCH, the Gateway and Campaigns through `supervisorctl` while supervisord runs them.
- **Logs:** each service writes its own log to `~/.yuviz/logs/<service>.log`. `<service>.supervisor.log` holds only errors from starting it. The `log-rotate` listener checks hourly and rotates any log over 20 MB, keeping 3 (`.1`–`.3`).
- **Crash loops stop instead of flapping:**
  - **Crashes during startup:** a service that dies within its first few seconds is retried 3 times, then marked FATAL.
  - **Crashes after startup:** the `crash-watch` listener backs off. The first crash restarts at once, and the next ones wait 2, 4 and 8 s.
  - **Circuit breaker:** after 5 crashes within 10 minutes, or a FATAL, the circuit opens. The service is stopped and gets one trial start after 5 minutes, then 10, 20 and so on, up to 1 hour. If it stays up for 10 minutes, the circuit closes.
  - **Where to look:** `~/.yuviz/logs/crash-watch.log`, plus a macOS notification when the circuit opens. Install `terminal-notifier` so clicking it opens that log: `brew install terminal-notifier`, then launch it once with `open "$(brew --prefix terminal-notifier)/terminal-notifier.app"` so macOS registers it, and allow its notifications. Without it, the notification is credited to Script Editor.
  - **Safe to restart:** state is saved in `~/.yuviz/crash-watch.json`, so a restart of crash-watch resumes pending restarts. A service you stop by hand is never restarted.

## Softphone

Use **Zoiper 5** (free, zoiper.com). Linphone Desktop 6.2.1 registered fine
but crashed the moment a call started, in its own call-window code.

| Setting | Value |
|---|---|
| Username | `1001` |
| Password | the one you set above |
| Domain / host | `127.0.0.1` |
| Transport | **UDP** (Kamailio only listens on UDP) |
| STUN / ICE | off |
| Media encryption (SRTP/ZRTP) | off |
| DTMF | RFC 2833 (needed for IVR menus) |
| Local SIP port | anything but 5060 (random is fine) |

Dial a mapped number. You should hear the agent's greeting within about a
second.

## Verify without a softphone

These are useful after any change, and they're how the setup was debugged.

**Is Kamailio answering?** Send a SIP OPTIONS request; you should get `200 Keepalive`:

```bash
python3 - <<'EOF'
import socket, uuid
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); s.settimeout(3)
p, cid = s.getsockname()[1], uuid.uuid4().hex
s.sendto((f"OPTIONS sip:127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{p};branch=z9hG4bK{cid[:10]};rport\r\n"
          f"Max-Forwards: 70\r\nFrom: <sip:1001@127.0.0.1>;tag=a\r\nTo: <sip:127.0.0.1>\r\n"
          f"Call-ID: {cid}\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n").encode(), ("127.0.0.1", 5060))
print(s.recv(4000).decode().splitlines()[0])
EOF
```

**Does the agent's audio reach the caller?** Place a loopback call through
the real dialplan and record what the caller would hear:

```bash
FC="fs_cli -P 8022"      # macOS: "$(brew --prefix freeswitch)/bin/fs_cli -P 8022"
U=$($FC -x "originate {origination_caller_id_number=1001}loopback/5006/public &park" | sed -n 's/^+OK //p' | tr -d '\r\n ')
$FC -x "uuid_record $U start /tmp/agent.wav"; sleep 12
$FC -x "uuid_record $U stop all"; $FC -x "uuid_kill $U"
```

Then measure the loudness of each second: speech is in the thousands, silence
is near zero.

```bash
venv/bin/python3 -c "
import wave, numpy as np
w = wave.open('/tmp/agent.wav'); r = w.getframerate()
a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float)
print([int(np.sqrt(np.mean(a[i:i+r]**2))) for i in range(0, len(a)-r+1, r)])"
```

## Troubleshooting

Work through the call path in order: SIP, then the fork, then the agent, then
the audio coming back.

| Symptom | Cause | Fix |
|---|---|---|
| Softphone says "timeout" when registering | Packets never reach Kamailio. On macOS with a VPN, the default-route IP is the VPN's, which drops traffic to itself. | Use `SIP_IP=127.0.0.1` everywhere. Confirm with the OPTIONS check above. |
| Kamailio won't start: `could not find module <db_mysql>` or `<http_client>` | Excluded from the default Kamailio build | Build and copy both modules (see Kamailio above) |
| `failed to create runtime dir /var/run/kamailio` | Not running as root | `start_kamailio` already passes `-Y ~/.yuviz/kamailio/run` |
| 403 on register or call | Subscriber domain or digest doesn't match the listen IP | Recreate the extensions with the right domain, or rerun `update_kamailio_ip.sh` |
| FreeSWITCH SIP profile fails to bind | The `internal` profile is fighting Kamailio for 5060 | Disable `internal*.xml` (the setup does this) |
| Rings, answers, silence; FreeSWITCH log says `connection failed: closed before established` | Gateway not running on `:8080` | `start_gateway` |
| Agent hears you (Gateway logs `stt_final`) but you hear nothing; FreeSWITCH log says `received binary frame, discarding` | Unpatched `mod_audio_fork` | Rebuild with the patch, then restart FreeSWITCH |
| Same symptom, no "discarding" line | The Lua script parks with `session:sleep()`, which sends no audio frames for the patch to replace | Use the repo's `start_voice_ai.lua` (it plays `silence_stream://-1`) |
| No audio in either direction; the SDP shows a public IP | STUN left in `external_rtp_ip` | Set it to `$${local_ip_v4}` |
| The wrong agent answers, or the `default` agent | No route for the number, or the metadata frame arrived late or malformed | `redis-cli get did:<number>`; check the Gateway log for `Route resolved` |
| IVR menu ignores keypresses | Softphone isn't sending RFC 2833 | Set DTMF to RFC 2833 in the softphone |
| Agent can't hang up or transfer | ESL on `:8022` not reachable | Check `event_socket.conf.xml`; the Gateway log should show `EslEventListener: connected` |

Logs:

| Component | Location |
|---|---|
| FreeSWITCH (macOS) | `~/.yuviz/freeswitch/log/freeswitch.log` |
| FreeSWITCH (Linux) | `/var/log/freeswitch/freeswitch.log` |
| Kamailio | stderr of `start_kamailio`, or `~/.yuviz/logs/kamailio.log` |
| Gateway | stdout of `start_gateway` |

## Using your LAN IP instead

To call from a phone on your Wi-Fi, set `SIP_IP=auto` in `.env`, run
`start_kamailio`, and once run `install_network_sync`. Register the phone as
`1001@<lan-ip>`. Don't use a VPN address: with `auto`, a full-tunnel VPN
(`utun*`, `ppp*`, `ipsec*`) keeps the last applied LAN address instead of
rebinding onto the tunnel (and dropping calls).

With `auto`, SIP (5060, 5080) is open to everyone on that network. On a café
or hotspot Wi-Fi, use `127.0.0.1`.

**Changing networks.** `install_network_sync` adds a launchd agent that reruns
`update_kamailio_ip.sh` on every network change. It updates the config and
`.env`, and restarts Kamailio, FreeSWITCH, the Gateway and Campaigns. Log:
`~/.yuviz/logs/network-sync.log`. `remove_network_sync` uninstalls it.

Transfers and campaigns dial through Kamailio at `SIP_PROXY_HOST`, which the
script writes. While it is blank, the Gateway and Campaigns refuse to dial
numbers instead of sending calls to a wrong host.

**Restarting by hand:** use a new terminal tab. An old tab keeps the
`SIP_PROXY_HOST` it exported, and `start_gateway` warns about it.
