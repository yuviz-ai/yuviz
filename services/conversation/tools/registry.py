"""ToolRegistry — static catalog of ToolDefinition schemas; per-agent enablement is ToolPolicyResolver's job.
The agent has exactly two tools (search_knowledge, execute_api); new capabilities are custom_apis rows, not new tools."""

from __future__ import annotations

from .types import ToolDefinition

# Local tool supplied by pipeline.py (not DB-gated, so absent from _DEFAULT_TOOLS);
# local calls don't burn the remote tool-iteration budget.
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


# api_name enum and per-API docs are filled per agent by ToolPolicyResolver._specialize_execute_api().
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

# DB-gated tools only.
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
