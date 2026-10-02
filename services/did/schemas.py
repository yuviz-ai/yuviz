"""Pydantic request models for DID Service; responses are plain dicts."""

from __future__ import annotations

from pydantic import BaseModel


class PurchaseNumberRequest(BaseModel):
    carrier_id:   str
    phone_number: str  # E.164 — must be one of the numbers a prior search returned
