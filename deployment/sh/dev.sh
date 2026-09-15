#!/usr/bin/env bash
# One-command local dev stack.
#
#   ./deployment/sh/dev.sh              bring up and verify
#   ./deployment/sh/dev.sh --down       stop, keep data
#   ./deployment/sh/dev.sh --clean      stop and wipe volumes
#   ./deployment/sh/dev.sh --logs       tail logs
#   ./deployment/sh/dev.sh --no-llm     skip the LLM (no ollama container)
#   ./deployment/sh/dev.sh --no-stt     skip speech-to-text (no whisper)
#   ./deployment/sh/dev.sh --no-tts     skip text-to-speech (no kokoro)
#   ./deployment/sh/dev.sh --llm-model M  Ollama model (default llama3.2)
#   ./deployment/sh/dev.sh --verbose    full build output
#   ./deployment/sh/dev.sh --timeout N  health budget (default 300s)
#   ./deployment/sh/dev.sh --version    versions, for bug reports
#
# Docker is the only prerequisite. On Windows use WSL2 or Git Bash.
set -Eeuo pipefail

DEV_SH_VERSION="1.0.0"
MIN_DOCKER="20.10"
MIN_COMPOSE="2.20"
TOTAL_PHASES=7

TIMEOUT=300
VERBOSE=0
# Everything on by default; --no-llm/--no-stt/--no-tts turn a leg off. Local
# inference is what makes this stack heavy — llama3.2 alone will saturate a
# laptop CPU — and not every kind of work needs all three running. Turning
# one off skips its model download, keeps its container/model out of the
# run, and drops it from the verification.
WANT_LLM=1
WANT_STT=1
WANT_TTS=1
# Which Ollama model to pull, seed and verify with. Any tag from
# ollama.com/library works; the Admin UI can point an agent at a different
# one afterwards, but only this one is downloaded here.
OLLAMA_MODEL="llama3.2"
ACTION="up"
CURRENT_PHASE="startup"
STARTED_WORK=0

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE_FILE="$ROOT/deployment/docker/docker-compose.yml"
ENV_FILE="$ROOT/deployment/.env"
ENV_EXAMPLE="$ROOT/deployment/.env.example"
cd "$ROOT"

if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

phase()  { CURRENT_PHASE="$1/$TOTAL_PHASES $2"; printf '\n%s[%s/%s] %s%s\n' "$BOLD" "$1" "$TOTAL_PHASES" "$2" "$RESET"; }
ok()     { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
info()   { printf '  %s\n' "$1"; }
dim()    { printf '  %s%s%s\n' "$DIM" "$1" "$RESET"; }
warn()   { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
fail()   { printf '\n%s✗ %s%s\n' "$RED" "$1" "$RESET" >&2; }

run_quiet() {
    if [ "$VERBOSE" = "1" ]; then
        "$@"
    else
        local log; log=$(mktemp)
        if "$@" >"$log" 2>&1; then
            rm -f "$log"
        else
            local rc=$?
            tail -30 "$log" >&2
            rm -f "$log"
            return $rc
        fi
    fi
}

cleanup_hint() {
    cat >&2 <<EOF

  Logs:
    ./deployment/sh/dev.sh --logs

  Reset and start over:
    ./deployment/sh/dev.sh --clean && ./deployment/sh/dev.sh
EOF
}

on_err() {
    local rc=$?
    trap - ERR INT TERM
    fail "Startup failed at [$CURRENT_PHASE] (exit $rc)"
    [ "$STARTED_WORK" = "1" ] && cleanup_hint
    exit "$rc"
}

on_int() {
    trap - ERR INT TERM
    printf '\n\n%s^C Interrupted during [%s].%s\n' "$YELLOW" "$CURRENT_PHASE" "$RESET" >&2
    if [ "$STARTED_WORK" = "1" ]; then
        echo "   Containers left running; downloads are cached so re-running resumes." >&2
    fi
    exit 130
}

trap on_err ERR
trap on_int INT TERM

while [ $# -gt 0 ]; do
    case "$1" in
        --down)    ACTION="down" ;;
        --clean)   ACTION="clean" ;;
        --logs)    ACTION="logs" ;;
        --no-llm)  WANT_LLM=0 ;;
        --no-stt)  WANT_STT=0 ;;
        --no-tts)  WANT_TTS=0 ;;
        --llm-model) OLLAMA_MODEL="${2:?--llm-model needs a model name, e.g. qwen2.5}"; shift ;;
        --verbose) VERBOSE=1 ;;
        --timeout) TIMEOUT="${2:?--timeout needs a value in seconds}"; shift ;;
        --version) ACTION="version" ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) fail "unknown flag: $1 (try --help)"; exit 2 ;;
    esac
    shift
