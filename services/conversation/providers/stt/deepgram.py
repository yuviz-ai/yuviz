"""
DeepgramSTT — cloud transcription via Deepgram.

transcribe() uses batch /v1/listen; the *_stream methods use the live WebSocket API
so the transcript is mostly computed by the time speech_ended fires.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from typing import Any

import httpx
import websockets

from libs.config_sdk.languages import normalize_language

from ..interfaces import INSTANCE_LANGUAGE, SttResult

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.deepgram.com"
_LIVE_WS_URL = "wss://api.deepgram.com/v1/listen"

# Deepgram closes a live connection after ~10-12s without audio; KeepAlive holds it through silence.
_KEEPALIVE_INTERVAL_S = 8.0


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
        base_url:  str = _DEFAULT_BASE_URL,
        timeout_s: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._model  = model
        self._language = _canonical_language(language)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Token {api_key}"},
            timeout=timeout_s,
        )
        # Swappable for tests. One live connection per session, opened on first chunk.
        self._ws_connect = websockets.connect
        self._streams: dict[str, _LiveStream] = {}
        log.info("DeepgramSTT model=%s language=%s", model, self._language)

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
        log.debug("DeepgramSTT transcript=%r confidence=%.2f lang=%s", text, confidence, lang_fields.get("language"))
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
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{_LIVE_WS_URL}?{query}"

    async def feed_stream(
        self, session_id: str, chunk: bytes, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE,
    ) -> None:
        if not chunk:
            return
        stream = self._streams.get(session_id)
        if stream is None:
            try:
                stream = await self._open_stream(session_id, sample_rate, self._effective_language(language))
            except Exception:
                log.exception("DeepgramSTT: failed to open live stream session=%s", session_id)
                return
            self._streams[session_id] = stream
        try:
            await stream.ws.send(chunk)
        except Exception:
            log.exception(
                "DeepgramSTT: failed to send audio chunk session=%s — evicting dead "
                "stream, next chunk will open a fresh one", session_id,
            )
            # Otherwise the dead socket is reused and STT is lost for the rest of the call.
            self._evict_stream(session_id, stream)

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
        # Only is_final segments are kept; interim results are ignored.
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
                if data.get("is_final") and text:
                    stream.final_segments.append(text)
                    stream.last_confidence = float(alt.get("confidence", 1.0))
                    if stream.language == "multi":
                        stream.word_languages.update(_word_languages(alt))
                        stream.languages.extend(alt.get("languages") or [])
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            log.exception("DeepgramSTT: live stream read loop failed session=%s", session_id)

    async def finalize_stream(
        self, session_id: str, audio: bytes, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE,
    ) -> SttResult:
        # audio is unused: the live stream already has everything.
        stream = self._streams.pop(session_id, None)
        if stream is None:
            return SttResult(text="")
        if stream.keepalive_task:
            stream.keepalive_task.cancel()
        try:
            await stream.ws.send(json.dumps({"type": "CloseStream"}))
            if stream.reader_task:
                await asyncio.wait_for(stream.reader_task, timeout=5.0)
        except Exception:
            log.exception("DeepgramSTT: finalize_stream failed session=%s", session_id)
        finally:
            try:
                await stream.ws.close()
            except Exception:
                pass

        text = " ".join(stream.final_segments).strip()
        lang_fields = _language_fields(stream.word_languages, stream.languages) if stream.language == "multi" else {}
        return SttResult(text=text, confidence=stream.last_confidence, **lang_fields)

    async def cancel_stream(self, session_id: str) -> None:
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
