"""Throwaway database with database/schema.sql applied, for schema tests that must not touch the shared DB."""

from __future__ import annotations

import os
import pathlib
import subprocess
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import asyncpg

DATABASE_DIR = pathlib.Path(__file__).resolve().parents[3] / "database"
SCHEMA_FILES = [DATABASE_DIR / "schema.sql", DATABASE_DIR / "telephony_schema.sql", DATABASE_DIR / "knowledge_schema.sql", DATABASE_DIR / "rls.sql"]


def _dsn_for(dbname: str) -> str:
    parts = urlsplit(os.environ["POSTGRES_DSN"])
    return urlunsplit(parts._replace(path=f"/{dbname}"))


def apply_schema(dsn: str) -> None:
    for path in SCHEMA_FILES:
        subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-v", "yuviz_app_password=scratch", "-f", str(path), dsn], check=True, capture_output=True)


@asynccontextmanager
async def scratch_database():
    name = f"schema_test_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(_dsn_for("postgres"))
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _dsn_for(name)
    try:
        apply_schema(dsn)
        yield dsn
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()

