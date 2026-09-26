# PRD: Cloudonix as a Managed Telephony Provider (Webhook + Media Stream Bridge)

## Problem
Inbound calls today reach the conversation engine only through our self-hosted
Kamailio + FreeSWITCH + C++ Gateway stack, which we operate and patch ourselves.
Cloudonix lets us keep our existing DID/carrier relationship while replacing that
self-hosted SIP/media stack with a managed one, but only if we can (a) answer its
per-call webhook fast enough to route correctly and (b) speak its Twilio
Media Streams-compatible WebSocket protocol into our existing gRPC `Converse()`
session — neither of which exists in this codebase yet.

## How this is solved elsewhere
Cloudonix's `<Connect><Stream>` model is a deliberate clone of Twilio Media
Streams: base64 JSON frames, `mulaw`/8kHz/mono, the same
`connected`/`start`/`media`/`dtmf`/`stop` event names. Dograh — a competitor
already shipping a Cloudonix integration — confirms the shape our request
describes: the webhook URL is bound to a Cloudonix **Voice Application**, not
to an individual DID, so every DID pointed at that application shares one
webhook and the receiving service must branch on the inbound `To` field itself
(unverified: exact field casing/name, confirm against the live trial account).
Dograh's docs list no warm-transfer capability for this integration.

Twilio's own docs state the load-bearing constraint directly: a bidirectional
`<Connect><Stream>` can only be torn down by ending the call, only one stream
is permitted per call, and it blocks any subsequent TwiML/CXML verb until the
WebSocket disconnects. Since Cloudonix's docs describe the same primitive and
do not confirm otherwise, **recommendation: build for cold/blind transfer as
the shipping capability, and treat "warm mid-call transfer while the Stream is
active" as a spike outcome, not a committed feature** — this matches how our
own AI-to-human transfer already works today (project memory
`architecture_decisions_voiceai` §6-7, live-validated cold-transfer path with
a confirmed voicemail fallback). If the trial account proves warm transfer
works, that becomes a follow-up PRD, not part of this one's acceptance
criteria.

Webhook authentication: Cloudonix sends a static `X-CX-APIKey` header rather
than an HMAC-signed payload (weaker than Stripe's or Twilio's request
signing). Recommendation: validate the header against a configured secret on
every request and treat a mismatch as a hard reject (no CXML returned) — do
not layer a "log and continue" fallback the way `resolve_did()`'s Redis-miss
path does, because a forged webhook is a different failure class than an
unprovisioned DID.

## Scope
- In: A publicly-reachable HTTP webhook endpoint that receives Cloudonix's
  per-call request (`To`/`From`/`CallSid`/`Domain` + `X-CX-APIKey`), validates
  the API key, resolves the called DID via the existing `did:{did}` Redis
  lookup (same key shape and same "never a rejected call, fall back to
  default tenant/agent on a miss" posture as `services/vobiz/redis_route.py`
  and `services/config/phone_numbers.py`), and returns CXML containing
  `<Connect><Stream>` pointing at our own WebSocket bridge, embedding
  whatever resolved routing context the bridge needs to open the right
  session.
- In: A WebSocket bridge service, architecturally parallel to
  `services/webcall/__main__.py`'s WS↔gRPC shuttling role, that speaks
  Cloudonix's Twilio-compatible protocol: consumes `start`/`media`/`dtmf`/
  `stop` JSON messages with base64 mulaw/8kHz/mono payloads, transcodes
  mulaw↔PCM16 and resamples 8kHz↔16kHz in both directions, and drives the
  existing gRPC `Converse()` session lifecycle (`SessionOpenRequest`,
  `AudioChunk`, `DtmfDigit`, `CancelGeneration`, session close) unchanged.
- In: Rejecting and logging (not silently dropping) any Cloudonix webhook
  request that fails `X-CX-APIKey` validation.
- In: An unrecognized/never-provisioned DID on the webhook path falls back to
  the platform default tenant/agent, exactly as the existing hot-path
  convention does — no new "reject the call" behavior introduced by this
  feature.
- In: A documented, run-once verification against the real Cloudonix trial
  account and API key already provided: (a) one full round-trip test call
  through the webhook and the new bridge into the existing conversation
  engine, with a recorded pass/fail and transcript, and (b) an explicit,
  written finding on whether a warm mid-call transfer to an external number
  is possible while `<Connect><Stream>` is active, or whether only
  cold/blind transfer is achievable.
- Out: DID/number provisioning, porting, or carrier trunk management — stays
  entirely on `services/did`'s existing `IDidProvider` registry and the
  current carrier relationship.
- Out: Any change to the conversation engine itself (STT, LLM, TTS,
  orchestrator, barge-in/VAD logic) — this feature only changes the
  telephony/transport leg in front of the existing `Converse()` RPC.
