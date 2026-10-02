#!/usr/bin/env bash
# Schemas -> service account -> default agent. Idempotent.
set -euo pipefail

echo "→ applying schemas"
for f in schema knowledge_schema telephony_schema; do
    psql "$POSTGRES_DSN" -v ON_ERROR_STOP=1 -q -f "/app/database/${f}.sql" >/dev/null
    echo "  ✓ ${f}.sql"
done

# Services still connect as superuser on $POSTGRES_DSN, so RLS is inert until the DSN cutover.
psql "$POSTGRES_DSN" -v ON_ERROR_STOP=1 -v yuviz_app_password="${YUVIZ_APP_PASSWORD:?set this in your shell — see docs/setup.md, never commit the real value}" \
    -q -f "/app/database/rls.sql" >/dev/null
echo "  ✓ rls.sql"

echo "→ service account"
# A unique-constraint error is expected on every run after the first.
if err=$(python3 /app/scripts/create_service_account.py \
             "$CONFIG_SERVICE_EMAIL" "$CONFIG_SERVICE_PASSWORD" 2>&1); then
    echo "  ✓ created ${CONFIG_SERVICE_EMAIL}"
elif printf '%s' "$err" | grep -qiE "unique|duplicate|already exists"; then
    echo "  ✓ ${CONFIG_SERVICE_EMAIL} already exists"
else
    printf '%s\n' "$err" >&2
    exit 1
fi

echo "→ superadmin"
python3 /app/scripts/seed_superadmin.py

echo "→ seeding default agent (ollama at ${OLLAMA_BASE_URL})"
python3 /app/scripts/seed_default_config.py

echo "✓ init complete"
