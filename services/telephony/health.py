"""
health_loop() — probes every loaded account on its own asyncio task, never
inline on a request (AC25-28, Latency section: "no vendor I/O on a request
path"). Status is derived from the single most recent probe: healthy on an
ok probe, degraded on a failed one. Absence of the key is standby, which is
both the pre-first-probe state and what a dead loop's last write decays to
after the EX 900 TTL — one code path for both (no probe ever writes
"standby" itself).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from services.config import cache as config_cache

from .accounts import Account, accounts

log = logging.getLogger("telephony.health")

HEALTH_TTL_S = 900
_PROBE_TIMEOUT_S = 5.0
_MAX_CONCURRENCY = 10


def _health_key(config_id: str) -> str:
    return f"telephony:health:{config_id}"


async def _probe_one(account: Account, semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        try:
            ok = await asyncio.wait_for(account.instance.check_health(), timeout=_PROBE_TIMEOUT_S)
        except Exception:
            ok = False

    status = "healthy" if ok else "degraded"
    await config_cache.set_json(
        _health_key(account.account_ref),
        {"status": status, "checked_at": datetime.now(timezone.utc).isoformat()},
        ttl=HEALTH_TTL_S,
    )


async def probe_all() -> None:
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    tasks = [_probe_one(account, semaphore) for account in accounts.all_accounts()]
    if tasks:
        await asyncio.gather(*tasks)


async def health_loop(interval_s: float | None = None) -> None:
    interval = interval_s if interval_s is not None else float(os.environ.get("TELEPHONY_HEALTH_INTERVAL_S", "300"))
    while True:
        await asyncio.sleep(interval)
        try:
            await probe_all()
        except Exception:
            log.exception("telephony: health loop pass failed")
