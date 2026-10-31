"""
SarvamSTT — Sarvam's Saaras speech recognition (saaras:v4 realtime, saaras:v3).

The *_stream methods use the WebSocket API so the transcript is mostly ready when speech_ended
fires; transcribe() and every fallback use the REST endpoint with the buffered audio as WAV.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
import wave
from typing import Any
from urllib.parse import urlencode

import httpx
import websockets

from libs.config_sdk.languages import normalize_language

from ..interfaces import INSTANCE_LANGUAGE, SttResult

log = logging.getLogger(__name__)

_REST_URL = "https://api.sarvam.ai/speech-to-text"
_WS_URL = "wss://api.sarvam.ai/speech-to-text/ws"
DEFAULT_MODEL = "saaras:v4"
# The streaming API takes 8 or 16 kHz; anything else goes over REST at finalize.
_STREAM_RATES = (8000, 16000)
# Output modes; only saaras:v3 honours them (v4 always transcribes).
MODES = ("transcribe", "translate", "verbatim", "translit", "codemix")
# After a flush Sarvam answers with the utterance's transcript; past this the turn uses what arrived.
_FLUSH_TIMEOUT_S = 1.5
# Sarvam has no keepalive message; silence is sent so an idle connection survives long agent replies.
_KEEPALIVE_INTERVAL_S = 8.0
_KEEPALIVE_MS = 100
# Reconnect cooldown after a failed connection attempt, to prevent storms.
_RECONNECT_COOLDOWN_S = 0.5


def _language_code(language: str | None) -> str:
    """Sarvam wants "hi-IN"-style codes; None or Deepgram's "multi" mean auto-detect."""
    base = normalize_language(language) if language and language != "multi" else None
    return f"{base}-IN" if base else "unknown"


def _wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(pcm)
    return buf.getvalue()


def _result(data: dict[str, Any], forced: str) -> SttResult:
    """Detected language only when Sarvam detected it, not when the call forced one."""
    if forced != "unknown":
        return SttResult(text=(data.get("transcript") or "").strip())
    prob = data.get("language_probability")
    return SttResult(
        text=(data.get("transcript") or "").strip(),
        language=normalize_language(data.get("language_code")),
        language_confidence=float(prob) if isinstance(prob, (int, float)) else None,
    )


class _LiveStream:
    def __init__(self, ws, language: str, sample_rate: int) -> None:
        self.ws = ws
        self.language = language
        self.sample_rate = sample_rate
        self.segments: list[SttResult] = []
        # True from Sarvam's START_SPEECH until its transcript for that speech arrives.
        self.pending = False
        self.got_data = asyncio.Event()
        self.reader_task: asyncio.Task | None = None
        self.keepalive_task: asyncio.Task | None = None
        self.last_send = time.monotonic()

    def reset_utterance(self) -> None:
        self.segments = []
        self.pending = False


