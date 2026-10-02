"""IDidProvider — carrier boundary; routers and DidProviderManager never import a concrete provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AvailableNumber:
    phone_number:  str  # E.164, e.g. "+14155551234"
    region:        str | None = None
    monthly_price: str | None = None  # carrier's own currency string, display-only, never parsed
    capabilities:  tuple[str, ...] = ()  # e.g. ("voice", "sms")


@dataclass(frozen=True)
class PurchasedNumber:
    phone_number:       str
    carrier_number_sid: str  # carrier's own id for this number — needed to release() it later


class DidProviderError(Exception):
    """Any provider-side failure. An empty search is a normal empty list, not this error."""


class IDidProvider(Protocol):
    async def search_available_numbers(
        self, country: str, area_code: str | None = None, limit: int = 10,
    ) -> list[AvailableNumber]:
        """Live, uncached search; an empty list is a valid result."""
        ...

    async def purchase_number(self, phone_number: str) -> PurchasedNumber:
        """Raises DidProviderError on any failure, including the number being taken since search."""
        ...

    async def release_number(self, carrier_number_sid: str) -> None:
        """Return the number to the carrier. Should be idempotent: don't raise for already-released."""
        ...