- Out: Decommissioning or cutting real traffic over from Kamailio/FreeSWITCH/
  the C++ Gateway. This PRD adds a second, parallel inbound path; migrating
  live DIDs onto it is a separate decision and a separate PRD.
- Out: Building a working warm mid-call transfer implementation. If the
  verification spike confirms it's possible, scoping and building it is
  follow-on work, not part of this feature's acceptance criteria.
- Out: Any change to `services/campaigns`' outbound dialer or trunk
  selection. Cloudonix's outbound trunk routing is investigated here only as
  far as it's needed to answer the cold/blind-transfer question in (b) above.

## Acceptance criteria
1. Given a Cloudonix webhook request with a valid `X-CX-APIKey` and a `To`
   value matching a DID present in `did:{did}` Redis, when the webhook is
   invoked and the Redis call returns within its bounded timeout (see
   Constraints), then the response is valid CXML containing `<Connect><Stream
   url="wss://...">` pointing at our bridge's public URL, returned within a
   latency budget that does not risk Cloudonix's own answer-timeout (confirm
   the exact threshold against the trial account; unverified until then).
2. Given a Cloudonix webhook request with a missing or incorrect
   `X-CX-APIKey`, when the webhook is invoked, then no CXML is returned, the
   request is rejected, and the rejection is logged with enough context to
   distinguish it from a routing miss.
3. Given a Cloudonix webhook request whose `To` does not match any
   provisioned DID in Redis, when the webhook is invoked, then the response
   still returns valid `<Connect><Stream>` CXML routed to the platform
   default tenant/agent, and the fallback is logged — the call is never
   rejected for this reason alone.
4. Given Redis is unreachable when the webhook fires, when DID resolution is
   attempted, then the webhook still returns valid CXML routed to the
   platform default tenant/agent (mirrors `resolve_did()`'s existing
   Redis-unreachable fallback), not an HTTP error.
5. Given Redis is reachable but slow — i.e. the lookup does not return an
   error, it simply has not returned by the bounded timeout configured for
   this call (see Constraints) — when the webhook is invoked, then the
   lookup is abandoned at the timeout and the webhook returns valid CXML
   routed to the platform default tenant/agent within the same overall
   latency budget as AC1, not left waiting on a slow Redis call until
   Cloudonix's own answer-timeout fires. A slow Redis is treated identically
   to an unreachable one for fallback purposes; the two cases must not
   produce different caller-facing behavior.
6. Given a live call whose CXML has connected `<Connect><Stream>` to our
   bridge, when Cloudonix sends a `start` message, then the bridge opens a
   `Converse()` gRPC session with a `SessionOpenRequest` populated from the
   webhook's resolved routing context (tenant/agent) and the call's
   caller/called DIDs.
7. Given an active bridged session, when Cloudonix sends `media` messages
   (base64 mulaw/8kHz/mono), then the bridge transcodes and resamples them to
   16-bit PCM/16kHz `AudioChunk`s and forwards them on the open gRPC stream
   with no audible corruption (verified by listening to a captured
   round-trip recording, not just by byte-length checks).
8. Given the conversation engine produces TTS audio on the gRPC stream, when
   the bridge receives it, then the bridge transcodes/resamples it to
   mulaw/8kHz and emits Cloudonix-protocol `media` messages back over the
   WebSocket, and the caller can hear intelligible audio.
9. Given Cloudonix sends a `dtmf` message during an active session, when the
   bridge receives it, then it is translated into a `DtmfDigit` message on
   the existing gRPC session, not dropped or logged-only.
10. Given Cloudonix sends a `stop` message or closes the WebSocket, when the
    bridge observes this, then it closes the gRPC session cleanly (same
    teardown path `services/webcall` uses on WS close), with no orphaned
    gRPC stream left open.
11. Given a burst of webhook requests exceeds the configured per-source rate
    limit (see Constraints), when the limit is exceeded, then excess requests
    are rejected before a Redis lookup or CXML response is generated — a
    rate-limited caller does not silently consume the default-tenant
    fallback path, and the rejection is logged separately from an
    `X-CX-APIKey` failure and from a routing miss.
12. Given the real Cloudonix trial account and API key, when a real inbound
    call is placed to a test DID, then a recorded, reviewable test produces a
    full webhook→bridge→`Converse()`→TTS-back-to-caller round trip, with a
    pass/fail verdict and transcript checked into the verification writeup.
13. Given the same trial account, when an in-progress `<Connect><Stream>`
    call attempts a transfer to an external number, then the verification
    writeup states explicitly, with evidence (call logs, recorded behavior),
    whether the stream survives a mid-call transfer attempt (warm) or only a
    call-ending transfer (cold/blind) is achievable — "not confirmed" is not
    an acceptable final answer once the trial account has been exercised.
