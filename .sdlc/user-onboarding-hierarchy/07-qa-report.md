# QA report: invite-based user onboarding
Built from: `125da01` (merge of PR #19, `feature/user-onboarding-hierarchy`)
Environment: macOS, native path per `docs/setup.md`. Fresh Postgres 16 DB `qa_voiceai` created
by `psql -v ON_ERROR_STOP=1 -f database/schema.sql` (exit 0, no skipped statements). Config Service
run with `venv/bin/python3 -m uvicorn services.config.app:app --host 127.0.0.1 --port 8000`
(`POSTGRES_DSN=postgresql://chandankumar@localhost:5432/qa_voiceai`, `JWT_SECRET` set,
`SMTP_HOST=localhost SMTP_PORT=1025 SMTP_STARTTLS=false`, `SMTP_USER` unset,
`INVITE_BASE_URL=http://localhost:3000`). admin-ui via `next dev -p 3000` (Next 16, Turbopack).
Local SMTP capture sink on :1025 writing `.eml` files (the only place a raw token is readable).
UI driven with Playwright/Chromium from `admin-ui/node_modules`; every step screenshotted and
viewed. Server log captured to a file and grepped, never streamed.
Screenshots: `/private/tmp/claude-502/-Users-chandankumar-yuviz/b9642eb3-257d-422e-877f-c8160a694bdc/scratchpad/shots/`
Server log: `…/scratchpad/config.log`

Findings already recorded in `05-security.md`, `05-security-prfix.md` and `05-review-prfix.md`
are not repeated. Nothing in the PRD's **Out** section is reported as missing.

## Defects

1. **[high] A soft-deleted (offboarded) user's JWT keeps full Config API access until it expires
   (~12h). Deleting the account does not de-provision the session.**
   Steps:
   1. As super_admin, invite `defadmin@d.test` as `admin` in the Default tenant; accept the invite;
      log in as that user and keep the token.
   2. As super_admin, `DELETE /users/{defadmin_id}` → `204`.
   3. Replay the old token: `GET /users` → **200** (full tenant roster);
      `POST /invites {"email":"zombie2@d.test","role":"admin",…}` → **201**, invite created,
      email sent, row visible as `pending` on the tenant's Users page.
   4. `GET /auth/me` with the same token → **401**.
   Expected / Actual: Expected a deleted account to lose all API access immediately.
   Actual it keeps read and *write* access (including minting new `admin` invites) for the token's
   remaining lifetime; only `/auth/me` re-checks the database, so the inconsistency is invisible in
   a browser (the UI bounces to /login) but not to anyone holding the token.
   Evidence: `=V3 204`, `=V4 201 {"id":"cd507a84-…","email":"zombie2@d.test","role":"admin"}`,
   `=V5 200`; reproduced 3×: `pwtest@acme.test` post-delete `GET /users → 200` while
   `GET /auth/me → 401`; `alice@acme.test` post-delete `GET /invites → 200`.
   Note: acceptance of the invites such a user plants *is* correctly refused
   (`{"detail":"invite is no longer valid"}` 410), so no account is created — the hole is the live
   session, not the deferred grant. JWT statelessness pre-dates this feature, but this feature made
   invites the only account-creation path and put it behind exactly this session.

2. **[medium] A user demoted to `supervisor` or `agent` keeps full console access until their JWT
   expires — the `CONSOLE_ROLES` gate reads the role from the JWT, not the database.**
   Steps:
   1. Log in as `viewer1@acme.test` (role `viewer`), keep the token.
   2. As super_admin, `PATCH /users/{id} {"role":"agent"}` → `200`.
   3. Replay the old token: `GET /users` → **200**, full user list returned.
   Expected / Actual: Expected `403 role 'agent' cannot access this service` (the design's central
   control: "supervisor and agent are runtime roles with no Config API surface at all").
   Actual the surface stays open for up to 12h. A freshly-minted `agent` token is correctly 403'd
   (`=N3/N4/N6/N8`), so the gate itself works — only the staleness window is wrong.
   Evidence: `=U1 200`, `=U2 200 [{"id":"e8476e40-…"}]`; same shape reproduced with
   `defadmin@d.test` demoted admin→agent still creating an `admin` invite (`=V1 200`, `=V2 201`).

3. **[medium] A malformed UUID returns `500 Internal Server Error` with a traceback in the log, on
   five endpoints of this feature.**
   Steps (any authenticated super_admin token):
   1. `POST /invites {"email":"x@y.test","role":"viewer","tenant_id":"not-a-uuid"}` → **500**
   2. `GET /invites?tenant_id=not-a-uuid` → **500**
   3. `GET /users?tenant_id=not-a-uuid` → **500**
   4. `POST /invites/not-a-uuid/resend` → **500**
   5. `POST /invites/not-a-uuid/revoke` → **500**
   Expected / Actual: Expected `400`/`422` (`05-security.md` control 22 asserts "a malformed
   `tenant_id` or `invite_id` surfaces as asyncpg's `ValueError` → a 400, not a 500 with a
   traceback" — that claim does not hold at the wire). Actual `Internal Server Error` and an
   unhandled `asyncpg.exceptions.DataError` traceback. A *well-formed but unknown* UUID is handled
   correctly (`400 request references an id that does not exist`, `404 invite … not found`).
   Evidence: `config.log:495-524` `ValueError: invalid UUID 'not-a-uuid'` /
   `asyncpg.exceptions.DataError: invalid input for query argument $1`. 5 occurrences, 0 other 500s
   in the whole run.

4. **[medium] An invite can be created into a soft-deleted tenant: `201`, real email sent, shown as
   healthy `pending` — but it can never be accepted.**
   Steps:
   1. As super_admin create tenant `doomed`; `DELETE /tenants/{doomed_id}` → `204`.
   2. `POST /invites {"email":"doom2@d.test","role":"admin","tenant_id":"{doomed_id}"}` → **201**,
      `email_sent: true`; the mail sink receives a real invite link.
   3. Open `/invite#<token>` → **"This invite isn't valid — invite is no longer valid"** (410).
   Expected / Actual: Expected create to refuse a dead tenant the same way accept does. Actual the
   two ends disagree: create validates nothing about the tenant, accept validates everything, so
   the admin is told the invite went out and the recipient gets a dead link with no way back.
   Evidence: `=X4 201 {"id":"9bf8a9f7-…","tenant_id":"71ee1077-…","email":"doom2@d.test"}`;
   sink file `msg-1788784661.502487.eml` `To: doom2@d.test`;
   shot `22b-invite-while-logged-in.png`.
   Related to but distinct from `05-security.md` finding 6 (which is about *pre-existing* invites
   not being revoked when their tenant dies); this is about creating new ones afterwards.

5. **[medium] A rate-limited invitee is told their invite is invalid.**
   Steps (single IP, clean state):
   1. Create a valid pending invite and open `/invite#<token>` repeatedly (each page load is one
      `GET /invites/accept`; the limiter is 10/min + 50/hour per IP, covering GET and POST).
   2. On the 10th request in the window the page renders **"This invite isn't valid — too many
      attempts; try again later"**.
   Expected / Actual: Expected a distinct "please wait a moment" state; the API even returns
   `Retry-After: 60`. Actual the page uses its dead-invite layout, telling a person with a
   perfectly good invite that it is broken, with no retry hint. The bucket is
   `request.client.host`, so a whole office behind one NAT egress shares the 10/min and 50/hour
   budget — 50 new hires accepting on their first morning is enough to lock the rest out for an
   hour, and this environment reached the 50/hour cap during normal testing.
   Evidence: shot `21-throttled-invitee.png`; wire: nine `200`s then
   `429 {"detail":"too many attempts; try again later"}` with `retry-after: 60`.

6. **[medium] The super_admin invite modal defaults to a combination that creates a dead-end
   account: role `admin` + "— Platform (no tenant) —". That account gets an "+ Invite User" button
   whose every submission is 403.**
   Steps:
   1. As super_admin open /users → "+ Invite User". Defaults are Role=`admin`,
      Tenant=`— Platform (no tenant) —`. Type an email, click Send Invite → **201**.
   2. Accept the invite, log in as that user → lands on /tenants, sidebar shows **Users**, page
      shows **+ Invite User**.
   3. Open the modal (it offers admin/supervisor/agent/viewer and *no* tenant selector), submit
      anything → **403 "not permitted to invite this role/tenant"**. There is no combination that
      succeeds: `may_invite` returns `False` unconditionally for `actor_role="admin",
      actor_tenant_id=NULL`.
   Expected / Actual: Expected either the platform-admin shape not to be the modal's default, or
   the Invite UI to be hidden for an actor that cannot invite anyone. Actual the shortest path
   through the feature's own default form manufactures a role whose Users page is a trap.
   Evidence: shots `05-invite-modal.png`, `09-platadmin-modal.png`, `09b-platadmin-result.png`;
   `27-whitespace-email.png` shows the default producing `"tenant_id":null,"role":"admin"`.

7. **[medium] An invite to a non-ASCII email address is created but can never be delivered, and
   resend will fail forever.**
   Steps:
   1. As a tenant_admin, `POST /invites {"email":"björn@acme.test","role":"viewer",…}` → **201**,
      `email_sent: false`. No message reaches the SMTP sink (which accepts everything else).
   2. Same via the UI with `renée@acme.test`: the row appears as normal `pending`; every subsequent
      Resend also returns `email_sent: false`.
   Expected / Actual: Expected either rejection at validation (`InviteCreate.email`'s pattern
   `^[^@\s]+@[^@\s]+\.[^@\s]+$` happily admits these) or SMTPUTF8/IDNA delivery. Actual the API
   accepts an address it structurally cannot send to; after the create-time banner is dismissed
   the invite is indistinguishable from a healthy one, and the only recovery offered (Resend) can
   never work. Accented names are ordinary human input.
   Evidence: `=Q1 email_sent=False`, no new file in the mail sink; shot `10-unicode-invite.png`.

8. **[medium] Resend that fails to send reports success in the UI. Create reports the failure;
   resend does not — so the documented recovery path lies.**
   Steps:
   1. Stop the SMTP relay.
   2. As super_admin on /users, click **Resend** on any pending invite.
   3. API returns `200 {… "email_sent": false}`; the page shows **no banner, no error, nothing** —
      the row just re-renders with a fresh 60s cooldown.
   Expected / Actual: Expected the same warning the create path shows ("Invite created for X, but
   the email failed to send. Use Resend once the SMTP issue is fixed."). Actual silent success.
   AC12 says a failed invite "can be resent" — it can be clicked, but the admin has no way to know
   whether the resend worked, which is precisely the state AC12 exists to get out of.
   Reproduced 3/3 (`smtpdown@acme.test`, `reclaim@acme.test`, and the first run).
   Evidence: shots `13-resend-smtpdown.png`, `26a-resend-silent.png`, `26b-resend-silent.png`;
   `resend 200 email_sent=false` / `banner about failure present: false`.
   Related: the resend still extends `expires_at` by 7 days even when delivery failed.

9. **[medium] Accepting an invite in a browser that already has a session leaves the *old* session
   live; one click puts the new hire inside someone else's console.**
   Steps:
   1. In one browser profile, sign in as `root@platform.test` (super_admin).
   2. In the same profile, open `/invite#<token>` for `crossuser2@acme.test` (a `viewer`) and
      complete "Create account" → "Account created. You can now sign in with your new password."
   3. Navigate to `/users`.
   Expected / Actual: Expected the accept page to clear any existing session (it is a
   "you are a new person now" screen). Actual `localStorage.yuviz_access_token` still holds the
   super_admin JWT; /users renders as **root@platform.test / superadmin**, listing every tenant's
   users, with "+ Invite User" available.
   Evidence: shots `23b-after.png`, `24-still-superadmin.png`;
   `localStorage: {"yuviz_access_token":"eyJhbGciOiJIUzI1NiIsInR5c…"}`.

10. **[medium] AC8 is not implemented for the "accepted" half: an accepted invite cannot be
    revoked, by API or UI.**
    Steps:
    1. Invite and accept `t1@acme.test`.
    2. `POST /invites/{id}/revoke` as super_admin → **409 `{"detail":"invite is not pending"}`**.
    3. On /users the accepted row's Actions cell is `—`; no Revoke button is rendered.
    Expected / Actual: AC8 reads "Given a pending **or accepted** invite, when an authorized admin
    clicks revoke, then the token can no longer be used to accept (revoking an accepted invite does
    not delete the resulting user)." Actual only pending invites are revocable. Not listed in the
    PRD's Out section. Security impact is nil (an accepted token already 410s as "already used"),
    so this is a spec/UX gap rather than a hole. Revoking a *pending* invite works correctly, and a
    double-revoke correctly 409s.
    Evidence: `=M1 409 {"detail":"invite is not pending"}`; shot `08-alice-users.png` (rows
    `t1@acme.test … accepted … —`).

11. **[low] A `viewer` typing `/users` directly is shown "No invites yet." while six pending
    invites exist — a permission error rendered as an empty state.**
    Steps: log in as a `viewer`; the sidebar has no Users item; type `http://localhost:3000/users`.
    The Users table renders in full (server-scoped, correct), and the Invites card says
    **"No invites yet."**
    Expected / Actual: Expected "you don't have access to invites" or no Invites card at all.
    Actual `GET /invites` returns `403 role 'viewer' cannot perform this action` and the failure is
    rendered as an affirmative claim that the tenant has no invites. (Lesson 21's blank-page bug is
    genuinely fixed — the users table no longer blanks — but the independent failure now reads as
    data.)
    Evidence: shot `viewer-_users.png`; `=N1 403`.

12. **[low] One long email address stretches the Invites table to ~2,930px, pushing Role/Status/
    Expires/Actions off screen for every row.**
    Steps: invite an address with a 300-character local part (accepted, `201`); reload /users.
    Expected / Actual: Expected the address to wrap or truncate. Actual the table becomes 2932px
    inside a 1010px card; the first Revoke button sits at x≈3103. The `.content` wrapper is
    `overflow-x:auto` so the buttons are still reachable by scrolling — degraded, not blocked.
    Evidence: shot `10-unicode-invite.png`; measured
    `TABLE.tbl sw=2932 / DIV.card cw=1010`, `revoke box x=3103`.

13. **[low] `email_sent: true` is returned for a message whose `To:` header is emitted empty.**
    Steps: invite `<300 a's>@acme.test` → `201`, `email_sent: true`. The captured message has a
    bare `To:` line with no address.
    Expected / Actual: Expected either rejection or an honest `email_sent: false`. Actual the admin
    is told the mail went out; it is addressed to nobody. Reproduced 2/2.
    Evidence: sink file `msg-1788783820.312048.eml` → `To:` (empty).

14. **[low] A raw JavaScript exception string is shown to the invitee as user-facing copy.**
    Steps: open a valid `/invite#<token>`, fill both password fields, cut the network, submit.
    The card renders **"TypeError: Failed to fetch"** above the form.
    Expected / Actual: Expected "Couldn't reach the server — try again." Actual the browser's
    exception message. Recovery is fine: restoring the network and clicking again succeeds.
    Evidence: shots `25-network-killed.png`, `25b-retry.png`.

15. **[low] `team` is collected at invite time, stored on the user, and displayed nowhere.**
    Steps: invite with Team = `Support`; accept. `users.team = 'Support'` in Postgres, but neither
    the Users table, the Invites table, nor the invite-accept page has a Team column or field.
    Expected / Actual: the PRD makes team an attribute of a tenant-scoped user and says role and
    team are fixed at send time and can only be changed by revoke-and-reinvite — an admin cannot
    see what they fixed, or tell two invites to the same team apart.
    Evidence: `psql → agent1@acme.test|Support`; shots `08-alice-users.png`, `04-users-superadmin.png`.

16. **[low] Roles are displayed with their stored names, not the PRD's display names.**
    The PRD (Hierarchy decision) says `superadmin` "is displayed in the UI … as **super_admin**"
    and `admin` "becomes **tenant_admin**, unchanged in storage". The sidebar, the Users table
    badges, the invite modal's Role dropdown, the /no-access copy and the accept page
    ("… is invited as **admin** on Acme Corp.") all render the raw stored strings.
    Evidence: shots `04-users-superadmin.png`, `05-invite-modal.png`, `07b-alice-load.png`,
    `agent-_users.png`.

17. **[low] Revoke has no confirmation.** A single click on the red Revoke button in the Invites
    table immediately revokes the invite, with no dialog and no undo; the invitee's live link dies
    instantly. Recoverable only by sending a fresh invite.
    Evidence: shot `15b-revoke-dbl.png` (no dialog fired; state went straight to `revoked`).

## Verified working
- Bootstrap: a fresh DB opens on "Create your administrator account"; once a super_admin exists the
  login page permanently drops the "Create your account" link — no self-serve signup (PRD Out).
- AC1: super_admin invites a new email as `admin` for Tenant A → `201`, invite `pending` scoped to
  Acme, real email delivered to the sink.
- AC2: tenant_admin inviting `superadmin` → `403 not permitted to invite this role/tenant`.
- AC3: tenant_admin inviting into another tenant (any role) → `403`.
- AC4: tenant_admin inviting `admin`/`supervisor`/`agent`/`viewer` inside their own tenant → `201`.
- AC5: recipient opens `/invite#<token>`, sets a password → user created with exactly the invite's
  role, tenant and team; the request body cannot override them.
- AC6: an invite past `expires_at` → accept `410 "invite has expired"`; the Users page renders the
  row's status as **expired** (derived, while the API still stores `pending`).
- AC7: resend rotates the token — the old token no longer accepts, the new one does; resend on an
  expired invite also extends `expires_at` by 7 days.
- AC8 (pending half): revoke blocks accept (`410 "invite has been revoked"`), the accepted user's
  account survives, and a second revoke correctly `409`s.
- AC9: tenant_admin → account in another tenant = flat `409 "this email cannot be invited"` naming
  no tenant; same string for an existing account in their own tenant; super_admin gets
  `409 "email already belongs to tenant 'acme'"`.
- AC10: replaying an accepted token → `410 "invite has already been used"`, distinct from the
  expired and revoked strings.
- AC11: tenant_admin's /users and /invites show only their tenant, including when they pass
  `?tenant_id=<other tenant>` (forced, not validated); super_admin sees every tenant.
- AC12: SMTP down at create → `201`, `email_sent:false`, row stays `pending`, and the UI shows
  "Invite created for X, but the email failed to send." (the resend half is defect 8).
- AC13: every create/resend/revoke/accept writes an `audit_log` row with `user_id` + `user_email`,
  action and old/new value — 74 invite rows, 0 with a null actor; `token_hash` appears only as
  `"[redacted]"` (0 rows containing a 64-hex hash).
- Console gate: `supervisor` and `agent` JWTs get `403 role '…' cannot access this service` on
  `/users`, `/invites`, `/audit-log`; `200` on `/auth/me`. Fresh `agent` login lands on
  `/no-access` with a plain, sidebar-free explanation, and typing `/users`, `/tenants`, `/agents`,
  `/settings`, `/calls` or `/` all redirect back to `/no-access` (lesson 22).
- Public accept routes need no `Authorization` header and are not 401'd by the gate.
- Token hygiene: token only in the mail body and the URL fragment; never in an API response body,
  never in `audit_log`, 0 occurrences of `invite#` or `X-Invite-Token` in the server log.
- Concurrency: same token submitted from two tabs simultaneously → exactly one `200` and one
  `410 "already used"`; exactly one user row.
- Mid-flow state change: revoking an invite between page load and submit → `410 "invite has been
  revoked"` at submit, and "This invite isn't valid" on refresh.
- Soft-deleted tenant / soft-deleted inviter between send and open → `410 "invite is no longer
  valid"` on both GET and POST, tenant-blind (lesson 16 revalidation).
- Double-clicking Send Invite, Resend and Revoke each fire exactly one request.
- Resend cooldown at its edge: `last_sent_at` −59s → `429 "try again in 1s"`; −60s → `200`.
- Probe/send caps at their edges: creates 1–20 → `201`; 21–30 → `429 "too many invites sent this
  hour"`; 31+ → `429 "too many invite attempts"`; both carry `Retry-After` (3588).
- Case-insensitivity: `ALICE@ACME.TEST` invited over an existing `alice@acme.test` → `409`; login
  with a differently-cased address succeeds (lesson 11).
- Soft-delete re-invite: a soft-deleted address can be invited and accepted again, producing a
  second live row; the ghost row keeps `deleted_at`, and the old password no longer authenticates.
- Expired-invite reclaim: re-inviting an address whose invite expired flips the old row to
  `revoked` and creates the new one.
- IDOR: tenant_admin resend/revoke of another tenant's invite id, and of a platform-scope invite →
  `403`; a nonexistent well-formed UUID → `404`.
- Duplicate pending invite in the same tenant (any case) → `409 "a pending invite already exists…;
  revoke it first"`; the same address may hold one pending invite per tenant, and the second
  accept correctly hits `409 "an account already exists for this email"` rather than a 500.
- Hostile input: `<img src=x onerror=…>` / `<script>` in `team` is stored verbatim and never
  rendered (no XSS); `'` in a local part is accepted, stored and delivered; SQL metacharacters in
  an email are rejected by the pattern; no SQL error anywhere in the log.
- Accept form: password < 8 chars blocked by native validation with a clear message; mismatched
  confirmation → "Passwords do not match."; both server-side (`422 string_too_short`) and client.
- Whitespace pasted around an email in the invite modal is trimmed by the UI before submit.
- Zero-result states render correctly ("No invites yet." on a fresh super_admin Users page).

## Not covered
- **Real 7-day wall-clock expiry.** Expiry was forced by `UPDATE user_invites SET expires_at = …`
  rather than by waiting; the derived-expiry logic is the same code path, but a genuine
  seven-day-old row was never produced.
- **Multi-replica behaviour of the throttles.** All limits are in-process fixed windows; only a
  single uvicorn worker was run, so the "limits multiply by replica count" property stated in the
  design was not exercised.
- **The `_MAX_BUCKETS` = 20,000 admission-failure path** (`05-security-prfix.md` finding 2) and the
  post-flood `_SWEEP_INTERVAL` recovery delay (`05-review-prfix.md` finding 1): both need ~20k
  distinct source IPs, which this single-host environment cannot produce.
- **SMTP transport security.** Testing ran with `SMTP_STARTTLS=false` against a plaintext sink, as
  instructed, so the STARTTLS/certificate-verification path was not exercised at all.
- **Browser Back after a successful accept.** The accept page is a client-side SPA state change
  with no history entry, so `goBack()` left the tab at `about:blank`; there was no
  form-resubmission state to reach.
- **A second concurrent Config Service worker / a background row change from another process
  mid-flow.** Row-level races were exercised via two simultaneous HTTP accepts and via out-of-band
  `psql` UPDATEs between page load and submit, but not via two application processes.
- **Everything outside the Config Service and admin-ui.** Knowledge, Conversation, webcall and the
  telephony stack were not started; the claim that `viewer` service accounts keep their access was
  checked only by role/tenant shape at the API, not by running Conversation's prewarm.
- **Sustained-load / latency behaviour.** No performance testing was attempted.
