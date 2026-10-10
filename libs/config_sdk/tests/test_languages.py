"""Language registry + per-role language resolution + runtime override tenancy."""

from __future__ import annotations

import pytest

from libs.config_sdk.languages import (
    DEEPGRAM_MULTI,
    agent_supported_languages,
    deepgram_supports_multi,
    normalize_language,
    tts_languages,
)
from libs.config_sdk.providers.cache_aside import CacheAsideConfigProvider

from .test_cache_aside import FakeRepo, _agent_row, _provider_row, _tenant_row


def _repo(agent_overrides=None, stt=None, tts=None, extra_providers=None):
    providers = {
        "stt1": stt or _provider_row("stt1", "stt", "deepgram", tenant_id="t1", model="nova-3", language="en"),
        "llm1": _provider_row("llm1", "llm", "openai", tenant_id="t1"),
        "tts1": tts or _provider_row("tts1", "tts", "cartesia", tenant_id="t1", voice="v-en"),
        **(extra_providers or {}),
    }
    return FakeRepo(
        tenants={"acme": _tenant_row("acme")},
        agents={("acme", "bot"): _agent_row(
            "bot", stt_config_id="stt1", llm_config_id="llm1", tts_config_id="tts1",
            **(agent_overrides or {}),
        )},
        providers=providers,
    )


async def _runtime(repo):
    return await CacheAsideConfigProvider(repo, FakeRepo()).get_runtime_config("acme", "bot")


def test_normalize_language():
    assert normalize_language("hi-IN") == "hi"
    assert normalize_language("hi-Latn") == "hi"
    assert normalize_language("EN") == "en"
    assert normalize_language("") is None
    assert normalize_language(None) is None


def test_supported_languages_puts_default_first_and_dedupes():
    assert agent_supported_languages("hi", ["en", "hi", "en-US"]) == ("hi", "en")
    assert agent_supported_languages("hi", None) == ()
    assert agent_supported_languages("hi", []) == ()


def test_deepgram_multi_coverage():
    assert deepgram_supports_multi("nova-3", ["en", "hi"])
    assert not deepgram_supports_multi("nova-3", ["en", "zh"])
    assert not deepgram_supports_multi("base", ["en", "hi"])


def test_tts_language_capability():
    assert "hi" in tts_languages("cartesia", "sonic-2")
    assert tts_languages("deepgram", None) == frozenset({"en"})
    assert tts_languages("macos", None) == frozenset({"en"})
    assert tts_languages("elevenlabs", "eleven_turbo_v2") == frozenset({"en"})
    assert "hi" in tts_languages("elevenlabs", "eleven_turbo_v2_5")
    assert "hi" in tts_languages("kokoro", None, "hf_alpha")


# ── agent.language now reaches the providers ────────────────────────────────

async def test_single_language_agent_language_overrides_provider_rows():
    rc = await _runtime(_repo({"language": "hi"}))
    assert rc.media.stt_language == "hi"
    assert rc.media.tts_language == "hi"
    assert rc.media.supported_languages == ()


async def test_single_language_without_agent_language_keeps_each_rows_own():
    # TTS must not inherit the STT row's "en" (would force ElevenLabs out of auto-detect).
    rc = await _runtime(_repo())
    assert rc.media.stt_language == "en"
    assert rc.media.tts_language is None


async def test_multilingual_deepgram_listens_in_multi():
    rc = await _runtime(_repo({"language": "en", "supported_languages": ["en", "hi"]}))
    assert rc.media.stt_language == DEEPGRAM_MULTI
    assert rc.media.default_language == "en"
    assert rc.media.supported_languages == ("en", "hi")


async def test_multilingual_whisper_auto_detects():
    stt = _provider_row("stt1", "stt", "faster_whisper", tenant_id="t1", model="small", language="en")
    rc = await _runtime(_repo({"language": "hi", "supported_languages": ["en"]}, stt=stt))
    assert rc.media.stt_language is None
    assert rc.media.supported_languages == ("hi", "en")


# ── per-language TTS overrides ───────────────────────────────────────────────

