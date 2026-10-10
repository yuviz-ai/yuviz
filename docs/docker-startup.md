# Docker Startup Guide

Everything needed to run this project as a local development stack. Docker is
the only prerequisite — no Python, Node, Postgres, Redis or Ollama install
required on the host.

Works on **Linux, macOS and Windows**. Verified end-to-end on Linux
(Arch, x86_64, Docker 29.6, Compose 5.3); the macOS and Windows paths are
written but not yet run on those platforms.

---

## 1. Quick start

```bash
git clone https://github.com/yuviz-ai/yuviz.git
cd yuviz
./deployment/sh/dev.sh
```

Then open <http://localhost:3000>. Startup seeds the platform superadmin
(`SUPERADMIN_EMAIL`, default `superadmin@yuviz.ai`) only if none exists. Its
password is `SUPERADMIN_PASSWORD` from `deployment/.env`; if that is blank there
is no default — set one with **Forgot password?** on the Sign In page (the code
lands in Mailpit in dev). Restarts never reset the email or password.

**Create account** on the login page is open to anyone: it creates a new
organization with the registrant as its **admin**, never a superadmin. Email
sign-up first emails a 6-digit code (`POST /auth/register`, then
`POST /auth/verify-email`); nothing is created until the code is entered.
Google sign-up skips the code — Google has already verified the address.
Admins then invite their own users.

Outgoing email goes to the bundled **Mailpit** container by default — open
<http://localhost:8025> to read verification codes and invites. To send real
mail, set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`
and `SMTP_STARTTLS=true` in `deployment/.env`.

Select the `default` agent → click **Test Agent** → allow microphone → talk.

First run takes **10–30 minutes** (image build + ~3 GB of models).
Every run after that takes **~75 seconds**.

Short on CPU or disk? `--no-llm`, `--no-stt` and `--no-tts` leave out any of
the local models — see [Turning off LLM, STT or TTS](#turning-off-llm-stt-or-tts).

---

## 2. Requirements

### Hardware

| | Minimum | Recommended |
|---|---|---|
| RAM | 8 GB | 16 GB |
| Free disk | 16 GB | 25 GB+ |
| CPU | 4 cores | 8+ cores |

The stack idles at **~4.9 GB RAM**. `dev.sh` refuses to start below **16 GB
free disk** rather than failing halfway through a download — the images and
models come to ~15.6 GB (see §4).

These are the figures for the full stack. The three local models are most of
it, and any of them can be left out — see
[Turning off LLM, STT or TTS](#turning-off-llm-stt-or-tts). `--no-llm` alone
drops ~8.3 GB of disk and the bulk of the CPU load.

No GPU is needed. In the default containerized mode everything runs on CPU,
including a CPU-only build of PyTorch, and a host GPU is not used even when
present. The one exception is `USE_HOST_OLLAMA=1` (see §8), which runs the LLM
on the host and can use its GPU; STT and TTS stay on CPU either way.

### Software

| | Minimum version | Why |
|---|---|---|
| Docker Engine | 20.10 | — |
| Docker Compose | **2.20** | Needs `depends_on` conditions and `service_completed_successfully`. Compose **v1 (`docker-compose`) will not work.** |
| bash | 3.2 | Supplied by Git Bash or WSL2 on Windows — see §2.1 |

Check yours:

```bash
./deployment/sh/dev.sh --version
```

### Installing Docker

**You don't have to do this by hand.** Run `./deployment/sh/dev.sh` with no
container runtime installed and it offers to install one for you:

```text
✗ docker not found

  No container runtime found. This stack needs one — nothing else.

  1) Docker Desktop
     Official app with a GUI. Bundles Compose. Free for personal use and
     small companies, but needs a paid licence at 250+ employees or
     $10M+ annual revenue.

  2) Colima
     Open source, CLI only, no licence restrictions at any company size.
     Runs a small Linux VM. You size it yourself and start it per session.

  Install which? [1/2/n]
```

The wording adapts to your OS — on Linux, option 1 is Docker Engine, which runs
natively with no VM and no licence restrictions, and Colima is flagged as
unnecessary there.

Whichever you pick, the exact commands are printed and you confirm a second time
before anything runs — these need `sudo` (Linux) or Homebrew (macOS). Nothing is
installed silently. In a non-interactive shell such as CI the script prints the
options and exits `1` rather than waiting on input.

After installing, the script stops and asks you to re-run it. That is
deliberate: on Linux the new `docker` group does not apply to your current
shell until you log out and back in, and on macOS Docker Desktop has to be
opened once to start its daemon.

Colima is started as `colima start --cpu 4 --memory 10 --disk 60`. Its defaults
(2 CPU / 2 GB) are too small — this stack idles at ~4.9 GB. One caveat: Colima's
disk lives inside its VM, so the free-space check in phase 1 reads your *host*
disk and can pass while the VM is full. The same limitation applies to Docker
Desktop on macOS.

#### Doing it manually

**Linux (Debian/Ubuntu)**
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER      # then log out and back in
sudo systemctl enable --now docker
```

