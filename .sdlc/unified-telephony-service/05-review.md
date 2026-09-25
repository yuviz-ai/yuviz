# Code review

## Round 2 (final)
VERDICT: AMBER — all 3 blocking findings verified fixed; 3 minor findings deliberately left open.

Fixes verified:
- R1-1 closed — `services/campaigns/worker.py:26` now does `from libs.telephony_sdk import providers as _telephony_providers  # noqa: F401`, so `TelephonyProviderRegistry.all()` is populated in the Campaigns process and `route["provider"] in …` can actually be true for `vobiz`/`cloudonix`.
- R1-2 / R1-3 closed — `outbound.place_call` calls `outbound_identities.remember(provider, account.account_ref, idempotency_key, …)` at `services/telephony/outbound.py:64-68`, **before** `initiate_call`, so a fast vendor answer callback cannot race the write. `orchestrator.handle_inbound_webhook` recalls it at step 7 *after* provider lookup, accounts-loaded, both rate limits, signature verification and `normalize_inbound_webhook` — so the `?idem=` lookup is reachable only by a caller that already passed the account's signature check, and the key is `(provider, account_ref, idem)`, so one account can only ever recall identities its own `place_call` stored. A miss falls through to ordinary `resolve_inbound_route`, so genuine inbound calls are unchanged; `direction` defaults to `"inbound"` on both `InboundRoute` and `CallRoute` and is now plumbed to the bridge at `services/telephony/app.py:90`, matching the deleted `services/vobiz/app.py:234` semantics (`caller_did`=our DID, `called_did`=callee).

No new defects found in the round-2 changes.

Note (not a finding, same constraint `CallContextStore` already carries): `OutboundIdentityStore` is process-local and `services/telephony/__main__.py` runs a single uvicorn process with one container, so this holds today — but a second replica behind a load balancer would send the answer callback to a process with no memory of the call and silently re-enter the DID path that findings 2/3 describe. If this service is ever scaled out, both stores need to move to Redis.

Open from round 1 (accepted, not fixed):

4. [minor] A pending (202) attempt is marked `completed` without ever learning whether the call was answered — `services/campaigns/worker.py:_resolve_pending_idem` — fails when: `poll_idempotency` returns a vendor call id for a call that then goes unanswered; the contact is set to `completed` with that id, the id is never put in `_job_to_contact`, so the later `/internal/vobiz-call-resolved` callback finds no mapping and is dropped. A no-answer on the pending path is recorded as a success and never retried, unlike the identical outcome on the non-pending path — fix: on resolution store `self._job_to_contact[call_id] = (campaign_id, contact_id, max_attempts, attempt_count)` and leave the contact at `calling`.

5. [minor] An indeterminate attempt can still be re-dialled with a fresh key — `services/campaigns/worker.py:_resolve_pending_idem` + `services/campaigns/telephony_originate.py:145` — fails when: the vendor is slow, `place_call` returns 202 and leaves the claim `in_flight`; after `IDEM_TTL_S=120` the Redis entry expires, the poll 404s, `poll_idempotency` raises `TelephonyOriginateError`, the worker requeues to `pending`, the next claim bumps `attempt_count` and mints a *different* idempotency key — a second real dial to a customer the vendor may already have called. The design licenses a new key only on a resolved `not_placed` (AC16) — fix: treat the 404-after-pending case as exhausted/indeterminate (no requeue), or keep the reference alive past the poll window.

6. [minor] SMS timeout reconciles as a call and can never resolve — `services/telephony/outbound.py:97,112` — fails when: `send_sms` times out; `_reconcile_timeout` calls `account.instance.reconcile_call(reference=key, …)`, and `note_reference` is only ever written by the *call* status callback, so `observed_call_id` is always `None` → `"indeterminate"` → a permanent 202 with the claim stuck `in_flight` for 120s, during which an honest retry of the same key also 202s. `ISmsProvider.reconcile_message`, which the design specified for this path, has no caller at all — fix: call `reconcile_message` from the SMS branch, or drop it from the interface and say why SMS reconciliation is unsupported.

---

## Round 1
VERDICT: RED

1. [blocking] Campaigns' REST-vs-ESL dispatch can never take the REST branch — `services/campaigns/worker.py:26,190` — the worker imported only `libs.telephony_sdk.registry`; provider classes self-register on import of `libs.telephony_sdk.providers`, which nothing in the campaigns process imported (`libs/telephony_sdk/__init__.py` is empty), so `TelephonyProviderRegistry.all()` was `{}` at runtime and every campaign contact fell through to the ESL path. **FIXED in round 2.**

2. [blocking] The outbound call's `agent_slug` never reached the media bridge — `answer_url` re-entered `handle_inbound_webhook`, which resolved the agent from `call.to_number` (the callee, a DID-cache miss) → `agent_slug="default"`, `direction="inbound"`; `identity.agent_slug` was read by nothing. **FIXED in round 2.**

3. [blocking] An outbound call to a number that is another tenant's provisioned DID was rejected — `resolve_inbound_route` saw `hit_tenant != account.tenant_slug`, returned `None`, and the answer webhook returned 403 with no answer XML (dead air). **FIXED in round 2.**

4-6. Minor findings, carried forward above.
