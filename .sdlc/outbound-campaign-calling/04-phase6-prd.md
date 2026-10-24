# PRD: Outbound campaigns, Phase 6: capacity API and campaign UI

## Problem
The capacity limits that already gate dialing (platform outbound cap, CPS per trunk/provider, per-tenant cap) can only be changed by writing to the database, so a platform operator cannot set or audit them. A tenant admin creating or running a campaign cannot see the readiness blocks the service already computes, why a campaign is waiting, or what happened to each contact's call attempts. The backend for readiness, progress and attempts exists (`services/campaigns/readiness.py`, `routers/campaigns.py`); none of it is surfaced in `admin-ui/app/(console)/campaigns/`, which contains no readiness, wait-reason, retry-gap or attempt UI today.

## How this is solved elsewhere
- Vapi: one account-wide concurrency number (default 10 slots), raised by purchase. Waiting dials queue silently, and community threads show users cannot tell why a campaign stalls (a 134-number campaign showing only 11 calls). Lesson: the wait reason must be visible. We already compute `wait_reason`; show it with the count waiting.
- Genesys Cloud: layered limits, org-wide CPS plus per-campaign CPS, with higher values gated behind an operator process. This matches our platform / tenant / campaign layering. Recommendation: keep the platform operator as the only writer of platform and tenant limits; tenants set only the campaign-level cap, and the readiness warning already tells them when the tenant limit overrides it.
- Bolna: retry config is attempts plus a wait between them, with a hard maximum. This matches our `max_attempts` and `retry_gap_minutes`. Recommendation: do not add per-status retry triggers (Bolna has them); that is Phase 7 outcome work.
- Unverified: Twilio and Retell concurrency/CPS behaviour (not found in search); Bland not checked.
- Reshaped from the request: a full cap is never a readiness block. `readiness.py` already treats capacity as "wait, not cannot dial", so Start stays enabled when the campaign is merely waiting. Only the existing block checks disable it.

## Scope
- In: Superadmin can read and change platform outbound limits (total capacity, inbound reserve, outbound cap) and CPS limits per scope, and read and change any tenant's outbound cap, through audited API routes.
- In: Superadmin sees a capacity card in the console showing live outbound usage against platform limits, CPS usage per scope, and each tenant's cap and usage; an over-limit save shows the server's error inline.
- In: Campaign create and edit forms offer retry gap (minimum 1, default 60), enforce attempts of at least 1, and a caller-ID select that badges each number's path (native, Vobiz, Cloudonix).
- In: Campaign detail page shows a readiness panel (blocks red, warnings amber, each linked to its form field), a live wait reason with waiting count, effective concurrency limit, and Start/Resume disabled while any block exists.
- In: Campaign detail shows attempts under each contact: outcome, disposition (answered calls only), masked callee number, attempt number, timestamp.
- In: When billing is enabled, readiness shows a per-contact and per-campaign cost estimate.

## Out
- Business outcomes, DNC management UI, callbacks, compliance, CRM sync (Phase 7).
- Billing implementation: charging, balances, credit checks (Phase 8). This phase only displays the estimate the existing billing seam returns.
- Tenant-admin visibility into platform limits or tenant caps beyond the existing campaign-level readiness warning.
- Per-status retry rules, new retry policies, or changes to dial admission logic.

## Acceptance criteria
Capacity API
1. Given a superadmin, when they GET the platform outbound limits, then the response has total capacity, inbound reserve, outbound cap, and the CPS limit for each scope.
2. Given a tenant admin, viewer, or any non-superadmin, when they call any capacity route (GET or PUT, platform or tenant), then the response is 403 and no value changes.
3. Given a non-superadmin, when they call a tenant cap route with their own tenant id, another tenant's id, or a random UUID, then status and body are identical (the role check runs before any tenant lookup).
4. Given a superadmin, when they GET or PUT a tenant cap for a UUID that is not a tenant, then the response is 404 and nothing is written.
5. Given a superadmin, when they PUT an outbound cap above total capacity minus inbound reserve, then the response is 400 with the reason and the stored row is unchanged.
6. Given a superadmin, when they PUT a non-integer, zero, negative, or missing-required value for any limit (cap, CPS, capacity), or a CPS scope that does not match a known scope pattern, then the response is 400 and the row is unchanged.
7. Given a superadmin sets a tenant cap to null, then the tenant has no tenant-level cap and a later GET returns null.
8. Given any successful write to platform limits, a CPS limit, or a tenant cap, then exactly one audit row exists with the acting user, entity, and old and new values.
9. Given a rejected write (400, 403, 404), then no audit row is written.
10. Given two concurrent PUTs to platform limits, then the final stored row is one of the two requested values and never a mix of their fields, and each audit row's old value equals the row state its write replaced.
11. Given a superadmin, when they request live usage, then the response reports in-flight outbound calls counted the same way admission counts them (the existing in-flight definition) for the platform and per tenant, and CPS sends in the last second per scope.
12. Given a route walk over the capacity router (derived from `app.routes`), then the route count equals the expected size and every route enforces the superadmin check.

Campaign forms
13. Given the create or edit form, when the user leaves retry gap blank, then 60 is submitted; when they enter 0 or a negative, then the form blocks submit and the API returns 400 for the same input.
14. Given the create or edit form, when max attempts is 0 or negative, then submit is blocked and the API returns 400.
15. Given a tenant with native, Vobiz and Cloudonix numbers, when the caller-ID select opens, then each option shows its path badge, and a number the tenant does not own cannot be submitted (API returns 422 as today).
16. Given a saved campaign, when the edit form loads, then retry gap, max attempts and caller ID show the stored values, and saving unchanged values changes nothing other than an audit-trailed no-op update.
17. Given any validation failure from the API, then the form shows the server message next to the offending field and keeps the user's input.

