from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.campaigns.schemas import CampaignCreate, CampaignUpdate

_BASE = {"agent_id": "a", "name": "n"}


@pytest.mark.parametrize("field", ["max_attempts", "max_concurrent_calls", "retry_gap_minutes"])
@pytest.mark.parametrize("bad", [0, -1])
def test_create_rejects_non_positive_counts(field, bad):
    with pytest.raises(ValidationError, match=field):
        CampaignCreate(**_BASE, **{field: bad})


@pytest.mark.parametrize("field", ["max_attempts", "max_concurrent_calls", "retry_gap_minutes"])
@pytest.mark.parametrize("bad", [0, -1])
def test_update_rejects_non_positive_counts(field, bad):
    with pytest.raises(ValidationError, match=field):
        CampaignUpdate(**{field: bad})


@pytest.mark.parametrize("model, kwargs", [(CampaignCreate, _BASE), (CampaignUpdate, {})])
@pytest.mark.parametrize("bad", ["Mars/Base", "", "not a zone"])
def test_unknown_timezone_is_rejected(model, kwargs, bad):
    with pytest.raises(ValidationError, match="timezone"):
        model(**kwargs, calling_hours_timezone=bad)


def test_defaults_apply():
    campaign = CampaignCreate(**_BASE)
    assert (campaign.retry_gap_minutes, campaign.max_attempts, campaign.max_concurrent_calls) == (60, 1, 1)
    assert campaign.calling_hours_timezone == "UTC"
    assert CampaignUpdate().model_dump(exclude_none=True) == {}


def test_the_new_campaign_form_payload_still_validates():
    # Initial state of admin-ui/app/(console)/campaigns/new/page.tsx, plus the values a user can pick there.
    payload = {
        "agent_id": "a", "name": "Renewals", "scheduled_start_at": None, "max_concurrent_calls": 1,
        "pacing_seconds": 5, "max_attempts": 1, "calling_hours_start": None, "calling_hours_end": None,
        "calling_hours_timezone": "UTC",
    }
    assert CampaignCreate(**payload).retry_gap_minutes == 60
    for zone in ("Asia/Kolkata", "America/New_York", "Europe/London"):
        assert CampaignCreate(**{**payload, "calling_hours_timezone": zone}).calling_hours_timezone == zone
    assert CampaignUpdate(**{k: v for k, v in payload.items() if k != "agent_id"}).max_attempts == 1


def test_route_returns_422_for_a_bad_payload():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.post("/c")
    def create(body: CampaignCreate):
        return body.model_dump()

    client = TestClient(app)
    assert client.post("/c", json={**_BASE, "max_attempts": 0}).status_code == 422
    assert client.post("/c", json={**_BASE, "calling_hours_timezone": "Mars/Base"}).status_code == 422
    assert client.post("/c", json=_BASE).status_code == 200
