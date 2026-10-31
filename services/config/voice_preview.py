"""Speak operator text in a provider_config's voice. Only HTTP engines (elevenlabs, deepgram,
cartesia, sarvam) are supported here. Returns WAV because <audio> can't play raw PCM."""

from __future__ import annotations

import base64
import io
import logging
import wave
from typing import Any

import httpx

from .secret_resolver import SecretResolver

log = logging.getLogger(__name__)

# Native ElevenLabs pcm_* rate, so no resampling is needed.
PREVIEW_RATE = 24_000
MAX_CHARS = 600
_TIMEOUT_S = 20.0

SUPPORTED_ENGINES = ("elevenlabs", "deepgram", "cartesia", "sarvam")
_CARTESIA_API_VERSION = "2024-06-10"  # keep equal to services/conversation/providers/tts/cartesia.py
UNSUPPORTED_ENGINES = {
    "macos": "macOS voices are synthesized by the host's own `say` binary, which this service "
             "cannot reach — use the sample clip on the voice picker instead.",
    "kokoro": "Kokoro runs from local model weights loaded inside the conversation service — "
              "use the sample clip on the voice picker instead.",
}


class PreviewUnavailable(Exception):
    """Engine can't be synthesized from here; `.detail` explains which and why."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def _to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)  # PCM16
        f.setframerate(rate)
        f.writeframes(pcm)
    return buf.getvalue()


async def _elevenlabs(cfg: dict[str, Any], api_key: str, text: str) -> bytes:
    extra = cfg.get("extra") or {}
    speed = float(extra.get("speed") or 1.0)
    payload: dict[str, Any] = {
        "text": text,
        "model_id": extra.get("model_id") or "eleven_turbo_v2_5",
    }
    # Same request shape as ai_provider_manager._make_elevenlabs_tts.
    if speed != 1.0:
        payload["voice_settings"] = {"speed": speed}
    if cfg.get("language"):
        payload["language_code"] = cfg["language"]

    async with httpx.AsyncClient(base_url="https://api.elevenlabs.io", timeout=_TIMEOUT_S) as client:
        resp = await client.post(
            f"/v1/text-to-speech/{cfg['voice']}",
            params={"output_format": f"pcm_{PREVIEW_RATE}"},
            headers={"xi-api-key": api_key},
            json=payload,
        )
    if resp.status_code != 200:
        log.warning("ElevenLabs TTS preview returned %s: %s", resp.status_code, resp.text[:200])
        raise PreviewUnavailable(f"ElevenLabs returned {resp.status_code}")
    return resp.content


async def _deepgram(cfg: dict[str, Any], api_key: str, text: str) -> bytes:
    async with httpx.AsyncClient(base_url="https://api.deepgram.com", timeout=_TIMEOUT_S) as client:
        resp = await client.post(
            "/v1/speak",
            params={
                # The Aura voice id is the model name; older rows stored it in model.
                "model": cfg.get("voice") or cfg.get("model") or "aura-asteria-en",
                "encoding": "linear16",
                "container": "none",
                "sample_rate": PREVIEW_RATE,
            },
            headers={"Authorization": f"Token {api_key}"},
            json={"text": text},
        )
    if resp.status_code != 200:
        log.warning("Deepgram TTS preview returned %s: %s", resp.status_code, resp.text[:200])
        raise PreviewUnavailable(f"Deepgram returned {resp.status_code}")
    return resp.content


async def _cartesia(cfg: dict[str, Any], api_key: str, text: str) -> bytes:
    extra = cfg.get("extra") or {}
    body: dict[str, Any] = {
        "model_id": extra.get("model") or cfg.get("model") or "sonic-2",
        "transcript": text,
        "voice": {"mode": "id", "id": cfg.get("voice") or ""},
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": PREVIEW_RATE},
    }
    if cfg.get("language"):
        body["language"] = str(cfg["language"]).split("-")[0].lower()
    async with httpx.AsyncClient(base_url="https://api.cartesia.ai", timeout=_TIMEOUT_S) as client:
        resp = await client.post(
            "/tts/bytes", json=body, headers={"X-API-Key": api_key, "Cartesia-Version": _CARTESIA_API_VERSION},
        )
    if resp.status_code != 200:
        log.warning("Cartesia TTS preview returned %s: %s", resp.status_code, resp.text[:200])
        raise PreviewUnavailable(f"Cartesia returned {resp.status_code}")
    return resp.content


async def _sarvam(cfg: dict[str, Any], api_key: str, text: str) -> bytes:
    """Sarvam answers with a WAV clip; its PCM is returned at PREVIEW_RATE (24 kHz is a supported rate)."""
    lang = str(cfg.get("language") or "en").split("-")[0].lower()
    body = {
        "text": text, "language_code": f"{lang}-IN", "speaker": (cfg.get("voice") or "").lower(),
        "model": (cfg.get("extra") or {}).get("model") or cfg.get("model") or "bulbul:v4-flash",
        "speech_sample_rate": PREVIEW_RATE, "output_audio_codec": "wav",
    }
    async with httpx.AsyncClient(base_url="https://api.sarvam.ai", timeout=_TIMEOUT_S) as client:
        resp = await client.post("/text-to-speech", json=body, headers={"api-subscription-key": api_key})
    if resp.status_code != 200:
        log.warning("Sarvam TTS preview returned %s: %s", resp.status_code, resp.text[:200])
        raise PreviewUnavailable(f"Sarvam returned {resp.status_code}")
    try:
        with wave.open(io.BytesIO(base64.b64decode(resp.json()["audios"][0])), "rb") as clip:
            return clip.readframes(clip.getnframes())
    except (KeyError, IndexError, ValueError, wave.Error) as exc:
        raise PreviewUnavailable("Sarvam returned audio this preview can't read") from exc


_SYNTH = {"elevenlabs": _elevenlabs, "deepgram": _deepgram, "cartesia": _cartesia, "sarvam": _sarvam}


async def synthesize_preview(
    cfg: dict[str, Any], text: str, *, secret_resolver: SecretResolver, voice_id: str | None = None,
) -> bytes:
    """`cfg` is an already-authorized provider_configs row. `voice_id`, already checked against the account's voice
    list by the caller, replaces the saved ElevenLabs voice. Returns WAV bytes."""
    if voice_id and cfg.get("engine") == "elevenlabs":
        cfg = {**cfg, "voice": voice_id}
    text = (text or "").strip()
    if not text:
        raise PreviewUnavailable("nothing to say — write a line first")
    if len(text) > MAX_CHARS:
        raise PreviewUnavailable(f"preview text is limited to {MAX_CHARS} characters")

    if cfg["role"] != "tts":
        raise PreviewUnavailable(f"provider_config {cfg['id']} is role={cfg['role']!r}, expected 'tts'")

    engine = cfg["engine"]
    if engine in UNSUPPORTED_ENGINES:
        raise PreviewUnavailable(UNSUPPORTED_ENGINES[engine])
    if engine not in SUPPORTED_ENGINES:
        raise PreviewUnavailable(f"previewing {engine!r} voices isn't supported yet")
    if not cfg.get("api_key_ref"):
        raise PreviewUnavailable(f"provider_config {cfg['id']} has no api_key_ref configured")
    if engine in ("elevenlabs", "cartesia", "sarvam") and not cfg.get("voice"):
        raise PreviewUnavailable("pick a voice first")

    try:
        api_key = await secret_resolver.resolve(cfg["api_key_ref"])
    except Exception as exc:
        # Otherwise a LookupError becomes a 404 that echoes the raw ref.
        log.warning("voice preview: could not resolve api_key_ref for %s: %s", cfg["id"], exc)
        raise PreviewUnavailable(
            "this voice's API key could not be read — check the provider's credential in AI & Voice"
        ) from exc
    try:
        pcm = await _SYNTH[engine](cfg, api_key, text)
    except httpx.RequestError as exc:
        raise PreviewUnavailable(f"could not reach the voice provider: {exc.__class__.__name__}") from exc

    if not pcm:
        raise PreviewUnavailable("the voice provider returned no audio")
    return _to_wav(pcm, PREVIEW_RATE)
