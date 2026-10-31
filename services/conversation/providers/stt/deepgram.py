"""
DeepgramSTT — cloud transcription via Deepgram.

transcribe() uses batch /v1/listen; the *_stream methods use the live WebSocket API
so the transcript is mostly computed by the time speech_ended fires.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from typing import Any

import httpx
import websockets

from libs.config_sdk.languages import normalize_language

from ..interfaces import INSTANCE_LANGUAGE, SttResult
from services.conversation.providers.http_pool import provider_http_limits

log = logging.getLogger(__name__)

_DEFAULT_HOST = "api.deepgram.com"
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")


def deepgram_host() -> str:
    """DEEPGRAM_API_HOST picks Deepgram's regional endpoint, e.g. api.in.deepgram.com for India
    (one round trip ~50 ms from Mumbai instead of ~260 ms to the US; it covers nova-3 general,
    Hindi and multi). An invalid value falls back to the global host rather than a broken URL."""
    host = os.environ.get("DEEPGRAM_API_HOST", "").strip().lower() or _DEFAULT_HOST
    if not _HOST_RE.fullmatch(host):
        log.warning("DEEPGRAM_API_HOST=%r is not a hostname; using %s", host, _DEFAULT_HOST)
        return _DEFAULT_HOST
    return host

# Deepgram closes a live connection after ~10-12s without audio; KeepAlive holds it through silence.
_KEEPALIVE_INTERVAL_S = 8.0

# After a Finalize message Deepgram flushes what it has and answers with a Results message marked
# from_finalize. Past this we use whatever final segments already arrived rather than stall the turn.
_FINALIZE_TIMEOUT_S = 1.5


def _canonical_language(language: str | None) -> str:
    """Deepgram wants hyphenated tags ("en-US"); None means English (no live auto-detect)."""
    return language.strip().replace("_", "-") if language and language.strip() else "en"


def _word_languages(alt: dict[str, Any]) -> Counter:
    """Per-word language tags (language=multi only); empty when untagged."""
    counts: Counter = Counter()
    for word in alt.get("words") or []:
        lang = normalize_language(word.get("language")) if isinstance(word, dict) else None
        if lang:
            counts[lang] += 1
    return counts


def _language_fields(word_langs: Counter, languages: list[str]) -> dict[str, Any]:
    """SttResult language fields from multi-mode tags. Deepgram reports no language
    confidence, so the dominant language's share of words stands in for it."""
    total = sum(word_langs.values())
    if total:
        shares = {lang: n / total for lang, n in word_langs.items()}
        dominant = max(shares, key=shares.get)
        return {"language": dominant, "language_confidence": shares[dominant], "language_shares": shares}
    langs = [l for l in (normalize_language(x) for x in languages) if l]
    if langs:
        # Ordered by dominance; with no word tags, one language is certain, several are not.
        return {"language": langs[0], "language_confidence": 1.0 if len(set(langs)) == 1 else None}
    return {}


class _LiveStream:
    def __init__(self, ws, language: str | None) -> None:
        self.ws = ws
        self.language = language
        self.final_segments: list[str] = []
        self.last_confidence: float = 1.0
        # language=multi only: word tags and alternative-level languages across segments.
        self.word_languages: Counter = Counter()
        self.languages: list[str] = []
        self.reader_task: asyncio.Task | None = None
        self.keepalive_task: asyncio.Task | None = None
        # Set by the read loop when Deepgram answers a Finalize; the connection outlives one utterance.
        self.flush_done = asyncio.Event()
        self.latest_text: str = ""    # interim text not yet made final; "" once everything heard is final
        # True once Deepgram reported speech_final and no new words have been heard since. The transcript
        # is then complete and a Finalize round trip would only add its wait.
        self.endpoint_seen: bool = False
        self.opened_ms: float = 0.0

    def reset_utterance(self) -> None:
        """Forget one utterance's text so the next one starts clean on the same connection."""
        self.final_segments = []
        self.last_confidence = 1.0
        self.word_languages = Counter()
        self.languages = []
        self.latest_text = ""
        self.endpoint_seen = False


