# Design: Unified Telephony Provider Service (`services/telephony/`)

## Approach

One FastAPI process, `services/telephony/`, that owns the *orchestration* of every REST-capable
vendor and delegates every vendor-specific byte to a provider adapter in the existing
`libs/telephony_sdk/`. The obvious alternative — a shared base class that each of
`services/vobiz/` and `services/cloudonix/` subclasses — was rejected because the duplication that
caused the two real gaps (plaintext `auth_token`, unprotected Vobiz webhook) is in the *process
wiring* (account preload, rate limiter, DID resolution, call-context store), not in the vendor
code; only collapsing to one process removes it. The single non-obvious call is that
`ITelephonyProvider` instances in this service are constructed from **decrypted** credentials by
a generalized `AccountStore`, which is what finally lets `CloudonixProvider.verify_webhook_signature()`
be a real check instead of today's hardcoded `return False` with the boundary living in the app.
The native Kamailio→FreeSWITCH→Gateway path is untouched and un-importable from here; the
5000-5009 relabel is bookkeeping in a one-off script so that `telephony_configs.provider` stops
lying about rows the REST plane never serves.

This service has two entirely different trust boundaries and they never share a code path. The
**inbound** routes are vendor-facing: the vendor's signature over that specific account's
credentials is the only authentication, exactly as today. The **outbound** routes
(`/{provider}/call`, `/sms/send`, the idempotency read) are *internal caller*-facing and carry the
same service-account JWT every other internal service already requires — `Authorization: Bearer`
verified through `services/config/deps`, with the tenant taken from the verified token and every
tenant-owned value in the body (agent, caller-id number) re-resolved server-side against it
before a vendor is ever called. No route derives a tenant from an unauthenticated body field.
None of that authentication, ownership or idempotency work sits on any path that carries live
audio or signaling — the WS media route has no dependencies and performs no Redis or Postgres
call at all, and the native path never enters this process. See **Latency** for the per-path
trace and for the two places where this design had to be tightened, not just documented, to keep
that true.

Cutover is three merges against one design: (1) build `services/telephony/` and repoint Vobiz,
(2) repoint Cloudonix and generalize Campaigns, (3) delete the old services and their references.
Rows 24-28 of the Changes table are phase-3-only.

## Changes

