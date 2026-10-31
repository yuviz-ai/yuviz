"""
SarvamTTS — cloud synthesis via Sarvam AI's Bulbul text-to-speech (POST /text-to-speech).

Built for Indian languages: one voice reads Hindi, Indian English and code-mixed Hinglish
naturally. The REST endpoint returns whole clips (base64 WAV), so synthesize_stream yields
one chunk per sentence, like ElevenLabs.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import time
import wave
from collections import OrderedDict
from typing import Any
from urllib.parse import urlencode

import httpx
import numpy as np
import websockets

from libs.config_sdk.languages import normalize_language

from ..interfaces import INSTANCE_LANGUAGE
from services.conversation.providers.http_pool import provider_http_limits

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.sarvam.ai"
# Sarvam's recommended model for new integrations. Its speakers are personas named
# <name>_<language>_<style>; the "enhi" ones are built for code-mixed Hindi and English.
DEFAULT_MODEL = "bulbul:v4-flash"
DEFAULT_SPEAKER = "shubh_enhi_ads"
# Registry language -> Sarvam language_code. Only the registry languages Sarvam speaks.
LANGUAGE_CODES = {code: f"{code}-IN" for code in ("en", "hi", "ta", "te", "kn", "mr", "bn")}
# Rates every Bulbul model serves; anything else is requested at the next one up and resampled.
_SUPPORTED_RATES = (8000, 16000, 22050, 24000)
_MAX_CHARS = 2500
# Short phrases recur on every call (tool fillers, the greeting, "anything else?") and a REST
# round trip costs 0.3-1 s each time; their audio is kept, per voice instance, in a small LRU.
_CACHE_MAX_CHARS = 120
_CACHE_ENTRIES = 256
# Phrases pinned by pin_phrases()/prewarm() (fillers, an agent's greeting) are kept outside the
# LRU, so a call's worth of one-off sentences can never evict them. Bounded, as a backstop.
_PINNED_MAX_ENTRIES = 512
_PINNED_MAX_CHARS = 600
# The pipeline's output rate (PipelineConfig.sample_rate); what prewarm synthesizes at.
_PREWARM_RATE = 16_000
# Streaming: the first audio of a sentence arrives in ~0.2 s over the WebSocket API, against
# ~1.2 s for the whole clip over REST. Connections are pooled per (language, rate); Sarvam
# closes one after about a minute idle, so an idle one is retired well before that.
_WS_URL = "wss://api.sarvam.ai/text-to-speech/ws"
_WS_IDLE_MAX_S = 40.0
_WS_POOL_MAX = 8
_WS_CONNECT_TIMEOUT_S = 3.0
_WS_FIRST_AUDIO_TIMEOUT_S = 4.0
_WS_CHUNK_TIMEOUT_S = 4.0


def _nearest_supported_rate(requested: int) -> int:
    for rate in _SUPPORTED_RATES:
        if rate >= requested:
            return rate
    return _SUPPORTED_RATES[-1]


def _pcm_from_wav(data: bytes) -> tuple[bytes, int]:
    """(16-bit mono PCM, sample rate) from a WAV clip."""
    with wave.open(io.BytesIO(data)) as clip:
        rate, channels, width = clip.getframerate(), clip.getnchannels(), clip.getsampwidth()
        frames = clip.readframes(clip.getnframes())
    if width != 2:
        raise ValueError(f"unexpected sample width {width}")
    if channels > 1:
        samples = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels).mean(axis=1)
        frames = samples.astype(np.int16).tobytes()
    return frames, rate


class SarvamTTS:
    """ITTS backed by Sarvam's /text-to-speech.

    language — Sarvam language code ("hi-IN", "en-IN") or a registry code; a per-call
               `language=` overrides it (accepts_language). Hindi is the default, since a
               Hindi request also reads the English words of a Hinglish sentence correctly.
    """

    accepts_language = True

    def __init__(
        self,
        api_key:   str,
        speaker:   str = DEFAULT_SPEAKER,
        model:     str = DEFAULT_MODEL,
        base_url:  str = _DEFAULT_BASE_URL,
        timeout_s: float = 15.0,
        pace:      float = 1.0,
        language:  str | None = None,
        streaming: bool = True,
    ) -> None:
        self._speaker = speaker
        self._model = model
        self._pace = pace
        self._language = language
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"api-subscription-key": api_key},
            timeout=timeout_s,
            limits=provider_http_limits(),
        )
        self._api_key = api_key
        self._streaming = streaming
        # (language_code, rate) -> idle (connection, last used at) pairs
        self._ws_idle: dict[tuple[str, int], list[tuple[Any, float]]] = {}
        self._ws_connect = websockets.connect   # swappable in tests
        self._cache: OrderedDict[tuple[str, str, int], bytes] = OrderedDict()
        self._pinned: dict[tuple[str, str, int], bytes] = {}
        self._prewarmed: set[str] = set()
        log.info("SarvamTTS speaker=%s model=%s pace=%s language=%s", speaker, model, pace, language)

    def _language_code(self, language: Any) -> str:
        chosen = self._language if language is INSTANCE_LANGUAGE else language
        if isinstance(chosen, str) and chosen in LANGUAGE_CODES.values():
            return chosen
        return LANGUAGE_CODES.get(normalize_language(chosen) or "", LANGUAGE_CODES["hi"])

    async def synthesize(self, text: str, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE) -> bytes:
        if not text.strip():
            return b""
        language_code = self._language_code(language)
        key = (text, language_code, sample_rate)
        pinned = self._pinned.get(key)
        if pinned is not None:
            return pinned
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        audio = await self._request(text, sample_rate, language_code)
        if audio and len(text) <= _CACHE_MAX_CHARS:
            self._cache[key] = audio
            if len(self._cache) > _CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return audio

    async def pin_phrases(self, phrases: dict[str | None, list[str]], sample_rate: int = _PREWARM_RATE) -> None:
        """Synthesizes each {language: [phrase, ...]} once and keeps it for the life of this
        voice: what an agent says on every call (its greeting) then plays without a round trip.
        A None language means this voice's own, as synthesize() without `language=` does."""
        wanted = []
        for language, texts in phrases.items():
            code = self._language_code(INSTANCE_LANGUAGE if language is None else language)
            for text in texts:
                key = (text, code, sample_rate)
                if (text and text.strip() and len(text) <= _PINNED_MAX_CHARS and key not in self._pinned
                        and len(self._pinned) + len(wanted) < _PINNED_MAX_ENTRIES):
                    wanted.append((key, language))
        if not wanted:
            return
        audios = await asyncio.gather(*(self._request(k[0], k[2], k[1]) for k, _lang in wanted))
        for (key, _lang), audio in zip(wanted, audios):
            if audio:
                self._pinned[key] = audio

    async def prewarm(self, languages: list[str]) -> None:
        """Pins the filler phrases for each language (the turn-1 pool and the tool-call pool), once per voice
        instance, so a "one moment" plays without a round trip."""
        from ...i18n import first_turn_fillers, tool_fillers

        todo = {}
        for language in languages:
            code = self._language_code(language)
            if code in self._prewarmed:
                continue
            self._prewarmed.add(code)
            phrases = {phrase for phrase, _seconds in (*(first_turn_fillers(language) or ()), *tool_fillers(language))}
            todo[language] = sorted(phrases)
        await asyncio.gather(self.pin_phrases(todo), *(self._warm_socket(lang) for lang in languages))

    async def _warm_socket(self, language: str) -> None:
        """Runs at each call's start (during the greeting): one open, configured connection per
        language, so the first reply doesn't pay the ~250 ms connect."""
        if not self._streaming:
            return
        code = self._language_code(language)
        idle = self._ws_idle.get((code, _PREWARM_RATE), [])
        if any(time.monotonic() - last < _WS_IDLE_MAX_S for _conn, last in idle):
            return
        try:
            conn = await self._ws_acquire(code, _PREWARM_RATE)
        except Exception as exc:
            log.info("SarvamTTS socket warm-up failed (%s); first sentence will connect", exc)
            return
        self._ws_release(conn, code, _PREWARM_RATE)

    async def _request(self, text: str, sample_rate: int, language_code: str) -> bytes:
        request_rate = _nearest_supported_rate(sample_rate)
        body = {
            "text": text[:_MAX_CHARS],
            "language_code": language_code,
            "speaker": self._speaker,
            "model": self._model,
            "speech_sample_rate": request_rate,
            "output_audio_codec": "wav",
        }
        if self._pace != 1.0:
            body["pace"] = self._pace
        try:
            resp = await self._client.post("/text-to-speech", json=body)
            resp.raise_for_status()
            clip = base64.b64decode(resp.json()["audios"][0])
            pcm, rate = _pcm_from_wav(clip)
        except httpx.HTTPStatusError as exc:
            # Sarvam's error body names the problem (bad speaker, quota) and holds no secrets.
            log.error("SarvamTTS request failed: %s %s", exc.response.status_code, exc.response.text[:300])
            return b""
        except (httpx.HTTPError, KeyError, IndexError, ValueError, wave.Error):
            log.exception("SarvamTTS request failed")
            return b""
        return pcm if rate == sample_rate else self._resample(pcm, rate, sample_rate)

    async def synthesize_stream(self, text: str, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE):
        """Cached and pinned phrases come whole; anything else streams over a pooled WebSocket,
        audio yielded as it arrives. A streaming failure before any audio falls back to REST."""
        if not text.strip():
            return
        language_code = self._language_code(language)
        key = (text, language_code, sample_rate)
        cached = self._pinned.get(key) or self._cache.get(key)
        if cached is not None or not self._streaming or sample_rate not in _SUPPORTED_RATES:
            audio = cached if cached is not None else await self.synthesize(text, sample_rate, language=language)
            if audio:
                yield audio
            return
        produced = False
        pieces: list[bytes] = []
        stream = self._stream_ws(text[:_MAX_CHARS], language_code, sample_rate)
        try:
            async for chunk in stream:
                produced = True
                pieces.append(chunk)
                yield chunk
        except Exception as exc:
            if produced:
                # Part of the sentence is already playing; replaying it from REST would repeat it.
                log.warning("SarvamTTS stream broke mid-sentence: %s", exc)
                return
            log.warning("SarvamTTS stream failed, falling back to REST: %s", exc)
            audio = await self.synthesize(text, sample_rate, language=language)
            if audio:
                yield audio
            return
        finally:
            # A caller who stops listening (barge-in) closes this generator, not the inner one:
            # close it here so its connection is dropped now, not whenever it is collected.
            await stream.aclose()
        if pieces and len(text) <= _CACHE_MAX_CHARS:
            self._cache[key] = b"".join(pieces)
            if len(self._cache) > _CACHE_ENTRIES:
                self._cache.popitem(last=False)

    async def _ws_acquire(self, language_code: str, sample_rate: int) -> Any:
        idle = self._ws_idle.setdefault((language_code, sample_rate), [])
        now = time.monotonic()
        while idle:
            conn, last_used = idle.pop()
            if now - last_used < _WS_IDLE_MAX_S:
                return conn
            await self._ws_close(conn)
        query = urlencode({"model": self._model, "send_completion_event": "true"})
        conn = await asyncio.wait_for(
            self._ws_connect(f"{_WS_URL}?{query}", additional_headers={"Api-Subscription-Key": self._api_key}),
            _WS_CONNECT_TIMEOUT_S,
        )
        config = {"speaker": self._speaker, "language_code": language_code, "speech_sample_rate": sample_rate,
                  "output_audio_codec": "linear16", "min_buffer_size": 30, "max_chunk_length": 150}
        if self._pace != 1.0:
            config["pace"] = self._pace
        await conn.send(json.dumps({"type": "config", "data": config}))
        return conn

    def _ws_release(self, conn: Any, language_code: str, sample_rate: int) -> None:
        idle = self._ws_idle.setdefault((language_code, sample_rate), [])
        if len(idle) < _WS_POOL_MAX:
            idle.append((conn, time.monotonic()))
        else:
            asyncio.ensure_future(self._ws_close(conn))

    @staticmethod
    async def _ws_close(conn: Any) -> None:
        try:
            await conn.close()
        except Exception:
            pass

    async def _stream_ws(self, text: str, language_code: str, sample_rate: int):
        """One sentence over one pooled connection. The connection goes back to the pool only
        after the 'final' event; if the caller stops early (barge-in) or anything fails, it is
        closed, because audio for the abandoned sentence may still be in flight on it."""
        conn = await self._ws_acquire(language_code, sample_rate)
        reusable = False
        carry = b""
        try:
            await conn.send(json.dumps({"type": "text", "data": {"text": text}}))
            await conn.send(json.dumps({"type": "flush"}))
            timeout = _WS_FIRST_AUDIO_TIMEOUT_S
            while True:
                message = json.loads(await asyncio.wait_for(conn.recv(), timeout))
                kind = message.get("type")
                if kind == "audio":
                    pcm = carry + base64.b64decode(message["data"]["audio"])
                    cut = len(pcm) - (len(pcm) % 2)          # whole 16-bit samples only
                    carry = pcm[cut:]
                    if cut:
                        yield pcm[:cut]
                    timeout = _WS_CHUNK_TIMEOUT_S
                elif kind == "event" and (message.get("data") or {}).get("event_type") == "final":
                    reusable = True
                    return
                elif kind == "error":
                    raise RuntimeError(f"sarvam error: {str(message.get('data'))[:200]}")
        finally:
            if reusable:
                self._ws_release(conn, language_code, sample_rate)
            else:
                await self._ws_close(conn)

    @staticmethod
    def _resample(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
        from scipy.signal import resample_poly

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        gcd = math.gcd(from_rate, to_rate)
        resampled = resample_poly(audio, to_rate // gcd, from_rate // gcd).astype(np.float32)
        return np.clip(resampled * 32767, -32768, 32767).astype(np.int16).tobytes()

    async def aclose(self) -> None:
        for idle in self._ws_idle.values():
            for conn, _last in idle:
                await self._ws_close(conn)
        self._ws_idle.clear()
        await self._client.aclose()