14. Given the bridge process crashes or restarts mid-call, when Cloudonix's
    side notices the WebSocket drop, then the caller-facing failure mode is
    documented (does Cloudonix retry, replay CXML, or simply drop the call) —
    this must be observed against the trial account, not assumed.

## Constraints
- DID resolution must use the existing `did:{did}` Redis key exactly as
  written by `services/config/phone_numbers.py` (`{"tenant_slug",
  "agent_slug", "version"}`) and read by `services/vobiz/redis_route.py` —
  no new schema, no new cache key, no Postgres call on this path (hot-path
  rule, project memory `architecture_decisions_voiceai` and
  `phase5_coding_rules`).
- The webhook's Redis call for DID resolution must carry an explicit,
  short, hard timeout (not the client library's default, which may be
  unbounded or far longer than the webhook's own latency budget) and must
  treat a timeout exactly like `redis.RedisError` in `resolve_did()`:
  immediate fallback to the default tenant/agent, never a blocked or hung
  response. The specific timeout value must be set against whatever answer
  latency Cloudonix's platform actually enforces (open question below), not
  guessed independently of it.
- The public webhook endpoint must apply a request-rate limit (per source
  IP and/or per API key) ahead of DID resolution and CXML generation. This
  is a materially different exposure than `services/vobiz`'s existing
  Redis-miss-falls-back-to-default convention, which that service inherits
  safely because it only ever sees traffic arriving over the telephony
  network from a real carrier — this webhook is a public HTTP endpoint
  fronted only by a static, unsigned `X-CX-APIKey` header, so an attacker
  who obtains or guesses that key (or simply floods the endpoint before any
  key check completes) can otherwise drive unlimited default-tenant/agent
  session opens against the conversation engine at no cost. Rate limiting
  is required in addition to the API key, not a substitute for it; both
  must reject before the default-tenant fallback is invoked.
- The bridge must drive `Converse()` per `proto/voiceai/v1/conversation.proto`
  exactly as it stands: `SessionOpenRequest` (16kHz/mono/PCM16 audio,
  `caller_did`/`called_did` informational fields), `AudioChunk`,
  `DtmfDigit`, `CancelGeneration`. No proto changes are in scope; if the
  Cloudonix wire format needs a field this proto doesn't carry, that's an
  open question for the architect, not a silent workaround.
- The webhook and bridge are new, separate deployable services (not additions
  to `services/gateway`'s C++ codebase, `services/vobiz`, or
  `services/webcall`) — this is a new inbound path, not a modification of an
  existing one, per the "Out: no cutover" scope line above.
- No change to the conversation engine's barge-in/VAD logic (project memory
  `project_bargein_playback_design`) — the bridge is a transport pipe only,
  identical in spirit to `services/webcall/__main__.py`'s framing.
- `X-CX-APIKey` is a static shared secret, not a signed payload — store it
  via the existing secrets convention (`env:`/`k8s:`/`enc:` ref-strings,
  project memory `secrets_architecture`), not hardcoded or committed.
- The webhook must be publicly reachable without authentication ahead of the
  API-key check (Cloudonix cannot present any session/JWT of ours) — this is
  an intentional carve-out, distinct from the console's authenticated
  surface, and must not be built by relaxing any existing auth dependency.

## Open questions
1. What latency budget does Cloudonix's webhook enforce before it times out
   or falls back to an error tone? Not documented in the material we've
   gathered; changes how aggressively the webhook can afford to block on
   Redis, and directly sets the timeout value required by the Constraints
   section above. Needs answering against the trial account before AC1 and
   AC5 can be marked verified with a number.
2. Does Cloudonix bind the webhook per Voice Application (one URL, multiple
   DIDs, branch on `To`) or can we register a distinct webhook per DID? This
   changes whether the webhook needs its own routing table or can rely
   entirely on Redis. Dograh's docs suggest per-application, but this is
   unverified against our own account.
3. Exactly what identifiers does Cloudonix send that we should carry into
   `SessionOpenRequest.trace_id`/`call_id` — is `CallSid`/`Session` stable
   and unique enough to use directly, or does it need namespacing to avoid
   collision with our own generated IDs? Needed before AC6 can be
   implemented precisely.
4. What rate-limit thresholds are reasonable for this endpoint — Cloudonix's
   own expected call volume per tenant/DID is not yet known, and setting the
   limit too low would reject legitimate bursts (e.g. a campaign driving
   many simultaneous inbound calls to one DID). Needs a real number before
   AC11 can be implemented, not just designed.