async def test_same_tenant_override_is_resolved():
    repo = _repo(
        {"language": "en", "supported_languages": ["en", "hi"],
         "tts_config_by_language": {"hi": "tts-hi"}},
        extra_providers={"tts-hi": _provider_row("tts-hi", "tts", "cartesia", tenant_id="t1", voice="v-hi")},
    )
    rc = await _runtime(repo)
    assert rc.providers.tts_by_language["hi"].id == "tts-hi"


async def test_cross_tenant_override_is_dropped_at_runtime():
    repo = _repo(
        {"language": "en", "supported_languages": ["en", "hi"],
         "tts_config_by_language": {"hi": "tts-other"}},
        extra_providers={"tts-other": _provider_row("tts-other", "tts", "cartesia", tenant_id="t2", voice="v")},
    )
    rc = await _runtime(repo)
    assert rc.providers.tts_by_language == {}


async def test_override_without_tenant_or_wrong_role_is_dropped():
    repo = _repo(
        {"language": "en", "supported_languages": ["en", "hi", "es"],
         "tts_config_by_language": {"hi": "no-tenant", "es": "llm1"}},
        extra_providers={"no-tenant": _provider_row("no-tenant", "tts", "cartesia", voice="v")},
    )
    rc = await _runtime(repo)
    assert rc.providers.tts_by_language == {}


async def test_override_ignored_for_single_language_agent():
    repo = _repo(
        {"language": "en", "tts_config_by_language": {"hi": "tts-hi"}},
        extra_providers={"tts-hi": _provider_row("tts-hi", "tts", "cartesia", tenant_id="t1", voice="v-hi")},
    )
    rc = await _runtime(repo)
    assert rc.providers.tts_by_language == {}
    assert "provider:tts-hi" not in repo.calls


# ── Aliases and voice-bound Kokoro ───────────────────────────────────────────

from libs.config_sdk.languages import resolve_alias  # noqa: E402


def test_urdu_resolves_to_hindi_only_when_urdu_unsupported():
    assert resolve_alias("ur", ("en", "hi")) == "hi"
    assert resolve_alias("ur", ("en", "es")) == "ur"
    assert resolve_alias("hi", ("en", "hi")) == "hi"
    assert resolve_alias(None, ("en", "hi")) is None


def test_kokoro_voice_speaks_only_its_own_language():
    assert tts_languages("kokoro", None, "af_sarah") == frozenset({"en"})
    assert tts_languages("kokoro", None, "bm_george") == frozenset({"en"})
    assert tts_languages("kokoro", None, "hf_alpha") == frozenset({"hi"})
    # No voice: the factory's default (an English voice) is what speaks.
    assert tts_languages("kokoro", None, None) == frozenset({"en"})



# ── Review fixes ─────────────────────────────────────────────────────────────

def test_kokoro_has_no_ja_zh_and_unknown_voice_speaks_nothing():
    assert "ja" not in tts_languages("kokoro", None) and "zh" not in tts_languages("kokoro", None)
    assert tts_languages("kokoro", None, "jf_alpha") == frozenset()
    assert tts_languages("kokoro", None, "zf_xiaobei") == frozenset()


async def test_override_that_cannot_speak_its_language_is_dropped_at_runtime():
    repo = _repo(
        {"language": "en", "supported_languages": ["en", "hi"],
         "tts_config_by_language": {"hi": "kokoro-en"}},
        extra_providers={"kokoro-en": _provider_row("kokoro-en", "tts", "kokoro", tenant_id="t1", voice="af_heart")},
    )
    rc = await _runtime(repo)
    assert rc.providers.tts_by_language == {}



@pytest.mark.parametrize("bad", ["English", "en us", "1234"])
async def test_free_text_agent_language_is_ignored_at_runtime(bad):
    rc = await _runtime(_repo({"language": bad}, stt=_provider_row(
        "stt1", "stt", "deepgram", tenant_id="t1", model="nova-3", language="en-IN")))
    assert rc.media.stt_language == "en-IN"   # the row's own, not "English"
    assert rc.media.tts_language is None


async def test_legacy_underscore_tag_still_reaches_providers():
    rc = await _runtime(_repo({"language": "en_IN"}))
    assert rc.media.stt_language == "en_IN"   # Deepgram canonicalises it to en-IN
