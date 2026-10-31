"""Provider HTTP clients must not be capped at httpx's default 100 connections (issue #191)."""

from __future__ import annotations

import pytest

from services.conversation.providers.http_pool import (
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_MAX_KEEPALIVE,
    provider_http_limits,
)
from services.conversation.providers.llm.anthropic import AnthropicLLM
from services.conversation.providers.llm.gemini import GeminiLLM
from services.conversation.providers.llm.openai import OpenAILLM
from services.conversation.providers.stt.deepgram import DeepgramSTT
from services.conversation.providers.tts.cartesia import CartesiaTTS
from services.conversation.providers.tts.deepgram import DeepgramTTS
from services.conversation.providers.tts.elevenlabs import ElevenLabsTTS
from services.conversation.providers.tts.sarvam import SarvamTTS

_ENVS = ("PROVIDER_HTTP_MAX_CONNECTIONS", "PROVIDER_HTTP_MAX_KEEPALIVE")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENVS:
        monkeypatch.delenv(k, raising=False)


def _pool_max(client) -> int:
    return client._transport._pool._max_connections


def _pool_keepalive(client) -> int:
    return client._transport._pool._max_keepalive_connections


def test_default_exceeds_httpx_default():
    lim = provider_http_limits()
    assert lim.max_connections == DEFAULT_MAX_CONNECTIONS > 100
    assert lim.max_keepalive_connections == DEFAULT_MAX_KEEPALIVE


def test_env_override(monkeypatch):
    monkeypatch.setenv("PROVIDER_HTTP_MAX_CONNECTIONS", "500")
    monkeypatch.setenv("PROVIDER_HTTP_MAX_KEEPALIVE", "50")
    lim = provider_http_limits()
    assert (lim.max_connections, lim.max_keepalive_connections) == (500, 50)


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "", "  ", "1.5"])
def test_bad_env_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv("PROVIDER_HTTP_MAX_CONNECTIONS", bad)
    monkeypatch.setenv("PROVIDER_HTTP_MAX_KEEPALIVE", bad)
    lim = provider_http_limits()
    assert lim.max_connections == DEFAULT_MAX_CONNECTIONS
    assert lim.max_keepalive_connections == DEFAULT_MAX_KEEPALIVE


def test_keepalive_never_exceeds_max(monkeypatch):
    monkeypatch.setenv("PROVIDER_HTTP_MAX_CONNECTIONS", "10")
    lim = provider_http_limits()
    assert lim.max_connections == 10
    assert lim.max_keepalive_connections == 10


_BUILDERS = {
    "openai": lambda: OpenAILLM(api_key="k"),
    "anthropic": lambda: AnthropicLLM(api_key="k"),
    "gemini": lambda: GeminiLLM(api_key="k"),
    "deepgram_tts": lambda: DeepgramTTS(api_key="k"),
    "elevenlabs": lambda: ElevenLabsTTS(api_key="k", voice_id="v"),
    "cartesia": lambda: CartesiaTTS(api_key="k", voice="v"),
    "sarvam": lambda: SarvamTTS(api_key="k"),
    "deepgram_stt": lambda: DeepgramSTT(api_key="k"),
}


@pytest.mark.parametrize("name", sorted(_BUILDERS))
def test_every_provider_client_uses_sized_pool(name, monkeypatch):
    monkeypatch.setenv("PROVIDER_HTTP_MAX_CONNECTIONS", "777")
    monkeypatch.setenv("PROVIDER_HTTP_MAX_KEEPALIVE", "77")
    p = _BUILDERS[name]()
    assert _pool_max(p._client) == 777
    assert _pool_keepalive(p._client) == 77


@pytest.mark.parametrize("name", sorted(_BUILDERS))
def test_every_provider_client_default_over_100(name):
    assert _pool_max(_BUILDERS[name]()._client) == DEFAULT_MAX_CONNECTIONS
