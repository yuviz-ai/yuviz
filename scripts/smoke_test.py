#!/usr/bin/env python3
"""
Gateway smoke test: send synthesized speech, expect a post-greeting TTS chunk.

Prerequisites:
  pip install websockets soundfile scipy
  macOS `say` command
  Build gateway:  cmake -B build && cmake --build build -j
  Run gateway:    ./build/gateway/voiceai_gateway config/gateway.yaml

Usage:
  python3 scripts/smoke_test.py [--host HOST] [--port PORT] [--timeout SECS]
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import subprocess
import sys
import tempfile
import time

try:
    import websockets
    import websockets.exceptions
except ImportError:
    print("FAIL: 'websockets' package not installed. Run: pip install websockets")
    sys.exit(1)

try:
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
except ImportError:
    print("FAIL: 'soundfile'/'scipy' not installed. Run: pip install soundfile scipy")
    sys.exit(1)


# ── Audio parameters (must match gateway.yaml media section) ─────────────────

SAMPLE_RATE   = 16_000   # Hz  — gateway.yaml media.sample_rate
FRAME_MS      = 20       # ms  — gateway.yaml media.frame_ms
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000   # 320 samples
FRAME_BYTES   = FRAME_SAMPLES * 2                # 640 bytes (16-bit LE)

_TEST_PHRASE = "Hello, what is two plus two?"

# Silence frame — all zeros, triggers VAD speech_ended after silence_threshold_ms.
_SILENCE_FRAME: bytes = bytes(FRAME_BYTES)


def _synthesize_test_speech(text: str, sample_rate: int) -> bytes:
    """Synthesize `text` via macOS `say` as L16 PCM; SileroVAD ignores sine tones."""
    fd, tmp_path = tempfile.mkstemp(suffix=".aiff")
    os.close(fd)
    try:
        subprocess.run(
            ["say", "-o", tmp_path, text],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        audio_f32, native_sr = sf.read(tmp_path, dtype="float32")
        if audio_f32.ndim == 2:
            audio_f32 = audio_f32.mean(axis=1).astype(np.float32)
        if native_sr != sample_rate:
            gcd = math.gcd(native_sr, sample_rate)
            audio_f32 = resample_poly(
                audio_f32, sample_rate // gcd, native_sr // gcd
            ).astype(np.float32)
        pcm_i16 = np.clip(audio_f32 * 32767, -32768, 32767).astype(np.int16)
        return pcm_i16.tobytes()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _chunk_frames(pcm: bytes) -> list[bytes]:
    """Split raw PCM into FRAME_BYTES chunks, zero-padding the last one."""
    frames = []
    for off in range(0, len(pcm), FRAME_BYTES):
        chunk = pcm[off:off + FRAME_BYTES]
        if len(chunk) < FRAME_BYTES:
            chunk = chunk + bytes(FRAME_BYTES - len(chunk))
        frames.append(chunk)
    return frames


_GREETING_GAP_S      = 0.8   # silence gap that marks "greeting playback done"
_GREETING_DEADLINE_S = 10.0  # max time to wait for the greeting to arrive+finish


async def _drain_greeting(ws) -> int:
    """Drain the greeting so it isn't mistaken for a reply; returns chunks drained."""
    deadline = time.monotonic() + _GREETING_DEADLINE_S
    chunks = 0
    seen_any = False
    while True:
        remaining = deadline - time.monotonic()
        wait_for = _GREETING_GAP_S if seen_any else remaining
        if wait_for <= 0:
            break
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=wait_for)
        except asyncio.TimeoutError:
            break
        if isinstance(msg, bytes):
            chunks += 1
            seen_any = True
    return chunks


# ── Smoke test ────────────────────────────────────────────────────────────────

