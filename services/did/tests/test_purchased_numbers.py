from __future__ import annotations

import pytest

from services.did import purchased_numbers

pytestmark = pytest.mark.integration


async def test_record_purchase_and_get(test_tenant, test_carrier):
    created = await purchased_numbers.record_purchase(
        tenant_id=test_tenant["id"], carrier_id=test_carrier["id"],
        phone_number="+14155550100", carrier_number_sid="PNxxxx",
    )
    assert created["phone_number"] == "+14155550100"
    assert created["phone_number_id"] is None
    assert created["released_at"] is None

    fetched = await purchased_numbers.get_purchased_number(created["id"])
    assert fetched is not None
    assert fetched["carrier_number_sid"] == "PNxxxx"


async def test_list_purchased_numbers_excludes_released(test_tenant, test_carrier):
    a = await purchased_numbers.record_purchase(
        tenant_id=test_tenant["id"], carrier_id=test_carrier["id"],
        phone_number="+14155550101", carrier_number_sid="PN0001",
    )
    b = await purchased_numbers.record_purchase(
        tenant_id=test_tenant["id"], carrier_id=test_carrier["id"],
        phone_number="+14155550102", carrier_number_sid="PN0002",
    )
    await purchased_numbers.record_release(a["id"])

    listed = await purchased_numbers.list_purchased_numbers(test_tenant["id"])
    assert [p["id"] for p in listed] == [b["id"]]


async def test_record_assignment_links_phone_number_id(test_tenant, test_carrier, pool):
    purchased = await purchased_numbers.record_purchase(
        tenant_id=test_tenant["id"], carrier_id=test_carrier["id"],
        phone_number="+14155550103", carrier_number_sid="PN0003",
    )
    phone_number_row = await pool.fetchrow(
        "INSERT INTO phone_numbers (did, tenant_id) VALUES ($1, $2) RETURNING *",
        "+14155550103", test_tenant["id"],
    )

    await purchased_numbers.record_assignment(purchased["id"], phone_number_row["id"])

    fetched = await purchased_numbers.get_purchased_number(purchased["id"])
    assert fetched["phone_number_id"] == phone_number_row["id"]


async def test_release_nonexistent_raises_lookup_error(test_tenant, test_carrier):
    with pytest.raises(LookupError):
        await purchased_numbers.record_release("00000000-0000-0000-0000-000000000000")