done

have_docker() { command -v docker >/dev/null 2>&1; }

compose() {
    local args=(-f "$COMPOSE_FILE" --project-directory "$ROOT")
    [ -f "$ENV_FILE" ] && args+=(--env-file "$ENV_FILE")
    docker compose "${args[@]}" "$@"
}

if [ "$ACTION" = "version" ]; then
    printf 'dev.sh          %s\n' "$DEV_SH_VERSION"
    printf 'requires        Docker Engine >= %s, Compose >= %s\n' "$MIN_DOCKER" "$MIN_COMPOSE"
    if have_docker; then
        printf 'docker          %s\n' "$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'not running')"
        printf 'docker compose  %s\n' "$(docker compose version --short 2>/dev/null || echo 'not found')"
    else
        printf 'docker          not installed\n'
    fi
    exit 0
fi

runtime_options() {
    local os; os=$(uname -s)
    cat <<EOF

  ${BOLD}No container runtime found.${RESET} This stack needs one — nothing else.

  ${BOLD}1) Docker${RESET}$([ "$os" = "Darwin" ] && echo " Desktop" || echo " Engine")
     $([ "$os" = "Darwin" ] \
       && echo "Official app with a GUI. Bundles Compose. Free for personal use and
     small companies, but needs a paid licence at 250+ employees or
     \$10M+ annual revenue." \
       || echo "Runs natively, no VM, no licence restrictions. The normal choice
     on Linux.")

  ${BOLD}2) Colima${RESET}
     $([ "$os" = "Darwin" ] \
       && echo "Open source, CLI only, no licence restrictions at any company size.
     Runs a small Linux VM. You size it yourself and start it per session." \
       || echo "Open source, runs Docker inside a VM. On Linux this adds a VM you
     do not need — pick 1 unless you have a specific reason.")

EOF
}

# Prints every step, takes one confirmation, then runs them in order.
run_steps() {
    local step
    info "Will run:"
    for step in "$@"; do dim "$step"; done
    printf '\n  Continue? [y/N] '; read -r yn
    case "$yn" in [Yy]*) ;; *) info "Cancelled."; return 1 ;; esac
    for step in "$@"; do
        bash -c "$step" || { fail "failed: $step"; return 1; }
    done
}

# Distro packages, not `curl https://get.docker.com | sudo sh`. Piping a remote
# response straight into a root shell means a compromised endpoint owns the
# machine; these paths verify signatures through the package manager instead.
install_docker_linux() {
    local id; id=$(. /etc/os-release 2>/dev/null && echo "${ID:-}")
    if command -v pacman >/dev/null 2>&1; then
        run_steps \
            "sudo pacman -S --needed --noconfirm docker docker-compose" \
            "sudo systemctl enable --now docker" \
            "sudo usermod -aG docker $USER" || return 1
    elif command -v apt-get >/dev/null 2>&1; then
        case "$id" in ubuntu|debian) ;; *) id=debian ;; esac
        run_steps \
            "sudo apt-get update" \
            "sudo apt-get install -y ca-certificates curl gnupg" \
            "sudo install -m 0755 -d /etc/apt/keyrings" \
            "curl -fsSL https://download.docker.com/linux/$id/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg" \
            "sudo chmod a+r /etc/apt/keyrings/docker.gpg" \
            "echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$id \$(. /etc/os-release && echo \\\$VERSION_CODENAME) stable\" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null" \
            "sudo apt-get update" \
            "sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin" \
            "sudo systemctl enable --now docker" \
            "sudo usermod -aG docker $USER" || return 1
    elif command -v dnf >/dev/null 2>&1; then
        run_steps \
            "sudo dnf -y install dnf-plugins-core" \
            "sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo" \
            "sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin" \
            "sudo systemctl enable --now docker" \
            "sudo usermod -aG docker $USER" || return 1
    else
        fail "No supported package manager found (pacman, apt-get, dnf)."
        info "Install Docker Engine for your distro: https://docs.docker.com/engine/install/"
        return 1
    fi
    ok "Docker installed"
    warn "Log out and back in (group membership), then re-run this script."
}

