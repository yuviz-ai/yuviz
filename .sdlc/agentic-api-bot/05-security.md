# Security review: uncommitted QA defect-fix diff on `feature/agentic-api-bot` (over `a4f1ad0`)
VERDICT: AMBER

Scope: `git diff HEAD` in the worktree — `services/toolexec/{auth_schemes,custom_apis,executor}.py`
plus three test files. Traced against the real `libs/config_sdk/secret_resolver.py`,
`database/schema.sql`, `services/toolexec/routers/custom_apis.py` and the executor's step loop.
No cross-tenant read or write path is opened by this diff. No privilege escalation is introduced.
This supersedes the earlier T1-T6 audit at this path.

## Findings

1. [medium] The param-row skip is fail-OPEN: a skipped `custom_api_params` row silently deletes a
   declared argument (and, for `source='upstream'`, a whole dependency edge) from the chain instead
   of failing it — `services/toolexec/executor.py:136-141`
   Attack: anyone with DB write access to `custom_api_params` (DBA, a future migration, a bulk
   importer, or any second writer that does not go through `_replace_params`) makes one param row of
   a side-effecting API undecodable. The next `execute_api` run on that API does NOT error:
   `_resolve_arguments` (executor.py:355-378) is driven purely by the params list, so a required
   `caller` param that is gone produces no `missing_fields` entry and the outbound POST is dispatched
   with the field simply absent (e.g. a refund with no `amount`); an `upstream` param that is gone
   removes the upstream node from the tree built at executor.py:145-155, so the chain runs without a
   dependency it was declared to have (AC 3 / AC 13). Worse, `arguments_hash` is then derived at
   executor.py:694 over the reduced argument set, so it cannot collide with the claim taken by the
   correct full-argument call — the same logical mutation fires a second time through
   `api_side_effect_claims`' `UNIQUE (tenant_id, custom_api_id, arguments_hash)` (AC 15 defeated for
   that mutation, lesson 8's exact failure mode).
   Why medium and not high/critical: I could not find a reachable trigger. `literal_value` is
   `JSONB` (`database/schema.sql:843`), so Postgres has already validated its content — `json.loads`
   in `_decode_literal_value` cannot fail for any value that exists in the column, including
   `to_jsonb('...'::text)`, which the new decoder deliberately accepts. No API path can produce it
   either (`_json_or_none` = `json.dumps`, always decodable). So the tenant-admin-authored-primitive
   hypothesis is REFUTED: a tenant admin cannot author a row that fails to decode, and the branch is
   currently unreachable dead code with an unsafe shape. It becomes live the moment the decoder gains
   a stricter guard (the way `db.json_col` has one), the column type changes, or the `except
   RuntimeError` widens — and nothing in the diff or the tests marks that. The new tests exercise
   only the API-row skip, never this branch (lesson 12: the param skip is untested because it cannot
   be triggered).
   Fix: `raise` for param rows — the reported defect is fully fixed by `_decode_literal_value` alone;
   the skip is extra. If a skip must stay, it must be `raise` whenever the row's api participates in
   the requested chain, and never for `source in ('upstream','literal')` or `required=True`.

