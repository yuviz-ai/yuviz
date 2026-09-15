#!/usr/bin/env python3
"""
Creates a superadmin user from the command line. The normal path for the
*first* account is the Admin UI's "Create your administrator account" screen
(POST /auth/bootstrap, open only while no superadmin exists); this script is
the headless equivalent, and the recovery path when every superadmin is
locked out — every other way to create an account now goes through the
invite flow (POST /invites, then POST /invites/accept), which itself
requires an existing superadmin/admin to send the invite (see
services/config/routers/invites.py). Not idempotent in the sense of "safe to re-run for the same
email": users.email is UNIQUE, so a second run for the same address fails
loudly (asyncpg.UniqueViolationError) rather than silently doing nothing —
correct here, unlike seed_default_config.py's config rows, because a second
"same email" call is far more likely to be a mistake than an intentional
re-seed.

Usage: python3 scripts/create_superadmin.py <email> <password>
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

from services.config import db  # noqa: E402
from services.config import users  # noqa: E402


async def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <email> <password>", file=sys.stderr)
        sys.exit(1)
    email, password = sys.argv[1], sys.argv[2]

    await db.get_pool(dsn=os.environ.get("POSTGRES_ADMIN_DSN") or os.environ["POSTGRES_DSN"])
    user = await users.create_user(email=email, password=password, role="superadmin", tenant_id=None)
    print(f"Created superadmin {user['email']} (id={user['id']})")


if __name__ == "__main__":
    asyncio.run(main())
