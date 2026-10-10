import pytest

from libs.config_sdk import ProviderConfig as SDKProviderConfig
from libs.config_sdk import ProviderConfigs

from services.conversation.provider_bundle import ProviderRegistry
from services.conversation.providers.llm.retry import RetryOnceLLM


def _sdk_cfg(id_, role, engine, model=None):
    return SDKProviderConfig(id=id_, role=role, engine=engine, model=model, voice=None, language=None, api_key_ref=None)


class _FakeManager:
    def __init__(self):
        self.requested_ids = []

    async def get(self, cfg):
        self.requested_ids.append(cfg.id)
        return f"instance:{cfg.id}"


@pytest.mark.asyncio
async def test_resolve_wraps_llm_in_retry_once():
    manager = _FakeManager()
    registry = ProviderRegistry(manager)
    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"),
        llm=_sdk_cfg("llm1", "llm", "groq"),
        tts=_sdk_cfg("tts1", "tts", "elevenlabs"),
    )

    bundle = await registry.resolve(providers)

    assert isinstance(bundle.llm, RetryOnceLLM)
    assert bundle.llm._llm == "instance:llm1"


@pytest.mark.asyncio
async def test_resolve_includes_per_language_tts_overrides():
    manager = _FakeManager()
    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"),
        llm=_sdk_cfg("llm1", "llm", "openai"),
        tts=_sdk_cfg("tts1", "tts", "cartesia"),
        tts_by_language={"hi": _sdk_cfg("tts-hi", "tts", "cartesia")},
    )

    bundle = await ProviderRegistry(manager).resolve(providers)

    assert bundle.tts_for("hi") == "instance:tts-hi"
    assert bundle.tts_for("en") == "instance:tts1"
    assert bundle.tts_for(None) == "instance:tts1"


@pytest.mark.asyncio
async def test_broken_override_falls_back_to_base_voice():
    class _Failing(_FakeManager):
        async def get(self, cfg):
            if cfg.id == "tts-hi":
                raise ValueError("no api key")
            return await super().get(cfg)

    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"),
        llm=_sdk_cfg("llm1", "llm", "openai"),
        tts=_sdk_cfg("tts1", "tts", "cartesia"),
        tts_by_language={"hi": _sdk_cfg("tts-hi", "tts", "cartesia")},
    )
    bundle = await ProviderRegistry(_Failing()).resolve(providers)
    assert bundle.tts_for("hi") == "instance:tts1"