install_docker() {
    case "$(uname -s)" in
        Linux) install_docker_linux || return 1 ;;
        Darwin)
            command -v brew >/dev/null 2>&1 || {
                fail "Homebrew not found. Install it first: https://brew.sh"
                return 1
            }
            run_steps "brew install --cask docker" || return 1
            ok "Docker Desktop installed"
            warn "Open Docker Desktop once to start the daemon, then re-run this script."
            ;;
        *)
            fail "Automatic install is not supported here. See https://docs.docker.com/get-docker/"
            return 1
            ;;
    esac
}

install_colima() {
    command -v brew >/dev/null 2>&1 || {
        fail "Colima installs via Homebrew, which was not found. See https://brew.sh"
        return 1
    }
    # Defaults are 2 CPU / 2 GB, and this stack idles at ~4.9 GB.
    run_steps "brew install colima docker docker-compose" \
              "colima start --cpu 4 --memory 10 --disk 60" || return 1
    ok "Colima running"
    warn "Colima's disk lives inside its VM, so the free-space check below reads your host disk."
    info "Re-run this script to start the stack."
}

if ! have_docker; then
    fail "docker not found"
    runtime_options
    if [ ! -t 0 ]; then
        info "Not an interactive terminal — install one of the above, then re-run."
        exit 1
    fi
    printf '  Install which? [1/2/n] '; read -r choice
    case "$choice" in
        1) install_docker || exit 1 ;;
        2) install_colima || exit 1 ;;
        *) info "Nothing installed. See https://docs.docker.com/get-docker/"; exit 1 ;;
    esac
    exit 0
fi

case "$ACTION" in
    down)  info "Stopping (volumes kept)…"; compose --profile ollama-container down; ok "stopped"; exit 0 ;;
    clean) info "Stopping and deleting volumes…"; compose --profile ollama-container down -v; ok "cleaned"; exit 0 ;;
    logs)  exec compose --profile ollama-container logs -f --tail=100 ;;
esac

# ── [1/7] ─────────────────────────────────────────────────────────────────────
phase 1 "Checking Docker..."

version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

if ! docker info >/dev/null 2>&1; then
    fail "the Docker daemon is not running. Start Docker Desktop, or: sudo systemctl start docker"
    exit 1
fi

DOCKER_VER=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "0")
COMPOSE_VER=$(docker compose version --short 2>/dev/null || echo "")

if [ -z "$COMPOSE_VER" ]; then
    fail "Docker Compose v2 required — 'docker-compose' v1 cannot run this stack. Upgrade Docker."
    exit 1
fi
version_ge "$DOCKER_VER"  "$MIN_DOCKER"  || { fail "Docker $DOCKER_VER is too old (need >= $MIN_DOCKER)"; exit 1; }
version_ge "$COMPOSE_VER" "$MIN_COMPOSE" || { fail "Compose $COMPOSE_VER is too old (need >= $MIN_COMPOSE)"; exit 1; }

if [ "$WANT_LLM" = "0" ]; then
    # No local model at all. The URL still gets written so the seeded
    # provider row stays valid — point an agent at a cloud provider in the
    # Admin UI (AI & Voice) and nothing here has to change.
    OLLAMA_MODE="off"
    OLLAMA_URL="http://ollama:11434"
    COMPOSE_PROFILE=()
elif [ "${USE_HOST_OLLAMA:-0}" = "1" ]; then
    OLLAMA_MODE="host"
    OLLAMA_URL="http://host.docker.internal:11434"
    COMPOSE_PROFILE=()
else
    OLLAMA_MODE="container"
    OLLAMA_URL="http://ollama:11434"
    COMPOSE_PROFILE=(--profile ollama-container)
fi

