"""
ToolRegistry — static, DB-unaware catalog of ToolDefinition (schemas only).

Deliberately mirrors ai_provider_manager.py's _DEFAULT_REGISTRY shape: a
module-level dict populated at import time, not queried from Postgres.
Whether a given agent may actually USE a registered tool is
ToolPolicyResolver's job (policy_resolver.py), not this class's — see the
Tool Execution Framework design's review point 7 for why that split exists.

THE AGENT HAS EXACTLY TWO TOOLS (2026-09-18). Both are defined in this
file, and the LLM's whole job is to pick between them from what the caller
just said:

  search_knowledge  — read the business's own documents (RAG).
  execute_api       — call the business's own systems (services/toolexec).

Neither is a per-capability tool, and no third one should be added. A new
capability is a new row in custom_apis, whose `description` is what routes
the model to it — never a new entry here. That is the point of the split:
the tool surface is fixed and small, so the model's choice is a two-way
decision it can make reliably, while the long tail of what a tenant can
actually do lives in data the tenant controls.

The Cal.com/Twilio built-ins (book_appointment, cancel_appointment,
reschedule_appointment, send_sms) were removed in this same change. They
were a first-class tool per capability, which is exactly the shape this
module no longer has; appointment booking is now an ordinary custom API
chain like any other integration.
"""

from __future__ import annotations

from .types import ToolDefinition

# search_knowledge — the RAG half of the two-tool surface.
#
# Supplied as an in-process LOCAL tool by pipeline.py, NOT through
# agent_tool_policies, so it is deliberately absent from _DEFAULT_TOOLS
# below (ToolPolicyResolver would never resolve it — there is no provider
# config to resolve). It lives here anyway so both tools the model can
# ever see are defined in one file.
#
# Local rather than DB-gated for two concrete reasons:
#   1. It needs no credential and no provider — the knowledge provider is
#      already constructed in __main__.py and already scoped per agent by
#      its KB links, so a tool_provider_configs row would be pure
#      ceremony and a second, redundant enablement gate.
#   2. Local tools do not burn the remote tool-iteration budget (see
#      orchestrator.py's `iteration += 1  # remote only`). A caller asking
#      something that needs a document lookup AND a live API call still
#      gets both inside one turn.
#
# The description carries the anti-hallucination contract directly,
# because this is the tool whose absence of a result is most likely to be
# filled in from the model's own general knowledge.
SEARCH_KNOWLEDGE = ToolDefinition(
    name="search_knowledge",
    description=(
        "Search this business's own documents for the answer to what the caller asked. "
        "Use it for anything the business would have written down — policies, hours, "
        "pricing rules, services, eligibility, terms, how something works. "
        "Prefer this over answering from memory: your general knowledge is not this "
        "business's, and a plausible-sounding answer that did not come from these "
        "documents is wrong even when it happens to be true. "
        "If it returns no passages, tell the caller you don't have that information "
        "and offer to connect them — never fill the gap yourself."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What to look up, in the caller's own words. Keep the caller's "
                    "phrasing rather than rewriting it into your own terms."
                ),
            },
        },
        "required": ["query"],
    },
    category="knowledge",
)


# execute_api — the ONE LLM-facing entry for every tenant-registered custom
# API (services/toolexec/). api_name's enum is empty here; ToolPolicyResolver
# ._specialize_execute_api() fills it in per agent at resolve time with that
# agent's enabled APIs (and appends their leaf-input docs to the
# description) — see policy_resolver.py. An agent with zero enabled custom
# APIs never gets this tool at all, so the LLM never sees an empty enum.
#
# Which API to call is decided ENTIRELY by the per-API `description` text
# injected at specialization time, never by anything written here. Those
# descriptions are therefore written as instructions ("Call this whenever
# the caller asks what something costs...") rather than as nouns.
_EXECUTE_API = ToolDefinition(
    name="execute_api",
    description=(
        "Call one of this business's own systems to look something up or to do something "
        "for the caller. Use it for live, caller-specific facts — an order, an account, a "
        "price, stock, an appointment — anything that changes per caller or per day and so "
        "could not be written in a document. Pick api_name from the list below and supply "
        "only the inputs it says come from the caller; anything a prior system must provide "
        "is fetched automatically — never ask the caller for it and never claim a result "
        "this function did not return."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "api_name": {"type": "string", "enum": []},
            "inputs": {"type": "object", "description": "Leaf inputs this api_name needs from the caller."},
        },
        "required": ["api_name"],
    },
    category="custom_api",
)

# DB-gated tools only — what ToolPolicyResolver can resolve an
# agent_tool_policies row against. search_knowledge is intentionally not
# here; see its own comment above.
_DEFAULT_TOOLS: dict[str, ToolDefinition] = {
    _EXECUTE_API.name: _EXECUTE_API,
}


class ToolRegistry:
    def __init__(self, tools: dict[str, ToolDefinition] | None = None) -> None:
        self._tools = dict(_DEFAULT_TOOLS) if tools is None else dict(tools)

    def register(self, definition: ToolDefinition) -> None:
        self._tools[definition.name] = definition

    def resolve(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def all(self) -> list[ToolDefinition]:
        return list(self._tools.values())
