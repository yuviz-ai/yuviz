"""Per-tenant rolling window of tool-call durations, read back by FillerSelector to size fillers.

Not thread-safe by design: single asyncio loop, and record()/average_ms() never await.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from itertools import islice

_WINDOW = 50           # samples retained per key
_MIN_SAMPLES = 3        # below this (after age-out), average_ms() returns None
_MAX_AGE_S = 900.0       # samples older than 15 min are dropped on read
_MAX_KEYS_PER_TENANT = 200   # distinct (agent, tool) keys per tenant; LRU beyond that
_SWEEP_EVERY = 64        # run the reclaim sweep on every Nth accepted record()
_SWEEP_SCAN = 32         # tenant entries examined per sweep, from the front of the outer map
# Caps the outer map: _sweep() alone can't keep up with a stream of unique tenant ids.
_MAX_TENANTS = 2000

TenantKey = str                    # tenant_id, outer partition
AgentToolKey = tuple[str, str]     # (agent_id, tool_name), inner LRU key


class ToolLatencyStore:
    def __init__(self) -> None:
        self._tenants: "OrderedDict[TenantKey, OrderedDict[AgentToolKey, deque]]" = OrderedDict()
        self._record_count = 0

    def record(self, tenant_id: str, agent_id: str, tool_name: str, elapsed_ms: float) -> None:
        if not tenant_id or not tenant_id.strip() or not agent_id or not agent_id.strip():
            return
        if not math.isfinite(elapsed_ms) or elapsed_ms <= 0:
            return

        key: AgentToolKey = (agent_id, tool_name)
        tenant_map = self._tenants.get(tenant_id)
        if tenant_map is None:
            tenant_map = OrderedDict()
            self._tenants[tenant_id] = tenant_map
            if len(self._tenants) > _MAX_TENANTS:
                self._tenants.popitem(last=False)
        self._tenants.move_to_end(tenant_id)

        samples = tenant_map.get(key)
        if samples is None:
            samples = deque(maxlen=_WINDOW)
            tenant_map[key] = samples
        tenant_map.move_to_end(key)
        samples.append((time.monotonic(), elapsed_ms))

        if len(tenant_map) > _MAX_KEYS_PER_TENANT:
            tenant_map.popitem(last=False)

        self._record_count += 1
        if self._record_count % _SWEEP_EVERY == 0:
            self._sweep()

    def average_ms(self, tenant_id: str, agent_id: str, tool_name: str) -> float | None:
        if not tenant_id or not tenant_id.strip() or not agent_id or not agent_id.strip():
            return None

        tenant_map = self._tenants.get(tenant_id)
        if tenant_map is None:
            return None
        self._tenants.move_to_end(tenant_id)

        key: AgentToolKey = (agent_id, tool_name)
        samples = tenant_map.get(key)
        if samples is None:
            return None
        tenant_map.move_to_end(key)

        cutoff = time.monotonic() - _MAX_AGE_S
        while samples and samples[0][0] < cutoff:
            samples.popleft()
        if not samples:
            del tenant_map[key]
            if not tenant_map:
                del self._tenants[tenant_id]
            return None

        if len(samples) < _MIN_SAMPLES:
            return None
        return sum(ms for _, ms in samples) / len(samples)

    def _sweep(self) -> None:
        now = time.monotonic()
        cutoff = now - _MAX_AGE_S
        for tenant_id in list(islice(self._tenants, _SWEEP_SCAN)):
            tenant_map = self._tenants.get(tenant_id)
            if tenant_map is None:
                continue
            if _tenant_is_reclaimable(tenant_map, cutoff):
                del self._tenants[tenant_id]
            else:
                self._tenants.move_to_end(tenant_id)


def _tenant_is_reclaimable(tenant_map: "OrderedDict[AgentToolKey, deque]", cutoff: float) -> bool:
    if not tenant_map:
        return True
    for samples in tenant_map.values():
        if samples and samples[-1][0] >= cutoff:
            return False
    return True
