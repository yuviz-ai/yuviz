# Code review
VERDICT: GREEN

Round 2 (final). All three round-1 blocking findings are genuinely fixed — verified through the real
wiring, not just by the presence of a route/constant/test:

1. **AC16 reachable for a tenant admin (was blocking #1) — fixed.** `handleEditSave`
   (`admin-ui/app/tenants/page.tsx:96-140`) now issues two independent, individually try/caught calls,
   with `updateTenantConcurrency` attempted FIRST and unconditionally on change, so the superadmin-only
   `PATCH /tenants/{id}` 403 can no longer abort the handler before the concurrency write. Traced the
   whole path for `role="admin"`: `Accounts` is in `MANAGEMENT_ITEMS` (`AppShell.tsx:109`) and is gated
   only on `isSupervisor`, so an admin sees the nav item; `GET /tenants` scopes on
   `current_user.tenant_id` (`routers/tenants.py:15-24`) and returns exactly their own tenant row;
   the row's Edit button (`page.tsx:188`) is not role-gated; `PATCH /tenants/{id}/concurrency` is
   `require_role("superadmin","admin")` with the own-tenant check re-read from the `users` row.
   Errors from either call are surfaced and the modal stays open. `tsc --noEmit` is clean (the aliased
   `concurrencyChanged` narrowing on `number | ""` does compile).

2. **Denial-audit key (was blocking #2) — fixed.** `_DenialAuditKey = (user_id, tenant_id)` and
   `_denial_audit_windows` is keyed on it (`services/config/live_calls.py:274-316`). A superadmin's
   cross-tenant probe now lands its own row per tenant, and
   `test_cross_tenant_denials_within_the_window_get_separate_rows` asserts exactly one `denied` row for
   each of the two tenants (it would fail on the old `user.id` key: tenant B's count would be 0).
   Same-tenant repeats still aggregate (`… == 1` for 20 attempts), so the growth bound holds. Key space
   is bounded by users x existing tenants — the 404 branch attributes to the caller's own tenant, so
   attacker-supplied slugs cannot inflate it.

3. **Throttle sizing + a non-sanitized test (was blocking #3) — fixed.** `FixedWindowCounter(limit=4,
   window_seconds=5)` (`services/config/app.py:234`) against a 5s poll gives ~3-4x steady-state
   headroom — enough for 2-3 tabs plus one tenant-switch/resume re-fetch.
   `test_throttle_tolerates_realistic_multi_tab_traffic_without_any_reset`
   (`services/config/tests/test_live_calls.py:695-722`) calls no `_reset_throttle()` anywhere, drives the
   real shared `app.state.live_calls_throttle` and the shipped limit (lesson 25), passes 4 ordinary
   requests from one user in one window, and still proves the bound with a 429 on the 5th. It would have
   failed under `limit=1`.

Open, non-blocking:

1. [minor] `_mask_msisdn` returns short numbers completely unmasked — `services/config/live_calls.py:49`
   — fails when: `calls.caller_number` is <= 9 chars (a short code / national number such as `5551234`);
   the `len(number) <= _MASK_PREFIX_LEN + _MASK_SUFFIX_LEN` branch returns the raw MSISDN, which AC4 says
   must never leave the API — fix: mask all but the last 2-4 digits instead of returning the input.
   *(Carried unchanged from round 1.)*

2. [minor] `interventions_pending` counts any live call that has EVER had an intervention row —
   `services/config/live_calls.py:60` — fails when: an operator requests Listen and gets
   `outcome='unavailable'`; nothing clears or supersedes that row, so the KPI reads "1 pending" for the
   rest of the call's life and a second request on the same session is still counted once — fix: rename
   the KPI for what it measures, or predicate on `outcome NOT IN ('granted','denied')` with a recency
   bound. *(Carried unchanged from round 1.)*

3. [minor] Denial aggregation overwrites the recorded `session_id` within a tenant window —
   `services/config/live_calls.py:296-303` — fails when: one admin probes 20 different guessed session
   ids in 60s (exactly what `test_twenty_rapid_denials_…` does): a single audit row survives with
   `count=20` and only the LAST session_id, so AC11's "which call" is lost for the other 19 — acceptable
   as the chosen growth bound, but worth naming: fix would be keying on
   `(user_id, tenant_id, session_id)` with a per-user cap, or appending ids to a bounded array.

4. [minor] A cap once set cannot be returned to "not set" — `admin-ui/app/tenants/page.tsx:107-108`
   with `TenantConcurrencyUpdate.max_concurrent_calls: int = Field(ge=1)` (`schemas.py:71`) — fails when:
   an operator mistypes 2000, saves, reopens Edit, clears the field to blank and saves: `concurrencyChanged`
   is false (`maxConcurrentCalls !== ""` guard), the modal closes with 2000 still stored and no feedback,
   while the placeholder said "Not set" — fix: either accept `null` on the route and send it, or disable
   the field's clear affordance. No AC requires clearing, so this is scope, not a regression.
