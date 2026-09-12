# Code review
VERDICT: AMBER

Scope: uncommitted working-tree diff on `feature/agentic-api-bot` over `a4f1ad0`
(`services/toolexec/{auth_schemes,custom_apis,executor}.py` + 3 test files). All four QA defects
are genuinely fixed on the paths I traced; the findings below are all minor.

1. [minor] Skipping a malformed `custom_api_params` row silently drops a declared param — and with
   it a dependency edge — instead of failing loudly — `services/toolexec/executor.py:139` — fails
   when: a param row of a side-effecting API has non-decodable JSON in `literal_value` (reachable
   only via raw SQL / legacy write, the same reachability as the defect being fixed). The row is
   skipped, so `_resolve_arguments` never sees the param: if `source='upstream'` the upstream step
   is no longer in the tree and is never called, and if the param was required the request is
   dispatched WITHOUT that field rather than returning `missing_fields`. The claim/idempotency
   `arguments_hash` is then derived over the reduced argument set, so it will not collide with the
   correct full-argument claim — a mutation can fire with incomplete arguments and escape the
   double-fire guard. The reported defect is already fixed by `_decode_literal_value`
   (`custom_apis.py:341`) alone; the skip is extra. — fix: re-raise for param rows (or at minimum
   for `source in ('upstream','literal')`) so a corrupt param fails the chain rather than mutating
   its shape.

2. [minor] Skipping a malformed `custom_apis` row is only graceful for rows no chain touches; for
   the target or an upstream it converts a clear error into `KeyError` —
   `services/toolexec/executor.py:127` — fails when: the target API's own row (or an upstream's) has
   a double-encoded `auth_config`. The row is skipped, then `_build` does `api_rows[api_id]["name"]`
   (executor.py:148) and raises `KeyError` to the route: still a 500, now with a less diagnosable
   error than the previous `RuntimeError`. No stranded run row (the tree is built before
   `_claim_run`), so this is diagnosability, not data damage. The new regression test only exercises
   an uninvolved row, so the involved case is untested (lesson 12). — fix: `raise` when the skipped
   id is the target or appears as an `upstream_api_id`, or drop the skip for API rows.

3. [minor] `api_chain_steps.side_effecting` now means "this row is hash-keyed", making the CHECK
   tautological and losing "this step was a mutation attempt" for pre-claim failures —
   `services/toolexec/executor.py:834` — fails when: an operator audits chain history (AC 14) for a
   refund step that failed at credential/endpoint resolution: the step now records
   `side_effecting=false`, and the only remaining record that it was a mutation is
   `custom_apis.side_effecting`, which is mutable via PATCH and whose row can be soft-deleted —
   precisely why the step table denormalizes `api_name`. Correctness of the fix itself checks out on
   every path I traced: `arguments_hash` is assigned only inside the `if side_effecting:` block
   (executor.py:691-693) before the claim, so success/timeout/truncated/409/`already_completed` all
   have a hash (→ true), `skipped` and every pre-claim failure have none (→ false), and a
   non-side-effecting API can never produce a hash (→ false, as before). I also confirmed nothing
   reads `api_chain_steps.side_effecting`: the double-fire guard arbitrates solely on
   `api_side_effect_claims`'s `UNIQUE (tenant_id, custom_api_id, arguments_hash)`, and the only
   readers of `api_chain_steps` are the two history `SELECT *`s (executor.py:203,
   agent_apis.py:195). So this is a design-semantics change, not a broken control — but it changes
   the meaning of a NOT NULL column and the `api_chain_steps_side_effect_keyed` invariant documented
   in `database/schema.sql:927-936`, and neither the schema comment nor the design was updated.
   — fix: update the column comment in `database/schema.sql` (and note it in the design), or add an
   `attempted_side_effect`/`api_side_effecting` column so the audit trail keeps the registered flag.

Cleared on inspection, no finding:
- Defect 4 (`auth_schemes.py:77`): `str(tenant_id)` cannot weaken namespace confinement. `env:` goes
  through `uuid.UUID(str).hex.upper()`, so any accepted form binds to that one tenant and a
  non-UUID string still raises (fail closed). `k8s:` compares the ref's segment to `str(tenant_id)`
  and joins the same value into `allowed_dir`, so the equality and the `resolve()/relative_to()`
  check use one identical value; `str()` on a UUID is injective and canonical, so no input maps onto
  another tenant's namespace. Existing `str` callers are unaffected (`str(str)` is identity); the
  only behaviour change is uppercase/braced `k8s:` tenant spellings, which are stricter, not looser.
  Both new tests fail if the normalization is reverted.
- Defect 3 (`custom_apis.py:379-403`): `update_custom_api` distinguishes omitted (`params is None` →
  re-reads and preserves) from explicit `[]` (`_replace_params`), custom_apis.py:519-533/554. The
  param fetch is scoped by `custom_api_id = ANY(...)` over ids from the already tenant-scoped first
  query, and it is one extra query, not an N+1. The list route has no `response_model`, so `params`
  actually reaches the client, and `admin-ui/lib/toolexecApi.ts` already typed `params` as required
  while `CustomApisPanel.tsx:273` already read `api.params` — no frontend change needed. Literal
  values cannot carry secrets (rejected at registration), so the widened read exposes no new secret.
- Both new chain tests are non-vacuous: reverting the `_persist_step` change retrips the CHECK and
  fails `test_side_effecting_step_missing_required_arg_finalizes_no_500` on the insert; reverting
  the row-by-row decode makes `execute_chain` raise in
  `test_one_malformed_custom_api_row_does_not_break_other_apis_chain`.
