"""
DeepgramSTT — cloud transcription via Deepgram.

transcribe() calls the pre-recorded /v1/listen REST endpoint (kept for
callers that still want one-shot batch transcription). feed_stream()/
finalize_stream()/cancel_stream() use Deepgram's real live-streaming
WebSocket API instead — found live 2026-08-02 that the batch-only path
throws away Deepgram's actual latency advantage: it transcribes
continuously while the caller is still talking, so by the time
speech_ended fires, the final transcript is already (mostly) computed
instead of needing a full decode from scratch. See pipeline.py's on_audio
(feeds every chunk immediately) and on_speech_ended (calls
finalize_stream() instead of transcribe()).

pip install httpx websockets (both already dependencies — httpx via
OllamaLLM, websockets via services/webcall)
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import websockets

from ..interfaces import SttResult

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.deepgram.com"
_LIVE_WS_URL = "wss://api.deepgram.com/v1/listen"

# Deepgram closes a live connection that receives neither audio nor a text
# message for ~10-12s (confirmed live: "did not receive audio data or a
# text message within the timeout window"). feed_stream() only sends real
# audio, so any gap longer than that — the agent's own TTS playing, a
# caller pause — kills the connection outright. Below that threshold, a
# periodic KeepAlive message (Deepgram's own documented keepalive type)
# keeps it open through silence.
_KEEPALIVE_INTERVAL_S = 8.0


class _LiveStream:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.final_segments: list[str] = []
        self.last_confidence: float = 1.0
        self.reader_task: asyncio.Task | None = None
        self.keepalive_task: asyncio.Task | None = None


class DeepgramSTT:
    """
    ISTT implementation backed by Deepgram's /v1/listen endpoint.

    api_key   — Deepgram API key (resolved once at construction by
                AIProviderManager via SecretResolver, never re-resolved
                per call).
    model     — e.g. "nova-3", "nova-2"
    language  — BCP-47 code, e.g. "en". None lets Deepgram auto-detect.
    """

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
        self._language = language
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Token {api_key}"},
            timeout=timeout_s,
        )
        # Swappable for tests — real code always uses websockets.connect; a
        # test injects a fake connect callable instead of hitting the
        # network. Keyed by session_id: one live connection per in-progress
        # call, opened lazily on the first feed_stream() chunk.
        self._ws_connect = websockets.connect
        self._streams: dict[str, _LiveStream] = {}
        log.info("DeepgramSTT model=%s language=%s", model, language)

    async def transcribe(self, audio: bytes, sample_rate: int) -> SttResult:
        if not audio:
            return SttResult(text="")

        params = {
            "model":     self._model,
            "encoding":  "linear16",
            "sample_rate": sample_rate,
            "channels":  1,
        }
        if self._language:
            params["language"] = self._language

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

        log.debug("DeepgramSTT transcript=%r confidence=%.2f", text, confidence)
        return SttResult(text=text, confidence=confidence)

    def _live_url(self, sample_rate: int) -> str:
        params = {
            "model": self._model,
            "encoding": "linear16",
            "sample_rate": str(sample_rate),
            "channels": "1",
            "interim_results": "true",
            "punctuate": "true",
        }
        if self._language:
            params["language"] = self._language
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{_LIVE_WS_URL}?{query}"

    async def feed_stream(self, session_id: str, chunk: bytes, sample_rate: int) -> None:
        if not chunk:
            return
        stream = self._streams.get(session_id)
        if stream is None:
            try:
                stream = await self._open_stream(session_id, sample_rate)
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
            # Deepgram already closed its end (e.g. the idle timeout above) —
            # without this, self._streams keeps handing back the same dead
            # socket forever and every remaining chunk this call fails the
            # same way, silently losing STT for the rest of the call.
            self._evict_stream(session_id, stream)

    def _evict_stream(self, session_id: str, stream: "_LiveStream") -> None:
        if self._streams.get(session_id) is stream:
            del self._streams[session_id]
        for task in (stream.reader_task, stream.keepalive_task):
            if task is not None:
                task.cancel()

    async def _open_stream(self, session_id: str, sample_rate: int) -> _LiveStream:
        ws = await self._ws_connect(
            self._live_url(sample_rate),
            additional_headers={"Authorization": f"Token {self._api_key}"},
        )
        stream = _LiveStream(ws)
        stream.reader_task = asyncio.create_task(self._read_loop(stream, session_id))
        stream.keepalive_task = asyncio.create_task(self._keepalive_loop(stream, session_id))
        return stream

    async def _keepalive_loop(self, stream: "_LiveStream", session_id: str) -> None:
        """Sends Deepgram's KeepAlive message every _KEEPALIVE_INTERVAL_S
        while the stream is open — real audio chunks from feed_stream()
        share the same connection, so this only matters during a gap with
        no audio (agent speaking, caller pause). Cancelled by
        _evict_stream()/finalize_stream()/cancel_stream(); a send failure
        here just ends the loop quietly — feed_stream()'s own eviction
        path handles the dead connection when the next real chunk arrives."""
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_INTERVAL_S)
                await stream.ws.send(json.dumps({"type": "KeepAlive"}))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.info("DeepgramSTT: keepalive send failed session=%s — stream likely closed", session_id)

    async def _read_loop(self, stream: _LiveStream, session_id: str) -> None:
        # Collects every is_final segment Deepgram sends during the live
        # stream — interim (non-final) results aren't used for anything
        # yet, they exist for a future partial-transcript display, not this
        # turn-based pipeline's decision-making.
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
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            log.exception("DeepgramSTT: live stream read loop failed session=%s", session_id)

    async def finalize_stream(self, session_id: str, audio: bytes, sample_rate: int) -> SttResult:
        # audio/sample_rate accepted-but-unused: ISTT's contract lets
        # pipeline.py call every implementation identically. Deepgram
        # already has everything it needs from feed_stream()'s live
        # connection; only FasterWhisperSTT's fallback actually uses them.
        stream = self._streams.pop(session_id, None)
        if stream is None:
            # feed_stream() was never called (e.g. a 0-chunk utterance) —
            # nothing was ever streamed.
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
        return SttResult(text=text, confidence=stream.last_confidence)

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
