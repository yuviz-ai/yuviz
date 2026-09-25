#!/usr/bin/env python3
"""
One-off, idempotent, re-runnable relabel + credential-sealing pass for the
unified telephony cutover (.sdlc/unified-telephony-service/02-design.md's
Data section, AC29-31):

1. Relabel `telephony_configs.provider` from `'cloudonix'` to `'native'`
   for every row whose tenant's DID is in the 5000-5009 Kamailio/FreeSWITCH
   range — those rows never were actually served by the REST Cloudonix
   plane, and the label was always wrong. Only `provider` changes;
   `updated_at` is deliberately NOT touched (AC29). Re-runnable because the
   `provider = 'cloudonix'` predicate is what makes a second pass a no-op
   (AC31).
2. Seal any pre-existing PLAINTEXT sensitive credential field (e.g. a
   Vobiz `auth_token` written before `_normalize_credentials()` existed)
   via `services.config.telephony_configs._normalize_credentials()`,
   writing back only when the result differs. Idempotent because
   `is_encrypted()` values are kept verbatim.
3. Delete the Redis `telephony_config:{id}` cache-aside entry for every
   relabelled row, so the 60s cache-aside copy cannot keep serving the
   stale 'cloudonix' label.

*** DO NOT RUN THIS AGAINST A LIVE DATABASE UNTIL services/vobiz IS FULLY
REPOINTED AT services/telephony (unified-telephony cutover phase 1). ***
Sealing a row's plaintext credential while the OLD services/vobiz is still
reading it live would break that still-running service (see the design's
Risks section) — this is a deliberate, explicit, separately-run step, not
something a deploy pipeline should invoke automatically.

Usage:
  python3 scripts/migrate_telephony_providers.py [--dry-run]

Requires: POSTGRES_DSN, REDIS_URL, SECRET_ENCRYPTION_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402
import redis.asyncio as redis  # noqa: E402

from services.config import telephony_configs as telephony_configs_service  # noqa: E402

_RELABEL_PREDICATE = """
    tc.provider = 'cloudonix'
    AND EXISTS (SELECT 1 FROM phone_numbers pn
                 WHERE pn.telephony_config_id = tc.id
                   AND pn.did ~ '^500[0-9]$')
"""

_RELABEL_SELECT_SQL = f"SELECT tc.id FROM telephony_configs tc WHERE {_RELABEL_PREDICATE}"
_RELABEL_UPDATE_SQL = f"UPDATE telephony_configs tc SET provider = 'native' WHERE {_RELABEL_PREDICATE} RETURNING tc.id"


async def _relabel(pool: asyncpg.Pool, *, dry_run: bool) -> list[str]:
    sql = _RELABEL_SELECT_SQL if dry_run else _RELABEL_UPDATE_SQL
    rows = await pool.fetch(sql)
    return [str(r["id"]) for r in rows]


async def _seal_plaintext_credentials(pool: asyncpg.Pool, *, dry_run: bool) -> int:
    """Reads every non-native row, applies _normalize_credentials(), and
    writes back only when the result differs — never touches a row whose
    credentials are already fully sealed (is_encrypted() values pass
    through verbatim, so the comparison is stable)."""
    rows = await pool.fetch(
        "SELECT id, provider, credentials FROM telephony_configs WHERE provider != 'native' AND deleted_at IS NULL",
    )
    sealed_count = 0
    for row in rows:
        credentials = row["credentials"]
        if isinstance(credentials, str):
            credentials = json.loads(credentials)
        try:
            sealed = telephony_configs_service._normalize_credentials(row["provider"], credentials)
        except ValueError as exc:
            print(f"  SKIPPED {row['id']} ({row['provider']}): {exc}", file=sys.stderr)
            continue
        if sealed == credentials:
            continue
        sealed_count += 1
        print(f"  sealing {row['id']} ({row['provider']})")
        if not dry_run:
            await pool.execute(
                "UPDATE telephony_configs SET credentials = $2::jsonb WHERE id = $1",
                row["id"], json.dumps(sealed),
            )
    return sealed_count


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args()

    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("POSTGRES_DSN is not set", file=sys.stderr)
        return 1
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    pool = await asyncpg.create_pool(dsn)
    try:
        relabelled_ids = await _relabel(pool, dry_run=args.dry_run)
        print(f"relabelled {len(relabelled_ids)} row(s) 'cloudonix' -> 'native'"
              f"{' (dry run)' if args.dry_run else ''}")

        if relabelled_ids and not args.dry_run:
            r = redis.from_url(redis_url, decode_responses=True)
            try:
                keys = [f"telephony_config:{cid}" for cid in relabelled_ids]
                await r.delete(*keys)
            finally:
                await r.aclose()

        sealed_count = await _seal_plaintext_credentials(pool, dry_run=args.dry_run)
        print(f"sealed {sealed_count} row(s)' plaintext credential field(s)"
              f"{' (dry run)' if args.dry_run else ''}")
    finally:
        await pool.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
