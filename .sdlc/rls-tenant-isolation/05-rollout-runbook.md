# Rollout runbook — Phase 9 (T60–T62)

This environment has no staging deployment (no second environment, no traffic
of its own) and no production tenants. The steps below are the actual runbook
for an operator who does — precise enough to follow, not a record of having
been run here. Where a step *is* something this sandbox could genuinely do
(a read-only SQL query, a code change), it was done for real and is marked so.
Nothing here claims a staging run happened; none did.

## T60 — shadow verification

**Done in this sandbox:** the three observability lines the design calls for
in `tenant_conn()`/`platform_conn()` (`libs/tenancy/session.py`):

1. `platform_conn`'s bypass line (`log.info("platform_conn bypass reason=%s
   ...")`) already existed before this task.
2. A new `log.warning` for the cross-tenant admin path
   (`scope.caller is not None and scope.target not in (None, scope.caller)`).
3. A new `log.warning` for an unresolved scope
   (`current_tenant() is None` with no `explicit_tenant`) — logged
   immediately before `tenant_conn` would raise `TenantUnresolved`, so an
   operator watching logs sees it even where the exception itself is caught
   and turned into an HTTP error further up.

Both are unconditional (this codebase has no staging-vs-production log-level
switch to hook into) — WARNING-level, low-cardinality, no request body or
secret in the message.

**Not done here, for a real operator to run:** deploy the wiring-only state
(services still on the superuser DSN — already true per T6) to staging for
one full deploy cycle, then:

- Grep `platform_conn bypass reason=` and diff the observed `reason=` set
  against the intended-bypass table in `docs/rls-tenant-isolation.md`
  (T63). Any reason string outside that table is a finding: a tenant-scoped
  query running unscoped today.
- Grep the new cross-tenant-admin-path WARNING and count occurrences — this
  is real traffic exercising the account class T61's sweep targets.
- Grep the new unresolved-scope WARNING. **The gate:** zero of these over a
  full deploy cycle. Any occurrence names a call site the Phase 4–7
  conversion missed, and must be fixed before T62 proceeds.

## T61 — pre-cutover account sweep

**Done in this sandbox (read-only):** ran the sweep query from
`scripts/pre_cutover_account_sweep.sql` against the local dev database. It
returned 6 rows, all identifiable as leftover fixtures from this build's own
test suites (`scoped-superadmin-*`, `test-livecalls-*` emails) — not real
accounts, and not from this task. This confirms the query itself runs and
returns the account shape it's designed to catch; it is not a substitute for
running it against a real environment's `users` table.

**Not done here:** the review-and-disposition step (each row is either "NULL
out `tenant_id`" or "leave it, it's a correctly tenant-scoped superadmin
seat") requires a human who knows the real accounts; see the script's own
comments for the two dispositions. Blocking gate: zero unreviewed rows before
T62.

## T62 — per-service DSN cutover

**Not done here.** This sandbox is a single local Postgres with no
independently-deployed services to cut over one at a time, and T59's finding
(`04-t59-test-suite-finding.md`) means the literal gate — "each service's own
suite (T59) ... pass before the next service flips" — is not honestly met
today: every service's pre-existing unit suite has real, unfixed fixture
failures under `yuviz_app` (test infrastructure, not production code; see the
finding). Flipping DSNs while claiming that gate passed would be exactly the
kind of fabricated result this task was told not to produce.

For a real operator, once T60's gate is clean and T61's sweep is reviewed, the
per-service cutover is:

1. Confirm `POSTGRES_APP_DSN` is set (per-service DSN, `yuviz_app` role +
   password) alongside the existing `POSTGRES_DSN` in whichever of
   `deployment/docker/docker-compose.yml` or `scripts/start_local.sh`
   actually starts that service — the six services split across the two:
   `config`/`knowledge`/`conversation` run under docker-compose today,
   `campaigns`/`toolexec`/`did` under `scripts/start_local.sh`'s own
   `start_<service>_service()` functions, each exporting its own
   `POSTGRES_DSN` independently (that per-function isolation is what makes a
   true one-service-at-a-time cutover possible without touching the other
   five).
2. In ascending blast radius, for each service in turn — **did → campaigns →
   toolexec → knowledge → conversation → config** — point that one service's
   `POSTGRES_DSN` at `POSTGRES_APP_DSN`'s value and restart just that
   service.
3. Before moving to the next service: confirm it started cleanly, its own
   suite is green under `yuviz_app` (not merely "TenantUnresolved-only", per
   T59's finding — that gate needs the fixture follow-up filed there), and a
   manual UI walkthrough starting with "superadmin acting on another tenant"
   passes.
4. **Rollback** is reverting that one service's `POSTGRES_DSN` back to the
   superuser value and restarting it — never a schema change, never requires
   touching `rls.sql` or any policy, and does not affect the other five
   services.
