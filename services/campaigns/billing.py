"""The only module that knows billing exists. Billing is not built yet, so campaigns wire NullBillingAdmission;
billing's PR swaps that one constructor and must pass tests/test_billing_contract.py."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

import asyncpg

BILLING_TIMEOUT_S = 2.0
ESTIMATE_DEFAULT_BILLABLE_S = 60

Path = Literal["sip", "cpaas"]


def billing_key(attempt_id: UUID) -> str:
    return f"campaign:{attempt_id}"


@dataclass(frozen=True)
class AccountStatus:
    has_account: bool
    balance_minor: int | None
    currency: str | None
    max_hold_s: int | None


@dataclass(frozen=True)
class Granted:
    max_seconds: int
    hold_minor: int | None


@dataclass(frozen=True)
class Refused:
    reason: Literal["insufficient_credit", "no_account", "concurrency", "unavailable"]


@dataclass(frozen=True)
class Settlement:
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class CostEstimate:
    currency: str
    calls: int
    avg_billable_s: int
    avg_source: Literal["tenant_history", "default"]
    per_call_minor: int
    total_minor: int


class BillingAdmission(Protocol):
    enabled: bool

    async def account_status(self, tenant_id: UUID) -> AccountStatus: ...

    async def estimate(
        self, tenant_id: UUID, *, path: Path, provider: str | None, destination_country: str | None, seconds: int,
    ) -> int: ...

    async def reserve(
        self, key: str, *, tenant_id: UUID, path: Path, provider: str | None, destination_country: str | None,
        max_seconds: int, max_amount_minor: int | None, expires_at: datetime,
    ) -> Granted | Refused: ...

    async def authorize(self, key: str, *, expires_at: datetime) -> None: ...

    async def settle(self, key: str, *, billable_ms: int) -> Settlement: ...

    async def release(self, key: str) -> None: ...

    async def aclose(self) -> None: ...


class BillingDisabled(RuntimeError):
    """A billing call was made while billing is off; the caller should have checked `enabled`."""


class NullBillingAdmission:
    """Billing off: attempts get billing_state='off', so nothing but account_status may be called."""

    enabled = False

    async def account_status(self, tenant_id: UUID) -> AccountStatus:
        return AccountStatus(has_account=False, balance_minor=None, currency=None, max_hold_s=None)

    async def estimate(self, *args: Any, **kwargs: Any) -> int:
        raise BillingDisabled("estimate")

    async def reserve(self, *args: Any, **kwargs: Any) -> Granted | Refused:
        raise BillingDisabled("reserve")

    async def authorize(self, *args: Any, **kwargs: Any) -> None:
        raise BillingDisabled("authorize")

    async def settle(self, *args: Any, **kwargs: Any) -> Settlement:
        raise BillingDisabled("settle")

    async def release(self, *args: Any, **kwargs: Any) -> None:
        raise BillingDisabled("release")

    async def aclose(self) -> None:
        return None


async def estimate_campaign(
    conn: asyncpg.Connection, billing: BillingAdmission, campaign: dict[str, Any],
) -> CostEstimate | None:
    if not billing.enabled:
        return None
    raise NotImplementedError("pre-start cost estimates arrive with the billing implementation")
