Build a unified Telephony Provider service (services/telephony/) that replaces services/vobiz/ and services/cloudonix/ as two separate processes, and replaces campaigns/originate.py's raw ESL bridge for non-native providers. This consolidates outbound voice, outbound SMS, and inbound voice for every REST-capable vendor (Native/local FreeSWITCH+Kamailio, Vobiz, Cloudonix, and future vendors like Telnyx) behind one ITelephonyProvider/ISmsProvider interface and one process.

FULL DESIGN ALREADY LOCKED IN (from extensive design discussion this session, also captured in a published mockup artifact, screens 11-14):

## Scope boundary (critical, non-negotiable)
services/telephony is NOT on the native DID media/signaling hot path. Native-range DIDs continue to use Kamailio -> FreeSWITCH -> the C++ Gateway directly, unchanged. services/telephony owns provider-facing outbound operations, SMS, and webhook-capable inbound providers only. Both inbound paths (webhook vendors via services/telephony, and native via the Gateway) converge at the Conversation Service, but never share a code path. Do not "unify" native into this service — that would add latency and a new failure domain to the one path that can't afford either.

services/telephony also owns provider selection/configuration only (which vendor, what credentials, normalized call/SMS operations). It never touches agent prompts, tools, or conversation state -- that stays exclusively in the Conversation Service.

## Interface (extend libs/telephony_sdk/interface.py's existing ITelephonyProvider, don't replace it)
```python
class ITelephonyProvider(ABC):
    async def initiate_call(self, request: CallRequest) -> CallResult: ...
    def verify_webhook_signature(self, request: WebhookRequest) -> bool: ...
    def normalize_inbound_webhook(self, request: WebhookRequest) -> NormalizedInboundCall: ...
    def build_answer_response(self, websocket_url: str) -> str: ...  # still vendor-specific (CXML vs Vobiz's own XML)
    async def get_call_status(self, call_id: str) -> CallStatus: ...  # for idempotency verification after an ambiguous timeout
    async def transfer_call(self, call_id: str, destination: str) -> None: ...  # deferred: capability unverified for Vobiz, only Cloudonix's docs were checked
    def sensitive_credential_fields(self) -> list[str]: ...  # NEW: generalizes today's Cloudonix-only credential encryption special case
    async def check_health(self) -> bool: ...  # default True/no-op; used by the health-status background loop
```
New sibling `ISmsProvider`: `send_sms(from_number, to_number, body) -> message_id`, `get_message_status(message_id) -> status`, plus the same credential-declaration shape.