| File | Change | Why |
|---|---|---|
| `libs/telephony_sdk/interface.py` | Add `NormalizedInboundCall` and `ReconcileResult` dataclasses; add `normalize_inbound_webhook()` (abstract), `sensitive_credential_fields()` (concrete classmethod, default `[]`), `check_health()` (concrete, default `True`), `transfer_call()` (concrete, raises `TelephonyTransferUnsupported`), `reconcile_call()` (concrete default that delegates to `get_call_status`); add `ISmsProvider` ABC | AC1-3, AC5, AC16. Extended in place per the constraint — the only abstract addition is `normalize_inbound_webhook`, and both concrete providers gain it in this same change |
| `libs/telephony_sdk/registry.py` | `register(name, cls, *, hidden=False)`; add `visible()` returning non-hidden entries; `get()` unchanged so hidden names still resolve. Add `SmsProviderRegistry` with the identical shape | AC4 — `"fake"` registers hidden, resolves by name, never appears in the admin list |
| `libs/telephony_sdk/providers/vobiz.py` | Implement `normalize_inbound_webhook` (form/`CallUUID`/`To`/`From`, `known_tenant_slug=None`), `sensitive_credential_fields() -> ["auth_token"]`, `check_health` (GET the account endpoint), `transfer_call` inherits the raising default. Add `ISmsProvider` to the bases with `send_sms`/`get_message_status`/`sensitive_credential_fields` | AC1, AC2, AC5, AC22. Vobiz's Message API mirrors its Call API on the same auth headers |
| `libs/telephony_sdk/providers/cloudonix.py` | Implement `normalize_inbound_webhook` (query ∪ JSON-or-form body, `CallSid`/`To`/`From`, `known_tenant_slug=account_tenant_slug`, domain mismatch raises `WebhookRejected`); replace `verify_webhook_signature`'s `return False` with an all-entries `hmac.compare_digest` over `self._api_keys` against `x-cx-apikey`, skipping any entry for which `is_encrypted()` is true; `sensitive_credential_fields() -> ["api_keys"]`; `check_health` returns `True` (no probe endpoint) | AC1-3, AC6-8. Moves today's `services/cloudonix/app.py` steps 2-3 into the adapter, where they are the vendor-specific part; the boundary is unchanged, only relocated |
| `libs/telephony_sdk/providers/fake.py` **(new)** | `FakeProvider(ITelephonyProvider, ISmsProvider)` — deterministic ids, scriptable failures/timeouts/`check_health` results, registered `hidden=True` under `"fake"` for both registries | AC4; also the only way to test AC13-17's timeout/ambiguity branches without a vendor |
| `libs/telephony_sdk/providers/__init__.py` | Import `fake` alongside `vobiz`/`cloudonix` | Registration-on-import, matching the existing comment in `registry.py` |
| `services/telephony/__init__.py` **(new)** | Empty | Package marker, matching `services/cloudonix/__init__.py` |
| `services/telephony/__main__.py` **(new)** | Generated-stub `sys.path` shim + `uvicorn.run("services.telephony.app:app", port=PORT default 8750)` | Verbatim shape of `services/cloudonix/__main__.py`; the shim is required before any transitive `conversation_pb2_grpc` import |
| `services/telephony/app.py` **(new)** | FastAPI app + `lifespan` (account preload, refresh task, health task, both cancelled on exit); the canonical routes; `/health`. Inbound + WS routes take **no** `Depends`; every outbound route takes `Depends(require_telephony_caller)` | AC6-20, AC28. Routes are thin per CURSOR.md — each one resolves the provider then calls into `orchestrator`/`outbound`. `/health` stays undepended so the docker-compose healthcheck still passes (lesson 1) |
| `services/telephony/auth.py` **(new)** | `require_telephony_caller` — `Depends(get_authenticated_user)` from `services.config.deps`, admitting `superadmin`/`admin` or a NULL-tenant service account (never a tenant `viewer`); **`async def` `resolve_caller_tenant(user, tenant_slug)`** returning `(tenant_id, tenant_slug)` from the `AccountStore`'s in-memory map, which **`await`s** the `async def deps.assert_tenant_access(uuid_tenant_id, user)` | Security findings #1, R2-1, R2-2. Reuses the identity resolution Knowledge/DID/Campaigns/Toolexec already import (lesson 9) rather than inventing a second one |
| `services/telephony/ownership.py` **(new)** | `resolve_outbound_identity(tenant_slug, agent_slug, from_number)` — the server-resolved twin: validates the caller-id DID and the agent against the authenticated tenant, Redis-first | Security finding #2, lesson 31 |
| `services/telephony/accounts.py` **(new)** | Generalized `AccountStore` moved from `services/cloudonix/accounts.py`: keyed `(provider, account_ref)` and `tenant_slug → default-outbound account`; decrypts every field named by that provider's `sensitive_credential_fields()`; holds a constructed provider instance per account; prewarms the `(tenant_slug, agent_slug)` ownership memo on preload and on each 300s refresh | Kills the duplicated login/401-retry/last-known-good preload; is what makes provider instances hold plaintext secrets so signature checks work |
| `services/telephony/orchestrator.py` **(new)** | Provider-agnostic `handle_inbound_webhook()` implementing the fixed AC6 order, plus `resolve_inbound_route()` and the two `FixedWindowCounter`s | AC6-12. The single provider-agnostic pipeline AC9 asserts |
| `services/telephony/callctx.py` **(new)** | `CallContextStore` — `services/cloudonix/handoff.py` kept as-is, including its server-minted `secrets.token_urlsafe(32)` admission token, claim-once, TTL and capacity bound; the stored `CallRoute` gains `provider`, `account_ref` and `provider_call_id` fields | Security finding #4 — the admission token stays server-minted and is never the vendor's own call id (see Risks for the deliberate deviation from AC6's literal URL wording) |
| `services/telephony/idempotency.py` **(new)** | `claim()` / `finalize()` / `await_outcome()` over `idem:{provider}:{tenant_id}:{key}`, plus `note_reference()`/`observed_call_id()` over `idemref:{provider}:{tenant_id}:{key}`; every function takes `tenant_id` as a required positional and has no overload that omits it | Security finding #3. AC13-17. Redis-only, per the hot/cold constraint |
| `services/telephony/outbound.py` **(new)** | `place_call()` / `send_sms()` — resolve ownership → claim → vendor → finalize, with the timeout-reconciliation branch | AC13-17, AC19, findings #1-3 |
| `services/telephony/health.py` **(new)** | `health_loop()` — every `TELEPHONY_HEALTH_INTERVAL_S` (default 300) probe every loaded account with a 5s per-probe timeout and bounded concurrency, derive status from the in-process previous result, write `telephony:health:{config_id}` with `EX 900`. Runs only in its own `asyncio` task, never inline on a request | AC25-28, and the Latency section's "no vendor I/O on a request path" |
| `services/telephony/tests/` **(new)** | `conftest.py`, `test_inbound_orchestration.py`, `test_ratelimit_order.py`, `test_idempotency.py`, `test_health.py`, `test_accounts.py`, `test_outbound_auth.py` | See Test plan |
| `services/config/telephony_configs.py` | `_normalize_credentials()` becomes provider-agnostic over `sensitive_credential_fields()` (scalar **and** list-valued fields); `_NON_REST_PROVIDERS = {"native"}` skips normalize+validate; `list_supported_providers()` uses `TelephonyProviderRegistry.visible()` and returns `{required, sensitive}` per provider; `list_telephony_configs()` attaches a `health` dict read from Redis | AC21-24, AC28, and keeps the 5000-5009 rows editable after the relabel |
| `services/campaigns/telephony_originate.py` **(new)** | Replaces `vobiz_originate.py`: `originate_call(*, provider, …, idempotency_key)` POSTing `{TELEPHONY_SERVICE_URL}/{provider}/call` with capped retries + backoff on the same key; `poll_idempotency()` for the 202 case. Carries the Config service-account JWT with the same login/401-retry-once helper `services/vobiz/app.py:62-88` already uses | AC20, AC33, finding #1 |
| `services/campaigns/worker.py` | Mint `sha256(campaign_id:contact_id:attempt_count)`; dispatch on `provider in _REST_PROVIDERS` vs the ESL path; track 202s in `_pending_idem` and resolve them on a later tick instead of marking failed | AC18, AC20, AC33 |
| `scripts/migrate_telephony_providers.py` **(new)** | One-off, idempotent, re-runnable: relabel 5000-5009 `'cloudonix'`→`'native'` (no other column touched), seal pre-existing plaintext sensitive credential fields, delete the affected `telephony_config:{id}` Redis keys | AC29-31, and lesson 11 — the sealing lands with the tolerant reader in the same merge |
| `deployment/docker/docker-compose.yml` | Add a `telephony` service (own `TELEPHONY_BIND_ADDR`/`TELEPHONY_PORT`, default `127.0.0.1:8750`), copying the `cloudonix` block's env + healthcheck + `depends_on: conversation` | Same exposure model the existing webhook service documents at lines 199-224 |
| `scripts/start_local.sh` | New block for the telephony service; add 8750 to the port list and the legend | Matches the existing per-service block convention; must `return 0` on hint branches (lesson 15) |
| `admin-ui/lib/api.ts` | `TelephonyConfig.health?: { status: TrunkHealth; checked_at: string \| null }`; retype `listTelephonyProviders` to `Record<string, {required: string[]; sensitive: string[]}>` | AC24, AC28 |
| `admin-ui/app/telephony/page.tsx` | Replace the DID-count heuristic at line 305 with `config.health?.status ?? "standby"` | AC28 — the badge becomes backed |
| `docs/telephony.md` **(new)** | Route table, env vars, cutover runbook, the Cloudonix dashboard URL change | Replaces `docs/cloudonix.md` at phase 3 |
| **Phase 3 deletions:** `services/vobiz/`, `services/cloudonix/`, `services/campaigns/vobiz_originate.py`, `docs/cloudonix.md`, their `docker-compose.yml` services and `start_local.sh` blocks | Delete | AC35 |
| `CURSOR.md` | One row: `Unified telephony (webhooks, outbound, SMS) | services/telephony/`, removing the Cloudonix row at phase 3 | Keeps the service table honest |

## Data

Schema: **None.** No new table, no new column — so no `database/rls.sql` change and
`tests/test_rls_coverage.py` stays green.

Data migration, `scripts/migrate_telephony_providers.py`, statement 1 (re-runnable; the
`provider = 'cloudonix'` predicate is what makes the second run a no-op, AC31):

```sql
UPDATE telephony_configs tc
   SET provider = 'native'
 WHERE tc.provider = 'cloudonix'
   AND EXISTS (SELECT 1 FROM phone_numbers pn
                WHERE pn.telephony_config_id = tc.id
                  AND pn.did ~ '^500[0-9]$')
RETURNING tc.id;
```

`updated_at` is deliberately **not** set — AC29 requires no other field to change. The returned
ids drive `DEL telephony_config:{id}` so the 60s cache-aside copy in `services/config/cache.py`
cannot serve the stale `'cloudonix'` label.

Statement 2 (credential sealing) is Python, not SQL — it reads each non-native row, applies
`telephony_configs._normalize_credentials()`, and writes back only when the result differs.
Idempotent because `is_encrypted()` values are kept verbatim.

Redis keys owned by this service (all new, no collisions with `did:{did}` or `telephony_config:{id}`):

