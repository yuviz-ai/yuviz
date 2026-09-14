# SDLC lessons

Rules earned from real misses on this codebase. Every SDLC agent reads this file before starting
and complies with the lessons tagged for it. Append-only via `/sdlc:retro` (Claude) or `/sdlc-retro`
(Cursor). **Single canonical path: `.sdlc/lessons.md`** — do not maintain copies under `.claude/`
or `.cursor/`. Keep each entry to two lines. Delete an entry only when it becomes wrong, not when
it becomes familiar.

Tags: [prd] [architect] [planner] [implementer] [critic] [security] [tester] [qa] [all]

---

1. [architect][security] A control is not designed until you have traced it through this
   framework's real wiring. Naming a mechanism is not specifying it.
   *Earned: an app-level FastAPI `require_console_role` dependency was proposed to fence off new
   roles — but `get_current_user` 401s on a missing header before any exemption logic runs, so it
   would have broken `/auth/login`, `/auth/bootstrap`, the public invite-accept routes and
   `/health` (which docker-compose healthchecks curl). The obvious field repair — make auth
   optional in the guard — creates an unauthenticated passthrough on 28 routes.*

2. [architect][critic][security] Any response that differs across a tenant boundary is an
   information leak. This includes status codes (403 vs 404), error text, and latency — not just
   response bodies.
   *Earned: a 409 conflict named the tenant an existing account belonged to, letting any
   tenant_admin probe an email and learn which customer employs that person.*

3. [architect][critic] When a design relies on a UNIQUE index or CHECK constraint, state its scope
   explicitly. An index that is unscoped across tenants is a cross-tenant denial-of-service.
   *Earned: a pending-invite unique index on email alone let Tenant B permanently block Tenant A
   from onboarding an address.*

4. [architect][implementer] Adding a value to a role enum or CHECK constraint grants that role
   everything currently guarded only by "is authenticated". Enumerate what the new role now
   reaches before adding it.
   *Earned: adding `supervisor`/`agent` silently granted read access to provider credentials,
   telephony inventory and transcripts via 28 bare `Depends(get_current_user)` routes.*

5. [architect][implementer] A data-normalizing backfill (lower-casing, trimming, deduping) can
   violate an existing constraint and abort the whole schema apply. Specify the dedupe step, or
   say why collision is impossible.
   *Earned: a `lower(email)` backfill against a case-sensitive UNIQUE column.*

6. [critic][security] A fix that is described but not specified well enough to implement correctly
   is still an open finding. Rate it at its original severity, not lower for having been addressed.

7. [prd][architect] Never drop a requirement to hit a length target. If something must go, say what
   went and why.
   *Earned: an audit-log acceptance criterion was demoted into a constraints line to fit a word cap.*

8. [architect][implementer] Check-then-act on a shared row is a race. Use a single conditional
   `UPDATE ... WHERE <precondition> RETURNING *` and treat zero rows as the loser's path — and
   verify the statement's column ordering does not force the racing insert to happen first.

9. [architect][implementer][tester] `services/config/deps.py` is imported by Knowledge, DID and
   Campaigns as well as Config. A guard or tripwire test scoped to one service does not protect a
   module four services share — scope the test to the module, not the app.
   *Earned: a console-role gate correctly placed in shared identity resolution, with its
   "only these routes may bypass" test written against the Config app alone.*

10. [architect][implementer][tester] `psql -f` is autocommit-per-statement. Any destructive DDL that
    precedes a guard has already committed when the guard raises — the file aborts half-applied and
    every statement after it never runs. Put the guard first, and verify against a database that can
    actually trip it.
    *Earned: a `DROP CONSTRAINT users_email_key` placed above its own duplicate-check guard. Verified
    "idempotent" against an empty DB, which had no duplicates and so never fired the guard.*

11. [architect][planner][implementer] A phase that changes stored data must land with the code that
    reads it. Normalizing a column while its reader still uses the old predicate breaks the feature
    that column serves, however correct each half looks alone.
    *Earned: a `lower(email)` backfill shipped in the schema phase while `get_user_by_email` stayed
    case-sensitive until the next phase — silently breaking login for every mixed-case account.*

