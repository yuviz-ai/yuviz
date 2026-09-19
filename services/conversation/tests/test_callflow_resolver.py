"""
resolve_call_flow() — the never-raises degradation contract, mirrored on
agent_resolver.resolve_handler_deps() (see test_agent_resolver.py). No
provider, no I/O: a fake IConfigProvider stands in for Config SDK.
"""

from __future__ import annotations

import dataclasses

import pytest

from libs.config_sdk.models import CallFlow
from libs.config_sdk.providers.mock_provider import MockConfigProvider

from ..agent_resolver import resolve_handler_deps
from ..ai_provider_manager import AIProviderManager, ProviderConfig
from ..callflow.resolver import resolve_call_flow
from ..provider_bundle import ProviderRegistry


async def _fake_factory(cfg: ProviderConfig, api_key: str | None):
    return object()


class FakeSecretResolver:
    async def resolve(self, ref: str) -> str:
        return f"resolved:{ref}"


FAKE_REGISTRY = {
    ("stt", "fake_stt"): _fake_factory,
    ("llm", "fake_llm"): _fake_factory,
    ("tts", "fake_tts"): _fake_factory,
}

VALID_GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "start", "type": "start", "data": {"name": "start", "prompt": ""}},
        {"id": "end", "type": "hangup", "data": {"name": "end", "prompt": "Bye."}},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "end"}],
}

INVALID_GRAPH = {"version": 1, "nodes": [], "edges": []}  # no start node


class FakeConfigProvider:
    """get_runtime_config() delegates to a real MockConfigProvider; get_
    call_flow() is what each test controls directly — this is the seam
    resolve_call_flow() actually calls."""

    def __init__(self, mock: MockConfigProvider, call_flow: CallFlow | None = None,
                 raise_on_get: Exception | None = None) -> None:
        self._mock = mock
        self._call_flow = call_flow
        self._raise_on_get = raise_on_get

    async def get_runtime_config(self, tenant_slug: str, agent_slug: str):
        return await self._mock.get_runtime_config(tenant_slug, agent_slug)

    async def get_call_flow(self, tenant_slug: str, call_flow_id: str) -> CallFlow | None:
        if self._raise_on_get is not None:
            raise self._raise_on_get
        return self._call_flow


async def _runtime_config(call_flow_id: str | None):
    mock = MockConfigProvider()
    mock.add_tenant(
        slug="acme", name="Acme",
        default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
    )
    mock.add_agent("acme", slug="sup", name="Sup", greeting="Hi", system_prompt="Be helpful.")
    mock.add_provider_config(id="stt1", role="stt", engine="fake_stt")
    mock.add_provider_config(id="llm1", role="llm", engine="fake_llm")
    mock.add_provider_config(id="tts1", role="tts", engine="fake_tts")
    registry = ProviderRegistry(AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY))
    resolved = await resolve_handler_deps("acme", "sup", registry, mock)
    assert resolved is not None
    runtime_config, _ = resolved
    return dataclasses.replace(
        runtime_config, agent=dataclasses.replace(runtime_config.agent, call_flow_id=call_flow_id),
    ), mock


async def test_no_call_flow_id_returns_none():
    runtime_config, mock = await _runtime_config(call_flow_id=None)
    result = await resolve_call_flow(runtime_config, FakeConfigProvider(mock))
    assert result is None


async def test_provider_miss_returns_none():
    runtime_config, mock = await _runtime_config(call_flow_id="flow-1")
    result = await resolve_call_flow(runtime_config, FakeConfigProvider(mock, call_flow=None))
    assert result is None


async def test_invalid_graph_returns_none_and_does_not_raise():
    runtime_config, mock = await _runtime_config(call_flow_id="flow-1")
    flow = CallFlow(id="flow-1", tenant_slug="acme", config_version=1, graph=INVALID_GRAPH)
    result = await resolve_call_flow(runtime_config, FakeConfigProvider(mock, call_flow=flow))
    assert result is None


async def test_arbitrary_provider_exception_returns_none_and_does_not_propagate():
    runtime_config, mock = await _runtime_config(call_flow_id="flow-1")
    provider = FakeConfigProvider(mock, raise_on_get=RuntimeError("config service unreachable"))
    # The assertion IS that this doesn't raise — a bare `except Exception`
    # that let anything through would fail this call, not a follow-up assert.
    result = await resolve_call_flow(runtime_config, provider)
    assert result is None


async def test_valid_flow_returns_graph_and_call_flow():
    runtime_config, mock = await _runtime_config(call_flow_id="flow-1")
    flow = CallFlow(id="flow-1", tenant_slug="acme", config_version=1, graph=VALID_GRAPH,
                     resolved_tts_config_id="tts-2")
    result = await resolve_call_flow(runtime_config, FakeConfigProvider(mock, call_flow=flow))

    assert result is not None
    graph, returned_flow = result
    assert graph.start_node_id == "start"
    assert returned_flow is flow
