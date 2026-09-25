from __future__ import annotations

from services.campaigns import dnc


def test_normalize_phone_strips_formatting():
    assert dnc.normalize_phone("+1 (415) 555-0100") == dnc.normalize_phone("4155550100")


async def test_add_and_list_numbers(test_tenant, scoped):
    await dnc.add_number(test_tenant["id"], "+14155550100", reason="opted out")
    numbers = await dnc.list_numbers(test_tenant["id"])
    assert len(numbers) == 1
    assert numbers[0]["phone_number"] == "+14155550100"
    assert numbers[0]["reason"] == "opted out"


async def test_add_number_is_idempotent_per_tenant(test_tenant, scoped):
    first = await dnc.add_number(test_tenant["id"], "+14155550100", reason="opted out")
    second = await dnc.add_number(test_tenant["id"], "+14155550100", reason="requested removal")
    assert first["id"] == second["id"]
    numbers = await dnc.list_numbers(test_tenant["id"])
    assert len(numbers) == 1
    assert numbers[0]["reason"] == "requested removal"


async def test_remove_number(test_tenant, scoped):
    row = await dnc.add_number(test_tenant["id"], "+14155550100")
    await dnc.remove_number(row["id"])
    assert await dnc.list_numbers(test_tenant["id"]) == []


async def test_is_blocked_matches_regardless_of_formatting(test_tenant, scoped):
    await dnc.add_number(test_tenant["id"], "4155550100")
    assert await dnc.is_blocked(test_tenant["id"], "+1 415-555-0100") is True
    assert await dnc.is_blocked(test_tenant["id"], "+14155559999") is False


async def test_is_blocked_false_for_empty_list(test_tenant, scoped):
    assert await dnc.is_blocked(test_tenant["id"], "+14155550100") is False