class DeepgramSTT:
    """ISTT backed by Deepgram's /v1/listen.

    language — a Deepgram language code ("en", "hi", "en-IN", ...) or "multi" for
    nova-2/3 code-switching, where the detected language is read back per utterance
    (alternatives[0].languages and words[].language, same path for REST and live).
    Deepgram does not auto-detect on the live API: no language means English, so
    None is treated as "en". A per-call `language=` overrides it (accepts_language).
    """

    accepts_language = True

    def __init__(
        self,
        api_key:   str,
        model:     str = "nova-3",
        language:  str | None = "en",
        base_url:  str | None = None,
        timeout_s: float = 10.0,
        endpointing_ms: int | None = None,
    ) -> None:
        host = deepgram_host()
        base_url = base_url or f"https://{host}"
        self._live_ws_url = f"wss://{host}/v1/listen"
        self._api_key = api_key
        self._model  = model
        self._language = _canonical_language(language)
        # Silence, in ms, after which Deepgram marks a transcript segment speech_final. None keeps its default.
        self._endpointing_ms = endpointing_ms
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Token {api_key}"},
            timeout=timeout_s,
            limits=provider_http_limits(),
        )
        # Swappable for tests. One live connection per session, opened on first chunk.
        self._ws_connect = websockets.connect
        self._streams: dict[str, _LiveStream] = {}
        # One open at a time per session, so a prewarm and the first audio chunk share one connection.
        self._open_locks: dict[str, asyncio.Lock] = {}
        # session_id -> callable(text) fired when Deepgram reports speech_final with text.
        log.info("DeepgramSTT model=%s language=%s host=%s", model, self._language, host)

    def _effective_language(self, language: Any) -> str:
        return self._language if language is INSTANCE_LANGUAGE else _canonical_language(language)

    async def transcribe(self, audio: bytes, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE) -> SttResult:
        if not audio:
            return SttResult(text="")

        params = {
            "model":     self._model,
            "encoding":  "linear16",
            "sample_rate": sample_rate,
            "channels":  1,
            "language":  self._effective_language(language),
        }

        try:
            resp = await self._client.post(
                "/v1/listen",
                params=params,
                content=audio,
                headers={"Content-Type": "audio/raw"},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            log.exception("DeepgramSTT request failed")
            return SttResult(text="")

        data = resp.json()
        try:
            alt = data["results"]["channels"][0]["alternatives"][0]
            text = alt["transcript"].strip()
            confidence = float(alt.get("confidence", 1.0))
        except (KeyError, IndexError, TypeError):
            log.warning("DeepgramSTT: unexpected response shape %r", data)
            return SttResult(text="")

        lang_fields = (
            _language_fields(_word_languages(alt), alt.get("languages") or [])
            if params["language"] == "multi" else {}
        )
        log.debug("DeepgramSTT transcript_chars=%d confidence=%.2f lang=%s", len(text), confidence, lang_fields.get("language"))
        return SttResult(text=text, confidence=confidence, **lang_fields)

    def _live_url(self, sample_rate: int, language: str) -> str:
        params = {
            "model": self._model,
            "encoding": "linear16",
            "sample_rate": str(sample_rate),
            "channels": "1",
            "interim_results": "true",
            "punctuate": "true",
            "language": language,
        }
        if self._endpointing_ms is not None:
            params["endpointing"] = str(int(self._endpointing_ms))
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self._live_ws_url}?{query}"

    async def feed_stream(
        self, session_id: str, chunk: bytes, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE,
    ) -> None:
        if not chunk:
            return
        stream = await self._ensure_stream(session_id, sample_rate, self._effective_language(language))
        if stream is None:
            return
        try:
            await stream.ws.send(chunk)
        except Exception:
            log.exception(
                "DeepgramSTT: failed to send audio chunk session=%s — evicting dead "
                "stream, next chunk will open a fresh one", session_id,
            )
            # Otherwise the dead socket is reused and STT is lost for the rest of the call.
            self._evict_stream(session_id, stream)

    async def prewarm_stream(
        self, session_id: str, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE,
    ) -> None:
        """Open the live connection before the caller speaks, so the connect time (about 0.9 s) is not
        paid on their first utterance. Best effort: never raises."""
        try:
            await self._ensure_stream(session_id, sample_rate, self._effective_language(language))
        except Exception:
            log.exception("DeepgramSTT: prewarm failed session=%s", session_id)

    async def _ensure_stream(self, session_id: str, sample_rate: int, language: str) -> "_LiveStream | None":
        lock = self._open_locks.get(session_id)
        if lock is None:
            lock = self._open_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            stream = self._streams.get(session_id)
            if stream is not None and stream.language != language:
                # The websocket's language is fixed at connect time, so a different one needs a new connection.
                self._evict_stream(session_id, stream)
                try:
                    await stream.ws.close()
                except Exception:
                    pass
                stream = None
            if stream is None:
                started = time.monotonic()
                try:
                    stream = await self._open_stream(session_id, sample_rate, language)
                except Exception:
                    log.exception("DeepgramSTT: failed to open live stream session=%s", session_id)
                    return None
                if self._open_locks.get(session_id) is not lock:
                    # The call ended while this connection was opening: nothing will ever evict it.
                    self._evict_stream(session_id, stream)
                    await self._close_socket(stream)
                    return None
                stream.opened_ms = (time.monotonic() - started) * 1000
                log.info("DeepgramSTT: live stream open session=%s in %.0f ms", session_id, stream.opened_ms)
                self._streams[session_id] = stream
            return stream

    def _evict_stream(self, session_id: str, stream: "_LiveStream") -> None:
        if self._streams.get(session_id) is stream:
            del self._streams[session_id]
        for task in (stream.reader_task, stream.keepalive_task):
            if task is not None:
                task.cancel()

    async def _open_stream(self, session_id: str, sample_rate: int, language: str) -> _LiveStream:
        ws = await self._ws_connect(
            self._live_url(sample_rate, language),
            additional_headers={"Authorization": f"Token {self._api_key}"},
        )
        stream = _LiveStream(ws, language)
        stream.reader_task = asyncio.create_task(self._read_loop(stream, session_id))
        stream.keepalive_task = asyncio.create_task(self._keepalive_loop(stream, session_id))
        return stream

    async def _keepalive_loop(self, stream: "_LiveStream", session_id: str) -> None:
        """Send KeepAlive periodically; on failure exit quietly (feed_stream() evicts dead streams)."""
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_INTERVAL_S)
                await stream.ws.send(json.dumps({"type": "KeepAlive"}))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.info("DeepgramSTT: keepalive send failed session=%s — stream likely closed", session_id)

    async def _read_loop(self, stream: _LiveStream, session_id: str) -> None:
        # Only is_final segments are kept for the transcript; interim text is tracked for the endpoint hook.
        try:
            async for raw in stream.ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if data.get("type") != "Results":
                    continue
                alternatives = data.get("channel", {}).get("alternatives") or [{}]
                alt = alternatives[0]
                text = (alt.get("transcript") or "").strip()
                if text:
                    # Words heard after an endpoint mean the caller kept talking: the transcript is not complete.
                    stream.endpoint_seen = False
                    stream.latest_text = text if not data.get("is_final") else ""
                if data.get("is_final") and text:
                    stream.final_segments.append(text)
                    stream.last_confidence = float(alt.get("confidence", 1.0))
                    if stream.language == "multi":
                        stream.word_languages.update(_word_languages(alt))
                        stream.languages.extend(alt.get("languages") or [])
                if data.get("speech_final") and stream.final_segments:
                    stream.endpoint_seen = True
                if data.get("from_finalize"):
                    stream.flush_done.set()
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            log.exception("DeepgramSTT: live stream read loop failed session=%s", session_id)
        finally:
            # A closed connection must not be reused: the next chunk opens a fresh one.
            if self._streams.get(session_id) is stream:
                del self._streams[session_id]
            stream.flush_done.set()


    async def finalize_stream(
        self, session_id: str, audio: bytes, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE,
    ) -> SttResult:
        """Flush the live connection and return the utterance's text. The connection stays open for the
        next utterance. If it is gone, the buffered `audio` is transcribed over REST so the turn is not lost."""
        stream = self._streams.get(session_id)
        if stream is None:
            return await self._transcribe_buffered(audio, sample_rate, language)
        stream.flush_done.clear()
        try:
            # Deepgram already closed the utterance by itself when there is nothing left to flush.
            if not (stream.endpoint_seen and not stream.latest_text and stream.final_segments):
                await stream.ws.send(json.dumps({"type": "Finalize"}))
                await asyncio.wait_for(stream.flush_done.wait(), timeout=_FINALIZE_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("DeepgramSTT: no flush acknowledgement within %.1fs session=%s; reconnecting for the next utterance",
                        _FINALIZE_TIMEOUT_S, session_id)
            self._evict_stream(session_id, stream)
            await self._close_socket(stream)
        except Exception:
            log.exception("DeepgramSTT: finalize_stream failed session=%s", session_id)
            self._evict_stream(session_id, stream)
            try:
                await stream.ws.close()
            except Exception:
                pass
            return await self._transcribe_buffered(audio, sample_rate, language)

        text = " ".join(stream.final_segments).strip()
        result = SttResult(
            text=text, confidence=stream.last_confidence,
            **(_language_fields(stream.word_languages, stream.languages) if stream.language == "multi" else {}),
        )
        stream.reset_utterance()
        if not text and audio and self._streams.get(session_id) is not stream:
            # The connection closed under us and nothing was recovered: do not drop the utterance.
            return await self._transcribe_buffered(audio, sample_rate, language)
        return result

    async def _transcribe_buffered(self, audio: bytes, sample_rate: int, language: Any) -> SttResult:
        if not audio:
            return SttResult(text="")
        return await self.transcribe(audio, sample_rate, language=language)

    async def _close_socket(self, stream: "_LiveStream") -> None:
        try:
            await stream.ws.close()
        except Exception:
            pass

    async def cancel_stream(self, session_id: str) -> None:
        self._open_locks.pop(session_id, None)
        stream = self._streams.pop(session_id, None)
        if stream is None:
            return
        if stream.reader_task:
            stream.reader_task.cancel()
        if stream.keepalive_task:
            stream.keepalive_task.cancel()
        try:
            await stream.ws.close()
        except Exception:
            pass

    async def aclose(self) -> None:
        await self._client.aclose()
