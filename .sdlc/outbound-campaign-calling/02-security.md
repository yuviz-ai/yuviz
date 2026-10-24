# Security review: .sdlc/outbound-campaign-calling/02-design.md (round 6, re-review of R5 #2 fixes)
VERDICT: AMBER

The verdict is AMBER only because of the deferred billing medium (finding 1). Everything in scope for this round is fixed, and no new medium or high was introduced.

## Changes since round 5
- **(a) FreeSWITCH Redis ACL now uses selectors: FIXED.** The ACL at L797 is `(~campaign:intent:* +getdel) (~campaign:claim:* +set) (~campaign:events:fs +xadd)` on top of `resetkeys -@all`, so there is no root key pattern for the cross-product to reach.
  - FreeSWITCH can no longer SET or DEL an intent. It also can no longer SET or DEL its own stream key, which closes the consumer-group destruction path.
  - The test list at L802 asserts both `SET campaign:intent:*` and `XADD campaign:events:cpaas` return NOPERM.
  - Task 0 (L79–80) gates on `mod_hiredis` working with ACL usernames and the selector matrix, and Path A does not ship without it.
  - Removing SET on intents breaks no legitimate write. Campaigns is the only intent writer (`SET NX EX 60`, then `EXISTS` on re-send, L527). The Lua only ever GETDELs, and Campaigns keeps `+get +del +getdel` for claims.
  - Residual (low, deferred, part of finding 2): `telephony` and `conversation` (L799–800) are still described as additive grants, not selectors.
- **(b) Egress MAC covers `key|callee|caller_id|tenant_id|max_call_s|exp`: FIXED.**
  - Kamailio (L783–785) recomputes the MAC over `$rU` and the token's fields, in the same order as `DialRequestClient` (L524). It also requires the P-Asserted-Identity user to equal `caller_id` (FreeSWITCH sends `sip_cid_type=pid`).
  - Vectors that change any single field are required to fail (L877, L986).
- **(c) `max_call_s` is always set and billing can only lower it: FIXED.**
  - `claim_next_due` sets `COALESCE(agents.max_call_duration_s, 7200)` on every attempt (L502).
  - `agents_max_call_duration_s_check` is `BETWEEN 30 AND 7200` (`database/schema.sql:97,128`), so the value always satisfies the attempt CHECK and the Lua regex.
  - The `held` transition is `LEAST(max_call_s, $granted)`, with granted ≥ 30 (L596).
  - This also resolves the R5 non-security note that NullBilling Path A intents had no `max_call_s` and so were always rejected.
- **(d) The Lua rejects a token whose fields differ from the intent: FIXED.** Step 2 (L114) compares the token's key, exp, tenant_id, caller_id and max_call_s against the intent. A mismatch gives a fail-closed 403. The callee is bound by Kamailio's MAC over `$rU`, and FreeSWITCH dials `intent.callee`.
- **New-gap checks requested this round (no finding):**
  - *Token parsing ambiguity:* none. Every token field is dot-free by construction: the key `^[0-9a-f-]{36}_[0-9]{1,3}$`, the UUID, the E.164 value, two integers and the hex MAC. Kamailio rejects any field that fails its anchored pattern, so a different field count cannot parse. The MAC input is `|`-joined over fields that cannot contain `|`.
  - *Cross-tenant replay:* none. `tenant_id` and `caller_id` are inside the MAC, and FreeSWITCH can no longer rewrite an intent, so the R5 tenant-swap path is gone. Single use is enforced by the htable entry on the MAC, kept 300 s. That outlives the token window: `exp` = intent `EX 60` + 120 s, about 180 s.
  - *Accepted residual:* a Kamailio restart empties the htable. Within that ~180 s window, a party holding both the `fs-egress` digest password and a sniffed token could re-dial the same callee, from the same caller ID, for the same tenant. That replay cannot cross a tenant or change any value, so it is not reported as a finding.
- **Carried non-security note:** the `:conv`-before-`:fs` ordering holds only within one `XREADGROUP` batch. This is unchanged from R5.

## Findings
1. [medium] *(deferred, billing; carried R4 #2 / R5 #1, not re-reviewed)* Telephony `POST /{provider}/call` still admits a tenant `admin` JWT and bypasses the A13 credit gate. — `services/telephony/auth.py:16` still reads `TELEPHONY_CALLER_ROLES = frozenset({"superadmin", "admin"})`
   Attack: A tenant admin with zero credit calls `/vobiz/call` directly. They place uncapped, unreserved AI outbound calls.
   Fix: As in R5. Restrict the route to tenant-less service accounts, or require a live billing hold plus a mandatory `time_limit_s`.
2. [low] *(deferred, billing; carried R5 #3, not re-reviewed)* A hinted CPaaS call id is accepted on a `to`/`from` match alone, and `idx_campaign_attempts_provider_call` (L471) is not UNIQUE.
   Attack: As in R5. A short direct call's id is hinted onto a pending campaign attempt, and the real call is under-billed.
   Fix: As in R5. Check provider start time ≥ `attempt.created_at` and make the index `UNIQUE (tenant_id, provider_call_id)`.
3. [low] *(deferred, billing; carried R5 #4, not re-reviewed)* Tenant-less service accounts can still read balance and spend (readiness, progress cost fields) along with attempts, contacts and export.
   Attack: As in R5. The holder of the Conversation service-account JWT reads every tenant's balance and spend and exports contact lists.
   Fix: As in R5. Return 403 for `is_service_account` on these routes.

## Verified controls
1. The FreeSWITCH Redis user is selector-scoped: GETDEL only on intents, SET only on claims, XADD only on `:fs`. It has no root key pattern (L797), and the NOPERM tests are listed (L802).
2. Campaigns is the sole writer of intents. No legitimate path needs a FreeSWITCH-side intent write (L527, L114).
3. The egress MAC binds callee, caller ID, tenant, `max_call_s` and exp, with the same field order in Python and Kamailio (L524, L785).
4. Kamailio checks that the PAI user equals the token's `caller_id` before relaying (L785).
5. Token parsing is unambiguous: every field is dot-free and matched by an anchored pattern, with exactly 6 fields (L524, L783).
6. The single-use htable entry (300 s) outlives the token validity window (~180 s), so there is no in-process replay window.
7. The Lua fails closed on any token/intent field mismatch, a missing `max_call_s`, or a Redis error (L114).
8. `max_call_s` is always present. Its domain matches `agents` (30–7200), and billing can only lower it via `LEAST` (L502, L525, L596; `schema.sql:97,128`).
9. `max_call_s` passes an anchored regex before it reaches `sched_hangup`, so there is no dialplan injection.
10. The egress route fails closed (503) on an empty `PSTN_GW_URI` or an empty HMAC secret, and removes `X-Egress-Token` before relay (L787).
11. Task 0 blocks shipping Path A unless `mod_hiredis` supports ACL users and selectors (L79–80, L835).
12. *(Carried from R5, unchanged areas: 24 controls)* The `billable_ms` clamp and its CHECK; the billing grant cap at settle; stream-derived `source`; disjoint producer prefixes; Path B duration from `call_status`; `:conv` resolution by session and tenant; the Telephony nonce placed after the signature check; and the 15 R4 controls (null-billing fail-closed, unclaim, idempotent settle, tenant-matched transitions, 404 parity, `egress_guard`, CRM AEAD, CSV guard, tenant-filtered erasure, among others).