**Linux (Arch)**
```bash
sudo pacman -S docker docker-compose
sudo systemctl enable --now docker
sudo usermod -aG docker $USER      # then log out and back in
```

**macOS** — Docker Desktop, or Colima:
```bash
brew install --cask docker
# or
brew install colima docker docker-compose && colima start --cpu 4 --memory 10 --disk 60
```

**Windows** — install Docker Desktop with the WSL2 backend. Colima does not run
on Windows. See §2.1 for which shell to use.

### 2.1 Windows

Every shell works, but through different entry points:

| Shell | Command |
|---|---|
| PowerShell | `.\deployment\sh\dev.ps1` |
| Command Prompt | `deployment\sh\dev.cmd` |
| Git Bash | `./deployment/sh/dev.sh` |
| WSL2 | `./deployment/sh/dev.sh` |

`dev.cmd` and `dev.ps1` are thin wrappers — they locate Git Bash (preferred) or
WSL and re-invoke `dev.sh`. All logic lives in the one bash script, so the
wrappers cannot drift out of sync with it. Flags pass straight through:
`.\deployment\sh\dev.ps1 --version` works exactly like the bash form.

In **VS Code**, the integrated terminal defaults to PowerShell, which is fine
with `dev.ps1`. To use the bash form instead: `Ctrl+Shift+P` →
*Terminal: Select Default Profile* → **Git Bash** or **WSL**.

Three Windows-specific things that will otherwise cost you time:

- **Enable WSL integration in Docker Desktop** — Settings → Resources → WSL
  Integration → toggle your distro on. Without it `docker` is invisible inside
  WSL even though Docker Desktop is running.
- **Clone inside the WSL filesystem** (`~/yuviz`), not `/mnt/c/Users/...`.
  Building across the Windows↔Linux filesystem boundary is dramatically slower.
  This does not apply to Git Bash, which uses the Windows filesystem natively.
- **Line endings are handled for you.** `.gitattributes` pins `.sh`, Dockerfiles
  and YAML to LF, and `.cmd`/`.ps1` to CRLF. Without it, Windows git's default
  `core.autocrlf=true` rewrites `dev.sh` with CRLF and bash fails with
  `bad interpreter: /usr/bin/env bash^M`. If you hit that, your checkout
  predates the `.gitattributes` — re-clone, or run
  `git add --renormalize . && git checkout -- .`

---

## 3. What actually runs — 8 containers

| # | Service | Image | Port | RAM (idle) | Purpose |
|---|---|---|---|---|---|
| 1 | `conversation` | `yuviz-python:dev` | 50051 | **2.05 GB** | The STT→LLM→TTS pipeline. Holds whisper + kokoro in memory. gRPC. |
| 2 | `ollama` | `ollama/ollama` | 11434 | **2.01 GB** | LLM server, `llama3.2` resident |
| 3 | `admin-ui` | `yuviz-adminui:dev` | 3000 | **665 MB** | Next.js dev server, hot reload |
| 4 | `config` | `yuviz-python:dev` | 8000 | 52 MB | REST API — auth, tenants, agents, provider config |
| 5 | `knowledge` | `yuviz-python:dev` | 8100 | 50 MB | RAG documents, pgvector-backed |
| 6 | `postgres` | `pgvector/pgvector:pg14` | 5432 | 31 MB | Database + vector store |
| 7 | `webcall` | `yuviz-python:dev` | 8300 | 22 MB | Bridges the browser WebSocket to conversation's gRPC |
| 8 | `redis` | `redis:7-alpine` | 6379 | 3 MB | Cache and pub/sub |
| | | | **Total** | **≈ 4.9 GB** | |

Plus a 9th short-lived container, `init`, which applies schemas, creates the
internal service account and seeds the default agent, then exits. It creates
no admin login — that is done once from the UI (see §1). Everything else waits
for it to finish successfully.

Three services account for 96% of the memory. The other five are nearly free.

**Four services share one image.** `config`, `knowledge`, `conversation` and
`webcall` all run from `yuviz-python:dev` with different `command:` entries —
they have identical dependencies, so four separate images would mean four
copies of PyTorch.

