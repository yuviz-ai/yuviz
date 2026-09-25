# PRD: Unified Telephony Provider Service (services/telephony/)

Skipping "how this is solved elsewhere": this is a consolidation of our own three existing
internal processes (services/vobiz/, services/cloudonix/, services/campaigns/originate.py's ESL
bridge) behind an interface we already designed and locked in a prior session — not a market-solved
problem to benchmark against.

## Problem
Outbound calling, outbound SMS, and inbound voice for every REST-capable telephony vendor are
split across services/vobiz/ (Vobiz only) and services/cloudonix/ (Cloudonix only), each
re-implementing webhook signature verification, DID resolution, and call-context construction
inline instead of sharing it. This duplication has already produced two real gaps: Vobiz's
`auth_token` is stored in plaintext (only Cloudonix's credentials are encrypted, via a
special-cased branch) and Cloudonix's per-account rate limiting protects only Cloudonix's inbound
webhook, leaving Vobiz's wide open. Every future REST vendor (Telnyx, etc.) means a third
copy-pasted process, and every outbound call/SMS today has zero idempotency, so a provider-side
timeout can double-dial or double-text a real phone number.

## Scope
- In: One `services/telephony/` process implementing outbound call initiation, outbound SMS,
  and inbound webhook handling for all REST-capable providers (Vobiz, Cloudonix, future vendors)
  behind `ITelephonyProvider`/`ISmsProvider`, replacing services/vobiz/ and services/cloudonix/
  as separate processes and replacing campaigns' raw ESL bridge for non-native providers.
- In: One canonical route template applied uniformly to every provider —
  `/{provider}/voice/{account_ref}` (inbound webhook), `/{provider}/call` (outbound trigger),
  `/{provider}/stream/{call_uuid}` (WS media bridge), `/{provider}/status` (hangup/ring
  callbacks), `/sms/send` (outbound SMS, provider resolved server-side).
- In: Idempotent outbound call and SMS placement, keyed by a caller-minted idempotency key,
  claimed in Redis before any vendor call, with a `get_call_status`/`get_message_status`
  reconciliation path for the caller-timeout-with-no-vendor-response case.
- In: Generalized credential encryption for every provider's declared sensitive fields
  (closing Vobiz's plaintext `auth_token` gap), and generalized per-account/per-DID inbound rate
  limiting applied to every provider's webhook entry point (closing Vobiz's unprotected gap).
- In: A `FakeProvider` registered for tests/CI, excluded from the admin-facing provider list.
- In: A background per-provider health-check loop publishing Healthy/Degraded/Standby status to
  Redis, backing the Admin UI's existing (currently unbacked) health-dot badges.
- In: A one-time data migration relabeling the 5000-5009 native test-range DIDs' `provider` value
  from `'cloudonix'` to `'native'` in `telephony_configs`.
- In: Sequenced cutover — Vobiz repointed first (free, unconfigured today), Cloudonix repointed
  second (one real dashboard change + one live verification call), campaigns' originate path
  made generic, old services deleted only after both are confirmed stable.

- Out: Native-range DID call/signaling hot path (Kamailio -> FreeSWITCH -> Gateway) — unchanged,
  never routes through this service, never implements `ITelephonyProvider`.
- Out: `transfer_call()` real implementation — ships as a defined interface method that raises
  "not yet supported"; no vendor capability is wired up.
- Out: Inbound SMS (a caller texting back) — outbound SMS only.
- Out: Agent prompts, tools, or any conversation-state handling — stays exclusively in the
  Conversation Service; this service only selects/configures providers and normalizes call/SMS
  operations.

## Acceptance criteria

