# Plan: store only the audio the caller actually heard on barge-in

Follow-up finding on PR #232 (independent, servicer.py:587): `on_sent` records a sentence once it is sent, but the gateway buffers and paces the audio, so sentences still buffered at barge-in time are stored as spoken.

## Event order (verified)
On a barge-in while the reply is playing (Speaking), `CallSession.cpp:494-495` clears the playback queue and sends `CancelGeneration`. `PlaybackFinished(interrupted=true)` follows only after `CancelAck` (`CallFSM.cpp:184-187`). The servicer handles it while the turn may still be running (`servicer.py:496-502`), so it can arrive before or after `record_turn` (`pipeline.py:~1215`). Both orders must work.

## Changes
1. **Proto** `proto/voiceai/v1/conversation.proto:137-141`: add `optional uint64 played_bytes = 4;` to PlaybackFinished. "optional" separates an older gateway (field absent) from a real 0.
2. **Stubs**: C++ stubs rebuild through CMake (`add_proto_library`). The Python stubs are committed; regenerate with `venv/bin/python -m grpc_tools.protoc -I proto --python_out=services/conversation/generated --pyi_out=services/conversation/generated --grpc_python_out=services/conversation/generated proto/voiceai/v1/conversation.proto` (grpcio-tools 1.83.0). Check that the `_pb2.py` header still says "Protobuf Python Version: 7.35.1" and that only the PlaybackFinished descriptor changed.
3. **Gateway `PlaybackDrain`** (`.h`/`.cpp`): add `std::atomic<uint64_t> sent_bytes_` and a `sent_bytes()` accessor. Add `payload.size()` after `send_binary` (`PlaybackDrain.cpp:55-56`); end-of-response markers add nothing.
4. **Gateway `CallSession`** (`.h`/`.cpp`, which owns `playback_drain_`):
   - Add `std::atomic<uint64_t> reply_start_bytes_` and `std::optional<uint64_t> barge_played_bytes_`.
   - `cbs.on_tts_started` (`:359`, sent once per reply before any audio): store `playback_drain_.sent_bytes()` as the reply's starting point.
   - Speaking barge-in branch (`:494`), after `cancel_playback()`: `barge_played_bytes_ = sent_bytes() - reply_start_bytes_`.
   - Thinking/Synthesizing barge-in branch (`:503`): set it to 0.
   - `h.on_playback_finished` (`:236`): pass `interrupted ? std::exchange(barge_played_bytes_, std::nullopt) : std::nullopt`.
5. **Gateway transport**: `send_playback_finished(..., std::optional<uint64_t> played_bytes = std::nullopt)` in `IConversationTransport.h:68`, `NullConversationTransport.h/.cpp` and `GrpcConversationTransport.h/.cpp`. Call `set_played_bytes` only when a value is present (`GrpcConversationTransport.cpp:179-186`). The default argument keeps #228's Null-transport test compiling.
6. **pipeline.py, sent ledger**:
   - `_TurnProgress` (`:102-110`) gets `sent: list[tuple[int, str]]` (cumulative end byte, piece text or "" for a filler) and `sent_bytes`, plus `mark_sent(piece, nbytes)`. `mark_sent` also appends to `spoken`, so the fallback is unchanged.
   - Set `on_sent=partial(progress.mark_sent, spoken_piece, len(tts_audio))` for every audio yield, fillers included, because filler audio shifts the offsets.
   - If it is small, extend the same ledger to the transfer-failed apology (`pipeline.py:~1430-1480`). Otherwise leave that path sent-based and say so.
7. **pipeline.py, `_heard_reply(sent, played_bytes)`**:
   - A piece runs from its first chunk to the start of the next piece.
   - Keep the pieces whose end is at or below `played_bytes`.
   - For the piece `played_bytes` falls inside, keep `floor(words × fraction)` words. Decision: keep this partial, since it is what the caller heard.
   - Run the result through `_spoken_reply(..., unspoken)` as today, using the CJK-aware join.
8. **pipeline.py, matching the two orders**:
   - Session state gets `heard_bytes: int | None` and `pending_heard: (turn_no, sent) | None`. Clear both at turn start (`:938`). This is safe because the gateway replays barge-in speech only after sending PlaybackFinished (`CallSession.cpp:587`).
   - Report already in: at the cancelled record (`:1213-1226`), if `heard_bytes` is set, store `_heard_reply(...)`.
   - Report not yet in: store the sent-based text and set `pending_heard` to the turn number returned by `record_turn`.
   - New `on_playback_interrupted(sid, played_bytes)`: skip it if `sid not in self._sessions`. If `pending_heard` is set and the heard text differs, call `transcripts.correct_turn_reply`. Otherwise set `heard_bytes`.
9. **transcript_builder.py**:
   - `record_turn` (`:197-227`) returns `turn_number`, or None when there is no pool.
   - New `correct_turn_reply(sid, turn_number, text)` goes through `self._spawn(sid, ...)`, so it is ordered after the INSERT on the same chain. It skips when there is no pool or no tenant slug (call already ended).
   - The UPDATE is guarded by tenant: `UPDATE transcript_entries SET ai_response=$3 WHERE session_id=$1 AND turn_number=$2 AND EXISTS (SELECT 1 FROM calls WHERE session_id=$1 AND tenant_id=$4)`. Run it under `tenant_conn(explicit_tenant=...)` and pass the text through `_strip_nul`.
10. **session.py:299 and servicer.py:502, :698**: `session.on_playback_finished(interrupted, played_bytes=None)` passes `pf.played_bytes if pf.HasField("played_bytes") else None`. When interrupted and a value is present, call `getattr(handler, "on_playback_interrupted", None)`, so the echo handler and test fakes are unaffected.
11. **Fallbacks**: a hang-up (no PlaybackFinished) or an older gateway (field absent) leaves the sent-based row unchanged. The hang-up path at `:916` is untouched.

## Tests
- `test_pipeline.py`: `_heard_reply` with whole pieces, a partial piece, a filler offset, 0 bytes and bytes past the end.
- `test_transcript_builder.py`: the UPDATE is tenant-guarded and ordered after the INSERT on the chain; nothing is written after `end_call`.
- `test_servicer_spoken_record.py`: report before the record, report after it (the row is corrected), hang-up, and field absent (both stay sent-based).
- C++: new `gateway/tests/playback_drain_test.cpp`, registered in `tests/CMakeLists.txt`, checks the byte counter (markers excluded).

## Overlap with #228
- No hunks in the servicer's tts_chunk send path or `HandlerResponse`.
- `CallSession.cpp` edits are separate hunks (`:236`, `:359`, `:494`, `:503`; #228 is at `:401-420`).
- In `pipeline.py`, the edits near `:1139` and `:1213` are close to #228's hunk at `@@ -1279`.

## Known limits (state them in the PR)
- Bytes count when sent to FreeSWITCH, so up to about 100 ms (`kLead`, roughly one word) of unplayed audio counts as heard.
- The gateway does not drop TTS chunks still in flight after a cancel. That pre-existing gap is not counted here.
- A partial sentence is cut by word proportion, which only approximates where the caller stopped hearing it.
- The correction is a second write to the row.
