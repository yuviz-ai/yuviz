Build a "Live Calls" monitoring page for the admin console, matching the approved
console-redesign mockup's Live Calls screen.

Context: the console today has no real-time view of in-progress calls. An operator
(tenant admin/supervisor) needs to see what is happening across the tenant's active
calls right now, and take two actions on any one of them: listen in, and barge in
(speak into the call, interrupting the AI agent).

From the mockup, the screen needs:
- A KPI row: live calls right now, how many are being handled by the AI agent only,
  how many have a human connected, how many are waiting for a human, and channels
  used as a percentage of the tenant's concurrency cap.
- A live-updating table of in-progress calls: caller number (masked appropriately),
  which AI agent/flow is handling it, current stage (e.g. "AI conversation",
  "waiting for human", "human connected", "DTMF menu", "language detect"), call
  duration so far, a sentiment indicator, and a live transcript snippet.
- Per-call actions: "Listen" (join the call audio read-only) and "Barge" (join and
  speak, interrupting the agent).
- A way to pause/resume the live stream, and export a snapshot of the current view.

This needs real backend work, not just UI: a way to stream or poll in-progress call
state (which calls exist right now, their stage, duration, sentiment, a recent
transcript window) scoped correctly per tenant, and wiring "Listen"/"Barge" into
whatever mechanism the conversation/telephony stack already uses for barge-in
(the conversation service has an existing cancel_event/barge-in path used for
tool-call interruption — investigate whether it extends to a human operator
joining a live call, or whether this needs new plumbing through the gateway).

Tenant isolation matters here specifically: a tenant's admin/supervisor must see
only their own tenant's live calls, never another tenant's, and a platform-scoped
account's reach into this view needs to be deliberate, not incidental.
