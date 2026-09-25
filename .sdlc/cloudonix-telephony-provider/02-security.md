# Security review: .sdlc/cloudonix-telephony-provider/02-design.md (revision 3, round 3)
VERDICT: RED

## Round-2 carry-over status
- **R2-1 (critical, cross-tenant via `phone_numbers.telephony_config_id`) — CLOSED.** Verified
  against the tree, not just the prose. See Verified controls 1-4.
- **R2-2 (high, pre-auth limiter denial) — CLOSED at the gate it named**, but a new shared
  exhaustible budget appears one step later; see finding 3.
- **R2-3 (high, tenant-writable `app_id` shadowing) — CLOSED**, but the replacement credential
  field (`api_key_refs`) is itself tenant-writable and reaches a far more powerful sink; see
  finding 1.

## Findings

1. [high] Tenant-writable `api_key_refs` are resolved by the Cloudonix process through
   `CompositeSecretResolver`, which accepts `env:` and `k8s:` — arbitrary env-var read,
   arbitrary absolute-path file read, and a blocking sync read on the event loop
   — design §`services/cloudonix/accounts.py` + §`libs/telephony_sdk/providers/cloudonix.py`
   (`validate_credentials`: "each `env:`/`k8s:`/`enc:`"), sink at
   `libs/config_sdk/secret_resolver.py:46-68`.
   `K8sFileResolver.resolve` does `self._mount_root / path_part` with no traversal or
   absolute-path guard — `Path("/var/run/secrets") / "/proc/self/environ"` is
   `/proc/self/environ`, the mount root is discarded entirely. `read_text()` is a synchronous
   call inside an `async def`. Note this is a *new* surface: `telephony_configs.credentials`
   today holds raw values (`VobizTelephonyProvider` reads `credentials["auth_token"]`
   directly), so nothing in the tree currently resolves a tenant-authored ref string out of
   that column — this design is the first.
   Attack: a `tenant_admin` of any tenant (a role that owns
   `PATCH /telephony-configs/{id}`; `update_telephony_config` re-runs only
   `validate_credentials`, which would pass) sets
   `credentials.api_key_refs = ["k8s:/dev/zero"]` on their own Cloudonix row. At the next
   `AccountStore.refresh()` (≤`CLOUDONIX_REFRESH_S`, 300s) the resolver blocks the Cloudonix
   event loop forever — every other tenant's inbound webhook and every live media bridge in
   that process stops. Variant: `["env:JWT_SECRET"]` makes the `hmac.compare_digest` in
   admission step 2 a confirmation oracle for any platform secret present in the Cloudonix
   container's environment (200 = guess correct), which for `JWT_SECRET` is superadmin token
   forgery.
   Fix: `CloudonixProvider.validate_credentials` must accept **`enc:` refs only** (reject
   `env:`/`k8s:` with an explicit message), and `refresh()` must resolve each row's refs
   inside `asyncio.to_thread` under its own `try/except` so one bad row is skipped, not
   fatal.

2. [medium] `list_configs_by_provider` filters `telephony_configs.deleted_at` but not
   `tenants.deleted_at`, and soft-deleting a tenant never removes its `did:{did}` keys —
   design §`services/config/telephony_configs.py` row ("non-deleted rows") and
   §`accounts.py`. `services/config/tenants.py:189` invalidates only `tenant:{slug}`;
   `phone_numbers` `did:` entries have no TTL by canonical design.
   Attack: an offboarded tenant whose `tenants.deleted_at` is set keeps a live
   `telephony_configs` row and live `did:` cache entries, so its Cloudonix Voice Application
   keeps authenticating, keeps passing the slug-equality check, and keeps opening billed
   conversation sessions against the deprovisioned tenant indefinitely — the contract-
   termination action does not terminate service. (`get_by_did`'s own SQL *does* filter
   `t.deleted_at IS NULL`, so this is an inconsistency, not a deliberate policy.) Lesson 16:
   the grant is re-cashed every refresh without re-validating the granting context.
   Fix: `list_configs_by_provider` joins `tenants t ON t.id = tc.tenant_id AND
   t.deleted_at IS NULL`; state in the design that a dropped account_id is removed from the
   map on refresh (not merely not-added), so revocation converges.

