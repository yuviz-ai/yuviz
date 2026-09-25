# Security review: .sdlc/unified-telephony-service/02-design.md
VERDICT: AMBER (round 3 — final)

Pre-implementation design review (`services/telephony/` does not exist yet). Round 3 re-traced the
authorization chain end to end against `services/config/deps.py`, `services/config/auth.py`,
`services/config/users.py`, `database/schema.sql`, `scripts/start_local.sh`,
`deployment/docker/docker-compose.yml`, `services/config/agents.py`,
`services/cloudonix/handoff.py`, `libs/telephony_sdk/did_route.py`,
`services/config/telephony_configs.py`.

No critical or high finding is open. Four mediums and two lows remain, all reported below with
their attack paths; none of them is a cross-tenant data path or a privilege escalation.

## Finding disposition across rounds

| # | Finding | R1 | R2 | R3 |
|---|---|---|---|---|
| 1 | No authN/authZ on outbound routes; tenant from body | critical | fixed | fixed |
| 2 | `agent_slug`/`from` not ownership-checked | critical | fixed | fixed (see M4 for the `None` widening) |
| 3 | Unscoped `idem:{provider}:{key}` | critical | fixed | fixed |
| 4 | Vendor-supplied WS admission key | high | fixed | fixed |
| 5 | `/{provider}/status` had no account segment | high | fixed | fixed |
| 6 | Idem key as a bearer capability in a vendor URL | medium | closed by #3 | closed |
| 7 | Tenant gate specified as a sync call to an `async` guard | — | critical | **fixed** — `async def` + literal `await deps.assert_tenant_access(...)`, written once at the site that must satisfy it, with the failure mode spelled out (lesson 32) |
| 8 | `viewer` in `TELEPHONY_CALLER_ROLES` | — | high | **fixed** — roles narrowed to `{superadmin, admin}` with a separate `is_service_account and tenant_id is None` clause |
| M1 | 403 vs 404 split is a tenant-existence oracle | — | medium | **still open** |
| M2 | Account/agent de-provisioning staleness | medium | medium | still open |
| M3 | 12h JWT revocation lag on outbound | — | medium | accepted, still open |
| M4 | `OutboundIdentity.agent_slug` widened to `str \| None` | — | — | **new** |
| M5 | One shared service-account credential now places PSTN calls for every tenant | — | — | **new** |
| L1 | `_NON_REST_PROVIDERS` bypasses the `env:`/`k8s:` rejection | low | low | still open |
| L2 | Rate-limit bucket choice is an account-existence oracle | low | low | still open |

## Findings

