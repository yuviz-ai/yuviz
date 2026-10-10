# PRD: One-click CRM and scheduling integrations

## Problem
Every customer who buys the platform registers bespoke custom APIs by hand (endpoint, auth scheme, parameters, field mapping) even for concepts that are identical across customers: look up a contact, create a lead, log the call. Most customers already run a CRM or scheduler, so this is repeated, error-prone setup by non-engineers, and it blocks agents from using the customer's system of record until someone finishes it. Tenant admins want to connect their CRM the way they connect any SaaS app: click, sign in to the CRM, done.

## How this is solved elsewhere
- Retell AI ships native Salesforce, HubSpot and Cal.com integrations (verified from Retell's own blog via search snippets). It describes two-way contact sync every five minutes keyed on phone number, configurable field mappings, and call activity logged back. The "configurable field mappings" part is exactly what the request wants users to NOT do; unverified whether Retell lets the user skip it.
- Vapi has no native Salesforce or HubSpot connector in its docs (per third-party comparison snippets; treat as unverified against Vapi's current docs). Any CRM goes through generic Custom Tools or webhooks. That is essentially what this platform does today, and it is the gap the request is trying to close.
- Nango and Pipedream Connect sell managed OAuth (consent flow, encrypted token storage, refresh before expiry, persisting rotated refresh tokens, per-tenant scoping). Merge offers hosted auth plus a unified data model; a Nango-authored comparison notes that with Merge the customer authorizes the unified-API vendor rather than the platform.
- HubSpot has two auth paths, a private-app token for one workspace and OAuth for multi-account distribution. Only OAuth fits "click and connect" across many customers.
- The real fork in the road is the write-back tradeoff. Retell markets full two-way sync and logging. Read-first is lower risk. Recommendation: treat read (look up a contact) as the first capability and make write-back (create lead, log call) an explicit, separately-granted, auditable capability, not an automatic consequence of connecting. Write-back into a customer's system of record is the destructive path. Whether v1 includes write-back at all is the user's decision (open question 3); this PRD carries both possibilities and marks which criteria depend on the answer.
- Recommendation on shape: do NOT follow Retell's two-way periodic sync (listed under Out).
- Recommendation on framing: cal.com is a scheduling tool, not a CRM (see open question 2). Retell groups it with CRMs anyway, so the market treats these as one "integrations" catalogue.

## Scope
- In: A dedicated "Integrations" tab in the admin console listing available integrations with per-tenant connection state. The named providers are Salesforce, HubSpot, Zoho, Dynamics, and cal.com (cal.com's inclusion in v1 is contingent, see below).
- In: A tenant admin connects a listed integration by signing in to the third party and approving access. They never enter endpoints, auth type, credentials by hand, or field mappings.
- In: Disconnect, and reconnect after expiry or revocation, from the same tab, with visible connection health.
- In: Agents of a connected tenant can look up a contact (by phone number) during a call.
- In (contingent on open question 3): Agents can create a lead and log the call, subject to the write-back controls.
- In (contingent on open question 2): cal.com is a catalogue entry with its own capabilities (check availability, book a slot), not the three CRM capabilities.
- In: Hand-registered custom APIs for internal APIs keep working exactly as today, unaffected.
- Out: Bulk or periodic two-way sync of a customer's contacts, accounts, or history into this platform.
- Out: Customer-editable field mappings or custom-object support for integrations.
- Out: CRMs beyond the five named; a self-serve "bring your own OAuth app" flow.
- Out: Removing or migrating existing custom APIs.

Assumption: "Log the call" means creating a call/activity record in the CRM containing outcome, duration, and a summary, not the full transcript or recording. Assumption: contact lookup key is the caller's phone number. Assumption: v1 is one connection per integration per tenant.

### Which criteria are committed and which are waiting on an answer
Committed regardless of answers: 1-13, 14, 16-26, 32, 33, and 34 (with its stated assumption).
Contingent, do not design against them as settled until the named question is answered:
- Open question 3 (is v1 read-only?): criteria 27-31. If v1 is read-only, these move to a later PRD and the tab must not offer a write-back control. Criteria 5, 12-14 and 16-26 still apply to lookup.
- Open question 2 (is cal.com in the v1 catalogue, and is the framing an integration catalogue?): criteria 35-38. If cal.com is out of v1, delete 35-38 and remove it from the tab; criteria 1-13 and 32 still hold for the remaining providers. If cal.com is in, the phrase "CRM" in criteria 21-31 applies only to the four CRM providers.
- Open question 1 (build OAuth in-house versus managed OAuth service): criterion 15 (where and how credentials are held) and, to a lesser degree, criteria 5, 6, 23 and 26 (revocation, reconnection and refresh behaviour). The outcomes stated are required either way; who holds the tokens is not decided. If a third-party service holds tokens, criteria 14, 15 and 18 apply to that service as well, and the customer-trust implication must be accepted by the user first.
- Open questions 5, 6, 7 and 8 also change specific criteria (24-25; 8; 21; provider list) but do not change which criteria exist.

## Acceptance criteria
Connect and disconnect
1. Given a tenant admin on the Integrations tab, when they click Connect on an integration and approve access at the third party, then they return to the tab and see that integration as Connected without entering any endpoint, credential, or mapping.
2. Given a tenant admin, when they deny access at the third party or abandon the flow, then the integration remains Not connected, an understandable message is shown, and no partial credentials are stored.
3. Given a connect flow that returns to the platform, when the callback's state value is missing, tampered with, expired, already used, or was issued to a different tenant or user, then the callback is rejected, nothing is stored, and no tenant's connection state changes.
4. Given an admin who started a connect flow, when at callback time their account has been deactivated, demoted below the connecting role, or their tenant has been deleted, then the connection is refused and nothing is stored.
5. Given a connected integration, when an admin clicks Disconnect, then the platform's stored credentials for it are deleted, the third party's token is revoked where the provider supports revocation, and agents can no longer use it on the very next call. (Where the credentials are held depends on open question 1; the outcome does not.)
6. Given a third party that has revoked access or a refresh token that no longer works, when that is detected, then the tab shows the integration as Needs reconnection and an admin can reconnect from that tab.
7. Given a provider whose API base URL is specific to the customer's instance (for example a Salesforce or Zoho instance), when the admin connects, then the platform determines that URL itself and the admin is never asked for it.

Permissions
8. Given a user whose role is not permitted to connect integrations (Assumption: tenant admin and above only; see open questions), when they open the Integrations tab or attempt connect, disconnect, or change write-back settings, then they can at most view state, and the attempt is refused server-side, not only hidden in the UI.
9. Given a role that has no console access at all (for example an agent-role user), when they log in, then they land on their normal destination and never see the Integrations tab or an error caused by it.
10. Given a user who is deleted or demoted, when they present a still-unexpired session, then they cannot connect, disconnect, or alter integrations (revocation must take effect for these actions rather than waiting for the session to expire).
11. Given a platform-level (no-tenant) admin viewing a tenant, when they act on that tenant's integrations, then the action is applied to the tenant they explicitly selected and is recorded with their identity.

Tenant isolation and credential handling
12. Given tenants A and B each connected to the same provider, when an agent of A runs a lookup, create, or log, then the request uses only A's connection and can never read, use, or overwrite B's connection or B's CRM data.
13. Given a tenant admin of A, when they request, list, disconnect, or reconnect a connection identified by an id belonging to B, or one that does not exist, then the response is identical in status, body, and shape in both cases, so existence in another tenant is not revealed.
14. Given any stored OAuth token, refresh token, or client secret, when it is read back through any API, UI, log line, error message, audit entry, transcript, call record, or analytics event, then its value never appears; only state and provider account label are visible. This applies wherever the credentials are held, including any third-party service used for them (open question 1).
15. Given stored integration credentials, when they are at rest, then they are encrypted and are not resolvable through any mechanism a tenant can name in its own configuration (for example a tenant-authored custom API cannot reference an integration's stored token). Contingent on open question 1: the location of the store (platform versus a managed OAuth service) is undecided, and if a managed service is chosen, this criterion must additionally hold for that service's tenant separation.
16. Given a tenant's tool configuration, call flow, or prompt that names an integration or connection id, when it runs, then the platform uses only a connection verified to belong to that same tenant; an id naming another tenant's connection fails, and the failure is indistinguishable from the id not existing.
17. Given all access to stored connections and to CRM data returned during calls, when a cross-tenant test runs, then the test fails if any single enforcement layer (application-level or database-level) is removed.
18. Given contact and lead data fetched from a CRM, when a call ends, then that data is not retained beyond what the call record and audit entries already keep, and no CRM data is written into a shared or cross-tenant cache, log, or vendor service. Assumption: exact retention of the returned contact fields inside the call session is decided in design, but it must be tenant-scoped and listed in the design's data-handling section.
19. Given the outbound calls to a CRM, when made, then they use encrypted transport only, reach only the provider's own hosts, and reject a base URL that resolves to a private, loopback, or metadata address.
20. Given a request to a third party made on behalf of a tenant, when the third party returns an error or unexpected payload, then error text shown to the caller, agent, or admin contains no token, secret, or another customer's data.

Runtime behaviour on a call
21. Given a tenant with a connected CRM and an agent with the lookup capability enabled, when the caller's number matches exactly one contact, then the agent receives that contact's name and the fields needed to serve the call, and no more fields than the capability defines. (The fixed field set is open question 7.)
22. Given a lookup that matches no contact or more than one contact, when the agent handles it, then the agent is told "no match" or "ambiguous", never a guess, and the call continues.
23. Given an access token that has expired mid-call, when the agent makes a request, then the platform refreshes it and completes the request without the caller noticing, provided the refresh token is valid.
24. Given a refresh that fails, or a CRM that is down, slow, or rate limiting, when the agent makes a request mid-call, then the request fails within a bounded time (Assumption: no longer than the existing custom-API call timeout), the agent continues the call without the CRM data, and the failure is recorded on the call record. The call is never dropped or left silent because of the CRM. (Whether continuing is right for every agent is open question 5.)
25. Given a tenant with no connection for an integration, or a connection needing reconnection, when an agent that expects it runs, then the call proceeds without it and the admin sees the problem on the Integrations tab.
26. Given a tenant with simultaneous calls, when several requests need a refreshed token at the same time, then the connection is not left with a stale or invalidated refresh token and no call sees another call's data.

Write-back controls (CONTINGENT on open question 3: apply only if v1 includes write-back; otherwise defer to a later PRD)
27. Given a newly connected integration, when no admin has enabled write-back, then create-lead and log-call are unavailable to agents and lookup alone works.
28. Given write-back enabled by an authorized admin, when an agent creates a lead or logs a call, then exactly one record is created in the CRM per intended action, and a retry of the same action after a timeout does not create a duplicate.
29. Given a create-lead or log-call attempt that fails or is rejected by the CRM, when it happens, then the failure is recorded, the agent does not tell the caller it succeeded, and nothing is reported as written that was not.
30. Given any write into a CRM, when it succeeds or fails, then an audit entry records tenant, integration, action, the agent and call that triggered it, and outcome, and is visible to tenant admins.
31. Given the third-party grant, when the platform requests access, then it requests only the permissions needed for the enabled capabilities (lookup only: read-only scope, where the provider allows that). If v1 is read-only, this criterion still applies and requires read-only scope only.

Integrations tab and coexistence
32. Given the console navigation, when a permitted user opens the console, then an Integrations entry exists alongside the existing entries and shows each integration's state: Not connected, Connected, Needs reconnection.
33. Given a tenant with custom APIs, when the Integrations feature ships, then existing custom APIs, their agents, and their calls behave identically, and internal APIs are still registered by hand as today.
34. Given a tenant that has both a hand-registered custom API and a connected integration serving the same concept, when an agent has both, then which one is used is deterministic and visible to the admin. Assumption: the tenant admin sees both and none is silently preferred; final rule is open question 4.

Scheduling capabilities for cal.com (CONTINGENT on open question 2: apply only if cal.com is in the v1 catalogue; if it is out, delete 35-38 and remove it from the tab)
35. Given a tenant with cal.com connected and an agent with scheduling enabled, when the agent checks availability for a requested date range, then it receives only open slots from the connected cal.com account, or "no availability", never another tenant's calendar data.
36. Given an open slot the caller accepts, when the agent books it, then exactly one booking is created in the tenant's cal.com account with the caller's name and phone number, and a retry after a timeout does not create a duplicate booking.
37. Given a slot that was taken between check and booking, or a cal.com rejection or outage, when the agent attempts to book, then the agent is told the booking failed, does not tell the caller it succeeded, and the failure is recorded on the call record.
38. Given a cal.com connection, when the admin connects, disconnects, or the connection needs reconnection, then criteria 1-11 and 32 apply to it unchanged, and booking is subject to the same audit entry as criterion 30 (audit of writes) regardless of the answer to open question 3, because a booking is a write into the customer's system. Assumption: cal.com's own auth method (OAuth or API key) is unverified; if it is not OAuth, criteria 3 and 6 apply only to the extent the method has a consent step or expiry.

## Constraints
- OAuth reality check: the request states the platform has no OAuth support. Corrected picture from the code: `services/toolexec/auth_schemes.py` supports `none`, `api_key`, `bearer`, and `oauth2_client_credentials` (a machine-to-machine token fetched with a stored client secret and cached in memory, keyed by tenant and custom API). Google OAuth exists in `services/config/google_oauth.py` for console sign-in only. Neither is an authorization-code flow for connecting a customer's third-party account. There is no user-consent redirect, no stored refresh token, no refresh rotation, no revocation, no per-instance base-URL discovery. "Click and connect" requires all of these. This is very likely the largest piece of work; see open questions.
- Tenant secret handling: `services/toolexec/auth_schemes.py` resolves tenant credentials only from a tenant-scoped secret root and forbids `env:` and `k8s:` platform references. Integration credentials must not widen that boundary.
- Outbound endpoint safety: `services/toolexec/custom_apis.py` (`resolve_and_validate_endpoint`, host allowlist, private-IP denial) already defines SSRF protection for tenant-registered endpoints. Integration traffic needs equivalent protection.
- Tenancy: tenant-owned rows carry `tenant_id` and are read with an explicit `tenant_id` predicate (`services/toolexec/custom_apis.py`). Every service still connects as a BYPASSRLS role (`scripts/start_local.sh:59-62`), so RLS alone is not a testable control; see `tests/test_rls_coverage.py`.
- Identity: `services/config/deps.py` resolves identity from the JWT alone, so deletion or demotion does not revoke access until token expiry. Criterion 10 needs an explicit answer to this; it is not free. It is shared by Knowledge, DID, and Campaigns.
- Navigation: the console nav lives in `admin-ui/components/AppShell.tsx` (nav entries near line 135); role-based landing must not send roles without console access to a page they cannot use.
- API style: Config service routers under `services/config/routers/` (for example `tool_catalog.py`, prefix `/tools`); tool execution at call time runs in `services/toolexec` and `services/conversation/tools`.
- Log and data sinks: anything that already flows through call logs, session variables, transcripts, or conversation-session records may now carry CRM data; the design must enumerate those sinks.

## Open questions
1. OAuth does not exist for this use. Do we accept building the authorization-code flow, token storage, refresh (including refresh-token rotation and concurrent-refresh safety), revocation, and per-instance URL discovery ourselves, or use a managed OAuth service (Nango, Pipedream Connect, and Merge are examples)? Managed services mean a third party holds every customer's CRM tokens, which is a data-handling and customer-trust decision; building in-house means owning provider app registrations, and for Salesforce, HubSpot, Zoho and Dynamics, each provider's app review or verification process. This decision drives scope and must be made before design, not found during implementation. Governs criterion 15 and shapes 5, 6, 23, 26.
2. Is cal.com in the same catalogue? It is a scheduling tool, not a CRM, and its natural capabilities (check availability, book a slot) are not look up / create lead / log call. Is the truer framing an "integration catalogue" with per-integration capabilities, and should the tab and the three named capabilities be defined that way? Its auth (OAuth or API key) is also unverified here. Governs criteria 35-38. Until answered, cal.com is drafted as a catalogue entry with its own scheduling capabilities.
3. Read-only versus write-back: is v1 read-only (lookup), with create-lead and log-call in a later release? Criteria 27 to 31 assume write-back ships but is off by default and separately enabled. If write-back is deferred, they move to a later PRD. Governs criteria 27-30.
4. Does this replace hand-registered custom APIs for CRM-shaped concepts, or sit alongside them? Criteria 33 and 34 assume alongside; a replacement would need a migration path for existing tenants.
5. Mid-call token failure: criteria 24 and 25 assume the agent continues without CRM data. Is that acceptable for every use case, or must some agents end or transfer the call when a CRM is required (for example when an agent must not proceed without verifying the account)?
6. Who in the customer organisation may connect a CRM? Connecting grants the platform access to business data. Assumption in criterion 8 is tenant admin only; confirm whether it should be a narrower or separate permission, and whether a second admin must confirm write-back.
7. Are field-level data limits acceptable, meaning the platform returns a fixed set of fields per capability with no customer mapping? Customers with heavily customised CRMs (custom fields, custom objects) may need mappings later; is a fixed schema acceptable for v1?
8. Provider availability: some CRMs need per-instance setup, a paid tier, or app-marketplace approval before customers can connect (for example the HubSpot and Salesforce marketplace processes; unverified for our case). Which of the five must work at launch, and which can follow?
