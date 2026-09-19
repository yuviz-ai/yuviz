# PRD: Live Calls Monitoring

## Problem
Today an admin/supervisor has no way to see what is happening across their tenant's calls while
those calls are still in progress — `services/config/calls.py` only reports on calls after they
end (`_status_of` derives `"live"` only as a side effect of `ended_at IS NULL`, and every other
query in that module — dashboard stats, usage trend, latency — windows on `started_at`/historical
aggregates). If a caller is stuck in a DTMF loop, an agent hands off badly, or a queue of callers
is waiting for a human right now, the operator finds out only when someone complains or the call
shows up in tomorrow's history. There is no live view, and no way to intervene in the moment.

## How this is solved elsewhere
Every mainstream contact-center platform (Five9, NICE CXone, Zoho Voice, the generic "call
barging" feature set) converges on the same three-tier intervention model: **Monitor** (silent
listen, neither party knows), **Whisper** (supervisor talks to the agent only, caller can't hear),
and **Barge** (supervisor joins the live audio for all parties) — unverified whether every vendor
implements all three, but Monitor and Barge are consistently named as the baseline pair, and
Whisper is the differentiator worth noting even though the request only asked for two. Twilio's
Voice Insights dashboard is the closest analog for the KPI-row-plus-live-list half of this
request: a filterable, auto-updating aggregate view over active/recent calls, not a raw audio
tap — Twilio does not ship "join the audio" in that same dashboard; that is a separate capability
(`<Conference>` with a supervisor participant) requiring the call to already be provisioned as a
multi-party conference leg, not a point-to-point stream.

That last fact is the one that reshapes this PRD. This platform's telephony bridge
(`services/vobiz/bridge.py`) is a **point-to-point shuttle**: one WebSocket carries mu-law audio
from the telephony gateway, one gRPC stream carries PCM16 audio to/from the Conversation Service,
and the file's own barge-in logic (`_turn_active`, `CancelGeneration`, `clearAudio`) only ever
mediates between *the caller* and *the AI agent's TTS* — mirroring the C++ Gateway's
`CallSession::on_speech_started`. There is no third leg, no audio mixer, no conference primitive
anywhere in `services/vobiz/` or the gateway notes referenced from it. Recommendation: build the
full KPI/live-table/pause-resume/export surface now — it is pure read/aggregation work this
codebase already has the shape for (`services/config/calls.py`'s tenant-scoped query pattern
extends cleanly to "still open" rows). Do **not** promise literal live-audio "Listen"/"Barge" in
this PRD's build; that requires new telephony-layer plumbing (a mixer/conference leg on the
gateway or Vobiz side) that does not exist in this repo today and is a materially different,
larger project. Ship the operator-facing request-and-fulfillment *workflow* for Listen/Barge now
(who can request it, against which call, audit-logged, gated by more than "authenticated"), with
the actual audio join wired in as a follow-on once the telephony-layer capability exists.

## Scope
- In: A KPI row per tenant — live call count, count in AI-only stage, count with a human
  connected, count waiting for a human, and channel utilization as a percentage of that tenant's
  concurrency cap (see Constraints for the new column this reads from).
- In: A live-updating table of the tenant's in-progress calls — masked caller number, agent/flow
  name, current stage, running duration, a sentiment indicator, and a short recent-transcript
  snippet — refreshed every 5 seconds.
- In: A pause/resume control on the live feed (freezes the table's auto-refresh; does not affect
  the underlying calls) and an export of the currently-displayed snapshot.
- In: A per-call "Request Listen" and "Request Barge" action that records the request, checks the
  requester is authorized to intervene in that specific call, and hands off to whatever
  audio-join mechanism exists at build time — a real audio join if the telephony-layer work has
  landed by then, otherwise a clearly-labeled "not yet available" outcome. Every request/grant/
  denial is audit-logged with actor, tenant, call, and outcome.
- In: Tenant isolation on every piece of the above, including a deliberate, explicit rule for how
  platform-scoped accounts use this feature (see Acceptance Criteria and Constraints) — see
  Acceptance Criteria.
- Out: Actual real-time audio mixing/bridging (a supervisor genuinely hearing or speaking into
  live call audio) — this PRD defines the operator-facing contract for it; the telephony-layer
  audio path is a separate, later PRD once gateway/Vobiz support exists. Building this without
  saying so would ship "Listen"/"Barge" buttons that lie about what they do.
- Out: "Whisper" (agent-only side channel) — not requested, and it inherits the same missing
  audio-mixing dependency as Barge, so it would only add scope without closing the gap.
