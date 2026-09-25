# Design: Cloudonix webhook + Media Stream bridge

> Revision 3 — after security round 2 (`.sdlc/cloudonix-telephony-provider/02-security.md`).
> R2-1 (critical, boundary resting on the never-written `phone_numbers.telephony_config_id`),
> R2-2 (high, pre-auth limiter check gating genuine traffic) and R2-3 (high, tenant-writable
> `app_id` shadowing) are fixed below by **removing** the three mechanisms that carried them
> rather than patching them: the DID↔account column, the pre-auth denial bucket, and the
> free-form `app_id`. Carried-forward mediums/lows (concurrent-session cap, dump-file path,
> token-in-URL) are knowingly still open and are flagged at their sites.
>
> Revision 4 — one out-of-loop fix: a Cloudonix account's key material is **`enc:`-only and
> never passes through `CompositeSecretResolver`**. Tenant-writable `env:`/`k8s:` refs would
> have turned a tenant-editable JSONB field into an arbitrary host env-var and absolute-path
> file read (`libs/config_sdk/secret_resolver.py:35-68` — `K8sFileResolver` does
> `mount_root / path_part`, and `Path("/var/run/secrets") / "/etc/passwd"` is `/etc/passwd`).
> See `## Interfaces → Account key material`.

## Approach
Cloudonix's wire protocol is a near-clone of the Vobiz/Twilio Media Streams protocol
`services/vobiz/bridge.py` already speaks live, and its per-call webhook is the same
shape as `services/vobiz/app.py`'s `/vobiz/answer`. So the whole feature reduces to:
extract the media-bridge machinery that is *not* provider-specific (mulaw↔PCM16
transcoding, the audio-delay hold, SileroVAD barge-in, the playback pacer, the
single-writer gRPC queue, the turn watchdog — every one of which was earned from a live
bug, see project memory `project_vobiz_integration`) into `libs/media_stream_sdk/`,
behind a ~5-method serializer seam that is the only place the JSON event names and frame
shapes live; then `services/cloudonix/` is a new FastAPI deployable: webhook → admission
→ bounded `did:{did}` lookup → CXML, plus a WebSocket route that constructs the shared
bridge with a `CloudonixSerializer`. The obvious alternative — copying `bridge.py` into
`services/cloudonix/` — was rejected because it forks 470 lines of hard-won barge-in and
pacing behavior that would then silently lack every future fix.

This repo has done exactly this extraction before: `libs/telephony_sdk` and `libs/vad_sdk`
are both "pulled out of `services/vobiz`" (project memory `libs_telephony_vad_sdk`).
`libs/media_stream_sdk/` is the third instance of the same move, not a new abstraction.

**A Cloudonix Voice Application belongs to exactly one tenant, and that is the whole
tenant boundary.** Revision 2 tried to let one account carry many tenants' DIDs and
policed it with `phone_numbers.telephony_config_id` — a column nothing in the tree writes,
with a non-composite FK. Revision 3 drops that ambition and that column. The webhook is
mounted at `/cloudonix/voice/{telephony_config_id}`, where the path segment is the
**primary key** of a `telephony_configs` row (`database/telephony_schema.sql:14-15`), and
that row's `tenant_id` is `NOT NULL REFERENCES tenants(id)`. So the authenticated account
*is* a tenant, decided by a column that already exists, is already written by
`services/config/telephony_configs.py`, and is already constrained. Admission then reduces
to one equality: the tenant that `did:{did}` resolves for the dialed number must equal the
authenticated account's tenant, else `403`. A leaked key reaches one tenant's own agents —
the same reach as dialing that tenant's number from a phone — and every failure direction
(stale cache, poisoned cache, Redis down, unprovisioned number) lands inside the
authenticated tenant, never in someone else's. Serving N tenants means N Voice
Applications, one per tenant, each with its own URL and key; that is stated as a product
limitation in `docs/cloudonix.md` rather than engineered around.

Routing context still never travels in the WebSocket URL as a tenant/agent name: the
webhook stores the resolved `CallRoute` behind an opaque single-use token and puts only
that token in the `<Stream url="">` (lesson 31).

## Changes