12. [implementer][tester] A verification that cannot fail proves nothing. Before reporting a check as
    passed, state what would have made it fail — and if the fixture could not produce that condition,
    say the check was not exercised.
    Recurring shapes on this repo: asserting a value is absent when it has no code path to that
    place at all; a compound assertion with an `or` arm that is unconditionally true; asserting
    `!= <error code>` instead of the expected code, which also passes on a different error; and a
    "did we cover every case" tripwire that cannot trip when a new case is added.
    *Earned: "applied twice, both succeeded" on an empty database, reported as idempotency proof for
    statements whose failure mode only appears when live rows exist.*

13. [architect][implementer] This repo applies `database/schema.sql` with plain `psql -f` and no
    `ON_ERROR_STOP` (checked `docs/setup.md` and the `scripts/*.sh` launchers). A failing statement is
    printed, skipped, and the apply still exits 0. So a guard only protects the statements inside its
    own `DO $$` block — sequencing alone protects nothing. Put destructive DDL in the same block as
    the check that gates it.
    *Earned: moving a duplicate guard above a `DROP CONSTRAINT` did not stop the DROP from running
    after the guard raised.*

14. [implementer][tester] Adding a safety flag does not make a command fail loudly. Check the whole
    line for what discards the failure — `2>/dev/null`, `|| true`, `|| echo "…"`, a swallowed exit
    code — and check the caller too. A guard that aborts into a success message is worse than no
    guard, because the operator is now told it is fine.
    *Earned: `-v ON_ERROR_STOP=1` added to a `psql -f` line ending in
    `2>/dev/null || echo "schema already applied"`.*

15. [implementer] `scripts/start_local.sh` is `source`-d into the operator's own shell under
    `set -euo pipefail` (`docs/setup.md:158`). A nonzero `return` from any function in it kills the
    operator's interactive shell — so a branch whose purpose is to print a hint must `return 0`.
    Reserve nonzero for failures that genuinely must stop the launcher.
    *Earned: a fail-loud fix that made the benign "db not created yet" path return 1, closing the
    terminal of anyone following the setup doc on a fresh checkout.*

16. [architect][implementer][security] Any grant that is issued now and redeemed later — an invite, a
    reset link, a signed URL, a queued job carrying an actor — is an authorization decision cashed in
    the future. Re-validate the whole granting context at redemption: the granter still exists, still
    holds the authority, and the target still exists. Otherwise de-provisioning someone does not
    de-provision what they granted.
    *Earned: an offboarded tenant_admin's planted invite still admitted a new admin days later, into
    a tenant that could itself have been soft-deleted in the meantime.*

17. [architect][planner][implementer] Deleting or replacing an endpoint is not done until every caller
    is found — and the callers are not all in the same language. Grep `admin-ui/`, `scripts/`, `docs/`
    and the other services, not just the service that owns the route. A cutover task that sequences
    the server side perfectly still ships a dead button if the frontend was never in its file list.
    *Earned: `POST /users` was deleted after the invite routes were mounted, precisely so no
    account-creation path would break — while `admin-ui/app/settings/page.tsx` kept calling it.*

18. [implementer][critic] Sync I/O inside an `async def` blocks the whole event loop, not just that
    request. `smtplib`, `requests`, `time.sleep`, file reads and any DB driver that is not the async
    one belong in `asyncio.to_thread`. Always pass an explicit timeout — the stdlib default for
    `smtplib.SMTP` is `None`, meaning wait forever.
    *Earned: a blocking `smtplib.SMTP(host, port)` with no timeout in an async invite handler, where
    an unreachable relay would have stalled every Config request including the `/health` check that
    docker-compose gates other services on.*

19. [critic][implementer] A control the design specified and the implementation dropped will not
    surface as a test failure — no test was written for it, because the design assumed it existed.
    When reviewing against a design, diff its named controls (timeouts, limits, predicates, headers)
    against the code one by one.
    *Earned: the design specified a 10s SMTP timeout; the implementation shipped without one and
    every test still passed.*

