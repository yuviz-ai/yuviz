"""Pre-render static macOS/Kokoro voice preview samples for LocalVoicePicker.

Re-run when engineCatalog.ts's VOICES_BY_ENGINE changes.
Usage: ./venv/bin/python scripts/generate_voice_samples.py
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

from services.conversation.providers.tts.kokoro import KokoroTTS
from services.conversation.providers.tts.macos import MacOSTTS

SAMPLE_RATE = 24_000
SAMPLE_TEXT = "Hi there, this is a quick preview of my voice."

OUT_ROOT = Path(__file__).resolve().parent.parent / "admin-ui" / "public" / "voice-samples"

# Mirrors admin-ui/lib/engineCatalog.ts's VOICES_BY_ENGINE; keep in sync by hand.
MACOS_VOICES = ["Samantha", "Karen", "Moira", "Alex", "Daniel"]
KOKORO_VOICES = [
    "af_sarah", "af_bella", "af_nicole",
    "bf_emma", "bf_isabella",
    "am_adam", "am_michael",
    "bm_george", "bm_lewis",
]


def _write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)  # 16-bit
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm)


async def generate_macos() -> None:
    for voice in MACOS_VOICES:
        tts = MacOSTTS(voice=voice)
        pcm = await tts.synthesize(SAMPLE_TEXT, SAMPLE_RATE)
        if not pcm:
            print(f"macos/{voice}: synthesis returned no audio, skipped")
            continue
        _write_wav(OUT_ROOT / "macos" / f"{voice}.wav", pcm)
        print(f"macos/{voice}: wrote {len(pcm)} bytes")


async def generate_kokoro() -> None:
    for voice in KOKORO_VOICES:
        tts = KokoroTTS(voice=voice)
        pcm = await tts.synthesize(SAMPLE_TEXT, SAMPLE_RATE)
        if not pcm:
            print(f"kokoro/{voice}: synthesis returned no audio, skipped")
            continue
        _write_wav(OUT_ROOT / "kokoro" / f"{voice}.wav", pcm)
        print(f"kokoro/{voice}: wrote {len(pcm)} bytes")


async def main() -> None:
    await generate_macos()
    await generate_kokoro()


if __name__ == "__main__":
    asyncio.run(main())
