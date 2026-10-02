from __future__ import annotations

import logging
from typing import Any

import httpx

from ..interfaces import ChatMessage


async def raise_with_body_logged(resp: httpx.Response, *, log: logging.Logger, provider: str) -> None:
    """raise_for_status(), but log the error body first (httpx never surfaces it)."""
    if resp.is_success:
        return
    body = await resp.aread()
    log.error("%s: HTTP %s error body=%s", provider, resp.status_code, body.decode(errors="replace"))
    resp.raise_for_status()


def build_chat_messages(system: str, messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Prepend `system` unless `messages` already has a system message (per-agent prompt)."""
    has_system = any(m.role == "system" for m in messages)
    result: list[dict[str, Any]] = []
    if system and not has_system:
        result.append({"role": "system", "content": system})
    for m in messages:
        entry: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls is not None:
            entry["tool_calls"] = m.tool_calls
        if m.tool_call_id is not None:
            entry["tool_call_id"] = m.tool_call_id
        result.append(entry)
    return result