case "$(uname -s)" in
    Linux)  RAM_H=$(awk '/MemTotal/{printf "%.0f GB", $2/1048576}' /proc/meminfo 2>/dev/null || echo unknown) ;;
    Darwin) RAM_H=$(sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.0f GB", $1/1073741824}' || echo unknown) ;;
    *)      RAM_H="unknown" ;;
esac

DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo "")
if [ -n "$DOCKER_ROOT" ] && [ -d "$DOCKER_ROOT" ]; then DF_TARGET="$DOCKER_ROOT"; else DF_TARGET="$ROOT"; fi
AVAIL_KB=$(df -Pk "$DF_TARGET" 2>/dev/null | awk 'NR==2{print $4}' || echo "")
if [ -n "$AVAIL_KB" ]; then AVAIL_GB=$(( AVAIL_KB / 1048576 )); DISK_H="${AVAIL_GB} GB"; else AVAIL_GB=""; DISK_H="unknown"; fi

printf '  %-11s %s\n' "Docker:"    "$DOCKER_VER"
printf '  %-11s %s\n' "Compose:"   "$COMPOSE_VER"
printf '  %-11s %s / %s\n' "OS/CPU:" "$(uname -s | tr '[:upper:]' '[:lower:]')" "$(uname -m)"
printf '  %-11s %s\n' "RAM:"       "$RAM_H"
printf '  %-11s %s\n' "Free disk:" "$DISK_H"
printf '  %-11s %s\n' "Ollama:"    "$OLLAMA_MODE"
[ "$WANT_LLM" = "1" ] && printf '  %-11s %s\n' "LLM model:" "$OLLAMA_MODEL"
off_list=""
[ "$WANT_LLM" = "0" ] && off_list="${off_list}LLM "
[ "$WANT_STT" = "0" ] && off_list="${off_list}STT "
[ "$WANT_TTS" = "0" ] && off_list="${off_list}TTS "
[ -n "$off_list" ] && printf '  %-11s %s\n' "Disabled:" "${off_list% }"

if [ -n "$AVAIL_GB" ] && [ "$AVAIL_GB" -lt 16 ]; then
    fail "only ${AVAIL_GB} GB free on ${DF_TARGET} — need at least 16 GB"
    echo "    Reclaim some with: docker system prune -a" >&2
    exit 1
fi

if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
if [ -z "$(compose "${COMPOSE_PROFILE[@]}" ps -q 2>/dev/null)" ]; then
    conflict=0
    check_port() {
        if port_busy "$1"; then
            printf '  %s✗%s port %s in use — %s\n' "$RED" "$RESET" "$1" "$2" >&2
            conflict=1
        fi
    }
    check_port "${ADMIN_UI_PORT:-3000}"      "set ADMIN_UI_PORT in deployment/.env"
    check_port "${CONFIG_PORT:-8000}"        "set CONFIG_PORT"
    check_port "${KNOWLEDGE_PORT:-8100}"     "set KNOWLEDGE_PORT"
    check_port "${WEBCALL_PORT:-8300}"       "set WEBCALL_PORT"
    check_port "${CONVERSATION_PORT:-50051}" "set CONVERSATION_PORT"
    check_port "${POSTGRES_PORT:-5432}"      "a local Postgres is running; stop it or set POSTGRES_PORT"
    check_port "${REDIS_PORT:-6379}"         "a local Redis is running; stop it or set REDIS_PORT"
    [ "$OLLAMA_MODE" = "container" ] && check_port "${OLLAMA_PORT:-11434}" "a host Ollama is running; use USE_HOST_OLLAMA=1"
    if [ "$conflict" = "1" ]; then
        fail "free the ports above, or override them in deployment/.env"
        exit 1
    fi
    ok "all required ports free"
else
    dim "stack already running — skipping port check"
fi

# ── [2/7] ─────────────────────────────────────────────────────────────────────
phase 2 "Creating .env..."

# head must be the reader, not the writer: `tr < /dev/urandom | head -c N` kills
# tr with SIGPIPE (exit 141) the moment head has enough, which trips the ERR trap.
rand() { head -c "$(( ${1:-32} * 3 ))" /dev/urandom | base64 | LC_ALL=C tr -cd 'A-Za-z0-9' | cut -c "1-${1:-32}"; }

