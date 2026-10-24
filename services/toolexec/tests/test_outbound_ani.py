"""A campaign call reaches Conversation as outbound with the callee as `caller_did` (the Gateway maps ani to it)
and the agent's DID, shared by every campaign call, as `called_did`. That DID must never be taken for the caller."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.toolexec import executor

from .test_chain_execution import _mock, _request
from .test_preset_execution import CALLEE, CAMPAIGN_DID, Upstream, _apply_calendar, _call, own_did  # noqa: F401

_REPO = Path(__file__).resolve().parents[3]
_NUMBER_FIELDS = re.compile(r"\b(caller_did|called_did|caller_number|called_number)\b")

# Every non-test module under these services that reads a call's numbers; each was read for how it uses direction.
_CONSUMERS = {
    "services/conversation/__main__.py",              # passes the session's numbers on, unchanged
    "services/conversation/pipeline.py",              # caller-number prompt line: the remote party on a campaign call
    "services/conversation/servicer.py",              # copies OpenSession into the session context
    "services/conversation/session.py",               # field definitions
    "services/conversation/tools/executors/api_exec_executor.py",  # forwards caller_number to the tenant's API
    "services/conversation/tools/orchestrator.py",    # forwards the numbers to toolexec
    "services/conversation/tools/types.py",           # field definitions
    "services/conversation/transcript_builder.py",    # stores both numbers with the direction
    "services/conversation/transfer_engine.py",       # caller ID shown to the human agent: the remote party
    "services/toolexec/executor.py",                  # _remote_party, the only branch point (direction aware)
    "services/toolexec/presets.py",                   # remote_party_number
    "services/toolexec/schemas.py",                   # request fields
}


def _found_consumers() -> set[str]:
    found = set()
    for service in ("conversation", "toolexec"):
        for path in (_REPO / "services" / service).rglob("*.py"):
            rel = str(path.relative_to(_REPO))
            if "/tests/" in rel or "/generated/" in rel:
                continue
            if _NUMBER_FIELDS.search(path.read_text()):
                found.add(rel)
    return found


def test_the_consumers_of_the_call_numbers_are_exactly_the_reviewed_set():
    found = _found_consumers()
    assert len(found) > 0
    assert found == _CONSUMERS, "a module started reading the call's numbers: check how it treats direction"


@pytest.mark.asyncio
async def test_a_campaign_call_cannot_find_or_match_a_booking_by_the_shared_did(pool, tenant_agent, own_did, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    upstream = Upstream(default=(200, {"items": []}))
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    response = await executor.execute_chain(_request(
        tenant, agent, "gcal_find_booking", **_call("outbound", CALLEE, CAMPAIGN_DID),
        caller_arguments={"timeMin": "2026-10-01T00:00:00+05:30"},
    ))

    assert (response.chain_status, response.error) == ("invalid_argument", "caller_id_unavailable")
    assert upstream.requests == []