| Key | Value | TTL |
|---|---|---|
| `idem:{provider}:{tenant_id}:{key}` | `{"state": "in_flight" \| "done" \| "failed", "call_uuid"?, "message_id"?, "error"?}` | `EX 120` on both the `NX` claim and the finalize overwrite |
| `idemref:{provider}:{tenant_id}:{key}` | the `provider_call_id` observed on a status callback carrying `?idem={key}`, written only under the tenant of the `account_ref` that signed that callback | `EX 120` |
| `telephony:health:{config_id}` | `{"status": "healthy" \| "degraded" \| "standby", "checked_at": iso8601}` | `EX 900` |

`{tenant_id}` is the UUID from the authenticated caller's token (or, on the status-callback write
path, from the `telephony_configs` row whose credentials verified the signature) — never a body
field. Two tenants that mint the same `{key}` — trivially possible, since AC18's formula is a
sha256 over ids one tenant can hold and another can guess — address different Redis entries, so
neither can read, poison, or pre-claim the other's in-flight outcome (finding #3).

`telephony:health` uses a 3×-interval TTL so a dead health loop decays to Standby rather than
pinning a stale Healthy — absence of the key *is* Standby (AC25), which is also the pre-first-probe
state, so there is exactly one code path for both.

## Interfaces

### `libs/telephony_sdk/interface.py`

```python
@dataclass(frozen=True)
class NormalizedInboundCall:
    provider_call_id: str
    from_number: str
    to_number: str
    known_tenant_slug: str | None   # filled by adapters whose account binds the tenant
    raw: dict[str, Any]

@dataclass(frozen=True)
class ReconcileResult:
    outcome: Literal["placed", "not_placed", "indeterminate"]
    provider_call_id: str | None = None

class ITelephonyProvider(ABC):
    @abstractmethod
    def normalize_inbound_webhook(
        self, *, url: str, headers: dict[str, str], fields: dict[str, Any],
        account_tenant_slug: str,
    ) -> NormalizedInboundCall: ...
        # Raises WebhookRejected for a vendor-specific rejection (Cloudonix's
        # domain mismatch). Contains NO DID lookup and NO call-context work (AC9).

    @classmethod
    def sensitive_credential_fields(cls) -> list[str]:
        return []                                   # usable default (AC1)

    async def check_health(self) -> bool:
        return True                                 # usable default (AC3)

    async def transfer_call(self, *, call_id: str, destination: str) -> None:
        raise TelephonyTransferUnsupported(f"{self.PROVIDER_NAME}: transfer_call not yet supported")

    async def reconcile_call(
        self, *, reference: str, observed_call_id: str | None,
    ) -> ReconcileResult:
        """Default: if a callback already observed a vendor call id for this
        reference, confirm it with get_call_status() and report placed/not_placed;
        otherwise 'indeterminate'. A provider whose API can look up by our own
        reference overrides this. Never guesses 'not_placed' (AC16/17)."""

class ISmsProvider(ABC):
    PROVIDER_NAME: str
    @classmethod
    def sensitive_credential_fields(cls) -> list[str]: ...        # same shape (AC2)
    @abstractmethod
    async def send_sms(self, *, from_number: str, to_number: str, text: str) -> str: ...
    @abstractmethod
    async def get_message_status(self, message_id: str) -> dict[str, Any]: ...
    async def reconcile_message(
        self, *, reference: str, observed_message_id: str | None,
    ) -> ReconcileResult: ...
```

### `services/telephony/orchestrator.py`

```python
async def handle_inbound_webhook(
    *, provider_name: str, account_ref: str, request: Request,
) -> Response
```

Exact order, which is the AC6/AC7/AC11/AC12 contract:

1. `TelephonyProviderRegistry.get(provider_name)` → `ValueError` ⇒ **404**, before anything else
   (AC10). Provider names are public constants, not tenant-owned, so a distinguishable status here
   is not a tenant-boundary leak (lesson 2).
