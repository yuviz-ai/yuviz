# Issue #204
title:	[medium] Flow-only calls have turn_count 0, so the dashboard counts them as failed
state:	OPEN
author:	ckss2k17
labels:	bug
comments:	0
assignees:	
projects:	
milestone:	
issue-type:	
parent:	
sub-issues:	
sub-issues-completed:	
blocked-by:	
blocking:	
number:	204
--
Calls handled inside a call flow (menu timeout hangup, a Dial that bridged with TRANSFER_SUCCESS, play-and-hangup announcements) have `turn_count` 0, which the dashboard counts as failed: one tenant showed 16 failed out of 17 calls. Expected: flow-only calls with a clean outcome are not counted as failed.

Found by the QA retest of PR #200 (`.sdlc/conversation-layer/11-qa-retest-persistence.md`).

---

# Issue #182
title:	[low] Transcript does not match what happened (five ways)
state:	OPEN
author:	ckss2k17
labels:	bug
comments:	0
assignees:	
projects:	
milestone:	
issue-type:	
parent:	
sub-issues:	
sub-issues-completed:	
blocked-by:	
blocking:	
number:	182
--
1) post-transfer summary stored as a caller turn `[session_summary]` and counted in `turn_count`; 2) recovery turn after a failed transfer stored at the end, out of order; 3) interrupted turn stores more text than was played; 4) a NUL character in caller text or reply fails the insert and loses the turn (turn_count 12 vs missing turns 5 and 6); 5) stale-sweep-closed calls keep `turn_count` 0 despite transcript rows.

Found by adversarial QA of the conversation layer at `92aa06a` (origin/redesign). Report: `.sdlc/conversation-layer/07-qa-report.md` (defect #15). Stubbed providers, private Postgres/Redis.

---

# Issue #205
title:	[low] Persistence leftovers: hangup counted as barge-in, interrupted rows store generated not spoken text, handoff row names only the IVR agent, caller words dropped from history on TTS failure, ringing dial shown as stage 'ai', transcript_entries.tool_calls never filled, TTS failure on END_CALL hangs up silently
state:	OPEN
author:	ckss2k17
labels:	bug
comments:	0
assignees:	
projects:	
milestone:	
issue-type:	
parent:	
sub-issues:	
sub-issues-completed:	
blocked-by:	
blocking:	
number:	205
--
1) A hangup mid-turn adds 1 to `barge_in_count` (base 0). 2) An interrupted row stores what the LLM had generated, not what was spoken (one sentence past the audio the caller got, sometimes a half sentence). 3) After an IVR handoff `agent_id` names the IVR agent and nothing records which agent took the call; the sales agent shows 0 calls though it answered 12 turns. 4) When a whole reply fails TTS the caller's words are also dropped from the model's history though the transcript keeps them. 5) Live Calls shows a call-flow Dial that is ringing a human as stage 'ai'. 6) (pre-existing) `transcript_entries.tool_calls` is never filled so an interrupted `pay_order` row doesn't show the payment was sent. 7) (pre-existing) TTS failure on a reply carrying END_CALL hangs up in silence; with TRANSFER the transfer goes out unannounced.

Found by the QA retest of PR #200 (`.sdlc/conversation-layer/11-qa-retest-persistence.md`).

---

# Issue #203
title:	[low] Session lifecycle leftovers: close during a stalled LLM keeps the session live ~28 s; kill -9 restart leaves turn_count 0; over-cap warning missing on a frame boundary
state:	OPEN
author:	ckss2k17
labels:	bug
comments:	0
assignees:	
projects:	
milestone:	
issue-type:	
parent:	
sub-issues:	
sub-issues-completed:	
blocked-by:	
blocking:	
number:	203
--
1) A close (webcall 28.4 s, gRPC half-close 28.3 s) during a stalled LLM keeps the session live until the LLM times out; a client-side cancel ends it in 3 ms. 2) After a kill -9 the restart reconciles open calls (`reconciled_stale`) but leaves `turn_count` 0 even when transcript rows exist (pre-existing). 3) The 'over cap' warning is missing when the cap is hit exactly on a frame boundary (5 truncations, 1 warning).

Found by the QA retest of PR #198 (`.sdlc/conversation-layer/11-qa-retest-lifecycle.md`).

---