### Ports

`3000` admin UI · `5432` postgres · `6379` redis · `8000` config ·
`8100` knowledge · `8300` webcall · `11434` ollama · `50051` conversation

All eight bind to **127.0.0.1** by default. Postgres ships with default
credentials, and **redis, ollama and the conversation gRPC port have no
authentication at all** — no password exists to set. Anyone who can reach the
host on those ports has full read/write access.

`BIND_ADDR=0.0.0.0` is therefore not something a stronger password makes safe.
Only set it behind a private network, firewall, VPN or authenticated reverse
proxy that restricts who can reach these ports.

`dev.sh` checks the ports compose will publish, reading any `*_PORT` overrides
from `deployment/.env`. In the default containerized mode that is all eight;
with `USE_HOST_OLLAMA=1` it checks the seven compose ports and instead verifies
that a host Ollama is answering on 11434. With `--no-llm` nothing binds 11434,
so that port is neither checked nor published.

---

## 4. Disk usage

| Item | Size |
|---|---|
| `ollama/ollama` image | 6.27 GB |
| `yuviz-python:dev` image | 3.47 GB |
| `yuviz-adminui:dev` image | 2.29 GB |
| `pgvector/pgvector:pg14` image | 606 MB |
| `redis:7-alpine` image | 59 MB |
| Volume `ollama-models` (llama3.2) | 2.02 GB |
| Volume `hf-cache` (whisper + kokoro) | 815 MB |
| Volume `pgdata` | 54 MB |
| Volume `knowledge-docs` (uploads) | 0 B until used |
| **Total** | **≈ 15.6 GB** |

This is why `dev.sh` gates on 16 GB free. `--no-llm` skips both the Ollama
image and its model volume (~8.3 GB together); `--no-stt` / `--no-tts` skip
their share of `hf-cache`. The gate itself doesn't move — it is checked before
the flags could help — but the download does.

Docker also accumulates **build cache** (tens of GB over time). Reclaim it with
`docker builder prune` — the next build is slower but nothing is lost.

Four named volumes persist across restarts, so `--down` never re-downloads
models. Only `--clean` deletes them.

---

## 5. What `dev.sh` does

Seven phases, each labelled so the script never looks hung:

```
[1/7] Checking Docker...      versions, RAM, free disk, port conflicts
[2/7] Creating .env...        generates secrets, backfills new keys
[3/7] Building containers...  docker compose up -d --build
[4/7] Downloading models...   llama3.2, whisper, kokoro (skips cached/disabled)
[5/7] Waiting for services... polls each healthcheck
[6/7] Running verification... proves STT, LLM and TTS actually work
[7/7] Ready!                  prints the URL and login
```

Phase 6 matters more than it looks. A healthcheck only proves a process is
listening — during development the Conversation Service reported healthy while
TTS was one missing model away from failing on the first reply. So phase 6
synthesises a sentence with TTS, transcribes that same audio back with STT, and
sends a prompt to the LLM. If any leg fails, the run fails. A leg you have
turned off (below) is skipped rather than failed.

### Flags

```bash
./deployment/sh/dev.sh                 # start and verify
./deployment/sh/dev.sh --down          # stop, keep all data
./deployment/sh/dev.sh --clean         # stop and delete volumes (full reset)
./deployment/sh/dev.sh --logs          # tail all logs
./deployment/sh/dev.sh --no-llm        # run without the LLM
./deployment/sh/dev.sh --no-stt        # run without speech-to-text
./deployment/sh/dev.sh --no-tts        # run without text-to-speech
./deployment/sh/dev.sh --llm-model qwen2.5   # a different Ollama model
./deployment/sh/dev.sh --verbose       # full build/pull output
./deployment/sh/dev.sh --timeout 600   # slower machines or CI
./deployment/sh/dev.sh --version       # versions, for bug reports
```

Re-running is always safe. Existing models are skipped, `deployment/.env` is
never overwritten, and the database bootstrap is idempotent.

### Choosing the Ollama model

`--llm-model <tag>` picks which model to run; the default is `llama3.2`
(~2 GB). The chosen tag is pulled in phase 4, written to
`VOICEAI_LLM_MODEL` in `deployment/.env`, seeded onto the default agent's
provider row, and used by the phase-6 LLM check — so all four agree.

| Tag | Size |
|-----|------|
| `llama3.2` (default) | ~2 GB |
| `qwen2.5:3b` | ~1.9 GB |
| `gemma3:4b` | ~3.3 GB |
| `qwen2.5` (7b) | ~4.7 GB |

