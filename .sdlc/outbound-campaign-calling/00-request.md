Design outbound (campaign) calling end to end, for both our own SIP infrastructure and CPaaS providers, including the configuration UX.

Two outbound paths, chosen per campaign by where the caller-ID DID lives:
(A) Own SIP infrastructure (Kamailio + FreeSWITCH pool).
(B) CPaaS providers (Vobiz, Cloudonix, and future ones through the existing telephony provider interface): the provider's REST API places the call and streams media to our Telephony service / media bridge, as today's REST-originate path (services/campaigns/telephony_originate.py) does.
Both paths share the same campaign job model, outcome tracking, retries and crash-safety guarantees.

Path A — call path: the Campaigns service gets its own SIP client and sends a trigger INVITE to Kamailio, which picks a FreeSWITCH node through the same dispatcher as inbound. Kamailio is the only component that knows the FreeSWITCH pool (today Campaigns does ESL originate to one FreeSWITCH and bridges leg B back through Kamailio to the agent DID — a hairpin that breaks with 2+ FreeSWITCH nodes).
Headers on the trigger: X-Call-Direction: outbound (FreeSWITCH branches on it to its outbound path; calls are recorded as outbound), X-Campaign-Id, X-Contact-Id, X-Callee, X-Caller-Id, X-Agent-DID, plus an idempotency key. Kamailio strips these headers from any source other than the Campaigns host (toll-fraud guard); FreeSWITCH accepts outbound triggers only from Kamailio.
Legs: the chosen FreeSWITCH node places the callee leg (out via Kamailio) and runs the AI on that same leg (audio fork to the Gateway) — one audio leg, no hairpin; a transfer bridges a human leg on the same node. The trigger leg carries no media (prototype needed to settle how).

Path B — CPaaS: outbound originate through the provider's API with the provider's idempotency mechanism where it exists; outcomes from provider status webhooks; the provider-owned caller-ID DID; provider rate limits and concurrency quotas respected; calls recorded with direction outbound. Webhooks authenticated per provider.

Outcomes (both paths): send-and-forget at the request level, but every call is a tracked job; outcomes (answered/busy/no answer/declined/failed/ended) arrive through a durable event stream keyed by contact and attempt; each attempt has a deadline; max_concurrent_calls slots are freed on call end.

Crash safety (both paths): dial-attempt state is persisted in Postgres before anything is sent; idempotency key = contact_id:attempt — on path A claimed in Redis (SET NX) by FreeSWITCH before dialing, on path B passed to the provider and checked before re-sending — so a re-sent or duplicated request never creates a second call. If the Campaigns worker crashes at any point, calls in progress are unaffected, outcomes are not lost, and recovery never causes a duplicate call or redials someone who already answered (check the call record before retrying). Retries use a gap (not the 5 s pacing). Fixes #116 (contacts stuck in 'calling'); removes the ESL dependency behind #117.

Configuration UX: everything a tenant needs to set up and launch a campaign — outbound agent, caller ID (a DID the tenant owns, on our SIP or a CPaaS provider), contacts upload, retry count and gap, calling hours and timezone, concurrency — with validation and a readiness check before launch.

Benchmark: compare the design and UX with Orato.ai and other outbound AI calling products on the market.

Out of scope: call recording.