2. [medium] Every authenticated role in the tenant — including `viewer`/`supervisor`/`agent` — reads
   `custom_api_params.literal_value` in plaintext, and nothing rejects a secret being stored there;
   this diff adds it to the list response too — `services/toolexec/routers/custom_apis.py:61-63`,
   `services/toolexec/custom_apis.py:391-403`
   Attack: a tenant admin registers a partner API with `{"source":"literal","location":"header",
   "name":"X-Api-Key","literal_value":"<real key>","sensitive":true}` — a normal use of the
   `sensitive` flag, and `_validate_credential_ref` (custom_apis.py:161-175) only constrains
   `auth_config` fields for the scheme, never `literal_value`. A console user with the lowest role in
   that tenant calls `GET /tenants/{own_tenant}/custom-apis` (bare `Depends(get_current_user)`, no
   `require_role`) and reads that key in cleartext — the same value `redaction.redact()` strips from
   `api_chain_steps.arguments_redacted` precisely so it never lands in history (AC 14).
   Not introduced here: `get_custom_api` already returned the identical param rows to the identical
   audience at `a4f1ad0` (custom_apis.py:363-376), and `list` already returned the ids needed to walk
   it — so the exposure is confirmed, not created. I am reporting it because 05-review.md cleared the
   join on the false premise that "literal values cannot carry secrets (rejected at registration)";
   they are not validated at all, and that premise should not be inherited.
   Fix: reject a `literal` param whose `sensitive` is true unless the value is an `enc:`/`env:`/`k8s:`
   ref that passes `validate_tenant_ref` (and redact `literal_value` for `sensitive` params in the
   read responses), or gate the two read routes behind `require_role("superadmin","admin")`.

3. [low] `api_chain_steps.side_effecting` now means "this row is hash-keyed", so a mutating step that
   failed before the claim records `false` and the only surviving evidence that it was a mutation
   attempt is tenant-mutable — `services/toolexec/executor.py:826-834`
   Attack: a tenant admin's agent repeatedly attempts a `side_effecting=true` refund API that fails at
   credential or endpoint resolution (pre-claim). Each step row now persists `side_effecting=false`
   with `arguments_hash=NULL`; the admin then PATCHes `custom_apis.side_effecting` to false, or
   soft-deletes the API. An operator auditing chain history (AC 14) can no longer establish that those
   steps were mutation attempts at all — `api_name` is denormalized into the step row for exactly this
   reason, and this change removes the second denormalized fact. Bounded because a pre-claim failure
   dispatched no request, so nothing outside the process happened.
   CHECK constraint: `CHECK (NOT side_effecting OR arguments_hash IS NOT NULL)` (schema.sql:933-936)
   is NOT tautological in SQL — a hand-written or future INSERT with `side_effecting=true,
   arguments_hash=NULL` still fails, so the original finding-5 NULL bypass stays unrepresentable. It
   is now trivially satisfied *by this writer*, which is the point of the fix, and I confirmed the
   assignment: `arguments_hash` is set only inside `if side_effecting:` (executor.py:691-694) before
   `_claim_side_effect`, so claimed/success/timeout/409 rows carry a hash and `skipped` plus every
   pre-claim failure carry none.
   Fix: add an `api_side_effecting BOOLEAN NOT NULL` column recording the registered flag at step
   time, keep the new column for "hash-keyed", and update the schema.sql comment and the design.

4. [low] Skipping a malformed `custom_apis` row converts a diagnosable `RuntimeError` into a
   `KeyError` when the skipped row is the chain's target or an upstream — `services/toolexec/executor.py:121-127`
   Attack: an operator (or the raw-SQL shape the new test itself writes,
   `to_jsonb('{"token_ref":"x"}'::text)`) double-encodes `auth_config` on an API that a chain targets.
   `_build` does `api_rows[api_id]["name"]` (executor.py:148) and 500s with an opaque `KeyError` — no
   `api_chain_runs` row is created (the tree is built before `_claim_run`), so nothing is stranded and
   no step executes. Fail-closed, so this is diagnosability and not a security consequence: unlike
   finding 1, a missing API row cannot let a chain proceed, because every node in `order` was created
   by a successful `api_rows[...]` lookup.
   Fix: `raise` when the skipped id is the target or appears as any `upstream_api_id`.

Carried, not new (open by prior decision — restated so the reader knows they were not re-found):
the two inaccurate code comments about `..` and `quote()`; the unstated resolver mount-root
construction; the `_oauth2_token_cache` key; the client-chosen run `idempotency_key`; the absent
DB-level same-tenant constraints; `agent_policy_timeout_ms` selected-but-unread; and the blanket
`ValueError` -> `str(exc)` handler interpretation.