`NormalizedInboundCall` carries: provider_call_id, from_number, to_number, and optionally a `known_tenant_slug` (Cloudonix's adapter fills this in from its account/config_id resolution since Cloudonix's tenant boundary is NOT DID-based; Vobiz's adapter leaves it None since Vobiz has no prior account context and needs full DID->tenant+agent resolution).

## Inbound webhook handler stays thin -- this is the core discipline
The service owns the orchestration; adapters only supply vendor-specific translation:
```python
async def handle_inbound_webhook(provider_name, request):
    provider = registry.get(provider_name)
    if not provider.verify_webhook_signature(request):
        return 403
    call = provider.normalize_inbound_webhook(request)  # ONLY vendor-specific parsing lives in the adapter
    tenant_slug, agent_slug = await resolve_inbound_route(call, known_tenant_slug=call.known_tenant_slug)  # shared core logic, reuses existing did_route.py Redis lookup
    call_state.put(call.provider_call_id, CallContext(tenant_slug, agent_slug, call))
    return provider.build_answer_response(f"{PUBLIC_BASE_URL}/{provider_name}/stream/{call.provider_call_id}")
```
Today's services/vobiz/app.py does signature verification, DID resolution, AND CallMeta construction all inline in one handler -- that duplication is exactly what this refactor removes. A new vendor adapter should never need to re-implement DID resolution or call-context construction.

## Routes -- one canonical template, no per-vendor exceptions
Since nothing is in production yet (confirmed with the user -- Vobiz's webhook was never actually configured in any vendor dashboard, Cloudonix's points at an ephemeral free-tier tunnel), standardize ALL vendors onto one shape now while it's free to change:
- `/{provider}/voice/{account_ref}` -- inbound webhook (account_ref: Cloudonix uses its config_id there since that's how its tenant-boundary security model works per its own design doc at .sdlc/cloudonix-telephony-provider/02-design.md; Vobiz uses tenant_slug there instead of its current call-state-dict-only resolution)
- `/{provider}/call` -- outbound call trigger
- `/{provider}/stream/{call_uuid}` -- WS media bridge (reuses libs/media_stream_sdk/bridge.py's MediaStreamBridge unchanged -- it's already provider-agnostic via the `serializer` parameter)
- `/{provider}/status` -- hangup/ring callbacks
- `/sms/send` -- outbound SMS, provider resolved server-side from the tenant's telephony_configs, not from the URL

## Idempotent outbound operations (explicit requirement from the user)
Every outbound call AND every outbound SMS must be idempotent to avoid a double-dial/double-send if a provider request times out.
- Idempotency key minted ONCE per real attempt by the caller: for Campaigns, `sha256(campaign_id:contact_id:attempt_count)` generated in claim_next_pending() alongside the attempt_count bump; for one-shot SMS tool calls, `sha256(call_session_id:tool_call_id)`.
- services/telephony claims the key before touching the vendor: `SET idem:{provider}:{key} in_flight NX EX 120` (Redis -- same store already used for DID routing). If the NX fails, a duplicate/retried request for this exact attempt arrived -- return the cached result once known, never place a second vendor call.
- On vendor response, overwrite the Redis entry with the real outcome (call_uuid/message_id or error), same TTL, so a late duplicate within the window gets the real result instead of re-dialing/re-sending.
- The irreducible gap -- our request to the vendor times out with ZERO response at all: do NOT guess and do NOT requeue blindly. Call ITelephonyProvider.get_call_status() / ISmsProvider.get_message_status() to check whether the vendor actually placed/sent it before reporting failure back to the caller. Only report failure -- letting the caller mint a genuinely new attempt -- once that's confirmed.
- Add real HTTP-level retry (capped attempts, backoff, same idempotency key reused across retries) on the Campaigns-to-services/telephony hop itself -- today there is NONE, so even a transient blip between our own two services currently causes an avoidable extra dial one tick later.

## 5 design decisions to build in now (explicitly requested by the user, items 6-7 below explicitly deferred)
1. Credential encryption, generalized: `_normalize_credentials()` in services/config/telephony_configs.py currently ONLY encrypts Cloudonix's `api_keys` field (special-cased). Generalize it to use the new `sensitive_credential_fields()` interface method -- encrypt whatever fields a provider declares sensitive, via the existing `encrypt_secret()`/`enc:` convention, for every provider uniformly. Vobiz's `auth_token` is stored in PLAINTEXT today -- this must get sealed the same way.
2. Rate limiting, moved into the core: Cloudonix's existing services/cloudonix/app.py has real per-account/per-DID rate limiting via `FixedWindowCounter` (libs/ratelimit.py) with a specific discipline: applied BEFORE signature verification, authenticated-only buckets, no pre-auth denial surface (see R2-2 in .sdlc/cloudonix-telephony-provider/02-design.md). Move this into services/telephony's shared inbound entry point so EVERY vendor's webhook gets it, not just Cloudonix's. Vobiz has none today.
3. Fake provider for CI: `libs/telephony_sdk/providers/fake.py` -- FakeProvider(ITelephonyProvider), in-memory, deterministic call/message ids, scriptable outcomes (e.g. `script_outcome(call_id, "answered"|"no_answer"|"error")`). Registered as "fake" in the TelephonyProviderRegistry but excluded from `list_supported_providers()`'s admin-facing output -- resolvable directly by tests, never selectable in the real Admin UI. This is how the orchestration (verify -> normalize -> resolve -> idempotency) gets tested without spending real vendor money, which every single live verification this session required.
4. Health status, defined: `ITelephonyProvider.check_health() -> bool` (default True, no-op for providers with no cheap probe). A background loop -- same shape as Cloudonix's existing `AccountStore.refresh_loop` in services/cloudonix/accounts.py -- probes every configured provider every ~5 minutes, writes `{status, checked_at}` to Redis keyed by the telephony_config id. Healthy = last probe ok. Degraded = probe failed, not yet 2 consecutive failures (avoid single-blip flapping). Standby = no recent calls / never probed. The Admin UI's Telephony page already has health-dot badges (Healthy/Degraded/Standby) in its mockup that currently have no real backing data -- this closes that gap.
5. Cutover plan (sequenced, not big-bang):
   a. Build services/telephony alongside the two existing services -- nothing deleted yet.
   b. Vobiz migrates for free -- confirmed its dashboard webhook was never actually configured this session, so pointing it at the new service costs nothing.
   c. Cloudonix is the one real step: update its dashboard's Voice Application webhook URL to the new canonical path, verify with one real inbound test call, THEN stop services/cloudonix.
   d. services/campaigns/vobiz_originate.py (built earlier this session) becomes a generic telephony_originate.py pointed at the new service, dispatching by the provider name resolved via services/campaigns/campaigns.py's existing resolve_outbound_route().
   e. Delete services/vobiz/ and services/cloudonix/ directories only once both are confirmed stable on the new service.

## Explicitly deferred, do not build now (items 6-7)
6. Vobiz's real transfer-capability API was never checked (only Cloudonix's docs were checked, and even that only partially confirmed a mechanism -- see below).
7. Inbound SMS (a caller texting back) is out of scope -- this covers outbound SMS only (booking confirmations, future SMS campaigns).

## Transfer call, partially researched, still open (do not block on this, but capture the finding)
Checked https://developers.cloudonix.com/Documentation/aiAgentsServices/TheCloudonixAdvantage and Cloudonix's CXML verb docs. Findings: Cloudonix's <Dial> CXML verb bridges the current caller to <Sip>/<Number>/<Conference> with an `action` callback. Cloudonix's own recommended architecture for transfers is NOT a mid-call REST redirect trick -- it's connecting the client's own phone system directly to Cloudonix as a SIP destination, so a transfer is just Cloudonix dialing that destination via <Dial><Sip>, which they claim "always works regardless of the telephony provider's capabilities" (as opposed to the SIP REFER / new-call approach other platforms use). Hit the real Cloudonix API directly with the account's real trial API key: confirmed `GET https://api.cloudonix.io/calls` is real and returns live call history, but could NOT find a working detail/update route for a specific in-progress call (multiple plausible paths all 404'd) -- their REST reference itself says this section is "under deprecation," pointing to an interactive Swagger Playground that isn't crawlable. `transfer_call()` should be implemented as an ITelephonyProvider method with a NotImplementedError/clear "not yet supported" body for now, not blocked on, and not guessed at.

## Existing code to reuse, not rewrite
- libs/telephony_sdk/interface.py's ITelephonyProvider (extend, don't replace) and libs/telephony_sdk/registry.py's TelephonyProviderRegistry (already exactly the registry pattern needed).
- libs/telephony_sdk/did_route.py's resolve_did()/resolve_did_route() (the shared Redis "did:{did}" lookup, already used by the Gateway, Vobiz, and Cloudonix today -- do not build a second lookup mechanism).
- libs/media_stream_sdk/bridge.py's MediaStreamBridge (already provider-agnostic via the `serializer` parameter -- Vobiz and Cloudonix both already use it directly; no changes needed to it for this feature except it already gained an `on_session_start` callback hook this session for call_session_id capture, keep that).
- services/cloudonix/accounts.py's AccountStore refresh-loop pattern (reuse its shape for the new health-check loop).
- services/campaigns/campaigns.py's resolve_outbound_route() (already returns the provider name generically for a campaign's caller_id DID -- built this session).
- services/config/telephony_configs.py's list_supported_providers() (already introspects the registry for required_credential_fields() -- extend it to also expose sensitive_credential_fields() so the Admin UI's credential form can mask/encrypt the right fields per vendor automatically).

## Existing running state to account for (do not break)
- Yuviz tenant currently has a REAL, working Vobiz telephony_config (id a68e86b7-da1f-49a8-a2ec-d3e54f206913, real credentials, is_default_outbound=true) and a real phone_numbers row for +14165550177 bound to it, verified against one real, successful live call this session (answered, transcript flowed through the Conversation Service correctly). This must keep working after the cutover, not regress.
- Tenants 5000-5009 range DIDs are currently mislabeled with provider='cloudonix' in telephony_configs when they're actually the Native/local Kamailio+FreeSWITCH test range -- part of this work should include relabeling them to a new 'native' provider value (data migration, one-time, low risk since it's just DID bookkeeping metadata).

Please produce a PRD for this from the above -- it is already a fully-decided design from extensive back-and-forth review, not a fresh requirement to brainstorm. The PRD stage should formalize what's above into testable acceptance criteria, not second-guess the architecture decisions already made (scope boundary, interface shape, route template, idempotency mechanism, and the 5 items are all final).
