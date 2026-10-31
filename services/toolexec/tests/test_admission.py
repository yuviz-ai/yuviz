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


# ── issue #193: a transient burst queues for a slot instead of being refused ──

import asyncio  # noqa: E402

import pytest  # noqa: E402


def _fill(tenant_id: str, agent_id: str) -> int:
    limit = admission._max_concurrent()
    for _ in range(limit):
        assert admission.acquire(tenant_id, agent_id) is True
    return limit


def _clean(tenant_id: str, agent_id: str) -> None:
    key = (tenant_id, agent_id)
    assert not admission._waiters.get(key), "waiter leaked"


@pytest.mark.asyncio
async def test_burst_over_the_cap_is_all_served_when_slots_free_within_the_wait():
    tenant_id, agent_id = _fresh_ids()
    limit = _fill(tenant_id, agent_id)
    extra = 10  # 20 callers on one agent in the issue: far more than the cap

    async def caller():
        ok = await admission.acquire_wait(tenant_id, agent_id, 2.0)
        if ok:
            await asyncio.sleep(0.01)
            admission.release(tenant_id, agent_id)
        return ok

    tasks = [asyncio.create_task(caller()) for _ in range(extra)]
    await asyncio.sleep(0.02)
    for _ in range(limit):  # the originally-held runs finish
        admission.release(tenant_id, agent_id)
    assert all(await asyncio.gather(*tasks))
    assert admission._concurrent[(tenant_id, agent_id)] == 0
    _clean(tenant_id, agent_id)


@pytest.mark.asyncio
async def test_wait_is_bounded_and_then_refuses():
    tenant_id, agent_id = _fresh_ids()
    _fill(tenant_id, agent_id)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await admission.acquire_wait(tenant_id, agent_id, 0.05) is False
    assert 0.04 <= loop.time() - t0 < 0.5
    _clean(tenant_id, agent_id)
    assert admission._concurrent[(tenant_id, agent_id)] == admission._max_concurrent()  # no slot leaked/stolen


@pytest.mark.asyncio
async def test_zero_wait_refuses_immediately():
    tenant_id, agent_id = _fresh_ids()
    _fill(tenant_id, agent_id)
    assert await admission.acquire_wait(tenant_id, agent_id, 0) is False


@pytest.mark.asyncio
async def test_per_minute_cap_is_never_waited_on():
    tenant_id, agent_id = _fresh_ids()
    for _ in range(admission._max_per_minute()):
        assert admission.acquire(tenant_id, agent_id) is True
        admission.release(tenant_id, agent_id)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await admission.acquire_wait(tenant_id, agent_id, 5.0) is False
    assert loop.time() - t0 < 0.5


@pytest.mark.asyncio
async def test_waiters_are_served_fifo_and_do_not_exceed_the_cap():
    tenant_id, agent_id = _fresh_ids()
    _fill(tenant_id, agent_id)
    order: list[int] = []

    async def caller(i: int):
        assert await admission.acquire_wait(tenant_id, agent_id, 2.0)
        order.append(i)

    tasks = []
    for i in range(3):
        tasks.append(asyncio.create_task(caller(i)))
        await asyncio.sleep(0.005)
    for _ in range(3):
        admission.release(tenant_id, agent_id)
        await asyncio.sleep(0.01)
    await asyncio.gather(*tasks)
    assert order == [0, 1, 2]
    assert admission._concurrent[(tenant_id, agent_id)] == admission._max_concurrent()


@pytest.mark.asyncio
async def test_a_new_caller_cannot_barge_past_a_queued_one():
    tenant_id, agent_id = _fresh_ids()
    _fill(tenant_id, agent_id)
    first = asyncio.create_task(admission.acquire_wait(tenant_id, agent_id, 2.0))
    await asyncio.sleep(0.01)
    admission.release(tenant_id, agent_id)  # frees one slot, wakes `first`
    second = asyncio.create_task(admission.acquire_wait(tenant_id, agent_id, 0.05))
    assert await first is True
    assert await second is False  # cap is full again and it queued behind


