#!/usr/bin/env python3
"""Recovery path to create a superadmin; fails loudly if the email already exists.

Usage: python3 scripts/create_superadmin.py <email> <password>
Requires POSTGRES_ADMIN_DSN (or POSTGRES_DSN): writes a tenant_id IS NULL row, so it bypasses RLS.
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
