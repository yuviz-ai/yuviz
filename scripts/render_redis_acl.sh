#!/usr/bin/env bash
# Usage: REDIS_<CLIENT>_PASSWORD=... render_redis_acl.sh [out]; writes the ACL file with sha256 hashes only.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tpl="$REPO/deployment/docker/redis/users.acl.tpl"
out="${1:-${REDIS_ACLFILE:-$REPO/deployment/docker/redis/users.acl}}"

rendered=$(cat "$tpl")
for client in $(grep -o '{{[a-z]*}}' "$tpl" | tr -d '{}' | sort -u); do
  var="REDIS_$(printf '%s' "$client" | tr '[:lower:]' '[:upper:]')_PASSWORD"
  [ -n "${!var:-}" ] || { echo "$var is not set — add it to $REPO/.env (see .env.example)" >&2; exit 1; }
  hash=$(printf '%s' "${!var}" | shasum -a 256 | cut -d' ' -f1)
  rendered=${rendered//"{{$client}}"/$hash}
done

tmp=$(umask 077; mktemp "$out.XXXXXX")
trap 'rm -f "$tmp"' EXIT
printf '%s\n' "$rendered" > "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" "$out"
trap - EXIT