if [ -f "$ENV_FILE" ]; then
    ok "deployment/.env exists (left untouched)"
    # Backfill keys added to .env.example since this .env was generated,
    # otherwise compose warns about unset variables after an update.
    while IFS='=' read -r key _; do
        case "$key" in ''|\#*) continue ;; esac
        grep -q "^${key}=" "$ENV_FILE" || {
            grep "^${key}=" "$ENV_EXAMPLE" >> "$ENV_FILE"
            ok "added missing key ${key}"
        }
    done < "$ENV_EXAMPLE"
else
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    ok "deployment/.env created"
fi

# Blank counts as missing: auth.py's os.environ.get returns "" rather than its
# fallback, so an empty JWT_SECRET silently becomes the signing key.
for secret in CONFIG_SERVICE_PASSWORD:32 JWT_SECRET:48 YUVIZ_APP_PASSWORD:32; do
    key=${secret%:*}; len=${secret#*:}
    if [ -n "$(grep "^${key}=" "$ENV_FILE" | cut -d= -f2-)" ]; then continue; fi
    value=$(rand "$len")
    if [ "${#value}" -ne "$len" ]; then
        fail "could not generate ${key} (got ${#value} of ${len} chars)"
        exit 1
    fi
    if ! sed -i.bak "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"; then
        fail "could not write ${key} to ${ENV_FILE}"
        exit 1
    fi
    rm -f "$ENV_FILE.bak"
    ok "generated ${key}"
done

# SECRET_ENCRYPTION_KEY can't go through the loop above: it is a Fernet key,
# which must be exactly 32 raw bytes in url-safe base64 (44 chars, trailing
# '='), and `rand` strips every non-alphanumeric character — so it would
# produce a string Fernet rejects. Without this, pasting a provider API key
# in the Admin UI raises SecretEncryptionUnavailable and the operator gets an
# opaque 500 on the headline setup step.
if [ -z "$(grep "^SECRET_ENCRYPTION_KEY=" "$ENV_FILE" | cut -d= -f2-)" ]; then
    fernet_key=$(head -c 32 /dev/urandom | base64 | LC_ALL=C tr '+/' '-_')
    if [ "${#fernet_key}" -ne 44 ]; then
        fail "could not generate SECRET_ENCRYPTION_KEY (got ${#fernet_key} of 44 chars)"
        exit 1
    fi
    if ! sed -i.bak "s|^SECRET_ENCRYPTION_KEY=.*|SECRET_ENCRYPTION_KEY=${fernet_key}|" "$ENV_FILE"; then
        fail "could not write SECRET_ENCRYPTION_KEY to ${ENV_FILE}"
        exit 1
    fi
    rm -f "$ENV_FILE.bak"
    ok "generated SECRET_ENCRYPTION_KEY"
fi

# Re-derived each run so toggling USE_HOST_OLLAMA needs no hand-editing.
if ! sed -i.bak "s|^OLLAMA_BASE_URL=.*|OLLAMA_BASE_URL=${OLLAMA_URL}|" "$ENV_FILE"; then
    fail "could not write OLLAMA_BASE_URL to ${ENV_FILE}"
    exit 1
fi
rm -f "$ENV_FILE.bak"
ok "ollama URL: ${OLLAMA_URL}"

# Conversation Service loads whisper and kokoro at boot (see _prewarm_agents)
# — downloading hundreds of MB and holding them in memory. Skipping the
# download here without telling it would just move the download to container
# start, so the choice has to reach the service itself.
set_env() {
    if grep -q "^$1=" "$ENV_FILE"; then
        sed -i.bak "s|^$1=.*|$1=$2|" "$ENV_FILE" || { fail "could not write $1 to ${ENV_FILE}"; exit 1; }
        rm -f "$ENV_FILE.bak"
    else
        printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
    fi
}
set_env VOICEAI_LLM_MODEL "$OLLAMA_MODEL"
set_env VOICEAI_ENABLE_STT "$WANT_STT"
set_env VOICEAI_ENABLE_TTS "$WANT_TTS"

set -a; . "$ENV_FILE"; set +a

if [ "$OLLAMA_MODE" = "host" ] && ! curl -fsS -m 5 http://localhost:11434/api/tags >/dev/null 2>&1; then
    fail "USE_HOST_OLLAMA=1 but nothing answers on localhost:11434 — run: ollama serve"
    exit 1
fi

# ── [3/7] ─────────────────────────────────────────────────────────────────────
phase 3 "Building containers..."
STARTED_WORK=1
dim "first run installs Python deps — several minutes"
if [ "$OLLAMA_MODE" = "off" ]; then
    # Leaving the profile out only means "don't start it" — an ollama left
    # over from an earlier run without --no-llm keeps running, and burning
    # the CPU this flag exists to give back. Its model volume is untouched,
    # so dropping the flag later costs nothing.
    if [ -n "$(compose --profile ollama-container ps -aq ollama 2>/dev/null)" ]; then
        run_quiet compose --profile ollama-container rm -f -s ollama
        ok "ollama stopped (models kept)"
    fi
fi
run_quiet compose "${COMPOSE_PROFILE[@]}" up -d --build
ok "containers started"

# ── [4/7] ─────────────────────────────────────────────────────────────────────
phase 4 "Downloading models..."

case "$OLLAMA_MODEL" in *:*) WANT_TAG="$OLLAMA_MODEL" ;; *) WANT_TAG="${OLLAMA_MODEL}:latest" ;; esac

