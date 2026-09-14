"""
to_runtime_config() is the legacy-fallback adapter that lets
PipelineConversationHandler have exactly one construction contract
regardless of whether config came from the real Config SDK path or the
legacy YAML path (see pipeline.py, __main__.py). The one behavior that
genuinely matters here: agent.id="" / version=0 must round-trip through
PipelineConversationHandler's `or None` handling to keep
TranscriptBuilder.begin_call() receiving agent_id=None for a legacy-path
call — calls.agent_id is a real UUID FK, so a fake sentinel string would
break the insert outright, not just be cosmetically wrong.
"""

from __future__ import annotations

from ..agent_config import AgentConfig, to_runtime_config
from ..provider_bundle import ProviderBundle


def test_to_runtime_config_carries_agent_fields_through():
    agent = AgentConfig(name="Test", greeting="Hi", system_prompt="Help.", goodbye_grace_period_ms=1500)

    runtime_config, bundle = to_runtime_config(agent, "acme", "sup", stt="fake-stt", llm="fake-llm", tts="fake-tts")

    assert runtime_config.conversation.greeting == "Hi"
    assert runtime_config.conversation.system_prompt == "Help."
    start = next(n for n in runtime_config.conversation.workflow["nodes"] if n["type"] == "start")
    assert start["data"]["greeting"] == "Hi"
    assert runtime_config.policies.goodbye_grace_ms == 1500
    assert isinstance(bundle, ProviderBundle)
    assert bundle.stt == "fake-stt" and bundle.llm == "fake-llm" and bundle.tts == "fake-tts"


def test_to_runtime_config_agent_id_and_version_are_falsy_sentinels():
    # Not None (RuntimeConfig.agent.id/version are non-optional fields) but
    # falsy — PipelineConversationHandler's `runtime_config.agent.id or
    # None` is what turns this into the real agent_id=None TranscriptBuilder
    # needs. See module docstring.
    agent = AgentConfig()
    runtime_config, _ = to_runtime_config(agent, "acme", "sup", stt=None, llm=None, tts=None)

    assert runtime_config.agent.id == ""
    assert not runtime_config.agent.id
    assert runtime_config.version == 0
    assert not runtime_config.version


def test_to_runtime_config_tools_and_optional_policy_fields_are_honestly_empty():
    agent = AgentConfig()
    runtime_config, _ = to_runtime_config(agent, "acme", "sup", stt=None, llm=None, tts=None)

    assert runtime_config.tools == []
    assert runtime_config.policies.barge_in_enabled is None
    assert runtime_config.policies.max_call_duration_s is None
    assert runtime_config.media.voice is None