async def smoke_test(host: str, port: int, timeout_s: float) -> bool:
    url = f"ws://{host}:{port}"
    print(f"Connecting to {url} …")

    try:
        async with websockets.connect(
            url,
            subprotocols=["voice-ai"],
            ping_interval=None,
            open_timeout=5,
        ) as ws:
            print("Connected. Draining greeting (if any) ...")
            greeting_chunks = await _drain_greeting(ws)
            print(f"  Drained {greeting_chunks} greeting chunk(s)")

            print(f"Synthesizing test phrase: {_TEST_PHRASE!r} ...")
            speech_pcm = _synthesize_test_speech(_TEST_PHRASE, SAMPLE_RATE)
            speech_chunks = _chunk_frames(speech_pcm)
            print(f"  {len(speech_chunks)} frames ({len(speech_chunks) * FRAME_MS} ms)")

            print("Sending real speech L16 PCM frames ...")
            t0 = time.monotonic()

            # Speech, then 1.5s of silence so VAD fires speech_ended.
            silence_frames = 1_500 // FRAME_MS   # 1.5 s of silence
            received_chunks = 0

            async def _send_loop():
                for frame in speech_chunks:
                    await ws.send(frame)
                    await asyncio.sleep(FRAME_MS / 1_000.0)
                for _ in range(silence_frames):
                    await ws.send(_SILENCE_FRAME)
                    await asyncio.sleep(FRAME_MS / 1_000.0)

            async def _recv_loop():
                nonlocal received_chunks
                async for msg in ws:
                    if isinstance(msg, bytes):
                        received_chunks += 1
                        elapsed = time.monotonic() - t0
                        print(f"  TTS chunk #{received_chunks}: {len(msg)} bytes "
                              f"at t={elapsed:.2f}s (post-greeting)")
                        return  # one chunk is enough for a smoke test

            send_task = asyncio.create_task(_send_loop())
            recv_task = asyncio.create_task(_recv_loop())
            try:
                # Keep the socket open past send_task; the pipeline takes 10-30s.
                await asyncio.wait_for(asyncio.shield(recv_task), timeout=timeout_s)
            except asyncio.TimeoutError:
                pass
            except websockets.exceptions.ConnectionClosed as e:
                print(f"  Connection closed during test: {e}")
            finally:
                for t in (send_task, recv_task):
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError,
                            websockets.exceptions.ConnectionClosed):
                        pass

    except ConnectionRefusedError:
        print(f"\nFAIL: connection refused on {url}")
        print("  Is the gateway running?")
        print("  cmake -B build && cmake --build build -j")
        print("  ./build/gateway/voiceai_gateway config/gateway.yaml")
        return False
    except OSError as e:
        print(f"\nFAIL: {e}")
        return False
    except Exception as e:
        print(f"\nFAIL: unexpected error — {type(e).__name__}: {e}")
        return False

    if received_chunks > 0:
        print(f"\nPASS: {received_chunks} post-greeting TTS chunk(s) received "
              f"— pipeline responded to sent audio")
        return True
    else:
        print(f"\nFAIL: no post-greeting TTS chunks received within {timeout_s:.0f}s")
        print("  Possible causes:")
        print("  1. set_on_binary not wired in CallSession (check wire_connection_callbacks)")
        print("  2. FSM not reaching Recognizing (check VAD energy threshold)")
        print("  3. PlaybackDrain not running (check playback_drain_.start())")
        print("  Hint: run gateway with --log-level debug and grep for 'FSM' transitions")
        return False


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Voice AI Gateway smoke test")
    p.add_argument("--host",    default="localhost", help="Gateway host (default: localhost)")
    p.add_argument("--port",    type=int, default=8080, help="Gateway WebSocket port (default: 8080)")
    p.add_argument("--timeout", type=float, default=45.0,
                   help="Max wait seconds (default: 45 — pipeline mode needs ~10-30s for STT+LLM+TTS)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    ok = asyncio.run(smoke_test(args.host, args.port, args.timeout))
    sys.exit(0 if ok else 1)