if [ "$OLLAMA_MODE" = "off" ]; then
    ok "LLM disabled — skipping ${OLLAMA_MODEL} (and the CPU it would burn)"
elif [ "$OLLAMA_MODE" = "container" ]; then
    deadline=$(( $(date +%s) + TIMEOUT ))
    until compose "${COMPOSE_PROFILE[@]}" exec -T ollama ollama list >/dev/null 2>&1; do
        [ "$(date +%s)" -ge "$deadline" ] && { fail "ollama did not come up within ${TIMEOUT}s"; cleanup_hint; exit 1; }
        sleep 3
    done
    if compose "${COMPOSE_PROFILE[@]}" exec -T ollama ollama list 2>/dev/null | awk '{print $1}' | grep -qxF "$WANT_TAG"; then
        ok "${OLLAMA_MODEL} already present, skipping"
    else
        dim "pulling ${OLLAMA_MODEL} (first run only)"
        run_quiet compose "${COMPOSE_PROFILE[@]}" exec -T ollama ollama pull "$OLLAMA_MODEL"
        ok "${OLLAMA_MODEL} pulled"
    fi
else
    if curl -fsS -m 10 http://localhost:11434/api/tags 2>/dev/null | grep -qF "\"$WANT_TAG\""; then
        ok "${OLLAMA_MODEL} already present on host, skipping"
    else
        dim "pulling ${OLLAMA_MODEL} on the host"
        ollama pull "$OLLAMA_MODEL"
        ok "${OLLAMA_MODEL} pulled"
    fi
fi

# Test for the weight files, not the directory: an interrupted download leaves
# the folder behind with only metadata in it, and treating that as "cached"
# skips the real download and fails later with IncompleteSnapshotError.
cached() { compose "${COMPOSE_PROFILE[@]}" exec -T conversation sh -c "ls $1 >/dev/null 2>&1" 2>/dev/null; }

if [ "$WANT_TTS" = "0" ]; then
    ok "TTS disabled — skipping kokoro (~313 MB)"
elif cached '/root/.cache/huggingface/hub/models--hexgrad--Kokoro-82M/snapshots/*/kokoro-v1_0.pth'; then
    ok "kokoro weights cached, skipping"
else
    dim "kokoro weights (~313 MB) download during startup"
fi

# Pull whisper here rather than letting it happen lazily at first transcribe:
# a flaky network then surfaces as a confusing verification failure, and
# huggingface_hub reports connection errors as "outgoing traffic disabled".
if [ "$WANT_STT" = "0" ]; then
    ok "STT disabled — skipping whisper (~500 MB)"
elif cached '/root/.cache/huggingface/hub/models--*faster-whisper*/snapshots/*/model.bin'; then
    ok "whisper weights cached, skipping"
