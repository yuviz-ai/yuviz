"""
Unit tests for services/toolexec/admission.py (T10) — no DB, no network.

Lesson 25: these drive the DEPLOYED cap values (whatever
TOOLEXEC_MAX_CONCURRENT_RUNS_PER_AGENT / _MAX_RUNS_PER_MINUTE_PER_AGENT
resolve to right now — the real default if unset), never a
test-reconfigured smaller constant, so a passing test proves the shipped
ceiling actually refuses the (N+1)th caller, not just some easier number.
"""

from __future__ import annotations

import uuid

from services.toolexec import admission


def _fresh_ids() -> tuple[str, str]:
    return str(uuid.uuid4()), str(uuid.uuid4())


def test_concurrent_cap_refuses_the_nplus1th_run_for_one_agent():
    tenant_id, agent_id = _fresh_ids()
    limit = admission._max_concurrent()

    admitted = [admission.acquire(tenant_id, agent_id) for _ in range(limit)]
    assert all(admitted), admitted  # every one of the deployed cap's own slots is admitted

    refused = admission.acquire(tenant_id, agent_id)
    assert refused is False  # the (limit + 1)th is refused


def test_concurrent_cap_is_scoped_per_agent_not_per_tenant():
    tenant_id, agent_id_a = _fresh_ids()
    _, agent_id_b = _fresh_ids()
    limit = admission._max_concurrent()

    for _ in range(limit):
        assert admission.acquire(tenant_id, agent_id_a) is True
    assert admission.acquire(tenant_id, agent_id_a) is False  # agent A is at its cap

    # A different agent in the SAME tenant is unaffected by agent A's cap.
    assert admission.acquire(tenant_id, agent_id_b) is True


def test_released_slot_admits_a_subsequent_call():
    tenant_id, agent_id = _fresh_ids()
    limit = admission._max_concurrent()

    for _ in range(limit):
        assert admission.acquire(tenant_id, agent_id) is True
    assert admission.acquire(tenant_id, agent_id) is False

    admission.release(tenant_id, agent_id)
    assert admission.acquire(tenant_id, agent_id) is True


def test_per_minute_cap_refuses_the_nplus1th_run_even_with_slots_free():
    """Runs that complete immediately (acquire then release right away)
    never touch the concurrency cap, but the per-minute cap still counts
    every one of them — proving the two caps are independent."""
    tenant_id, agent_id = _fresh_ids()
    per_minute_limit = admission._max_per_minute()

    for _ in range(per_minute_limit):
        assert admission.acquire(tenant_id, agent_id) is True
        admission.release(tenant_id, agent_id)

    # Concurrency is back to zero, but the per-minute window is exhausted.
    assert admission.acquire(tenant_id, agent_id) is False
