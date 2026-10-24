from __future__ import annotations

import pytest

from services.campaigns import campaign_contacts, campaigns


def test_parse_contacts_csv_basic():
    content = b"phone_number,name\n+14155551111,Alice\n+14155552222,Bob\n"
    contacts = campaign_contacts.parse_contacts_csv(content)
    assert contacts == [
        {"phone_number": "+14155551111", "name": "Alice"},
        {"phone_number": "+14155552222", "name": "Bob"},
    ]


def test_parse_contacts_csv_skips_blank_phone_rows():
    content = b"phone_number,name\n+14155551111,Alice\n,Blank\n"
    contacts = campaign_contacts.parse_contacts_csv(content)
    assert contacts == [{"phone_number": "+14155551111", "name": "Alice"}]


def test_parse_contacts_csv_case_insensitive_header():
    content = b"Phone_Number\n+14155551111\n"
    contacts = campaign_contacts.parse_contacts_csv(content)
    assert contacts == [{"phone_number": "+14155551111", "name": ""}]


def test_parse_contacts_csv_no_name_column():
    content = b"phone_number\n+14155551111\n"
    contacts = campaign_contacts.parse_contacts_csv(content)
    assert contacts == [{"phone_number": "+14155551111", "name": ""}]


def test_parse_contacts_csv_missing_phone_column_raises():
    content = b"name\nAlice\n"
    with pytest.raises(ValueError, match="phone_number"):
        campaign_contacts.parse_contacts_csv(content)


def test_parse_contacts_csv_strips_bom():
    content = "﻿phone_number\n+14155551111\n".encode("utf-8")
    contacts = campaign_contacts.parse_contacts_csv(content)
    assert contacts == [{"phone_number": "+14155551111", "name": ""}]


def test_parse_contacts_csv_strips_common_separators():
    content = b'phone_number\n"+1 (415) 555-1111"\n"+1 (415) 555-2222"\n'
    contacts = campaign_contacts.parse_contacts_csv(content)
    assert [c["phone_number"] for c in contacts] == ["+14155551111", "+14155552222"]


def test_parse_contacts_csv_rejects_multiline_injection():
    content = b'phone_number,name\n+14155551111,Alice\n"5551234\n\napi hupall",Mallory\n'
    with pytest.raises(ValueError, match=r"row\(s\) 3:"):
        campaign_contacts.parse_contacts_csv(content)


def test_parse_contacts_csv_rejects_any_non_digit_and_names_every_row():
    content = b"phone_number\n+14155551111\n555@evil\n+14155552222\nabc\n"
    with pytest.raises(ValueError, match=r"row\(s\) 3, 5:"):
        campaign_contacts.parse_contacts_csv(content)


def test_parse_contacts_csv_reports_spreadsheet_row_after_multiline_cell():
    content = b'phone_number,name\n+14155551001,"Acme\nBilling"\nxx,c\n'
    with pytest.raises(ValueError, match=r"row\(s\) 3:"):
        campaign_contacts.parse_contacts_csv(content)


async def test_release_claim_returns_contact_to_pending_without_counting_the_attempt(test_tenant, test_agent, scoped):
    campaign = await _make_campaign(test_tenant, test_agent)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])
    claimed = await campaign_contacts.claim_next_pending(campaign["id"])
    assert claimed["attempt_count"] == 1

    await campaign_contacts.release_claim(claimed["id"])

    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "pending"
    assert contacts[0]["attempt_count"] == 0


async def _make_campaign(test_tenant, test_agent):
    return await campaigns.create_campaign(
        test_tenant["id"], agent_id=test_agent["id"], name="Contacts test",
        caller_id="+14155550100", max_concurrent_calls=2, pacing_seconds=5, max_attempts=1,
    )


async def test_bulk_insert_and_list_contacts(test_tenant, test_agent, scoped):
    campaign = await _make_campaign(test_tenant, test_agent)
    inserted = await campaign_contacts.bulk_insert_contacts(
        campaign["id"], [{"phone_number": "+14155551111", "name": "Alice"}],
    )
    assert inserted == 1

    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert len(contacts) == 1
    assert contacts[0]["status"] == "pending"


async def test_bulk_insert_empty_list_is_noop(test_tenant, test_agent, scoped):
    campaign = await _make_campaign(test_tenant, test_agent)
    assert await campaign_contacts.bulk_insert_contacts(campaign["id"], []) == 0


async def test_claim_next_pending_marks_calling_and_increments_attempts(test_tenant, test_agent, scoped):
    campaign = await _make_campaign(test_tenant, test_agent)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    claimed = await campaign_contacts.claim_next_pending(campaign["id"])
    assert claimed["status"] == "calling"
    assert claimed["attempt_count"] == 1
    assert claimed["last_attempted_at"] is not None


async def test_claim_next_pending_returns_none_when_empty(test_tenant, test_agent, scoped):
    campaign = await _make_campaign(test_tenant, test_agent)
    assert await campaign_contacts.claim_next_pending(campaign["id"]) is None


async def test_claim_next_pending_does_not_reclaim_already_calling(test_tenant, test_agent, scoped):
    campaign = await _make_campaign(test_tenant, test_agent)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    first = await campaign_contacts.claim_next_pending(campaign["id"])
    assert first is not None
    second = await campaign_contacts.claim_next_pending(campaign["id"])
    assert second is None


async def test_mark_contact_status_updates_status_and_session(test_tenant, test_agent, scoped):
    campaign = await _make_campaign(test_tenant, test_agent)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])
    claimed = await campaign_contacts.claim_next_pending(campaign["id"])

    await campaign_contacts.mark_contact_status(claimed["id"], "completed", call_session_id="sess-123")

    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "completed"
    assert contacts[0]["call_session_id"] == "sess-123"
