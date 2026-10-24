# Design: Outbound campaign calling (own SIP + CPaaS), crash-safe, with launch readiness

## Approach
Each dial becomes a Postgres `campaign_attempts` row, committed `pending` before anything is sent. Everything else is a conditional `UPDATE` on that row. Outcomes come in through durable Redis Streams, one per writer: `campaign:events:fs` (FreeSWITCH, Path A), `campaign:events:cpaas` (the Telephony webhook, Path B) and `campaign:events:conv` (Conversation's `dnc_request`). Campaigns consumes them with a consumer group and acks only after Postgres commits, so the in-memory maps in `worker.py` (`_in_flight`, `_job_to_contact`, `_pending_idem`) and the ESL listener are deleted outright. Path A replaces ESL `originate ... &bridge(...)` with a JSON dial request over HTTP (owner decision; PRD criteria 31–35 and 102–104). Campaigns first writes a **dial intent** (key, tenant, callee, caller ID, agent DID, call UUID, expiry) to Redis, where only Campaigns can write. It then POSTs the HMAC-signed JSON to a dedicated loopback-only Kamailio `xhttp` handler that can do one thing: dial. Kamailio validates the request, picks a node with `ds_select_dst`, and sends the INVITE itself with `uac_req_send`. That INVITE carries only the key and `X-Call-Direction`. The chosen FreeSWITCH node dials only if it atomically consumes the matching intent with `GETDEL`, and it takes the callee, caller ID and tenant from the intent, never from SIP headers. A spoofed INVITE therefore cannot choose who is called or on whose behalf. The node places the callee leg through Kamailio's digest-authenticated PSTN egress and runs the existing `start_voice_ai.lua` on that same leg, so there is one audio leg and no hairpin. The obvious alternative was to keep ESL and teach Campaigns the pool. I rejected it because it breaks the product owner's rule that only Kamailio knows the FreeSWITCH pool, and because it keeps #117. Path B keeps the existing Telephony REST originate (`services/telephony/outbound.py`, Redis idempotency claim, `reconcile_call`). It only changes the key format, how rate limits are handled, and where status webhooks deliver.

Capacity limits (criteria 43–56) are enforced in one place: the `claim_next_due` admission transaction. That transaction takes a single platform-wide Postgres advisory lock. It derives every usage figure from `campaign_attempts` (campaign, tenant, platform and CPS) and reads every limit from Postgres in the same transaction. Because of that, multiple Campaigns workers cannot over-admit, a limit change applies on the next tick, and usage after a crash is simply the persisted rows. Nothing is counted in memory or in Redis. A limit that is reached returns a wait reason, and nothing is written except `campaigns.wait_reason`. The alternative was Redis counters. I rejected it because they would need rebuilding after a crash and would drift from the attempt rows, which are already the source of truth.

Business outcomes, callbacks, DNC, CRM hand-off and compliance (criteria 57–101) add no new store of truth for call data. Disposition and extracted variables are read live from `calls` through `campaign_attempts.call_session_id`. Both in-call tools (`schedule_callback`, `record_do_not_call`) are local Conversation tools that are offered only on outbound calls and send only the server-side `session_id` to an internal Campaigns route. Campaigns resolves the attempt, contact and tenant from that `session_id`. Time parsing, checking and storage happen there, not in the model. The CRM webhook uses a transactional outbox (`crm_deliveries`, inserted in the same transaction that makes the contact final) and a separate delivery task, so it is crash-safe and never runs on the dial loop. Retention and erasure follow data ownership. Campaigns purges its own rows. Campaign-linked `calls` and `transcript_entries` are erased through one SQL function, `erase_call_pii`, which the Conversation side owns. Campaigns calls it only for the sessions of its own attempts, during purge and erasure. It is the only call-data deletion in this design, and it keeps a single definition of "call personal data".

Billing (A13) is a seam, not a feature. Campaigns reaches billing only through `BillingAdmission` (`services/campaigns/billing.py`): `reserve`, `authorize`, `settle` and `release`, keyed by `campaign:<attempt row id>`. Until billing exists it ships with `NullBillingAdmission`. Every billing call is on the Campaigns control path: `reserve` right after the admission transaction commits, and the rest in a `_billing_sync()` step driven by `campaign_attempts.billing_state`, which `apply_outcome` sets in the same transaction as the outcome (an outbox, like `crm_deliveries`). FreeSWITCH, Gateway and Conversation make no billing call; the only billing value on the call path is `max_call_s` in the dial intent, which FreeSWITCH turns into `sched_hangup`. The obvious alternative, reserving inside `claim_next_due`, was rejected because it would put an external call under the platform-wide advisory lock that serialises every tenant's admissions.

## PRD amendments (resolving the open review findings)
These override the PRD text where they differ. The planner should treat them as acceptance criteria.

- **A1 (finding 1, Path A `sent` with no claim and no call record).** Before the deadline, nothing happens and nothing is re-sent under that key. At the deadline the attempt resolves as `failed` with `closed_by_deadline=true`. If attempts remain, the contact is re-queued for attempt+1 with a new key after the retry gap. (Possible causes: FreeSWITCH crashed between replying and claiming; the claim TTL lapsed on an unanswered call whose hangup event was lost. Neither can be an answered call, because an answered call always produces a `calls` row.)
- **A2 (finding 2, Path A live-call signal).** An attempt counts as live, and keeps its concurrency slot, if any of these holds:
  - (a) a `calls` row exists where `session_id = attempt.call_session_id` and `ended_at IS NULL`;
  - (b) the Redis key `campaign:claim:<key>` exists. FreeSWITCH sets it with `NX EX 105` before dialing, and 105 s covers the 45 s `originate_timeout` plus the gap between answer and `INSERT INTO calls`. A ringing call therefore always holds a claim;
  - (c) the attempt outcome is `answered` and has not yet become `ended`.

  The deadline handler fails an attempt only when (a), (b) and (c) are all false. FreeSWITCH channel state is not queried directly: that would need ESL from Campaigns, which is the #117 dependency this removes. The claim TTL, which is bounded by `originate_timeout`, stands in for "channel exists and is ringing".
- **A3 (finding 3).** Criterion 12 reads: "...then the outcome is corrected to answered or ended...".
- **A4 (finding 4).**
  - **Criterion 30, latency.** No latency benchmark exists in the repo (`scripts/` and `tests/` have none), so the baseline is defined here. Take inbound calls on the same agent and the same FreeSWITCH node on the same day, with at least 20 turns per direction. Compute per-turn latency as `stt+llm+tts` from `transcript_entries.latency_ms`. Pass if outbound p50 ≤ inbound p50 + 50 ms and outbound p95 ≤ inbound p95 + 100 ms.
  - **CSV.** Every `phone_number` is normalised with `libs/telephony_sdk/did_format.py` to E.164. Rows that fail are rejected and reported by line number (the number is not echoed). Duplicates within the file, or against the campaign's existing contacts compared on normalised form, are skipped and counted.
  - **Readiness, agent check.** Readiness blocks on `agent_unavailable` when the agent is missing, deleted, `status <> 'active'`, `direction = 'inbound'`, or belongs to another tenant. All of these return one message ("agent not found or not active"), so it does not reveal whether a foreign agent id exists (lesson 2).
  - **Pause.** Pause stops creation of new attempts, including retries that come due. Attempts already `pending` or `sent` finish normally: a crash-recovered `pending` attempt is still sent once (criterion 16), because it is a commitment made before the pause. Their outcomes are recorded and their slots are freed. Resume re-runs readiness. The UI says: "Pausing stops new calls; calls already ringing or connected will finish."
- **A5 (open question 2, closed by the PRD).** Retry gap is a single campaign-level `retry_gap_minutes`, default 60, minimum 1, set in the UI. It applies equally to `busy`, `no_answer`, `declined` and `failed`.
- **A6 (open question 3).** Webhooks are primary. Provider polling (`get_call_status` / `reconcile_call`) is used only by the reconciler, at deadline or on recovery. A provider that supports neither cannot meet criteria 19 and 20, so readiness blocks it (`provider_not_campaign_capable`). Today that is Cloudonix: its `get_call_status` raises.
- **A7.** The idempotency key is the logical pair `(contact_id, attempt)`. Its wire form is `f"{contact_id}_{attempt}"`. The colon was dropped because the existing Telephony validator (`^[A-Za-z0-9_\-]{1,128}$`, `services/telephony/app.py:167`) rejects `:`, and because the key is embedded unencoded in `?idem=` webhook URLs (`outbound.py`). A UUID contains no `_`, so the encoding is unambiguous.
- **A9 (open question 5, capacity).** A tenant with no outbound cap (`tenants.max_outbound_concurrent_calls IS NULL`) is bounded only by the platform cap. The platform row ships seeded with the product owner's defaults: `total_call_capacity=300`, `inbound_reserve=75` and `max_outbound_concurrent_calls=225`, all counted as concurrent calls. A superadmin can edit them, and the CHECK holds because 225 ≤ 300 − 75. The existing `tenants.max_concurrent_calls` is a live-calls utilisation KPI covering all directions (`schema.sql:1179`). It is not reused, because that would silently change its meaning.
- **A10 (open question 5, owner may override).** A number without `+` is parsed with the campaign's `default_country`. That value is set at campaign create, defaulting to `tenant_campaign_settings.default_country`. If neither is set, the row is rejected with `no_default_country`. No tenant has a default country at deploy time, so legacy DNC rows start as `last10` entries, which is fail-safe (they over-block). They are converted to E.164 when the tenant first saves a default country (criterion 91).
- **A11 (open question 6, owner may override).** Campaigns purges its own rows: contacts, attempts, callbacks, uploads and CRM deliveries. The Conversation side owns `calls` and `transcript_entries` and defines `erase_call_pii()`. Campaigns calls that function only for the `call_session_id`s of its own attempts, during campaign purge and erasure. That is the only call-data deletion. Inbound calls, non-campaign calls and historical transcripts are never touched, and the Conversation service gets no retention job. Platform-wide call and transcript retention is out of scope; a possible future addition is a separate, default-off setting. Exports are streamed and never stored, so there is nothing stored to erase.
- **A12.** Both in-call tools are offered only when `call_direction == 'outbound'`. `record_do_not_call` is likewise outbound-only, because the number comes from the attempt; on inbound, ANI-based DNC is out of scope. A tool call on an outbound session with no matching attempt returns `not_a_campaign_call`.
- **A8.** Criterion 26's "recorded with campaign id and contact id" is met by `campaign_attempts.call_session_id = calls.session_id`. No column is added to `calls`, and the Gateway/proto metadata (`did/ani/direction/freeswitch_host`) is unchanged.
- **A13 (billing integration, owner request 2026-10-21).** Billing P0/P1 is designed but not built. This amendment defines only the seam, so campaigns ship before or after billing; billing internals (ledger, Redis credit, reservation reconciler, rates) are billing's.
  - **Seam.** One interface, `BillingAdmission` (Interfaces, Billing seam). Until billing ships, `app.py` wires `NullBillingAdmission` (`enabled=False`): attempts get `billing_state='off'`, no billing readiness check runs, and behaviour is exactly that of this design without A13. Billing's PR swaps that one constructor; there is no env flag.
  - **Billing key (owner to confirm).** `campaign:<campaign_attempts.id>`, not the dial idempotency key. A billing refusal deletes the unsent attempt row (no attempt consumed), which frees `{contact_id}_{attempt}` for the next claim; the row id is never reused, so a late `reserve` or `release` from an abandoned claim can never touch a later claim's hold. Settlement is idempotent by attempt id.
  - **Readiness.** Block `billing_account_missing` (no billing account; billing refuses outbound for such tenants anyway). Warn `low_balance` when the balance is below the pre-start estimate of remaining spend. Also: block `budget_reached` on start/resume when spend has reached the budget; block `provider_cannot_cap_call` (Path B provider that cannot enforce a time limit); warn `call_length_capped` when the agent's max call duration exceeds the billing hold ceiling; warn `budget_unenforced` when a budget is set while billing is disabled.
  - **Admission.** One hold per attempt, reserved after `claim_next_due` commits and before anything is sent. A refusal deletes the attempt row, restores the contact and writes `campaigns.wait_reason`: `insufficient_credit` (no credit or no account), `tenant_cap` (billing concurrency refusal), `billing_unavailable` (billing error or 2 s timeout; fail closed), `budget_reached` (grant under 30 s within the remaining budget). No attempt is consumed in any of these.
  - **Path A authorization.** The hold becomes an authorization when Campaigns first observes FreeSWITCH's consume of the intent: the `campaign:claim:<key>` FreeSWITCH sets right after `GETDEL`, an `answered` event, or a `calls` row. FreeSWITCH makes no billing call. The Kamailio-to-FreeSWITCH trigger INVITE is never answered (Task 0 criteria 1-2), produces no `calls` row and no attempt outcome, so nothing ever settles for it: it is never billed. Path B authorizes on `mark_sent` with a provider call id.
  - **Settlement.** `answered → ended` settles `billable_ms` (answer to hangup). `busy`, `no_answer`, `declined` and `failed` (deadline failures included) release. Ring time is never charged here. A late-answer upgrade of a released attempt settles it (billing accepts settle after release). A re-delivered event changes nothing: `apply_outcome`'s guard rejects it, `settled` is terminal, and billing settles a key at most once.
  - **Billable seconds.** Path A: FreeSWITCH `billmsec` on the `hangup` event. Path B: only the provider status API's billed duration, from the same `call_status` call that confirms the outcome; webhook fields are never used. When the end is known but no duration is: `calls.ended_at − answered_at`, clamped to the cap. `end_not_observed`: settled at `max_call_s` (owner to confirm; see open item 4).
  - **Concurrency owner (owner to confirm).** Tenant outbound concurrency has one source: `tenants.max_outbound_concurrent_calls`, enforced only by Campaigns' `capacity.admit`. Billing reads that column if it needs the value and does not count `campaign:` reservations against an outbound limit of its own. Campaigns owns it because it already enforces the cap atomically under the admission lock with wait-not-fail semantics (criteria 43-56), rebuilds usage from persisted rows after a crash, and must work with no billing at all. An all-direction tenant limit that billing AC39 owns may still refuse a campaign reserve with `concurrency`; that becomes a `tenant_cap` wait, never a failed attempt.
  - **Mid-call credit: cap, not extend (owner to confirm).** Each call is capped from answer at `max_call_s = min(granted seconds, COALESCE(agents.max_call_duration_s, 7200))`, enforced by FreeSWITCH `sched_hangup` from the intent (Path A) or the provider's `time_limit` (Path B). Trade-off: a call that outruns its grant (at most the 30 min seed ceiling) is cut with no farewell, so the 2 h agent default is unreachable while billing is on. In exchange no billing call is made mid-call, no mid-call hangup path is needed, and a billing outage cannot create unfunded minutes. Extending holds was rejected because a failed extension needs a hangup path Campaigns does not have (no ESL; Path B would need a provider hangup) and leaves unfunded minutes on a billing outage.
  - **Budget.** Optional `campaigns.budget_limit_minor`, in the tenant's billing currency. Admission waits with `budget_reached` while spend plus open holds reaches the budget, and auto-pauses the campaign once settled spend alone reaches it. Each reserve is capped at the remaining budget, so settled spend never exceeds it. Per-attempt cost is on the attempts list, each contact's total on the contact list, and a pre-start estimate in the readiness panel.
  - **Latency.** Nothing is added to the media or per-turn path: FreeSWITCH runs only its existing `GETDEL`, `SET NX` and `XADD` plus one `sched_hangup` channel variable; Gateway and Conversation are unchanged.
  - **PSTN transfers** from a campaign call follow billing P0 (refused there); this design adds nothing for them.
  - **Open, billing-policy-dependent (phase-0 policy not signed off).** (1) Increments and rounding: Campaigns sends raw `billable_ms`; billing rounds. (2) Ring time: never charged here; if policy charges it, settle needs a second duration and `release` becomes a settle for unanswered calls. (3) Voicemail and answering machines: billed as answered, since no answering-machine detection exists. (4) `end_not_observed`: settled at the cap, which can overcharge; the alternative is release, which can undercharge. (5) Whether billing's call-path admission (if any) exempts `direction=outbound` sessions; if not, every campaign call is admitted twice. (6) Rate inputs: the estimate uses the campaign's default country as the destination for every contact. (7) The 30 s minimum grant and the 60 s estimate default are constants here, pending policy.

## Task 0 — Kamailio-originated dial prototype (first task; blocks Path A build, not Path B or schema)
**Question.** Can Kamailio take a signed JSON dial request on a loopback `xhttp` socket, select a FreeSWITCH node, and send a header-only INVITE that the node turns into exactly one callee leg from a consumed intent? It must do this with no media, no billing row, and without the INVITE itself touching the carrier.

**Rig.**
- Local Kamailio with the new `tcp:127.0.0.1:5071` and `tcp:[::1]:5071` sockets, plus two FreeSWITCH `external` profiles in `dispatcher.list`.
- A curl script in the scratchpad (not in the repo) writes an intent to Redis as ACL user `campaigns`, then POSTs the signed JSON.
- The callee is a softphone behind a stand-in "PSTN" target, so the egress route is exercised too.

**How FreeSWITCH answers Kamailio's INVITE. Candidates, tried in order.**
- **T2 (preferred).** FreeSWITCH never answers. `start_campaign_call.lua` consumes the intent, claims the key and fires `bgapi originate`. It then sends a final non-2xx with `respond`, carrying `sip_rh_X-Campaign-Result: accepted|duplicate|rejected`, which `event_route[uac:reply]` only logs.
- **T3.** `200 OK` with `a=inactive`, then an immediate BYE from FreeSWITCH. This is only for when T2 fails, because Kamailio's `uac_req_send` would have to ACK a 2xx.
- **T1.** `183` without SDP, then a final non-2xx.

**Decision criteria. All must hold; take the first candidate that passes.**
1. No RTP port is allocated for the INVITE's channel: `fs_cli show channels` and the RTP port count are the same before and after.
2. No `kamailio_cdr` (acc) row is written for the INVITE, and no `calls` row or Gateway session exists for it.
3. The callee leg from `bgapi originate` survives the INVITE channel being torn down, and the AI conversation completes.
4. `tm` sends the ACK for a non-2xx final reply to a `uac_req_send` INVITE, `event_route[uac:reply]` fires, and no transaction leaks: `kamcmd tm.stats` matches before and after 100 dials.
5. A second POST with the same key produces no second dial, because the intent was already consumed.
6. The handler replies within 200 ms. It answers once `uac_req_send` returns and does not wait for FreeSWITCH.
7. Kamailio's INVITE never reaches the carrier. Only the FreeSWITCH callee leg reaches the PSTN target, and only after digest authentication as `fs-egress`.
8. An INVITE sent straight to FreeSWITCH :5080 with Kamailio's address spoofed as the source, carrying a valid-looking key that has no intent in Redis, dials nothing.

**Also verified in Task 0.**
- `ds_select_dst("1","4")` inside `event_route[xhttp:request]` sets `$du`, and that value is usable as `$uac_req(ouri)`.
- The `jansson`, `crypto` and `uac` modules load. `crypto_hmac_sha256` matches Python's HMAC for the same body.
- The canonical-JSON equality check (see Kamailio) rejects an extra key, a wrong type and a reordered body.
- A SIP request arriving on the dial socket is dropped by `request_route`, and `/RPC` and every jsonrpc method get 404 or 405 there.
- `mod_hiredis` (`/opt/src`) can authenticate as the named ACL user `freeswitch` (`AUTH <user> <pass>`). If it only supports a password, the profile connect is patched in `/opt/src` and the patch is recorded.
- From Lua, `GETDEL campaign:intent:*`, `SET NX EX campaign:claim:*` and `XADD campaign:events:fs` work. `SET campaign:intent:*`, `GETDEL campaign:claim:*` and any other command or key return NOPERM (Redis 7 ACL selectors, `redis:7-alpine`).
- The sofia gateway `kamailio_egress` on the dedicated `campaign_egress` profile passes Kamailio's `auth_check`, and its INVITE carries `X-Egress-Token` from the originate variable.
- A transfer leg cannot use `fs-egress`. Run a warm and a cold transfer on a live campaign call to a PSTN-format destination. The transfer goes out over `external` and its existing path, and Kamailio's `route(PSTN_EGRESS)` never admits it. Also: `originate sofia/gateway/kamailio_egress/+1…` from `fs_cli` with no token gets 403, and a reused, expired or other-callee token gets 403.
- `${acl(${network_addr} kamailio)}` works in a dialplan condition.

**Output.** Append a "Prototype result" section to this file, recording the chosen candidate, the exact response code and the module versions. If criteria 1–4 cannot all hold, or criterion 8 fails, Path A stops and goes back to the owner; Path B and the schema proceed.

## Changes
| File | Change | Why |
|---|---|---|
| `database/schema.sql` | Add `campaign_attempts`, `retry_gap_minutes`, `next_attempt_at`, `send_count`, the contact status set and the legacy backfill (see Data). Billing-seam columns, `budget_limit_minor`, `spent_minor` and three wait reasons (A13). | Persisted attempt state (criteria 7, 15) and retry gap (criterion 13). |
| `services/campaigns/attempts.py` (new) | Attempt creation inside the claim transaction. All state and outcome transitions are single conditional `UPDATE … RETURNING` (lesson 8). Includes in-flight count, reconcile queries and `idempotency_key()`. Billing (A13): `unclaim()`, the `billing_state` transitions of the Billing seam, and `apply_outcome` sets `billing_state` in its own UPDATE. | One owner for every attempt write; no check-then-act. |
| `services/campaigns/worker.py` | Delete `_in_flight`, `_job_to_contact`, `_pending_idem`, `EslJobEventListener`, `_idempotency_key`. The tick runs `_drain_events()`, `_reconcile()` and then a per-campaign `_dial_next()`. Dispatch is three-way on `resolve_outbound_route`: refuse if `not caller_id_owned or not agent_ok`; Path B if `provider in TelephonyProviderRegistry.all()`; Path A only if `provider is None`; any other provider value is refused. Completion requires every contact terminal and `in_flight_count == 0`. Billing (A13): after `Claimed`, `_dial_next` calls `billing.reserve` before any send and `attempts.unclaim` on refusal; the tick runs `_billing_sync()` after `_reconcile()`; completion also requires no attempt in an open `billing_state`. | Removes the state behind #116 and the ESL dependency behind #117. Fixes today's `else → ESL` fall-through for an unregistered provider (lesson 39). |
| `services/campaigns/dial_request.py` (new) | `DialRequestClient`: writes and checks the dial intent in Redis, then POSTs canonical signed JSON to Kamailio's dial handler over `httpx` (already a dependency) with a 3 s timeout. Before anything is sent, every field is validated: `callee` and `caller_id` must be E.164 (`^\+[1-9][0-9]{6,14}$`), ids must be UUIDs, the key must match its pattern, and no field may contain CR, LF or NUL. `probe()` serves readiness. `DialIntent` gains `max_call_s` (A13). | Path A send; replaces the SIP client by owner decision. |
| `services/campaigns/originate.py` | Delete. | ESL removed (#117). Before deleting, grep `scripts/`, `docs/`, `admin-ui/` and `services/` for `originate.originate_call`, `EslJobEventListener`, `vobiz-call-resolved` and the `FREESWITCH_ESL_*` uses that belong to Campaigns (lesson 17). The Gateway's own ESL env vars stay. |
| `services/campaigns/tests/test_originate.py` | Delete, and replace it with `test_dial_request.py`. Carry over the CR/LF/NUL and dial-number cases, now applied to the intent and body fields (lesson 42). Add HMAC and canonical-JSON vectors shared with the Kamailio test. | Keeps the injection control tested. |
| `services/campaigns/outcome_events.py` (new) | For a `source=cpaas` `hangup` event, the consumer never applies the webhook's `status`. It calls `telephony_originate.call_status(provider, tenant_slug, provider_call_id)` and applies only the provider API's terminal status. The call id is the one stored on the attempt when available, otherwise the event's hint, accepted only if the API-reported `to` and `from` equal the attempt's `callee` and `caller_id`. A status of `live` → ignored (a later event or the deadline decides). `unknown` or an API error → the event is acked and the attempt is left to the deadline rule (criterion 11). Then: one `XREADGROUP` consumer over `campaign:events:fs`, `campaign:events:cpaas` and `campaign:events:conv`, plus `XAUTOCLAIM` for entries left idle more than 60 s by a dead consumer. Maps each event to `attempts.apply_outcome`, then `XACK` after commit. Unknown keys are acked and dropped, logged at DEBUG with no PII. The source is taken from the stream name, never the event's `source` field: only `:fs` yields `source=fs`, only `:cpaas` yields `source=cpaas`, only `:conv` yields `dnc_request`; any other kind on a stream is acked and dropped with a WARNING (security HIGH 1, R4). In each batch `:conv` entries are applied before the other two (keeps DNC-before-hangup). Billing (A13): passes `billable_ms`/`billing_source` to `apply_outcome`, from the event only for entries read from `campaign:events:fs`, and only from `call_status` for `source=cpaas`. | Durable ingestion that does not depend on the worker being up (criterion 22). |
| `libs/telephony_sdk/campaign_events.py` (new) | `STREAM_FS`, `STREAM_CPAAS`, `STREAM_CONV`, `GROUP`, `encode()` and `decode()` for the event fields, including `billable_ms` (A13). | The single schema shared by the Telephony producer and the Campaigns consumer (lesson 32). The Lua producer is pinned to it by a test. |
| `services/campaigns/readiness.py` (new) | `check()` builds the list of blocks and warnings (see Interfaces), including the billing checks and `CostEstimate` (A13). | Readiness (criteria 1–5, A4). |
| `services/campaigns/campaign_contacts.py` | `parse_contacts_csv` normalises and validates. `bulk_insert_contacts` dedupes under a campaign-row `FOR UPDATE`. `claim_next_pending` becomes `claim_next_due`, which also sets `billing_state` and `budget_cap_minor` (A13). | CSV validation and dedupe (A4); concurrency cap enforced in Postgres (criterion 8). |
| `services/campaigns/campaigns.py` | `resolve_outbound_route` also returns `telephony_config_id` (`tc.id`), which is the Path B CPS scope. `get_progress` returns `wait_reason`, `wait_since`, `waiting_count`, `tenant_cap` and `effective_limit`. `resolve_outbound_route` adds `agent_ok` (`a.tenant_id = t.id AND a.status='active' AND a.deleted_at IS NULL`) as a separate field (lesson 39), plus `did_agent_id` (`pn.agent_id`). `get_progress` counts from attempts and adds `spent_minor`, `budget_limit_minor` and `billing_currency` (A13). New `get_campaign_for_caller(campaign_id, tenant_id: UUID \| None)` with an explicit `tenant_id = $2` predicate (lesson 36). | Today the query does `JOIN agents a ON a.id = $2` with no tenant check (a cross-tenant agent). Lesson 16: start-time grants are re-validated on every attempt. |
| `services/campaigns/schemas.py` | `retry_gap_minutes: int = 60` on Create/Update. Validators: `max_attempts >= 1`, `retry_gap_minutes >= 1`, `max_concurrent_calls >= 1`, and the timezone must load in `ZoneInfo`. `budget_limit_minor: int | None` (≥ 1) on Create/Update (A13). | Criterion 6. |
| `services/campaigns/routers/campaigns.py` | Add `GET …/readiness` and `GET …/attempts` (attempts carry cost fields, A13). The contact list adds each contact's total `cost_minor`. Start and resume run readiness and return 409 when blocked. Every `{campaign_id}` route switches from `get_or_404` + `assert_tenant_access` to `get_campaign_for_caller`, which returns the same 404 as a nonexistent id. Today a foreign id gets 403 from `assert_tenant_access` (`deps.py:111`) while a missing id gets 404. | Criteria 5, 41, 42; matches the existing `require_role`/`get_current_user` split. |
| `services/campaigns/app.py` | Remove `/internal/vobiz-call-resolved` and `VobizCallResolved`. The lifespan opens and closes the `DialRequestClient` (its `httpx.AsyncClient`), the Redis client and the `BillingAdmission` (`NullBillingAdmission` until billing ships; billing's PR swaps this one constructor), and `stop()` awaits all three (lesson 26). | Replaced by the stream. |
| `services/campaigns/telephony_originate.py` | Key format per A7. Handle 429 before the generic 4xx branch and raise `TelephonyRateLimited(retry_after)`. Add `reconcile_call()`, `call_status()` and `outbound_readiness()` clients. Stop logging `phone_number` at INFO. Billing (A13): `place_call` sends `time_limit_s`; `call_status()` returns `billable_ms`. | Criteria 19, 20, 39; PII constraint. |
| `services/telephony/app.py` | Replace the HTTP push in `status_callback`'s `_notify_campaigns_best_effort` with `XADD campaign:events:cpaas` of a `hangup` event. Before that XADD, after signature verification, the nonce must pass `SET telephony:webhook_nonce:<provider>:<account_ref>:<nonce> 1 NX EX 86400`. A nonce already seen → 403 with no XADD (replay, security MEDIUM 2). `tenant_id` comes from the signature-verified `account`. When the provider's signature does not cover the event fields (`signature_covers_fields` is False, as for Vobiz, which signs only base URL plus nonce), the event is marked `unconfirmed=1`: `idem`, `event`, `status` and the provider call id are then untrusted hints. `key` comes from `idem`. `on_session_start` emits an `answered` event for outbound sessions, carrying `call_session_id`. Add `POST /{provider}/call/reconcile`, `GET /{provider}/call/{call_id}/status` and `GET /{provider}/outbound-readiness`. `place_call` maps `TelephonyRateLimited` to 429 with `retry_after`. Billing (A13): `POST /{provider}/call` accepts `time_limit_s`; the status route returns `billable_ms`. | Durable Path B outcomes (criteria 22, 37, 38, 40); recovery reads (criteria 19, 20). |
| `services/telephony/outbound.py` | On `TelephonyRateLimited`, call `idempotency.release()` and return 429. Add `reconcile(...)` and `call_status(...)`, wrapping `account.instance.reconcile_call` and `get_call_status`. `call_status` maps the provider's billed duration to `billable_ms`; `place_call` passes `time_limit_s` to `initiate_call` (A13). | Criterion 39: a rate-limited key must be reusable because no call was created. |
| `services/telephony/idempotency.py` | Add `release(provider, tenant_id, key)` (DEL on the claim key). | See above. |
| `libs/telephony_sdk/exceptions.py` | Add `TelephonyRateLimited(TelephonyProviderError)` with `retry_after: float \| None`. | Lets a 429 be told apart from a real failure. |
| `libs/telephony_sdk/interface.py` | Add `supports_campaign_outbound: ClassVar[bool] = False` and `signature_covers_fields: ClassVar[bool] = False`. The second becomes True only for a provider whose webhook signature covers the query and body (idem, event, status, call id), verified per provider. Billing (A13): `initiate_call` gains `time_limit_s: int | None = None`, and `supports_time_limit: ClassVar[bool] = False` is added. | Readiness gate (A6); webhook trust (security MEDIUM 2). |
| `libs/telephony_sdk/providers/vobiz.py`, `fake.py` | Set `supports_campaign_outbound = True`. Vobiz maps HTTP 429, and its documented concurrency-limit error, to `TelephonyRateLimited`. Billing (A13): send `time_limit` and map the status API's billed duration to `billable_ms`; set `supports_time_limit = True` only after the field names (`time_limit`, `bill_duration`) are verified against Vobiz's API docs. | Criterion 39. |
| `scripts/kamailio/kamailio.cfg.tpl` | See the Kamailio section. In summary:<br>- Add `listen=tcp:127.0.0.1:5071` and `listen=tcp:[::1]:5071`. Load `jansson.so`, `crypto.so` and `uac.so`. Add `#!substdef` for `CAMPAIGNS_DIAL_SRC`, `CAMPAIGNS_DIAL_HMAC` and `PSTN_GW_URI`.<br>- A dial-only branch goes first in the existing `event_route[xhttp:request]` (line ~1366), ahead of the jsonrpc `/RPC` branch. `request_route` drops SIP that arrives on the dial socket.<br>- Add `event_route[uac:reply]`, `route[PSTN_EGRESS]` (digest user `fs-egress` only) and a header strip on every inbound request.<br>- Corrected references: the live FreeSWITCH-origin test is line 673, the 500x loop guard. 500x itself is hard-wired to `__LAN_IP__:5080` at line 682 and stays single-node and unchanged. Line 694 is the 100x `FS_LOOP` block. Line 794 sits in a commented-out `/* … */` block and is untouched. The existing `route[PSTN]` (line 1303) stays compiled out, because `WITH_PSTN` is undefined. | Criteria 25, 28, 31–35, 102–104; security HIGH 1, 2. |
| `scripts/update_kamailio_ip.sh` | Render the following, writing the output with mode `0600` because it now holds a secret:<br>- `__CAMPAIGNS_DIAL_SRC__` (default `127.0.0.1 ::1`)<br>- `__CAMPAIGNS_DIAL_HMAC__` (from `CAMPAIGNS_KAMAILIO_HMAC_SECRET`)<br>- `__CAMPAIGNS_EGRESS_HMAC__` (from `CAMPAIGNS_EGRESS_HMAC_SECRET`, a separate secret)<br>- `__PSTN_GW_URI__`<br>An empty source or secret is rendered empty, so the handler fails closed (criterion 35), and an empty `PSTN_GW_URI` makes egress return 503. The script also provisions the `fs-egress` subscriber idempotently (`kamctl add` / `kamctl passwd` with `KAMAILIO_FS_EGRESS_PASSWORD`). | Single renderer; egress credential. |
| `scripts/freeswitch/000_campaign_trigger.xml` (new) | In `public`; the file sorts before `00_voice_ai.xml`.<br>- Extension 1 (`campaign_header_guard`): if `${sip_h_X-Call-Direction}` is non-empty and `${acl(${network_addr} kamailio)}` is `false`, `respond 403`.<br>- Extension 2: `^outbound-trigger$` with `sip_h_X-Call-Direction=outbound` and the acl `true` runs `start_campaign_call.lua`.<br>The ACL is defence in depth, because a spoofed source passes it. The real control is the intent consume in the Lua. | Criterion 34. |
| `scripts/freeswitch/start_campaign_call.lua` (new) | Steps:<br>1. Read only `X-Idempotency-Key` and check it against `^[0-9a-f-]{36}_[0-9]{1,3}$`.<br>2. `GETDEL campaign:intent:<key>` as ACL user `freeswitch`. A nil result, unparsable JSON, a key mismatch, a past `exp`, a missing `max_call_s`, an `egress_token` whose key, exp, tenant_id, caller_id or max_call_s fields differ from the intent's, or a Redis error gives `respond 403` (`rejected`) and nothing is dialed (fail closed).<br>3. Re-check every intent field with anchored patterns.<br>4. `SET campaign:claim:<key> <intent.call_uuid>\|<fs_host> NX EX 105`. If it is not set, reply `duplicate`.<br>5. `bgapi originate {origination_uuid=<intent.call_uuid>,origination_caller_id_number=<intent.caller_id>,originate_timeout=45,campaign_key=<key>,campaign_tenant_id=<intent.tenant_id>,campaign_agent_did=<intent.agent_did>,campaign_callee=<intent.callee>}sofia/gateway/kamailio_egress/<intent.callee> &lua(start_voice_ai.lua)`, with `sip_h_X-Egress-Token=<intent.egress_token>` and `sip_cid_type=pid` (caller ID sent as `P-Asserted-Identity`, which Kamailio checks against the token) added to the originate variables. `intent.max_call_s` is mandatory on every intent, billing on or off, and must match `^[1-9][0-9]{1,3}$` (else `rejected`); it adds `execute_on_answer='sched_hangup +<intent.max_call_s> ALLOTTED_TIMEOUT'`; no other billing logic. No header value reaches originate.<br>6. Reply per the Task 0 result. | Security HIGH 1; criteria 25, 27. |
| `scripts/freeswitch/campaign_event.lua` (new) | Registered as a global event hook in `lua.conf.xml` for CHANNEL_ANSWER and CHANNEL_HANGUP_COMPLETE, not as per-channel `api_on_answer`/`api_hangup_hook`. `XADD campaign:events:fs MAXLEN ~ 100000` with `source=fs`, `kind`, `key`, `tenant_id` (from channel variable `campaign_tenant_id`, which comes from the consumed intent), `call_session_id`, `cause` (`hangup_cause`) and `answered` (`answer_epoch > 0`), plus `billable_ms` from channel variable `billmsec` on `hangup` (A13). | Durable Path A outcomes carrying the tenant (security HIGH 3). |
| `scripts/freeswitch/start_voice_ai.lua` | If `campaign_key` is set, the metadata becomes `{"did":<campaign_agent_did>,"ani":<campaign_callee>,"direction":"outbound","freeswitch_host":…}`, run through the existing `sip_token` filter. Otherwise the metadata is unchanged. | Calls are recorded as outbound and `ani` is the remote party (lesson 44). The Gateway parses `direction` already (`Config.cpp:346`); no Gateway change. |
| `scripts/freeswitch/setup_macos.sh` | Install the two Lua files, the dialplan file and the egress gateway. Enable `mod_hiredis` with profile `default` pointing at Redis and authenticating as ACL user `freeswitch` (`REDIS_FREESWITCH_PASSWORD`). Add acl list `kamailio` containing the Kamailio LAN IP. | Wiring for FreeSWITCH. |
| `scripts/freeswitch/kamailio_egress.xml` (new) | A dedicated sofia profile `campaign_egress` (SIP port 5090 on the node's LAN IP, `inbound` context `campaign_egress_refuse` that answers `respond 403` to anything inbound) that holds the only gateway `kamailio_egress`: proxy = Kamailio LAN IP:5060, `register=false`, `username=fs-egress`, `password=$${kamailio_fs_egress_password}`, realm = the Kamailio domain. No other profile defines this gateway. Transfers keep using `external` and its existing dial strings, so they cannot select `sofia/gateway/kamailio_egress`. | Digest-authenticated PSTN egress for the campaign callee leg only (security HIGH 2, MEDIUM 1). |
| `services/campaigns/capacity.py` (new) | `admit()` runs inside `claim_next_due`, plus limit reads and writes and the save-time validation. Billing (A13): the `budget_reached` check and auto-pause. | The single enforcement point for criteria 44–52 and 55. |
| `services/campaigns/billing.py` (new) | `BillingAdmission` protocol, its result types, `billing_key()`, `NullBillingAdmission` and `estimate_campaign()` (see Billing seam). | The single seam to billing (A13), so campaigns ship before or after billing. |
| `services/campaigns/tests/test_billing_contract.py` (new) | The seam contract, parametrised over every `BillingAdmission` implementation (a ledger-backed fake today; billing's client when it ships). | Billing's PR inherits one executable checklist instead of prose. |
| `services/campaigns/routers/capacity.py` (new) | Platform-limit, CPS-limit and tenant-cap routes (see HTTP). Included in `app.py`. | Settings API for criteria 43, 52 and 54. |
| `admin-ui/app/(console)/tenants/page.tsx` | Superadmin only. Adds a "Campaign compliance" card: platform default retention, and number series per purpose (none pre-filled). Adds a "Platform outbound capacity" card (total capacity, inbound reserve, outbound cap, CPS table with one row for the SIP trunk and one per provider account, and current usage for each) and a per-tenant "Outbound concurrency cap" field. A 400 from the save check is shown inline. | Superadmin UX for criteria 43, 48 and 52. |
| `services/config/calls.py` | `:35` label: `inbound→"AI"`, `outbound→"Outbound"`, `test→"WebRTC"`. | Outbound calls would otherwise be labelled "WebRTC". |
| `admin-ui/app/(console)/campaigns/new/page.tsx` | Purpose select (`promotional`/`service`/`transactional`, required) and a default-country select. Add a retry gap field (min 1, default 60) and enforce `max_attempts ≥ 1`. The caller-ID select lists tenant-owned DIDs, each with a path badge (Our SIP / provider name). Optional budget field, in the billing currency (A13). | Configuration UX. |
| `admin-ui/app/(console)/campaigns/[id]/page.tsx` | Upload dialog requires the consent checkbox and shows the per-row rejection list. Contact list shows masked numbers, the disposition column and filters (disposition, outcome), and the pending callback time. Adds an Export CSV button. Every ended campaign shows its purge date, with a warning banner within 14 days that links to Export and CRM settings. Shows the campaign cap, the tenant cap (read-only, labelled "set by platform admin"), the effective limit, and the live wait reason with its waiting count (criteria 46, 53). Readiness panel: blocks in red, warnings in amber, each with a link to its field. Start/Resume are disabled while any block exists, and a 409 also renders the panel. The contact list shows the last outcome, and expanding a contact lists its attempts. Show the pause note from A4. Readiness, progress and attempts are fetched independently, not in one `Promise.all` (lesson 21). Billing (A13): pre-start estimate in the readiness panel, spend against budget, per-attempt and per-contact cost, and labels for `insufficient_credit`, `budget_reached` and `billing_unavailable`. | Criteria 1–5, scope bullet on per-attempt outcomes. |
| root `.env.example` | Add:<br>- `KAMAILIO_DIAL_URL=http://127.0.0.1:5071/campaign-dial`<br>- `CAMPAIGNS_KAMAILIO_HMAC_SECRET`<br>- `CAMPAIGNS_EGRESS_HMAC_SECRET`<br>- `PSTN_GW_URI`<br>- `KAMAILIO_FS_EGRESS_PASSWORD`<br>- one `REDIS_<CLIENT>_PASSWORD` per ACL user<br>Each service's `REDIS_URL` becomes `redis://<its user>:<password>@127.0.0.1:6379/0`. Remove Campaigns' `FREESWITCH_ESL_*` mention if it is Campaigns-only. | `tests/test_env_catalogue.py` enforces the catalogue. |
| `deployment/docker/docker-compose.yml` | Run redis with `--appendonly yes --aclfile /etc/redis/users.acl --protected-mode yes`, mounting the rendered ACL file. The port stays bound to `127.0.0.1`. | Un-acked stream entries survive a restart; Redis requires auth (security HIGH 3). |
| `deployment/docker/redis/users.acl.tpl` (new) | Per-client ACL users (see Redis ACL), rendered by the launcher from `REDIS_<CLIENT>_PASSWORD` as `#<sha256>` hashes, with `user default off`. | Least privilege per client. |
| `tests/test_local_binds.py` | Assert that the Kamailio dial `listen` lines are exactly `tcp:127.0.0.1:5071` and `tcp:[::1]:5071`, never `0.0.0.0` or `__LAN_IP__`. Assert that the compose redis port binds `127.0.0.1` and passes `--aclfile`. | Criterion 31; Redis bind. |
| `services/campaigns/callbacks.py` (new) | `propose()` parses, cross-checks, clamps and moves out-of-hours times to the next allowed slot, then stores a proposal in Redis. `confirm()` stores the callback (replace-pending, cap of 3). `check_stated_time()` is the digit and relative-term cross-check. | Criteria 61–69. |
| `services/campaigns/routers/internal.py` (new) | `POST /internal/calls/{session_id}/callback` and `POST /internal/calls/{session_id}/do-not-call`. Service-account only. | The only entry points for the in-call tools; contact and tenant come from server-side metadata (criterion 62, lesson 44). |
| `services/conversation/tools/campaign_tools.py` (new) | `SCHEDULE_CALLBACK` and `RECORD_DO_NOT_CALL` `ToolDefinition`s, plus handler factories that POST to Campaigns with the Conversation service-account JWT. Timeout 2000 ms; no retry for the callback; one retry for the idempotent DNC call. On DNC failure, the handler `XADD`s a `dnc_request` event, then returns `FAILED`. | Tool plumbing that follows `SEARCH_KNOWLEDGE`'s local-tool pattern (`tools/registry.py:10`, `pipeline.py:1398`). |
| `services/conversation/pipeline.py` | `_local_tools(history, store, session_id)` adds both tools when the session direction is `outbound`. Their handlers are closures built there, capturing the server-side `self._tenant_slug`/tenant id and `session_id` (the same way `_make_knowledge_handler(session_id)` does), so the handler still has the orchestrator's `handler(arguments)` signature (`orchestrator.py:446-447`). No other per-turn change. | Criteria 61, 62, 84. |
| `services/conversation/tools/orchestrator.py` | Add a module-level `CURRENT_TURN_ID: ContextVar[str \| None]`. The run sets it right after `turn_id = str(uuid.uuid4())` (line 137) and resets it with the token in `finally`. Local handlers run in the same task, or in an `asyncio.wait_for` child task that copies the context, so they read it. No signature changes. | Exposes the server-side per-caller-turn id to the callback handler (criterion 63). |
| `services/conversation/__main__.py` | Construct the Campaigns HTTP client from `CAMPAIGNS_SERVICE_URL`, closed on shutdown (lesson 26). | Wiring for the tools. |
| `services/campaigns/crm_delivery.py` (new) | `DeliveryWorker`: lease-based claim (`FOR UPDATE SKIP LOCKED`, `leased_until`), build the payload at send time, sign it, send through the guarded client, and run the backoff schedule. Started and stopped in `app.py`'s lifespan. | Criteria 75–83. |
| `services/campaigns/retention.py` (new) | `purge_due_campaigns()`: daily, elected by advisory lock, batched and idempotent. `erase_number(tenant_id, e164, actor)` handles erasure. | Criteria 93–98. |
| `services/campaigns/exports.py` (new) | Streamed CSV of contacts with a formula-injection guard, writing an audit row. | Criteria 60, 86, 101. |
| `services/campaigns/dnd_registry.py` (new) | `IDndRegistry` protocol and `AllowAllRegistry` (the default). | Criterion 92, interface only. |
| `services/campaigns/routers/settings.py` (new) | Tenant campaign settings (CRM URL and secret, retention, default country), platform campaign settings (default retention, number series), erasure, uploads read, audit read. | Criteria 75, 85, 88, 93, 97, 101. |
| `services/campaigns/dnc.py` | `normalize_phone` (last 10 digits) is replaced by `did_format.to_e164`. `is_blocked(tenant, e164)` matches `e164` rows exactly OR active `last10` rows (`superseded_at IS NULL`) on the last-10 suffix. `add_number` normalises, uses `ON CONFLICT DO NOTHING`, and in the same transaction cancels pending callbacks and pending retries across the tenant's campaigns (sets them `blocked`), finding contacts through `campaign_contacts.match_number`. `convert_legacy(tenant_id, country)` runs when the tenant saves a default country. It is additive: for each active `last10` row it INSERTs an `e164` row with `legacy_of` set (`ON CONFLICT DO NOTHING`), and the legacy row keeps matching. `conversion_preview(tenant_id) -> {retire_count, newly_dialable_count}` is exposed as `GET /tenants/{tenant_id}/dnc/conversion-preview` (admin; viewer 403; tenant 404 rule).
  - `retire_count` is the number of active `last10` rows that have an `e164` child.
  - `newly_dialable_count` is the number of this tenant's non-purged contacts that a retiring row blocks today by suffix but that would be unblocked after retirement: their `to_e164` value differs from every remaining DNC row, which is how the foreign numbers are counted.

  `confirm_legacy_conversion(tenant_id, actor, expected_retire_count)` is exposed as `POST /tenants/{tenant_id}/dnc/confirm-conversion` with body `{"expected_retire_count": n}`, admin-only.
  - In one transaction, under the advisory lock `hashtext('dnc:'||tenant_id)`, it recomputes `retire_count`. If that differs from `expected_retire_count`, it returns 409 with the current preview and changes nothing.
  - Otherwise it sets `superseded_at` only on legacy rows that have an `e164` child, and audits `dnc_conversion_confirmed` with both counts. Unconvertible rows keep blocking by suffix forever. The DNC list API masks numbers. | Criteria 70–73, 90, 91. Every caller is moved in the same change: `dnc.py:64-65`, `routers/campaigns.py:125-128` (lessons 11, 17). |
| `libs/telephony_sdk/did_format.py` | Add `to_e164(raw, default_country) -> str \| None` (using `phonenumbers`, `is_valid_number`) and `mask_number(e164) -> str` (`+91******3210`: country code plus the last 4 digits). | One normaliser and one masker for every caller. |
| `libs/egress_guard.py` | Add `validate_webhook_url(url) -> WebhookTarget(origin, path_query)`: https only, no userinfo, path and query allowed, same literal-IP and host checks as `validate_base_url`. Thread `use_operator_allowlist: bool = True` through `is_allowed`, `validate_base_url`, `resolve_validated`, `_GuardedBackend` and `guarded_client`. Defaults are unchanged, so existing callers behave identically. CRM validation and sending pass `False`, so `KNOWLEDGE_PROVIDER_ALLOWED_HOSTS` (which includes local Ollama) can never whitelist a tenant URL. | Criterion 76. Reuses the pinned-IP connect that already exists. |
| `libs/telephony_sdk/campaign_events.py` | Add the `kind="dnc_request"` event (`session_id` only). | DNC fallback, criterion 74. |
| `requirements.txt` | Add `phonenumbers`. | E.164 validity per country (criterion 89); see Risks. |
| `admin-ui/app/(console)/settings/page.tsx` | Tenant admin "Campaigns" section: CRM URL and secret (secret write-only, shown as "set"), retention (max = platform default), default country, "Erase a number", audit log (admin only). | Criteria 75, 93, 97, 101. |

## Data
```sql
-- campaigns: retry gap and attempts floor
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS retry_gap_minutes INT NOT NULL DEFAULT 60;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'campaigns_retry_gap_check') THEN
    ALTER TABLE campaigns ADD CONSTRAINT campaigns_retry_gap_check CHECK (retry_gap_minutes >= 1);
  END IF;
  -- NOT VALID: legacy rows are not re-checked; new writes are.
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'campaigns_max_attempts_check') THEN
    ALTER TABLE campaigns ADD CONSTRAINT campaigns_max_attempts_check CHECK (max_attempts >= 1) NOT VALID;
  END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS campaigns_id_tenant_key ON campaigns(id, tenant_id);

-- campaign_contacts: retry scheduling and outcome statuses
ALTER TABLE campaign_contacts ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
DO $$ BEGIN
  -- Drop and re-add in one block (lesson 13). The new set is a superset of the old one, so no existing row can violate it.
  ALTER TABLE campaign_contacts DROP CONSTRAINT IF EXISTS campaign_contacts_status_check;
  ALTER TABLE campaign_contacts ADD CONSTRAINT campaign_contacts_status_check CHECK (status IN
    ('pending','calling','completed','failed','no_answer','busy','declined','blocked'));
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS campaign_contacts_id_campaign_key ON campaign_contacts(id, campaign_id);
DROP INDEX IF EXISTS idx_campaign_contacts_pending;
CREATE INDEX IF NOT EXISTS idx_campaign_contacts_due
    ON campaign_contacts(campaign_id, next_attempt_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS campaign_attempts (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id),
    campaign_id        UUID NOT NULL,
    contact_id         UUID NOT NULL,
    attempt            INT  NOT NULL CHECK (attempt >= 1),
    -- f"{contact_id}_{attempt}". Contact ids are globally unique UUIDs, so a global UNIQUE cannot collide across tenants (lesson 3).
    idempotency_key    TEXT NOT NULL UNIQUE,
    path               TEXT NOT NULL CHECK (path IN ('sip', 'cpaas')),
    provider           TEXT,
    callee             TEXT,            -- E.164 snapshot for provider recovery; NULL after purge/erasure
    caller_id          TEXT,
    state              TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'sent', 'resolved')),
    outcome            TEXT CHECK (outcome IS NULL OR outcome IN
                         ('answered', 'ended', 'busy', 'no_answer', 'declined', 'failed')),
    closed_by_deadline BOOLEAN NOT NULL DEFAULT false,
    failure_reason     TEXT,
    call_session_id    TEXT,            -- path A: pre-generated FS uuid (= calls.session_id); path B: from the answered event
    provider_call_id   TEXT,
    send_count         INT  NOT NULL DEFAULT 0,
    not_before         TIMESTAMPTZ,     -- path B rate-limit backoff
    deadline_at        TIMESTAMPTZ NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at            TIMESTAMPTZ,
    answered_at        TIMESTAMPTZ,
    resolved_at        TIMESTAMPTZ,
    ended_at           TIMESTAMPTZ,
    UNIQUE (contact_id, attempt),
    FOREIGN KEY (campaign_id, tenant_id) REFERENCES campaigns(id, tenant_id),       -- lesson 48
    FOREIGN KEY (contact_id, campaign_id) REFERENCES campaign_contacts(id, campaign_id),
    CHECK ((state = 'resolved') = (outcome IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_campaign_attempts_open
    ON campaign_attempts(campaign_id) WHERE state <> 'resolved' OR outcome = 'answered';
CREATE INDEX IF NOT EXISTS idx_campaign_attempts_open_tenant
    ON campaign_attempts(tenant_id) WHERE state <> 'resolved' OR outcome = 'answered';

-- ── Capacity limits (criteria 43–56) ──
-- CPS scope of each dial start. 'sip_trunk:pstn' is Kamailio's single route(PSTN) gateway;
-- 'provider_account:<telephony_configs.id>' is the CPaaS account.
ALTER TABLE campaign_attempts ADD COLUMN IF NOT EXISTS cps_scope    TEXT NOT NULL DEFAULT 'sip_trunk:pstn';
ALTER TABLE campaign_attempts ADD COLUMN IF NOT EXISTS last_send_at TIMESTAMPTZ;   -- set at every reserved send
CREATE INDEX IF NOT EXISTS idx_campaign_attempts_cps ON campaign_attempts(cps_scope, last_send_at);

-- Outbound-only tenant cap; superadmin-set. NULL = only the platform cap applies (A9).
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS max_outbound_concurrent_calls INT;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tenants_max_outbound_concurrent_calls_check') THEN
    ALTER TABLE tenants ADD CONSTRAINT tenants_max_outbound_concurrent_calls_check
      CHECK (max_outbound_concurrent_calls IS NULL OR max_outbound_concurrent_calls >= 1);
  END IF;
END $$;

-- Platform singleton (no tenant_id: platform data, read via platform_conn).
CREATE TABLE IF NOT EXISTS platform_outbound_limits (
    singleton                     BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    total_call_capacity           INT CHECK (total_call_capacity IS NULL OR total_call_capacity >= 1),
    inbound_reserve               INT NOT NULL DEFAULT 0 CHECK (inbound_reserve >= 0),
    max_outbound_concurrent_calls INT CHECK (max_outbound_concurrent_calls IS NULL OR max_outbound_concurrent_calls >= 1),
    -- Save check for criterion 48: an outbound cap must fit within capacity minus the reserve.
    CONSTRAINT platform_outbound_cap_fits CHECK (max_outbound_concurrent_calls IS NULL OR
        (total_call_capacity IS NOT NULL AND max_outbound_concurrent_calls <= total_call_capacity - inbound_reserve)),
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by                    UUID
);
INSERT INTO platform_outbound_limits (singleton, total_call_capacity, inbound_reserve, max_outbound_concurrent_calls)
    VALUES (true, 300, 75, 225) ON CONFLICT DO NOTHING;   -- owner defaults; superadmin-editable

-- Platform-wide CPS per scope; no row = no CPS limit for that scope.
CREATE TABLE IF NOT EXISTS outbound_cps_limits (
    scope      TEXT PRIMARY KEY CHECK (scope ~ '^(sip_trunk:[a-z0-9_-]{1,40}|provider_account:[0-9a-f-]{36})$'),
    cps        INT  NOT NULL CHECK (cps >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by UUID
);

-- Live wait reason (criterion 53). Written only when it changes.
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS wait_reason TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS wait_since  TIMESTAMPTZ;
DO $$ BEGIN
  -- Drop and re-add in one block (lesson 13); the set only grows, so no row can violate it.
  ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS campaigns_wait_reason_check;
  ALTER TABLE campaigns ADD CONSTRAINT campaigns_wait_reason_check CHECK (wait_reason IS NULL OR wait_reason IN
    ('campaign_cap','tenant_cap','platform_cap','trunk_cps','provider_cps','calling_hours','pacing','registry',
     'insufficient_credit','budget_reached','billing_unavailable'));
END $$;

-- ── Billing seam (A13) ──
-- billing_state is the outbox for billing calls; 'off' = NullBillingAdmission. Money is minor units of the billing currency.
ALTER TABLE campaign_attempts ADD COLUMN IF NOT EXISTS billing_state    TEXT NOT NULL DEFAULT 'off';
ALTER TABLE campaign_attempts ADD COLUMN IF NOT EXISTS max_call_s       INT;     -- cap from answer; agent max at claim, billing may only lower; sent in the intent or as time_limit
ALTER TABLE campaign_attempts ADD COLUMN IF NOT EXISTS budget_cap_minor BIGINT;  -- remaining budget at claim; the reserve's max amount
ALTER TABLE campaign_attempts ADD COLUMN IF NOT EXISTS hold_minor       BIGINT;
ALTER TABLE campaign_attempts ADD COLUMN IF NOT EXISTS billable_ms      BIGINT;
ALTER TABLE campaign_attempts ADD COLUMN IF NOT EXISTS billing_source   TEXT;
ALTER TABLE campaign_attempts ADD COLUMN IF NOT EXISTS cost_minor       BIGINT;
ALTER TABLE campaign_attempts ADD COLUMN IF NOT EXISTS cost_currency    TEXT;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'campaign_attempts_billing_check') THEN
    ALTER TABLE campaign_attempts ADD CONSTRAINT campaign_attempts_billing_check CHECK (
      billing_state IN ('off','reserving','held','authorized','settle_due','release_due','settled','released')
      AND (billing_source IS NULL OR billing_source IN ('fs_billmsec','provider_api','calls_row','cap'))
      AND (max_call_s IS NULL OR max_call_s BETWEEN 30 AND 7200)
      AND (billable_ms IS NULL OR billable_ms >= 0)
      AND (billable_ms IS NULL OR max_call_s IS NULL OR billable_ms <= max_call_s::bigint * 1000)
      AND (cost_currency IS NULL OR cost_currency ~ '^[A-Z]{3}$')
      AND (billing_state NOT IN ('settle_due','settled') OR billable_ms IS NOT NULL)
      AND (billing_state <> 'settled' OR cost_minor IS NOT NULL));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_campaign_attempts_billing_open
    ON campaign_attempts(campaign_id) WHERE billing_state IN ('reserving','held','authorized','settle_due','release_due');

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS budget_limit_minor BIGINT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS spent_minor        BIGINT NOT NULL DEFAULT 0;  -- += cost_minor in the settle transaction
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'campaigns_budget_check') THEN
    ALTER TABLE campaigns ADD CONSTRAINT campaigns_budget_check
      CHECK ((budget_limit_minor IS NULL OR budget_limit_minor >= 1) AND spent_minor >= 0);
  END IF;
END $$;

-- ── Business outcome, callbacks, DNC, CRM, compliance (criteria 57–101) ──
ALTER TABLE campaign_attempts ADD COLUMN IF NOT EXISTS is_callback BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS purpose         TEXT;   -- NULL only on legacy rows; readiness blocks
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS default_country TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS ended_at        TIMESTAMPTZ;   -- set with status='completed'
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS purged_at       TIMESTAMPTZ;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'campaigns_purpose_check') THEN
    ALTER TABLE campaigns ADD CONSTRAINT campaigns_purpose_check
      CHECK (purpose IS NULL OR purpose IN ('promotional', 'service', 'transactional'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'campaigns_default_country_check') THEN
    ALTER TABLE campaigns ADD CONSTRAINT campaigns_default_country_check
      CHECK (default_country IS NULL OR default_country ~ '^[A-Z]{2}$');
  END IF;
END $$;
UPDATE campaigns SET ended_at = updated_at WHERE status = 'completed' AND ended_at IS NULL;

-- Contacts: personal fields become nullable so purge/erasure can null them; aggregates stay.
ALTER TABLE campaign_contacts ALTER COLUMN phone_number DROP NOT NULL;
ALTER TABLE campaign_contacts ADD COLUMN IF NOT EXISTS consent_source   TEXT CHECK (consent_source IS NULL OR length(consent_source) <= 200);
ALTER TABLE campaign_contacts ADD COLUMN IF NOT EXISTS upload_id        UUID;
ALTER TABLE campaign_contacts ADD COLUMN IF NOT EXISTS dnc_requested_at TIMESTAMPTZ;
ALTER TABLE campaign_contacts ADD COLUMN IF NOT EXISTS review_reason    TEXT;
ALTER TABLE campaign_contacts ADD COLUMN IF NOT EXISTS pii_purged_at    TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_campaign_contacts_phone ON campaign_contacts(phone_number) WHERE phone_number IS NOT NULL;

CREATE TABLE IF NOT EXISTS contact_uploads (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL,
    campaign_id    UUID NOT NULL,
    attested_by    UUID NOT NULL REFERENCES users(id),
    attested_email TEXT NOT NULL,
    attested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    file_sha256    TEXT NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    inserted       INT NOT NULL, duplicates INT NOT NULL, rejected INT NOT NULL,
    FOREIGN KEY (campaign_id, tenant_id) REFERENCES campaigns(id, tenant_id)
);

CREATE TABLE IF NOT EXISTS campaign_callbacks (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL,
    campaign_id       UUID NOT NULL,
    contact_id        UUID NOT NULL,
    scheduled_at      TIMESTAMPTZ NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','dialed','cancelled','replaced')),
    cancelled_reason  TEXT,
    source_attempt_id UUID NOT NULL REFERENCES campaign_attempts(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (campaign_id, tenant_id) REFERENCES campaigns(id, tenant_id),
    FOREIGN KEY (contact_id, campaign_id) REFERENCES campaign_contacts(id, campaign_id)
);
-- Criterion 66. Scoped by contact_id, a globally unique UUID owned by one tenant (lesson 3).
CREATE UNIQUE INDEX IF NOT EXISTS campaign_callbacks_one_pending ON campaign_callbacks(contact_id) WHERE status = 'pending';

-- DNC: full E.164 matching (criteria 90, 91). The migration is ADDITIVE: no row is deleted and no phone_number is
-- rewritten, so no UNIQUE (tenant_id, phone_number) collision is possible (lesson 5). Legacy rows keep blocking by
-- last-10 suffix until an admin confirms the conversion.
ALTER TABLE dnc_numbers ADD COLUMN IF NOT EXISTS match_kind      TEXT NOT NULL DEFAULT 'e164';
ALTER TABLE dnc_numbers ADD COLUMN IF NOT EXISTS legacy_of       UUID REFERENCES dnc_numbers(id);  -- E.164 row derived from a legacy row
ALTER TABLE dnc_numbers ADD COLUMN IF NOT EXISTS superseded_at   TIMESTAMPTZ;  -- legacy row retired by admin confirmation
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'dnc_numbers_match_kind_check') THEN
    UPDATE dnc_numbers SET match_kind = 'last10' WHERE phone_number !~ '^\+[1-9][0-9]{6,14}$';
    ALTER TABLE dnc_numbers ADD CONSTRAINT dnc_numbers_match_kind_check CHECK (match_kind IN ('e164', 'last10'));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_dnc_numbers_last10
    ON dnc_numbers (tenant_id, right(regexp_replace(phone_number, '[^0-9]', '', 'g'), 10))
    WHERE match_kind = 'last10' AND superseded_at IS NULL;
-- Legacy contacts stored in non-E.164 formats are matched by suffix candidate and then exact to_e164 (dnc/erasure).
CREATE INDEX IF NOT EXISTS idx_campaign_contacts_legacy_last10
    ON campaign_contacts (right(regexp_replace(phone_number, '[^0-9]', '', 'g'), 10))
    WHERE phone_number IS NOT NULL AND phone_number !~ '^\+[1-9][0-9]{6,14}$';

-- Unconvertible suffix candidates of an erasure request await an admin decision; never auto-erased.
CREATE TABLE IF NOT EXISTS erasure_reviews (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id),
    contact_id    UUID NOT NULL REFERENCES campaign_contacts(id),
    number_sha256 TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','erased','dismissed')),
    decided_by    UUID,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at    TIMESTAMPTZ,
    UNIQUE (contact_id, number_sha256)
);

-- Per-tenant campaign settings, owned by Campaigns. A row is created on first save.
CREATE TABLE IF NOT EXISTS tenant_campaign_settings (
    tenant_id        UUID PRIMARY KEY REFERENCES tenants(id),
    default_country  TEXT CHECK (default_country IS NULL OR default_country ~ '^[A-Z]{2}$'),
    retention_days   INT  CHECK (retention_days IS NULL OR retention_days >= 1),   -- ≤ platform default, enforced on write
    crm_webhook_url  TEXT,
    crm_secret_ref   TEXT,   -- encrypt_tenant_secret(tenant_id, …) only (lesson 43); never returned
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by       UUID
);

CREATE TABLE IF NOT EXISTS platform_campaign_settings (
    singleton              BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    default_retention_days INT NOT NULL DEFAULT 90 CHECK (default_retention_days >= 1),
    -- {"promotional": ["140"], "service": ["1600"], ...}. Ships empty: nothing is hard-coded (criterion 88).
    number_series          JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by             UUID
);
INSERT INTO platform_campaign_settings (singleton) VALUES (true) ON CONFLICT DO NOTHING;

-- Transactional outbox; id is the stable delivery id (criterion 79).
CREATE TABLE IF NOT EXISTS crm_deliveries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL,
    campaign_id     UUID NOT NULL,
    contact_id      UUID NOT NULL UNIQUE,   -- one delivery per contact final outcome; contact UUIDs are global (lesson 3)
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered','failed','cancelled')),
    attempts        INT  NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 6),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '30 seconds',  -- lets calls.disposition land first
    leased_until    TIMESTAMPTZ,
    last_http_status INT,
    last_error_code TEXT,                    -- egress_guard.failure_code(); never a message or a URL
    cancelled_reason TEXT CHECK (cancelled_reason IS NULL OR cancelled_reason IN ('purged','erased','crm_unset')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at    TIMESTAMPTZ,
    FOREIGN KEY (campaign_id, tenant_id) REFERENCES campaigns(id, tenant_id),
    FOREIGN KEY (contact_id, campaign_id) REFERENCES campaign_contacts(id, campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_crm_deliveries_due ON crm_deliveries(next_attempt_at) WHERE status = 'pending';

-- Append-only, tenant-readable audit (criteria 85, 98, 101). audit_log is not reused: it has no tenant_id and its
-- action CHECK is created/updated/deleted, and widening it would expose every entity type to tenant admins.
CREATE TABLE IF NOT EXISTS campaign_audit_events (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    campaign_id UUID,
    actor_id    UUID,
    actor_email TEXT,
    action      TEXT NOT NULL CHECK (action IN ('contacts_exported','consent_attested','number_erased',
                                                'retention_changed','crm_settings_changed','dnc_conversion_confirmed')),
    details     JSONB NOT NULL DEFAULT '{}'::jsonb,   -- never a phone number; erasure stores number_sha256
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION campaign_audit_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'campaign_audit_events is append-only'; END $$;
DROP TRIGGER IF EXISTS campaign_audit_events_no_change ON campaign_audit_events;
CREATE TRIGGER campaign_audit_events_no_change BEFORE UPDATE OR DELETE ON campaign_audit_events
    FOR EACH ROW EXECUTE FUNCTION campaign_audit_events_append_only();

-- Marks campaign-linked calls already erased by erase_call_pii (idempotency).
ALTER TABLE calls ADD COLUMN IF NOT EXISTS pii_purged_at TIMESTAMPTZ;

-- Owned by the Conversation side (A11): the single definition of "call personal data".
CREATE OR REPLACE FUNCTION erase_call_pii(p_tenant_slug TEXT, p_session_ids TEXT[]) RETURNS INT
LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  DELETE FROM transcript_entries te USING calls c
   WHERE te.session_id = c.session_id AND c.tenant_id = p_tenant_slug AND c.session_id = ANY(p_session_ids);
  -- Column coverage is pinned by test (Test plan 10): every calls column is classified as personal or not.
  UPDATE calls SET caller_number = NULL, called_number = NULL, extracted_variables = NULL,
                   sentiment_reason = NULL, recording_ref = NULL, call_id = NULL, pii_purged_at = now()
   WHERE tenant_id = p_tenant_slug AND session_id = ANY(p_session_ids) AND pii_purged_at IS NULL;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
CREATE INDEX IF NOT EXISTS idx_campaign_attempts_provider_call
    ON campaign_attempts(tenant_id, provider_call_id) WHERE provider_call_id IS NOT NULL;

-- Legacy #116 rows: 'calling' with no attempt row came from the old in-memory worker. Never redial them.
-- Scoped to contacts with no attempt row, so re-applying while the new worker runs cannot touch live attempts.
UPDATE campaign_contacts c
   SET status = CASE WHEN c.call_session_id IS NOT NULL THEN 'completed' ELSE 'failed' END
 WHERE c.status = 'calling'
   AND NOT EXISTS (SELECT 1 FROM campaign_attempts a WHERE a.contact_id = c.id);
```
- **RLS.** `campaign_attempts` gets the same RLS enable and policy shape that `schema.sql` applies to `campaigns`. `tests/test_rls_coverage.py` enforces it. Every tenant read also carries an explicit `tenant_id` predicate (lesson 36).
- **New tenant tables.** `contact_uploads`, `campaign_callbacks`, `crm_deliveries`, `campaign_audit_events`, `erasure_reviews` and `tenant_campaign_settings` get the RLS shape used for `campaigns`. Every read also carries an explicit `tenant_id` predicate (lesson 36). `platform_campaign_settings` is platform data with no RLS, written by superadmin only.
- **Legacy campaigns.** `purpose IS NULL` rows are not backfilled, because defaulting a legacy promotional campaign to `service` would misclassify it. Readiness blocks on `purpose_missing` until an admin sets the purpose.
- **Capacity tables.** `platform_outbound_limits` and `outbound_cps_limits` hold platform data with no tenant column, so they get no RLS policy. They are written only through superadmin routes and read by Campaigns through `platform_conn`. `cps_scope` and `last_send_at` are on a table that is new in this change, so no backfill is needed.
- **Billing columns (A13).** All are on `campaign_attempts` (new in this change) or are nullable/defaulted on `campaigns`, so no backfill. Legacy campaigns get `spent_minor = 0` and no budget. No new table, so no new RLS policy; every billing read also carries `tenant_id = $tenant` (lesson 36).
- **Outcome order.** `answered → ended` is the only forward move between outcomes, and only the conditional `UPDATE` in `apply_outcome` applies it. The PRD exception (`failed` with `closed_by_deadline` and no call evidence → `answered|ended`) is the only overwrite. `send_count` caps a Path A `pending` attempt at 2 sends: the original send plus one recovery re-send (criterion 16). It is incremented by `attempts.reserve_send` before every send, not by `mark_sent`, so a send that times out still counts.
- **Callbacks and DNC in contact status.** On `answered`/`ended`, if a `pending` callback exists the contact becomes `pending` with `next_attempt_at = callback.scheduled_at` instead of `completed`. If `dnc_requested_at` is set, the contact becomes `blocked` and is never re-queued. If `review_reason` is set, the contact is not re-queued until it is cleared. A contact whose `attempt_count = max_attempts` but which has a due pending callback is still dialed, because callbacks do not consume attempts (criterion 67).
- **Contact status.** It mirrors the latest attempt: `answered`/`ended` → `completed`; `busy`/`no_answer`/`declined`/`failed` with attempts left → `pending` with `next_attempt_at = resolved_at + retry_gap`; exhausted → that outcome's status (`declined` when the last attempt was declined). `declined` stays a distinct recorded outcome; only `answered`/`ended` stop redialing (PRD criteria 10, 13, 14).

## Interfaces

### Campaigns internals
- `attempts.idempotency_key(contact_id: str, attempt: int) -> str`
- Worker pre-check, done before claiming and with no lock: `campaign_contacts.peek_next_due(campaign_id) -> dict | None`, then `dnc.is_blocked` (on a hit: `blocked` plus cancel callbacks), then `dnd_registry.check(e164, purpose, tenant_id)` with a 2 s timeout. `deny` → contact `blocked`, with no attempt created. `unknown` → allow. An exception or timeout → `set_wait_reason('registry')` with no attempt created and nothing consumed (criterion 92). No external call is ever made under the admission lock.
- `campaign_contacts.claim_next_due(campaign: dict, *, contact_id: UUID, path: Literal["sip","cpaas"], provider: str | None, cps_scope: str) -> Claimed | Waiting`. `Claimed(contact: dict, attempt: dict)`; `Waiting(reason: WaitReason)`.
  - One transaction:
    1. `SELECT pg_advisory_xact_lock(hashtext('campaigns:admission'))`. This one platform-wide lock serialises every admission and re-send reservation across all Campaigns workers, so two workers cannot both see the last free slot. It is held only for this short transaction; dial starts are already CPS-bounded. Then lock the campaign row with `FOR UPDATE`, always in this order, which prevents deadlock.
    2. Call `capacity.admit(conn, campaign, cps_scope)`. If it returns a `WaitReason`, the transaction writes only `campaigns.wait_reason`/`wait_since` (and only when they changed), then commits and returns `Waiting`. No contact or attempt row is touched, `attempt_count` is unchanged and no attempt fails (criterion 51).
    3. Lock the contact the worker pre-checked (`contact_id` argument) only if it is still `status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= now())`, using `FOR UPDATE SKIP LOCKED`. If it is not, return `Waiting(None)` and the worker peeks again next tick.
    4. Exclude contacts that already have an `answered`/`ended` attempt unless they have a due `pending` callback. Exclude any contact with `review_reason` set.
    5. Set the contact to `calling`. If a due `pending` callback exists, it becomes `dialed` and the attempt has `is_callback=true`, with `attempt_count` unchanged (criterion 67). Otherwise, increment `attempt_count`. The attempt number is `max(attempt)+1` for the contact, kept separate from `attempt_count`, so keys stay unique across callbacks.
    6. Insert an attempt with `state='pending'`, `deadline_at = now() + 120 s`, `call_session_id = gen_random_uuid()::text` for `sip`, a snapshot of callee and caller-ID, `max_call_s = COALESCE(agents.max_call_duration_s, 7200)` for the campaign's agent (set on every attempt, billing on or off), `cps_scope`, `send_count = 1` and `last_send_at = clock_timestamp()`. This reserves the first send in the same transaction that checked CPS; the worker then sends without calling `reserve_send`. Set `billing_state='reserving'` (`'off'` when `billing.enabled` is false) and, when the campaign has a budget, `budget_cap_minor = budget_limit_minor − committed` (step 0 of `capacity.admit`). Clear `campaigns.wait_reason`.
- `capacity.admit(conn, campaign: dict, cps_scope: str) -> WaitReason | None`. It runs under the admission lock and checks in this order, returning the first reason that applies:
  0. `budget_reached` (A13; only when `budget_limit_minor` is set): `committed = spent_minor + Σ COALESCE(hold_minor, budget_cap_minor, 0)` over the campaign's attempts in an open `billing_state` (`idx_campaign_attempts_billing_open`) is ≥ `budget_limit_minor`. If `spent_minor` alone is ≥ the budget, the same transaction also sets `campaigns.status='paused'` (`WHERE status='running'`).
  1. `campaign_cap`: campaign in-flight ≥ `max_concurrent_calls`.
  2. `tenant_cap`: `tenants.max_outbound_concurrent_calls` is set and tenant in-flight (`campaign_attempts.tenant_id`, all campaigns) ≥ that value. The effective limit is therefore the lower of the campaign and tenant caps (criterion 44), and other tenants are unaffected (criterion 45).
  3. `platform_cap`: `platform_outbound_limits.max_outbound_concurrent_calls` is set and platform in-flight ≥ that value (criterion 47).
  4. `trunk_cps` (`sip_trunk:*`) or `provider_cps` (`provider_account:*`): an `outbound_cps_limits` row exists for the scope, and `count(*) WHERE cps_scope=$1 AND last_send_at > clock_timestamp() - interval '1 second'` ≥ `cps` (criteria 49, 50).

  In-flight means `state IN ('pending','sent') OR outcome='answered'`, the same predicate as A2. Every limit is read in this transaction, so a change applies on the next tick with no restart (criterion 52). Lowering a limit only blocks new admissions; nothing is ended. Usage is a query over persisted attempts, so a restart rebuilds it exactly (criterion 55).
  - `WaitReason = Literal["campaign_cap","tenant_cap","platform_cap","trunk_cps","provider_cps","calling_hours","pacing","registry","insufficient_credit","budget_reached","billing_unavailable"]`; `registry` is used for criterion 92. `insufficient_credit` and `billing_unavailable` are written only by `attempts.unclaim` (A13). `calling_hours` and `pacing` are set by the worker's existing pre-checks through `campaigns.set_wait_reason(campaign_id, reason)`, which updates only on change.
  - CPS scope: Path A uses `sip_trunk:pstn`. Every Path A callee leg leaves through Kamailio's single `route(PSTN)` gateway (`pstn.gw_ip`), so that gateway is the one trunk shared by all tenants. Path B uses `provider_account:<resolve_outbound_route().telephony_config_id>`.
  - DNC and calling-hours checks run before this call, so a blocked contact never gets an attempt (criterion 9).
- `attempts.reserve_send(attempt_id, *, path: Literal["sip","cpaas"]) -> Literal["ok","wait_cps","refused"]`: the gate before every re-send (Path A recovery, Path B `not_placed` and 429 retries). The first send is reserved by `claim_next_due`. It takes the same advisory lock. If the attempt's `cps_scope` is at its CPS limit it returns `wait_cps`, writes nothing, and the next tick retries; the attempt keeps its slot and is neither failed nor consumed. Otherwise it runs `UPDATE … SET send_count = send_count + 1, last_send_at = clock_timestamp() WHERE id=$1 AND state='pending' AND ($2 <> 'sip' OR (send_count < 2 AND created_at > now() - interval '60 seconds')) RETURNING id`. Zero rows means `refused` (do not send). The Path A re-send window is 60 s, which is the claim TTL (105 s) minus a 45 s margin. Age is measured from `created_at`. The first send, and so any claim, happens after `created_at`, so the claim lives until at least `created_at + 105 s`. The 45 s margin absorbs clock skew between the Postgres and FreeSWITCH/Redis hosts and the 3 s HTTP timeout. Inside the window, a lapsed claim is impossible, and FreeSWITCH's `SET NX` dedupes any duplicate. Past the window, no send can happen under that key.
- `attempts.mark_sent(attempt_id, *, provider_call_id: str | None) -> bool`: `UPDATE … SET state='sent', sent_at=now() WHERE id=$1 AND state='pending' RETURNING id`.
- `attempts.apply_outcome(*, tenant_id: UUID | None, key: str | None, provider_call_id: str | None, outcome: str, call_session_id: str | None, closed_by_deadline: bool = False, failure_reason: str | None = None, billable_ms: int | None = None, billing_source: str | None = None) -> dict | None`
  - One transaction and one conditional `UPDATE`. The match is `(idempotency_key = $key OR (provider_call_id = $pcid AND tenant_id = $tenant))`, and also `tenant_id = $tenant` whenever the event carries a tenant (criterion 38). The transition guard is the outcome order above.
  - When a row is updated, the contact is updated in the same transaction.
  - A late `answered`/`ended` that upgrades a deadline-failed attempt also sets the contact to `completed`. It deletes the queued retry (`next_attempt_at = NULL`) with a conditional `UPDATE … WHERE status='pending'`; when the contact is already `calling` for attempt+1, it marks that attempt `failure_reason='superseded'` so its outcome cannot re-queue the contact (criterion 12).
  - Returns `None` when nothing matched (duplicate, out of order, or foreign tenant).
  - **Billing (A13).** The same `UPDATE` moves `billing_state` per the Billing seam's outcome table, so a duplicate or out-of-order event, which matches no row, cannot move it.
  - **Final business outcome (criterion 77).** When this transaction leaves the contact final (`completed` with no pending callback, `blocked` with `dnc_requested_at`, or attempts exhausted) and `tenant_campaign_settings.crm_webhook_url` is set, it also runs `INSERT INTO crm_deliveries (...) ON CONFLICT (contact_id) DO NOTHING`. This is the outbox, and it commits atomically with the outcome.
- `DialRequestClient(dial_url: str, hmac_secret: bytes, redis)`, with `async close() -> None`.
  - `DialIntent(key, tenant_id, campaign_id, contact_id, call_uuid, callee, caller_id, tenant_slug, agent_slug, exp, egress_token, max_call_s)` is stored as JSON at `campaign:intent:<key>`. `egress_token = f"{key}.{exp}.{tenant_id}.{caller_id}.{max_call_s}." + base64url_unpadded(HMAC_SHA256(CAMPAIGNS_EGRESS_HMAC_SECRET, f"{key}|{callee}|{caller_id}|{tenant_id}|{max_call_s}|{exp}"))`, where `exp` is the intent expiry plus 120 s. Every token field is dot-free (UUID, E.164, integers). The token authorises PSTN egress once, until `exp`, for exactly this callee, caller ID, tenant and time limit; a rewritten intent cannot carry a valid token for changed values. `call_uuid` is the attempt's pre-generated `call_session_id`, so the call record can be found by primary key before any event arrives.
  - `max_call_s` is required and always set: the attempt's `max_call_s`, which `claim_next_due` initialises to `COALESCE(agents.max_call_duration_s, 7200)`. With billing on, the `held` transition may only lower it (`LEAST`), never raise it (A13). It is the only billing value FreeSWITCH sees.
  - `async send(intent: DialIntent, *, first: bool) -> Literal["accepted","duplicate","rejected","no_node","timeout"]`. It is called only after `claim_next_due` returns `Claimed` (first send) or `reserve_send` returns `ok` (a re-send).
    1. **Intent.** On the first send: `SET campaign:intent:<key> <json> NX EX 60`. On a re-send: `EXISTS campaign:intent:<key>`. If the intent is gone, or `campaign:claim:<key>` exists, return `duplicate` without making an HTTP request.
    2. **POST.** `POST {dial_url}` with the canonical body `{"key":…,"campaign_id":…,"contact_id":…,"callee":…,"caller_id":…,"tenant_slug":…,"agent_slug":…,"ts":<int>}`: fixed key order, no whitespace. The header is `X-Campaign-Signature: <unpadded base64url HMAC_SHA256(secret, raw_body)>` and the timeout is 3 s.

    | Result | HTTP / condition | Meaning | Attempt action |
    |---|---|---|---|
    | `accepted` | 200 `{"result":"accepted"}` | Kamailio selected a node and sent the INVITE | `mark_sent(provider_call_id=None)`. The outcome comes from events, the call row or the deadline. If FreeSWITCH then refuses (no intent), nothing is dialed and A1 resolves the attempt at the deadline. |
    | `duplicate` | Pre-check: the intent was consumed or a claim exists | An earlier send of this key reached FreeSWITCH | `mark_sent`. Never mark it failed from this result. |
    | `rejected` | 400 (body), 401 (HMAC/ts), 403 (source), 404/405 (path/method) | Kamailio sent nothing | First `DEL campaign:intent:<key>`, so the key can never dial. Then `apply_outcome(outcome='failed', failure_reason='dial_rejected')` and retry as attempt+1 after the gap, bounded by `max_attempts`. Logged at ERROR because it means a config fault. |
    | `no_node` | 503 `{"result":"no_node"}` | No active dispatcher node, so nothing was sent | `DEL` the intent, then `apply_outcome(outcome='failed', failure_reason='no_sip_node')` and retry as attempt+1 after the gap (criterion 28). |
    | `timeout` | Timeout, connection error, or any other 5xx | Unknown whether the INVITE left | Leave it `pending`, with no immediate re-send. The reconcile table decides: a call row → adopt; a claim → wait; the intent still unconsumed with no claim → one re-send through `reserve_send` (a single `GETDEL` means only one node can ever dial); the intent gone or expired with no claim → the deadline path (fail, attempt+1, new key). |
  - `async probe() -> bool`: signed `POST {dial_url}/health` with body `{"ts":<int>}`. Kamailio returns 200 if `ds_select_dst("1","4")` succeeds and `PSTN_GW_URI` is non-empty, otherwise 503. It sends no INVITE, and readiness uses it for `no_healthy_sip_node`.
  - The intent carries the campaign's `tenant_slug` and `agent_slug`. For outbound campaign calls the Gateway routes on those; the caller ID is only the number shown to the callee (user decision 2026-10-22).
- `readiness.check(campaign: dict, *, tenant_id: UUID) -> Readiness`, where `Readiness(ready: bool, checks: list[Check], estimate: CostEstimate | None)` and `Check(id: str, level: Literal["block","warn"], field: str, message: str, count: int | None)`.
  - Blocks: `agent_unavailable`, `caller_id_missing`, `caller_id_not_owned`, `no_pending_contacts`, `timezone_invalid`, `provider_not_ready` (Path B, from Telephony `outbound-readiness`), `provider_not_campaign_capable`, `no_healthy_sip_node` (Path A, from `probe()`).
  - Also blocks: `purpose_missing` (legacy rows only).
  - Billing (A13), only when `billing.enabled`, from one `account_status(tenant_id)` call: blocks `billing_account_missing` (`has_account` false), `budget_reached` (`spent_minor ≥ budget_limit_minor`), `provider_cannot_cap_call` (Path B provider without `supports_time_limit`); warns `low_balance` (`balance_minor < estimate.total_minor`; message gives both amounts), `call_length_capped` (`COALESCE(agents.max_call_duration_s, 7200) > max_hold_s`; message gives both in minutes). When billing is disabled and a budget is set: warn `budget_unenforced`. A billing error during readiness blocks with `billing_unavailable`, because start would only wait.
  - Warns: `caller_id_series_mismatch`, when `number_series[purpose]` is non-empty and the caller-ID's national number starts with none of the series; the message names the purpose and the series, and start still succeeds (criterion 88). Also warns `contacts_on_dnc` (count), `outside_calling_hours_today` (count of pending), `campaign_cap_above_tenant_cap` (message gives both numbers, criterion 46). Capacity never blocks readiness: a full cap means wait, not "cannot dial".
  - `tenant_id` is the campaign's tenant, already authorised by `get_campaign_for_caller`.

### Worker reconcile (every tick, after `_drain_events`; skips attempt ids in this process's `_sending` set)
| Path | State | Condition | Action |
|---|---|---|---|
| A | `pending` | `calls` row exists | Take its state: `ended_at` null → answered; set → ended. |
| A | `pending` | claim exists, no call row | Wait. Criterion 18 applies at the deadline. |
| A | `pending` | no claim, no call row, intent still present, older than 10 s, and `reserve_send` returns `ok` (`send_count < 2`, age < 60 s; `wait_cps` means try again next tick) | `send(first=False)` with the same key (criterion 16), then act per the send-result table. |
| A | `pending` | no claim, no call row, and either the intent is gone/expired (its 60 s TTL equals the re-send window) or `reserve_send` returns `refused` | No send under this key. Nothing until the deadline, then `failed` with `closed_by_deadline`, and retry as attempt+1 with a new key after the gap. |
| A | `sent` | no claim, no call row | Nothing until the deadline (A1). |
| A | any open | past deadline and not live per A2 | `failed`, `closed_by_deadline`, retry attempt+1 after the gap. |
| B | `pending`, `not_before` passed or null | — | `reconcile_call`: `placed` → `mark_sent(provider_call_id)`; `not_placed` → send once; `unknown` or multiple matches → nothing, deadline rule applies (criterion 19). |
| B | `sent` | — | `call_status(provider_call_id)` → apply a terminal status; `live` → keep (criterion 20). Webhook-sourced terminal outcomes always pass through this same call before they are applied (security MEDIUM 2). |
| B | open | past deadline | `call_status`, then the call row; fail only if both say "no call". |
| both | `answered` | — | `calls.ended_at` set → `ended`. No end after `COALESCE(agents.max_call_duration_s, 7200) + 300` s from `answered_at` → `ended` with `failure_reason='end_not_observed'` (frees the slot), `billable_ms = max_call_s·1000`, `billing_source='cap'` (A13). An `ended` taken from `calls.ended_at` uses `calls_row` timing. |

Path B 429 (criterion 39): the attempt stays `pending` with `not_before = now() + min(300, retry_after or 30·2^n)` and `deadline_at` moved past `not_before`. `attempt_count` is not consumed, and the key is reused because the Telephony service released it.

### Billing seam (A13)
`services/campaigns/billing.py` is the only module that knows billing exists.
```
def billing_key(attempt_id: UUID) -> str                      # f"campaign:{attempt_id}"
class BillingAdmission(Protocol):
    enabled: bool
    async def account_status(self, tenant_id: UUID) -> AccountStatus
    async def estimate(self, tenant_id: UUID, *, path: Literal["sip","cpaas"], provider: str | None,
                       destination_country: str | None, seconds: int) -> int          # minor units, one call
    async def reserve(self, key: str, *, tenant_id: UUID, path: Literal["sip","cpaas"], provider: str | None,
                      destination_country: str | None, max_seconds: int, max_amount_minor: int | None,
                      expires_at: datetime) -> Granted | Refused
    async def authorize(self, key: str, *, expires_at: datetime) -> None
    async def settle(self, key: str, *, billable_ms: int) -> Settlement
    async def release(self, key: str) -> None
    async def aclose(self) -> None
AccountStatus(has_account: bool, balance_minor: int | None, currency: str | None, max_hold_s: int | None)
Granted(max_seconds: int, hold_minor: int | None)
Refused(reason: Literal["insufficient_credit","no_account","concurrency","unavailable"])
Settlement(amount_minor: int, currency: str)
class NullBillingAdmission                                     # enabled=False; only account_status is ever called
async def estimate_campaign(conn, billing: BillingAdmission, campaign: dict) -> CostEstimate | None
CostEstimate(currency: str, calls: int, avg_billable_s: int, avg_source: Literal["tenant_history","default"],
             per_call_minor: int, total_minor: int)
```
- **Contract** (pinned by `test_billing_contract.py` for every implementation):
  1. `reserve` is idempotent per key: a repeat returns the live hold for that key. A `Refused` leaves no record.
  2. A `campaign:` key is never counted against an outbound-concurrency limit in billing (A13 concurrency owner); only an all-direction limit may return `Refused("concurrency")`.
  3. `authorize` and `release` on an unknown, released or expired key are no-ops. `settle` posts at most one charge per key, also after release or expiry; a repeat returns the first `Settlement`.
  4. No phone number crosses the seam; `destination_country` is the callee's ISO-2 region from `phonenumbers`.
  5. The client bounds every call at 2 s. For `reserve`, timeout or error is `Refused("unavailable")`; the other methods raise and `_billing_sync` retries next tick.
  6. `settle` never charges more than the `Granted.max_seconds` that `reserve` returned for that key, also after release or expiry: billing records the grant per key and charges `min(billable_ms, max_seconds·1000)`. The grant is the cap, not the caller's value. A `settle` for a key with no recorded grant posts nothing and raises (security HIGH 1, R4).
- **Dial admission** (`worker._dial_next`, after `claim_next_due` returns `Claimed`, outside the advisory lock, skipped when `billing_state='off'`):
  1. `reserve(billing_key(attempt.id), max_seconds=COALESCE(agents.max_call_duration_s, 7200), max_amount_minor=attempt.budget_cap_minor, expires_at=attempt.deadline_at + 300 s, …)`.
  2. `Granted` with `max_seconds ≥ 30`: `UPDATE campaign_attempts SET billing_state='held', hold_minor=$2, max_call_s=LEAST(max_call_s, $3) WHERE id=$1 AND billing_state='reserving' RETURNING id`. Zero rows (a recovery already removed the row): `release`, do not send. Otherwise send: Path A puts `max_call_s` in the intent; Path B passes it as `time_limit_s`.
  3. Anything else: `attempts.unclaim(attempt.id, wait_reason=…)` with `insufficient_credit`/`no_account` → `insufficient_credit`, `concurrency` → `tenant_cap`, `unavailable` → `billing_unavailable` (after a best-effort `release`, since the reserve's fate is unknown), a grant under 30 s → `release`, then `budget_reached` if the campaign has a budget, else `insufficient_credit`.
- **`attempts.unclaim(attempt_id: UUID, *, wait_reason: WaitReason | None) -> bool`.** One transaction: `DELETE FROM campaign_attempts WHERE id=$1 AND state='pending' AND billing_state='reserving' RETURNING contact_id, is_callback`; zero rows → `False`. Otherwise the contact goes back to `pending` with its `next_attempt_at` unchanged and `attempt_count − 1`, or, for a callback, the contact's `dialed` callback with the latest `scheduled_at` goes back to `pending`; then `set_wait_reason`. Deleting is safe: a send happens only after the `held` UPDATE, and nothing references an unsent attempt.
- **`billing_state` transitions in `apply_outcome`** (same `UPDATE` as the outcome; `off`, `reserving` and `settled` never move):

  | New outcome | From | To |
  |---|---|---|
  | `answered` | any | unchanged (authorization is `_billing_sync`'s) |
  | `ended` | `held`, `authorized`, `release_due`, `released` | `settle_due`, with `billable_ms` and `billing_source` |
  | `busy`, `no_answer`, `declined`, `failed` | `held`, `authorized` | `release_due` |

  `billable_ms` sources: Path A `hangup` event (`billmsec`) → `fs_billmsec`; Path B `call_status().billable_ms` → `provider_api`; an end known only from `calls.ended_at` (or a Path B status with no duration) → `calls.ended_at − COALESCE(answered_at, calls.started_at)` clamped to `[0, max_call_s·1000]` → `calls_row`; `end_not_observed` → `max_call_s·1000` → `cap`. Every source, `fs_billmsec` and `provider_api` included, is clamped to `[0, max_call_s·1000]` in the `apply_outcome` `UPDATE` itself (`LEAST(GREATEST($billable_ms, 0), max_call_s::bigint * 1000)`) before it is written, so no event value can exceed the grant; the CHECK enforces the same bound (security HIGH 1, R4).
- **`_billing_sync()`** (every tick after `_reconcile`, outside the lock, 50 rows oldest first, `tenant_id` on every read):
  - `held` with consumption evidence → `authorize(key, expires_at=now() + 45 s + max_call_s + 300 s)` → `authorized`. Evidence: Path A `EXISTS campaign:claim:<key>`, outcome `answered`, or a `calls` row on `call_session_id`; Path B `provider_call_id IS NOT NULL`.
  - `settle_due` → `settle(key, billable_ms=…)`, then one transaction: `UPDATE campaign_attempts SET billing_state='settled', cost_minor=$2, cost_currency=$3 WHERE id=$1 AND billing_state='settle_due' RETURNING campaign_id`, and only if a row returned, `UPDATE campaigns SET spent_minor = spent_minor + $2`. A crash between the two re-settles, which returns the first `Settlement`, so `spent_minor` moves once.
  - `release_due` → `release(key)` → `released`.
  - `reserving` older than 30 s and not in this process's `_sending` set (a crashed admission) → `release(key)`, then `unclaim(wait_reason=None)`.
  - A billing error leaves the row for the next tick and logs the attempt id only.
- **Estimate.** `estimate_campaign` returns `None` when billing is disabled. `calls` = the campaign's `pending` contacts (unanswered calls are released, so each contact costs at most one answered call, callbacks aside). `avg_billable_s` = the mean `billable_ms` of the tenant's `settled` attempts in the last 30 days when there are at least 20, else `ESTIMATE_DEFAULT_BILLABLE_S = 60`. `per_call_minor = estimate(…, destination_country=campaign.default_country, seconds=avg_billable_s)`; `total_minor = calls × per_call_minor`.

### In-call tools (criteria 61–74, 84)
- Both tools are local (`pipeline._local_tools`). They are built per session only when the direction is `outbound`. Each description is at most 80 tokens, to bound prompt growth.
- **`schedule_callback`.** Parameters:
  - `caller_words`: the caller's phrasing, verbatim.
  - `local_datetime`: ISO 8601 local time, without an offset, that the model computed.
  - `confirmed`: bool.
  - `proposal_id`: string, required when `confirmed` is true.

  The description tells the model to call first with `confirmed=false`, read back `spoken_time` exactly, and call again with `confirmed=true` and the returned `proposal_id` only after the caller says yes. On error, it must say nothing was booked and ask again.
- **`record_do_not_call`.** No parameters. The description tells the model to call it when the caller asks not to be called again, and to confirm to the caller only on `ok`.
- **Handler.** It is built in `pipeline._local_tools` as `handler(arguments)`, closing over the server-side `tenant_id` and `session_id`. It reads `orchestrator.CURRENT_TURN_ID.get()`; if that is `None`, it returns `FAILED` (`no_turn_context`) and fails closed. It POSTs `{"tenant_id": <captured>, "turn_id": <contextvar>, ...args}` to `/internal/calls/{<captured session_id>}/…`. The model never supplies the session, contact or number. Any `contact_id`/`phone` key in the arguments is dropped before sending (`guard.scrub_scope_args`).
- **`POST /internal/calls/{session_id}/callback`**, body `{tenant_id, caller_words, local_datetime, confirmed, proposal_id?}`.
  - Responses: `{"status":"proposed","proposal_id","spoken_time","adjusted":bool}`, or `{"status":"scheduled","spoken_time"}`, or 422 `{"error": "unparseable"|"in_past"|"too_far"|"time_mismatch"|"callback_limit_reached"|"proposal_expired"|"confirmation_needs_caller_turn"|"not_a_campaign_call"}`, or 503 `{"error":"not_saved"}`.
  - The handler maps 422 to `INVALID_ARGUMENT` and 503 or a timeout to `FAILED` with `callback_not_saved` (criterion 69).
- **`POST /internal/calls/{session_id}/do-not-call`**, body `{tenant_id}` → `{"status":"recorded"}` (also when the number was already listed, criterion 73) or 503.
- **Resolution, both routes.** `attempt = SELECT … FROM campaign_attempts WHERE call_session_id = $1 AND tenant_id = $tenant`. If no row matches, the route drains `campaign:events:cpaas` once (applying a Path B `answered` event that has not been consumed yet), then retries the lookup; if there is still no row, the route returns `not_a_campaign_call`. A `tenant_id` that does not match the attempt's tenant also gets `not_a_campaign_call`.
- **Auth, both routes.** `get_current_user` plus `is_service_account AND tenant_id IS NULL` (lesson 24); everything else gets 403. Routes are in `routers/internal.py`, and a module-scoped test asserts that every `/internal/*` route carries this dependency (lesson 9).
- **`callbacks.propose(attempt, caller_words, local_datetime) -> Proposal | CallbackError`.**
  1. Parse `local_datetime` in `campaigns.calling_hours_timezone`; on failure, `unparseable`.
  2. `check_stated_time(caller_words, computed, now_local)`. Every integer in the words (digits, and English number words one to sixty) must equal the computed day, hour (12-hour or 24-hour form) or minute, or match a stated relative quantity ("in N minutes/hours/days" means `now + N` within ±5 minutes). Recognised terms must agree: today, tomorrow, day after tomorrow, weekday names (within 7 days), month names, next week, and morning/afternoon/evening/night as hour bands. Any disagreement is `time_mismatch`. A phrase with no integer and no recognised term is `unparseable`.
  3. `computed < now` → `in_past`. `computed > now + 30 days` → `too_far`.
  4. If the time is outside calling hours, move it to the next window start and set `adjusted=true` (criterion 65).
  5. If the contact has `count(status IN ('pending','dialed')) >= 3` and no pending callback to replace → `callback_limit_reached` (criterion 67).
  6. Store the Redis key `campaign:cbprop:<uuid>` holding `{session_id, scheduled_at}` with `EX 600`, and return `spoken_time`, formatted by the server (for example "Friday, 24 October at 3:00 PM").
- **`callbacks.confirm(attempt, proposal_id, turn_id)`.** Read the proposal; a missing key or a different `session_id` is `proposal_expired`. If `turn_id` equals the proposal's stored `turn_id`, the result is `confirmation_needs_caller_turn` and the proposal is kept. `propose()` stores `{session_id, scheduled_at, turn_id}`. `turn_id` is the server-side `CURRENT_TURN_ID`, one per orchestrator run, and the pipeline starts one run per caller utterance. So a confirm needs at least one caller utterance after the read-back, and a model-set `confirmed=true` in the same turn is not enough. On success, delete the proposal. Then, in one transaction: lock the contact; re-check the cap; set the old `pending` callback to `replaced`; insert the new `pending` one. Nothing is stored without `confirmed=true` and a server-issued proposal (criterion 63).
- **`dnc.request_from_call(attempt)`.** In one transaction: `add_number(tenant, e164)` with `ON CONFLICT DO NOTHING`, set `campaign_contacts.dnc_requested_at`, cancel the tenant's pending callbacks and retries for that E.164 (criterion 72). This runs during the call, so it lands before hang-up (criterion 70); the contact list shows disposition `do_not_call_requested` while `dnc_requested_at` is set.
- **DNC failure path (criterion 74).** On a 503 or timeout, the Conversation handler `XADD`s `{kind:"dnc_request", session_id, tenant_id}` to `campaign:events:conv` (Conversation already holds a Redis client, `__main__.py:281`) and returns `FAILED`, so the agent does not claim success. The Campaigns consumer applies it with `request_from_call`, sets `review_reason='dnc_write_delayed'` so an admin sees it, and acks only after commit; un-acked entries are retried through `XAUTOCLAIM`. Retry is suppressed in the meantime because the `hangup` event for the same session is written later and the consumer applies `:conv` entries before `:fs`/`:cpaas` entries in each batch. If both the Campaigns call and the `XADD` fail, the handler logs an ERROR with the masked number and session id; that residual is in Risks.

### Disposition, export, uploads (criteria 57–60, 85–87, 89, 100, 101)
- **Contact list query.** `GET /campaigns/{campaign_id}/contacts?outcome=&disposition=&limit=&offset=` joins each contact's latest linked attempt and `calls` on `session_id` (both with `tenant_id` predicates). It returns `phone_masked`, `name`, `status`, `attempt_count`, `last_outcome`, `disposition`, `extracted_variables`, `pending_callback_at`, `review_reason` and `cost_minor` (the sum over the contact's attempts, A13). `disposition` is `'do_not_call_requested'` when `dnc_requested_at` is set; otherwise it is `calls.disposition`. A missing call or disposition gives `null` with no error (criterion 58). Filters apply to those same expressions. The full number is never returned (criterion 100).
- **Export.** `GET /campaigns/{campaign_id}/contacts/export.csv?outcome=&disposition=` uses `get_current_user` (admin or viewer, criterion 60) and `get_campaign_for_caller`, so a foreign id gets the same 404.
  - Columns: `phone_number` (full E.164), `name`, `consent_source`, `attempts`, `last_outcome`, `disposition`, `extracted_variables` (compact JSON).
  - Every cell starting with `=`, `+`, `-`, `@`, `\t` or `\r` gets a leading `'`. Phone numbers start with `+`, so they are always prefixed, as the criterion requires.
  - Streamed with `StreamingResponse`; nothing is written to disk.
  - Before the first byte, a `campaign_audit_events` row `contacts_exported` is written with `{row_count, filters}`. The row count is computed by the same query.
- **Upload.** `POST …/contacts/upload` (multipart) adds a required form field `consent_attested=true`; if it is missing, the response is 400 and nothing is stored. In one transaction: insert a `contact_uploads` row (actor, `sha256(file bytes)`, counts), insert the contacts with `upload_id`, and write a `consent_attested` audit row.
  - Rows are normalised with `to_e164(raw, campaign.default_country)`.
  - Per-row rejections: `invalid_number`, `no_default_country`, `consent_source_too_long` (> 200).
  - Duplicates after normalisation, within the file or against the campaign's existing contacts, are counted and stored once.
  - An optional `consent_source` column is stored when present.
- **Uploads read.** `GET /campaigns/{campaign_id}/uploads` → upload rows. Requires `require_role("superadmin","admin")`, so viewers get 403 (criterion 85).
- **Campaign create/update** require `purpose` in the enum on create and validate it on update; `default_country` must be ISO-2 (criterion 87).

### CRM delivery (criteria 75–83)
- **Settings.** `GET|PUT /tenants/{tenant_id}/campaign-settings`: reads use `get_current_user`, writes use `require_role("superadmin","admin")`; a viewer gets 403.
  - Body `{default_country?, retention_days?, crm_webhook_url?, crm_secret?}`.
  - `crm_secret` is write-only. It is stored as `encrypt_tenant_secret(tenant_id, secret)` (`libs/config_sdk/secrets.py:120`) and responses carry only `"crm_secret_set": bool`.
  - `crm_webhook_url` on GET depends on the caller (security MEDIUM 3):
    - A tenant `admin` of that tenant gets `scheme://host[:port]` plus `"crm_webhook_url_path_set": bool`.
    - Every other role (viewer, superadmin, any other console role) gets `scheme://host[:port]` only.
    - A NULL-tenant service account (`is_service_account AND tenant_id IS NULL`) gets the field omitted entirely.

    The full URL, with path and query, is returned only in the response to the admin's own `PUT`, which echoes what was saved. Logs and audit rows record the host only.
  - The URL must pass `validate_webhook_url` and a save-time `resolve_validated` (criterion 76). `retention_days` must be ≤ the platform default (criterion 93).
  - A foreign `tenant_id` gets the same 404 as a nonexistent one, the same rule as the capacity tenant route.
  - Each change writes `crm_settings_changed`/`retention_changed` audit rows that never contain the secret.
  - When `default_country` is saved, `dnc.convert_legacy` runs.
- **`DeliveryWorker.run()`.** A separate asyncio task, not the dial tick (criterion 81). Every 5 s it does `UPDATE crm_deliveries SET leased_until = now()+60s WHERE id IN (SELECT id … status='pending' AND next_attempt_at <= now() AND (leased_until IS NULL OR leased_until < now()) ORDER BY next_attempt_at LIMIT 20 FOR UPDATE SKIP LOCKED) RETURNING *`, which makes it multi-worker safe, then sends each with up to 4 concurrent requests.
- **Per send.**
  1. Re-read the URL and secret. If unset, the delivery becomes `cancelled`/`crm_unset`.
  2. If the contact is purged or erased, the delivery becomes `cancelled`.
  3. Build the body: `{delivery_id, campaign_id, contact_id, phone_number (E.164), name, telephony_outcome, disposition, extracted_variables, occurred_at}`.
  4. Set `ts = int(time())` and `sig = hex(HMAC_SHA256(secret, f"{ts}.{body}"))`.
  5. Send with headers `X-Yuviz-Delivery-Id`, `X-Yuviz-Timestamp`, `X-Yuviz-Signature: v1=<sig>` and `Content-Type: application/json`, using `guarded_client(timeout=10, use_operator_allowlist=False)` with `follow_redirects=False`. The connect is pinned to the validated IP (criterion 76).
- **Results.**
  - 2xx → `delivered`.
  - 429, 5xx or timeout → `attempts += 1` and `next_attempt_at` = now + [1 min, 10 min, 1 h, 4 h, 18 h][attempts−1]. That is 6 sends over about 23 h; after the 6th failure the delivery becomes `failed` (criterion 80).
  - 3xx, or any other 4xx → `failed` immediately.
  - All of these are conditional `UPDATE … WHERE id=$1 AND leased_until=$lease`.
- **Crash.** An expired lease is re-claimed with the same `id` (criterion 83). Delivery is at-least-once: a crash between a 2xx and the commit re-sends the same delivery id, which the receiver dedupes on.
- **Logs.** Only the delivery id, the tenant id, the host (no path or query) and `failure_code`. Never the secret, the signature, the body or `extracted_variables` (criterion 82). The `httpx` and `httpcore` loggers are set to WARNING in Campaigns (lesson 20).

### Retention, erasure, masking (criteria 93–100)
- **Effective retention.** `LEAST(COALESCE(tenant.retention_days, platform.default_retention_days), platform.default_retention_days)`, read at purge time. A later reduction of the platform default therefore applies too.
- **`GET /campaigns/{campaign_id}`** adds `purge_at = ended_at + effective retention` for ended campaigns.
- **`purge_due_campaigns()`.** Runs hourly; it only acts if 24 h have passed since the last completed run, and it holds `pg_try_advisory_lock(hashtext('campaigns:purge'))`. It selects campaigns where `status='completed' AND purged_at IS NULL AND ended_at < now() - retention` (criterion 94: campaigns that have not ended are never selected). For each, it processes batches of 500 contacts where `pii_purged_at IS NULL`, one transaction per batch:
  1. `SELECT erase_call_pii(tenant_slug, session_ids_of_these_contacts_attempts)`.
  2. Null `callee` and `caller_id` on the attempts.
  3. Null `phone_number`, `name` and `consent_source` on the contacts, and set `pii_purged_at`.
  4. Pending `crm_deliveries` → `cancelled`/`purged`.
  5. Pending callbacks → `cancelled`.

  When no batch remains it sets `purged_at`. Statuses, outcomes, counts, dispositions and DNC rows are kept. Re-running is a no-op (criterion 95).
- **Erasure.** `POST /tenants/{tenant_id}/erasure` with body `{phone_number}` requires `require_role("superadmin","admin")`; a viewer gets 403. It always returns 202 `{"status":"processed"}`, whether or not the number exists (criterion 98, lesson 2). In one transaction:
  1. `e164 = to_e164(phone_number, tenant default)`.
  2. `campaign_contacts.match_number(conn, tenant_id, e164, *, mode: Literal["block","erase"]) -> MatchResult(exact: list[UUID], unconvertible: list[UUID])`. Within this tenant (explicit `campaigns.tenant_id = $tenant`), `exact` holds contacts whose `phone_number = e164`, plus legacy-format rows found through `idx_campaign_contacts_legacy_last10` on the same last-10 suffix whose `to_e164(stored, campaign.default_country or tenant default) == e164`. `unconvertible` holds suffix candidates whose stored value cannot be converted.
     - `mode="block"` (DNC cancellation, dial-time DNC check) acts on `exact + unconvertible`, which errs toward not calling.
     - `mode="erase"` acts on `exact` only, and never on the suffix alone. Each `unconvertible` candidate is inserted into `erasure_reviews` (`status='pending'`) for an admin decision. The erasure response is unchanged (criterion 98).
     - Purge selects by campaign, never by number, so its transcript and call erasure is exact by construction.
  3. `erase_call_pii` on the sessions of the `exact` contacts' attempts; null those attempts' `callee`.
  4. Pending deliveries → `cancelled`/`erased`.
  5. Null the contacts' personal fields.
  6. `dnc.add_number(tenant, e164)` with `ON CONFLICT DO NOTHING`.
  7. Audit `number_erased` with `{number_sha256: sha256(tenant_id || e164)}`.

  A second run changes nothing and writes another audit row.
- **Erasure reviews.**
  - `GET /tenants/{tenant_id}/erasure/reviews`: `require_role("superadmin","admin")`, returning pending candidates with `phone_masked`, campaign and the request's `number_sha256`.
  - `POST /tenants/{tenant_id}/erasure/reviews/{review_id}` with body `{"decision":"erase"|"dismiss"}`: same auth and the tenant 404 rule. `erase` runs steps 3–5 for that one contact and audits `number_erased`, with `review_id` in the details.
  - Nothing is erased on a suffix match without that confirmation.
- **Audit read.** `GET /tenants/{tenant_id}/campaign-audit` requires `require_role("superadmin","admin")` and applies the tenant 404 rule.
- **Masking (criteria 99, 100).** New code logs `mask_number(x)` and never the raw value. This also replaces the existing raw logs in `worker.py` (the DNC line) and `telephony_originate.py`, and covers the Telephony status webhook's logging of `to`/`from`. API reads return only `phone_masked`. The full number goes only to the CSV export and the CRM body.

### Outcome mapping
- **Path A `hangup_cause`, with `answered=false`:**
  - `USER_BUSY` → `busy`
  - `NO_ANSWER`, `NO_USER_RESPONSE`, `ALLOTTED_TIMEOUT` → `no_answer`
  - `CALL_REJECTED` → `declined`
  - anything else → `failed`
- **Path A `answered=true`:** `ended`.
- **Path B (Vobiz):**
  - `completed` → `ended`
  - `busy` → `busy`
  - `no-answer` → `no_answer`
  - `rejected`/`canceled` → `declined`
  - `failed` → `failed`
- **`answered` events:** Path A `api_on_answer`; Path B Telephony `on_session_start`.

### Event schema (`libs/telephony_sdk/campaign_events.py`)
```
encode(*, source: Literal["fs","cpaas"], kind: Literal["answered","hangup"], key: str | None,
       tenant_id: str | None, provider_call_id: str | None, call_session_id: str | None,
       cause: str, answered: bool, billable_ms: int | None) -> dict[str, str]
decode(fields: dict[bytes, bytes]) -> CampaignCallEvent
```
`tenant_id` is required for both sources. For `source="cpaas"` it comes only from the signature-verified account. For `source="fs"` it comes from the consumed intent, which only Campaigns can write; FreeSWITCH copies it into a channel variable. `apply_outcome` always matches `tenant_id = event.tenant_id` as well as the key, so an event that names another tenant's key changes nothing. `billable_ms` is set only by `source="fs"` hangups (from `billmsec`); Telephony always sends `None`, and Campaigns ignores the field for `cpaas` (A13). `source` is bound to the stream: `fs` events go only to `campaign:events:fs`, which only the `freeswitch` user can write, and the consumer derives `source` from the stream it read, so `telephony` or `conversation` cannot forge an `fs` event or a `billable_ms` (security HIGH 1, R4).

### HTTP
Campaigns routes use the auth from `routers/campaigns.py`:
- `GET /campaigns/{campaign_id}/readiness` → `Readiness`. Requires `get_current_user`.
- `GET /campaigns/{campaign_id}/attempts?contact_id=&limit=&offset=` → attempt rows without `callee`, with `billable_ms`, `cost_minor`, `cost_currency` and `billing_state` (A13). Requires `get_current_user`.
- `POST /campaigns/{campaign_id}/start|resume` → 409 `{"detail": {"checks": [...]}}` on any block, with status unchanged. Requires `require_role("superadmin","admin")`.
- `POST …/contacts/upload` → `{"inserted": n, "duplicates": n, "rejected": [{"line": n, "reason": str}]}`.
- `GET /campaigns/{campaign_id}/progress` additionally returns `{"wait_reason": WaitReason|null, "wait_since": ts|null, "waiting_count": n, "campaign_cap": n, "tenant_cap": n|null, "effective_limit": n, "in_flight": n, "spent_minor": n, "budget_limit_minor": n|null, "billing_currency": str|null}`. `waiting_count` is the number of pending contacts with `next_attempt_at` null or in the past. Routing goes through `get_campaign_for_caller`, so a foreign id gets the same 404 as a nonexistent one (criterion 54).

Capacity routes live in `services/campaigns/routers/capacity.py` on the existing Campaigns listener, using the auth dependencies from `services/config/deps.py`:
- `GET|PUT /platform/outbound-limits`: `require_role("superadmin")`. Body `{total_call_capacity, inbound_reserve, max_outbound_concurrent_calls}`. A violation of `platform_outbound_cap_fits` returns 400 "outbound cap exceeds capacity minus inbound reserve" and the row is unchanged (criterion 48). The GET also returns platform in-flight.
- `GET /platform/outbound-cps`, `PUT /platform/outbound-cps/{scope}` with body `{cps}`, `DELETE /platform/outbound-cps/{scope}`: `require_role("superadmin")`. The GET lists every scope with its current 1-second usage. For a `provider_account:` scope, the PUT requires that `telephony_configs` row to exist and be live (`deleted_at IS NULL`).
- `PUT /tenants/{tenant_id}/outbound-cap` with body `{max_outbound_concurrent_calls: int|null}`: `require_role("superadmin")` only. A tenant admin gets 403 and the value is unchanged (criterion 43). This is a separate route on purpose: the field is not added to any tenant-admin-writable update schema (lesson 4).
- `GET /tenants/{tenant_id}/outbound-cap` → `{tenant_cap, in_flight}`: `get_current_user`. A caller with a tenant whose `tenant_id` differs from the path gets the same 404 body as a nonexistent tenant. The route checks this explicitly before any lookup and does not rely on `require_path_tenant_access`'s 403 (criterion 54, lesson 2). Superadmin and NULL-tenant callers are scoped by `tenant_id IS NULL` (lesson 24).
- Every write is audited through `services/campaigns/audit.py`, recording the old and new value and the actor.

Telephony routes all use `Depends(require_telephony_caller)` and `await resolve_caller_tenant(user, tenant_slug)` (lesson 38):
- `POST /{provider}/call/reconcile` with body `{tenant_slug, idempotency_key, to, from, created_after}` → `{"outcome": "placed"|"not_placed"|"unknown", "provider_call_id": str|null}`.
- `GET /{provider}/call/{call_id}/status?tenant_slug=` → `{"status": "live"|"answered"|"ended"|"busy"|"no_answer"|"declined"|"failed"|"unknown", "to": e164|null, "from": e164|null, "billable_ms": int|null}`. `billable_ms` is the provider's billed answer-to-hangup duration, `null` when not reported. The provider is queried with that tenant's own credentials, so a foreign `call_id` returns `unknown`.
- `GET /{provider}/outbound-readiness?tenant_slug=&from=` → `{"ready": bool, "reason": str|null}`. Checks the account resolves, `supports_campaign_outbound`, and `check_health()`.
- `POST /{provider}/call` gains a 429 `{"error":"rate_limited","retry_after":s}` response and an optional body field `time_limit_s` (A13), passed to `initiate_call`.

### Kamailio
- **Dial sockets.** `listen=tcp:127.0.0.1:5071` and `listen=tcp:[::1]:5071` (criterion 31). The first statement in `request_route` is `if ($Rp == 5071) { drop; }`, so SIP is never processed on these sockets.
- **`event_route[xhttp:request]`.** The new first branch is `if ($Rp == 5071)`, and every path inside it ends in `exit`. The existing `/RPC` jsonrpcs branch is therefore unreachable from the dial socket, and jsonrpcs is never exposed to Campaigns (criterion 103). The checks run in order, and each failure answers immediately:
  1. `$si` must be in `CAMPAIGNS_DIAL_SRC` and `CAMPAIGNS_DIAL_HMAC` must be non-empty → otherwise 403 (criteria 33, 35).
  2. Path must be `/campaign-dial` or `/campaign-dial/health` → otherwise 404. Method must be POST → otherwise 405 (criterion 103).
  3. `Content-Length` must be ≤ 1024 → otherwise 400.
  4. `crypto_hmac_sha256("$rb", "CAMPAIGNS_DIAL_HMAC", "$var(mac)")` must equal the lower-case hex `X-Campaign-Signature` → otherwise 401.
  5. `jansson_get` each field. Missing, wrong type, or a parse failure → 400. Then `|$Ts − ts| ≤ 30` → otherwise 401.
  6. Field patterns: `key` `^[0-9a-f-]{36}_[0-9]{1,3}$`; `campaign_id`/`contact_id` UUID; `callee`/`caller_id`/`agent_did` `^\+[1-9][0-9]{6,14}$`. Any mismatch → 400. These patterns exclude CR, LF and every SIP-breaking character (criterion 102).
  7. Rebuild the canonical JSON from the extracted values and require byte equality with `$rb` → otherwise 400. This rejects extra keys and reordered or retyped fields (criterion 102).
  8. `ds_select_dst("1","4")` → if no node is active, 503 `{"result":"no_node"}`. `/health` stops here with 200 (it also requires `PSTN_GW_URI` to be non-empty).
  9. Set `$uac_req(method)="INVITE"`, `$uac_req(ruri)="sip:outbound-trigger@"+$dd`, `$uac_req(ouri)=$du`, `$uac_req(furi)="sip:campaigns@__LAN_IP__"`, `$uac_req(hdrs)="X-Call-Direction: outbound\r\nX-Idempotency-Key: "+key+"\r\n"` and `$uac_req(evroute)=1`. Call `uac_req_send()` (the tm `t_uac` path), then reply 200 `{"result":"accepted"}`.

  Only the pattern-checked `key` reaches a SIP header. Callee, caller ID and agent DID are validated but not forwarded; FreeSWITCH uses the intent's copies. The branch fails closed: any unmatched case returns 403, and no path inside it reaches `t_relay`, PSTN or jsonrpc.
- **`event_route[uac:reply]`.** Logs `$uac_req(evcode)` and the key only. Kamailio does not retry. A node that answers 5xx or times out leaves the intent unconsumed, so the Campaigns reconciler re-sends once, and round-robin plus dispatcher probing pick another node (criterion 28).
- **Header strip (criterion 104).** `remove_hf_re("^X-(Call-Direction|Idempotency-Key|Campaign-Id|Contact-Id|Callee|Caller-Id|Agent-DID)$")` runs on every request in `request_route`, right after `route(REQINIT)`. Kamailio's own `uac_req_send` INVITE does not pass through `request_route`, so the headers it adds are preserved.
- **PSTN egress (security HIGH 2).** The callee leg leaves FreeSWITCH through gateway `kamailio_egress`, so its From is `fs-egress@<kamailio domain>` and `from_uri==myself`.
  - The existing `route(AUTH)` (called at line 836; body at lines 1101–1133; `WITH_AUTH` defined, `WITH_IPAUTH` not) therefore challenges it and runs `auth_check` against `subscriber`.
  - A new `route(PSTN_EGRESS)` is called right after `route(AUTH)`. It acts only on `is_method("INVITE") && $au == "fs-egress" && $rU =~ "^\+[1-9][0-9]{6,14}$"`, and then also requires a valid per-call egress token (security MEDIUM 1):
    1. Split `$hdr(X-Egress-Token)` into `key.exp.tenant_id.caller_id.max_call_s.mac`. Missing or malformed (any field failing its anchored pattern) → 403.
    2. `exp ≥ $Ts`, otherwise 403.
    3. `crypto_hmac_sha256(key + "|" + $rU + "|" + caller_id + "|" + tenant_id + "|" + max_call_s + "|" + exp, "CAMPAIGNS_EGRESS_HMAC")` must equal `mac`, and `$(ai{uri.user})` (P-Asserted-Identity) must equal `caller_id`, otherwise 403. This binds the token to this exact callee, caller ID, tenant and time limit, with the same field order as `DialRequestClient`.
    4. `$sht(egress=>$var(mac))` must be unset, otherwise 403 (single use). Then set it with `autoexpire` 300 s; the htable module is already loaded.
    5. `remove_hf("X-Egress-Token")`, then `$ru = "sip:"+$rU+"@"+"PSTN_GW_URI"`, `setflag(FLT_ACC)` and `t_relay`. An empty `PSTN_GW_URI` or an empty `CAMPAIGNS_EGRESS_HMAC` gives 503, which fails closed.

    The digest credential alone is therefore useless for any leg that is not the intended campaign callee with its intended caller ID: a transfer, a dialplan bridge, or a reuse of a leaked password. Only Campaigns, which holds the egress secret, can mint a token, and the token is only reachable from a consumed intent. Changing the intent's callee, caller ID, tenant or `max_call_s` without the secret invalidates the token (Kamailio) or fails the Lua field match.
  - No other authenticated user (the 100x softphones included) ever matches. Source IP plays no part, so spoofing a dispatcher address on :5060 gains nothing without the digest password. Digest nonces expire under the `auth` module defaults, which bounds replay.
- **Loop guard.** At line 673, `src_ip == __LAN_IP__ && src_port == 5080` becomes `ds_is_from_list("1")`, so a transfer back from any pool node is still refused. Line 694 (100x) gets the same change. Line 794 is commented out and left untouched.
- **Removed from the earlier design.** The UDP trigger socket, `route[CAMPAIGN_TRIGGER]`, its failover route, and SIP source-IP trust.

### Redis ACL (security HIGH 3)
- **Bind.** Redis binds `127.0.0.1` (compose `127.0.0.1:6379:6379`) with `protected-mode yes` and `user default off`. When FreeSWITCH nodes run on other hosts, bind the internal interface only, with a host firewall that allows 6379 from the listed FreeSWITCH and service hosts only, and require TLS (`tls-port`) so ACL passwords never cross the network in the clear.
- **Users.** One user per client, each with its own password from env.
  - `freeswitch`: `on resetkeys -@all +auth +hello +ping (~campaign:intent:* +getdel) (~campaign:claim:* +set) (~campaign:events:fs +xadd)`. Redis 7 selectors scope each command to its own key pattern: it can only consume intents, set claims and append to its own stream. It cannot write an intent, delete a claim or read `did:*`.
  - `campaigns`: `on ~campaign:* -@all +get +set +del +exists +getdel +xreadgroup +xack +xautoclaim +xgroup|create +xadd +auth +hello +ping`. It is the only writer of `campaign:intent:*` and `campaign:cbprop:*`.
  - `telephony`: its existing idempotency prefix (`services/telephony/idempotency.py::_key`) with the commands it uses today, plus `~telephony:webhook_nonce:* +set` and `~campaign:events:cpaas +xadd`.
  - `conversation`: its existing patterns (provider-config pub/sub channel, config-repo keys, `did:*` read), plus `~campaign:events:conv +xadd` for `dnc_request`.
  - Gateway, Config, DID and Knowledge: each gets exactly its current key patterns and commands. These are derived mechanically by grepping each service's Redis calls and key prefixes (lesson 29).
- **Tests.** A test connects as each user and asserts its denied set. For example: `freeswitch GET did:x` → NOPERM; `freeswitch SET campaign:intent:x v` → NOPERM; `freeswitch GETDEL campaign:claim:x` and `freeswitch XADD campaign:events:cpaas` → NOPERM; `freeswitch` writing `campaign:cbprop:x` → NOPERM; `conversation GETDEL campaign:intent:x` → NOPERM; `telephony XADD campaign:events:fs` and `conversation XADD campaign:events:fs` → NOPERM; an unauthenticated `PING` → NOAUTH.
- **Rollout.** Every client's `REDIS_URL` gains `user:password`; the Gateway's C++ client reads the same URL. A client that cannot authenticate fails at startup, loudly.

### Listeners, reachability, auth (all new components)
| Component | Bind | Who can reach it | Auth | LAN-peer impact |
|---|---|---|---|---|
| Kamailio dial handler (`xhttp`) | `tcp:127.0.0.1:5071` and `tcp:[::1]:5071` only | Same host only | Source allowlist (empty means nobody) plus HMAC-SHA256 with a ±30 s timestamp. Can only dial; SIP, `/RPC` and other paths are refused | None |
| Kamailio public `udp:__LAN_IP__:5060` | Unchanged | Unchanged | Campaign headers always stripped. PSTN egress requires digest user `fs-egress` | Reduced: a spoofed source gains no egress |
| Campaigns → Kamailio dial | Outbound HTTP to `KAMAILIO_DIAL_URL` (loopback) | — | HMAC secret shared only with Kamailio | None |
| FreeSWITCH `external` :5080 | Unchanged | Unchanged (Kamailio and LAN) | Outbound branch requires `acl kamailio` and a `GETDEL` of an intent that only Campaigns can write. All dial values come from the intent | A spoofed INVITE can at most fire one already-intended call early; it can never choose the callee, caller ID or tenant |
| Redis | `127.0.0.1:6379`, or the internal interface plus firewall plus TLS when FreeSWITCH is on other hosts | Service hosts and the listed FreeSWITCH nodes | ACL user per client, `default` user off | It is now a trust anchor (intents, claims, events, `did:*`). A LAN peer without credentials gets nothing |
| FreeSWITCH `campaign_egress` profile | `udp:<node LAN IP>:5090`, used for egress only | Kamailio and LAN peers can reach it, but every inbound request on it gets 403 | — | None: nothing inbound is served |
| FreeSWITCH → Kamailio egress | Outbound SIP from each node's `campaign_egress` profile to Kamailio :5060 | — | Digest user `fs-egress`, plus a single-use HMAC egress token bound to callee, caller ID, tenant and `max_call_s` | A leaked digest password alone gains nothing |
| Telephony new routes | Existing Telephony listener (`LISTEN_HOST`) | As today | Service-account JWT via `require_telephony_caller` | None new |
| Telephony status webhook | Existing public route | Internet | Provider signature, verified before any Redis write | Unchanged |
| Redis Streams `campaign:events:fs`/`:cpaas`/`:conv` | Existing Redis | Internal | Each writable only by its one producer user (`freeswitch`, `telephony`, `conversation` respectively) plus `campaigns`. Every event carries a tenant that is matched on apply | Forging an event needs Redis credentials |
| Internal tool routes (`/internal/calls/*`) | Existing Campaigns listener (`LISTEN_HOST`, loopback default) | Conversation on the same host | Service-account JWT, `is_service_account AND tenant_id IS NULL` | None new: loopback |
| Conversation → Campaigns | Outbound HTTP to `CAMPAIGNS_SERVICE_URL` (default `http://localhost:8400`) | — | Conversation's existing service-account JWT | None |
| CRM delivery egress | No listener. Outbound HTTPS from Campaigns to tenant URLs | Public IPs only: `egress_guard` resolves once, refuses any private, loopback, link-local or metadata address, connects to the validated IP, and does not follow redirects. The operator allow-list is disabled for this client | HMAC-SHA256 per-tenant secret (criterion 78); TLS verification on the original hostname | A tenant cannot make the platform reach LAN peers. Recommended host egress rule: Campaigns may reach the internet on 443 and internal services only on their ports |
| Settings, erasure, export, uploads, audit routes | Existing Campaigns listener | As the Campaigns API today | `get_current_user`; writes need `require_role("superadmin","admin")`; platform settings need `superadmin` | None new |
| Campaigns purge job | No listener | — | Postgres only | None |
| DND registry hook | No listener and no egress (in-process `AllowAllRegistry`) | — | — | A real registry would add egress needing its own review |
| Billing admission client (A13) | No listener. `NullBillingAdmission` does no I/O; the real client's transport is billing's design | Campaigns worker only, control path | Defined by billing | None while Null; billing's review covers its transport |
| Capacity routes | No new listener: they run on the existing Campaigns listener (`LISTEN_HOST`, loopback by default per `tests/test_local_binds.py`) | As the Campaigns API today | JWT via `get_current_user`; writes need `require_role("superadmin")` | None new. Enforcement is in-process in the Campaigns dial path; Gateway and Conversation make no new calls (criterion 56). |

## Risks
- Task 0 may find no candidate meets criteria 1–3. Mitigation: Path A is gated on Task 0; schema, Path B and readiness ship without it, and the failure goes back to the owner rather than being guessed.
- `mod_hiredis` is a new FreeSWITCH module dependency (justification: FreeSWITCH must claim the key and emit outcomes without Campaigns or ESL in the loop, and Redis is the only shared store reachable from the call node). Mitigation: Task 0 verifies the module builds and loads from `/opt/src`; on a Redis error the Lua fails closed (`rejected`, no dial).
- Redis without AOF loses un-acked stream entries on restart. Mitigation: `--appendonly yes` in compose. Residual loss is covered by the deadline reconciler (call record, provider status). A lost Path A `declined` would resolve as `failed`. It is retried exactly as `declined` would be, so the only loss is the label, surfaced as `failure_reason='outcome_not_observed'`.
- A late answer for attempt N can arrive after attempt N+1's dial request has already left, within the sub-second window between `claim_next_due` commit and `send`. Mitigation: A2 makes deadline-failure of a live call impossible except when Redis lost the claim. `claim_next_due` refuses contacts with an answered attempt, and attempt N+1 is marked `superseded`. The residual is one extra call in a Redis-loss-plus-race case, which is stated rather than claimed away.
- Without an ESL query, a FreeSWITCH node crash mid-ring leaves the claim alive for up to 105 s, holding the slot. Mitigation: the bound is the claim TTL, which is no longer than the deadline, then the call resolves as `failed` with a retry (A1).
- A LAN peer can still send a spoofed INVITE to FreeSWITCH :5080 with Kamailio's address as the source. Mitigation: the Lua dials only after `GETDEL` of an intent that only the `campaigns` Redis user can write, and takes every dial value from that intent, so a spoof can at most fire one already-intended call early. The PRD's "FreeSWITCH SIP ports not reachable from LAN peers" stays a host-firewall item for the owner.
- Redis ACL auth touches every client (Gateway, Config, DID, Knowledge, Conversation, Telephony, Campaigns, FreeSWITCH), and a missed client fails at startup. Mitigation: per-client users are derived mechanically, a CI test checks the per-user allowed and denied matrix, and `start_local.sh` checks every `REDIS_URL` with AUTH plus PING before starting anything.
- `mod_hiredis` may not support ACL usernames. Mitigation: Task 0 verifies it; the fallback is a recorded patch in `/opt/src`, and Path A does not ship without it.
- `jansson`, `crypto` and `uac` are new Kamailio modules (justification: the owner chose JSON over HTTP, so Kamailio must verify the HMAC and send the INVITE itself). Mitigation: Task 0 verifies them, and the handler is one branch with no path to jsonrpc or `t_relay`.
- The HMAC secret now sits in the rendered Kamailio config. Mitigation: the file is written `0600`, the secret is never logged, and rotating it means re-rendering and restarting both Kamailio and Campaigns.
- PSTN egress needs the `fs-egress` credential and `PSTN_GW_URI`. If either is missing, Path A fails with 503. Mitigation: it fails closed, and readiness `no_healthy_sip_node` detects it through `/campaign-dial/health`.
- No app-level rate limiter exists on Telephony webhooks, and this design adds none. Mitigation: the signature check runs before any Redis or DB work, so an unsigned flood costs one HMAC per request. The PRD's "rate-limited" item belongs at the public ingress; it is listed here so it is not silently dropped (lesson 7).
- `ani` becomes the callee on outbound, where the ESL hairpin used to make it the agent DID. Mitigation: the implementer greps `caller_did`/`ani` consumers in `services/conversation` and `services/toolexec` and confirms each branches on `direction` (lesson 44); a test drives an outbound session through `gcal_find_booking`.
- The existing by-id campaign routes return 403 for a foreign campaign but 404 for a missing one, which is the criterion 42 leak. Mitigation: all `{campaign_id}` routes move to `get_campaign_for_caller`. A test walks `router.routes` mechanically, asserts the walk's size, and checks per-caller invariance (lessons 2, 29).
- Removing `/internal/vobiz-call-resolved` breaks any caller that was not found. Mitigation: grep `services/`, `scripts/`, `docs/`, `admin-ui/` before deleting (lesson 17); the only known caller (`services/telephony/app.py` `_notify_campaigns_best_effort`) is replaced in the same change.
- The changed Kamailio FreeSWITCH-origin test (`ds_is_from_list("1")`) affects inbound loop prevention. Mitigation: the existing 500x inbound and transfer flows are re-run live on one node, and on two nodes, before merge.
- The global advisory lock serialises admissions across all tenants and workers. Mitigation: it is held only for the admission transaction (a few indexed counts on partial indexes and one insert). Throughput is already bounded by CPS and the 2 s tick; the lock wait is logged at DEBUG.
- The CPS window uses `clock_timestamp()` on one Postgres primary, so it is exact for every worker. It counts reserved send starts, not SIP INVITEs that actually reached the carrier. Mitigation: a reservation that fails to send still occupies its slot for 1 s, which errs toward under-sending, never over.
- The trunk scope is a single `sip_trunk:pstn` because Kamailio has one PSTN gateway today. Mitigation: if Kamailio gains multiple egress routes, a new scope value plus a DID-to-route mapping is needed; the scope CHECK already allows other `sip_trunk:` names.
- Per-turn latency (criteria 30, 84). Mitigation: no Gateway change. Billing (A13) adds no call path work: FreeSWITCH only gets one `execute_on_answer` variable, and a tripwire asserts no module under `services/conversation`, `services/gateway` or `scripts/freeswitch` references billing. In Conversation, the only call-path changes are two tool definitions offered on outbound turns, which add at most about 160 prompt tokens, and their handlers, which run only when invoked. Verified by the A4 SQL comparison on an agent with both tools offered.
- `phonenumbers` is a new dependency (justification: criterion 89 requires per-country E.164 validity, and hand-maintaining numbering-plan metadata would be wrong more often than the library). Mitigation: it is pure Python, offline, and used only on the cold path, through `did_format.to_e164`.
- Legacy DNC rows keep blocking by last-10 suffix until an admin confirms the conversion, so they over-block numbers in other countries that share those digits. Mitigation: this fails safe toward not calling (criterion 91). The conversion adds E.164 rows and never deletes or rewrites anything, and an admin retires the suffix rows explicitly.
- Legacy contacts stored without `+` are matched by suffix candidate and then exact `to_e164(stored, campaign/tenant country)`. Unconvertible candidates block dialing (safe) but are never erased automatically: they go to `erasure_reviews` for an admin. Mitigation: an unanswered review leaves that contact's personal data in place until it is decided or the campaign purge removes it. New uploads are always stored in E.164.
- `CURRENT_TURN_ID` only proves "a new caller turn" if every `ToolCallOrchestrator.run` call site is driven by a caller utterance. Mitigation: a tripwire test enumerates every call site of `ToolCallOrchestrator.run` by AST, asserts the count, and asserts each is on the caller-turn path, so a new non-utterance entry point fails until it is reviewed.
- The time cross-check recognises English terms only. A non-English phrase with no digits is `unparseable`, so the agent asks again. Mitigation: digits are language-neutral, and the error never books anything. Hindi relative terms are a follow-up, not claimed here.
- A DNC request can be lost if the Campaigns write and the Redis `XADD` both fail. Mitigation: the agent is told it failed and does not claim success; the ERROR log carries the session id for manual follow-up; and the caller can be added from the transcript. This needs Postgres and Redis down together.
- CRM delivery is at-least-once. Mitigation: a stable `X-Yuviz-Delivery-Id`, documented for receiver dedupe (criterion 79).
- Extracted variables can be sensitive, and they reach three sinks: the contact list (viewers included, per criterion 57), the CSV export and the CRM body. Mitigation: they are never logged; the export is audited; and purge and erasure null them at the source (lesson 33).
- Call and transcript data outside campaigns is kept indefinitely (A11). Mitigation: this is unchanged from today and stated as out of scope; a default-off platform retention setting is the noted future option.
- The erasure audit hash (`sha256(tenant_id || e164)`) can be brute-forced by anyone who can read the audit log. Mitigation: only that tenant's admins can read it, and they already held the number. The PRD asks for a hash, not a keyed MAC.
- Billing is not built, so the seam contract is unverified against the real implementation. Mitigation: the contract is executable (`test_billing_contract.py`) and billing's client must pass it before `NullBillingAdmission` is swapped out.
- Capping each call at its grant (A13) cuts a long call abruptly with no farewell, and the 2 h agent default is unreachable while billing is on. Mitigation: readiness warns `call_length_capped` with both numbers; the owner confirms cap over extend.
- `end_not_observed` is settled at the cap, which overcharges when the real call was shorter. Mitigation: it needs both the hangup event and the `calls` end to be lost; `billing_source='cap'` marks the row for review, and the choice is open item 4.
- A settle can post after its hold was released or expired (late-answer upgrade, a Path B attempt held back past its hold by 429 backoff), so the balance can go negative by at most one call capped at `max_call_s`. Mitigation: settle-after-release is in the contract, and the bound is the cap.
- A budgeted campaign counts each `reserving` row at the whole remaining budget until its reserve returns, and each open hold at its full amount, so concurrency is limited to about remaining budget ÷ one hold. Mitigation: that is what guarantees spend never exceeds the budget; the UI shows `budget_reached` as the wait reason.
- A billing outage stops all campaign dialing (`billing_unavailable`). Mitigation: intended fail-closed behaviour; no attempt is consumed and dialing resumes on the next tick after recovery.
- Billing's reservation reconciler can release an authorized hold if Campaigns is down past `expires_at`. Mitigation: `billing_state` is durable, `_billing_sync` settles on restart, and settle after release still posts.
- Path B call capping depends on Vobiz honouring `time_limit`, unverified. Mitigation: `supports_time_limit` stays False until verified, and readiness blocks `provider_cannot_cap_call` while billing is on.
- The pre-start estimate uses one destination country and a 60 s default duration. Mitigation: it is labelled with its source and only drives a warning, never a block.

## Test plan
**Unit** (`services/campaigns/tests`, `services/telephony/tests`, `libs/telephony_sdk/tests`):
- `apply_outcome` transition table: every (current, new) pair, including duplicates, out-of-order events, foreign `tenant_id` (no row changed, `None`), and the deadline-failed upgrade.
- Contact-status rule: `declined` with attempts left → `pending` with `next_attempt_at = resolved_at + retry_gap` (identical to `busy`). `declined` on the last attempt → contact `declined`, not re-queued. In both cases the attempt's outcome stays `declined`.
- `claim_next_due` respects N under two concurrent callers. `capacity.admit` returns the first applicable reason, in the documented order.
- CSV normalisation and dedupe.
- `schemas` validators.
- `DialRequestClient` rejects intent or body fields that contain CR/LF/NUL, and numbers that are not E.164 (carried over from `test_originate.py`). The canonical body and HMAC vectors match the Kamailio test. Result mapping: 200 → `accepted`; 400/401/403/404/405 → `rejected`, with the intent deleted before the failure; 503 → `no_node`, with the intent deleted; a timeout → still `pending` with no re-send; a re-send against a consumed intent → `duplicate`, with no HTTP request made.
- Time limit, billing off (`NullBillingAdmission`): a Path A intent carries `max_call_s` equal to the agent's `max_call_duration_s` (7200 when NULL). Billing on: a grant below the agent max lowers it; a grant above leaves it at the agent max.
- Egress token vectors: the Python HMAC over `key|callee|caller_id|tenant_id|max_call_s|exp` matches Kamailio's; changing any one of those fields fails verification.
- `campaign_events` encode and decode, plus a tripwire that the field names in `campaign_event.lua` equal `encode()`'s keys.
- Telephony 429 path releases the idempotency key.
- Billing seam (A13): the `apply_outcome` `billing_state` table, every (state, outcome) pair, including `settled` never moving and a duplicate event moving nothing. `test_billing_contract.py` against the ledger fake. With `NullBillingAdmission`, the whole existing suite passes with every attempt `billing_state='off'` and zero billing calls recorded.

**Integration (real Postgres and Redis, fake SIP endpoint and fake provider), the cases that matter:**
1. **Crash matrix.** Kill the worker at each of: after the `pending` commit; after send, before `mark_sent`; after the FreeSWITCH claim, before dial (fake); while ringing; after answer; with events left un-acked in the stream. Add a delayed restart: the worker is killed after send and before `mark_sent`, the fake FreeSWITCH claims and dials but its hangup event is dropped, and the worker restarts after 105 s, once the claim has expired. Assert no re-send under the key (zero `reserve_send` rows), resolution as `failed` at the deadline, and attempt+1 with a new key. Add a `timeout` case: Kamailio accepts but the HTTP reply is lost, and the fake FreeSWITCH consumes the intent and dials. Assert there is no second dial request while the claim exists, and that a re-send after the consume returns `duplicate` without making an HTTP request. Add a lost-INVITE case: the intent is never consumed. Assert exactly one re-send within 60 s, then the deadline path. On restart, assert exactly one call per key, no redial after answer, every contact out of `calling` by its deadline, and campaign `completed` at the end (criteria 15–23).
2. **Late answer.** The deadline passes with no call evidence and the retry is queued. An `answered` event for attempt 1 arrives. Assert the outcome becomes `answered`, the contact is `completed`, and the fake SIP or provider sees no second call (criterion 12 / A3).
   Also: a Path A `CALL_REJECTED` hangup and a Path B `rejected` webhook each record `declined`, and the contact is redialed as attempt+1 no sooner than `retry_gap_minutes` (not the pacing interval). With `max_attempts` reached, the contact ends `declined` and is not redialed.
3. **Live-signal guard.** A claim exists, there is no `calls` row, and the deadline passes. Assert the attempt is not failed and in-flight stays at N; after the claim expires it fails and retries as attempt+1 with a new key (A1, A2).
4. **Webhook trust.** A bad or missing signature returns 403 and the stream is unchanged. Replaying a captured valid request (same signature and nonce) → 403 the second time, with exactly one stream entry. Tampering, with a valid signature and nonce: a Vobiz request whose body `status` is changed to `completed`, or whose `?idem=`/`event=` names another attempt, still applies only the provider API's status. With the fake provider reporting `busy`, the attempt records `busy`; with the fake reporting `to` ≠ the attempt's callee, the hinted attempt is unchanged. A terminal webhook while the API says `live` → no change. A valid tenant-B webhook naming a tenant-A key changes no row and gets a byte-identical response to an unknown key (criteria 37, 38, 40).
5. **Capacity (criteria 43–56).** Real Postgres, deployed tick and constants (lesson 25).
   - (a) Two worker processes with separate pools run `claim_next_due` concurrently for two campaigns of one tenant with tenant cap 5 and each campaign at 5. Sample the in-flight count after every commit and assert it is never above 5. Assert the sample count is greater than 0, so the check can fail.
   - (b) Tenant A is at its cap and tenant B still admits.
   - (c) The platform cap M is reached: due contacts keep `status='pending'`, there is no new attempt row, and `attempt_count` is unchanged.
   - (d) Trunk CPS of 2 is shared by campaigns of two tenants, and provider-account CPS of 2 likewise: no 1-second window contains more than 2 `last_send_at` values.
   - (e) Lower a cap mid-run: no attempt is ended, and admission resumes only once usage is below the new value, on the next tick and without a restart.
   - (f) Kill both workers with attempts open and restart: in-flight is recomputed from rows and no limit is exceeded.
   - (g) One test per `WaitReason` value, iterated from the `Literal` so a new reason cannot be skipped, each asserting `progress.wait_reason` and `waiting_count`.
   - (h) Saving a platform cap above capacity minus reserve returns 400 and leaves the row unchanged.
   - (i) Each capacity write route returns 403 for a tenant admin, with the value unchanged.
   - (j) For one fixed tenant-B principal, `GET /tenants/{A}/outbound-cap` and A's campaign progress return the same status and body as a random UUID.
   - (k) The readiness warning `campaign_cap_above_tenant_cap` states both numbers.
6. **Disposition, export, uploads.**
   - A contact with a linked call that has a disposition shows it alongside `answered`; one without a call shows null.
   - The outcome and disposition filters each return only matching rows of the caller's tenant.
   - In the CSV, every cell starting with `= + - @ \t \r` is prefixed, and the export writes one audit row with the right count.
   - A tenant-B principal gets the same 404 on tenant A's export as on a random UUID.
   - Upload without attestation → 400 and zero rows.
   - Upload with attestation stores `sha256`, the actor and the counts, and viewers get 403 reading uploads.
   - A row longer than 200 characters is rejected; a missing `consent_source` column is fine.
   - Duplicates after normalisation are stored once.
   - A purpose outside the enum → 422.
7. **Callbacks.**
   - With a frozen clock in the campaign timezone: "tomorrow at 3" with `local_datetime` for the day after → `time_mismatch`.
   - The correct date → `proposed`, with a server-formatted `spoken_time`.
   - Past, 31 days out and gibberish → each returns its error with zero rows.
   - 22:00 outside a 09:00–18:00 window → the next 09:00 with `adjusted`.
   - `confirmed=true` with no or a foreign `proposal_id` → `proposal_expired`, nothing stored.
   - Same-run test: a scripted LLM calls `schedule_callback(confirmed=false)` and then `schedule_callback(confirmed=true, proposal_id=…)` inside one `ToolCallOrchestrator` run, so both read the same `CURRENT_TURN_ID`. The result is `confirmation_needs_caller_turn` and nothing is stored. The next run, started by a new caller utterance, confirms → `scheduled`. Also: a handler invoked outside any run (`CURRENT_TURN_ID` is `None`) returns `FAILED`/`no_turn_context`. A `session_id` or `tenant_id` in the model's arguments is ignored in favour of the captured values.
   - A second callback replaces the first (exactly one `pending`).
   - The 4th → `callback_limit_reached`.
   - A model-supplied `contact_id` is ignored.
   - `max_attempts=1` exhausted plus a due callback → dialed with `is_callback`, and `attempt_count` unchanged.
   - DNC added before the time → callback cancelled, no attempt.
   - Store failure (DB down) → the handler returns `FAILED`/`callback_not_saved`.
   - Inbound sessions get neither tool, which is asserted on `_local_tools`.
8. **DNC.**
   - A request during the call inserts the E.164 row before hang-up and cancels other pending retries and callbacks for that number in the tenant; tenant B's identical number is unaffected.
   - A repeat request → still one row and `recorded`.
   - With the Campaigns route failing, a `dnc_request` event is applied after recovery, sets `review_reason`, and the contact is not redialed.
   - `+14155550100` does not match `+9114155550100`.
   - The migration fixture holds a legacy `4155550100`, two legacy rows that share a suffix (`+1 415-555-0100`, `(415) 555-0100`) and an E.164 row. After the schema apply: the row count is unchanged and no row text is rewritten. `+9114155550100`, a foreign number that the legacy row blocked before migration, is still blocked. So is `+14155550100`.
   - After the tenant saves country `US`: `+14155550100` rows are added with `legacy_of`, and the foreign number is still blocked. `GET conversion-preview` reports `retire_count=1` (with the foreign contact counted in `newly_dialable_count` when it exists as a contact). A `POST confirm-conversion` with a wrong `expected_retire_count` → 409 and nothing changed. With the right count (admin; viewer 403), `+9114155550100` is allowed, while `+14155550100` stays blocked. Run against a DB that actually holds the colliding rows (lesson 10).
9. **CRM.**
    - With `KNOWLEDGE_PROVIDER_ALLOWED_HOSTS` set to its default (local Ollama, `localhost:11434`), `https://localhost:11434/hook` is refused both on save and on send. Existing Knowledge callers still reach the allow-listed host (defaults unchanged).
    - Saving or sending to `http://…`, `https://127.0.0.1`, `https://10.0.0.5`, `https://169.254.169.254`, a hostname resolving to `10.x`, and a URL that 302s to `127.0.0.1` → each rejected, with no connection to the internal address. Assert with a local listener whose accept count is 0.
    - The signature verifies with the tenant secret over `ts.body`.
    - A 500 receiver gives 6 sends with the scheduled gaps (the clock is advanced in a test double of `next_attempt_at`; the deployed schedule constants are unchanged), then `failed`. A 400 is failed after 1 send.
    - A worker killed mid-lease → re-sent with the same delivery id; delivered rows are never re-sent.
    - Settings responses never contain the secret; the secret is stored with `is_tenant_bound`.
    - `crm_webhook_url` per role, with a URL that has a path and a query token:
      - Tenant admin GET → scheme and host plus `path_set: true`, with no path or query.
      - Viewer and superadmin GET → scheme and host only.
      - NULL-tenant service account GET → the field is absent.
      - Admin `PUT` response → the full URL.
      - A foreign tenant → 404.
      - The test asserts that the path and query substrings appear in no GET body for any role.
    - A blocking receiver (sleeps 10 s) does not delay `claim_next_due` ticks.
    - Logs captured over all of the above contain no secret, signature or variable value.
10. **Retention and erasure.**
    - A campaign ended 91 days ago with the default retention is purged: contact, attempt and call PII null, transcripts gone, DNC and counts kept. A running campaign is untouched.
    - Run twice or interrupted mid-batch → resumes and is a no-op on the second run; pending deliveries → `cancelled`/`purged`.
    - Tenant retention above the platform default → 400. A viewer → 403.
    - Purge date shown, with the 14-day banner.
    - A legacy contact stored as `98765 43210` in a campaign with `default_country='IN'`: erasing `+919876543210` nulls it, and adding that number to DNC cancels its pending retry. A tenant-B contact with the same digits is untouched.
    - A legacy contact `9876543210` in a campaign with no country and no tenant default (unconvertible): erasure leaves it intact and creates one `erasure_reviews` row, and the erasure response is byte-identical to a no-match request. Adding the number to DNC still blocks its retry. A review decision of `erase` then nulls it; `dismiss` leaves it.
    - Erasure removes the number everywhere for tenant A only, adds it to DNC, and writes an audit row with the hash. A second run produces identical output and another audit row. A nonexistent number gets an identical response. A viewer gets 403.
    - Audit rows reject UPDATE and DELETE.
    - `erase_call_pii` column coverage: the test reads `information_schema.columns` for `calls` and asserts every column is in exactly one of two hand-kept lists, `PERSONAL` (`caller_number`, `called_number`, `extracted_variables`, `sentiment_reason`, `recording_ref`, `call_id`) or `NOT_PERSONAL`. It asserts the union equals the column set, so a new column fails the test until it is classified. After a call, every `PERSONAL` column is NULL. Inbound and non-campaign calls of the same tenant are unchanged (A11).
11. **Masking tripwire (criterion 99, lesson 42).**
    - A runtime test drives each entry point with a known number and captures all log records: the dial loop (both paths), the Telephony status webhook, the CRM delivery, the upload, the erasure, both internal tool routes, the purge, and the DNC add. It asserts the full digit string appears in no record and that the masked form appears at least once (so the check can fail).
    - A second AST test walks `services/campaigns`, `services/telephony/app.py` and `services/conversation/tools/campaign_tools.py` for logging calls whose arguments name `phone|number|callee|caller_id|to|from` without `mask_number(`. It asserts that the walk visited more than 0 logging calls, and that its list of entry points equals the set of routes and tasks the test enumerates from `app.routes` plus the named background tasks.
12. **Readiness and tenancy.** Each block id is produced by its fixture, including a cross-tenant agent and an inactive agent, both with the same message. Start/resume return 409 with status unchanged. For one fixed tenant-B principal, every `{campaign_id}` route returns the same status and body for tenant A's id as for a random UUID, with the route list derived from `router.routes` and its size asserted (criteria 1–5, 42).
13. **Billing seam (A13; ledger-backed fake `BillingAdmission` that records every call).**
    - No credit: `wait_reason='insufficient_credit'`, zero attempt rows, `attempt_count` unchanged, a due callback still `pending`. After a top-up the next tick dials with the same idempotency key and a new billing key. `Refused("concurrency")` → `tenant_cap`; a reserve that hangs 5 s → `billing_unavailable`, `release` called, no attempt consumed.
    - Forged money write (security HIGH 1, R4): an entry with `source=fs`, `billable_ms=999999999` XADDed to `campaign:events:cpaas` or `:conv` is acked and dropped with no row change; a genuine `:fs` hangup with `billmsec` above `max_call_s·1000` is stored and settled at exactly `max_call_s·1000`; the ledger fake charges no more than the grant when `settle` is called directly with a larger value.
    - Answered Path A call with `billmsec=61234`: exactly one `settle(…, 61234)`. Re-deliver the hangup event three times and run `_billing_sync` twice: still one ledger charge. Kill the worker between `settle` and the local commit: re-settle returns the first `Settlement` and `spent_minor` moves once.
    - `busy`, `no_answer`, `declined` and deadline `failed` each release and never settle. Late-answer upgrade after release: exactly one settle.
    - Path B: the webhook says `completed` with `duration=999` while the fake provider API reports `ended` with a billed duration of 42 s: settled `42000`, `billing_source='provider_api'`.
    - Trigger leg: per Path A key, settles = 1 for an answered callee and 0 when the intent was consumed but the callee never answered; no settle is keyed to the trigger INVITE.
    - Budget equal to 2.5 holds with campaign cap 5 under two workers: `spent_minor` + open holds never exceed the budget at any sample; once `spent_minor` reaches it the campaign is `paused` with `budget_reached`, and resume returns 409 until the budget is raised.
    - Readiness: `billing_account_missing` blocks; `low_balance` warns when the balance is below `estimate.total_minor`; with `NullBillingAdmission` no billing check appears and `estimate` is null.
    - Crash in `reserving`: after 30 s, `_billing_sync` releases and unclaims; a slow original reserve returning `Granted` afterwards releases and sends nothing.

**Live (two FreeSWITCH nodes, Kamailio, sipp from a second host):**
- Dial handler: none of these sends an INVITE (`kamcmd tm.stats` unchanged).
  - Wrong HMAC or stale `ts` → 401. Non-allowlisted source → 403.
  - Each criterion 102 case → 400: malformed or oversized JSON, a missing, extra or retyped field, a non-E.164 number, a CR/LF value.
  - Any other path → 404, other methods → 405, and `/RPC`, `core.*` and `ps` → 404 (criterion 103).
  - An empty secret or allowlist refuses everything (criterion 35).
  - A port scan from another host finds 5071 closed (criterion 31).
- Header strip: an inbound INVITE on :5060 carrying every campaign header reaches FreeSWITCH with none of them (criterion 104).
- Spoofed INVITE to FreeSWITCH :5080 from a second host using Kamailio's source IP:
  - With a valid-looking key but no intent → 403, no dial.
  - With extra `X-Callee` headers alongside a real intent's key → the intent's callee is dialed, never the header value.
- PSTN egress:
  - From a 100x softphone subscriber → not routed to `PSTN_GW_URI`.
  - From a spoofed FreeSWITCH address without digest → refused.
  - Via `kamailio_egress` with a valid token → routed. With no token, a reused token, an expired token, or a token for another callee, caller ID (PAI mismatch), tenant or `max_call_s` → 403.
  - A warm and a cold transfer from a live campaign call to a PSTN-format number go out by their existing path, and `kamailio_cdr` shows no `fs-egress`-authenticated INVITE for them (security MEDIUM 1).
- Redis ACL: the per-user allowed and denied matrix passes against the running Redis.
- Ten campaign calls: each callee leg and its audio fork are on the same node, `calls.direction='outbound'`, `ani` = callee, and there is no second leg to the agent DID.
- A transfer bridges on the same node.
- Billing cap (A13): with a fake grant of 40 s, the call hangs up about 40 s after answer with `ALLOTTED_TIMEOUT`, the outcome is `ended`, and `billable_ms` is within 1 s of the carrier CDR, for 10 calls.
- One node stopped: dial requests go to the other node, and a request already sent to the stopped node is re-sent once from the unconsumed intent. With both nodes down the result is `no_node`, the attempt fails, and it retries after the gap (criteria 25–29, 31–35).
- Restart FreeSWITCH while Campaigns runs: Campaigns CPU stays at idle (criterion 24).

**Latency** (criterion 30 / A4):
```sql
SELECT direction,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY t) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY t) AS p95
FROM (
  SELECT c.direction,
         (te.latency_ms->>'stt')::int + (te.latency_ms->>'llm')::int + (te.latency_ms->>'tts')::int AS t
  FROM transcript_entries te JOIN calls c USING (session_id)
  WHERE c.agent_id = $1 AND c.started_at::date = $2
) x
GROUP BY direction;
```
Pass: outbound p50 ≤ inbound p50 + 50 ms, outbound p95 ≤ inbound p95 + 100 ms, with at least 20 turns each.

## Prototype result (Task 0, run 2026-10-21)

**Chosen candidate: T2.** Every criterion passed on the first candidate, so T3 and T1 were not run. Phase 4 is not halted.

**Reply.** Kamailio `uac_req_send` INVITE with no SDP, answered by `respond "603 Decline"` with `sip_rh_X-Campaign-Result: accepted|duplicate|rejected`. `event_route[uac:reply]` sees `$uac_req(evcode)=603` and reads the header (all three values observed). Code is 603, one code for all three outcomes.

**Versions.** Kamailio 6.2.0-dev1 (090a19), `jansson` 2.15.0, `crypto` on OpenSSL 3.6, `uac`, `xhttp`, `dispatcher`, `htable`, `tm`. FreeSWITCH 1.10.13-dev (git 8babcee), `mod_hiredis` from `/opt/src` at that commit plus the username patch, hiredis 1.3.0. Redis 8.8.0 (Homebrew; the design says 7, ACL selectors are the same).

**Rig (isolated, scratchpad only).** Redis 127.0.0.1:16379 with its own aclfile. Kamailio udp 127.0.0.1:15060, dial tcp 127.0.0.1:15071, ctl tcp 127.0.0.1:15099. FreeSWITCH with its own conf, db, log and run dirs: `external` 15080, `campaign_egress` 15090, stand-in carrier `pstn_stub` 15100, ESL 18022, RTP 40000-40400. A logging UDP relay on 15081 sat between Kamailio and FreeSWITCH to see ACKs. The live FreeSWITCH, Kamailio and Redis were not touched.

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | No RTP for the INVITE channel | PASS | Channels and RTP sockets 0 before and after 20 rejected dials. During an accepted call: 2 channels (callee leg, stand-in carrier leg), 2 RTP sockets, none for the trigger. Trigger channel log has 0 RTP/SDP/codec lines. |
| 2 | No acc row, `calls` row or Gateway session | PASS (partly proxied) | 0 `acc` log lines for the trigger Call-ID (the rig logs acc instead of writing `kamailio_cdr`). The trigger channel runs only `start_campaign_call.lua` then `respond`. The rig has no Gateway, so a `calls` row or Gateway session is shown only by the trigger never running a conversation script. |
| 3 | Callee leg survives, conversation completes | PASS (stand-in conversation) | Trigger gets 603 and is gone while 2 legs stay up. A 2.5 s Lua conversation completes and hangs up. The hangup hook XADDs `campaign:events:fs`. The real AI conversation was not run. |
| 4 | tm ACKs the non-2xx, `uac:reply` fires, no leak | PASS | 100 dials: 100 INVITEs and 100 ACKs seen at the relay, 0 retransmits, 100 `uac:reply` events with 603. `tm.stats` after: current 0, waiting 0, created 327 = freed 327. The "before" sample still had 2 in-flight transactions from the previous call, so this is "no residue", not "equal to before". |
| 5 | Duplicate POST makes no second dial | PASS | Sequential repeat: second reply `rejected`. 10 concurrent POSTs of one key: 1 dial, 9 `rejected`. New intent written for a claimed key: `duplicate`, no dial. |
| 6 | Reply within 200 ms | PASS | 100 POSTs: p50 0.9 ms, p95 1.4 ms, max 1.7 ms (loopback). |
| 7 | Kamailio's INVITE never reaches the carrier | PASS | The carrier saw only `fs-egress` INVITEs to E.164 numbers, one per admitted token. 16 hostile shapes produced 0 carrier INVITEs: trigger-shaped From 403, no credentials 407, wrong password 403, leaked password with no/malformed/other-callee/other-caller-ID/no-PAI/expired/wrong-secret/tampered-max_call_s/tampered-tenant/reused/non-E.164 token all 403 (reused token, reused on another callee included). A valid token with PAI gave 200 (positive control). |
| 8 | Spoofed INVITE to FreeSWITCH dials nothing | PASS | Source 127.0.0.1 (the `kamailio` ACL address) with a valid-looking key and no intent: 603 `rejected`, 0 originates. Same with forged `X-Callee`, `X-Caller-Id`, `X-Egress-Token`; a malformed key; an intent whose egress token was tampered (Lua field match rejects). A missing `X-Call-Direction` gets 404. |

**Also verified.**
- `ds_select_dst("1","4")` sets `$du`, usable as `$uac_req(ouri)`. The `jansson`, `crypto` and `uac` modules load. The HMAC matches Python's, but as base64url (see deviation 3).
- Canonical-JSON check: extra key, reordered keys, retyped `ts`, whitespace and trailing newline all give 400. Bad or stale (±31 s) timestamps give 401, ±29 s passes. Wrong secret or signature gives 401. Oversize body 400. GET gives 405. `/RPC` (POST and GET) and any other path give 404. SIP on the dial socket gets no reply. 6 malformed bodies caused 0 dials.
- `mod_hiredis` authenticates as the ACL user only with the patch (`mod_hiredis-username.patch` in this directory, adds a `username` connection param and `AUTH <user> <pw>`). Unpatched behaviour (password-only `AUTH`, i.e. as `default`, which is off) fails: a profile without `username` could not connect. A wrong password fails. A Redis error or NOPERM makes `hiredis_raw` return nothing, so the Lua fails closed.
- Redis matrix, 22 checks (12 denied), all as designed: `freeswitch` GETDEL intent / SET NX EX claim / XADD `events:fs` succeed; SET intent, GETDEL claim, GET claim, GET `did:x`, XADD `events:cpaas`, SET `cbprop`, SET any other key and FLUSHALL are NOPERM. `campaigns` writes intent and `cbprop`, reads no `did:x`. `conversation` GETDEL intent and XADD `events:fs` NOPERM. `telephony` XADD `events:fs` NOPERM. Unauthenticated PING is NOAUTH. Not exercised: an explicit `AUTH default` attempt (redis-cli printed NOAUTH, not a distinct WRONGPASS).
- Sofia gateway `kamailio_egress` on `campaign_egress` passed `auth_check`, and its INVITE carried `X-Egress-Token` and `P-Asserted-Identity`. `originate` through it with no token, an other-callee token, or an expired token: all `CALL_REJECTED` (403), 0 carrier INVITEs. `${acl(${network_addr} kamailio)}` works in a dialplan condition (true for 127.0.0.1; `acl 10.9.9.9 kamailio` is false).
- Transfer legs on a live campaign call (warm: `originate sofia/external/sip:+15557770001@127.0.0.1:15060 &park`; cold: `uuid_transfer <leg> 'bridge:sofia/external/sip:+15557770001@127.0.0.1:15060' inline`, the dial strings from `gateway/src/telephony/EslClient.cpp`): both got 403 from Kamailio, `route(PSTN_EGRESS)` admitted 0, the carrier saw 0. The Gateway itself did not drive them, and the rig has no "existing path" for PSTN transfers, so this shows only that a transfer leg carries neither `fs-egress` nor a token.

**Not exercised.** Two-node round-robin and failover, the `[::1]:5071` listener, the real Gateway and AI conversation, a DB-backed `kamailio_cdr`, a softphone callee, rtpengine.

**Corrections the design and Phase 4 tasks must take from this run.**
1. The FreeSWITCH profile that receives the trigger needs `enable-3pcc=proxy`; `profile external` gets it for the SDP-less trigger INVITE. Without it FreeSWITCH answers an SDP-less INVITE with 480 MANDATORY_IE_MISSING before the dialplan runs, and `enable-3pcc=true` sends 200 OK with SDP and allocates RTP (breaks criteria 1-2). It also lets every SDP-less INVITE on that profile reach the dialplan, so use a dedicated trigger profile or confirm `external` can take that.
2. `$uac_req(hdrs)` must include `Contact: <sip:campaigns@127.0.0.1:5060>`; FreeSWITCH 400s a request with no Contact ("NO CONTACT!").
3. `crypto_hmac_sha256` returns unpadded base64url, not lower-case hex. `X-Campaign-Signature` and the egress-token `mac` use that form, so `DialRequestClient` must emit `urlsafe_b64encode(...).rstrip("=")` and the token pattern is `[A-Za-z0-9_-]`.
4. Convert strings with `$(var(x){s.int})` before comparing `exp` and `ts`. `$var(x) + 0` on a string concatenates, which made every expired token admissible during the run (found and fixed in the rig).
5. Set `$uac_req(callid)` to something unique per send (rig: key + "." + microseconds). Reusing the key as Call-ID made a repeated POST come back with the first reply or 482.
6. `$dd` carries no port; `ouri=$du` carries it, so the R-URI `sip:outbound-trigger@<host>` is fine.
7. Kamailio must run with `-DD`, not `-D`: under `-D -E` (`scripts/lib/sip.sh:112`) no TCP listener opened, so `listen=tcp:127.0.0.1:5071` needs the launcher change. Also set `tcp_accept_no_cl=yes`, or a GET with no Content-Length closes the connection with no 405.
8. `jansson.so` and `crypto.so` are not installed in `/usr/local/lib64/kamailio/modules`; they build from `/opt/src/kamailio` and must be installed before the dial socket can start.
9. `api_hangup_hook` rejects `${...}` in its value, and `billmsec` is not set when a Lua hangup hook runs. The rig used `session:setHangupHook` with `(now - answered_time)`, which gave 2071-2511 ms for 2.5 s calls. T-tasks that settle on FreeSWITCH `billmsec` should use that or a `CHANNEL_HANGUP_COMPLETE` consumer instead.
10. `hiredis_raw` returns `-ERR no reply` for a nil `GETDEL`; the Lua must require the reply to start with `{` rather than test for empty.