Campaign detail
18. Given a campaign with at least one block check, when the page loads, then each block renders red with its message and a link that focuses its form field, each warning renders amber, and Start/Resume is disabled.
19. Given every block check id (`agent_unavailable`, `caller_id_missing`, `caller_id_not_owned`, `caller_id_not_routed_to_agent`, `no_pending_contacts`, `timezone_invalid`, `provider_not_ready`, `provider_not_campaign_capable`, `no_healthy_sip_node`), then a test produces each one and asserts the 409 body from Start and Resume lists it with `level=block`.
20. Given a blocked campaign, when Start or Resume is called directly, then the response is 409 with the checks and the campaign status is unchanged.
21. Given a campaign whose only checks are warnings (including `campaign_cap_above_tenant_cap`), then Start/Resume is enabled and succeeds.
22. Given a running campaign that is waiting on a cap or CPS, then the page shows the wait reason in plain words, the number of contacts waiting, the time it has waited, and the effective concurrency limit; given it is not waiting, the wait indicator is absent.
23. Given a paused campaign, then the page shows a note that in-flight calls finish and no new calls start.
24. Given the readiness, progress and attempts requests are independent, when one returns 403 or 500, then the other sections still render and the failed section shows its own error.
25. Given a campaign with live calls, then progress and attempts refresh on a timer without a full page reload, and polling stops when the page is hidden or the campaign is not running.

Attempts and PII
26. Given a contact with attempts, then each attempt row shows outcome, attempt number, timestamp, and for answered calls the call disposition; non-answered attempts show no disposition.
27. Given any attempts or capacity response, then no field contains a full callee number: the callee appears only as a mask showing at most the last 4 digits, produced by the server.
28. Given a campaign id belonging to another tenant, when a fixed tenant-B principal calls readiness, progress, attempts, contacts, start, pause, resume or patch, then status and body equal those for a random UUID on every route (existing route-walk test stays green with any new campaign route added).
29. Given a viewer, when they open campaign detail, then data renders and Start, Resume, Pause and edit controls are not shown; the API returns 403 on those writes.
30. Given the logs produced by these routes and the audit rows, then neither contains a full callee number.

Cost preview
31. Given billing is enabled and the estimate is returned, then the readiness panel shows cost per contact and per campaign with currency.
32. Given billing is disabled or the estimate is null, then no cost line renders and no error shows.
33. Given a cost estimate, then it never blocks Start/Resume in this phase.

Console surface
34. Given a tenant admin or viewer, when they open the tenants page, then the capacity card and cap field are not rendered and no capacity request is sent.
35. Given a superadmin saves an over-capacity cap, then the inline error shows the server message, the field keeps the entered value, and the card's displayed stored value is unchanged.
36. Given the admin UI, when type-check and build run, then both pass.

## Constraints
- Capacity routes live in Campaigns (`services/campaigns/`), reusing `capacity.py` write functions and their `LimitError` messages, which are written to be safe to show callers. Tenant cap and platform limit storage already exist in `database/schema.sql`; no new limit semantics.
- Tenant isolation follows lesson 2 and the existing `_campaign_for` helper in `services/campaigns/routers/campaigns.py`: per-caller invariant status and body, no ids echoed. Superadmin check precedes any lookup. Scope by `tenant_id IS NULL`, not role name, where service accounts are involved (lesson 24).
- Audit rows use `services/campaigns/audit.py`, whose action values are created/updated/deleted; a limit write is "updated". Old and new values are captured in the same transaction as the write (lesson 8: single conditional update, no check-then-act).
- Guards are `await`ed (lesson 38); route enumeration is mechanical (lessons 12, 29).
- The attempts endpoint today returns no callee or caller ID (`attempts.py` `list_for_campaign`), and no disposition. Adding disposition means joining the call record; it must carry a `tenant_id` predicate, not rely on RLS (lesson 36), and must not surface other call fields.
- Frontend: Promise.all must not be used across role-dependent fetches (lesson 21); verify by driving the real UI as superadmin, tenant admin and viewer before sign-off (lesson 23); role landing and nav per lesson 22. Callers of the changed campaign form and any changed route response shape are grepped across `admin-ui/`, `scripts/`, `docs/` (lesson 17).
- Retry gap and max attempts constraints already exist in `services/campaigns/schemas.py` (`ge=1`, default 60 for gap); the form must mirror, not redefine them. Existing campaigns with out-of-range stored values must still load in the edit form.
- Readiness check ids and levels are those in `services/campaigns/readiness.py`; UI maps ids to field links and must tolerate an unknown id by showing its message without a link.
- Cost display uses the existing billing seam (`services/campaigns/billing.py`); Phase 8 will replace the implementation without changing what this page reads.

## Open questions
1. Should a tenant admin be able to read (not write) their own tenant's outbound cap and usage? The request says superadmin only, so this PRD assumes no; the readiness warning is their only signal. Answer changes whether a tenant-facing read route is built.
2. The existing contacts list likely returns full phone numbers to tenant admins who uploaded them. Assumption: criterion 27 covers attempts and capacity responses and the new UI, and the contacts list is unchanged. If the intent is no full number in any response, the contacts list and its consumers change too.
3. Is the attempt "disposition" the call-record disposition (the only existing field, set on the call) or does it wait for Phase 7 outcome fields? Assumption: the call-record disposition for answered attempts, null otherwise.
