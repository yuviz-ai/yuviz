# PRD: Transcript and turn-count accuracy

## Problem
Operators reading the dashboard and call transcripts see numbers and text that do not match what happened on the call. Flow-only calls with a clean outcome show as failed (one tenant: 16 failed of 17), stored transcripts contain a fake caller turn, out-of-order rows, text the caller never heard, or missing turns, and calls closed by the stale sweep or a restart after kill -9 keep `turn_count` 0 despite having transcript rows. Barge-in counts, the answering agent and the live stage are also wrong in several cases.

Market research skipped: this is codebase-specific persistence bookkeeping, not a solved-elsewhere product feature.

Scope source: issues #204, #182, #205 (items 1-6; item 7 belongs to the fix/tts-failure-handling batch and is excluded), #203 item 2 (items 1 and 3 of #203 are excluded). Assumption: the five items in #182 are exactly those listed in the issue body.

## Scope
- In: a call that ended cleanly inside a call flow (menu-timeout hangup, Dial bridged with TRANSFER_SUCCESS, play-and-hangup) is not counted as failed on the dashboard (#204).
- In: the stored transcript matches the call (#182): no `[session_summary]` caller turn and no effect on `turn_count` (the summary itself is still stored and still shown to operators); a recovery turn after a failed transfer stored in order; an interrupted turn stores only what was played; text containing NUL characters does not lose the turn; stale-sweep-closed calls get the true `turn_count`.
- In: after a restart that reconciles calls left open by kill -9 (and the dead-node reconcile), `turn_count` equals the number of stored transcript rows for each reconciled call (#203 item 2).
- In: barge-in count excludes hangups (#205.1); interrupted rows store spoken text, not generated text (#205.2, same fix as #182.3); the answering agent after an IVR handoff is recorded and the answering agent's call count includes it (#205.3); caller words are kept in model history when a reply fails TTS (#205.4); Live Calls does not show a ringing call-flow Dial as stage `ai` (#205.5); interrupted tool-calling rows record the tool calls made (#205.6).
- Out: TTS failure on a reply carrying END_CALL or TRANSFER (#205 item 7); owned by branch fix/tts-failure-handling.
- Out: #203 item 1 (close during stalled LLM) and item 3 (over-cap warning on frame boundary).
- Out: redesigning the dashboard's success/failed definitions beyond flow-only clean outcomes; backfilling historical rows.
- Out: changes to TTS sentence assembly in the pipeline beyond the minimum needed to learn what was spoken; any overlap with fix/tts-failure-handling is called out in the PR body.

## Acceptance criteria
1. Given a call that ended inside a call flow with zero model turns and a clean outcome (menu-timeout hangup, Dial with TRANSFER_SUCCESS, play-and-hangup), when the dashboard stats are computed, then it counts as success and not failed.
2. Given a call with `turn_count` 0 and close_reason TRANSFER_FAILED, when stats are computed, then it is still counted as failed.
3. Given a call with zero turns that ended abnormally (e.g. reconciled_stale with no transcript rows, caller drop before any flow outcome), when stats are computed, then it is still counted as failed.
4. Given a call still live (`ended_at` NULL), when stats are computed, then it is in neither success nor failed.
5. Given a post-transfer session summary is persisted, when the transcript is read, then no row has caller text `[session_summary]`, `turn_count` is unchanged by it, and the summary text is not presented as caller speech or included in any caller-text output.
5a. Given a post-transfer session summary was persisted for a call, when an operator opens that call's transcript or call view in the admin-ui (or reads it via the call API that serves it), then the summary text is still shown, labelled as a summary and not as a turn; a call with no summary shows none.
5b. Given the summary has no dedicated storage today (the transcript row written by the session finalizer is its only home), when the fix ships, then the summary is stored in a place that is not a counted turn, and any schema change lands in the same change as the code that reads it.
6. Given a transfer fails and the agent resumes with a recovery turn, when the transcript is read, then the recovery turn sits after the failed-transfer turn and before later turns, by turn number and by time.
7. Given the caller barges in mid-reply, when the turn is stored, then the AI text contains only the sentences or portion actually played to the caller, not what the LLM generated afterward.
8. Given a reply fully played, when the turn is stored, then the AI text equals the full reply and is not marked interrupted.
9. Given caller text or an AI reply containing a NUL character, when the turn is persisted, then the row is stored (NUL removed or replaced), the turn is not lost, and neighbouring turns and `turn_count` stay consistent (no gap such as count 12 with turns 5 and 6 missing).
10. Given a call closed by the stale sweep or dead-node reconcile that has N transcript rows, when it is closed, then `calls.turn_count` = N.
11. Given a kill -9 of the conversation service with calls open and transcript rows written, when the service restarts and reconciles, then each reconciled call has `turn_count` = its transcript row count and close_reason `reconciled_stale`.
12. Given reconciliation closes a call with zero transcript rows, then `turn_count` stays 0.
13. Given a reconcile runs on an instance, when other instances own live calls, then those calls are not closed or modified (existing node-scoping preserved).
14. Given the caller hangs up mid-turn without barging in, when the call ends, then `barge_in_count` is unchanged by that hangup; a genuine barge-in still adds 1.
15. Given a call handed off from an IVR agent to a sales agent that answers turns, when the call is read, then the answering agent is recorded and the sales agent's per-agent call list/count includes the call; the original IVR agent is still recorded.
16. Given a call that never hands off, when read, then agent attribution is unchanged.
17. Given a whole reply fails TTS, when the next turn is built, then the caller's words from the failed turn are present in the model's history and still in the transcript.
18. Given a call-flow Dial is ringing a human, when Live Calls is queried, then the stage is `waiting_for_human` (not `ai`); once bridged, `human_connected`; a later stage is never overwritten by an earlier one.
19. Given an interrupted turn in which a tool such as `pay_order` was invoked, when the transcript entry is read, then `tool_calls` lists the invoked tool(s) with name and outcome status, so the row shows the payment was sent.
20. Given a turn with no tools, when read, then `tool_calls` is empty/null as today.
21. Given tool-call arguments or results contain sensitive values (card, PIN, tokens), when recorded to `tool_calls`, then they are not stored in clear (names and status only, or existing redaction rules apply).
22. Given each fixed behaviour, when the suite runs, then a regression test exists that fails on the pre-fix code for that behaviour, using shipped configuration values and not modified constants.
23. Given the existing tests in `services/conversation/tests` and `services/config/tests`, when run after the change, then no previously passing test is broken (failures identical on base are pre-existing).

## Constraints
- Persistence lives in `services/conversation/transcript_builder.py`: per-session in-memory counters (`_turn_counts`, `_barge_in_counts`) and a per-session write chain (`_spawn`), `reconcile_stale_calls` (line ~86) and `reconcile_dead_nodes` (~120); ordering of writes per session must be preserved.
- Dashboard success/failed is defined in `services/config/calls.py:165-172` (`turn_count > 0` / `turn_count = 0 OR TRANSFER_FAILED`); any change must keep tenant scoping via `tenant_conn` and RLS-plus-explicit tenant predicate (lesson 36).
- The session summary is written via `services/conversation/session_finalizer.py:172-177` (`record_turn(..., "[session_summary]", ...)`); no summary column exists in `database/schema.sql` or `services/config/calls.py`, so that transcript row is the only place the summary lives today and removing it without a replacement drops every post-transfer summary (AC5, 5a, 5b). Other readers of calls/transcript rows (admin-ui, scripts, docs, test-chat in `services/config/agent_testing.py`, which increments `turn_count` itself) must be grepped before changing semantics (lesson 17). Where the summary is stored instead is the architect's decision.
- Sinks: anything newly written to `tool_calls`, transcript text or logs is persisted/visible; apply lesson 33 (credential-shaped values, DTMF/PIN variables must not leak).
- Any schema change must be applied via `database/schema.sql` with `psql -f` semantics (guard and destructive DDL in the same `DO` block; lessons 5, 10, 13) and land with the code that reads it (lesson 11).
- Keep changes in the persistence path; the pipeline's TTS sentence assembly is being changed concurrently on fix/tts-failure-handling (#174/#219/#220/#205.7). Where "text actually spoken" requires a pipeline touch, keep it minimal and list the overlap in the PR body.
- Async DB calls must not block the event loop and must be bounded (lessons 18, 52); any new timer/background writer must have a single writer per session (lesson 34).
- Base branch `redesign`; one PR. Run the tests with new files marked intent-to-add (lesson 55) and note any environment-only failures as BLOCKED-ENV (lesson 50).

## Open questions
- Which close_reason values (or what flow-outcome signal) mark a flow-only call as "clean" for #204? Assumption: a call with `turn_count` 0 is clean when its close_reason is a normal flow end (hangup node, TRANSFER_SUCCESS, announcement end), and failed otherwise; the architect should enumerate the real close_reason values and propose the exact predicate.
- For #205.3, should the answering agent be a new recorded attribute alongside the original `agent_id`, or should `agent_id` be updated at handoff? Assumption: keep the IVR agent and add the answering agent, since the issue says "nothing records which agent took the call". Per-agent call counts must then include calls the agent answered.
