"""LLMAdapter feature detection: generate_with_tools() when tools are offered and supported, else wrapped generate()."""

from __future__ import annotations

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.tools.llm_adapter import LLMAdapter, TokenEvent, ToolCallEvent

_SCHEMAS = [{"name": "book_appointment", "description": "d", "parameters": {"type": "object"}}]


class _PlainLLM:
    """Implements only ILLM — no generate_with_tools() at all."""

    async def generate(self, messages):
        for t in ["Hi", " there"]:
            yield t


class _ToolAwareLLM:
    """Implements ILLM AND IToolAwareLLM."""

    def __init__(self, tool_call: bool) -> None:
        self._tool_call = tool_call

    async def generate(self, messages):
        yield "should not be called when tools offered"

    async def generate_with_tools(self, messages, schemas, tool_choice=None):
        self.seen_schemas = schemas
        self.seen_tool_choice = tool_choice
        if self._tool_call:
            yield ToolCallEvent(tool_call_id="c1", tool_name="book_appointment", arguments={"start_time": "x"})
        else:
            yield TokenEvent(text="plain answer")


async def test_falls_back_to_plain_generate_when_provider_not_tool_aware():
    adapter = LLMAdapter(_PlainLLM())
    events = [e async for e in adapter.generate([ChatMessage(role="user", content="hi")], _SCHEMAS)]

    assert events == [TokenEvent(text="Hi"), TokenEvent(text=" there")]


async def test_falls_back_to_plain_generate_when_no_schemas_offered():
    llm = _ToolAwareLLM(tool_call=False)
    adapter = LLMAdapter(llm)
    events = [e async for e in adapter.generate([ChatMessage(role="user", content="hi")], schemas=None)]

    # Even though llm supports tools, no schemas means plain generate() is used.
    assert events == [TokenEvent(text="should not be called when tools offered")]


async def test_uses_generate_with_tools_when_schemas_offered_and_supported():
    llm = _ToolAwareLLM(tool_call=True)
    adapter = LLMAdapter(llm)
    events = [e async for e in adapter.generate([ChatMessage(role="user", content="book 3pm")], _SCHEMAS)]

    assert len(events) == 1
    assert isinstance(events[0], ToolCallEvent)
    assert events[0].tool_name == "book_appointment"
    assert llm.seen_schemas == _SCHEMAS


async def test_tool_aware_provider_can_still_answer_plainly():
    llm = _ToolAwareLLM(tool_call=False)
    adapter = LLMAdapter(llm)
    events = [e async for e in adapter.generate([ChatMessage(role="user", content="say hi")], _SCHEMAS)]

    assert events == [TokenEvent(text="plain answer")]
