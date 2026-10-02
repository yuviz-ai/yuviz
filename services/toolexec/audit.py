"""audit_log writer for Tool Execution Service (shared platform table).
Redacts auth_config and *_ref fields: they may hold sealed credentials or mistakenly pasted raw keys."""

from __future__ import annotations

import json
from typing import Any, Literal

import asyncpg

# Masked wholesale; the *_ref suffix check in _redact() catches any other column.
_SECRET_FIELDS = {"auth_config"}


def _redact(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        k: ("[redacted]" if k in _SECRET_FIELDS or k.endswith("_ref") else v)
        for k, v in value.items()
    }


async def write_audit(
    conn: asyncpg.Connection,
    *,
    entity_type: str,
    entity_id: Any,
    action: Literal["created", "updated", "deleted"],
    user_id: Any | None = None,
    user_email: str | None = None,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO audit_log "
        "(entity_type, entity_id, user_id, user_email, action, old_value, new_value, ip_address) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8)",
        entity_type,
        entity_id,
        user_id,
        user_email,
        action,
        json.dumps(_redact(old_value), default=str) if old_value is not None else None,
        json.dumps(_redact(new_value), default=str) if new_value is not None else None,
        ip_address,
    )
