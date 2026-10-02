"""
MacOSTTS — local TTS using macOS built-in `say` command.

Zero external dependencies beyond soundfile (already required by scipy stack).
Uses the native NSSpeechSynthesizer via the say CLI; outputs AIFF then converts
to raw L16 PCM at the requested sample rate.

Voices (choose with --voice flag, e.g. "Samantha", "Alex", "Karen"):
  say -v "?" | grep en_US   — list US English voices
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import tempfile

import numpy as np

log = logging.getLogger(__name__)

_SAY_PATH = "/usr/bin/say"


class MacOSTTS:
    """
    ITTS implementation backed by macOS `say` command.

    voice  — any macOS TTS voice (e.g. "Samantha", "Alex", "Karen", "Moira").
             Run `say -v "?" | grep en` to list English voices.
    speed  — words per minute (180 = default natural rate)
    """

    def __init__(
        self,
        voice: str = "Samantha",
        speed: int = 180,
    ) -> None:
        self._voice = voice
        self._speed = speed
        log.info("MacOSTTS voice=%s speed=%d wpm", voice, speed)

    async def synthesize(self, text: str, sample_rate: int) -> bytes:
        if not text.strip():
            return b""

        fd, tmp_path = tempfile.mkstemp(suffix=".aiff")
        os.close(fd)

        try:
            # posix_spawn(3), not fork(): subprocess/asyncio both fork() on
            # Python 3.14 macOS, which trips gRPC's fork handlers.
            pid = os.posix_spawn(
                _SAY_PATH,
                ["say", "-v", self._voice, "-r", str(self._speed), text, "-o", tmp_path],
                os.environ,
                file_actions=[
                    (os.POSIX_SPAWN_OPEN, 1, "/dev/null", os.O_WRONLY, 0o666),
                    (os.POSIX_SPAWN_OPEN, 2, "/dev/null", os.O_WRONLY, 0o666),
                ],
            )

            loop = asyncio.get_running_loop()
            _, raw_status = await loop.run_in_executor(None, os.waitpid, pid, 0)
            rc = os.waitstatus_to_exitcode(raw_status)
            if rc != 0:
                log.error("MacOSTTS say failed rc=%d voice=%s", rc, self._voice)
                return b""

            return await loop.run_in_executor(None, self._decode, tmp_path, sample_rate)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    async def synthesize_stream(self, text: str, sample_rate: int):
        audio = await self.synthesize(text, sample_rate)
        if audio:
            yield audio

    def _decode(self, path: str, sample_rate: int) -> bytes:
        import soundfile as sf
        from scipy.signal import resample_poly

        try:
            audio_f32, native_sr = sf.read(path, dtype="float32")
        except Exception:
            log.exception("MacOSTTS read failed path=%s", path)
            return b""

        # Some voices output stereo AIFF; stereo would corrupt the gateway's mono L16 framing.
        if audio_f32.ndim == 2:
            audio_f32 = audio_f32.mean(axis=1).astype(np.float32)

        # Resample from native_sr (typically 22050) to the requested sample_rate.
        if native_sr != sample_rate:
            gcd = math.gcd(native_sr, sample_rate)
            audio_f32 = resample_poly(
                audio_f32, sample_rate // gcd, native_sr // gcd
            ).astype(np.float32)

        pcm_i16 = np.clip(audio_f32 * 32767, -32768, 32767).astype(np.int16)
        return pcm_i16.tobytes()