**Interface and registry**
1. Given `libs/telephony_sdk/interface.py`, when `ITelephonyProvider` is inspected, then it
   exposes `initiate_call`, `verify_webhook_signature`, `normalize_inbound_webhook`,
   `build_answer_response`, `get_call_status`, `transfer_call`, `sensitive_credential_fields`,
   and `check_health`, and every existing concrete provider (Vobiz, Cloudonix) still satisfies it
   after the extension (no abstract method added that isn't implemented or given a usable default).
2. Given the new `ISmsProvider` interface, when inspected, then it exposes `send_sms`,
   `get_message_status`, and a credential-declaration method with the same shape as
   `sensitive_credential_fields()`.
3. Given a provider that does not implement `check_health()`, when the health loop probes it,
   then the default no-op returns `True` and the provider is never reported unhealthy on that
   basis alone.
4. Given `TelephonyProviderRegistry.list_supported_providers()` (or its Config Service caller),
   when it is invoked for the admin-facing provider list, then `"fake"` is never present, and
   when a test resolves `"fake"` directly from the registry, then it succeeds.
5. Given a call to an unimplemented `transfer_call()`, when invoked on Vobiz or Cloudonix, then
   it raises `NotImplementedError` (or an equivalent typed "not supported" error) rather than
   silently no-op'ing or partially executing a transfer.

**Inbound webhook orchestration**
6. Given a webhook POST to `/{provider}/voice/{account_ref}` with a valid signature for a
   registered provider, when handled, then the request passes through, in order: rate-limit
   check, signature verification, `normalize_inbound_webhook`, route resolution
   (`resolve_inbound_route` reusing the shared Redis DID lookup), call-context storage, and
   returns the provider-specific answer response pointing at
   `{PUBLIC_BASE_URL}/{provider}/stream/{call.provider_call_id}`.
7. Given a webhook POST with an invalid or missing signature, when handled, then the response is
   403 and no call context is created, no route resolution is attempted, and this check happens
   only after the rate-limit check (never before it, matching Cloudonix's existing ordering).
8. Given a normalized inbound call whose adapter filled `known_tenant_slug` (Cloudonix), when
   route resolution runs, then it uses that value directly without a DID lookup; given one that
   left it `None` (Vobiz), then route resolution performs the DID->tenant+agent lookup.
9. Given two different provider adapters, when their `handle_inbound_webhook` code paths are
   compared, then neither adapter contains DID-resolution or call-context-construction logic —
   only vendor-specific request parsing lives in the adapter; the shared orchestration function is
   provider-agnostic.
10. Given a webhook for a provider name not present in the registry, when the route is hit, then
    it returns 404 (or an equivalent typed not-found response) without touching rate limiting,
    signature verification, or call state.

**Inbound rate limiting**
11. Given N inbound webhook requests to any one provider's authenticated-only bucket within the
    configured window, where N exceeds the configured limit, then the (N+1)th request is rejected
    before signature verification runs, for every registered provider — not just Cloudonix.
12. Given an inbound request that fails signature verification, when it is retried repeatedly,
    then it still counts against the rate-limit bucket exactly as a valid request would (no
    pre-auth denial surface — an attacker cannot use failed auth to avoid consuming their own
    quota or to probe the limiter's behavior differently from a legitimate caller).

**Outbound idempotency — calls and SMS**
13. Given an outbound call request carrying an idempotency key not previously seen, when
    `services/telephony` processes it, then it sets `idem:{provider}:{key}` to `in_flight` with
    `NX EX 120` in Redis before making any vendor request.
14. Given a second outbound call/SMS request carrying the same idempotency key while the first is
    still `in_flight` (the `NX` claim fails), then the second request polls the Redis entry for a
    bounded wait no longer than the endpoint's own request timeout (10s), and: if the entry
    resolves to a final outcome within that window, returns the cached result (never placing a
    second vendor call); if the entry is still `in_flight` when the bound is hit, returns HTTP 202
    with the idempotency key (never a 5xx, never a fresh vendor dial, and never a block past the
    10s bound) so the caller can re-poll the same key later — the response is deterministic on
    every non-final outcome, not implementation-defined.
15. Given a vendor responds to an outbound call/SMS request (success or a definite error), when
    the response arrives, then the Redis entry for that idempotency key is overwritten with the
    real outcome (`call_uuid`/`message_id` or error) at the same TTL, so any later duplicate
    within the window returns that real outcome instead of retrying the vendor.
16. Given the request to the vendor times out with zero response, when `services/telephony`
    handles that ambiguity, then it calls `get_call_status()`/`get_message_status()` before
    reporting anything back to the caller, and only reports failure (permitting the caller to
    mint a new, distinct idempotency key for a genuinely new attempt) once that status check
    confirms the vendor never placed/sent it.
17. Given the vendor status check confirms the call/SMS actually was placed/sent despite the
    timeout, then `services/telephony` reports success with the real vendor id — it never reports
    failure for an operation the vendor actually completed.
18. Given the Campaigns worker's `claim_next_pending()`, when it bumps `attempt_count` for a
    contact, then it mints exactly one idempotency key `sha256(campaign_id:contact_id:attempt_count)`
    for that attempt, and a retry of the same attempt (not a new attempt) reuses that same key
    rather than minting a new one.
19. Given a one-shot SMS tool call from a live conversation, when it triggers an outbound SMS,
    then the idempotency key is `sha256(call_session_id:tool_call_id)`.
20. Given the HTTP hop from Campaigns (or any caller) to `services/telephony` itself fails
    transiently (connection reset, timeout, 5xx), when the caller retries, then the retry is
    capped in attempt count, uses backoff, and reuses the same idempotency key — it never mints a
    new key for a retry of the same underlying attempt.

**Credential encryption**
21. Given a `telephony_configs` row created or updated for any registered provider, when
    `_normalize_credentials()` runs, then every field named in that provider's
    `sensitive_credential_fields()` is sealed via `encrypt_secret()`/the `enc:` convention before
    the row reaches Postgres — not just Cloudonix's `api_keys`.
22. Given Vobiz's `auth_token` field specifically, when a Vobiz `telephony_configs` row is
    created after this change, then `auth_token` is stored as an `enc:`-prefixed value, never
    plaintext.
23. Given an existing `enc:`-prefixed value passed back in on update, when normalized, then it is
    kept verbatim (not double-encrypted); given a raw `env:`/`k8s:` reference passed in, then it
    is rejected the same way Cloudonix's current handling rejects it.
24. Given `services/config`'s provider-discovery endpoint (`list_supported_providers()` or
    equivalent), when called, then it exposes `sensitive_credential_fields()` per provider
    alongside `required_credential_fields()`, so the Admin UI's credential form can mask/encrypt
    the right fields automatically for every vendor, not a hardcoded list.

**Health status**
25. Given a registered provider with no recent probe, when the health loop has not yet run for
    it, then its reported status is Standby.
26. Given a provider whose most recent probe succeeded, then its status is Healthy.
27. Given a provider whose most recent probe failed but the immediately preceding probe
    succeeded (a single blip), then its status is Degraded, not Healthy and not a harder-failure
    state — flapping on one failed probe must not read as anything worse than Degraded.
28. Given the health loop, when it runs, then it probes every currently configured provider on
    an interval of approximately 5 minutes and writes `{status, checked_at}` to Redis keyed by
    the telephony_config id, readable by the Admin UI's Telephony page health-dot badges.

**Data migration (5000-5009 relabel)**
29. Given the 5000-5009 native test-range DIDs' `telephony_configs` rows currently labeled
    `provider='cloudonix'`, when the one-time migration runs, then their `provider` value becomes
    `'native'` and no other field on those rows changes.
30. Given the migration has run, when any code path reads those rows' `provider` field, then
    nothing in the native call/signaling hot path (Kamailio/FreeSWITCH/Gateway) consults this
    value at all — the relabel is bookkeeping only and produces no behavior change on that path.
31. Given the migration runs a second time (re-applied), then it is a no-op — rows already
    labeled `'native'` are left unchanged, not double-processed or errored.

**Cutover and regression**
32. Given the Yuviz tenant's existing real Vobiz `telephony_config`
    (`a68e86b7-da1f-49a8-a2ec-d3e54f206913`) and its `phone_numbers` row for `+14165550177`, when
    `services/telephony` is live and Vobiz is repointed at it, then an inbound call to
    `+14165550177` is answered, media streams through, and the transcript flows through the
    Conversation Service exactly as it did before the cutover.
33. Given `services/campaigns`, when its originate path is generalized to
    `telephony_originate.py`, then it dispatches to the provider resolved by the existing
    `resolve_outbound_route()` and no longer constructs a raw ESL bridge for any non-native
    provider.
34. Given services/vobiz/ and services/cloudonix/ are still running alongside
    services/telephony/ during the migration window, then no route, DID, or credential is served
    by two processes at once — each `telephony_configs` row's traffic is cut over atomically
    (per the sequenced plan), not split.
35. Given both Vobiz and Cloudonix traffic are confirmed stable on `services/telephony`, when
    services/vobiz/ and services/cloudonix/ are deleted, then no remaining code path (Campaigns,
    Admin UI, docs, docker-compose) still references the deleted services' routes or ports.

## Constraints
- Extend `libs/telephony_sdk/interface.py`'s `ITelephonyProvider` and
  `libs/telephony_sdk/registry.py`'s `TelephonyProviderRegistry` in place — do not replace them.
  As read in this checkout, `ITelephonyProvider` currently omits `normalize_inbound_webhook`,
  `sensitive_credential_fields`, and `check_health`; these are additive abstract/default methods
  per the locked design, and every existing concrete provider must be updated to satisfy the
  extended interface in the same change that extends it.
- `services/telephony` must never call Postgres or `services.config` synchronously on the inbound
  webhook or outbound-call hot path — Redis-only lookups for routing/idempotency, matching this
  repo's hot/cold path split (`.sdlc/lessons.md` architecture conventions; see
  `libs/config_sdk` and the Gateway's existing Redis-only DID lookup pattern).
- Credential sealing must go through the existing `enc:` convention
  (`libs/config_sdk/secrets.py`'s `encrypt_secret()`/`is_encrypted()`) — no new encryption scheme.
- `telephony_configs.credentials` is tenant-writable JSONB; per the existing Cloudonix handling
  in `services/config/telephony_configs.py`, a raw `env:`/`k8s:` reference in a credential field
  must be rejected, not resolved — those schemes are for admin-entered infra config, not tenant
  input (`.sdlc/lessons.md` #37).
- `services/telephony` owns provider selection/config/normalized operations only; it must never
  touch agent prompts, tools, or conversation state (Conversation Service's exclusive domain).
- Native-range DID calls never route through `services/telephony` and `ITelephonyProvider` is
  never implemented by the native Gateway path — this boundary is load-bearing for hot-path
  latency and must not be crossed by this work.
- `MediaStreamBridge` (`libs/media_stream_sdk/bridge.py`) is reused unchanged, including its
  existing `on_session_start` hook.
- `services/cloudonix/`, `libs/telephony_sdk/did_route.py`, `libs/telephony_sdk/providers/cloudonix.py`,
  `libs/ratelimit.py`, and `services/campaigns/vobiz_originate.py` are all present and intact in
  this checkout (re-verified after a transient working-tree scare, see Open Questions) — build
  against them as the "existing code to reuse, not rewrite" the request describes; do not
  recreate any of them from scratch.

## Open questions
1. Resolved — was a transient working-tree issue, not a real gap. An earlier pass of this
   checkout found `services/cloudonix/`, `libs/telephony_sdk/did_route.py`,
   `libs/telephony_sdk/providers/cloudonix.py`, and `libs/ratelimit.py` missing; this turned out
   to be an accidental `git stash drop` in the coordinator's own session, since restored from the
   dangling stash and re-verified present and matching what the design session read (confirmed
   via direct filesystem check: all five paths — including `services/campaigns/vobiz_originate.py`
   — exist with the expected content). No action needed; cutover step (c) is a repoint of real,
   running code as the request describes.
2. The design's idempotency key for SMS (`sha256(call_session_id:tool_call_id)`) assumes the
   tool-call layer never retries the same `tool_call_id` with different SMS content; if a tool
   implementation ever regenerates a `tool_call_id` per retry, the key stops deduplicating. Out
   of scope to fix here, but worth a one-line confirmation from whoever owns the
   tool-execution retry path.
