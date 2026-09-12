"""
audit_log writer for Tool Execution Service — writes to the same platform-
wide audit_log table Config Service and Knowledge Service use (same
Postgres database, shared table, entity_type='custom_api' for every row
this service writes). Duplicated from services/knowledge/audit.py rather
than imported cross-service — small (a single INSERT), and importing
services.knowledge.audit would pull in a dependency this service doesn't
otherwise need, the same reasoning services/knowledge/audit.py already
gives for not importing services.config.audit.

Redacts before writing (finding 8 / low #8), against the audit_log
schema's own explicit rule that *_ref values must be redacted: a
custom_apis row's whole auth_config object is `*_ref` values end to end
(api_key/bearer/oauth2_client_credentials all resolve through it), and one
of them — enc:<fernet-token> — IS the credential sealed at rest. A tenant
admin who pastes a raw key into a *_ref field (the exact failure mode
secret_resolver.py itself names as the likeliest one) must not have that
plaintext land in audit_log, readable by every superadmin and anyone with
DB access, outside the secret-resolution boundary and never rotated.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import asyncpg

# auth_config is masked wholesale (not walked key-by-key): every field
# inside it is a *_ref by construction, and the field-name check below is
# a second, independent net for anything else on the row that happens to
# be named *_ref (defense in depth, not redundant — a future column could
# add one without ever touching this file).
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
