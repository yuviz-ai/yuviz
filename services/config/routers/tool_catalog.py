"""
Static tool catalog for the Admin UI's "Tools" tab — what tools exist and
what an admin needs to fill in to configure one. No database, no auth
required beyond being logged in; this is metadata, not tenant data.

KNOWN DUPLICATION, flagged rather than silently done: this list's
tool_name/display shape overlaps services/conversation/tools/registry.py's
ToolRegistry (the actual LLM-facing schema catalog). They aren't unified
because Config Service and Conversation Service are deliberately separate
deployables with no shared import today (see architecture_decisions:
Gateway/ConvSvc/Config Service responsibility boundaries).

ONE ENTRY, and that is the design (2026-09-18). The agent has exactly two
tools — search_knowledge and execute_api — and only execute_api is
configured here:

  search_knowledge  is not admin-configurable at all. It needs no
                    credential and is already enabled per agent by linking
                    a knowledge base to it on the Knowledge tab, so a
                    second enablement switch here would be a redundant
                    gate that can only ever disagree with the first.

  execute_api       is configured once per agent, then every individual
                    integration is a row in custom_apis (the APIs tab) —
                    NOT a new entry in this file. Adding a capability must
                    never mean shipping conversation-service code.

The Cal.com "book_appointment" and Twilio "send_sms" entries were removed
here along with the built-ins themselves. Appointment booking is now an
ordinary custom API chain like any other integration; an outbound SMS is
a side-effecting custom API.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import CurrentUser
from ..deps import get_current_user

router = APIRouter(prefix="/tools", tags=["tool_catalog"])

_CATALOG = [
    {
        "tool_name": "execute_api",
        "display_name": "Business APIs",
        "description": (
            "Lets the agent call your own systems during a call — to look up an order, check "
            "stock, verify an account, or book something. Turn this on once, then add each API "
            "on the APIs tab; the agent picks between them using the description you write for "
            "each one, so no code change is needed to add a new capability."
        ),
        "category": "custom_api",
        "engines": [
            {
                "engine": "toolexec",
                "display_name": "Tool Execution Service",
                # engine='toolexec' is internal infrastructure, not a tenant
                # credential — each API carries its own auth on the APIs tab,
                # which is why there is no API key field here (see
                # provider_manager.py's _make_toolexec and configs.py's
                # exemption from the usual api_key_ref requirement).
                "extra_fields": [
                    {
                        "key": "max_chain_depth",
                        "label": "Maximum Chain Depth",
                        "type": "number",
                        "required": False,
                        "help": (
                            "How many dependent APIs may run for a single request — an API that "
                            "needs a lookup first counts as 2. Leave blank for the platform "
                            "default. The platform maximum is 4 and this cannot raise it."
                        ),
                    },
                ],
            },
        ],
    },
]


@router.get("/catalog")
async def get_tool_catalog(current_user: CurrentUser = Depends(get_current_user)):
    return _CATALOG