2. `accounts.loaded` false ⇒ **503** (keeps today's "no key map means admit nothing" posture).
3. **Rate limit**, on server-derived values only: `_per_account.over_limit(f"{provider}:{ref}")`
   where `ref` is the path segment if it names a loaded account and the literal `"unknown"`
   otherwise. `increment()` is called unconditionally, before step 4, so a signature failure
   consumes quota identically to a valid request (AC12). Over limit ⇒ **429**.
4. `provider.verify_webhook_signature(url, headers)` on the instance built from this account's
   decrypted credentials; unknown account **or** false ⇒ **403**, identical body and status for
   both (AC7).
5. `provider.normalize_inbound_webhook(...)`; `WebhookRejected` ⇒ **403**.
6. `_per_did.over_limit(f"{provider}:{ref}:{call.to_number}")` — this bucket needs the parsed body
   and therefore cannot run at step 3; it is a second limiter, not a replacement.
7. `route = await resolve_inbound_route(call, account)`; `None` ⇒ **403**.
8. `token = callctx.issue(route)` — a server-minted `secrets.token_urlsafe(32)`, exactly as
   `services/cloudonix/handoff.py` does today; capacity exhausted ⇒ **503**.
9. `return Response(provider.build_answer_response(f"{WS_BASE}/{provider}/stream/{token}"))`.

```python
@dataclass(frozen=True)
class InboundRoute:
    tenant_slug: str; agent_slug: str; caller_did: str; called_did: str
    provider: str; provider_call_id: str

async def resolve_inbound_route(
    call: NormalizedInboundCall, account: Account,
) -> InboundRoute | None
```

The tenant is **never** derived from the DID. It is `call.known_tenant_slug` when the adapter
filled it and `account.tenant_slug` otherwise — both are server-side facts about the
authenticated account, so AC8's "uses that value directly without a DID lookup" holds for the
tenant determination in every case. `did_route.resolve_did_route(call.to_number)` then runs only
to select the *agent*: a miss yields `"default"`; a hit whose tenant differs from the account's
tenant returns `None` (⇒ 403, today's `cloudonix.reject.foreign_did`). This applies Cloudonix's
outcome table to Vobiz as well — a deliberate tightening, since `resolve_did()`'s
"always fall back to the default tenant" wrapper currently lets a Vobiz account land a call in
another tenant's session. The Yuviz regression call (AC32) is unaffected: `+14165550177` resolves
to the same tenant its `telephony_config` belongs to.

### `services/telephony/auth.py`

```python
TELEPHONY_CALLER_ROLES = frozenset({"superadmin", "admin"})

async def require_telephony_caller(
    user: CurrentUser = Depends(get_authenticated_user),   # services.config.deps
) -> CurrentUser:
    """401 on a missing/invalid/expired Bearer token (the dependency raises it
    itself, so there is no 'forgot to check' path). 403 unless the caller is
    either a TELEPHONY_CALLER_ROLES console user, or a platform service
    account."""
    if user.role in TELEPHONY_CALLER_ROLES:
        return user
    if user.is_service_account and user.tenant_id is None:
        return user
    raise HTTPException(status_code=403, detail=f"role {user.role!r} cannot access this service")

async def resolve_caller_tenant(user: CurrentUser, tenant_slug: str) -> tuple[uuid.UUID, str]:
    ...
    await deps.assert_tenant_access(tenant_uuid, user)      # async def — deps.py:156
    ...
```

**`deps.assert_tenant_access` is `async def` (`services/config/deps.py:156`), so it MUST be
awaited.** Calling it without `await` constructs a coroutine, raises nothing, and silently
deletes the entire tenant gate — a tenant-B token naming `tenant_slug: "tenant-a"` would then
sail through and the DID/agent ownership checks below would validate against tenant A and pass
too. That is why `resolve_caller_tenant` is itself `async def` and why the literal awaited
expression is written above rather than described.

`viewer` is deliberately **not** in `TELEPHONY_CALLER_ROLES`: placing a real PSTN call and sending
a real SMS are writes, and a tenant's read-only console user must not reach them — the same
exclusion `require_live_calls_operator` (`deps.py:204-216`) already makes for the same shape of
grant. The platform service accounts that Campaigns and Conversation authenticate with *do* carry
`role="viewer"`, which is exactly why the second clause tests `is_service_account and
tenant_id is None` (the token carries both — `auth.py:32,50,71`) rather than widening the role
set: "is this actor privileged?" and "which tenant is this actor scoped to?" are different
questions (lesson 24), and a tenant-scoped viewer fails both clauses while the NULL-tenant
service account passes the second.

`get_authenticated_user` is the deliberate choice over `get_current_user`: the latter calls
`fresh_console_authority()`, which reads `users` from Postgres, and the outbound path is
constrained to Redis-only. The cost is lesson 27's revocation lag (≈12h until token expiry),
which the security review classified as an accepted medium and which this design does not close.

`resolve_caller_tenant` maps `tenant_slug` to its UUID through the `AccountStore`'s already-loaded
`tenant_slug → account` map (no Postgres, no `tenants.get_tenant` slug lookup) and then awaits
`deps.assert_tenant_access(tenant_uuid, user)` — the UUID branch, a pure string comparison. A
platform-scoped caller (`tenant_id IS NULL`) passes for any slug; a tenant-scoped caller passes
only for its own, and an unknown slug raises the same 404 `assert_tenant_access` already raises
for a slug outside the caller's tenant, so the endpoint is not a tenant-existence oracle
(lesson 2).

All three outbound handlers begin with the same awaited call, and there is no other route into
`outbound.*` or `idempotency.*`:

```python
# POST /{provider}/call
tenant_id, tenant_slug = await auth.resolve_caller_tenant(user, body.tenant_slug)
identity = await ownership.resolve_outbound_identity(
    tenant_id=tenant_id, tenant_slug=tenant_slug,
    requested_agent_slug=body.agent_slug, from_number=body.from_,
)
return await outbound.place_call(provider=provider, identity=identity, ...)

# POST /sms/send  — no agent is involved in an SMS
tenant_id, tenant_slug = await auth.resolve_caller_tenant(user, body.tenant_slug)
identity = await ownership.resolve_outbound_identity(
    tenant_id=tenant_id, tenant_slug=tenant_slug,
    requested_agent_slug=None, from_number=body.from_,
)
return await outbound.send_sms(identity=identity, ...)

# GET /{provider}/call/idempotency/{key}?tenant_slug=...
tenant_id, _slug = await auth.resolve_caller_tenant(user, tenant_slug)
return await idempotency.read(provider, tenant_id, key)
```

`requested_agent_slug=None` skips only the agent clause; the caller-id DID check still runs, so an
SMS cannot be sent from a number the authenticated tenant does not own. `OutboundIdentity.agent_slug`
is therefore `str | None`.

`tenant_slug` is a **required** query parameter on the idempotency read — not optional with a
fallback to `user.tenant_id` — so that route takes the identical awaited gate as the other two
rather than a second, differently-shaped one that could drift from it. Campaigns'
`poll_idempotency()` passes the same `tenant_slug` it passed to `originate_call()`; it is
platform-scoped and so would have no tenant namespace of its own to fall back to anyway.

### `services/telephony/ownership.py`

```python
@dataclass(frozen=True)
class OutboundIdentity:
    tenant_id: uuid.UUID
    tenant_slug: str
    agent_slug: str | None   # server-resolved; None for SMS, which has no agent
    from_number: str       # server-validated caller-id DID

async def resolve_outbound_identity(
    *, tenant_id: uuid.UUID, tenant_slug: str, requested_agent_slug: str | None, from_number: str,
) -> OutboundIdentity          # raises OwnershipError -> 403
```

Two checks, both before any vendor call (finding #2):

- **Caller-id number.** `did_route.resolve_did_route(from_number)` must hit and its `tenant_slug`
  must equal the authenticated tenant's. A miss or a foreign tenant is a 403. `did:{did}` is the
  right source because it is written through on every `phone_numbers` create/update, carries no
  TTL at all, and is preloaded at startup (see the no-TTL DID cache design), so a cold cache
  cannot manufacture a false rejection.
- **Agent** (skipped when `requested_agent_slug is None`, i.e. SMS).
  `agent:{tenant_slug}:{requested_agent_slug}` (the key `services/config/agents.py:47`
  already writes) is consulted first; the key is itself tenant-scoped, so a hit *is* the ownership
  proof. Because that key is cache-aside with a 60s TTL, a miss falls back to one **cold-path**
  `GET /tenants/{slug}/agents/{agent_slug}` through the Config service-account client and is
  memoized in the `AccountStore`'s process-local map, refreshed on the same 300s loop — at most
  one such lookup per (tenant, agent) per process, never one per call. A 404 is a 403 to the
  caller.

`OutboundIdentity` is the only value `outbound.place_call`/`send_sms` accept for these three
fields; the request body's own `tenant_slug`/`agent_slug`/`from` are consumed by the route
handler and are not in scope at the call site (lesson 31/32 — a required parameter only forces
the implementer to find *a* value, so the unvalidated one must not be reachable there).

### `services/telephony/idempotency.py`

```python
IDEM_TTL_S = 120
POLL_BUDGET_S = 10.0     # equals the endpoint's own request timeout (AC14)
POLL_INTERVAL_S = 0.2

async def claim(provider: str, tenant_id: uuid.UUID, key: str) -> bool        # SET NX EX 120 "in_flight"
async def finalize(provider: str, tenant_id: uuid.UUID, key: str, outcome: dict) -> None  # SET ... EX 120 (AC15)
async def await_outcome(provider: str, tenant_id: uuid.UUID, key: str) -> dict | None     # bounded poll
async def note_reference(provider: str, tenant_id: uuid.UUID, key: str, call_id: str) -> None
async def observed_call_id(provider: str, tenant_id: uuid.UUID, key: str) -> str | None
async def read(provider: str, tenant_id: uuid.UUID, key: str) -> dict | None   # no poll, no claim
```

`tenant_id` is positional and second on every one of these, so there is no call site that can
omit it and still typecheck; the Redis key is built in exactly one private helper,
`_key(prefix, provider, tenant_id, key)`, which no caller bypasses (finding #3).

`await_outcome` returns `None` only on budget exhaustion, never on error — a Redis failure during
the poll is logged and also returns `None`, which the caller turns into 202. There is no branch in
which a losing claimant reaches the vendor.

### `services/telephony/outbound.py`

```python
async def place_call(*, provider: str, identity: OutboundIdentity,
                     to_number: str, idempotency_key: str) -> OutboundResult
async def send_sms(*, identity: OutboundIdentity, to_number: str,
                   text: str, idempotency_key: str) -> OutboundResult
```

Neither takes a `tenant_slug`, an `agent_slug` or a `from_number` — only the `OutboundIdentity`
that `ownership.resolve_outbound_identity()` returns, and it is the sole source of `tenant_id`
for every `idempotency.*` call inside them. A handler that skipped ownership resolution has no
value of this type to pass.

`OutboundResult` is `(http_status, body)`. The three deterministic outcomes (AC14):

- claim won, vendor answered → `finalize(done|failed)`; **200** `{"ok": true, "call_uuid": …}` or
  `{"ok": false, "error": …}`.
- claim lost, `await_outcome` returns a final entry → **200** with that cached body, no vendor call.
- claim lost and still `in_flight` at 10s, **or** claim won and the vendor timed out with
  `reconcile_call() == "indeterminate"` → **202** `{"status": "pending", "idempotency_key": key}`.
  Never 5xx, never a second dial.
- claim won, vendor timed out, `reconcile_call() == "placed"` → `finalize(done)` + **200** with the
  real vendor id (AC17). `"not_placed"` → `finalize(failed)` + **200** `{"ok": false}`, which is
  the only path that licenses the caller to mint a new key (AC16).

Outbound URLs carry the account's own `account_ref` in the path and `?idem={key}` in the query on
`answer_url`/`hangup_url`/`ring_url`, so `/{provider}/status/{account_ref}` can `note_reference()`
the vendor call id under the tenant of the account whose credentials verified that callback's
signature — never under a tenant the callback names (finding #5); that is what makes `reconcile_call`'s default
non-indeterminate for the common case where the vendor did place the call and merely answered slowly.

`send_sms` resolves the provider server-side from
`accounts.default_outbound_for(identity.tenant_slug)` — the in-memory map refreshed on the 300s
cold-path loop, never a synchronous Config/Postgres call, and keyed off the authenticated
tenant rather than anything in the request body.

### Routes (`services/telephony/app.py`)

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/{provider}/voice/{account_ref}` | GET, POST | vendor signature over that account's credentials | `handle_inbound_webhook` |
| `/{provider}/stream/{call_uuid}` | WS | the server-minted admission token in the path | `callctx.claim(token)`; unknown/already-claimed ⇒ `close(1008)`; else `MediaStreamBridge(...).run(websocket)` **unchanged**, with `on_session_start` retained |
| `/{provider}/status/{account_ref}` | POST | vendor signature over that account's credentials | hangup/ring; `note_reference(provider, account.tenant_id, idem_key, call_id)`, then the existing best-effort `POST {CAMPAIGNS}/internal/vobiz-call-resolved` |
| `/{provider}/call` | POST | `Depends(require_telephony_caller)` + `await resolve_caller_tenant` | `{tenant_slug, agent_slug, to, from, idempotency_key}` → `resolve_caller_tenant` → `resolve_outbound_identity` → `outbound.place_call` |
| `/{provider}/call/idempotency/{key}?tenant_slug=…` | GET | `Depends(require_telephony_caller)` + `await resolve_caller_tenant` | read-only replay of `idem:{provider}:{resolved tenant_id}:{key}`; `{"status": "pending"}` if `in_flight`, 404 if the window expired or the key belongs to another tenant — one indistinguishable 404 for both (lesson 2) |
| `/sms/send` | POST | `Depends(require_telephony_caller)` + `await resolve_caller_tenant` | `{tenant_slug, to, from, text, idempotency_key}` → same two resolution steps → `outbound.send_sms`; the provider is resolved from the authenticated tenant's default-outbound account |
| `/health` | GET | none | `{"loaded": accounts.loaded}` — the docker-compose healthcheck target, deliberately undepended (lesson 1) |

`/{provider}/status/{account_ref}` takes the account segment for the same reason the voice route
does: it is the only way to know whose credentials verify the signature. A callback that verifies
under account A can only ever write `idemref` under account A's tenant, so a tenant cannot reach
another tenant's idempotency state by replaying a callback it can legitimately sign (finding #5).
`{account_ref}` is unknown ⇒ 403, identical in body and status to a bad signature.

The `/{provider}/call` and `/sms/send` bodies keep `tenant_slug` as an explicit, *checked* field
rather than being purely implicit, so a platform-scoped service account can act on behalf of a
named tenant — which is what Campaigns does. It is never trusted: `assert_tenant_access` pins a
tenant-scoped caller to its own tenant regardless of what the body claims (CURSOR.md's caller/
target rule), and the ownership step then re-derives the agent and caller-id from that decision.

### `services/config/telephony_configs.py`

```python
def _normalize_credentials(provider: str, credentials: dict) -> dict
```
For `provider in _NON_REST_PROVIDERS` (`{"native"}`) return verbatim. Otherwise, for each field in
`TelephonyProviderRegistry.get(provider).sensitive_credential_fields()`: seal a `str` value, or
every entry of a `list[str]` value; `is_encrypted()` kept verbatim (AC23); a value starting
`env:`/`k8s:` raises `ValueError` with today's wording (AC23, lesson 37). `validate_credentials()`
gets the same `_NON_REST_PROVIDERS` early return so the relabelled 5000-5009 rows stay editable
from the Telephony page after the migration.

```python
def list_supported_providers() -> dict[str, dict[str, list[str]]]
# {"vobiz": {"required": ["auth_id","auth_token"], "sensitive": ["auth_token"]}, ...}
```
Built from `TelephonyProviderRegistry.visible()` (AC4, AC24). `GET /telephony-providers` in
`routers/telephony_configs.py` needs no code change — only its `admin-ui/lib/api.ts` type.

```python
async def list_telephony_configs(tenant_id) -> list[dict]   # each row gains:
#   "health": {"status": "...", "checked_at": "..."} | {"status": "standby", "checked_at": None}
```
Read with one `cache.get_json` per row after the Postgres fetch, never folded into the
`telephony_config:{id}` cached row (which would freeze a stale status for 60s). A Redis outage
degrades every badge to Standby, matching `cache.py`'s documented never-fail contract.

### `services/campaigns`

```python
# worker.py
idem_key = hashlib.sha256(f"{campaign_id}:{contact['id']}:{contact['attempt_count']}".encode()).hexdigest()
```
Minted from the row `claim_next_pending()` already returned post-increment, so the key is pinned
to the attempt (AC18); `_resolve_with_retry` requeues to `pending`, the next claim bumps
`attempt_count`, and the next key is legitimately different — a *new* attempt, not a retry.

```python
# telephony_originate.py
_jwt_token: str | None = None        # Config service-account JWT, 401-retry-once, as vobiz does
class TelephonyOriginateError(Exception): ...
class TelephonyOriginatePending(Exception):        # carries .idempotency_key
    ...
async def originate_call(*, provider: str, phone_number: str, caller_id: str,
                         tenant_slug: str, agent_slug: str, idempotency_key: str) -> str
async def poll_idempotency(*, provider: str, tenant_slug: str, idempotency_key: str) -> str | None
```
Every request carries `Authorization: Bearer <service-account JWT>`, obtained with the same
`_login()`/401-retry-once helper `services/vobiz/app.py:62-88` and
`services/cloudonix/accounts.py:60-90` already share; a 401 is retried once with a fresh token and
then raises `TelephonyOriginateError` (it is not a transient condition worth the backoff loop).
The Campaigns worker is platform-scoped (`tenant_id IS NULL`), so it passes
`assert_tenant_access` for the `tenant_slug` it names — the same authority it already exercises
through `platform_conn(reason="campaign-by-id")`.

`originate_call` retries the HTTP hop at most 3 times with 0.5/1/2s backoff on
`httpx.HTTPError` or 5xx, reusing the identical key every time (AC20). A 202 raises
`TelephonyOriginatePending`; `worker.py` stores it in `_pending_idem[(campaign_id, contact_id)]`
and calls `poll_idempotency` on a later tick, leaving the contact at `'calling'` — it must not go
down today's "no trackable id ⇒ failed ⇒ requeue" path, which would mint a fresh key for an
attempt the vendor may already have dialled.

Dispatch in `worker.py` becomes `if route["provider"] in TelephonyProviderRegistry.all(): <REST>
else: <ESL originate>` — so `'native'`, `None`, and any unregistered label all take the ESL path,
and a new vendor needs no worker edit (AC33).

## Latency

Traced concretely, not asserted. Four distinct paths exist; the two that carry live audio or
signaling acquire **zero** new work from this design, and that is a structural property of where
the new checks are mounted, not a tuning choice.

| Path | Who serves it | New work this design adds | Evidence |
|---|---|---|---|
| **Native DID call/signaling** (Kamailio → FreeSWITCH → C++ Gateway → gRPC → Conversation) | `gateway/`, C++ | **None.** `services/telephony` is not in this path and cannot be — the Gateway reads `did:{did}` from Redis itself in `PhoneRoute::from_redis` (`gateway/src/config/Config.cpp:186-203`, called at `gateway/src/core/Application.cpp:261`) and never makes an HTTP call to any Python service during call setup | The only artefact this design changes that the native path could see is the 5000-5009 `provider` relabel, and nothing in `gateway/` reads `telephony_configs.provider` at all (AC30) |
| **Vobiz/Cloudonix connected call — WS media** (`/{provider}/stream/{token}`) | `services/telephony/app.py` → `MediaStreamBridge.run()` | **None.** The route has no `Depends`, does not touch `auth.py`, `ownership.py` or `idempotency.py`, performs no Redis call, and no Postgres call. Admission is one in-process dict `pop()` on the server-minted token, then `MediaStreamBridge` is entered unchanged | The PRD constraint keeps `MediaStreamBridge` (`libs/media_stream_sdk/bridge.py`) byte-identical, including `on_session_start`; every per-frame cost — VAD, pacing, barge-in, the gRPC stream — is that file's, untouched |
| **Vobiz/Cloudonix inbound webhook** (`/{provider}/voice/{account_ref}`, once per call, pre-answer) | `orchestrator.handle_inbound_webhook` | **None added over what Cloudonix does live today.** Rate limit = in-process dict; signature = `hmac.compare_digest`, no I/O; normalize = parsing already done today; route resolution = the same single `did:{did}` Redis `GET` behind the existing 250ms `asyncio.wait_for` ceiling (`did_route._timeout_s`); call-context = in-process dict + `secrets.token_urlsafe`. No auth dependency, no ownership resolution, no idempotency key | The new authN/authZ lives only on the outbound routes; the vendor's signature is still the sole inbound credential, as in `services/cloudonix/app.py` today |
| **Outbound trigger** (`/{provider}/call`, `/sms/send`, the idempotency read) | `auth` → `ownership` → `idempotency` → vendor | JWT decode (HS256, in-process, no I/O); `assert_tenant_access` UUID branch (string compare, no I/O); one `did:{did}` `GET`; one `agent:{tenant}:{slug}` `GET`; one `SET NX`. Three local Redis round-trips | Ordered entirely *before* the vendor HTTP request, which is itself 100s of ms (`initiate_call` runs on a 15s client timeout) and is followed by seconds of ringing. No media exists yet on this path |

Two things in the design as written could have reached a live path, and both are tightened here
rather than documented away:

1. **`assert_tenant_access`'s slug branch resolves the slug via `tenants.get_tenant`, which can hit
   Postgres.** That is why `resolve_caller_tenant` maps `tenant_slug → tenant_id` through the
   `AccountStore`'s already-loaded in-memory map first and passes a `uuid.UUID`, taking the
   comparison branch (`deps.py:189-191`). Passing the slug straight through would have put a
   cache-aside DB read on every outbound trigger. The design requires the UUID form; a slug
   argument at that call site is a defect, and `test_no_unawaited_guards.py`'s AST walk also
   asserts the argument at each `assert_tenant_access` call site is the resolved UUID variable,
   never the request's `tenant_slug`.
2. **The agent-ownership check's Config Service fallback.** As written it was a synchronous
   Config call reachable from an outbound trigger — the exact shape the PRD constraint forbids.
   Fixed: the `AccountStore`'s startup preload and its existing 300s refresh loop now also pull
   `GET /tenants/{slug}/agents` per configured tenant and populate the `(tenant_slug, agent_slug)`
   memo, so the steady state is a memo hit with no I/O at all and the Redis `agent:` key is only
   the second-line check. The repair fetch survives solely for an agent created since the last
   refresh, and carries an explicit `timeout=2.0` — an unreachable Config Service costs a bounded
   2s on one outbound trigger and never blocks a connected call, which has no code path here.

The one deliberately slow branch is `await_outcome`'s bounded poll (AC14): a *duplicate* outbound
request can block up to `POLL_BUDGET_S = 10.0` before returning 202. The first request for a key
never enters it (it won the `NX` claim), and no connected call reaches it. Its cost is ≤50 Redis
`GET`s at `POLL_INTERVAL_S = 0.2`.

The health loop (`health.py`) makes real vendor HTTP calls, so it is given a per-probe timeout of
5s and runs probes with bounded concurrency inside its own `asyncio` task — never inline on a
request. A hung vendor delays only that provider's next status write, which decays to Standby via
the `EX 900` TTL.

## Risks

- **Deliberate deviation from AC6's literal wording: the `{call_uuid}` segment of
  `/{provider}/stream/{call_uuid}` is a server-minted `secrets.token_urlsafe(32)`, not
  `call.provider_call_id`.** The PRD fixes that URL shape, but making the vendor's own call id the
  WS admission secret would have (a) stopped it being server-minted — a regression from what
  `HandoffStore.issue()` does live today — and (b) made the admission namespace shared across every
  account of a provider, so one tenant's account could attach to another's session on the same
  provider by presenting an id it had legitimately seen. The route *template* is unchanged; only
  the value in the segment differs, and `provider_call_id` is carried inside the stored
  `CallRoute` where the bridge still gets it. Mitigation is therefore the existing control kept
  intact: server-minted 256-bit token, claim-once, short TTL, capacity bound, and a context that
  exists only after a signature-verified webhook for one specific `account_ref`.
- **Rate limiting moved before authentication means an attacker who learns a `telephony_config` UUID
  can exhaust that account's inbound quota** — the exact pre-auth denial surface the Cloudonix
  design removed, now re-required by AC12. Mitigated by keying only on server-derived values, by
  bucketing every unknown `account_ref` into one shared `{provider}:unknown` key so no real
  account's quota is reachable without its UUID, and by the UUID never appearing in any
  tenant-facing response body.
- **Outbound authentication uses `get_authenticated_user` (pure JWT decode), not
  `get_current_user`**, so a soft-deleted or demoted caller keeps outbound access until its token
  expires (lesson 27). Accepted deliberately, and named rather than left implicit: the PRD
  constrains the outbound path to Redis-only lookups, and `get_current_user`'s
  `fresh_console_authority()` is a Postgres read. The security review classified this window as a
  medium. The blast radius is bounded by the tenant gate — a revoked tenant-scoped token still
  cannot reach another tenant's agents, DIDs or idempotency entries.
- **The agent-ownership check can still reach Config Service for an agent created since the last
  refresh**, which is a synchronous cold-path dependency on an otherwise Redis-only route.
  Mitigated by prewarming the `(tenant_slug, agent_slug)` memo in the `AccountStore`'s startup
  preload and 300s refresh so the steady state is memo-hit-only, by the `agent:{tenant}:{slug}`
  Redis key as the second-line check, and by a `timeout=2.0` on the repair fetch. A Config outage
  degrades to the last-known-good memo; a genuine miss with Config down is a 403, not a silent
  admit. No connected call has a code path to this function (see Latency).
- **`CloudonixProvider.verify_webhook_signature` stops being a hardcoded `False`, so any *other*
  caller constructing it with sealed or empty credentials now runs a real comparison.** Mitigated
  by skipping every `is_encrypted()` entry (a ciphertext is never compared against a presented
  key) and comparing all remaining entries without short-circuit — an instance built from sealed
  or empty `api_keys`, which is how `services/config` constructs it, matches nothing and still
  fails closed.
- **Widening the tenant rule to "tenant comes from the account, never the DID" changes Vobiz's
  live routing**: a Vobiz DID pointing at a tenant other than its `telephony_config`'s tenant now
  403s where it previously connected. Mitigated by verifying the Yuviz row's
  `phone_numbers`/`telephony_configs` tenancy agree before the AC32 regression call, and by a
  `telephony.reject.foreign_did` warn log that names the mismatch for any row that does not.
- **Pre-existing Vobiz rows keep a plaintext `auth_token` until the migration script runs, and the
  reader must tolerate both.** Mitigated by `AccountStore._decrypt_field` passing a non-`enc:`
  value through with a warning rather than dropping it, and by shipping the sealing pass in the
  same merge as that reader (lesson 11). A row sealed while the old `services/vobiz` is still
  running would break it — so the sealing pass runs in phase 1, *after* Vobiz is repointed.
- **`reconcile_call`'s default returns `indeterminate` for Vobiz, whose API has no
  lookup-by-our-reference**, so a genuine zero-response timeout ends at 202 rather than a verdict.
  Accepted deliberately: AC16/17 forbid reporting failure without confirmation, and the
  `?idem=` callback tag plus `idemref:` makes the common slow-answer case resolve concretely. The
  caller re-polls the same key; no double-dial is possible within the 120s window.
- **`ISmsProvider` on Vobiz is written against an unverified Message API** (only the Call API was
  live-verified 2026-07-30). Mitigated by `FakeProvider` carrying every SMS test and by `/sms/send`
  having no production caller at merge — the `send_sms` tool was retired
  (`scripts/retire_calendar_sms_tools.sql`), so AC19's key format is implemented and tested but
  dormant until a tool re-adopts it (PRD open question 2 stays open).
- **Three services serving telephony during the migration window.** Mitigated by the per-row
  atomic cutover: a `telephony_configs` row's vendor dashboard URL points at exactly one host at a
  time, and the two old services keep their own ports (8500/8700) while the new one takes 8750,
  so no route is ever double-bound (AC34).
- **Statuses are exactly `healthy`/`degraded`/`standby`, with no fourth "down" state**, so a
  provider failing every probe reads the same as one failing its first. Accepted: those are the
  only three the Admin UI badge renders (`page.tsx:59-64`), and inventing a fourth would render as
  an unstyled dot. AC27's floor is satisfied either way.

## Test plan

**Unit, `libs/telephony_sdk/tests/`**

- `test_interface_conformance.py` — mechanically instantiate *every* class in
  `TelephonyProviderRegistry.all()` and `SmsProviderRegistry.all()` and assert no
  `TypeError: abstract`; assert the registry's own size against the count of modules in
  `providers/`, so a provider added without conforming makes the enumeration itself fail rather
  than being silently skipped (lesson 12, lesson 29). Separately assert `"fake" not in visible()`
  **and** `get("fake")` succeeds (AC4).
- `test_transfer_unsupported.py` — `transfer_call` on both real providers raises (AC5).
- `test_check_health_default.py` — a provider that does not override returns `True` (AC3).

**Unit, `services/telephony/tests/`**

- `test_ratelimit_order.py` — the case that matters: N+1 requests **all with an invalid signature**
  get 429, not 403, proving the limiter runs first and that failed auth consumes quota (AC11, AC12).
  It fails if the order is swapped, because a signature-first implementation returns 403 forever.
- `test_inbound_orchestration.py` — (a) valid signature produces the exact
  `/{provider}/stream/{token}` URL whose token is absent from the request the vendor sent (i.e.
  server-minted), plus exactly one stored call context; (b) invalid signature
  returns 403 **and** the `CallContextStore` is empty and `resolve_did_route` was never awaited
  (assert on a spy's call count, not on absence of a value that has no path there); (c) unknown
  provider returns 404 with the rate limiter's bucket count still zero (AC10); (d) a Cloudonix-shaped
  normalized call with a foreign `did:` entry returns 403; (e) an AST/import check that neither
  `providers/vobiz.py` nor `providers/cloudonix.py` references `did_route` or `CallContextStore`
  (AC9 — mechanically derived, not asserted in prose).
- `test_idempotency.py` — against a real Redis (the repo's tests already require one): first call
  sets `in_flight NX EX 120` before the `FakeProvider` is touched; a concurrent second call with a
  key finalized at t+1s returns the cached body with the fake's dial counter still at 1; a second
  call against a never-finalized key returns **202 within 10s** (asserted with a wall-clock upper
  bound *and* a lower bound, so a fix that returns 202 instantly without polling also fails);
  fake-timeout + `reconcile=placed` returns 200 with the real id (AC17); fake-timeout +
  `reconcile=not_placed` returns `ok: false`; fake-timeout + `indeterminate` returns 202.
  The deployed `POLL_BUDGET_S` is exercised, never reassigned (lesson 25).
- `test_accounts.py` — a provider declaring a scalar sensitive field and one declaring a list field
  both decrypt; a plaintext legacy value passes through with a warning; an undecryptable entry is
  skipped without failing the refresh; a failed Config fetch keeps the last-known-good map.
- `test_outbound_auth.py` — the findings' own regression suite, each case written so it goes red
  if the control is deleted:
  (a) every outbound route with **no** `Authorization` header returns 401 and the `FakeProvider`'s
  dial/send counters stay at 0 — enumerated by walking `app.routes` and filtering on the route's
  own `dependant`, asserting the walk's *size* as well as its members so a future route added
  without the dependency fails the enumeration rather than being skipped (lessons 12, 29);
  (b) a tenant-B admin's token naming `tenant_slug: "tenant-a"` gets 403/404 with the counters at
  0 — and the assertion is on tenant B's *own* response shape being invariant to whether
  `tenant-a` exists at all, not on byte-equality with some other principal's body (lesson 2);
  (c) a valid tenant-A token with `from` = a DID owned by tenant B ⇒ 403, counters at 0;
  (d) a valid tenant-A token with `agent_slug` = tenant B's agent ⇒ 403, counters at 0, and the
  `agent:tenant-a:<b-agent>` key genuinely absent from Redis so the fallback path is the one
  exercised;
  (e) tenants A and B submitting the *identical* idempotency key concurrently both reach the
  provider and each gets its own result — the tenant-scoping test that fails on the unscoped key
  (finding #3), where the un-scoped implementation returns B the cached A result;
  (f) `GET /{provider}/call/idempotency/{key}` for a key finalized under tenant B returns 404 to
  tenant A, identical to a key that never existed.
- `test_hot_path_isolation.py` — the mechanical version of the Latency section's claim, so it
  cannot rot into prose. Walks `app.routes` and asserts the WS route and the inbound voice/status
  routes have an empty `dependant.dependencies` list; ASTs the module backing the WS handler and
  asserts it contains no call into `auth`, `ownership` or `idempotency`; and drives one full
  connected-call fixture through `MediaStreamBridge` against a monkeypatched Redis client that
  raises on every operation, asserting the call still streams — a check that goes red the moment
  any Redis lookup is added to the connected-call path, and that could not pass vacuously because
  the same fixture's *webhook* phase does perform a `did:` GET and so must be run before the
  raising client is installed.
- `test_no_unawaited_guards.py` — an AST walk over every module under `services/telephony/` that
  fails on any `Call` to `assert_tenant_access`, `resolve_caller_tenant` or
  `resolve_outbound_identity` not wrapped in an `Await` (and on any `def` — rather than
  `async def` — definition of the latter two). This is the layer that catches R2-1 directly: a
  missing `await` raises nothing at runtime, so without it the only signal is a behavioural test
  going red for a reason nobody attributes to the await. The walk asserts its own found-call
  count is non-zero, so it cannot pass vacuously by matching nothing (lesson 12).
- `test_outbound_roles.py` — a tenant `viewer`'s token gets 403 on `/{provider}/call` and
  `/sms/send` with the `FakeProvider` counters at 0 (R2-2); a NULL-tenant `is_service_account`
  token with the *same* `role="viewer"` claim gets through, so the test distinguishes the two
  clauses rather than passing on the role alone; a tenant-scoped `is_service_account=True` token
  is still refused. Enumerated over the same `app.routes` walk as `test_outbound_auth.py`,
  asserting its size, so a route added later without the dependency fails here too.
- `test_status_callback_scoping.py` — a callback validly signed by account A carrying
  `?idem=<a key tenant B minted>` writes `idemref` only under A's tenant, and B's
  `observed_call_id()` still returns `None`; the test fails against a route with no `account_ref`
  because such a route has no tenant to scope the write to (finding #5).
- `test_callctx.py` — the WS admission token is not equal to, and not derivable from, the
  `provider_call_id` in the same context; connecting with the `provider_call_id` in the path is
  refused (1008); a second connection with the correct token is refused; a token issued under
  account A does not admit a WS connection claiming account B's provider/account (finding #4).
- `test_health.py` — probe sequences `[]`→standby (key absent), `[ok]`→healthy,
  `[ok, fail]`→degraded, `[fail, fail]`→degraded; and that the key carries a TTL greater than the
  interval (AC25-28).

**Unit, `services/config/tests/test_telephony_configs.py`** (extend)

- Vobiz create stores `auth_token` as `enc:` (AC22); Cloudonix's `api_keys` list behaviour is
  unchanged; an `enc:` value round-trips un-double-encrypted; `env:`/`k8s:` is rejected for both a
  scalar and a list field (AC21, AC23); `provider='native'` skips normalize and validate;
  `list_supported_providers()` exposes both key sets and omits `"fake"` (AC24).

**Unit, `services/campaigns/tests/test_worker.py`** (extend)

- Same attempt retried through the HTTP hop reuses one key across 3 attempts (AC18, AC20); a new
  attempt (post-requeue, incremented `attempt_count`) mints a different one; a 202 leaves the
  contact at `'calling'` and resolves via `poll_idempotency` on the next tick rather than
  requeueing; a `'native'`/`None` provider still takes the ESL path (AC33).

**Migration, `scripts/`**

- Applied against a DB seeded with a 5000-5009 `'cloudonix'` row, a non-5000-range `'cloudonix'`
  row, and an already-`'native'` row: only the first changes, `updated_at` is byte-identical
  before and after, and a second run reports zero rows (AC29-31). Run against a database that can
  actually trip each branch — an empty DB proves nothing (lesson 12).

**Integration / manual, phase gated**

- Phase 1: place a real inbound call to `+14165550177` after Vobiz is repointed at 8750; assert the
  call answers, media streams, and a `calls` row plus transcript appear exactly as before (AC32).
- Phase 1: with the service live, `curl -X POST /{provider}/call` with no token and with a
  wrong-tenant token, confirming 401/403 against the real app rather than a TestClient (lesson 23).
- Phase 2: one live Cloudonix call after the dashboard URL change; then drive the Admin UI
  Telephony page in a browser and confirm the health dots are green from Redis, not from the DID
  heuristic (lesson 23).
- Phase 3: `grep -rn 'services\.vobiz\|services\.cloudonix\|8500\|8700'` across `services/`,
  `admin-ui/`, `scripts/`, `docs/`, `deployment/` returns nothing before the deletion merges
  (AC35, lesson 17).