Any tag from [ollama.com/library](https://ollama.com/library) works — the
list above is what the Admin UI offers, not a limit. Switching later is a
re-run with the new flag; previously pulled models stay in the volume.

### Turning off LLM, STT or TTS

Everything runs by default. The three local models are the expensive part of
this stack — llama3.2 on CPU will saturate a laptop by itself — so any of them
can be left out. Nothing else changes: the same containers start, the Admin UI
works the same, and re-running without the flag brings the model back.

A flag skips the download and the startup warm-up. It does not reconfigure
the agent, so a leg that is still assigned to a local engine will fetch and
load that model on the first call that needs it — see "What a flag does not
do" below.

| Flag | Skips | Saves |
|------|-------|-------|
| `--no-llm` | Ollama container + llama3.2 | ~2 GB, and most of the CPU |
| `--no-stt` | Whisper (`faster-whisper`) | ~500 MB and its load time |
| `--no-tts` | Kokoro voice synthesis | ~313 MB and its load time |

Combine them: `--no-llm --no-stt --no-tts` runs the platform with no local
inference at all.

What a flag actually does:

- **The model is never downloaded** in phase 4.
- **Conversation Service doesn't load it at startup.** `dev.sh` writes
  `VOICEAI_ENABLE_STT` / `VOICEAI_ENABLE_TTS` into `deployment/.env`, and the
  service's startup warm-up reads them — otherwise skipping the download here
  would just move it to container start. Both default to `1`; a deployment that
  sets neither behaves exactly as it always has.
- **Phase 6 skips that leg** and prints `disabled` instead of a tick. STT is
  verified by transcribing the audio TTS just made, so `--no-tts` on its own
  leaves STT unverified too (it says so).
What a flag does **not** do:

- **It doesn't reconfigure the agent.** `scripts/seed_default_config.py` still
  seeds `faster_whisper` and `kokoro` provider rows and assigns them to the
  default agent, so pressing "Test Agent" after `--no-stt` downloads Whisper
  during the call — the cost moves out of startup, it doesn't disappear.
  Repoint the agent at a cloud provider under **AI & Voice** to actually avoid
  it. `dev.sh` says as much in its closing output.

- **`--no-llm` stops a running Ollama.** Leaving the compose profile out only
  means "don't start it" — a container from an earlier run would keep going,
  burning the CPU the flag exists to give back. Its model volume is untouched.

With `--no-llm` there is no local model to talk to. Add a cloud provider key
under **AI & Voice** in the Admin UI and point the agent at it — for most work
past the first look around, that is the better setup anyway.

---

## 6. File layout

```
deployment/
├── .env.example                 committed template
├── .env                         generated, gitignored, holds secrets
├── docker/
│   ├── docker-compose.yml       8 services, healthchecks, ordering
│   ├── Dockerfile.python        shared image for the 4 Python services
│   └── Dockerfile.adminui       Next.js dev image
└── sh/
    ├── dev.sh                   entry point — all logic lives here
    ├── dev.ps1                  PowerShell wrapper → dev.sh
    ├── dev.cmd                  Command Prompt wrapper → dev.ps1
    └── init.sh                  schemas → service account → seed agent
```

`.dockerignore` stays at the repo root — Docker only reads it from the build
context root.

---

## 7. Configuration

Everything lives in `deployment/.env`, generated on first run.

| Variable | Default | Notes |
|---|---|---|
| `CONFIG_SERVICE_PASSWORD` | random | Internal service account, not a UI login |
| `JWT_SECRET` | random | Signs Config Service tokens |
| `POSTGRES_DSN` | built by `dev.sh` from the generated `POSTGRES_PASSWORD` | Host is the compose service name |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Set automatically from `USE_HOST_OLLAMA` |
| `BIND_ADDR` | `127.0.0.1` | Host interface for every published port |
| `VOICEAI_STT_MODEL` | `small` | Used by both conversation and the seed — they must agree |
| `VOICEAI_ENABLE_STT` | `1` | `0` keeps Whisper out of startup. Written by `--no-stt` |
| `VOICEAI_ENABLE_TTS` | `1` | `0` keeps Kokoro out of startup. Written by `--no-tts` |
| `*_PORT` (×8) | see §3 | Override on collision |
| `SPACY_MODEL_URL` | empty | Mirror for the spaCy model if github.com is unreachable |

Three settings are pinned in `docker-compose.yml` instead, because a wrong
value breaks the stack **silently**:

- `VOICEAI_TTS_ENGINE=kokoro` — the code defaults to `macos`, which shells out
  to the macOS `say` binary and cannot work in a Linux container.
- `VOICEAI_TTS_VOICE=af_sarah` — the code defaults to `Samantha`, also a macOS
  voice. Kokoro returns 404 for it and the agent never speaks.
- `CONVERSATION_SVC_TARGET=conversation:50051` — webcall otherwise defaults to
  Envoy on `:10000`, which this stack does not run.

**Security note:** the Admin UI account is yours, chosen at first run — no
credential for it ships in this repo. Postgres still defaults to
`voiceai`/`voiceai`, so change `POSTGRES_PASSWORD` and `POSTGRES_DSN` before
this stack leaves your machine.

Credentials alone are not enough to expose it, though: redis, ollama and the
conversation gRPC port have no authentication to configure. Keep `BIND_ADDR` at
`127.0.0.1` unless a firewall, VPN or authenticated proxy controls access to
those ports.

---

## 8. Using a host Ollama (Mac GPU)

Containers cannot reach Apple Metal, so containerized Ollama runs the LLM on
CPU inside a VM — noticeably slow on a Mac. To use the host's Ollama instead:

```bash
ollama serve                       # in another terminal
USE_HOST_OLLAMA=1 ./deployment/sh/dev.sh
```

The stack then skips its own Ollama container and points every service at
`host.docker.internal:11434`. `dev.sh` fails early with a clear message if
nothing is listening there.

---

## 9. Troubleshooting

**"docker not found"** — the script offers to install Docker or Colima; pick
one and confirm, or install manually (§2). In CI it exits `1` instead of
prompting.

**"the Docker daemon is not running"** — `sudo systemctl start docker` on Linux,
or open Docker Desktop. With Colima: `colima start`.

**"Compose 1.x is too old"** — you have `docker-compose` v1. This stack needs
Compose v2 (`docker compose`, no hyphen). Upgrade Docker. On Colima, install it
separately: `brew install docker-compose`.

**"only N GB free — need at least 16 GB"** — free space, or run
`docker system prune -a` (removes unused images from *all* projects) or
`docker builder prune` (build cache only, safer). On Colima or Docker Desktop
this check reads your host disk, not the VM's — if it passes but the build still
runs out of space, grow the VM.

**"port NNNN in use"** — a local Postgres/Redis/dev server is running. Stop it,
or override that `*_PORT` in `deployment/.env`.

**A service never becomes healthy** — the error names the service and its log
command. `conversation` is the slowest by far; on a first run it loads whisper
and kokoro before serving. Retry with `--timeout 600`.

**Agent transcribes your speech but never replies** — TTS is failing. Check
`./deployment/sh/dev.sh --logs` for a 404 on a voice file; the engine and voice
settings must both be kokoro-compatible (see §7).

**Model download stalls or reports "outgoing traffic disabled"** — that is
`huggingface_hub`'s misleading message for a *connection failure*. Retry.
On IPv6-only networks, IPv4-only hosts (github.com, HF's CDN) need working
DNS64; test with `getent ahosts github.com`.

**Edits to `init.sh` seem ignored** — it is baked into the image via
`COPY . /app`. `dev.sh` always rebuilds, but a manual `docker compose up init`
does not.

**Forgot the admin password** — use **Forgot password?** on the Sign In page;
it emails a 6-digit code (needs working SMTP — in dev, read it in Mailpit).
Without SMTP there is no default credential to fall back on: create another
superadmin from the host with
`POSTGRES_DSN="postgresql://voiceai:$(grep ^POSTGRES_PASSWORD= deployment/.env | cut -d= -f2)@127.0.0.1:5432/voiceai" python3
scripts/create_superadmin.py <email> <password>` — from the host, use
`127.0.0.1`, not the compose service name `postgres`. Or wipe and start over
with `./deployment/sh/dev.sh --clean`, which brings the setup screen back.

**The setup screen appears again unexpectedly** — it is shown whenever the
`users` table has no live superadmin row, so a wiped volume or a deleted last
superadmin brings it back.

---

## 10. Scope

**Included:** the browser-testing path — real STT → LLM → TTS calls from
`admin-ui`'s Test Agent panel, with barge-in.

**Not included:** real telephony. Kamailio SIP routing, FreeSWITCH media, the
C++ Gateway and the `campaigns`/`did`/`vobiz` services are not part of this
stack. Those are system-level installs with local config outside version
control — see the main [README](../README.md).

Also out of scope: GPU/CUDA support, production images (this runs `next dev`
with hot reload), and the knowledge ingestion worker (only the API is started).

For a native install without Docker, see [setup.md](setup.md) — more moving
parts, and only ever verified on macOS.
