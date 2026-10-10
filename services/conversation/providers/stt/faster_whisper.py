"""
FasterWhisperSTT — local transcription using faster-whisper (CTranslate2 backend).

Loads the model once at construction; transcribe() is CPU/GPU-bound and runs
in a thread-pool executor so it does not block the asyncio event loop.

pip install faster-whisper
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

import numpy as np

from libs.config_sdk.languages import LANGUAGES, normalize_language

from ..interfaces import INSTANCE_LANGUAGE, SttResult

log = logging.getLogger(__name__)


class FasterWhisperSTT:
    """
    ISTT implementation backed by faster-whisper.

    model_size  — "tiny", "base", "small", "medium", "large-v3"
    device      — "cpu" | "cuda" | "auto"
    compute_type — "int8" (CPU) | "float16" (GPU) | "float32"
    language    — ISO-639-1 code, e.g. "en".  None = auto-detect, reported back on
                  SttResult.language / language_confidence (needs a non-".en" model).
                  A per-call `language=` overrides it (accepts_language).
                  With `languages=` (the agent's supported languages) and no fixed
                  language, detection picks among those only, counting registry aliases
                  (Whisper labels spoken Hindi as Urdu), then decodes once in that language.
                  `require_language=(lang, min_conf)` skips the decode (empty text) when
                  detection lands on another language; the caller applies min_conf after.
    """

    accepts_language = True
    accepts_language_candidates = True

    def __init__(
        self,
        model_size:   str = "small",
        device:       str = "cpu",
        compute_type: str = "int8",
        language:     str | None = "en",
        beam_size:    int = 2,
    ) -> None:
        # Import eagerly so a missing package fails at startup, not at first call.
        from faster_whisper import WhisperModel
        self._WhisperModel  = WhisperModel
        self._model_size    = model_size
        self._device        = device
        self._compute_type  = compute_type
        self._language      = language
        self._beam_size     = beam_size
        self._model         = None   # populated by load()
        self._lock          = asyncio.Lock()
        self._warned_english_only = False

    async def load(self) -> None:
        """Load the model in a thread-pool executor; call after server.start()."""
        log.info("Loading FasterWhisper model=%s device=%s compute=%s",
                 self._model_size, self._device, self._compute_type)
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(
            None,
            lambda: self._WhisperModel(
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
                # Skip huggingface_hub's per-load revision check (latency; breaks air-gapped hosts).
                local_files_only=True,
            ),
        )
        log.info("FasterWhisper ready")

    async def transcribe(
        self, audio: bytes, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE,
        languages: tuple[str, ...] | None = None,
        require_language: tuple[str, float] | None = None,
    ) -> SttResult:
        if not audio or self._model is None:
            return SttResult(text="")

        language = self._language if language is INSTANCE_LANGUAGE else language
        # Whisper takes bare ISO 639-1 codes only: agents.language is often "en-US".
        language = normalize_language(language) if language else None
        if language is None and self._model_size.endswith(".en") and not self._warned_english_only:
            self._warned_english_only = True
            log.error(
                "FasterWhisper model=%s is English-only and cannot detect other languages — "
                "use a multilingual model (e.g. 'small') for multilingual agents", self._model_size,
            )
        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, functools.partial(
                    self._transcribe_sync, audio, sample_rate, language, languages, require_language,
                ),
            )

    async def feed_stream(
        self, session_id: str, chunk: bytes, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE,
        languages: tuple[str, ...] | None = None,
    ) -> None:
        # No incremental decode; finalize_stream() gets the full buffer.
        return

    async def finalize_stream(
        self, session_id: str, audio: bytes, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE,
        languages: tuple[str, ...] | None = None,
        require_language: tuple[str, float] | None = None,
    ) -> SttResult:
        return await self.transcribe(
            audio, sample_rate, language=language, languages=languages, require_language=require_language,
        )

    async def cancel_stream(self, session_id: str) -> None:
        return

    def _detect_among(self, pcm: np.ndarray, candidates: tuple[str, ...]) -> tuple[str, float | None]:
        """Best candidate language and its share of the candidates' probability (aliases
        folded in). One extra encoder pass, no decode; the transcribe that follows decodes once.

        The share, not the raw probability: Whisper spreads accented speech across many
        languages the agent can't use, which would leave English at ~0.5 for clearly
        English speech and block every switch back to it."""
        _top, _p, all_probs = self._model.detect_language(pcm)
        probs = dict(all_probs)
        def score(code: str) -> float:
            entry = LANGUAGES.get(code)
            aliases = entry.aliases if entry else ()
            return probs.get(code, 0.0) + sum(probs.get(a, 0.0) for a in aliases if a not in candidates)
        scores = {code: score(code) for code in candidates}
        best = max(scores, key=scores.get)
        total = sum(scores.values())
        return best, (scores[best] / total if total > 0 else None)

    def _transcribe_sync(
        self, audio: bytes, sample_rate: int, language: str | None,
        languages: tuple[str, ...] | None = None,
        require_language: tuple[str, float] | None = None,
    ) -> SttResult:
        # Convert raw L16 PCM bytes → float32 numpy array in [-1, 1].
        pcm = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0

        # FasterWhisper expects 16 kHz mono float32.  Resample if needed.
        if sample_rate != 16_000:
            pcm = self._resample(pcm, sample_rate, 16_000)

        detected: tuple[str, float | None] | None = None
        # English-only (.en) models can't detect; ctranslate2 raises if asked. They decode
        # English as they always did.
        if language is None and languages and self._model.model.is_multilingual:
            detected = self._detect_among(pcm, tuple(languages))
            # Short utterances are only kept in the session's language at the caller's minimum
            # confidence: skip the decode (and its encoder pass) for any blip that would be
            # dropped anyway. The caller passes 0 where the decoded text can still decide
            # (Devanagari "हाँ" is Hindi whatever Whisper's confidence).
            if require_language is not None and (
                detected[0] != require_language[0] or (detected[1] or 0.0) < require_language[1]
            ):
                return SttResult(text="", language=detected[0], language_confidence=detected[1])

        segments, info = self._model.transcribe(
            pcm,
            beam_size=self._beam_size,
            language=detected[0] if detected else language,
            vad_filter=False,   # Gateway EnergyVAD already gates speech; double-VAD
                                # strips short utterances and non-speech test tones.
            condition_on_previous_text=False,  # prevents hallucination loops
                                               # ("Bye. Bye. Bye. ...") on noisy audio
        )

        # Drop segments Whisper itself flags as probably-not-speech.  Echo and
        # line noise otherwise decode as polite filler ("Thank you very much.").
        kept: list[str] = []
        for seg in segments:
            # A short utterance (require_language set) has little else to show it is speech, so
            # no_speech_prob alone drops it.
            if seg.no_speech_prob > 0.6 and (require_language is not None or seg.avg_logprob < -0.8):
                log.debug(
                    "FasterWhisper dropped segment %r no_speech_prob=%.2f avg_logprob=%.2f",
                    seg.text.strip(), seg.no_speech_prob, seg.avg_logprob,
                )
                continue
            kept.append(seg.text.strip())

        text = " ".join(kept).strip()
        log.debug("FasterWhisper transcript=%r lang=%s p=%.2f", text, info.language, info.language_probability)
        if detected is not None:
            return SttResult(text=text, confidence=1.0, language=detected[0], language_confidence=detected[1])
        if language is not None:
            # Forced language: nothing was detected.
            return SttResult(text=text, confidence=1.0)
        return SttResult(
            text=text, confidence=1.0,
            language=normalize_language(info.language),
            language_confidence=float(info.language_probability),
        )

    @staticmethod
    def _resample(pcm: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
        import math
        from scipy.signal import resample_poly
        gcd = math.gcd(from_rate, to_rate)
        return resample_poly(pcm, to_rate // gcd, from_rate // gcd).astype(np.float32)