else
    dim "pulling whisper (~500 MB, first run only)"
    whisper_pulled=0
    for _ in 1 2 3; do
        if run_quiet compose "${COMPOSE_PROFILE[@]}" exec -T conversation python -c \
            "import os;from faster_whisper.utils import download_model;download_model(os.environ.get('VOICEAI_STT_MODEL','small.en'))"; then
            whisper_pulled=1; break
        fi
        dim "download interrupted, retrying"
    done
    [ "$whisper_pulled" = "1" ] || { fail "whisper model download failed after 3 attempts (network?)"; cleanup_hint; exit 1; }
    ok "whisper pulled"
fi

# ── [5/7] ─────────────────────────────────────────────────────────────────────
phase 5 "Waiting for services..."

wait_healthy() {
    local svc="$1" deadline=$(( $(date +%s) + TIMEOUT )) cid status
    while :; do
        cid=$(compose "${COMPOSE_PROFILE[@]}" ps -q "$svc" 2>/dev/null || true)
        if [ -n "$cid" ]; then
            status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo "")
            case "$status" in
                healthy|running) [ "$status" = "healthy" ] && { ok "$svc healthy"; return 0; } ;;
                exited|dead)
                    fail "$svc exited during startup"
                    echo "    docker compose -f $COMPOSE_FILE logs $svc" >&2
                    return 1 ;;
            esac
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            fail "$svc failed to become healthy after ${TIMEOUT}s"
            echo "    docker compose -f $COMPOSE_FILE logs $svc" >&2
            echo "    slow machine or first-run download? retry with: --timeout $(( TIMEOUT * 2 ))" >&2
            return 1
        fi
        sleep 5
    done
}

if [ "$WANT_STT" = "1" ] && [ "$WANT_TTS" = "1" ]; then
    dim "conversation loads its speech models before serving — slowest on first run"
fi
for svc in postgres redis config knowledge conversation webcall admin-ui; do
    wait_healthy "$svc" || { cleanup_hint; exit 1; }
done

# ── [6/7] ─────────────────────────────────────────────────────────────────────
phase 6 "Running verification..."

# Health endpoints only prove a process is listening. Exercise the legs that
# are actually running: TTS makes audio, STT reads that same audio back, the
# LLM answers a prompt. A disabled leg is skipped rather than failed — and
# STT rides on TTS's output, so it needs both.
VERIFY_OUT=$(compose "${COMPOSE_PROFILE[@]}" exec -T \
    -e WANT_LLM="$WANT_LLM" -e WANT_STT="$WANT_STT" -e WANT_TTS="$WANT_TTS" \
    -e OLLAMA_MODEL="$OLLAMA_MODEL" \
    conversation python - <<'PY' 2>&1
import asyncio, os, sys

async def main():
    # Imported lazily: importing kokoro/faster_whisper pulls in torch and
    # ctranslate2, which is exactly the cost --no-tts / --no-stt is avoiding.
    import httpx

    want = lambda leg: os.environ.get(f"WANT_{leg}", "1") == "1"
    phrase = "The quick brown fox jumps over the lazy dog."
    pcm = b""

    if want("TTS"):
        from services.conversation.providers.tts.kokoro import KokoroTTS
        # Use the configured voice, not a hardcoded one: a voice the engine cannot
        # load leaves the agent permanently silent, and hardcoding hides exactly that.
        tts = KokoroTTS(voice=os.environ.get("VOICEAI_TTS_VOICE", "af_heart"), speed=1.0)
        pcm = b"".join([c async for c in tts.synthesize_stream(phrase, 16000)])
        if not pcm:
            print("FAIL tts produced no audio"); sys.exit(1)
        print(f"OK tts {len(pcm)} bytes ({len(pcm)/2/16000:.1f}s)")
    else:
        print("SKIP tts disabled")

    if not want("STT"):
        print("SKIP stt disabled")
    elif not pcm:
        # Nothing to transcribe: the sample this check reads back is the one
        # TTS just made.
        print("SKIP stt no sample audio (TTS is disabled)")
    else:
        from services.conversation.providers.stt.faster_whisper import FasterWhisperSTT
        stt = FasterWhisperSTT(model_size=os.environ.get("VOICEAI_STT_MODEL", "small.en"))
        await stt.load()
        res = await stt.transcribe(pcm, 16000)
        text = (getattr(res, "text", "") or "").strip()
        if not text:
            print("FAIL stt returned an empty transcript"); sys.exit(1)
        print(f"OK stt {text!r}")

    if want("LLM"):
        url = os.environ.get("VOICEAI_LLM_URL", "http://ollama:11434").rstrip("/")
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{url}/api/generate", json={
                "model": os.environ.get("OLLAMA_MODEL", "llama3.2"),
                "prompt": "Say hello in three words.", "stream": False})
            r.raise_for_status()
            out = (r.json().get("response") or "").strip()
        if not out:
            print("FAIL llm returned an empty completion"); sys.exit(1)
        print(f"OK llm {out[:60]!r}")
    else:
        print("SKIP llm disabled")

