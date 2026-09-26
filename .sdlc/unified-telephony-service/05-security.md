# Security review: unified telephony service (working diff)
VERDICT: AMBER  (round 2 of 3 — round 1's high is closed and verified; no critical/high open)

## Round 1 findings — status

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 1 | high | Unowned `caller_id` falls through to the unchecked ESL originate | **FIXED, verified** |
| 2 | medium | Query injection into vendor callback URLs via `idempotency_key` | Open (accepted this round) |
| 3 | medium | No rate limit on outbound `/{provider}/call` and `/sms/send` | Open (accepted this round) |
| 4 | medium | Unauthenticated, unscoped `/internal/vobiz-call-resolved` | Open (accepted this round) |
| 5 | medium | `provider='native'` credentials unsealed and unredacted to any tenant role | Open (accepted this round) |
| 6 | low | 403-vs-404 existence oracle on telephony-config by-id routes | Open (accepted this round) |
| 7 | low | Vobiz signature covers neither query nor body; replayable | Open (accepted this round) |
| 8 | low | `JOIN agents a ON a.id = $2` with no tenant predicate | Open (accepted this round) |

All six accepted items were re-confirmed present in the current tree
(`outbound.py:73-75`, no `over_limit` in `services/telephony/app.py`, `campaigns/app.py:90`,
`routers/telephony_configs.py:37,90`, `vobiz.py:115`, `campaigns.py:199`) — unchanged, not
regressed, not newly reachable.

### Finding 1 — verified closed

Traced all three layers:
- `campaigns.py:194-205` now returns `(pn.id IS NOT NULL) AS caller_id_owned` as a fact separate
  from `provider`, so "not owned" and "owned but native/ESL" are no longer conflated.
- `routers/campaigns.py:54,86-89` 422s on create and on PATCH when `caller_id` is not a
  `phone_numbers` row for the campaign's tenant, mirroring the `agent_id` check.
- `worker.py:183-198` refuses the dial before the `try` block — crucially *before* the
  `provider in TelephonyProviderRegistry.all()` branch, so the refusal cannot be reached by the ESL
  `else` arm, and an owned-but-native DID (`provider is None`, `caller_id_owned True`) still dials.
  This is the load-bearing layer: it re-checks at dial time, not only at create time.
The round-1 attack (tenant A campaign with tenant B's DID as `caller_id`) is now rejected at 422 at
creation and, if the row predates the fix or the DID moves afterward, refused at dispatch with no
vendor call. Confirmed the `?idem=`/`OutboundIdentityStore` path was not used as a substitute
control here — it remains tenant-bound via `account_ref` and populated only from an already
validated `resolve_outbound_identity`.

## New findings

9. [low] The dial-time ownership gate ignores `deleted_at`, so it is weaker than the create-time
   check it backstops — `services/campaigns/campaigns.py:200` vs `campaigns.py:77-80`
   `caller_id_owned_by_tenant` filters `AND deleted_at IS NULL`; `resolve_outbound_route`'s
   `LEFT JOIN phone_numbers pn ON pn.tenant_id = t.id AND pn.did = $3` does not. A soft-deleted
   row therefore still sets `caller_id_owned = true` at dispatch.
   Attack: a tenant admin creates a campaign on DID X, then releases X
   (`DELETE /phone_numbers/...`, a soft delete). The campaign keeps dialing and keeps presenting X
   as caller id — a number the tenant no longer holds and that the carrier may have handed to
   someone else — and the create-time 422 would now refuse the very same configuration. Cross-tenant
   re-provisioning inside the platform is blocked by `phone_numbers.did TEXT NOT NULL UNIQUE`
   (the soft-deleted row keeps the DID reserved), which is what holds this at low rather than
   reopening finding 1.
   Fix: add `AND pn.deleted_at IS NULL` to the `resolve_outbound_route` LEFT JOIN so the two
   predicates are literally the same.

10. [low] A permanently-unowned `caller_id` burns the contact's whole retry budget rather than
    stopping the campaign — `services/campaigns/worker.py:197`
    The refusal path calls `_resolve_with_retry(..., "failed")`, which requeues the contact to
    `pending` until `max_attempts` is reached. The condition is not transient: every retry re-runs
    the same query, gets the same answer, and consumes a pacing slot and an attempt.
    Attack: low-impact self-inflicted — a tenant whose DID was reassigned sees every contact churn
    to `failed` over `max_attempts` ticks with only a `log.warning` and no campaign-level signal,
    which is how a misconfiguration stays invisible. Not a cross-tenant issue; noted because the
    refusal branch is new code.
    Fix: treat an unowned `caller_id` as terminal for the contact (mark `failed`, no requeue), or
    pause the campaign once it is observed, since the condition is campaign-wide not contact-wide.

## Verified controls

(Re-verified this round unless noted; unchanged from round 1.)

- Campaign `caller_id` ownership is now enforced at create, at update and at dial time, with the
  dial-time gate placed ahead of the provider branch so the ESL path is no longer reachable with an
  unowned number (`worker.py:183-198`). New: 4 unit tests on `caller_id_owned_by_tenant` and 2
  worker regressions (unowned refused not ESL-dialed; owned-but-native still dials) — the second of
  which is what stops the fix from being a blanket "refuse everything native".
- `auth.resolve_caller_tenant` really does `await deps.assert_tenant_access(...)` (`auth.py:63`);
  `resolve_outbound_identity` and `_assert_agent_owned` are awaited at every call site (lesson 38).
- `require_telephony_caller` (`auth.py:35-39`) pairs the role allowlist with an explicit
  `is_service_account and tenant_id is None` clause instead of re-admitting `viewer` (lesson 24).
- Tenant-scoped callers get an identical 404 for "slug unknown" and "slug not mine", decided before
  `assert_tenant_access` can 403 (`auth.py:57-60`) — per-caller invariance on the outbound routes.
- Idempotency Redis keys are tenant-scoped through one private `_key()` helper
  (`idempotency.py:37-38`); `claim` is `SET NX EX` (atomic, not check-then-act) and a Redis error
  returns the loser path, so no branch lets a losing claimant reach the vendor.
- `OutboundIdentityStore` (`callctx.py:83-134`) is keyed on `(provider, account_ref,
  idempotency_key)` where `account_ref` is `telephony_configs.id` and therefore tenant-bound
  (`accounts.py:187-196`); `recall` can only return the `(tenant_slug, agent_slug)`
  `resolve_outbound_identity` already validated. The `?idem=` recall cannot name a foreign tenant or
  a foreign agent, and is not a route around the foreign-DID check.
- WS admission token: server-minted `secrets.token_urlsafe(32)`, single-use `pop`, TTL re-checked at
  claim, provider matched before the bridge starts (`callctx.py:49-69`, `app.py:70-98`).
- Inbound tenant is `known_tenant_slug or account.tenant_slug`, never the DID; a DID routing to a
  different tenant is a 403 with no answer XML (`orchestrator.py:82-94`).
- Webhook order is rate-limit → signature → parse; unknown account and bad signature return a
  byte-identical 403 and both consume quota, so neither account existence nor signature validity is
  observable (`orchestrator.py:117-144`); unknown refs bucket into `{provider}:unknown`.
- Cloudonix `api_keys` must be `enc:` at write time, `env:`/`k8s:` rejected in both
  `_normalize_one` and `validate_credentials` (lesson 37).
- `hmac.compare_digest` in both providers; Cloudonix iterates all keys without short-circuit and
  skips still-sealed entries.
- `list_configs_by_provider` gated on `is_platform_scoped`, not role, and is the only cross-tenant
  read, through the named `platform_conn` bypass.
- `resolve_outbound_identity` validates caller-id DID and agent ownership before any vendor call;
  `OutboundIdentity` is the sole carrier of `tenant_id` into every `idempotency.*` call
  (lesson 31/32 shape absent).
- Redis unavailability fails closed outbound (`resolve_did_route` → None → 403) and, inbound, to
  `agent_slug="default"` within the account's own tenant — never to a foreign tenant.
- Credential decryption accepts only `is_encrypted()` values and skips undecryptable rows
  (`accounts.py:131-141`); audit trails redact `credentials`.
- All SQL on the reviewed paths is parameterized, including the amended `resolve_outbound_route`
  and the new `caller_id_owned_by_tenant`.