class SarvamSTT:
    """ISTT backed by Sarvam Saaras. `language` is an ISO code ("en", "hi") or None to auto-detect;
    a per-call `language=` overrides it (accepts_language)."""

    accepts_language = True

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        language: str | None = None,
        mode: str | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        if mode is not None and mode not in MODES:
            raise ValueError(f"Sarvam mode {mode!r} is not one of {', '.join(MODES)}")
        self._api_key = api_key
        self._model = model
        self._language = language
        self._mode = mode if model == "saaras:v3" else None
        self._client = httpx.AsyncClient(headers={"api-subscription-key": api_key}, timeout=timeout_s)
        # Swappable for tests. One live connection per session, opened on prewarm or first chunk.
        self._ws_connect = websockets.connect
        self._streams: dict[str, _LiveStream] = {}
        self._open_locks: dict[str, asyncio.Lock] = {}
        self._failed_connects: dict[str, float] = {}
        log.info("SarvamSTT model=%s language=%s mode=%s", model, language, self._mode)

    def _effective_language(self, language: Any) -> str:
        return _language_code(self._language if language is INSTANCE_LANGUAGE else language)

    async def transcribe(self, audio: bytes, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE) -> SttResult:
        if not audio:
            return SttResult(text="")
        code = self._effective_language(language)
        form = {"model": self._model, "language_code": code}
        if self._mode:
            form["mode"] = self._mode
        try:
            resp = await self._client.post(
                _REST_URL, data=form, files={"file": ("audio.wav", _wav(audio, sample_rate), "audio/wav")},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            # Sarvam's error body names the problem (bad language, quota) and holds no secrets.
            log.error("SarvamSTT request failed: %s %s", exc.response.status_code, exc.response.text[:300])
            return SttResult(text="")
        except (httpx.HTTPError, ValueError):
            log.exception("SarvamSTT request failed")
            return SttResult(text="")
        return _result(data, code)

    def _live_url(self, sample_rate: int, language: str) -> str:
        params = {
            "model": self._model, "language-code": language, "sample_rate": str(sample_rate),
            "input_audio_codec": "pcm_s16le", "vad_signals": "true", "flush_signal": "true",
        }
        if self._mode:
            params["mode"] = self._mode
        return f"{_WS_URL}?{urlencode(params)}"

    async def prewarm_stream(self, session_id: str, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE) -> None:
        """Open the connection before the caller speaks so its connect time isn't paid on the first turn."""
        try:
            await self._ensure_stream(session_id, sample_rate, self._effective_language(language))
        except Exception:
            log.exception("SarvamSTT: prewarm failed session=%s", session_id)

    async def feed_stream(
        self, session_id: str, chunk: bytes, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE,
    ) -> None:
        if not chunk or sample_rate not in _STREAM_RATES:
            return
        stream = await self._ensure_stream(session_id, sample_rate, self._effective_language(language))
        if stream is None:
            return
        try:
            await self._send_audio(stream, chunk)
        except Exception:
            log.exception("SarvamSTT: failed to send audio session=%s — the next chunk opens a fresh stream", session_id)
            self._evict_stream(session_id, stream)

    async def _send_audio(self, stream: _LiveStream, pcm: bytes) -> None:
        await stream.ws.send(json.dumps({"audio": {
            "data": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": str(stream.sample_rate),
            "encoding": "audio/wav",
        }}))
        stream.last_send = time.monotonic()

    async def _ensure_stream(self, session_id: str, sample_rate: int, language: str) -> _LiveStream | None:
        if sample_rate not in _STREAM_RATES:
            return None
        lock = self._open_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            stream = self._streams.get(session_id)
            if stream is not None and (stream.language, stream.sample_rate) != (language, sample_rate):
                # Language and rate are fixed at connect time.
                self._evict_stream(session_id, stream)
                await self._close_socket(stream)
                stream = None
            if stream is None:
                failed_at = self._failed_connects.get(session_id)
                if failed_at is not None and time.monotonic() - failed_at < _RECONNECT_COOLDOWN_S:
                    return None
                self._failed_connects.pop(session_id, None)
                started = time.monotonic()
                try:
                    ws = await self._ws_connect(
                        self._live_url(sample_rate, language),
                        additional_headers={"Api-Subscription-Key": self._api_key},
                    )
                except Exception:
                    log.exception("SarvamSTT: failed to open live stream session=%s", session_id)
                    self._failed_connects[session_id] = time.monotonic()
                    return None
                stream = _LiveStream(ws, language, sample_rate)
                if self._open_locks.get(session_id) is not lock:
                    # The call ended while this connection was opening: nothing would ever close it.
                    await self._close_socket(stream)
                    return None
                stream.reader_task = asyncio.create_task(self._read_loop(stream, session_id))
                stream.keepalive_task = asyncio.create_task(self._keepalive_loop(stream, session_id))
                self._streams[session_id] = stream
                log.info("SarvamSTT: live stream open session=%s in %.0f ms", session_id, (time.monotonic() - started) * 1000)
            return stream

    async def _keepalive_loop(self, stream: _LiveStream, session_id: str) -> None:
        silence = b"\x00\x00" * (stream.sample_rate * _KEEPALIVE_MS // 1000)
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_INTERVAL_S)
                if time.monotonic() - stream.last_send >= _KEEPALIVE_INTERVAL_S:
                    await self._send_audio(stream, silence)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.info("SarvamSTT: keepalive failed session=%s — stream likely closed", session_id)

    async def _read_loop(self, stream: _LiveStream, session_id: str) -> None:
        try:
            async for raw in stream.ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind, data = msg.get("type"), msg.get("data") or {}
                if kind == "events":
                    if data.get("signal_type") == "START_SPEECH":
                        stream.pending = True
                elif kind == "data":
                    result = _result(data, stream.language)
                    if result.text:
                        stream.segments.append(result)
                    stream.pending = False
                    stream.got_data.set()
                elif kind == "error":
                    log.warning("SarvamSTT: stream error session=%s: %s", session_id, str(data.get("error"))[:200])
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            log.exception("SarvamSTT: live stream read loop failed session=%s", session_id)
        finally:
            if self._streams.get(session_id) is stream:
                del self._streams[session_id]
            stream.got_data.set()

    async def finalize_stream(
        self, session_id: str, audio: bytes, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE,
    ) -> SttResult:
        """Flush the live connection and return the utterance's text; the connection stays open for the
        next one. With no usable connection the buffered `audio` goes over REST so the turn is not lost."""
        stream = self._streams.get(session_id)
        if stream is None:
            return await self._transcribe_buffered(audio, sample_rate, language)
        # Sarvam already returned the transcript for everything it heard: no flush round trip needed.
        if stream.pending or not stream.segments:
            stream.got_data.clear()
            try:
                await stream.ws.send(json.dumps({"type": "flush"}))
                await asyncio.wait_for(stream.got_data.wait(), timeout=_FLUSH_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("SarvamSTT: no transcript within %.1fs of flush session=%s", _FLUSH_TIMEOUT_S, session_id)
                self._evict_stream(session_id, stream)
                return await self._transcribe_buffered(audio, sample_rate, language)
            except Exception:
                log.exception("SarvamSTT: flush failed session=%s", session_id)
                self._evict_stream(session_id, stream)
                await self._close_socket(stream)
                return await self._transcribe_buffered(audio, sample_rate, language)
        segments = stream.segments
        stream.reset_utterance()
        if not segments:
            if self._streams.get(session_id) is not stream:
                # The connection dropped and nothing came back: don't lose the caller's turn.
                return await self._transcribe_buffered(audio, sample_rate, language)
            return SttResult(text="")
        detected = [s for s in segments if s.language]
        last = detected[-1] if detected else None
        return SttResult(
            text=" ".join(s.text for s in segments).strip(),
            language=last.language if last else None,
            language_confidence=last.language_confidence if last else None,
        )

    async def _transcribe_buffered(self, audio: bytes, sample_rate: int, language: Any) -> SttResult:
        return await self.transcribe(audio, sample_rate, language=language) if audio else SttResult(text="")

    def _evict_stream(self, session_id: str, stream: _LiveStream) -> None:
        if self._streams.get(session_id) is stream:
            del self._streams[session_id]
        for task in (stream.reader_task, stream.keepalive_task):
            if task is not None:
                task.cancel()

    async def _close_socket(self, stream: _LiveStream) -> None:
        try:
            await stream.ws.close()
        except Exception:
            pass

    async def cancel_stream(self, session_id: str) -> None:
        self._open_locks.pop(session_id, None)
        self._failed_connects.pop(session_id, None)
        stream = self._streams.pop(session_id, None)
        if stream is None:
            return
        self._evict_stream(session_id, stream)
        await self._close_socket(stream)

    async def aclose(self) -> None:
        await self._client.aclose()
