# T59 finding: existing test suites against `yuviz_app`

Design step 2 ("Migration and rollout"): "CI runs all six services' existing suites
against a database where the app role is `yuviz_app` and `rls.sql` *is* applied
(AC 7). This is the real gate ... A suite that needs a workaround is a finding,
not a pass." This is that finding, filed instead of a green checkbox, per T59's
own "done when."

## What was run

Each service's existing suite (unmodified — no test file touched), twice against
the same local Postgres with `rls.sql` applied:

1. **Baseline** — `POSTGRES_DSN` on the superuser (`chandankumar`, `BYPASSRLS`).
   Sourced `.env`, no other change. This is today's status quo.
2. **Real gate** — `POSTGRES_DSN` on `yuviz_app` (password set locally for this
   run only via `ALTER ROLE yuviz_app PASSWORD ...`; `yuviz_app` is
   `NOBYPASSRLS`, matching what the DSN cutover (T62) will point every
   service at).

| Service      | baseline (super) F/P/E | `yuviz_app` F/P/E |
|--------------|------------------------|--------------------|
| config       | 125 / 279 / 0          | 156 / 231 / 136    |
| campaigns    | 28 / 18 / 0            | 6 / 17 / 23        |
| conversation | 0 / 650 / 0            | 19 / 631 / 0       |
| did          | 4 / 29 / 0             | 0 / 23 / 10        |
| knowledge    | 5 / 45 / 0             | 6 / 11 / 44        |
| toolexec     | 57 / 64 / 0            | 0 / 57 / 64        |

(config/knowledge item totals drift between the two columns — see "DB residue"
below; this does not change the root-cause finding, only the raw counts.)

## Root cause — one thing, two symptoms

Every additional failure/error under `yuviz_app` traces to the same gap: test
code (a `conftest.py` fixture, or a test calling a service-layer function like
`agents.create_agent(...)` directly) runs **outside any HTTP request**, so
nothing ever calls `get_authenticated_user`/`bind_path_tenant` to set the
ambient caller/target scope `libs.tenancy` reads. This is not new to RLS — it
is inherent to unit tests exercising service functions directly — but it was
invisible under the superuser DSN and becomes visible in two different shapes
depending on *how* the untouched test code reaches Postgres:

- **Application-level, fail-closed (pre-existing, present in BOTH columns
  above).** A call that does go through `tenant_conn()`/`platform_conn()`
  with no resolvable scope raises `libs.tenancy.session.TenantUnresolved`
  before ever reaching the database — by design (AC 9), and identical
  under either DSN, since it's pure Python and never depends on the
  connecting role. Confirmed directly: `toolexec`'s 56-of-57 baseline
  failures are already this exception under the *superuser* DSN.
- **Database-level, newly surfaced under `yuviz_app`.** Test fixtures that
  build data with a *raw* `pool.execute`/`pool.fetchrow()` call (not
  `tenant_conn()`/`platform_conn()`) never resolved a scope in the first
  place, so under the superuser DSN the insert simply succeeded
  (`BYPASSRLS` ignores the policy). Under `yuviz_app` the same insert now
  correctly trips the table's own RLS policy:
  `asyncpg.exceptions.InsufficientPrivilegeError: new row violates row-level
  security policy for table "agents"/"calls"/"carriers"/"users"/…` — one
  row per service, same shape, same message template, only the table name
  varies (`agents` in config/campaigns/knowledge/toolexec, `calls` in
  conversation, `carriers` in did). This is exactly the class the design
  calls "fails closed and breaks outright" for a table with a missing
  grant/scope, except here it is a *test fixture*, not a missing grant —
  the fixture itself never resolved a tenant.
  - A visible secondary effect of the same gap: when a fixture's insert
    partially succeeds before this trips (e.g. the tenant row lands but a
    child insert doesn't), a later fixture's teardown
    (`DELETE FROM tenants WHERE id = $1`, itself a raw, unscoped
    `pool.execute`) can hit `ForeignKeyViolationError` on a leftover child
    row. Seen in `config` and `knowledge`.
- **One narrower, separate, and correct restriction:** `config`'s
  `test_bootstrap.py` issues a bare `CREATE DATABASE` against
  `POSTGRES_DSN` to get a scratch database per test. `yuviz_app` is
  `NOCREATEDB` by design (T1) — a real application role must never hold
  that privilege — so this fails with `permission denied to create
  database`. This is not a bug to fix; it is test infrastructure assuming
  a privileged connecting role that RLS correctly no longer grants.
- **Two pre-existing, unrelated test bugs**, present under *both* DSNs,
  found while separating the noise, not caused by this feature:
  `toolexec/tests/test_routes_auth.py`'s `test_lookup_error_over_http_...`
  (a local mock's signature doesn't accept the `platform_scoped` kwarg a
  Phase 5 router now passes) and one `UnboundLocalError` in the same file.

## DB residue (why the item totals drift)

The fixture-teardown `ForeignKeyViolationError`s above mean a failed test run
under `yuviz_app` leaves `tenants`/`users`/etc. rows behind (`tenants` alone
had grown to 886 rows in the shared local dev DB by the time this was run —
itself evidence of the same root cause, accumulated across earlier phases'
test runs). A couple of services (`config`, `knowledge`) have collection-time
logic sensitive to existing row counts, which is why their two columns above
don't sum to the same total. Not a distinct failure class; noted so the raw
counts aren't over-read.

## What this means for T59 / T62

No production code defect was found. Every one of the RLS-owning test suites
that IS written to set ambient scope correctly — `tests/test_rls_isolation.py`,
`tests/test_rls_coverage.py`, `tests/test_cross_tenant_admin.py`,
`tests/test_tenant_conn.py`, `tests/test_no_bare_pool_calls.py` — is green
against `yuviz_app` (60 passed, see below). The failures above are entirely in
each service's own pre-existing unit-test suite, which was written against an
always-`BYPASSRLS` connection and never needed to route its own fixtures
through `tenant_conn()`/`platform_conn()`.

**Filed as a finding, not fixed here**, per this task's explicit scope (do not
rewrite hundreds of tests): every service's `conftest.py` fixture that seeds
data via a raw `pool.execute`/`pool.fetchrow()` call, and every unit test that
calls a service-layer function directly instead of through a `TestClient`
request, needs to either route through `tenant_conn(explicit_tenant=...,
reason="test-fixture")`/`platform_conn(reason="test-fixture")`, or accept
`TenantUnresolved` as an expected part of running that test un-scoped. This is
a real, nontrivial follow-up (hundreds of call sites across six suites), and
it — not a production bug — is what currently blocks T62's own "each service's
own suite passes" gate from being met honestly.

```
$ pytest tests/test_tenant_conn.py tests/test_rls_isolation.py \
         tests/test_rls_coverage.py tests/test_cross_tenant_admin.py -q
60 passed
```