- Out: Historical/completed-call analytics — that is the existing Dashboard/Usage Trends surface
  (`services/config/calls.py`'s `get_dashboard_stats`/`get_usage_trend`), untouched by this work.
- Out: An aggregated, cross-tenant "all calls, all tenants" view — even for platform-scoped
  accounts. This feature is always viewed one tenant at a time (see AC2).

## Acceptance criteria
1. Given a `tenant_admin` or `supervisor` authenticated for tenant A, when they open Live Calls,
   then the KPI row and table show only calls whose `calls.tenant_id` equals tenant A's slug —
   never a row belonging to any other tenant, matching the tenant-scoping predicate already used
   by `list_calls`/`get_call`/`get_dashboard_stats` in `services/config/calls.py`.
2. Given a platform-scoped account (NULL `tenant_id`, e.g. the same identity class documented for
   Conversation/Vobiz service accounts and superadmin console users), when it opens Live Calls,
   then it must first select a single tenant to view — exactly like any other tenant-scoped
   operator — and is then shown that one tenant's live calls only; there is no mode in this
   feature that aggregates calls across tenants simultaneously, for platform-scoped or any other
   account. This mirrors this codebase's existing pattern of treating "which tenant" and "may this
   actor act" as separate questions (`tenant_id IS NULL` answers scope, never authority) — a
   platform-scoped account's ability to select any tenant at all must still be gated on its role/
   identity, not granted merely because its own `tenant_id` is NULL.
3. Given a `viewer`-role or unauthenticated caller, when they call the Live Calls endpoint(s),
   then they receive the same 403/401 the rest of the console gives non-privileged callers on
   tenant-scoped surfaces — the response must not differ in status/body/latency depending on
   whether the tenant or call they attempted actually exists (no cross-tenant existence probing).
4. Given a call whose `ended_at` is still NULL for tenant A, when tenant A's operator views the
   live table, then that call appears with: masked caller number (never the raw MSISDN), current
   stage, elapsed duration computed from `started_at`, a sentiment indicator, and the most recent
   transcript snippet available for that `session_id`.
5. Given a call ends (`ended_at` becomes non-NULL) while the operator's view is open, then that
   call is removed from the live table within 5 seconds and is reflected in the KPI counts — it
   must not linger as "live" past its actual end for more than one refresh cycle.
6. Given the operator has paused the live feed, when new calls start or existing calls change
   stage, then the displayed table does not change until the operator resumes — the underlying
   5-second refresh is suspended client-side, not silently continuing to overwrite state the
   operator is trying to hold still.
7. Given the operator resumes after a pause, then the table reflects current live state within
   the next 5-second refresh, with no stale rows left over from before the pause.
8. Given the operator clicks "Export snapshot", then the exported data contains exactly the rows
   and columns currently rendered in their tenant-scoped view at that moment — never rows from
   another tenant, and never more/fewer rows than were on screen (no silent re-query against a
   wider or narrower scope than what was displayed).
9. Given an operator authorized for tenant A clicks "Request Listen" or "Request Barge" on a call
   belonging to tenant A, when the request is evaluated, then the system re-validates the
   requester's current authority against the database at request time — not solely against
   claims baked into their JWT — given `services/config/deps.py` resolves identity purely from
   the decoded token and does not reflect a role change or deactivation until the token expires;
   a supervisor demoted or deactivated after login must not be able to join or request to join a
   live call using a still-valid token.
10. Given an operator attempts "Request Listen"/"Request Barge" on a call belonging to a tenant
    other than their own (or, for a platform-scoped account, a tenant other than the one currently
    selected per AC2), then the request is denied with the same tenant-isolation guarantee as
    AC1–AC3 — denial must not reveal whether the `session_id` exists at all in another tenant.
11. Given any Listen/Barge request (granted, denied, or unavailable because the audio-join
    capability doesn't exist yet), then an audit record is written capturing actor identity,
    tenant, target `session_id`, action requested, and outcome — matching this codebase's existing
    posture that privileged actions are logged, not just gated.
12. Given the audio-join capability described in "How this is solved elsewhere" has not yet been
    built, when an operator clicks "Listen" or "Barge", then the UI shows an explicit
    not-yet-available state rather than a silent no-op or a button that appears to succeed —
    the request is still recorded per AC11.
13. Given a tenant's `max_concurrent_calls` value (see Constraints for the schema addition), when
    the KPI row renders channel utilization, then it computes that tenant's current live-call
    count as a percentage of that tenant's own `max_concurrent_calls` — never another tenant's
    value, never a hardcoded or global constant — and updates it on the same 5-second cycle as
    the rest of the KPI row.
14. Given two operators from the same tenant have the Live Calls view open concurrently, when one
    of them requests Barge on a call, then the other operator's view reflects that a human is now
    connected (KPI counts and per-call stage both update) within 5 seconds, without requiring a
    manual page reload.
15. Given the transcript snippet shown per call, when a viewer without transcript-read authority
    for that tenant would otherwise see it, then the snippet is withheld or the whole row's
    transcript field is omitted — the live table must not become a wider transcript-read surface
    than the existing per-call transcript endpoint already enforces (`get_transcript`'s tenant
    predicate in `services/config/calls.py`).
16. Given a tenant_admin edits their tenant's `max_concurrent_calls` value on the Tenants page,
    then the Live Calls KPI row's channel-utilization percentage reflects the new value on its
    next 5-second refresh, without requiring the operator to close and reopen the view.

## Constraints
- Tenant scoping must follow the pattern already established in `services/config/calls.py`:
  `calls.tenant_id` is a TEXT slug (not a UUID FK), and every query filters on it explicitly
  (`list_calls`, `get_call`, `get_transcript`, `get_dashboard_stats`) rather than relying on a
  join to implicitly exclude other tenants — per this codebase's own recorded lesson, a guard
  that only exists inside a JOIN's incidental exclusion is not a guard a test can prove.
- "Live" has exactly one existing definition in this schema: `ended_at IS NULL` on `calls`
  (`_status_of` in `calls.py`). This feature must reuse that definition, not invent a second one.
- Schema addition required: a new `tenants.max_concurrent_calls` column (INT, admin-editable from
  the Tenants page), mirroring the existing per-campaign `campaigns.max_concurrent_calls` column
  and its default/validation conventions. This is the only source for the channel-utilization KPI
  (AC13, AC16) — the feature must not fall back to a hardcoded or inferred cap.
- Identity resolution for any privileged action here (granting Listen/Barge, selecting a tenant as
  a platform-scoped account) goes through `services/config/deps.py`, which resolves purely from
  the JWT and does not re-check `deleted_at` or a changed role against the database. A request to
  join a live call, and a platform-scoped account's selection of which tenant to view, are exactly
  the kind of privileged, moment-of-use actions this codebase's lessons call out — this feature
  must not treat "has a valid token" as sufficient for granting either.
- Distinguish "is this actor privileged" from "which tenant is this actor scoped to" as two
  separate predicates, per this codebase's existing platform-scoped-account pattern (`tenant_id
  IS NULL` on Conversation/Vobiz service accounts and superadmin). A platform-scoped account's
  access into this feature is always one selected tenant at a time (never an aggregated
  cross-tenant view — see AC2), and the ability to select any given tenant must itself be gated
  on the account's role/identity, not inferred from having a NULL tenant.
- `services/vobiz/bridge.py`'s `cancel_event`/barge-in machinery is a single in-process
  `asyncio.Event` gating whether a tool call keeps running — it has no notion of a second audio
  participant and does not extend to a human operator joining call audio. Do not describe or
  build against it as if it were telephony-layer audio bridging; it is not.
- No WebSocket/SSE push channel currently exists in `admin-ui` for admin-console list/aggregate
  data (the one WebSocket in the codebase, in `TestAgentPanel.tsx`, is the browser-mic test-call
  bridge to `services/webcall`, an unrelated capability). This feature's live-updating table and
  KPI row are a 5-second polling refresh against a tenant-scoped read endpoint following the
  existing `services/config/calls.py` query style, not a new push transport. At 5-second intervals
  this is a repeated tenant-scoped query per open operator session — the design stage must account
  for this query load (indexing, connection pool headroom) rather than treat "poll every 5s" as
  free; introducing WebSocket/SSE instead is a bigger architectural decision left to the architect
  to accept or reject, not assumed here.
- Sentiment signal is a design-stage responsibility, not an assumed input: the architect must
  check whether `transcript_entries` or the Conversation Service already computes anything
  sentiment-like today. If nothing exists, the design must either (a) wire in a minimal signal
  (e.g., a coarse per-turn heuristic) as part of this feature, or (b) explicitly scope the
  sentiment indicator out of the first build and say so in the design doc — it must not silently
  assume a computed signal that isn't there.
- Caller-number masking must match whatever masking convention the console already applies
  elsewhere to phone numbers (if any exists) rather than inventing a new masking rule for this
  screen alone.

## Open questions
None.
