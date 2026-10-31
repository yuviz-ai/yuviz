"""Time-bound an awaitable without waiting for it to finish cancelling."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")

# Pool-wide asyncpg command timeout: caps each query and the ROLLBACK/reset on release once the
# connection is idle, so a blackholed DB cannot hang them for the TCP timeout. It does NOT bound the
# wait for a cancelled query's cancel request (asyncpg waits on that before applying any timeout);
# POOL_RELEASE_TIMEOUT_S and the terminate-on-cancel in libs.tenancy.session cover it.
POOL_COMMAND_TIMEOUT_S = 10.0

# Passed as acquire_timeout= on the run_bounded paths. It bounds the wait for a free connection and,
# because asyncpg reuses it as the release budget, the release after a cancel before BEGIN. A cancel
# after BEGIN terminates the connection (tenant_conn/platform_conn do this when acquire_timeout is
# set) so the ROLLBACK fails fast. With None neither bound applies.
POOL_RELEASE_TIMEOUT_S = 10.0


def _consume(task: asyncio.Task) -> None:
    # Retrieve the exception so an abandoned task never logs "exception was never retrieved".
    if not task.cancelled():
        task.exception()


def abandon(task: asyncio.Task) -> None:
    task.add_done_callback(_consume)
    task.cancel()


async def run_bounded(awaitable: Awaitable[T], timeout: float) -> T:
    """Result of `awaitable`, or TimeoutError after `timeout` seconds.

    Unlike asyncio.wait_for, a timeout does not await the cancelled work: a driver that is slow to
    honour cancellation (asyncpg's pool release on a blackholed connection) cannot stretch the bound.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        abandon(task)
        raise
    if not done:
        abandon(task)
        raise TimeoutError
    return task.result()
