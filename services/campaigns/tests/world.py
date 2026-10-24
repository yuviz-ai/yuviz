"""A tenant, agent, owned caller ID and campaign on a scratch database, plus fakes for the worker's collaborators."""

from __future__ import annotations

import random
import uuid
from typing import Any

from services.campaigns.dial_request import DialIntent
from services.campaigns.tests import seed
from services.campaigns.worker import CampaignWorker


class FakeDial:
    """Stands in for DialRequestClient: records every send and answers from a script."""

    def __init__(self, *results: str) -> None:
        self.results = list(results)
        self.sent: list[tuple[DialIntent, bool]] = []
        self.probe_ok = True

    async def send(self, intent: DialIntent, *, first: bool) -> str:
        self.sent.append((intent, first))
        return self.results.pop(0) if self.results else "accepted"

    async def probe(self) -> bool:
        return self.probe_ok

    async def close(self) -> None:
        return None


class FakeBilling:
    enabled = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def account_status(self, tenant_id):
        self.calls.append("account_status")

    async def aclose(self) -> None:
        return None


def worker(redis_client, dial: FakeDial | None = None, billing: FakeBilling | None = None, **kwargs: Any) -> CampaignWorker:
    return CampaignWorker(
        dial_client=dial or FakeDial(), redis_client=redis_client, billing=billing or FakeBilling(),
        egress_secret=b"egress", **kwargs,
    )


async def make(conn, *, provider: str | None = None, **campaign_kwargs: Any) -> dict[str, Any]:
    """provider None = a native DID (Path A); otherwise a telephony config of that provider (Path B)."""
    tenant_id = await seed.tenant(conn)
    campaign = await seed.campaign(conn, tenant_id, **campaign_kwargs)
    caller_id = f"+1415{random.randrange(10**6, 10**7)}"
    config_id = None
    if provider is not None:
        config_id = await conn.fetchval(
            "INSERT INTO telephony_configs (tenant_id, name, provider) VALUES ($1, 'cfg', $2) RETURNING id",
            tenant_id, provider,
        )
    await conn.execute(
        "INSERT INTO phone_numbers (did, tenant_id, agent_id, telephony_config_id) VALUES ($1, $2, $3, $4)",
        caller_id.lstrip("+"), tenant_id, campaign["agent_id"], config_id,
    )
    campaign = dict(await conn.fetchrow(
        "UPDATE campaigns SET caller_id = $2, pacing_seconds = 0 WHERE id = $1 RETURNING *", campaign["id"], caller_id,
    ))
    return {"tenant_id": tenant_id, "campaign": campaign, "config_id": config_id}


async def refreshed(conn, campaign_id) -> dict[str, Any]:
    return dict(await conn.fetchrow("SELECT * FROM campaigns WHERE id = $1", campaign_id))


async def attempt_rows(conn, campaign_id) -> list[dict[str, Any]]:
    return [dict(r) for r in await conn.fetch(
        "SELECT * FROM campaign_attempts WHERE campaign_id = $1 ORDER BY created_at, attempt", campaign_id,
    )]


async def contact_row(conn, contact_id) -> dict[str, Any]:
    return dict(await conn.fetchrow("SELECT * FROM campaign_contacts WHERE id = $1", contact_id))


async def open_attempt(
    conn, world: dict[str, Any], *, path: str = "sip", state: str = "pending", age_s: float = 0,
    provider: str | None = None, provider_call_id: str | None = None, deadline_in_s: float = 120,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A contact in 'calling' with one open attempt, aged `age_s`, deadline `deadline_in_s` from now."""
    camp = world["campaign"]
    contact = await seed.contact(conn, camp["id"], status="calling", attempt_count=1)
    row = await seed.attempt(
        conn, camp, contact, path=path, state=state, provider_call_id=provider_call_id,
        cps_scope="sip_trunk:pstn" if path == "sip" else f"provider_account:{world['config_id']}",
    )
    await conn.execute(
        "UPDATE campaign_attempts SET callee = $2, caller_id = $3, provider = $4, max_call_s = 900, send_count = 1, "
        "call_session_id = CASE WHEN path = 'sip' THEN gen_random_uuid()::text END, "
        "created_at = now() - make_interval(secs => $5), deadline_at = now() + make_interval(secs => $6) WHERE id = $1",
        row["id"], contact["phone_number"], camp["caller_id"], provider, age_s, deadline_in_s,
    )
    return contact, dict(await conn.fetchrow("SELECT * FROM campaign_attempts WHERE id = $1", row["id"]))


async def add_call(conn, tenant_slug_of: uuid.UUID, session_id: str, *, ended: bool) -> None:
    slug = await conn.fetchval("SELECT slug FROM tenants WHERE id = $1", tenant_slug_of)
    await conn.execute(
        "INSERT INTO calls (session_id, tenant_id, ended_at) VALUES ($1, $2, CASE WHEN $3 THEN now() END)",
        session_id, slug, ended,
    )