3. [medium] `_per_did` is one process-wide `FixedWindowCounter` keyed on attacker-supplied,
   un-normalized `To`, and its `_MAX_BUCKETS` cap refuses *new* keys — design §Rate limiting
   step 2 and §`app.py` step 4, against `services/config/app.py:97,119-129`.
   The counter refuses any key not already in `_buckets` once `len(_buckets) >= 20_000`
   (`_refuse_new_key` → `over_limit` returns `True`), so filling it denies every *other*
   account's DIDs. Two under-specifications decide whether that is reachable: (a) the design
   never says whether `_per_did.increment` runs before or after the `_per_account` check —
   if before, one account can mint 20,000 distinct buckets inside a window while its own
   account limit rejects the traffic; (b) the key is built from `did` at step 4 while
   `normalize_e164` is not applied until step 5, so `+15551234`, `15551234` and `0015551234`
   are three buckets for one number, which both multiplies the key space and lets an account
   trivially exceed its own `CLOUDONIX_DID_LIMIT`.
   Attack: tenant A's Cloudonix account (legitimate key) posts requests with distinct `To`
   spellings until `_per_did` is at capacity; from then on every first call of the window to
   tenant B's DIDs gets a `429` with no CXML — the caller hears nothing. This is R2-2's shape
   (a shared budget consumable on another account's behalf) re-entering one step past the
   gate that was fixed. Lesson 6: described-but-unspecified ordering is still open.
   Fix: key on the *normalized* E.164 (`_per_did.over_limit(f"{config_id}:{normalize_e164(to)}")`,
   moving normalization above step 4) and increment only after both checks admit the request;
   state both explicitly in the step list.

4. [medium] The tenant boundary's second fact — `did:{did}.tenant_slug` — has no ownership
   validation anywhere in the platform, and `phone_numbers.did` is globally `UNIQUE`
   (`database/schema.sql:415`) — design §Tenant boundary, table row "hit, tenant != account
   tenant → 403".
   `create_phone_number` (`services/config/phone_numbers.py:154-187`) inserts any DID string
   a tenant-scoped caller supplies, with no carrier/ownership check, and `did` is in
   `_UPDATABLE_FIELDS`.
   Attack: tenant A's `tenant_admin` POSTs a phone number whose `did` is tenant B's Cloudonix
   DID. B's inbound calls now resolve to tenant A, fail the equality check, and every one is
   `403`ed at the webhook — a total inbound outage for B that B cannot repair, because the
   global `UNIQUE` on `did` also prevents B from registering its own number. The design
   acknowledges "a poisoned or stale `did:{did}` can cause a denial" but attributes it to
   cache staleness; the actual writer is an ordinary authenticated tenant admin using a
   supported route. Lesson 3 (an unscoped unique index is a cross-tenant denial of service).
   Fix: out of scope to build here, but the design must name it as a stated prerequisite
   (DID ownership proof at `POST/PATCH /phone-numbers`) rather than treat `did:{did}` as
   trusted input, since this feature is what converts a misroute into a hard outage.

5. [low] Timing oracle over `{config_id}` — design §`app.py` step 2 and §Rate limiting step 3
   ("keeps the response identical for 'no such config id' and 'wrong key'").
   The unknown-config path returns `403` after a dict miss; the known-config path runs up to
   three `hmac.compare_digest` calls plus the account-key list walk. Bodies and status match,
   latency does not, and lesson 2 names latency explicitly.
   Attack: someone holding a candidate config id from a leaked URL, a proxy log or a referrer
   can confirm it names a real account before attempting key theft. Bounded in practice by
   `gen_random_uuid()` — 122 bits is not enumerable — which is why this is low and not
   higher.
   Fix: on the `account is None` path, run `hmac.compare_digest(presented, _DUMMY_KEY)` once
   before returning, so both paths do constant work.

Carried forward, knowingly accepted by the design and unchanged this round (not re-rated):
no concurrent-bridge cap or max-call-duration ceiling; `MEDIA_STREAM_DUMP_DIR` filename and
retention; the handoff token in the WebSocket URL path (60s, single-use, 256-bit).

## Verified controls
1. R2-1 is genuinely closed: the boundary rests on `telephony_configs.id` (PK) →
   `tenant_id UUID NOT NULL REFERENCES tenants(id)` (`database/telephony_schema.sql:14-15`),
   a column written only by `create_telephony_config` under the tenant-scoped
   `/tenants/{tenant_id}/telephony-configs` router, whose dependencies are
   `bind_path_tenant` + `require_path_tenant_access`. No caller-supplied tenant id anywhere.
2. `phone_numbers.telephony_config_id` really is unreferenced — a repo-wide grep across
   `*.py`/`*.ts`/`*.tsx` returns zero hits, confirming the design's "no writer anywhere in
   the tree" claim and justifying deletion-over-patch.
3. The slug-equality comparison is safe against impersonation: `tenants.slug` is
   `NOT NULL UNIQUE` (`database/schema.sql:9`), is **not** in
   `services/config/tenants.py:22-30`'s `_UPDATABLE_FIELDS` (immutable), and soft-delete
   retains the row, so a slug can never be released and re-taken by another tenant. The
   stale-`AccountStore`-slug-vs-fresh-`did:`-slug cross-tenant hit I went looking for is not
   reachable.
4. R2-3 is closed: no `app_id`; `domain` is the only tenant-writable identity-ish field and
   the boundary explicitly does not rest on it (step 3 is defense-in-depth and droppable
   without weakening step 2).
5. R2-2's named defect is closed: admission step 2 is a dict lookup plus ≤3
   `compare_digest` with no I/O and no shared mutable state, so an unauthenticated flood
   consumes no authenticated account's budget; the failed-auth counter is keyed on
   `config_id` or the literal `"unknown"` (so unknown path segments cannot mint buckets) and
   its `over_limit` is never consulted for a status code.
6. `FixedWindowCounter`'s move to `libs/ratelimit.py` is behavior-preserving as specified —
   read `services/config/app.py:36-155` in full: `_SWEEP_THRESHOLD`/`_SWEEP_INTERVAL`/
   `_MAX_BUCKETS`, `_maybe_sweep` called before the capacity check, and `_refuse_new_key`'s
   forced sweep are all earned behavior the design keeps verbatim (lesson 25 respected: the
   test plan runs deployed defaults).
7. `did:{did}`'s value shape is `{"tenant_slug","agent_slug","version"}`
   (`services/config/phone_numbers.py:104-112`), so `resolve_did_route` returning
   `tuple[str,str] | None` matches real data, and the `resolve_did` back-compat wrapper
   preserves the `("default","default")` contract `services/vobiz/app.py:151` depends on.
8. The new cross-tenant Config listing is gated on `is_platform_scoped`
   (`services/config/deps.py:88-98`, `tenant_id is None`), not on role — correct per lesson
   24, and it is the predicate the Conversation/vobiz `role="viewer"` service accounts
   actually satisfy.
9. Handoff design is sound: 256-bit `secrets.token_urlsafe(32)`, single-use pop, 60s TTL,
   `_MAX_PENDING` surfacing as `503` rather than being laundered into the routing fallback,
   and the WS URL carries no tenant or agent slug (lesson 31). The deliberate divergence from
   `services/vobiz/app.py:245`'s default-route fallback on an unknown id is the right call —
   that fallback is itself a latent misroute.
10. `verify_webhook_signature` returning `False` unconditionally is correct for a generic
    interface that cannot see the account's resolved secrets; a `True` stub would be a
    bypass for any future caller.
11. CXML generation interpolates only `CLOUDONIX_PUBLIC_BASE_URL` + a server-generated
    token, through `xml.sax.saxutils.quoteattr` — no tenant- or caller-controlled string
    reaches the XML.
12. Exposure: a dedicated `CLOUDONIX_BIND_ADDR` rather than the shared `BIND_ADDR`, which
    also publishes Postgres, Redis and the unauthenticated conversation gRPC port — the R1-3
    fix holds.
13. DTMF: the shared bridge's log line carries no digit and the existing regex assertion in
    `services/vobiz/tests/test_bridge_dtmf.py` now covers the Cloudonix path by sharing the
    code (lesson 33 sink check).
