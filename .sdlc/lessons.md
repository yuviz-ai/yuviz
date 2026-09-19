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
   response bodies. The property is **per-caller invariance**: for one fixed principal, the response
   must not vary with the target's existence or state. It is NOT cross-caller equality — two
   different principals legitimately hit different guards, so a test asserting byte-identical bodies
   across two principals can neither fail on a real leak nor pass on correct code.
   *Earned: a 409 conflict named the tenant an existing account belonged to, letting any
   tenant_admin probe an email and learn which customer employs that person. Sharpened when a test
   asserted a tenant-B admin's router-tier 404 body equalled a platform service account's route-tier
   404 body — a comparison no attacker can make, which stayed red while the code was correct.*

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
    `!= <error code>` instead of the expected code, which also passes on a different error; a
    "did we cover every case" tripwire that cannot trip when a new case is added; an enumeration
    helper whose return value the assertion never uses, so the check computes the right set and then
    asserts over a hand-written list; and a mechanical enumeration that silently skips what it cannot
    classify — vacuous for exactly the new case it was built to catch, so assert the enumeration's
    own size or its residue, not just its members.
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

29. [architect][security] A design's own list of "every router/call site/module this change touches"
    is a claim, not a fact, until something mechanical derives it. A hand-enumerated list against a
    codebase this size is wrong the first time and stays wrong every time it's patched by hand again.
    *Earned: an RLS design's router list went 11 (missed a 12th), then its call-site count went 67
    (missed ~116 more), then its by-id-route count went "4-5" (missed most of 53) — three separate
    security rounds, each catching a different omission from the same un-mechanized enumeration.
    Should have been an `app.routes`/AST walk from round 1, with the design's prose describing the
    walk's rule rather than its output.

30. [implementer][critic] When a build is split across parallel per-service agents implementing the
    *same* cross-cutting pattern (a shared helper's calling convention, an authorization sequence), one
    service establishing the convention does not mean a sibling service replicates it — each agent only
    sees its own slice, not the others' code as it lands concurrently. Diff parallel-built call sites
    against each other explicitly, not just against the design's prose.
    *Earned: Config's Tier 3 routers all called `set_target_tenant` after `assert_tenant_access`;
    Knowledge's analogous `_authorize_agent`/`_authorize_kb`/`_authorize_document` helpers (built by a
    different agent in the same parallel wave) resolved the tenant and asserted access but never called
    `set_target_tenant` — a platform-scoped caller passed the check and then hit `TenantUnresolved`
    on the very next line, in every one of Knowledge's by-id routes at once.

31. [architect][security] When a tenant-authored document (a call-flow graph, a workflow node, a
    prompt template, a campaign spec) names another tenant-owned row by id, the runtime must receive
    only a server-resolved twin, arriving as a parameter the call site cannot construct without it.
    If the unvalidated original is still in scope at that call site, the control does not exist yet.
    *Earned: a call-flow's `start.tts_config_id` stayed on the parsed graph while the validated
    `resolved_tts_config_id` had no channel into `CallFlowRunner` — and `get_provider_config(id)` is
    tenant-unscoped with no `tenant_id` on the DTO, so tenant A's flow would have driven calls
    through tenant B's TTS config and its `api_key_ref` secret. The design asserted the control in
    prose for two rounds while the interface made the unvalidated id the only reachable value.*

32. [architect][critic] A design that states the same signature in two places has two sources of
    truth, and the implementer reads the row for the file they are editing. Where a security control
    depends on a value arriving somewhere, write the literal call expression at the one site that
    must satisfy it — and note that making a parameter *required* only forces the implementer to find
    *a* value, which is the unvalidated one when the validated one was never passed down.
    *Earned: the same cross-tenant TTS hole reopened one round after being fixed, because the Changes
    table still typed `resolve_call_flow() -> CallFlowGraph | None` while the Interfaces block said
    `-> tuple[CallFlowGraph, CallFlow] | None`, leaving an implementer with no `CallFlow` in scope
    and `graph.start.tts_config_id` as the only id a now-required keyword could be fed.*

33. [architect][implementer][security] When a feature gives new meaning to data that already flows
    through an existing log line, metric label, variable dict or persistence column, every sink that
    pipe already has is part of this change's blast radius. Enumerate the terminal sinks — log, DB
    column, third-party vendor — before calling the review surface new-code-only.
    *Earned: routing caller DTMF into `collect` nodes turned two harmless existing pipes into
    credential leaks: `bridge.py`'s `log.info("dtmf digit=%s")` made a keyed-in PIN reconstructable
    from logs, and the existing `variables` → `extracted_variables()` → `conversation_sessions`
    jsonb channel persisted it and rendered it into an LLM prompt. Neither was in the feature's own
    new code, which is exactly why a diff-scoped review would have missed both.*

34. [architect][implementer] If a design arms a timer task alongside an event handler over shared
    per-session state, it has two writers by default. Say which single task mutates the state: the
    timer may only enqueue, and a late event must identify the **arm** it belongs to, not the state —
    a node that replays itself re-enters the identical id, so bump a monotonic per-arm generation and
    drop a mismatch. Give the test a timeout long enough that the replay's own real timer cannot
    mature inside it, or it passes for the wrong reason.
    *Earned: a `Listen` node's timeout task and `on_dtmf` from the servicer loop both mutated the
    same `CallFlowRunner` across awaits — a double advance, a `Handoff` plus a `Hangup`, or the
    wrong node. `services/conversation` is full of paired producers (servicer loop, audio delay
    pump, VAD), so this is the default shape there, not an unlucky one.*

35. [implementer][critic] After adding a method or branch to an existing file, re-read the whole
    enclosing function. A new `def` inserted inside another function's body silently adopts that
    function's trailing statements, and neither the type checker, the linter nor the suite will catch
    it — a diff-scoped review reads the added lines, not the function they landed in.
    *Earned: an `on_dtmf()` pasted inside `PipelineConversationHandler.on_cancel()` stole its last
    statement, so `_interrupt_workflow_background_llm()` ran on a keypress documented as inert and
    stopped running on barge-in — silently regressing every ordinary conversational call, with a
    dead `pass` above it as the only visible trace.*

36. [architect][implementer][security][tester] RLS is not a verifiable control while the app
    connects as a BYPASSRLS role — and on this repo every service still uses the superuser DSN
    (`scripts/start_local.sh:59-62`: "RLS is live but inert"). So a tenant-scoped read whose only
    protection is RLS has an isolation test that cannot fire. Give such a read an explicit
    `tenant_id` predicate as well: the layers stay independent, and the one you can actually
    exercise goes red the moment the predicate is deleted.
    *Earned: the call-flow runtime read relied solely on RLS for a platform-scoped service account
    that passes `assert_tenant_access` for every slug, so the feature's single most important
    assertion — tenant B cannot load tenant A's flow — was red under the only DB role anyone ran and
    untested under the one nobody could. Adding `tenant_id = $2` to the three reads made two of the
    three cross-tenant tests pass on the predicate alone. Related: [[31]] — `agents.call_flow_id`
    was a bare `REFERENCES call_flows(id)` with nothing stopping it pointing at another tenant's
    flow, fixed with a composite FK onto `(id, tenant_id)`.*
