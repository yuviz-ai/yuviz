"""An agent's outbound caller ID must be an E.164 number its tenant owns; an empty value clears it."""

import uuid

import pytest

from services.config import agents


async def _agent(test_tenant):
    return await agents.create_agent(
        tenant_id=test_tenant["id"], slug=f"ob-{uuid.uuid4().hex[:8]}", name="Outbound",
        direction="outbound", status="inactive",
    )


@pytest.mark.asyncio
async def test_an_owned_number_is_saved_and_an_empty_value_clears_it(pool, test_tenant, scoped):
    number = f"9198{uuid.uuid4().int % 10**8:08d}"
    await pool.execute("INSERT INTO phone_numbers (did, tenant_id) VALUES ($1, $2)", number, test_tenant["id"])
    agent = await _agent(test_tenant)

    saved = await agents.update_agent(agent["id"], tenant_slug=test_tenant["slug"], outbound_caller_id=f"+{number}")
    assert saved["outbound_caller_id"] == f"+{number}"

    cleared = await agents.update_agent(agent["id"], tenant_slug=test_tenant["slug"], outbound_caller_id="")
    assert cleared["outbound_caller_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value, message", [("5005", "E.164"), ("+919000000001", "owns")])
async def test_a_number_that_is_not_e164_or_not_owned_is_refused(test_tenant, scoped, value, message):
    agent = await _agent(test_tenant)
    with pytest.raises(ValueError, match=message):
        await agents.update_agent(agent["id"], tenant_slug=test_tenant["slug"], outbound_caller_id=value)
