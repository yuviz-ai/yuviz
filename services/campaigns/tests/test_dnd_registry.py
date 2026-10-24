from __future__ import annotations

import uuid

import pytest

from services.campaigns.dnd_registry import AllowAllRegistry, IDndRegistry


@pytest.mark.parametrize("purpose", ["promotional", "service", "transactional", None])
async def test_allow_all_registry_allows_any_number_and_purpose(purpose):
    registry: IDndRegistry = AllowAllRegistry()
    assert await registry.check("+919876543210", purpose, uuid.uuid4()) == "allow"
    assert await registry.check("+14155550100", purpose, uuid.uuid4()) == "allow"
