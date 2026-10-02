# Shared .env helpers for the native launchers. Source after setting REPO.
# Sourced into the operator's shell: functions return 0 on a printed hint
# (a nonzero return under `set -e` would close the terminal tab).

_rand() { head -c "$(( ${1:-32} * 3 ))" /dev/urandom | base64 | LC_ALL=C tr -cd 'A-Za-z0-9' | cut -c "1-${1:-32}"; }

# Replaces KEY's line in .env literally: ENVIRON (unlike sed or awk -v) doesn't
# interpret `&` or `\`. Returns 1 on failure; callers test it with `if`.
_env_set() {
  local tmp="$REPO/.env.tmp.$$"
  if (umask 077; _ENV_SET_KEY="$1" _ENV_SET_VALUE="$2" awk '
        index($0, ENVIRON["_ENV_SET_KEY"] "=") == 1 { print ENVIRON["_ENV_SET_KEY"] "=" ENVIRON["_ENV_SET_VALUE"]; next }
        { print }' "$REPO/.env" > "$tmp") && mv -f "$tmp" "$REPO/.env"; then
    return 0
  fi
  rm -f "$tmp"
  echo "could not write $1 to $REPO/.env" >&2
  return 1
}

_env_get() { grep "^$1=" "$REPO/.env" | cut -d= -f2-; }

# Warns when the shell exports KEY with a value other than .env's (_load_env
# never overrides shell vars, so stale tabs keep old values). Always returns 0.
_warn_env_drift() {
  local shell_value file_value
  shell_value=$(printenv "$1" || true)
  [ -n "$shell_value" ] || return 0
  file_value=$(_env_get "$1" 2>/dev/null || true)
  [ "$shell_value" = "$file_value" ] && return 0
  printf 'WARNING: this shell exports %s=%s but .env has %s=%s.\n' "$1" "$shell_value" "$1" "$file_value" >&2
  printf '  What you start from here uses %s. Open a new tab, or run\n' "$shell_value" >&2
  printf '  `unset %s` and source scripts/start_local.sh again, before restarting.\n' "$1" >&2
  return 0
}

# Like _env_set, but appends KEY when .env has no line for it (_env_set alone
# would change nothing and still succeed). Returns 1 on failure.
_env_put() {
  if grep -q "^$1=" "$REPO/.env"; then
    _env_set "$1" "$2"
    return
  fi
  # Keep the new line on its own when .env does not end in a newline.
  if [ -s "$REPO/.env" ] && [ -n "$(tail -c 1 "$REPO/.env")" ]; then
    printf '\n' >> "$REPO/.env" || return 1
  fi
  printf '%s=%s\n' "$1" "$2" >> "$REPO/.env" && return 0
  echo "could not write $1 to $REPO/.env" >&2
  return 1
}

# Creates .env from .env.example, adds keys added to the example since, and
# fills any blank platform secret. Never touches a value already set.
_env_init() {
  [ -f "$REPO/.env" ] || { (umask 077; cp "$REPO/.env.example" "$REPO/.env"); echo "✓ created .env from .env.example"; }
  local line key
  while IFS= read -r line; do
    key=${line%%=*}
    case "$line" in ''|'#'*) continue ;; esac
    # printf, not echo: zsh's echo would expand a `\` in the line.
    grep -q "^${key}=" "$REPO/.env" || { printf '%s\n' "$line" >> "$REPO/.env"; echo "✓ added $key to .env"; }
  done < "$REPO/.env.example"
  local spec name len current
  for spec in JWT_SECRET:48 CONFIG_SERVICE_PASSWORD:32 YUVIZ_APP_PASSWORD:32 TOOLEXEC_ARGS_HMAC_KEY:48 SECRET_ENCRYPTION_KEY:fernet; do
    name=${spec%:*}; len=${spec#*:}
    [ -z "$(_env_get "$name")" ] || continue
    # Keep a value already exported in this shell, or later tabs get a different key.
    current=$(printenv "$name" || true)
    if [ -n "$current" ]; then
      if _env_set "$name" "$current"; then echo "✓ saved $name from your shell to .env"; fi
    elif [ "$len" = fernet ]; then
      # Fernet: 32 random bytes, url-safe base64.
      if _env_set "$name" "$(head -c 32 /dev/urandom | base64 | LC_ALL=C tr '+/' '-_')"; then echo "✓ generated $name"; fi
    else
      if _env_set "$name" "$(_rand "$len")"; then echo "✓ generated $name"; fi
    fi
  done
  return 0
}

# Exports every non-blank .env value, except variables already set in the
# shell (so a one-off override still works).
_load_env() {
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key=${line%%=*}; value=${line#*=}
    [ -n "$value" ] && [ -z "$(printenv "$key")" ] && export "$key=$value"
  done < "$REPO/.env"
  export POSTGRES_DSN="${POSTGRES_DSN:-postgresql://$USER@localhost:5432/voiceai}"
}

# Returns 1 with a pointer to .env when a setting is blank. Callers use
# `_require X || return 0` so the hint stays on screen.
_require() {
  local name
  for name in "$@"; do
    [ -n "$(printenv "$name")" ] || { echo "$name is not set — add it to $REPO/.env (see .env.example)" >&2; return 1; }
  done
}
