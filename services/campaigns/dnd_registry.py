"""Hook for a national do-not-disturb registry. Only the interface exists; AllowAllRegistry makes no network call."""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

Verdict = Literal["allow", "deny", "unknown"]


class IDndRegistry(Protocol):
    async def check(self, e164: str, purpose: str | None, tenant_id: UUID) -> Verdict: ...


class AllowAllRegistry:
    async def check(self, e164: str, purpose: str | None, tenant_id: UUID) -> Verdict:
        return "allow"
