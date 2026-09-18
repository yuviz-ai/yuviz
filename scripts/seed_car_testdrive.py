#!/usr/bin/env python3
"""
Seeds the 'default' tenant with a multi-stage Car Test Drive Booking
workflow (slug: car-testdrive). Idempotent — safe to run more than once.

Goes through the Config Service audited write path (create / publish),
not raw SQL.

Usage: python3 scripts/seed_car_testdrive.py
Requires: POSTGRES_DSN, REDIS_URL (same as seed_default_config.py)

Docker (stack already up):
  docker compose -f deployment/docker/docker-compose.yml --project-directory . \\
    exec -T config python /app/scripts/seed_car_testdrive.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.config import agents, tenants, workflows  # noqa: E402

TENANT_SLUG = "default"
AGENT_SLUG = "car-testdrive"
AGENT_NAME = "Car Test Drive Booking"

GRAPH = {
    "version": 1,
    "nodes": [
        {
            "id": "global",
            "type": "global",
            "position": {"x": 420, "y": 0},
            "data": {
                "name": "always applies",
                "prompt": (
                    "You are the phone receptionist for Apex Motors, a car dealership. "
                    "Speak in short, natural sentences (1-2). No markdown or lists. "
                    "Be warm and helpful. Never invent inventory you were not told. "
                    "If asked about price, say a salesperson will confirm at the visit."
                ),
            },
        },
        {
            "id": "n_start",
            "type": "start",
            "position": {"x": 40, "y": 120},
            "data": {
                "name": "greeting",
                "greeting": (
                    "Hi, thanks for calling Apex Motors! I can help you book a test drive, "
                    "or answer a quick question. What would you like to do?"
                ),
                "prompt": (
                    "Greet the caller. Figure out if they want to book a test drive, "
                    "ask a general question, or speak with a salesperson. "
                    "Keep it brief and ask one clear question."
                ),
                "tools": [],
                "knowledge_base_ids": [],
            },
        },
        {
            "id": "n_qualify",
            "type": "agent",
            "position": {"x": 40, "y": 320},
            "data": {
                "name": "qualify interest",
                "prompt": (
                    "Help them pick a vehicle for a test drive. Ask which model they are "
                    "interested in (SUV, sedan, truck, or a specific name). Then ask roughly "
                    "when they want to come in (today, tomorrow, or a preferred day). "
                    "Collect one piece of info at a time. When you have a model interest and "
                    "a rough time window, move on."
                ),
                "extraction": {
                    "enabled": True,
                    "prompt": "Capture what vehicle they want to try and when they prefer to visit.",
                    "variables": [
                        {
                            "name": "vehicle_interest",
                            "type": "string",
                            "prompt": "Model or type they want to test drive",
                        },
                        {
                            "name": "preferred_day",
                            "type": "string",
                            "prompt": "Preferred day or time window",
                        },
                    ],
                },
            },
        },
        {
            "id": "n_book",
            "type": "agent",
            "position": {"x": 40, "y": 520},
            "data": {
                "name": "book appointment",
                "prompt": (
                    "Finish booking the test drive. Confirm vehicle_interest="
                    "{{ vehicle_interest | the vehicle }} "
                    "and preferred_day={{ preferred_day | their preferred time }}. "
                    "Ask for their full name and a callback phone number if not already given. "
                    "Read back: name, phone, vehicle, and day/time. Ask them to confirm. "
                    "Only proceed once they clearly confirm."
                ),
                "extraction": {
                    "enabled": True,
                    "prompt": "Capture booking contact details.",
                    "variables": [
                        {
                            "name": "customer_name",
                            "type": "string",
                            "prompt": "Caller's full name",
                        },
                        {
                            "name": "callback_phone",
                            "type": "string",
                            "prompt": "Phone number to reach them",
                        },
                    ],
                },
            },
        },
        {
            "id": "n_end_booked",
            "type": "end",
            "position": {"x": 40, "y": 720},
            "data": {
                "name": "booked goodbye",
                "disposition": "qualified",
                "prompt": (
                    "Confirm the test drive is booked for {{ customer_name | the customer }} "
                    "to try {{ vehicle_interest | the vehicle }} on "
                    "{{ preferred_day | the agreed time }}. "
                    "Say someone from Apex Motors may text a reminder. Thank them and end warmly."
                ),
            },
        },
        {
            "id": "n_end_info",
            "type": "end",
            "position": {"x": 360, "y": 320},
            "data": {
                "name": "info goodbye",
                "disposition": "completed",
                "prompt": (
                    "Wrap up after answering their question. Offer that they can call back anytime "
                    "to book a test drive. Thank them and say goodbye."
                ),
            },
        },
        {
            "id": "n_transfer",
            "type": "transfer",
            "position": {"x": 360, "y": 520},
            "data": {
                "name": "to sales",
                "prompt": (
                    "Tell the caller you are connecting them to a salesperson now. "
                    "Keep it to one short sentence."
                ),
                "transfer_destination": "+15555550100",
            },
        },
    ],
    "edges": [
        {
            "id": "e_start_qualify",
            "source": "n_start",
            "target": "n_qualify",
            "data": {
                "label": "wants test drive",
                "condition": "The caller wants to book, schedule, or reserve a test drive.",
                "transition_speech": "Great — let's get that test drive set up.",
            },
        },
        {
            "id": "e_start_info",
            "source": "n_start",
            "target": "n_end_info",
            "data": {
                "label": "just a question",
                "condition": (
                    "The caller only had a general question that is now answered, "
                    "and they do not want to book a test drive."
                ),
            },
        },
        {
            "id": "e_start_human",
            "source": "n_start",
            "target": "n_transfer",
            "data": {
                "label": "wants salesperson",
                "condition": (
                    "The caller asks to speak with a human, salesperson, or someone on the lot."
                ),
            },
        },
        {
            "id": "e_qualify_book",
            "source": "n_qualify",
            "target": "n_book",
            "data": {
                "label": "ready to book",
                "condition": (
                    "You know which vehicle or type they want to try, "
                    "and roughly when they want to visit."
                ),
                "transition_speech": "Perfect. I'll just need a couple of details to lock it in.",
            },
        },
        {
            "id": "e_qualify_human",
            "source": "n_qualify",
            "target": "n_transfer",
            "data": {
                "label": "needs sales help",
                "condition": (
                    "The caller is unsure which vehicle to try and asks for a salesperson, "
                    "or the request is too complex for booking alone."
                ),
            },
        },
        {
            "id": "e_book_done",
            "source": "n_book",
            "target": "n_end_booked",
            "data": {
                "label": "booking confirmed",
                "condition": (
                    "The caller confirmed name, callback number, vehicle, "
                    "and day or time for the test drive."
                ),
            },
        },
    ],
}


async def main() -> None:
    tenant = await tenants.get_tenant(TENANT_SLUG)
    if tenant is None:
        raise SystemExit(
            f"tenant {TENANT_SLUG!r} missing — run scripts/seed_default_config.py "
            "(or deployment/sh/init.sh) first"
        )

    existing = await agents.get_agent(TENANT_SLUG, AGENT_SLUG)
    if existing is None:
        agent = await agents.create_agent(
            tenant_id=tenant["id"],
            slug=AGENT_SLUG,
            name=AGENT_NAME,
            workflow=GRAPH,
            user_email="seed@local",
        )
        print(f"created agent {AGENT_SLUG!r} ({agent['id']})")
    else:
        result = await workflows.publish(
            existing["id"],
            tenant_slug=TENANT_SLUG,
            graph=GRAPH,
            note="refresh car test-drive demo workflow",
            user_email="seed@local",
        )
        print(
            f"updated agent {AGENT_SLUG!r} ({existing['id']}) "
            f"version={result.get('version')}"
        )

    live = await agents.get_agent(TENANT_SLUG, AGENT_SLUG)
    assert live is not None
    print(json.dumps({
        "tenant": TENANT_SLUG,
        "slug": live["slug"],
        "name": live["name"],
        "id": str(live["id"]),
        "ui": f"/workflows/{TENANT_SLUG}/{AGENT_SLUG}",
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
