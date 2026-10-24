# PRD: Outbound campaign calling (own SIP + CPaaS), crash-safe, with launch readiness

## Problem
Tenants running outbound campaigns get unreliable results. On our own SIP path, Campaigns originates over ESL to one FreeSWITCH and hairpins leg B back through Kamailio, so with 2+ FreeSWITCH nodes the legs split across nodes. The call is also recorded as `inbound` to the agent DID. In-flight dial state lives in worker memory, so a crash leaves contacts stuck in `calling` and the campaign never completes (#116); the ESL listener spins at 100% CPU when FreeSWITCH restarts (#117). Tenants also have no pre-launch check that a campaign can actually dial.

## How this is solved elsewhere
Benchmark set: Sarvam, Autocalls, Retell, Vapi and Bland. Sources are search snippets and docs.
Orato.ai was dropped from the benchmark set because no public documentation was found.
- Sarvam (Indian) needs a ready, committed agent, a CSV with fields mapped to variables, a telephony connection or number group, and a schedule. Suppression is one org-wide Do Not Disturb list shared by all campaigns. Outcomes go to a per-attempt webhook carrying outcome, transcript, duration and variables ([Sarvam](https://docs.sarvam.ai/conversations/deploy/campaigns)).
- Autocalls.ai has start and end time plus allowed weekdays, 1-5 retries (default 3) and a retry interval of 10 to 4320 minutes (default 60) ([Autocalls](https://docs.autocalls.ai/campaigns/creating-campaigns)).
- Retell batch calls take local calling windows and a future schedule, and queue calls for free concurrency slots rather than rejecting them ([Retell](https://docs.retellai.com/api-references/create-batch-call.md)). A community thread reports a batch that created duplicate calls ([Retell community](https://community.retellai.com/t/batch-campaign-created-duplicate-calls-with-missing-metadata/568)). That is the failure our idempotency key is for.
- Vapi's campaign config is read-only once scheduled, and it retries while waiting for concurrency, for up to one hour ([Vapi](https://docs.vapi.ai/outbound-campaigns/scheduling-and-lifecycle)). Its support threads are full of campaigns stuck "scheduled" or never finishing ([Vapi support](https://support.vapi.ai/t/33193798/outbound-calling-campaign-never-finished-its-scheduled-calls)). That is our #116 shape.
- Bland: CSV batch with one shared prompt or pathway. Concurrency and daily caps are plan limits and appear to be a common failure source (unverified, third-party sources only).

Convergence: CSV plus variable mapping, one caller number, calling window plus timezone, retry count plus interval, a concurrency cap, and a org-wide suppression list. Differences: Sarvam validates readiness through a committed agent, and the others mostly fail at runtime.

Recommendation: follow the convergent set, which the request already matches. Add what the market underdelivers: a launch readiness check that blocks start on hard failures, and restart-safe state. Do not expose per-campaign DNC lists; keep one tenant-wide list as Sarvam does. Do not add number-group rotation or CSV variable mapping now; both are cuttable (see Out).

## Scope
- In: A tenant admin configures a campaign with outbound agent, caller-ID DID, CSV contacts, retry count, retry gap, calling hours and timezone, and concurrency. The caller-ID may be on our SIP infrastructure or on a CPaaS provider, and the system picks the path from where the DID lives.
- In: A readiness check run on demand and again at start, listing each failed or warning item with the field to fix.
- In: Path A calls: Campaigns sends a JSON dial request over HTTP to a dedicated Kamailio handler; Kamailio picks a FreeSWITCH node through the same dispatcher as inbound and itself sends the INVITE. Campaigns has no SIP client. One audio leg to the callee carries the AI, no hairpin. Calls are recorded as `outbound`.
- In: Path B calls are placed through the provider REST API, with outcomes from authenticated provider status webhooks and calls recorded as `outbound`.
- In: Crash-safe tracking on both paths: persisted attempts with explicit states, idempotent dialing, recovery of stuck contacts, deadlines on every attempt, slots freed on call end.
- In: Per-contact, per-attempt outcome visible in campaign progress and contact list (answered, busy, no answer, declined, failed, ended).
- In: Three capacity limits that delay dials and never fail them: a tenant-wide outbound concurrency cap (superadmin-set), a platform-wide outbound cap with an inbound reserve, and a calls-per-second limit per trunk (Path A) or provider account (Path B), each visible as a wait reason.
- In: A business disposition per contact (interested, not interested, wrong number, callback requested, do-not-call requested), shown, filterable and exportable as CSV, separate from the telephony outcome.
- In: A `schedule_callback` agent tool that books that contact's next call at a validated, confirmed time within calling hours.
- In: Automatic do-not-call: a caller's "do not call me" adds the number to the tenant DNC list at once.
- In: A signed lead hand-off webhook to the tenant's CRM endpoint when a contact reaches a final business outcome.
- In: Contact data and compliance: consent attestation at upload, a campaign purpose with a number-series readiness warning, E.164 numbers with full-number DNC matching, per-tenant retention with a daily purge, erasure on request, and masking of numbers outside exports. The platform is the data processor; the tenant is responsible for consent.
- Out: Call recording.
- Out: CSV variable mapping beyond `phone_number,name`, number-group rotation, per-contact scheduling, predictive dialing.
- Out: Changing AI per-turn latency or the Gateway/Conversation call path.
- Out: A new DNC model; the existing DNC list is reused.
- Out: Building an external national DND or consent registry check (only the interface hook is in scope).

## Acceptance criteria
Attempt states (referenced below). Every attempt is persisted in exactly one of: `pending` (row committed, nothing sent), `sent` (send accepted by Kamailio or the provider; provider call id stored for Path B), `resolved` (terminal outcome recorded). Transitions only move forward. The single exception: a `failed` attempt that was closed by its deadline with no call record may be upgraded to `answered` or `ended` by a late event for the same contact:attempt (criterion 12); no other terminal outcome changes.

Readiness
1. Given a campaign with no agent, no caller-ID, zero pending contacts, or a missing timezone, when readiness is requested, then each is returned as a blocking failure naming its field.
2. Given a caller-ID the tenant does not own, when readiness is requested or start is called, then it is a blocking failure and no call is placed.
3. Given a caller-ID on a CPaaS provider with no active credentials, or on our SIP with no healthy FreeSWITCH node, when readiness is requested, then it is a blocking failure.
4. Given all contacts are on the DNC list or outside the calling hours today, when readiness is requested, then it returns a warning (not a block) with counts.
5. Given any blocking readiness failure, when start or resume is called, then it returns 4xx and the campaign state is unchanged.
6. Given a campaign with `max_attempts` below 1 or a retry gap below 1 minute, when it is saved, then validation rejects it.

Dial and outcome tracking (both paths)
7. Given a contact is selected for dialing, when the worker proceeds, then an attempt row (contact id, attempt number, idempotency key `contact_id:attempt`, deadline, state `pending`) is committed to Postgres before any Kamailio dial request or provider REST request is sent, and moves to `sent` only after the send is accepted.
8. Given a campaign with `max_concurrent_calls=N`, when more than N contacts are due, then never more than N attempts are in flight, and a slot frees when the call ends, fails or hits its deadline.
9. Given a contact on the tenant DNC list or outside calling hours in the campaign timezone, when the worker ticks, then no attempt is created for it.
10. Given an attempt, when the callee answers, is busy, does not answer, declines, the dial fails, or the call ends, then the attempt gets exactly that outcome, recorded once, keyed by contact and attempt; `declined` is recorded and reported as `declined`, not relabelled `busy`.
11. Given an attempt in `pending` or `sent` whose deadline passes, when the deadline handler runs, then it first reads the call record and provider status for that contact and attempt: if the call is live or was answered the attempt stays/becomes answered (or ended on completion), keeps its slot while live, and is never re-queued; only when no call record exists is it resolved as failed.
12. Given an attempt closed as failed at its deadline with no call record, when a webhook or call record later shows the call was answered ("answered after deadline"), then the outcome is corrected to answered, the contact is not redialed, and if a retry was already queued it is cancelled before dialing (test: deadline passes, retry queued, late answered event arrives, no second call).
13. Given a retryable outcome (busy, declined, no answer, failed) with attempts remaining, when it resolves, then the contact is re-queued no sooner than the campaign's single retry gap (the UI setting, default 60 minutes, applied identically to every retryable outcome), not the pacing interval; a declined call is re-queued exactly as a busy call is.
14. Given an outcome of answered or ended, or `max_attempts` reached, when it resolves, then the contact is never redialed.

Crash safety and recovery by state
15. Given the worker is killed and restarted, when it starts, then every attempt in `pending` or `sent` is reconciled per the rules below and no contact stays in `calling` beyond its attempt deadline.
16. Path A, attempt `pending`, no Redis claim for its key and no call record: on restart the dial request is sent once with the same key.
17. Path A, attempt `pending` or `sent`, call record exists: the attempt takes the call record's state and nothing is re-sent.
18. Path A, Redis claim held for the key but no call record (claimed, never dialed): the claim has a TTL no longer than the attempt deadline; no re-send is made under the held key; at the deadline the attempt resolves as failed (not dialed) and the retry uses attempt+1 and a new key.
19. Path B, attempt `pending` (provider call id not stored): the provider is queried by key `contact_id:attempt`, passed as the provider idempotency key or client reference where supported. For a provider with neither, the query is by callee, caller-ID and created-after the attempt's creation time. Exactly one match is adopted and moved to `sent`; zero matches sends once; more than one match sends nothing and resolves per criterion 11 at the deadline.
20. Path B, attempt `sent` (provider call id stored): the provider call is fetched by that id; its status gives the outcome, and no re-send is made.
21. Given a contact whose call record shows it was answered, when recovery runs, then it is marked answered and not retried.
22. Given the worker is down while calls are live, when a call ends, then the outcome is persisted at ingestion independently of the worker and applied when it returns; live calls are not dropped by the crash.
23. Given every contact reaches a terminal state, when the last one resolves, then the campaign status becomes completed (regression for #116).
24. Given FreeSWITCH restarts, when Campaigns is running, then Campaigns CPU use stays at idle level, since Campaigns holds no ESL connection (regression for #117).

Path A (own SIP)
25. Given two or more FreeSWITCH nodes, when a campaign call is placed, then the callee leg and the AI audio fork are on the same node and no second leg is routed back through Kamailio to the agent DID.
26. Given Kamailio accepts a valid dial request, when it sends the INVITE, then the INVITE to FreeSWITCH carries `X-Call-Direction: outbound` and the campaign, contact, callee, caller-ID, agent DID and idempotency values, and FreeSWITCH takes its outbound branch and records the call with direction `outbound`, the campaign id and the contact id.
27. Given the same idempotency key arrives twice (retry, duplicate HTTP request or duplicate INVITE), when FreeSWITCH processes it, then the first claims the key (Redis SET NX) and dials; the second is rejected without dialing.
28. Given a Kamailio dispatcher node marked down, when a dial request is handled, then it is routed to a healthy node; given none healthy, the attempt fails and is retried per the retry gap.
29. Given a transfer on an outbound call, when it fires, then the human leg is bridged on the same FreeSWITCH node.
30. Given the AI call path on an outbound call, when compared with an inbound call on the same agent, then per-turn latency is not worse (checked by the existing latency benchmark).

Dial-request handler trust (default, owner may override)
31. Given the default deployment, when listeners are inspected, then the Kamailio dial-request handler binds only to loopback (127.0.0.1 and ::1), not to 0.0.0.0 or any internal or public address, and a connection attempt from any non-loopback peer is refused (checked by a bind test and a port scan from another host).
32. Given a well-formed JSON dial request (callee, caller_id, agent_did, campaign_id, contact_id, idempotency key) from an allowlisted source with a valid shared-secret/HMAC, when Kamailio receives it, then it is accepted, a FreeSWITCH node is selected through the dispatcher, and Kamailio itself sends the INVITE.
33. Given a request with a missing or wrong secret/HMAC, or from a source not on the allowlist (including a spoofed or LAN-peer source), when received, then it is refused with 401/403 and no INVITE is sent.
34. Given an INVITE with outbound-trigger headers reaching FreeSWITCH from any source other than Kamailio, when received, then it is refused.
35. Given the secret or the allowlist is empty or unset, when Kamailio starts, then the handler refuses every request (fail closed).
102. Given a request body that is malformed JSON, oversized, missing a required field, has an extra or wrongly typed field, a callee or caller_id that is not E.164, or a value containing CR/LF or SIP-header-breaking characters, when received, then it is refused with 400 and no INVITE is sent; no field value reaches a SIP header unescaped.
103. Given the handler's port, when any path or method other than the single dial endpoint is requested, or any `jsonrpcs` (admin RPC) path or method such as `core.*` or `ps`, then it returns 404/405 and executes nothing; the admin interface is not routed from the handler and not reachable by Campaigns (checked by requests to the handler, and a scan of the admin interface's own bind, which is loopback or Unix socket only).
104. Given a SIP request on the public SIP listener or any non-dial source carrying `X-Call-Direction`, `X-Campaign-Id`, `X-Contact-Id`, `X-Callee`, `X-Caller-Id`, `X-Agent-DID` or the idempotency header, when Kamailio receives it, then those headers are stripped and the call is handled as ordinary inbound; the headers Kamailio adds itself on its own dial INVITE are preserved.

Path B (CPaaS)
36. Given a caller-ID bound to a provider, when a call is placed, then it is originated through that provider's API using the lookup key defined in criterion 19.
37. Given a provider status webhook with an invalid or missing signature or secret, when received, then it returns 401/403 and changes no state.
38. Given a valid provider webhook for a tenant's call, when received, then the outcome applies only to the attempt matched by contact and attempt (or stored provider call id), and a webhook naming another tenant's attempt is ignored without a distinguishable response.
39. Given the provider returns a rate-limit or concurrency-quota error, when dialing, then the attempt is not counted as failed or consumed; it is retried after backoff, and concurrency never exceeds the lower of campaign and provider limits.
40. Given a duplicate or out-of-order webhook, when received, then the outcome is recorded once and a terminal outcome is not overwritten, with one exception: a `failed` attempt closed by its deadline with no call record may be upgraded to `answered` or `ended` by a late event for the same contact:attempt (criterion 12); no other terminal outcome changes. The call record is checked before any status that would trigger a retry (criteria 11 and 12).

Permissions and tenancy
41. Given a viewer-role user, when they call start, pause, resume, upload or edit, then it is denied; viewers may read progress and outcomes (matches the existing role rules).
42. Given tenant B, when they request tenant A's campaign, attempt, readiness or outcomes by id, then the response is identical to a nonexistent id.

Capacity limits (tenant cap, platform cap with inbound reserve, CPS)
43. Given a tenant admin or any non-superadmin, when they try to set or raise the tenant outbound concurrency cap, then it is denied and the value is unchanged; given a superadmin, when they set it, then it is stored per tenant and read back.
44. Given a tenant cap of 5 and two running campaigns of that tenant each with `max_concurrent_calls=5`, when many contacts are due, then total in-flight outbound attempts for the tenant never exceed 5; and for a single campaign the effective limit is the lower of its `max_concurrent_calls` and the tenant cap.
45. Given tenant A is at its cap, when tenant B has due contacts and its own headroom, then tenant B's dials proceed unaffected.
46. Given a campaign whose `max_concurrent_calls` exceeds its tenant's cap, when readiness is requested, then a non-blocking warning states both numbers; and the campaign view shows the tenant cap and the effective limit.
47. Given a fresh install, when the platform settings are read, then total concurrent call capacity is 300 and the outbound cap is 225 (75%), as shipped defaults editable by superadmin only; given total in-flight outbound attempts across all tenants reach the outbound cap, then further due attempts wait (none fail, none are created) until a slot frees. A tenant with no tenant cap set is limited by the platform cap alone. All three numbers are concurrent calls, not per hour or per day.
48. Given outbound is at its cap of 225, when inbound calls arrive, then they are accepted as before, and outbound never occupies the 75 concurrent calls (25%) reserved for inbound; given a superadmin edits the settings, then a save that would make the outbound cap exceed total capacity minus the inbound reserve is rejected, and the reserve is shown next to the cap.
49. Given a per-trunk CPS limit of C on Path A, when campaigns of one or more tenants send dial requests through that trunk or carrier route, then no more than C dials start in any one second on that trunk, shared across all campaigns and tenants using it; the excess waits.
50. Given a per-provider-account CPS limit of C on Path B, when campaigns place REST originates through that account, then no more than C originate requests start in any one second per account, shared across campaigns and tenants; the excess waits.
51. Given any of these limits is reached, when a due attempt cannot start, then the contact stays queued with no attempt row created, `attempt_count` and `max_attempts` are not consumed, no attempt is marked failed, and calls already in progress are not ended.
52. Given a superadmin raises or lowers the tenant cap, platform cap, or a CPS limit while campaigns are running, when the change is saved, then it applies without restarting any service within one worker tick; lowering never drops a call in progress and new dials wait until usage is below the new limit.
53. Given a campaign is waiting, when its progress is read, then it states the current wait reason (campaign cap, tenant cap, platform cap, trunk CPS, provider CPS, calling hours, or pacing) and the count of contacts waiting on it; one test per reason.
54. Given a non-superadmin, when they set the platform cap or a CPS limit, then it is denied; given tenant B, when it reads tenant A's usage or wait state by id, then the response is identical to a nonexistent id.
55. Given the worker is killed and restarted, when it starts, then tenant, platform and trunk usage are rebuilt from persisted attempts and live call state, so no limit is exceeded after restart.
56. Given the limit checks run, when an outbound call is in progress, then they execute only in the Campaigns dial path, and Gateway and Conversation make no new calls and per-turn latency is unchanged (criterion 30 benchmark).

Business disposition
57. Given a contact whose attempt is linked to a call (via the attempt's `call_session_id`), when the call ends, then the contact shows the call's workflow disposition and extracted variables as its business disposition, stored and displayed separately from the telephony outcome (a contact can be `answered` with disposition `not interested`).
58. Given a contact with no linked call, or a linked call with no disposition, when the contact list is read, then the disposition is empty and no error is raised.
59. Given the contact list, when filtered by disposition (and by telephony outcome), then only matching contacts are returned, scoped to the caller's tenant.
60. Given a campaign, when an admin or viewer exports contacts as CSV, then the file has one row per contact with phone, name, attempts, last telephony outcome, disposition and extracted variables; cell values beginning with `=`, `+`, `-` or `@` are neutralised against spreadsheet formula injection; and tenant B cannot export tenant A's campaign (response identical to a nonexistent id).

Callback scheduling
61. Given an outbound campaign call, when the caller asks to be called back, then the agent has a `schedule_callback` tool; the tool is not offered on inbound calls.
62. Given the tool is called, when it runs, then the contact it acts on is taken from server-side call metadata (the call's campaign and contact), and any contact id or phone number supplied by the model is ignored (lesson 44).
63. Given the caller states a time ("tomorrow at 3", "in two hours", "Friday 10:30"), when the tool resolves it, then it converts to an absolute time in the campaign's timezone (decided: contacts carry no timezone), cross-checks the stated digits and relative terms against the computed date, and returns the computed time to the agent, which must read it back and get the caller's confirmation before the callback is stored; without confirmation nothing is scheduled.
64. Given a stated time that is unparseable, in the past, or more than 30 days ahead, when the tool runs, then it returns an error, nothing is scheduled, and the agent asks again rather than claiming success.
65. Given a requested time outside the campaign's calling hours, when the tool runs, then the callback is set to the next allowed slot and the tool returns that slot, which the agent says aloud.
66. Given a pending callback for a contact, when another callback is requested, then the new one replaces the old; there is never more than one pending callback per contact.
67. Given a callback is scheduled, when it is stored, then the contact's earlier attempt is not counted as failed and a callback attempt does not count against `max_attempts` (decided); callbacks are capped at 3 per contact, and a fourth request is refused and the agent says so; the callback is still subject to DNC, calling hours, concurrency and capacity limits.
68. Given the callback time arrives, when the worker ticks, then the contact is dialed at or after that time through the normal path; given the contact was added to DNC meanwhile, then the callback is cancelled and not dialed.
69. Given the callback store fails, when the tool runs, then the agent is told it failed and does not tell the caller a time was booked.

Automatic do-not-call
70. Given the caller asks not to be called again, when the agent records a do-not-call request, then the number is added to the tenant's DNC list immediately, before the call ends, normalised to the same form the DNC check uses, and the contact's disposition shows do-not-call requested.
71. Given a number on the tenant DNC list, when any campaign of that tenant (including a callback or retry) would dial it, then no attempt is created; a number added by tenant A has no effect on tenant B.
72. Given a contact with a pending retry or callback, when its number is added to DNC, then those pending dials are cancelled.
73. Given the number is already on the DNC list, when a do-not-call request arrives, then no duplicate entry is created and no error is raised; the existing DNC removal remains admin-only (criterion 41).
74. Given the do-not-call action runs, when it fails to write, then the agent does not claim success, the contact is flagged for review, and the retry is suppressed until the write succeeds.

Lead hand-off webhook
75. Given a tenant admin, when they configure the CRM endpoint URL and signing secret, then only admins can write them, the secret is stored encrypted bound to the tenant, and API responses never return the secret (masked) (lesson 43).
76. Given a tenant URL, when it is saved and again at every send, then it must be HTTPS, and its DNS resolution is rejected if any address is private, loopback, link-local or a cloud metadata address; redirects are not followed; the connection is made to the validated IP (checked by tests with `127.0.0.1`, `10.x`, `169.254.169.254`, a hostname resolving to those, and a redirect to them).
77. Given a contact reaches a final business outcome (answered with a disposition and no pending retry or callback, a do-not-call request, or retries exhausted), when it is reached, then one webhook is sent with phone, name, telephony outcome, disposition, extracted variables, campaign and contact ids.
78. Given a webhook delivery, when it is sent, then it carries an HMAC-SHA256 signature over timestamp plus body using the tenant secret, and the timestamp, so the receiver can reject replays.
79. Given a delivery, when it is retried, then it keeps the same delivery id so the receiver can deduplicate, and exactly one delivery id exists per contact final outcome.
80. Given the endpoint returns 5xx, 429 or times out (10 s), when delivery fails, then it is retried with exponential backoff up to 6 attempts over about 24 hours, then marked failed and visible per contact; a 4xx other than 429 is not retried.
81. Given deliveries are slow or failing, when campaigns run, then dialing and call handling are not blocked (delivery runs off the dial loop and off the event loop, lesson 18).
82. Given logs and error output from delivery, when inspected, then neither the signing secret, the signature nor extracted-variable values appear (lesson 33).
83. Given the worker is killed with deliveries pending, when it restarts, then pending deliveries resume without duplicating a delivered one.
84. Given the callback, DNC and disposition features, when a call is in progress, then per-turn AI latency is unchanged: the only in-call addition is the tool call, executed only when the agent invokes it, and nothing is added to the per-turn path (criterion 30 benchmark).

Contact data and compliance (the platform is the data processor; the tenant is responsible for consent. Legal specifics, namely the DPDP Rules notified 14 Nov 2025 with their 18-month transition and the TRAI TCCCPR February 2025 amendment, need confirmation by counsel before launch; nothing below is legal advice)
85. Given a contact upload, when the admin has not ticked the consent attestation ("I have consent to call these numbers"), then the upload is rejected and no contact is stored; given it is ticked, then the upload records who attested, when, and the SHA-256 of the file, and these are readable by tenant admins of that tenant only.
86. Given a CSV with an optional `consent_source` column, when it is uploaded, then each value is stored with its contact (length-limited) and appears in the export; given the column is absent or empty, then the upload succeeds.
87. Given a campaign, when it is created or edited, then it has a purpose of `promotional`, `service` or `transactional`; any other value is rejected.
88. Given a campaign whose caller-ID DID does not match any number series configured for its purpose, when readiness is requested, then a non-blocking warning names the purpose and the configured series and start still succeeds; given no series is configured for that purpose, then no warning is raised. The series (for example 140 for promotional, 1600 for service and transactional) are platform settings editable by superadmin only, with none hard-coded, because sources conflict between 160 and 1600.
89. Given an uploaded number, when it is stored, then it is normalised to E.164; a number without a `+` prefix is interpreted using the campaign's default country (Assumption: a campaign-level country setting, see open question 4); rows that cannot be parsed to a valid E.164 number are rejected with a per-row reason and the rest are accepted; numbers duplicated after normalisation are stored once and counted in the upload result.
90. Given the DNC check, when it compares numbers, then it matches the full E.164 number, replacing the last-10-digits comparison in `dnc.normalize_phone`: `+14155550100` and `+9114155550100` no longer match each other.
91. Given existing DNC entries stored as last-10-digits, when the change is applied, then each is converted to E.164 using the tenant's default country; an entry that cannot be converted is kept and blocks any number ending in those 10 digits (fail safe toward not calling); collisions are de-duplicated inside the same guarded block (lessons 5, 13); and the reader and the data change land together (lesson 11).
92. Given a registry hook interface (input: E.164 number, purpose, tenant; output: allow, deny or unknown), when a stub implementation denies a number, then no attempt is created for it; given the hook is unavailable, then dials wait and attempts are neither failed nor consumed (as in criterion 51); the default implementation allows everything, and no real registry integration is built.
93. Given retention, when no tenant value is set, then it is 90 days after the campaign ends; a platform admin sets the platform default, a tenant admin may shorten but not exceed it, and a viewer cannot change it.
94. Given a campaign ended more than its retention ago, when the daily purge runs, then contact phone numbers, names, `consent_source`, extracted variables and per-attempt personal data are deleted, including the callee number and extracted variables on the linked call records, and only aggregate counts remain; DNC entries are kept; campaigns not yet ended are never purged.
95. Given the purge is interrupted or run twice, when it runs again, then it resumes without error, deletes nothing outside its scope, and leaves pending webhook deliveries for purged contacts cancelled and marked as such.
96. Given a campaign within 14 days of its purge date, when an admin opens it, then the UI warns of the date and points to the CSV export and CRM webhook (criteria 60 and 75-83); the purge date is shown on every ended campaign.
97. Given a tenant admin erases a number, when the request is processed, then the number is removed from contacts, attempts, stored exports, pending webhook deliveries and the linked call records' personal data for that tenant, and remains only on that tenant's DNC list (added if absent).
98. Given the same erasure is requested twice, when it runs again, then it succeeds without error or change, and each request writes an audit entry (who, when, a hash of the number, not the number); given a number that does not exist, the response is identical to one that did (lesson 2); given a viewer, then it is denied; erasure for tenant A does not touch tenant B's data.
99. Given any log line, error message, metric label or trace from the new code, when inspected, then phone numbers appear only masked (for example `+91******3210`); a tripwire test covers every entry point that handles a number, including the dial loop, provider webhooks, the CRM webhook, upload and erasure (lesson 42).
100. Given the contact list, progress and any other UI or API read, when it shows a number, then it is masked; the full number appears only in CSV exports and in the CRM webhook payload to the tenant's own endpoint.
101. Given a CSV export, when it is generated, then an audit entry is written with who, when, campaign, row count and filters, without the numbers; the audit log is append-only and readable only by that tenant's admins.

## Constraints
- Auth and tenancy follow `services/campaigns/routers/campaigns.py`: path tenant binding, `require_role("superadmin","admin")` for writes, `get_current_user` for reads; shared identity comes from `services/config/deps.py` (lessons 9, 24, 27).
- Caller-ID ownership uses the existing `caller_id_owned` field, checked before the provider branch (`services/campaigns/worker.py:150`, lesson 39). Same-tenant checks apply to the agent and caller-ID link (lessons 31, 48).
- The existing `max_attempts`, `pacing_seconds`, `max_concurrent_calls`, calling hours and timezone fields, CSV `phone_number,name` upload, and DNC list are kept. Calling hours are evaluated in the campaign timezone (`worker.py:32`).
- The hot/cold split holds: Campaigns is cold path; Gateway and Conversation do not gain Postgres or Config Service calls on the media path. The Gateway call path and FreeSWITCH-to-Gateway audio fork are unchanged.
- Kamailio stays the only component that knows the FreeSWITCH pool; the dispatcher and its health probes are reused (`scripts/kamailio/kamailio.cfg.tpl`). Campaigns has no SIP client and no ESL connection; it reaches Kamailio only through the HTTP dial handler. Outcomes still return through the FreeSWITCH to Redis `campaign:events` stream, because Kamailio cannot track call results.
- Campaign calls carry `caller_did` equal to the tenant DID, so tools must not authorize on it for outbound (lesson 44).
- Capacity limits are platform and tenant settings read from the Campaigns side only; they are platform-admin-writable (superadmin), identified by `tenant_id IS NULL` scoping rather than role alone where a service account reads them (lesson 24).
- Disposition and extracted variables already exist on calls (`calls.disposition`, `calls.extracted_variables`, `database/schema.sql` lines 532-534), and the attempt links to the call by `call_session_id` (line 626); this feature reads them and adds no second source of truth. Extracted variables can hold sensitive values (lesson 33): enumerate sinks (contact list, CSV, webhook, logs) before shipping.
- The callback tool must follow the existing agent-tool conventions and the date-validation gap (digit cross-check; see the LLM timezone bugs) rather than trust model-computed dates.
- DNC matching today is last-10-digits in `services/campaigns/dnc.py` (`normalize_phone`); every caller of it, and every stored DNC row, must be found and migrated together (lessons 11, 17).
- Compliance: the platform is the data processor and the tenant owns consent. The DPDP Rules (notified 14 Nov 2025, 18-month transition) and the TRAI TCCCPR February 2025 amendment, and the 160 versus 1600 series, are unverified here and need confirmation by counsel; number series are platform settings so a legal correction needs no code change.
- Retention and erasure touch contacts, attempts, webhook deliveries, exports and `calls` rows owned by other services; a data-ownership boundary must be agreed before building (open question 5).
- Network perspective:
  - Kamailio dial-request handler (replaces the Campaigns SIP client). An HTTP (`xhttp`) endpoint that can only dial. It binds to loopback only, so a LAN peer or the internet cannot reach it; Campaigns and Kamailio must therefore share a host (if they ever do not, the override is an internal-interface bind plus the source allowlist, owner to decide). Auth is a source allowlist plus a shared-secret/HMAC check held in config, with the JSON validated before use; any missing setting makes it refuse everything (criteria 31-35, 102). Toll-fraud exposure: whoever can make this handler accept a request can place calls on a tenant's trunk, hence loopback, allowlist, HMAC, strict validation and the per-tenant and platform caps. Kamailio's `jsonrpcs` admin interface is never routed from this handler and never exposed to Campaigns (criterion 103).
  - Kamailio SIP listeners. Strip the campaign X- headers from every request that did not come from Kamailio's own dial path, including on public SIP ports (criterion 104). FreeSWITCH accepts the outbound branch only from Kamailio's address (criterion 34). FreeSWITCH SIP ports must not be reachable from LAN peers or the internet.
  - CPaaS webhooks. These are public HTTPS endpoints, authenticated per provider by signature or secret (criterion 37), replay-safe (criterion 40), rate-limited, with no secrets or phone numbers in logs.
  - Redis (idempotency claims) and Postgres stay internal-network, as today.
  - Capacity limits add no new listener; they are enforced inside Campaigns and are an additional toll-fraud and cost brake.
  - Lead hand-off webhook. Opens no listener; it is outbound egress from the Campaigns service over HTTPS to a tenant-supplied URL, the only tenant-controlled egress target in this feature, hence the SSRF controls (criterion 76). Auth is an HMAC signature with a per-tenant secret (criterion 78); the egress host should be allowed to reach the public internet only, not internal services. A LAN peer cannot reach anything new.
  - Compliance features add no listener. The registry hook (criterion 92) is an interface only; if a real registry is later wired in, it becomes another outbound egress target needing its own review.
- Phone numbers are personal data: masked in all logs and non-export reads (criteria 99-100).
- Removing the ESL originate path must find all callers first: grep `scripts/`, `docs/`, `admin-ui/` and other services (lesson 17).
- Any DB schema change in `database/schema.sql` must be idempotent, with guards inside the same `DO $$` block as destructive DDL (lessons 10, 13), and the reader of any changed column ships in the same phase (lesson 11).

## Open questions
1. How does the media-less leg work on Path A: can the INVITE that Kamailio itself originates to FreeSWITCH be answered and then bridged to the callee with no media on the Kamailio-to-FreeSWITCH leg, or must FreeSWITCH return 183/200 with a held or silent stream? A Task 0 prototype settles it; the tenant-visible risk is a leg billed or counted as a call, or carriers seeing a phantom call.
2. Assumption: every CPaaS provider can report call status by webhook. If one only supports polling, is polling an accepted fallback for that provider?
3. Assumption: readiness hard-blocks only on missing or unowned items and unhealthy telephony; calling-hours and DNC problems are warnings. Confirm.
4. Which country code applies to uploaded numbers without a `+` (criterion 89)? Assumption: a campaign-level default country, defaulting to the tenant's setting. Confirm, or require a `+` on every number.
5. Who owns purge and erasure of `calls` rows (callee number, extracted variables, transcripts) that belong to the Conversation service: does Campaigns call a deletion interface, or does the owning service run its own retention? Transcript retention is unspecified in criteria 94 and 97.
