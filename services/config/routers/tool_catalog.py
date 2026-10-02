"""Static tool catalog for the Admin UI's Tools tab (metadata, not tenant data).

Only execute_api is listed: search_knowledge is enabled by linking a knowledge base, and
each integration is a custom_apis row, not a new entry here.
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
                # No API key field: toolexec is internal and each API carries its own auth.
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