## Verified controls (11 verified sound)
1. `validate_tenant_ref`'s `str(tenant_id)` normalization cannot widen the namespace: `str()` on a
   `uuid.UUID`/`pgproto.UUID` is canonical lowercase-hyphenated and injective, so no two tenants map
   to one string. Verified independently of the comments.
2. `env:` binding — `uuid.UUID(str(tenant_id)).hex.upper()` compared against
   `_ENV_REF_RE`'s `[0-9A-F]{32}` under `fullmatch`; every accepted spelling (hyphenated, braced,
   urn, upper/lower) collapses to that one tenant's hex, and no platform variable name
   (`JWT_SECRET`, `SECRET_ENCRYPTION_KEY`, `POSTGRES_DSN`) can match the `TENANT_<hex>_` prefix. The
   T4 CRITICAL stays closed.
3. `k8s:` binding — the regex's `(?P<tenant_id>[^/]+)` is compared to the same `str(tenant_id)` that
   is joined into `allowed_dir`, so equality and containment use one identical value; `[^/]+` means a
   `tenant_id` containing a separator can never be matched by a ref, and `target.resolve()` +
   `relative_to(allowed_dir)` still rejects a symlink escape (unresolved `tenants/<id>` component is
   safe: resolution moves `target` out, which fails the check). `str()` only tightens accepted
   spellings.
4. Fail-closed on hostile input: a non-UUID `tenant_id` raises `ValueError` out of `uuid.UUID` in the
   `env:` branch, and `None`/anything else falls through to the terminal
   `credential_ref_outside_tenant_namespace`; `_validate_credential_ref` catches `ValueError` (400)
   and `_run_steps` maps it to the `credential_unavailable` step outcome with no ref in the message.
   `resolve_tenant_ref` re-validates at resolution, not only at registration (lesson 29).
5. `CompositeSecretResolver(k8s_mount_root=TENANT_SECRET_ROOT)` — checked in the real
   `libs/config_sdk/secret_resolver.py`: `EnvResolver` returns any env var and `K8sFileResolver`
   joins the ref onto its mount root unguarded, so `validate_tenant_ref` is the whole control, and
   the tenant resolver is instantiated against `TOOLEXEC_TENANT_SECRET_ROOT`, not the platform mount.
6. `list_custom_apis`' new params join is tenant-scoped: `custom_api_id = ANY($1::uuid[])` over ids
   produced by the already `tenant_id = $1 AND deleted_at IS NULL` query, so no other tenant's params
   can appear; parameterized, one extra query, not N+1.
7. The list response exposes no *new* class of data beyond `get_custom_api` at `a4f1ad0` (same param
   columns, same audience) — see finding 2 for the audience itself.
8. `update_custom_api` distinguishes omitted params (`params is None` -> preserve) from explicit `[]`
   (`_replace_params`), so the list-shape fix (lesson 33) genuinely stops the Edit form wiping
   dependency edges, and `_validate_upstream_params` still re-checks same-tenant + not-deleted on
   every write (AC 10/AC 17).
9. Double-fire arbitration is untouched: the claim is taken BEFORE the outbound call
   (executor.py:694-699), `_claim_side_effect` is a single conditional upsert with the loser's path,
   and nothing reads `api_chain_steps.side_effecting` — the only readers of that table are the two
   history `SELECT *`s (executor.py:203, agent_apis.py:195).
10. `_decode_literal_value` widens decoding without widening trust: it accepts any valid JSON value
    (fixing the string-literal outage, lesson 34) and still raises for undecodable input; it does not
    touch `auth_config`/`sensitive_response_paths`, which keep `db.json_col`'s double-encoding guard.
11. The failure path now satisfies `api_chain_steps_side_effect_keyed` on every path I traced
    (skipped / missing_fields / endpoint / credential / success / timeout / 409 / already_completed),
    and the run reaches a terminal status instead of being stranded `running` (lesson 32 closed).
