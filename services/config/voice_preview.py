"""
Speak a line of text in one provider_config's voice, so an operator can hear
what a prompt will actually sound like before a real call does.

Only the engines whose synthesis is a plain HTTP call with an API key are
reachable from here — elevenlabs and deepgram. macos needs `/usr/bin/say` on
a macOS host and kokoro needs its model weights loaded in-process (see
services/conversation/providers/tts/), neither of which this service has or
should acquire; those keep the pre-rendered static clips under
admin-ui/public/voice-samples/ (scripts/generate_voice_samples.py), which are
a fixed sentence rather than the operator's own text. UNSUPPORTED_ENGINES
names them explicitly so the UI can say why instead of failing vaguely.

Returns WAV, not the raw PCM16 the call pipeline passes around: the admin UI
plays previews through a plain <audio> element (the same one both voice
pickers already use), and that cannot decode headerless PCM.
"""

from __future__ import annotations

import io
import logging
import wave
from typing import Any

import httpx

from .secret_resolver import SecretResolver

log = logging.getLogger(__name__)

# What the preview is rendered at. 24 kHz is one of ElevenLabs' natively
# supported pcm_* rates, so nothing has to be resampled here — resampling
# would pull scipy into this service purely for a preview.
PREVIEW_RATE = 24_000
MAX_CHARS = 600
_TIMEOUT_S = 20.0

SUPPORTED_ENGINES = ("elevenlabs", "deepgram")
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
    # Matches ai_provider_manager._make_elevenlabs_tts: only send the knobs
    # that are actually set, so a preview is the same request a call makes.
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
                "model": cfg.get("model") or "aura-asteria-en",
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


async def synthesize_preview(cfg: dict[str, Any], text: str, *, secret_resolver: SecretResolver) -> bytes:
    """`cfg` is an already-authorized provider_configs row (Tier 3 — the
    router checks tenant access before calling here). Returns WAV bytes."""
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
    if engine == "elevenlabs" and not cfg.get("voice"):
        raise PreviewUnavailable("this ElevenLabs provider has no voice selected yet")

    try:
        api_key = await secret_resolver.resolve(cfg["api_key_ref"])
    except Exception as exc:
        # An unresolvable ref is a misconfigured provider, not a missing
        # resource — without this it reaches the app's LookupError handler as
        # a 404 whose detail is the raw ref string.
        log.warning("voice preview: could not resolve api_key_ref for %s: %s", cfg["id"], exc)
        raise PreviewUnavailable(
            "this voice's API key could not be read — check the provider's credential in AI & Voice"
        ) from exc
    try:
        pcm = await (_elevenlabs if engine == "elevenlabs" else _deepgram)(cfg, api_key, text)
    except httpx.RequestError as exc:
        raise PreviewUnavailable(f"could not reach the voice provider: {exc.__class__.__name__}") from exc

    if not pcm:
        raise PreviewUnavailable("the voice provider returned no audio")
    return _to_wav(pcm, PREVIEW_RATE)
