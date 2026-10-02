"""Unit tests for admission.py, driven against the deployed cap values, not test-only constants."""

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
    """Per-minute cap counts runs even when the concurrency cap is free."""
    tenant_id, agent_id = _fresh_ids()
    per_minute_limit = admission._max_per_minute()

    for _ in range(per_minute_limit):
        assert admission.acquire(tenant_id, agent_id) is True
        admission.release(tenant_id, agent_id)

    # Concurrency is back to zero, but the per-minute window is exhausted.
    assert admission.acquire(tenant_id, agent_id) is False