@pytest.mark.asyncio
async def test_cancelled_waiter_leaks_nothing_and_does_not_strand_the_next():
    tenant_id, agent_id = _fresh_ids()
    _fill(tenant_id, agent_id)
    a = asyncio.create_task(admission.acquire_wait(tenant_id, agent_id, 5.0))
    b = asyncio.create_task(admission.acquire_wait(tenant_id, agent_id, 5.0))
    await asyncio.sleep(0.01)
    admission.release(tenant_id, agent_id)  # wakes a
    a.cancel()  # caller barged in after the slot was handed to a but before a used it
    try:
        got = await a
    except asyncio.CancelledError:
        got = False
    if got:  # cancellation lost the race and a owns the slot (executor's finally releases it)
        admission.release(tenant_id, agent_id)
    assert await asyncio.wait_for(b, 1.0) is True  # b gets the slot
    assert admission._concurrent[(tenant_id, agent_id)] == admission._max_concurrent()
    _clean(tenant_id, agent_id)


@pytest.mark.asyncio
async def test_queues_are_per_agent_and_per_tenant():
    t1, a1 = _fresh_ids()
    t2, _ = _fresh_ids()
    _fill(t1, a1)
    # same agent id under another tenant is a different key: admitted at once
    assert await admission.acquire_wait(t2, a1, 0) is True


def test_max_wait_env_parsing(monkeypatch):
    monkeypatch.setenv("TOOLEXEC_ADMISSION_MAX_WAIT_MS", "250")
    assert admission.max_wait_ms() == 250
    monkeypatch.setenv("TOOLEXEC_ADMISSION_MAX_WAIT_MS", "-5")
    assert admission.max_wait_ms() == 0
    monkeypatch.setenv("TOOLEXEC_ADMISSION_MAX_WAIT_MS", "junk")
    assert admission.max_wait_ms() == 1000


@pytest.mark.asyncio
async def test_cancelled_while_queued_removes_itself():
    tenant_id, agent_id = _fresh_ids()
    _fill(tenant_id, agent_id)
    w = asyncio.create_task(admission.acquire_wait(tenant_id, agent_id, 5.0))
    await asyncio.sleep(0.01)
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    _clean(tenant_id, agent_id)
    admission.release(tenant_id, agent_id)
    assert admission._concurrent[(tenant_id, agent_id)] == admission._max_concurrent() - 1  # nothing handed to a ghost


@pytest.mark.asyncio
async def test_handoff_respects_the_per_minute_cap(monkeypatch):
    tenant_id, agent_id = _fresh_ids()
    monkeypatch.setenv("TOOLEXEC_MAX_RUNS_PER_MINUTE_PER_AGENT", str(admission._max_concurrent()))
    _fill(tenant_id, agent_id)  # per-minute budget now exhausted too
    w = asyncio.create_task(admission.acquire_wait(tenant_id, agent_id, 5.0))
    await asyncio.sleep(0.01)
    admission.release(tenant_id, agent_id)
    assert await asyncio.wait_for(w, 1.0) is False
    assert admission._concurrent[(tenant_id, agent_id)] == admission._max_concurrent() - 1


@pytest.mark.asyncio
async def test_newcomer_behind_a_queued_waiter_is_refused_at_once_when_per_minute_is_full(monkeypatch):
    import time as _t
    tenant_id, agent_id = _fresh_ids()
    monkeypatch.setenv("TOOLEXEC_MAX_RUNS_PER_MINUTE_PER_AGENT", str(admission._max_concurrent() + 1))
    _fill(tenant_id, agent_id)
    queued = asyncio.create_task(admission.acquire_wait(tenant_id, agent_id, 5.0))
    await asyncio.sleep(0.01)
    assert admission._waiters.get((tenant_id, agent_id))  # genuinely queued
    admission._run_timestamps[(tenant_id, agent_id)].append(_t.time())  # window now full
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await admission.acquire_wait(tenant_id, agent_id, 5.0) is False
    assert loop.time() - t0 < 0.2
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