20. [architect][implementer][security] An outbound connection carrying a secret needs transport
    security requested explicitly — the client library will not do it for you. `smtplib` sends AUTH
    credentials in cleartext until you call `starttls()`. When a plaintext fallback must exist for
    local development, make it an explicit opt-out that defaults to secure, never a silent downgrade
    when the upgrade fails.
    *Earned: invite tokens — bearer-equivalent secrets — and the SMTP password crossing the network
    in cleartext on the documented port 587.*

21. [implementer][tester] `Promise.all` over independent fetches makes every one of them required:
    a single 403 on a call the current role legitimately cannot make rejects the whole batch and
    blanks the page, discarding data the API already returned successfully. Fetch what the role can
    actually see, and let independent failures stay independent.
    *Earned: `/users` showed "No users yet." to a viewer because the sibling `GET /invites` — an
    admin-only route — 403'd inside the same `Promise.all`.*

22. [architect][implementer] Adding a role is not finished when the API authorizes it. Check where
    that role LANDS: the post-login destination, the nav it sees, and the first screen it renders.
    A role with no console surface must not be dropped onto an admin page to discover that via a
    403 banner.
    *Earned: a freshly invited `agent` — deliberately given zero Config API surface — logged in and
    was redirected to /tenants, greeted by a red error and a "+ New Tenant" button it could not use.*

23. [implementer][tester] Run the app. Every bug in this feature that a human would hit first —
    a blanked table, an error page as a welcome screen, a mandatory auth call against a relay that
    wants none — was found by driving the real UI in a browser, after the suite was green, the
    types checked, the linter passed, and three review rounds had closed.

24. [architect][implementer][security] "Is this actor privileged?" and "which tenant is this actor
    scoped to?" are different questions, and picking the wrong one breaks something real. On this
    codebase the scoping predicate is `tenant_id IS NULL`, not `role == "superadmin"` — the
    Conversation and vobiz service accounts authenticate as `role="viewer"` with a NULL tenant and
    legitimately need platform-wide reads.
    *Earned: scoping a leaky tenant listing on role would have closed the leak and simultaneously
    broken Conversation's startup prewarm and vobiz's per-call telephony lookup.*

25. [implementer][tester][critic] A test that reconfigures a tuning constant to make its scenario
    reachable is testing a system that never ships. Exercise the deployed value, or the test proves
    the fix works only in a configuration nobody runs.
    *Earned: a rate-limiter recovery test set `_SWEEP_INTERVAL = 1` and passed, hiding that under the
    real interval of 500 roughly 499 legitimate users are still refused after a flood ends.*

26. [architect][implementer] A fix that moves work off the event loop, out of a shared pool, or into
    a background executor inherits a lifecycle. Ask who shuts it down. `concurrent.futures` joins its
    non-daemon threads at exit, so an un-torn-down pool turns a stalled network call into a process
    that will not stop within its shutdown grace — and leaks a pool per reload in development.
    *Earned: a dedicated SMTP `ThreadPoolExecutor` added to isolate stalled sends, never shut down in
    `lifespan`, would have blocked SIGTERM past docker-compose's 10s grace into a SIGKILL.*

27. [architect][implementer][security][qa] `services/config/deps.py` resolves identity **purely from
    the decoded JWT** — it never reads the database. So `deleted_at` and a changed `role` do not take
    effect until the token expires (~12h). Soft-deleting or demoting a user does not revoke their
    access. Any feature that grants authority through a role, or assumes deactivation is immediate,
    must say how revocation actually works — or add the lookup.
    *Earned: post-merge QA found a soft-deleted user's token still returning 200 on `GET /users` and
    minting admin invites via `POST /invites`, while `/auth/me` (which does hit the DB) 401'd.*

28. [tester][qa] The suite and the reviewers test what someone thought to test. Running the merged
    app against edge cases found 17 defects after three review rounds, a green security audit and 330
    passing tests — including a high. Budget QA as its own stage, not as confirmation.
