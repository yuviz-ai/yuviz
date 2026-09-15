"""
agent_retrieval_policies CRUD — the server-side configuration surface that
lets each agent's RetrievalPolicy be tuned (top_k, minimum_score, max_tokens,
citation behavior) without touching Conversation Service or any caller's
code. No row for an agent is a normal, cheap state ("use the system
default"), not a missing-config error — get_policy() returns None rather
than raising, matching every other optional-config lookup in this project.
"""

from __future__ import annotations

from typing import Any

from libs.tenancy import tenant_conn

from . import audit, db

_UPDATABLE_FIELDS = {"top_k", "max_tokens", "minimum_score", "rerank", "hybrid_search", "include_citations"}


async def get_policy(agent_id: Any) -> dict[str, Any] | None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow("SELECT * FROM agent_retrieval_policies WHERE agent_id = $1", agent_id)
    return dict(row) if row is not None else None


async def upsert_policy(
    agent_id: Any, *, user_id: Any | None = None, user_email: str | None = None, **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("upsert_policy() called with no fields to set")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"upsert_policy() got unknown field(s): {unknown}")

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM agent_retrieval_policies WHERE agent_id = $1 FOR UPDATE", agent_id,
        )
        old = dict(old_row) if old_row is not None else None

        columns = ["agent_id", *fields.keys()]
        placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
        update_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(fields.keys()))
        new_row = await conn.fetchrow(
            f"INSERT INTO agent_retrieval_policies ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT (agent_id) DO UPDATE SET {update_clause}, updated_at = now() RETURNING *",
            agent_id, *fields.values(),
        )
        new = dict(new_row)

        await audit.write_audit(
            conn, entity_type="agent_retrieval_policy", entity_id=agent_id,
            action="updated" if old is not None else "created",
            user_id=user_id, user_email=user_email, old_value=old, new_value=new,
        )
    return new
