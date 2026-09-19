# Review: 01-prd.md (Call-flow executor) — round 2
VERDICT: AMBER

Round-1 findings, verified against the revised file:

1. [blocking → still blocking, different defect] Open Question 1 was removed and replaced by a Constraints bullet (line 76) citing `services/vobiz/bridge.py`'s `_vobiz_to_grpc()`, lines 342-343, event shape `{"event": "dtmf", "dtmf": {"digit": <key>}}`. The citation itself is accurate — I read those exact lines and they match verbatim. But the new bullet's conclusion is wrong: it says "no new transport, encoding, or event type needs to be introduced," when in fact one is. `_vobiz_to_grpc()` only *logs* the dtmf event (`log.info(...)`); unlike the `media` branch three lines above it, which builds a `pb.GatewayMessage(audio_chunk=...)` and forwards it across the gRPC stream, the `dtmf` branch builds nothing and forwards nothing. I checked `proto/voiceai/v1/conversation.proto`: `GatewayMessage`'s `oneof payload` has only `audio_chunk` (plus other non-DTMF arms) — there is no DTMF message type crossing the vobiz→conversation gRPC boundary at all. So the constraint correctly answers "how does a keypress reach the vobiz process" but not the question it was written to close — "how does a keypress reach `services/conversation`," where the executor this PRD is scoping actually lives. A new proto message (or a new oneof arm) is required and is currently un-scoped. Fix: correct the constraint to state that a new `GatewayMessage` oneof arm (e.g. `DtmfEvent`) must be added to carry the digit from vobiz to conversation — this is in-scope proto/plumbing work for this feature, not a solved prerequisite — or reopen it explicitly as an open question if the architect is meant to decide the proto shape.

2. [minor — resolved] Open Question 2 (OBD) is gone, and the Scope "Out" bullet for outbound/OBD (line 19) no longer has the stale "see Open Questions" cross-reference — it now reads as a flat, self-contained exclusion. Confirmed no dangling reference remains anywhere else (checked all "Open Questions" mentions in Scope: line 21's monitoring bullet still correctly points at Open Question 1, which is in fact the monitoring question post-renumbering).

3. [minor — resolved] New Scope "Out" bullet added at line 23 for post-transfer dial outcomes (busy/no-answer/failure), matching the ask.

Regression check:
- ACs 15-23 and 25/31/32: byte-for-byte unchanged from round 1; no AC text, numbering, or count changed (still 33 ACs, same content).
- No restructuring of sections; Open Questions renumbered 1-4 (old 3,4,5,6 → new 1,2,3,4) after removing old Q1 (DTMF) and old Q2 (OBD).
- Cross-reference check on the renumbering: ACs 12, 21, 23 reference "Open Questions" by name only, not by number, so the renumbering did not break any in-AC numeric citation. No AC anywhere cites an open question by number. No broken cross-references found.

Net: two of three findings are genuinely fixed. The DTMF finding was not fixed — it was converted from "wrongly marked as an open question" to "wrongly marked as already answered," which is worse for the next stage: an architect reading this constraint will believe the transport is a solved, no-op citation and won't budget the proto change it actually requires.
