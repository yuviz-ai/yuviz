#!/usr/bin/env python3
"""
Creates a service-account user for a backend service to authenticate as —
e.g. Conversation Service's HttpConfigRepository (libs/config_sdk), which
needs a real JWT to call Config Service's REST API now that every route
requires auth (see services/config/deps.py). Role is always 'viewer': every
known Config SDK consumer only ever reads configuration, never writes it,
so least-privilege is the correct default. Re-run this with a different
role directly via services.config.users.create_user() if a future service
genuinely needs to write.

Usage: python3 scripts/create_service_account.py <email> <password>
Requires: POSTGRES_ADMIN_DSN, falling back to POSTGRES_DSN (see services/config/db.py) —
this writes a tenant_id IS NULL row and must keep bypassing RLS, so it connects
as the superuser, never as yuviz_app.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.config import users  # noqa: E402


async def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <email> <password>", file=sys.stderr)
        sys.exit(1)
    email, password = sys.argv[1], sys.argv[2]

    from services.config import db

    await db.get_pool(dsn=os.environ.get("POSTGRES_ADMIN_DSN") or os.environ["POSTGRES_DSN"])
    user = await users.create_user(email=email, password=password, role="viewer", tenant_id=None)
    pool = await db.get_pool()
    await pool.execute("UPDATE users SET is_service_account = true WHERE id = $1", user["id"])
    print(f"Created service account {user['email']} (id={user['id']}, role=viewer)")


if __name__ == "__main__":
    asyncio.run(main())