asyncio.run(main())
PY
) || {
    fail "pipeline verification failed"
    printf '%s\n' "$VERIFY_OUT" | sed 's/^/    /' >&2
    cleanup_hint
    exit 1
}

# ${leg^^} would be cleaner but macOS still ships bash 3.2.
printf '%s\n' "$VERIFY_OUT" | grep -E '^(OK|SKIP) ' | while read -r verdict leg rest; do
    label="$(printf '%s' "$leg" | tr '[:lower:]' '[:upper:]')"
    if [ "$verdict" = "OK" ]; then ok "${label}: ${rest}"; else dim "${label}: ${rest}"; fi
done

# With no LLM there is no url to cross-check, so the seed check is just
# whether the row is there.
if [ "$OLLAMA_MODE" = "off" ]; then
    ok "database seeded"
else
    SEEDED_URL=$(compose "${COMPOSE_PROFILE[@]}" exec -T postgres \
        psql -U "${POSTGRES_USER:-voiceai}" -d "${POSTGRES_DB:-voiceai}" -tAc \
        "select extra->>'base_url' from provider_configs where engine='ollama' limit 1" 2>/dev/null | tr -d '[:space:]' || echo "")
    if [ -n "$SEEDED_URL" ] && [ "$SEEDED_URL" != "$OLLAMA_URL" ]; then
        warn "seeded LLM url is '$SEEDED_URL' but this run uses '$OLLAMA_URL' — reseed with --clean"
    else
        ok "database seeded (llm url: ${SEEDED_URL:-$OLLAMA_URL})"
    fi
fi

# ── [7/7] ─────────────────────────────────────────────────────────────────────
leg_line() {
    if [ "$2" = "1" ]; then
        printf '  %s✓%s %s verified' "$GREEN" "$RESET" "$1"
    else
        printf '  %s-%s %s disabled (--no-%s)' "$DIM" "$RESET" "$1" "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    fi
}
off_hint() {
    [ "$WANT_LLM" = "1" ] && [ "$WANT_STT" = "1" ] && [ "$WANT_TTS" = "1" ] && return 0
    printf '\n  %sA disabled leg is skipped here, not switched off in the agent.\n' "$DIM"
    [ "$WANT_LLM" = "0" ] && printf '  No local LLM is running: add a cloud provider key in the Admin UI\n  under AI & Voice, then point the agent at it.\n'
    { [ "$WANT_STT" = "0" ] || [ "$WANT_TTS" = "0" ]; } && printf '  The default agent still uses Whisper/Kokoro, so a test call downloads\n  that model mid-call unless you repoint it first.\n'
    printf '%s' "$RESET"
}

phase 7 "Ready!"
cat <<EOF

  ${GREEN}✓${RESET} All services healthy
  ${GREEN}✓${RESET} Models ready
  ${GREEN}✓${RESET} Database seeded
$(leg_line STT "$WANT_STT")
$(leg_line LLM "$WANT_LLM")
$(leg_line TTS "$WANT_TTS")
$(off_hint)
  ${BOLD}Open: http://localhost:3000${RESET}
  First run shows "Create your administrator account" — pick your own
  email and password there; this stack ships with no default login.
  Then select the "default" agent → Click "Test Agent"

  ${DIM}./deployment/sh/dev.sh --logs    tail logs
  ./deployment/sh/dev.sh --down    stop
  ./deployment/sh/dev.sh --clean   stop and wipe data${RESET}

EOF
