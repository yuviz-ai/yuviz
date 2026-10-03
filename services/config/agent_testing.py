"""
Test sessions for agents that may still be inactive.

A voice test is only a short-lived credential that Conversation redeems. A chat
test runs here: one LLM call per turn, written to the existing calls /
transcript_entries rows with direction 'test'. The calls row is born ended, so
neither the live-call queries nor soft_delete_agent's live-call guard see it.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any, Literal

from libs.config_sdk.test_credentials import (
    CHAT_TTL_S,
    VOICE_TTL_S,
    TestGrant,
    mint_test_credential,
    redeem_test_credential,
)
from libs.tenancy import tenant_conn

from . import db
from .secret_resolver import SecretResolver
from .system_prompt import _load_tenant_llm_config, chat_test_reply

TURN_CAP = 40


class TurnLimitReached(Exception):
    pass


async def mint_test_session(
    redis, *, tenant: dict[str, Any], agent: dict[str, Any], channel: Literal["voice", "chat"],
) -> dict[str, Any]:
    grant = TestGrant(
        tenant_slug=tenant["slug"], tenant_id=str(tenant["id"]), agent_id=str(agent["id"]),
        agent_slug=agent["slug"], channel=channel, session_id=None,
    )
    if channel == "voice":
        return {"credential": await mint_test_credential(redis, grant, VOICE_TTL_S),
                "expires_in": VOICE_TTL_S}

    session_id = str(uuid.uuid4())
    greeting = agent["greeting"]
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        await conn.execute(
            "INSERT INTO calls (session_id, tenant_id, direction, agent_id, agent_config_version, "
            "turn_count, ended_at) VALUES ($1, $2, 'test', $3, $4, 0, now())",
            session_id, tenant["slug"], agent["id"], agent["config_version"],
        )
        await conn.execute(
            "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response) "
            "VALUES ($1, 0, NULL, $2)",
            session_id, greeting,
        )
    credential = await mint_test_credential(
        redis, replace(grant, session_id=session_id), CHAT_TTL_S,
    )
    return {"credential": credential, "session_id": session_id, "greeting": greeting,
            "expires_in": CHAT_TTL_S}


async def load_test_transcript(
    conn, *, tenant_slug: str, agent_id: str, session_id: str,
) -> list[tuple[str | None, str | None]]:
    """(caller_text, agent_text) per turn; empty when the session is not this tenant's
    test session for this agent."""
    rows = await conn.fetch(
        "SELECT te.caller_text, te.ai_response FROM transcript_entries te "
        "JOIN calls c ON c.session_id = te.session_id "
        "WHERE c.session_id = $1 AND c.tenant_id = $2 AND c.agent_id = $3 AND c.direction = 'test' "
        "ORDER BY te.turn_number",
        session_id, tenant_slug, agent_id,
    )
    return [(r["caller_text"], r["ai_response"]) for r in rows]


async def run_chat_turn(
    redis, *, tenant: dict[str, Any], agent: dict[str, Any], session_id: str, credential: str,
    message: str, secret_resolver: SecretResolver,
) -> str:
    grant = await redeem_test_credential(redis, credential, channel="chat", consume=False)
    if (
        grant is None or grant.tenant_slug != tenant["slug"] or grant.agent_id != str(agent["id"])
        or grant.session_id != session_id
    ):
        raise LookupError("test session not found")

    llm_config_id = agent["llm_config_id"] or tenant["default_llm_config_id"]
    llm = await _load_tenant_llm_config(tenant["id"], llm_config_id)

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        turn_number = await conn.fetchval(
            "UPDATE calls SET turn_count = turn_count + 1, ended_at = now() "
            "WHERE session_id = $1 AND tenant_id = $2 AND agent_id = $3 AND direction = 'test' "
            "AND turn_count < $4 RETURNING turn_count",
            session_id, tenant["slug"], agent["id"], TURN_CAP,
        )
        if turn_number is None:
            raise TurnLimitReached()
        history = await load_test_transcript(
            conn, tenant_slug=tenant["slug"], agent_id=agent["id"], session_id=session_id,
        )

    reply = await chat_test_reply(
        tenant["id"], llm_config_id, system_prompt=agent["system_prompt"], history=history,
        message=message, secret_resolver=secret_resolver,
    )

    async with tenant_conn(pool) as conn:
        await conn.execute(
            "INSERT INTO transcript_entries "
            "(session_id, turn_number, caller_text, ai_response, llm_engine) "
            "VALUES ($1, $2, $3, $4, $5)",
            session_id, turn_number, message, reply, llm["engine"],
        )
    return reply