| File | Change | Why |
|------|--------|-----|
| `libs/media_stream_sdk/__init__.py` (new) | Empty package marker, matching `libs/vad_sdk/`. | Third extraction from `services/vobiz`, same convention. |
| `libs/media_stream_sdk/audio.py` (new) | `services/vobiz/audio.py` moved verbatim; `AudioBridge` methods renamed `vobiz_to_pcm16`→`to_pcm16`, `pcm16_to_vobiz_bytes`→`from_pcm16`; the unused `pcm16_to_vobiz()` wrapper dropped (grep: no caller). | mulaw 8k↔PCM16 16k is protocol-generic; Cloudonix needs the identical codec pair. |
| `libs/media_stream_sdk/serializers.py` (new) | `MediaStreamSerializer` Protocol + `VobizSerializer` + `CloudonixSerializer`. The *only* module that knows event names, `streamId`/`streamSid`, or frame JSON. | One seam, two providers; a third provider is a new class here and nothing else. |
| `libs/media_stream_sdk/bridge.py` (new) | `MediaStreamBridge` — body moved verbatim from `services/vobiz/bridge.py` with exactly four edits: (1) takes `serializer`, (2) the three protocol literals in `_vobiz_to_grpc`/`_run_vad`/`_playback_pacer` go through the serializer, (3) `call_uuid`→`call_id`, (4) logger name from a `log_name` ctor arg. Adds the opt-in `MEDIA_STREAM_DUMP_DIR` WAV dump. | Preserves pacer/delay/barge-in/watchdog semantics byte-for-byte on the Vobiz side while giving Cloudonix all of it. |
| `services/vobiz/bridge.py` | Reduced to `class VobizCallBridge(MediaStreamBridge)` — keeps the `call_uuid=` keyword and the `vobiz.bridge` logger name, passes `VobizSerializer()`. Module docstring retained (it documents *why* the pacer/delay exist). | Existing callers (`app.py`, `services/vobiz/tests/test_bridge_dtmf.py`) keep working unchanged. |
| `services/vobiz/audio.py` (deleted) | Moved to the lib. | Single source of truth for the codec. |
| `libs/telephony_sdk/providers/cloudonix.py` (new) | `CloudonixProvider(ITelephonyProvider)`, `PROVIDER_NAME = "cloudonix"`, `required_credential_fields() -> ["domain", "api_keys"]` (**no `app_id`** — R2-3; **`api_keys`, not `api_key_refs`** — every entry must be an `enc:` Fernet token, see *Account key material*), `validate_credentials`, `build_answer_response(ws_url)` → CXML, `verify_webhook_signature` → always `False` with a docstring saying why. `initiate_call`/`hangup_call`/`get_call_status` raise `TelephonyProviderError("cloudonix: outbound call control out of scope")`. Registered beside `vobiz`. | Per-account credentials in `telephony_configs` are the existing convention (`services/vobiz/app.py:90-114`) and give the Admin UI its credential form for free. |
| `libs/telephony_sdk/did_route.py` (new) | `services/vobiz/redis_route.py` moved here, plus the bounded timeout and a new `resolve_did_route()` that distinguishes a miss from a hit (see Interfaces). | PRD forbids a new cache key or a second DID reader; Cloudonix must read the same key the Gateway and Vobiz read, but must not confuse "miss" with "routed to the tenant named `default`". |
| `services/vobiz/redis_route.py` (deleted) | — | Replaced by the lib module; `app.py` is the only importer. |
| `services/vobiz/app.py` | `from . import redis_route` → `from libs.telephony_sdk import did_route`; line 151 becomes `did_route.resolve_did(...)`. | Mechanical; no behavior change beyond the now-bounded Redis timeout (see Risks). |
| `libs/ratelimit.py` (new) | `FixedWindowCounter` moved verbatim out of `services/config/app.py:36-155` (class, docstring, `_SWEEP_THRESHOLD`/`_SWEEP_INTERVAL`/`_MAX_BUCKETS` unchanged). | Its bounded-memory behavior is an earned fix (lesson 25); a hand-rolled counter would re-earn it. |
| `services/config/app.py` | Delete the class body, add `from libs.ratelimit import FixedWindowCounter` (re-exported at module scope so `InviteThrottle`/`AcceptThrottle`/`LiveCallsThrottle` and `services/config/tests/test_throttle.py` are untouched). | Zero-behavior-change extraction. |
| `services/config/telephony_configs.py` | (a) Add `list_configs_by_provider(provider)` returning `{id, tenant_id, tenant_slug, credentials}` for non-deleted rows, opened with `platform_conn(reason="cloudonix account preload: cross-tenant by construction")`. (b) In `create_telephony_config`/`update_telephony_config`, normalize a `provider='cloudonix'` row's `credentials.api_keys` on write: a plaintext entry is stored as `encrypt_secret(entry)`, an `enc:` entry is kept verbatim, anything else — including `env:`/`k8s:` — raises `ValueError` → `400`. | The cross-tenant read this feature needs (named, greppable bypass per CURSOR.md), plus the write-side half of the `enc:`-only rule, mirroring `services/config/provider_configs.resolve_api_key_input()`'s "never paste a raw key into a pointer field" handling. |
| `services/config/routers/telephony_configs.py` | Add `GET /telephony-configs?provider=` on the existing flat `router`, `403` unless `is_platform_scoped(current_user)` (`tenant_id is None`, never `role == "superadmin"` — lesson 24). Response carries `credentials.api_keys` (sealed `enc:` Fernet tokens — worthless without `SECRET_ENCRYPTION_KEY`, the same stance as `services/config/provider_configs.py:28-31`'s note) and `credentials.domain`. | The Cloudonix service's cold-path preload, scoped like Conversation's prewarm. **Revision 2's second endpoint (`/{config_id}/phone-numbers`) is not built** — nothing needs a DID inventory now. |
| `services/cloudonix/__init__.py` (new) | Empty. | Package. |
| `services/cloudonix/__main__.py` (new) | Copy of `services/vobiz/__main__.py`: generated-stubs `sys.path` shim, `uvicorn.run("services.cloudonix.app:app", port=PORT default 8700)`. | 8000/8100/8300/8400/8500/8600 are taken. |
| `services/cloudonix/accounts.py` (new) | `AccountStore` — cold-path preload + periodic refresh of `{config_id → Account}` from Config Service, with last-known-good retention. Decrypts key material with `libs.config_sdk.secrets.decrypt_secret` only; **does not import `CompositeSecretResolver`**. (Revision 2's `inventory.py`, minus the DID half.) | The server-side half of the tenant boundary, and the read-side half of the `enc:`-only rule. |
| `services/cloudonix/app.py` (new) | FastAPI app: `GET /health`, `GET|POST /cloudonix/voice/{config_id}`, `WS /cloudonix/stream/{token}`, lifespan owning the refresh task. | The service; mirrors `services/vobiz/app.py`'s webhook+WS-in-one-process shape. |
| `services/cloudonix/handoff.py` (new) | `CallRoute` dataclass + `HandoffStore` (issue/claim, TTL, cap). | The server-resolved-twin channel from webhook to bridge. |
| `libs/media_stream_sdk/tests/test_serializers.py` (new) | Golden frames for both serializers. | Freezes today's exact Vobiz wire bytes across the extraction. |
| `services/cloudonix/tests/test_admission.py` (new) | The tenant boundary, the ordering, and the rate-limit scoping. | See Test plan. |
| `services/cloudonix/tests/test_bridge_session.py` (new) | AC6–AC10 against a fake WS + fake gRPC call. | See Test plan. |
| `services/config/tests/test_telephony_config_listing.py` (new) | The new platform-scoped route: service account 200, tenant admin 403, no resolved secret in the body. | New cross-tenant read surface needs its own test, scoped to the route (lesson 9). |
| `libs/telephony_sdk/tests/test_did_route.py` (new) | Miss / malformed / `RedisError` / timeout / post-timeout recovery / miss-vs-`default`-tenant distinction. | AC3–AC5 and the admission rule below. |
| `deployment/docker/docker-compose.yml` | Add a `cloudonix` service published as `"${CLOUDONIX_BIND_ADDR:-127.0.0.1}:${CLOUDONIX_PORT:-8700}:8700"` — its **own** bind variable, never the shared `BIND_ADDR` — with an inline comment pointing at `docs/cloudonix.md`. | R1-3: the shared `BIND_ADDR` also publishes Postgres, Redis and the unauthenticated conversation gRPC port (`docs/docker-startup.md:423`). |
| `docs/cloudonix.md` (new) | Setup, the one-tenant-per-Voice-Application rule, the exposure model, the ingress requirements (CIDR + connection rate limit), key rotation ownership, env vars, and the AC12/13/14 verification writeup answering OQ1–4. Secrets by ref name only. | AC12/13/14 require a checked-in, reviewable artifact. |
| `CURSOR.md` | One row in the area table: `Cloudonix webhook + media bridge | services/cloudonix/`; `libs/` line gains `media_stream_sdk`. | The file enumerates every service and lib. |

**Not in this design, deliberately:** any read or write of `phone_numbers.telephony_config_id`
(R2-1), any composite-FK migration for it, any `app_id` credential field (R2-3), and any
app-level pre-auth rate-limit gate (R2-2).

## Data
None. No schema change, no new table, no new column, no new Redis key, no new constraint.

The boundary uses only columns that already exist **and are already written by existing
code**: `telephony_configs.id` (PK), `telephony_configs.tenant_id`
(`NOT NULL REFERENCES tenants(id)`, written by
`services/config/telephony_configs.create_telephony_config`), and the `did:{did}` Redis
value written by `services/config/phone_numbers.py`.

`phone_numbers.telephony_config_id` (`database/telephony_schema.sql:39`) is **not used**.
It has no writer anywhere in the tree and a non-composite FK, so binding authorization to
it would mean shipping the control and its enforcement in the same unreviewed breath
(R2-1; same shape as lesson 36's `agents.call_flow_id`). If a future PRD needs one
Cloudonix account to carry several tenants' DIDs, the prerequisite is that column's
composite FK onto `(id, tenant_id)`, a `UNIQUE (id, tenant_id)` on `telephony_configs`, a
tenant-matching write path on `PATCH /phone-numbers/{id}`, and a tenant-joined read — as a
change with its own review, not as an assumption inside this one.

Operator setup: create a `provider='cloudonix'` `telephony_configs` row on the tenant
(existing Admin UI surface), paste `{CLOUDONIX_PUBLIC_BASE_URL}/cloudonix/voice/{that row's id}`
into the Cloudonix Voice Application, and point that application's DIDs at it. The DIDs
themselves are provisioned through today's `phone_numbers` flow, unchanged.

## Interfaces

### Tenant boundary (R2-1, R2-3)
Two facts decide the tenant, both server-side, neither caller-supplied:

| Fact | Source | Enforced by | Caller-controllable? |
|------|--------|-------------|----------------------|
| Which account is calling | `{config_id}` path segment + `X-CX-APIKey` matching one of that row's decrypted `api_keys` | `telephony_configs` PK — uniqueness and ownership are the primary key's, not a JSONB field's (R2-3: a tenant cannot mint, collide with, or shadow another tenant's config id) | No |
| Which tenant that account is | `telephony_configs.tenant_id` | `NOT NULL REFERENCES tenants(id)`, written only through the tenant-scoped create route | No |

`To` never selects a tenant. It is checked *against* the account's tenant
(`resolve_did_route(To).tenant_slug == account.tenant_slug`) and otherwise only picks the
agent. Every non-match and every unknown is resolved inside the authenticated tenant:

| `did:{did}` outcome | Result |
|---|---|
| hit, tenant == account tenant | route to that tenant/agent |
| hit, tenant != account tenant | `403`, log `cloudonix.reject.foreign_did` — no token, no session |
| miss / malformed | route to `(account.tenant_slug, "default")`, log `cloudonix.route.fallback reason=miss` |
| timeout / unreachable | route to `(account.tenant_slug, "default")`, log `cloudonix.route.fallback reason=timeout|unreachable` |

A poisoned or stale `did:{did}` can therefore cause a denial (403) or a wrong *agent*
within the right tenant — never a cross-tenant session. A leaked key reaches one tenant's
own agents. `docs/cloudonix.md` names the rotation owner and procedure: add the new ref to
`api_keys` (the list exists so rotation has no 403 gap), rotate in the Cloudonix console,
then remove the superseded entry — every entry sealed with `enc:` on write, never a pointer
into the host's env or filesystem.

**AC3/AC4/AC5 restated, not dropped (lesson 7).** The PRD asks for a fallback to the
*platform* default tenant/agent on an unprovisioned or unreachable DID. This design falls
back to the *authenticated account's* tenant and its `default` agent instead. The call is
still never rejected for a routing miss, which is AC3/AC4/AC5's stated intent, and the
substitution is what makes the miss path non-exploitable: a platform-default fallback on a
per-tenant account is a tenant the caller did not authenticate to. The only newly rejected
requests are ones naming a DID that resolves to a *different* tenant, which no genuine
Cloudonix call for this account can be.

### Account key material — `enc:` only
`telephony_configs.credentials` is tenant-writable JSONB, so anything this feature resolves
out of it is tenant-supplied input. `CompositeSecretResolver` was built for admin-entered
infrastructure config and dispatches to `EnvResolver` (arbitrary `os.environ` read) and
`K8sFileResolver` (`self._mount_root / path_part`, where an absolute `path_part` discards
the root — `Path("/var/run/secrets") / "/etc/passwd"` is `/etc/passwd`, a sync `read_text()`
on the event loop besides). Pointing it at a tenant-editable field turns a customer admin
into a host env-var and filesystem reader. So this feature never uses it. The rule is one
scheme, no exceptions, enforced at both ends:

- **Write side** (`services/config/telephony_configs.py`, before the row is stored): for
  `provider='cloudonix'`, each `credentials.api_keys` entry is either plaintext — stored as
  `encrypt_secret(entry)` (`libs/config_sdk/secrets.py:54`) — or an existing `enc:` token,
  kept verbatim. Any other value, **including `env:` and `k8s:`**, raises `ValueError` →
  `400` with a message saying to send the key itself, mirroring
  `services/config/provider_configs.resolve_api_key_input()` (`provider_configs.py:32-55`).
  Non-list, empty and oversized `api_keys` are rejected here too, via
  `CloudonixProvider.validate_credentials`, which `libs/telephony_sdk/interface.py`
  documents as create-time-only.
- **Read side** (`services/cloudonix/accounts.py`): `AccountStore._decrypt()` accepts an
  entry **only** if `libs.config_sdk.secrets.is_encrypted(entry)`, then calls
  `decrypt_secret(entry)`; anything else is skipped with
  `log.warning("cloudonix: account %s has a non-enc api_keys entry, ignoring", config_id)`,
  and an account left with zero usable keys is dropped from the map (it then 403s, the safe
  direction). `services/cloudonix/` imports `decrypt_secret`/`is_encrypted` and **not**
  `CompositeSecretResolver`, so there is no call site through which an `env:`/`k8s:` string
  could reach a resolver — the dangerous value is not merely rejected, it has no channel
  (lesson 31). A row written before this validation existed, or by raw SQL, or by a future
  route that forgets the check, still cannot read a file or an env var.

`decrypt_secret` is pure CPU (Fernet), so unlike `K8sFileResolver.read_text()` it does no
blocking I/O on the event loop (lesson 18). The Cloudonix service therefore needs
`SECRET_ENCRYPTION_KEY`, exactly as Config and Conversation do; without it the refresh
fails, `loaded` stays `False`, and the webhook 503s rather than falling back to anything.

### Rate limiting (R2-2)
There is **no app-level pre-auth denial bucket**. Revision 2's step 1 evaluated
`over_limit(request.client.host)` before authenticating, and the mandated ingress makes
that host one constant for attacker and carrier alike — so the check itself was the
denial. Removed entirely.

What replaces it, in order:
1. **Authentication is the first gate and cannot be exhausted.** Resolving `{config_id}`
   is a dict lookup and the key check is `hmac.compare_digest` over a ≤3-entry tuple: no
   I/O, no shared mutable budget, nothing an attacker can consume on another account's
   behalf. Every unauthenticated request dies here, before Redis, before any token, before
   any CXML (AC11's "rejected before a Redis lookup or CXML response is generated").
2. **Authenticated buckets are keyed on server-derived values only:**
   `_per_did.over_limit(f"{config_id}:{did}")` (`CLOUDONIX_DID_LIMIT`, default 30/60s) and
   `_per_account.over_limit(config_id)` (`CLOUDONIX_ACCOUNT_LIMIT`, default 300/60s). A
   flood can only spend the budget of the account whose key it holds, and within it only
   the DID it dials — one tenant cannot starve another, and one DID cannot starve its
   sibling.
3. **Failed-auth attempts are counted but never gate a response.** A `FixedWindowCounter`
   keyed on `config_id` (or the literal `"unknown"` when the path names no row) is
   incremented on each failure purely to drive one warn-level log per window instead of
   one per request, and to feed alerting. `over_limit` on it is never consulted to decide
   a status code, so there is no bucket whose exhaustion changes what any caller gets. It
   also keeps the response identical for "no such config id" and "wrong key" (both `403`,
   same body, always) — no existence oracle over config ids (lesson 2).
4. **Unauthenticated flood protection is an ingress requirement, not an app control**,
   stated in `docs/cloudonix.md` alongside the CIDR allowlist: a connection/request rate
   limit at the tunnel or ingress, which is the only layer that can still see distinct
   sources. The app deliberately implements no per-source limit, because behind the
   mandated proxy any such limit is either inert or a self-inflicted global denial —
   exactly R2-2. `X-Forwarded-For` remains untrusted (`services/config/app.py:188-197`).

### `services/cloudonix/accounts.py`
```python
@dataclass(frozen=True)
class Account:
    config_id: str; tenant_slug: str
    domain: str; api_keys: tuple[str, ...]     # decrypted secrets, never logged

class AccountStore:
    async def refresh(self) -> None            # one Config Service pass; never clears on failure
    def get(self, config_id: str) -> Account | None
    @property
    def loaded(self) -> bool
```
`refresh()` calls `GET /telephony-configs?provider=cloudonix` with the same service-account
login/401-retry helper as `services/vobiz/app.py:61-87` and decrypts each `api_keys` entry
through `_decrypt()` (`enc:`-only — see *Account key material*). It runs once in `lifespan`, then every
`CLOUDONIX_REFRESH_S` (default 300) in a task `lifespan` cancels on shutdown (lesson 26).
A failed refresh logs and keeps the last-known-good map — a Config Service outage must not
drop live inbound calls. A cold start that loads nothing leaves `loaded == False`, and the
webhook then returns `503` with no CXML plus a loud log: with no account map there is no
key to check, and admitting anything would reinstate R1-1. `/health` reports `loaded`, so
the compose healthcheck fails visibly rather than the service silently rejecting calls.
Rows whose `credentials` fail `CloudonixProvider.validate_credentials` are skipped with a
warning rather than aborting the refresh, so one malformed tenant row cannot take the
service down for everyone.

### `services/cloudonix/app.py`
```python
@app.api_route("/cloudonix/voice/{config_id}", methods=["GET", "POST"])
async def voice(config_id: str, request: Request) -> Response
```
Strict order; each step returns before the next; each logs its own tag; no reject path
reaches Redis, the handoff store, or CXML.

1. `if not accounts.loaded:` → `503`, log `cloudonix.reject.not_loaded`.
2. `account = accounts.get(config_id)`; reject if `None`, or if no entry of
   `account.api_keys` matches `request.headers.get("x-cx-apikey", "")` under
   `hmac.compare_digest` (all entries compared, no short-circuit on the first mismatch).
   Both → `403`, body `"forbidden"`, identical in either case; log
   `cloudonix.reject.api_key config_id=… known=<bool> present=<bool>`, never the presented
   value; increment the observability counter from *Rate limiting* §3.
3. Parse `To`/`From`/`CallSid`/`Domain` case-tolerantly from form ∪ query. `Domain` must
   case-insensitively equal `account.domain` → else `403` (same body), log
   `cloudonix.reject.domain`, increment the same counter. Defense in depth only: the
   boundary does not depend on it, so if the trial call shows Cloudonix does not send this
   field under this name, the check is dropped with a note in `docs/cloudonix.md` rather
   than the feature blocked.
4. `_per_did` then `_per_account` (see *Rate limiting* §2) → `429`, log
   `cloudonix.reject.rate_limit config_id=…`.
5. `route = await did_route.resolve_did_route(normalize_e164(to))` (bounded; `None` on
   miss/error). Apply the outcome table in *Tenant boundary*: foreign tenant → `403`;
   otherwise `(tenant_slug, agent_slug)` = the hit, or `(account.tenant_slug, "default")`.
6. `token = handoff.issue(CallRoute(tenant_slug=…, agent_slug=…, caller_did=from_, called_did=to, call_sid=call_sid, issued_at=time.monotonic()))`;
   return `Response(CloudonixProvider(...).build_answer_response(f"{ws_base}/cloudonix/stream/{token}"), media_type="application/xml")`.

```python
@app.websocket("/cloudonix/stream/{token}")
async def stream(websocket: WebSocket, token: str) -> None
```
`await websocket.accept()`; `route = handoff.claim(token)`; if `None`, log
`cloudonix.stream.unknown_token` and `close(code=1008)` — no fallback session (a deliberate
divergence from `services/vobiz/app.py:245`: an unknown token means replay, double-connect
or a bridge restart). Otherwise build
`MediaStreamBridge(serializer=CloudonixSerializer(), call_id=route.call_sid,
tenant_slug=route.tenant_slug, agent_slug=route.agent_slug, direction="inbound",
caller_did=route.caller_did, called_did=route.called_did, log_name="cloudonix.bridge")`
and `await bridge.run(websocket)` inside the same `try/except WebSocketDisconnect` as
`services/vobiz/app.py:256-259`. `bridge.run()`'s existing `finally` cancels the five tasks
and calls `call.cancel()` — that is the AC10 teardown, unchanged. (Carried-forward medium:
no concurrent-bridge cap; carried-forward low: the token travels in the URL path.)

### `services/cloudonix/handoff.py`
```python
@dataclass(frozen=True)
class CallRoute:
    tenant_slug: str; agent_slug: str
    caller_did: str; called_did: str; call_sid: str
    issued_at: float                                   # time.monotonic()

class HandoffStore:
    _TTL_S = 60.0
    _MAX_PENDING = 10_000
    def issue(self, route: CallRoute) -> str           # secrets.token_urlsafe(32)
    def claim(self, token: str) -> CallRoute | None    # single-use pop; None if unknown/expired
```
`issue()` evicts expired entries first and raises `HandoffCapacityError` at `_MAX_PENDING`;
the webhook turns that into `503` with no CXML and logs `cloudonix.reject.capacity` —
resource exhaustion is not laundered through the routing fallback.

### `libs/telephony_sdk/did_route.py`
```python
DEFAULT_TENANT = "default"
DEFAULT_AGENT  = "default"

def _timeout_s() -> float:          # DID_REDIS_TIMEOUT_MS, default 250
def _get_client() -> redis.Redis:   # redis.from_url(url, decode_responses=True,
                                    #   socket_timeout=_timeout_s(),
                                    #   socket_connect_timeout=_timeout_s(),
                                    #   retry_on_timeout=False)

async def resolve_did_route(did: str) -> tuple[str, str] | None
async def resolve_did(did: str) -> tuple[str, str]   # back-compat wrapper for services/vobiz
```
`resolve_did_route` wraps the GET in `asyncio.wait_for(client.get(f"did:{did}"), _timeout_s())`
as a hard ceiling independent of redis-py's retry/health-check internals, catches
`(redis.RedisError, asyncio.TimeoutError)` in **one** except branch, and returns `None` for
every non-hit — miss, malformed JSON, timeout, unreachable — logging the reason. Because
`redis.TimeoutError` is a `RedisError` subclass, slow and unreachable collapse into one
branch by construction, so AC5's "must not produce different caller-facing behavior" holds
structurally, with the distinction living only in the log.

`resolve_did` is `resolve_did_route(did) or (DEFAULT_TENANT, DEFAULT_AGENT)` — today's
semantics, kept because `services/vobiz/app.py:151` depends on them. Cloudonix calls
`resolve_did_route` and **must not** call `resolve_did`: the wrapper cannot distinguish a
miss from a genuine route to a tenant whose slug is literally `default`, and on the
Cloudonix path that difference decides whether the equality check in the boundary table is
being applied to real data or to a sentinel.

### `libs/telephony_sdk/providers/cloudonix.py`
```python
class CloudonixProvider(ITelephonyProvider):
    PROVIDER_NAME = "cloudonix"
    @classmethod
    def required_credential_fields(cls) -> list[str]           # ["domain", "api_keys"]
    @classmethod
    def validate_credentials(cls, credentials: dict) -> None   # api_keys: non-empty list of
                                                               # enc: tokens ONLY — never env:/k8s:,
                                                               # never a raw key; domain non-empty
    def build_answer_response(self, websocket_url: str) -> str
    def verify_webhook_signature(self, url: str, headers: dict[str, str]) -> bool   # always False
```
`build_answer_response` returns
`<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url={quoteattr(url)}/></Connect></Response>`
(`xml.sax.saxutils.quoteattr`; the URL is `CLOUDONIX_PUBLIC_BASE_URL` plus a generated
token, so nothing tenant- or caller-supplied is interpolated).
`verify_webhook_signature` returns `False` unconditionally, with a docstring stating that
Cloudonix sends a static header rather than a signature and that the real check needs the
account's resolved secrets, which this interface's parameters do not carry — a stub
returning `True` would be a silent authentication bypass for any future generic caller.
`validate_credentials` runs at `telephony_configs` creation/update time only, as
`libs/telephony_sdk/interface.py` documents, and rejects any `api_keys` entry that is not an
`enc:` token — `env:` and `k8s:` included (see *Account key material*). There is **no
`app_id` field**: the account's identity is its row id (R2-3).

### `libs/media_stream_sdk/serializers.py`
```python
class MediaStreamSerializer(Protocol):
    def event_kind(self, event: dict) -> str            # "start"|"media"|"dtmf"|"stop"|"other"
    def stream_id(self, event: dict) -> str | None      # from the start event
    def media_payload(self, event: dict) -> str | None  # inbound base64 mulaw
    def dtmf_digit(self, event: dict) -> str | None
    def play_frame(self, stream_id: str | None, ulaw_frame: bytes) -> str   # JSON text frame
    def clear_playback(self, stream_id: str | None) -> str | None           # JSON text, None = unsupported
```
`VobizSerializer` emits exactly today's literals:
`{"event":"playAudio","media":{"contentType":"audio/x-mulaw","sampleRate":8000,"payload":…},"streamId":…}`
and `{"event":"clearAudio","streamId":…}`; reads `start.streamId`, `media.payload`,
`dtmf.digit`.

`CloudonixSerializer` (Twilio Media Streams-compatible): reads `start.streamSid`,
`media.payload`, `dtmf.digit`; maps `connected`/`mark` to `"other"`; emits
`{"event":"media","streamSid":sid,"media":{"payload":b64}}` and
`{"event":"clear","streamSid":sid}`. Field lookups are case-tolerant, mirroring
`services/vobiz/app.py:139`'s `form.get("CallUUID") or form.get("call_uuid")` habit —
exact casing is unverified until the trial call (OQ2/OQ3).

### `libs/media_stream_sdk/bridge.py`
```python
class MediaStreamBridge:
    def __init__(self, *, serializer: MediaStreamSerializer, call_id: str,
                 tenant_slug: str, agent_slug: str, direction: str,
                 caller_did: str = "", called_did: str = "",
                 log_name: str = "media_stream") -> None
    async def run(self, ws) -> None
```
`run()` is unchanged from `VobizCallBridge.run()`: `SessionOpenRequest(protocol_version="1.0",
session_id=uuid4(), tenant_id=<tenant slug>, script_id=<agent slug>, call_id=<provider call
id>, caller_did, called_did, codec=AUDIO_CODEC_PCM_S16LE, sample_rate=16000, channels=1,
direction="inbound")`, then the same five tasks under `asyncio.wait(FIRST_COMPLETED)`. No
proto change (constraint). **OQ3 resolved:** `call_id` is Cloudonix's `CallSid` verbatim —
`session_id` is our own uuid4 and is what everything downstream keys on, so no namespacing
is needed; if the trial call shows `CallSid` absent, the fallback is the handoff token,
recorded in `docs/cloudonix.md`. `MEDIA_STREAM_DUMP_DIR` (opt-in, unset by default) writes
each inbound utterance and outbound TTS turn to WAV following
`services/webcall/__main__.py:_write_wav_dump` — how AC7/AC8 get something to listen to.
(Carried-forward medium: the dump's path construction and retention.)

### Deployment and exposure
The compose entry publishes **only**
`${CLOUDONIX_BIND_ADDR:-127.0.0.1}:${CLOUDONIX_PORT:-8700}:8700` and never reads the shared
`BIND_ADDR`, so making the webhook public cannot drag Postgres, Redis, Ollama and the
unauthenticated conversation gRPC port out with it (`docs/docker-startup.md:423`).
`docs/cloudonix.md` states, in terms:
- Do **not** set `BIND_ADDR=0.0.0.0` — it is the exposure control for services with no
  authentication to configure.
- Public reachability is a dedicated TLS-terminating tunnel or ingress pointed at `:8700`
  only, never a host-level port publish.
- That ingress restricts source addresses to Cloudonix's published egress ranges **and**
  applies a connection/request rate limit — both connection-level, because the app cannot
  trust `X-Forwarded-For` and deliberately implements no per-source limit of its own.
- `CLOUDONIX_BIND_ADDR` is raised off loopback only when a firewall or that ingress already
  fronts the port.

### Environment
| Var | Default | Purpose |
|-----|---------|---------|
| `PORT` | `8700` | uvicorn bind |
| `CLOUDONIX_BIND_ADDR` | `127.0.0.1` | compose publish address for this service only |
| `CLOUDONIX_PUBLIC_BASE_URL` | *(required)* | `https://…`; `https→wss` rewrite as `services/vobiz/app.py:168` |
| `CONFIG_SERVICE_URL` / `CONFIG_SERVICE_EMAIL` / `CONFIG_SERVICE_PASSWORD` | as `services/vobiz/app.py:53-55` | account preload service account |
| `SECRET_ENCRYPTION_KEY` | *(required)* | Fernet key for decrypting `enc:` account keys (`libs/config_sdk/secrets.py`) |
| `CLOUDONIX_REFRESH_S` | `300` | account refresh interval |
| `CLOUDONIX_ACCOUNT_LIMIT` / `CLOUDONIX_DID_LIMIT` | `300` / `30` per 60s | the two authenticated buckets (OQ4) |
| `DID_REDIS_TIMEOUT_MS` | `250` | bounded DID lookup, both services (OQ1) |
| `CONVERSATION_SVC_TARGET` | `localhost:10000` | inherited from the shared bridge |
| `MEDIA_STREAM_DUMP_DIR` | unset | opt-in WAV capture for AC7/AC8 |

No `CLOUDONIX_API_KEY_REF` and no unauth-limit var: the keys live in the account's
`telephony_configs.credentials.api_keys` as `enc:` tokens, decrypted with
`SECRET_ENCRYPTION_KEY`, and there is no pre-auth bucket to configure.

## Risks
- **One Cloudonix Voice Application can serve only one tenant, so a shared platform account is no longer expressible.** Mitigation: `docs/cloudonix.md` states it as the supported model (N tenants = N Voice Applications, each its own URL + key), which the trial account satisfies as-is; the multi-tenant account is a follow-up PRD whose named prerequisite is the composite-FK work in `## Data`. Accepting the limitation is what let R2-1 be closed by deletion rather than by a new, unreviewed write path.
- **Revoking a tenant's Cloudonix access is not immediate.** Mitigation: soft-deleting the `telephony_configs` row takes effect at the next refresh (≤`CLOUDONIX_REFRESH_S`, 300s), which `docs/cloudonix.md` states explicitly alongside "rotate the key in the Cloudonix console to revoke now" — the same "identity is cached, not re-read per request" property lesson 27 records for JWTs, surfaced rather than assumed.
- **A Config Service outage during a cold start leaves the webhook returning 503 for every call.** Mitigation: last-known-good is never cleared by a failed refresh, so this only bites on a simultaneous restart of both; `/health` surfaces `loaded` so the condition is visible rather than silent. Failing open was rejected: with no account map there is no key to check at all.
- **The extraction touches live-validated barge-in/pacing code (project memory: "playback pacing — the big one").** Mitigation: the bridge body moves verbatim with only the four listed edits; `test_serializers.py` asserts `VobizSerializer`'s output equals the exact JSON dicts in today's `bridge.py:377` and `:463-471`, so drift fails a test rather than a call; and one live Vobiz call is re-run before merge (lesson 23).
- **A new cross-tenant Config endpoint is new attack surface.** Mitigation: gated on `is_platform_scoped` (`tenant_id is None`, not role — lesson 24), reads through `platform_conn(reason=…)` so the bypass is greppable, returns sealed `enc:` tokens rather than plaintext or resolvable pointers, and has a tenant-admin-403 test scoped to the route (lesson 9).
- **`DID_REDIS_TIMEOUT_MS=250` now applies to `services/vobiz` too, which today has no bound.** Mitigation: strictly safer on a call path (a hung Redis currently hangs the Vobiz answer webhook indefinitely) and the failure mode is the already-live default-tenant fallback; env-overridable.
- **250 ms is a guess until OQ1 is measured.** Mitigation: env var, and `docs/cloudonix.md` must record the observed Cloudonix answer timeout and the chosen value before AC1/AC5 are marked verified.
- **`asyncio.wait_for` cancelling an in-flight redis-py command could poison the pooled connection.** Mitigation: `socket_timeout`/`socket_connect_timeout` are set so the library usually fires first; redis 8.0.1's asyncio connection disconnects on `CancelledError`; `test_did_route.py` asserts a *successful* lookup immediately after a timed-out one.
- **With no app-level pre-auth limit, an unauthenticated flood is bounded only by uvicorn's concurrency and the ingress.** Mitigation: each such request costs a dict lookup and ≤3 `compare_digest` calls with no I/O and no shared state, so it cannot deny any authenticated account; the ingress connection rate limit is a stated deployment requirement. The alternative — an app-level per-source bucket behind a proxy that collapses every source to one address — is R2-2 and is not reintroduced.
- **The two authenticated limits are guesses until OQ4 is answered.** Mitigation: env-tunable; the writeup records observed inbound concurrency from the trial account and the revised numbers.
- **`HandoffStore` and `AccountStore` are per-process, so the service cannot be horizontally scaled as designed.** Mitigation: identical, deliberate precedent in `services/vobiz/app.py`'s `_calls`/`_provider_cache` dicts, with the same documented upgrade path (a Redis hash if and when it scales); stated in `docs/cloudonix.md`.
- **A bridge restart mid-call invalidates in-flight tokens, so a Cloudonix reconnect gets 1008 rather than a re-established session.** Mitigation: the alternative (a fallback session on an unknown token) routes a live caller to the wrong agent. AC14 requires the observed Cloudonix-side behavior to be documented from the trial account.
- **`CloudonixSerializer` and the `Domain`/`To`/`CallSid` field names are written against unverified casing (OQ2/OQ3).** Mitigation: case-tolerant lookups; the `Domain` check is defense-in-depth and droppable; the trial call (AC12) is the merge gate, and a casing miss surfaces as a logged 403 or a silent no-audio call that the WAV dump diagnoses.
- **Key rotation has an owner and a zero-gap procedure, but a forgotten superseded entry in `api_keys` stays valid.** Mitigation: `docs/cloudonix.md` makes removing it the second half of the rotation step, and the reject/accept log records which index matched, so a still-live old key is observable.
- **DTMF digits must not reach any log or variable sink (lesson 33).** Mitigation: the shared bridge's `log.info("…dtmf received call=%s")` carries no digit, and `services/vobiz/tests/test_bridge_dtmf.py`'s regex assertion now covers the Cloudonix path because the code is shared.
- **`enc:`-only key material means an operator cannot point a Cloudonix account at a Kubernetes-mounted secret or an env var, unlike `provider_configs.api_key_ref`.** Mitigation: deliberate — `telephony_configs.credentials` is tenant-writable, and `provider_configs`' looser rule is safe only because the schemes there are meant for admin-entered infra config; the Fernet-sealed value is stored at rest and never leaves Config Service in plaintext, so the operational loss is one indirection, not a weaker secret. `docs/cloudonix.md` states that rotating `SECRET_ENCRYPTION_KEY` requires re-entering each account's key, the same as every other `enc:` field.
- **Carried-forward, knowingly open (security findings 4, 5, 6):** no concurrent-bridge cap or max-call-duration ceiling; `MEDIA_STREAM_DUMP_DIR`'s filename/retention; the handoff token in the WS URL path with a 60 s single-use window.

## Test plan
**Unit — `services/cloudonix/tests/test_admission.py`** (FastAPI `TestClient`, a seeded fake
`AccountStore`, a Redis double that records every call). Two accounts are seeded:
`cfg-a` (tenant A, key `ka`, domain `a.cloudonix.io`) and `cfg-b` (tenant B, key `kb`).
- **R2-1 regression, the load-bearing case:** `POST /cloudonix/voice/cfg-b` with key `kb`
  and `To=<a DID the Redis double resolves to tenant A>` → `403`
  (`cloudonix.reject.foreign_did`), no token issued, no `CallRoute` created. The same
  request against `cfg-a` with key `ka` → `200` routed to tenant A. Both halves are
  required: the first fails if the equality check is missing, the second fails if
  admission degenerated into rejecting everything.
- **R2-3 regression:** two accounts whose `credentials` carry the *same* `domain` (the only
  remaining tenant-writable field) and whose `config_id`s differ → each still authenticates
  only with its own key and routes only to its own tenant; the account map's size equals
  the number of configs seeded (asserting the map's own size, not just a member, so a
  collision that silently dropped a row would fail — lesson 12).
- **R2-2 regression:** send `CLOUDONIX_ACCOUNT_LIMIT + 50` junk-key requests against
  `cfg-a`'s URL and another 50 against `/cloudonix/voice/unknown`, then a *valid* request
  for `cfg-a` and a valid request for `cfg-b` → both `200`. Run against the deployed
  default limits, not lowered ones (lesson 25). This fails on revision 2's ordering and on
  any reintroduction of a pre-auth denial bucket.
- Cache-poisoning direction: the Redis double returns tenant B for a DID dialed on `cfg-a`
  → `403`, not a tenant-B session (the failure direction is denial, by design).
- AC1: valid account, DID resolving to its own tenant → `200`, `application/xml`,
  containing `<Connect><Stream url="wss://…/cloudonix/stream/`; the URL contains neither
  the tenant slug nor the agent slug (lesson 31).
- AC2/lesson 2: unknown `config_id`, wrong key, and missing key → all `403` with
  byte-identical bodies and no existence signal; zero Redis calls in all three; the caplog
  tags distinguish them from the routing-fallback tag.
- AC3/AC4/AC5: Redis miss, `RedisError`, and a `get` that sleeps past the timeout → all
  `200` with valid CXML routed to `(account.tenant_slug, "default")` — never `default`
  tenant, never an HTTP error — and all three within AC1's latency budget.
- A DID whose Redis value names the tenant slug `default` while the account's tenant is
  `default` → routed as a hit with its real agent, proving `resolve_did_route`'s miss/hit
  distinction is actually used (this case is indistinguishable if the code calls the
  `resolve_did` wrapper).
- AC11: `CLOUDONIX_DID_LIMIT + 1` requests for one DID → last is `429`, the Redis double's
  call count equals the admitted count (red if the limiter runs after the lookup), a `429`
  issues no token, and a request for a *different* DID on the same account in the same
  window still succeeds.
- `accounts.loaded == False` → `503` with no CXML and `/health` unhealthy.
- Handoff: `claim` twice → second `None`; past TTL → `None`; `_MAX_PENDING`+1 issues →
  `HandoffCapacityError` → `503`, not a routed session.

**Unit — `libs/telephony_sdk/tests/test_did_route.py`** (fake client, no live Redis)
- `resolve_did_route`: hit → the route; miss → `None`; malformed JSON → `None`;
  `redis.ConnectionError` → `None`; a `get` that sleeps past the timeout → `None` **and**
  returned within `2 × DID_REDIS_TIMEOUT_MS` (fails if the bound is absent, not merely if
  the value is off).
- Post-timeout recovery: a second call against a now-fast client returns the real route.
- `resolve_did` still returns `("default","default")` for every `None` case — the
  back-compat contract `services/vobiz/app.py:151` depends on.
- `CloudonixProvider.verify_webhook_signature` returns `False` for a request carrying a
  *valid* key — the assertion that the generic interface is not a bypass.
- `validate_credentials` rejects an empty `api_keys`, a missing `domain`, and — the
  load-bearing case — `"env:PATH"`, `"k8s:/etc/passwd"` and a raw key, while accepting an
  `enc:` token produced by `encrypt_secret()`.

**Unit — `libs/media_stream_sdk/tests/test_serializers.py`**
- `VobizSerializer.play_frame`/`clear_playback` parse back to the exact dicts hardcoded in
  today's `bridge.py` (red if the extraction changes one key or drops
  `contentType`/`sampleRate`).
- `CloudonixSerializer`: a Twilio-shaped `start`/`media`/`dtmf`/`stop`/`connected` sample
  maps to the right `event_kind`; `play_frame` emits `streamSid` + base64 payload; a
  `connected` frame classifies as `"other"`, not `"start"`.

**Unit — `services/config/tests/test_telephony_config_listing.py`**
- Platform service account (`tenant_id is None`, `role="viewer"` — the real shape, lesson
  24) → `200`; a tenant admin of the owning tenant → `403`; the body carries
  `api_keys` as `enc:` tokens and no plaintext or `env:`/`k8s:` value.
- Write path: creating a `provider='cloudonix'` config with `api_keys=["env:POSTGRES_DSN"]`
  or `["k8s:/etc/passwd"]` → `400` and no row written; with a plaintext key → stored
  `enc:`-encrypted, and the stored value is not the plaintext.

**Integration — `services/cloudonix/tests/test_bridge_session.py`** (fake WS feeding
recorded Cloudonix frames, fake gRPC `Converse` capturing `GatewayMessage`s)
- AC6: `start` → one `SessionOpenRequest` whose `tenant_id`/`script_id` come from the
  claimed `CallRoute` (not from the URL), with `caller_did`/`called_did` from the webhook.
- AC7: a known mulaw sine `media` frame → an `AudioChunk` whose decoded PCM matches the
  expected 16 kHz waveform within tolerance (not a length-only check).
- AC9: `dtmf` → a `DtmfDigit` on the stream, after any held audio, with the digit absent
  from caplog.
- AC10: `stop` and abrupt WS close → the fake call records `.cancel()` and all five tasks
  are done.

**Live verification (AC12/13/14), written into `docs/cloudonix.md`:** one real inbound call
to the trial DID with `MEDIA_STREAM_DUMP_DIR` set — pass/fail verdict, transcript, and the
WAV confirming intelligible audio both ways; the measured webhook answer budget (OQ1) and
the final `DID_REDIS_TIMEOUT_MS`; whether the webhook binds per Voice Application or per
DID (OQ2) — either answer works here, since the URL is keyed on the account row and a
per-DID registration simply means several URLs for the same row; the observed `CallSid`
shape and whether `Domain` arrives under that name (OQ3); observed inbound concurrency and
the final two limits (OQ4); an explicit warm-vs-cold transfer finding with call logs
(AC13 — "not confirmed" is not an acceptable final answer); and the observed Cloudonix
behavior when the bridge is killed mid-call (AC14). Plus one live Vobiz call re-run to
prove the extraction did not regress barge-in. Secrets appear by ref name only.
