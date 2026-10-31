# Code review (round 4, final: full working tree vs 97fce24)
VERDICT: GREEN
Coverage: callers 11 checked (every cancel_generation/send_cancel_generation/send_playback_finished/on_cancel_ack(_id) site in gateway/src, gateway/include and tests; #228's KeepCallbacksTransport) · raises 4 traced (a failed send resolves its id; an ack with an id erases that id; an ack without an id erases the oldest, std::map begin(); the Null transport acks synchronously after arming) · promises 6 matched · contracts 2 compared (proto `optional uint64 cancel_id = 3` on both messages, echoed at servicer.py:499/:699 via HasField; C++ stubs are generated at build time by add_proto_library) · failure-visibility done · env BLOCKED-ENV: full gateway build not possible locally (no cmake).

Verified:
- **Grpc sends both fields.** `send_cancel_generation` sets `cancel_id` when present (GrpcConversationTransport.cpp:171-186). The three-argument `send_playback_finished` sets `played_bytes` (:193-206).
- **Nothing calls the old methods to drop the fields.** CallSession uses only `send_cancel_generation` (CallSession.cpp:178) and the three-argument `send_playback_finished` (:246). The old one-argument `cancel_generation` and two-argument `send_playback_finished` are only forwarders inside Grpc and Null that pass nullopt.
- **Ack ordering.**
  - Grpc's reader thread calls `on_cancel_ack_id` (which resolves the gate) and then `on_cancel_ack` (which only posts the FSM event), :379-385. The gate is open before the FSM leaves BargeIn, so no chunk of the next reply can arrive while the gate is still closed for this ack.
  - An ack without an id (an older servicer) resolves the oldest outstanding cancel. Acks come back in cancel order on one stream, so that is the right one.
  - `on_cancel_ack` no longer touches the gate, so nothing resolves twice.
- **Defaults.**
  - Null overrides both methods and fires `on_cancel_ack_id(id)` then `on_cancel_ack`, as before.
  - #228's stub keeps `void cancel_generation(const std::string&)` and the two-argument `send_playback_finished`, which match the restored pure virtuals. It inherits the defaults, which forward to its no-ops. Its tests never barge in (no speech_started or cancel path), so the gate is never armed there.
- **TransferCoordinatorContext.** It now uses designated initializers in declaration order, with `freeswitch_host = ctx_.freeswitch_host`.

1. [minor] A default `send_cancel_generation` closes the gate on every barge-in — `gateway/include/transport/IConversationTransport.h` (default send_cancel_generation) — fails when: a transport that keeps the default (today only #228's test stub) acks a cancel. Such a transport only fires `on_cancel_ack`, never `on_cancel_ack_id`, yet the default returns true. The gate then stays closed for the full 3 s after every barge-in and drops the start of the next reply. No production transport is affected (Grpc and Null both override it). — fix: have the default return false ("no id-carrying ack will come"), so CallSession resolves the gate immediately and fails open.
2. [minor] Clang `-Wall` will warn that #228's stub hides an overloaded virtual — `origin/fix/tts-failure-handling:tests/call_session_end_call_test.cpp` — fails when: the stub is built with clang in Debug with `-Werror`. Its two-argument `send_playback_finished` override hides the three-argument virtual, and `-Woverloaded-virtual` is part of clang's `-Wall`. CI builds gcc Release without `-Werror`, so CI is unaffected. — fix: add `using IConversationTransport::send_playback_finished;` to the stub when #228 rebases.
