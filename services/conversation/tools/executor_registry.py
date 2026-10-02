"""Tool name -> executor factory; kept separate from ToolRegistry so LLM-facing code never imports it."""

from __future__ import annotations

from typing import Any, Callable, Protocol

from .types import ToolExecutionRequest, ToolResult


class IToolExecutor(Protocol):
    async def execute(self, request: ToolExecutionRequest) -> ToolResult: ...


# One positional arg: the provider this tool's policy resolved to.
ExecutorFactory = Callable[[Any], IToolExecutor]


class ExecutorRegistry:
    def __init__(self, factories: dict[str, ExecutorFactory] | None = None) -> None:
        self._factories: dict[str, ExecutorFactory] = dict(factories) if factories else {}

    def register(self, tool_name: str, factory: ExecutorFactory) -> None:
        self._factories[tool_name] = factory

    def resolve(self, tool_name: str, provider: Any) -> IToolExecutor | None:
        factory = self._factories.get(tool_name)
        if factory is None:
            return None
        return factory(provider)