M1. [medium] `resolve_caller_tenant` returns 403 for a known slug and 404 for an unknown one — a
   tenant-existence oracle — Interfaces → `services/telephony/auth.py` prose ("an unknown slug
   raises the same 404 `assert_tenant_access` already raises")
   Attack: unchanged from round 2 and still stated the opposite way round in the design. The UUID
   branch of `assert_tenant_access` raises **403** on mismatch (`deps.py:190-191`); the unknown-slug
   case never reaches that function at all — it dies in the `AccountStore` map lookup — and the
   design specifies **404** there. So for one fixed principal, a tenant-B admin, the response varies
   with the target's existence: a slug with a loaded telephony account gives 403, one without gives
   404. Tenant B enumerates which customers exist on the platform and which have telephony
   provisioned. Lesson 2's per-caller invariance. `test_outbound_auth.py` case (b) asserts
   "403/404", an `or` arm true under both implementations, so it cannot fail on this (lesson 12).
   Fix: raise the same 404 with the same body for both — catch the 403 from the UUID branch and
   re-raise it as the unknown-slug 404 — and change case (b) to assert the exact status and body
   and to assert it is invariant across an existing and a non-existing target slug for that one
   principal.

M2. [medium] De-provisioning still does not take effect; the agent memo is process-lifetime —
   `services/telephony/accounts.py`; `ownership.py` agent fallback; Risks
   Attack: a tenant whose Cloudonix API key or Vobiz `auth_token` leaked deletes the
   `telephony_configs` row. The `AccountStore` refresh is a 300s loop whose only stated failure
   policy is "keep the last-known-good map", with no cap and no statement that a successful refresh
   *removes* rows that vanished — so the stolen credential keeps producing signature-valid inbound
   webhooks, and with them a call context and a WS bridge into that tenant's agent, for at least
   5 minutes and indefinitely while Config is unreachable. The agent memo is "per (tenant, agent)
   for the process lifetime", so an agent deleted after a compromise stays dialable until restart.
   Fix: replace the account map wholesale on each successful refresh; cap last-known-good to N
   consecutive failures then fail closed (503, matching step 2's `accounts.loaded` posture); give
   the agent memo the same 300s refresh rather than process lifetime.

M3. [medium] Outbound auth is a pure JWT decode, so revocation lags up to the 12h token TTL —
   Interfaces → `auth.py`, Risks bullet 3
   Attack: lesson 27. `get_authenticated_user` (`deps.py:72-87`) never reads `users`; only
   `get_current_user` does, via the memoized `fresh_console_authority` (`deps.py:252+`). A
   tenant_admin soft-deleted or demoted today keeps placing calls and SMS for their tenant until
   the token expires. The design names and accepts this against the PRD's Redis-only constraint,
   which is defensible — with the caveat that the avoided cost is one row-read per user per memo
   TTL, on a request that already crosses the network to a vendor, and that the Redis-only rule was
   written for the inbound/media hot path. Now that the role gate is `{superadmin, admin}` plus
   service accounts, the exposed population is small.
   Fix (optional): use a freshness check on the outbound routes with this route's own role set, or
   record the 12h window explicitly in the offboarding runbook.

M4. [medium] Widening `OutboundIdentity.agent_slug` to `str | None` for SMS makes the agent
   ownership clause skippable by omission on `/{provider}/call` — Interfaces → `ownership.py`,
   route snippets
   Attack: the SMS change is correct in itself (no agent is involved, and the caller-id DID check
   still runs). But the validated twin's agent field is now nullable for *both* callers, and the
   design does not say that `/{provider}/call`'s request model requires `agent_slug`. An
   implementer who makes `body.agent_slug: str | None = None` — the natural reading of a field the
   twin now permits to be absent — gives any authorized caller a request that skips the agent
   clause entirely: `resolve_outbound_identity` validates only the DID, and `place_call` proceeds
   with `agent_slug=None`. Not cross-tenant (the tenant is still pinned), but it is an ownership
   check that a body field can turn off, which is the shape lesson 31/32 exists for.
   Fix: state that `/{provider}/call`'s body model types `agent_slug: str` (required, non-empty)
   and that `place_call` asserts `identity.agent_slug is not None` on entry; keep `None` reachable
   only through the `/sms/send` snippet. Add a test case: `/{provider}/call` with `agent_slug`
   omitted returns 422/403 with the dial counter at 0.

M5. [medium] One shared service-account credential now grants outbound PSTN calling and SMS for
   every tenant — `services/telephony/auth.py` second clause;
   `deployment/docker/docker-compose.yml:70,136,206`, `scripts/start_local.sh:135`
   Attack: the `is_service_account and tenant_id is None` clause is the right predicate (verified:
   `is_service_account` is a real JWT claim — `auth.py:32,50,71` — and
   `conversation-service@internal.yuviz.ai` is flagged `true` by `schema.sql:249`'s `%@internal.%`
   backfill, so Campaigns and Conversation really do pass it). The residual risk is that
   `CONFIG_SERVICE_EMAIL`/`CONFIG_SERVICE_PASSWORD` is *one* credential shared by Conversation,
   vobiz and Campaigns across three compose services. Before this design that credential bought
   platform-wide config reads; after it, it also buys "place a call or send an SMS from any
   tenant's trunk to any number". Anyone who reads the compose env — an operator with host access,
   a leaked `.env`, or a foothold in any one of the three containers — gets a cross-tenant
   telephony capability that did not previously exist.
   Fix: mint a dedicated service account for Campaigns (`scripts/create_service_account.py`
   already exists) and scope the second clause to that identity rather than to any NULL-tenant
   service account; note the new blast radius of `CONFIG_SERVICE_PASSWORD` in `docs/telephony.md`.

L1. [low] `_NON_REST_PROVIDERS = {"native"}` bypasses the `env:`/`k8s:` rejection on a
   tenant-writable column — Interfaces → `services/config/telephony_configs.py`
   Attack: `credentials` is tenant-writable JSONB and `provider` is settable at create; a
   `provider='native'` row skips both `_normalize_credentials` and `validate_credentials`, so
   `{"api_keys": ["env:JWT_SECRET"]}` is stored verbatim and read back unredacted
   (`telephony_configs.py:7-9`). No `env:`-resolving sink reads this column today, so the gain is
   persistence only — but this is lesson 37's control and the design hands tenants a field value
   that switches it off.
   Fix: keep the `native` skip for sealing only and run the `env:`/`k8s:` rejection for every
   provider, or reject a non-empty `credentials` object for `provider='native'`.

L2. [low] The rate-limit bucket choice is a `telephony_config` existence oracle — orchestrator
   step 3
   Attack: step 3 picks `{provider}:{ref}` for a loaded account and the shared `{provider}:unknown`
   otherwise, before the 403-identical step 4. Exhaust the shared bucket, then probe: a candidate
   UUID still returning 403 is a real account, one returning 429 is not. Low value — UUIDs are
   unguessable, so this confirms rather than discovers — but it partly undoes the Risks bullet's
   own mitigation.
   Fix: bucket an unknown `ref` under a hash of the presented ref, or name the oracle in that Risks
   bullet as accepted.

## Verified controls

- `resolve_caller_tenant` is `async def` and the design writes the literal
  `await deps.assert_tenant_access(tenant_uuid, user)` at the one site that must satisfy it, with
  the un-awaited failure mode spelled out rather than left implicit (lesson 32).
- All three outbound-reaching handlers — `/{provider}/call`, `/sms/send` and
  `GET /{provider}/call/idempotency/{key}` — are written out individually and each begins with the
  same awaited `resolve_caller_tenant`; the parallel call sites were diffed against each other, not
  just against prose (lesson 30), which is what surfaced the SMS and idempotency-read gaps.
- `GET /{provider}/call/idempotency/{key}` taking `tenant_slug` through the same awaited gate is
  the correct repair: reading `user.tenant_id` directly would have 404'd Campaigns' own 202 poll,
  since that caller is NULL-tenant (lesson 24 — scoping is `tenant_id IS NULL`, not role).
- `TELEPHONY_CALLER_ROLES = {superadmin, admin}` plus a separate
  `is_service_account and tenant_id is None` clause. Verified end to end: `is_service_account` is a
  real JWT claim (`auth.py:32` on `CurrentUser`, `:50` at mint, `:71` at decode), and the shared
  service identity `conversation-service@internal.yuviz.ai` (`start_local.sh:135`,
  `services/vobiz/app.py:54`) is flagged by `schema.sql:249`'s `%@internal.%` backfill — so the
  clause admits the real callers and a tenant-scoped `viewer` fails both clauses. Matches
  `require_live_calls_operator`'s existing precedent (`deps.py:204-216`) of a non-console grant
  carrying its own role set instead of widening `CONSOLE_ROLES` (lesson 4).
- `test_outbound_roles.py` distinguishes the two admission clauses rather than passing on role
  alone — so deleting the `tenant_id is None` conjunct goes red (lesson 12).
- `test_no_unawaited_guards.py` walks the AST of every `services/telephony/` module for the
  un-awaited-guard shape, which is the mechanical form lesson 29 asks for. One caveat, not a
  finding: derive its list of guard names from the `async def`s actually exported by
  `services/config/deps.py` rather than a hand-written list, or a future guard is silently skipped.
- `OutboundIdentity` is still the only value `place_call`/`send_sms` accept for
  tenant/agent/caller-id; the raw body fields are out of scope at the call site (lessons 31, 32) —
  subject to M4 on the nullable agent field.
- Caller-id ownership runs for SMS as well as calls, via `did:{did}` — write-through, no TTL,
  preloaded at startup, so a cold cache cannot manufacture a false rejection.
- Agent ownership uses `agent:{tenant_slug}:{agent_slug}`, confirmed at
  `services/config/agents.py:47`; the key is itself tenant-scoped, so a hit is the ownership proof,
  and a Config-down miss is a 403, not a silent admit.
- `idem`/`idemref` carry `{tenant_id}` from `OutboundIdentity` only; required positional on all
  five functions, one private `_key()` builder.
- WS admission is back to `HandoffStore`'s server-minted `secrets.token_urlsafe(32)` with
  claim-once, TTL and capacity bound (`services/cloudonix/handoff.py:44-58`); the deviation from
  AC6's literal URL wording is declared in Risks rather than silently taken.
- `/{provider}/status/{account_ref}` verifies against exactly one account and writes `idemref`
  under that account's tenant; unknown `account_ref` and bad signature share one status and body.
- Inbound step 4 makes unknown account and bad signature indistinguishable; step 1's 404 for an
  unregistered provider is correctly reasoned as outside the tenant boundary (lesson 2).
- Tenant comes from the authenticated account, never the DID; a DID hit whose tenant differs
  returns `None` ⇒ 403, closing `resolve_did()`'s default-tenant fallback for the Vobiz path.
- Rate limiting precedes signature verification with an unconditional `increment()` (AC11/12); the
  per-DID bucket includes `provider:ref`.
- `claim()` is `SET NX EX 120` before any vendor request; `await_outcome` returns `None` on both
  budget exhaustion and Redis error, so no losing claimant reaches the vendor; `reconcile_call`
  never guesses `not_placed`.
- `CloudonixProvider.verify_webhook_signature` becomes a real constant-time compare over
  non-encrypted entries only, no short-circuit — fails closed for Config-constructed instances.
- `_normalize_credentials` generalized over `sensitive_credential_fields()`; `enc:` kept verbatim;
  `env:`/`k8s:` rejected for REST providers (lesson 37) — modulo L1's `native` bypass.
- Campaigns carries the service-account JWT with the existing `_login()`/401-retry-once helper
  (`services/vobiz/app.py:62-88`) and is platform-scoped, so it legitimately passes
  `assert_tenant_access` for the tenant it names.
- Campaigns key pinned to `attempt_count`; retries reuse it; a 202 leaves the contact at
  `'calling'` rather than requeueing into a fresh key.
- Health key TTL is 3× the interval, absence == Standby, one code path; read per-row after the
  tenant-scoped Postgres fetch.
- `/health` is deliberately undepended so the docker-compose healthcheck still passes — lesson 1's
  exact failure mode, avoided.
- Migration is predicate-idempotent, touches no other column, leaves `updated_at` byte-identical,
  invalidates `telephony_config:{id}`; no schema change, so `tests/test_rls_coverage.py` stays
  valid and nothing leans on RLS as its only isolation layer (lesson 36).
- Test plan derives the "every outbound route carries the dependency" list by walking `app.routes`
  and asserts the walk's size (lessons 12, 29), exercises the deployed `POLL_BUDGET_S` (lesson 25),
  and includes a live `curl` against the running app (lesson 23).
