"""
Postgres connection pool for Config Service.

One process-wide pool, lazily created on first use. Callers acquire a
connection per operation (`async with (await get_pool()).acquire() as conn`)
rather than holding one open — this module owns lifecycle only, not query
logic, which lives in tenants.py / agents.py / provider_configs.py etc.
"""

from __future__ import annotations

import json
import os
from typing import Any

import asyncpg

_pool: asyncpg.Pool | None = None


def json_col(value: Any) -> Any:
    """Decode a JSONB column. asyncpg returns strings (no pool codec — writers
    already pass json.dumps into $n::jsonb, so a codec would double-encode)."""
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


async def get_pool(dsn: str | None = None) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = dsn or os.environ["POSTGRES_DSN"]
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
