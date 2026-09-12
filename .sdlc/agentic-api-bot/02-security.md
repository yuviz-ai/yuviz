# Security review: .sdlc/agentic-api-bot/02-design.md (round 4, final)
VERDICT: GREEN

Fourth and final pass. Both open mediums and the two coupled lows are closed as real specified code
paths — I checked each against the codebase it depends on, and in the case of the `LoggingMiddleware`
fix I checked every one of the six sub-parts that were missing last round, because the round-3
version of that "fix" was a dataclass field nothing could populate.

No round-1 critical or high has re-opened. No previously verified control was weakened. Nothing new
of security consequence was introduced. Six lows remain open: one is the rotation-documentation gap
I raised in round 3, which is still not stated; five were deliberately left unfixed and are restated
here without re-litigation.

## Round-3 findings: disposition

| # | Round-3 finding | Status |
|---|---|---|
| 1 | [medium] `LoggingMiddleware` sensitive-input leak; `sensitive_arg_keys` a no-op | **closed** — Verified 32 |
| 2 | [medium] `test_routes_auth.py` asserted the superseded `/internal` gate | **closed** — Verified 34 |
| 3 | [low] no test could fail for five new controls | **closed** — Verified 35 |
| 4 | [low] `idempotency_header` exported the stored hash — HMAC oracle | **closed** — Verified 33 |
| 5 | [low] two false rationales (k8s `..`, `quote` and `.`) | open, restated below |
| 6 | [low] resolver mount-root construction unstated | open, restated below |
| 7 | [low] OAuth2 token cache key omits credential/destination | open, restated below |
| 8 | [low] client-chosen run `idempotency_key` | open, restated below |
| 9 | [low] no DB-level same-tenant constraint | open, restated below |

### Medium 1 — all six sub-parts verified present, and the plumbing traced in the real code
Round 3 failed on six specific counts. Each is now closed, and I verified the three that depend on
how this repo is actually wired rather than on what the design says:
1. **`p.sensitive` in the SELECT** — present as `p.sensitive AS param_sensitive`, alongside the
   existing `LEFT JOIN custom_api_params p ON … AND p.source = 'caller'`. Correctly scoped: a
   `sensitive` param sourced `upstream` or `literal` never appears in the LLM's `inputs` at all.
2. **In what `_specialize_execute_api` returns** — its signature is now
   `-> (ToolDefinition, frozenset[str])`, the frozenset being the union of `param_name WHERE
   param_sensitive` across the agent's enabled APIs, and `enabled_tools()` puts it on the
   `ResolvedToolPolicy` it builds for `execute_api` (`frozenset()` for every other tool). The field
   has a producer.
3. **A stated path to the code that logs** — and this is the part I checked hardest, because a
   middleware receives a `ToolExecutionRequest`, never a policy. The design injects at
   *construction*: `LoggingMiddleware(redact_arg_keys=…)` with a pass-through parameter on
   `build_default_chain`, and the orchestrator passes `policy.sensitive_arg_keys` at its call site.
   Verified against the repo: `build_default_chain` is defined at `middleware.py:168` and has exactly
   **one** non-test call site, `orchestrator.py:194`, inside `_execute_tool_call` — so the chain is
   built per tool call, the middleware instances are not shared or long-lived (per-agent key state on
   them is legitimate), and `policy` is already in scope one line earlier (`policy.timeout_ms` is
   read at `orchestrator.py:193`). The set genuinely reaches the log line.
4. **Changes rows present** — `services/conversation/tools/middleware.py` and
   `services/conversation/tools/orchestrator.py` both now have their own rows. `types.py` is
   explicitly stated **unchanged**, with the reason: putting the set on `ToolExecutionContext` widens
   the blast radius of the seam every executor imports, for no gain. That is the right call, and
   "unchanged, deliberately" is a specification, not an omission.
5. **The superseded Risks sentence is gone** — "caller-supplied inputs are already in the transcript
   by definition" is deleted. The bullet now splits the two halves and says the request half *is not*
   covered by the response-projection argument, which is what I flagged. Two further bullets were
   added covering the HMAC and the domain separation.
6. **A `caplog` assertion exists** — three of them, and the two design choices in them are the ones
   that make the test able to fail: it drives the **real `build_default_chain`** rather than a
   hand-built middleware (so it fails if the orchestrator never passes the argument — the exact
   gap that would otherwise ship silently), and it asserts over `caplog.records` rather than
   `caplog.text`. A negative case asserts `api_name` and non-sensitive inputs still appear, so
   redact-everything does not pass, and a third asserts the empty-frozenset path is byte-identical to
   today. `test_policy_resolver.py` separately asserts the set is populated from the query.

I also confirmed the alternative the design rejects is rejected for a true reason:
`LoggingMiddleware.__call__` logs `arguments=%r` **before** `await call_next(request)`
(`middleware.py:44-46`, then `:49`), so scrubbing inside `ApiExecExecutor` could not have pre-empted
it. And `result.payload` is logged after, which the same `redact_arg_keys` pass covers.

### Medium 2 — the regression pressure is gone
The `test_routes_auth.py` case no longer contains "accepts the `tenant_id=NULL` viewer service
account". It now asserts exactly the three outcomes that pin the current gate: a `role="viewer"`,
`tenant_id=NULL` service account **not** in `TOOLEXEC_EXECUTE_SUBJECTS` gets 403 (named as the
vobiz/SDK case, with the note that this fails under an `is_platform_scoped`-only gate); a human
`superadmin` also gets 403 — which is what proves `is_service_account` is load-bearing rather than
incidental; and the allow-listed Conversation account succeeds. It adds a fourth case I did not ask
for and that is worth having: a body pairing tenant A's `tenant_id` with tenant B's `agent_id`
returns `api_not_enabled_for_agent` with zero transport calls and no `api_chain_runs` row, which
pins executor step 1's ownership join as well as the identity gate.

### Low 4 — the oracle is removed, not relabeled
`_derive(tag, tenant_id, custom_api_id, resolved_arguments)` is one function, one key, one canonical
input, with the tag prefixed into the HMAC message: `arguments_hash = _derive("claim", …)` is stored
(`api_chain_steps`, `api_side_effect_claims`) and `idempotency_key = _derive("idem", …)` is the only
value exported in `custom_apis.idempotency_header`. This is real separation, not a relabeling: HMAC-
SHA256 is a PRF, so outputs on the two distinct prefixed messages are computationally independent —
the tenant admin who reads the `idem` value off their own server learns nothing about the `claim`
value stored beside the redacted argument, and the shortlist attack (guess a value, invoke, compare
the header against the stored hash) no longer has anything to compare against. Drift is still
structurally impossible: same key, same canonical input, same function, differing only in a literal
tag. The exported value remains stable across turns, sessions and redials, so downstream dedupe is
unaffected. Test *(vi)* asserts stored `!= sha256(args)`, stored differs under a second key ref,
stored `!=` exported for the same call, and a `kid` change alters the stored prefix — four
assertions, each of which a single-derivation or bare-sha256 implementation fails.

### Low 3 — the tests can now fail, and two of them exercise the deployed value
Every one of the five controls has a case with a stated failure mode. Two details matter more than
the rest: the post-TTL admission case back-dates the claim row's `claimed_at` **at the deployed
default TTL** instead of shortening the constant, and the per-minute cap is tripped by driving real
calls at the deployed value — both are lesson 25 applied correctly, where a reconfigured constant
would have tested a system nobody runs. The byte-cap case counts bytes actually pulled and asserts
at most the cap plus one chunk, so it fails if the body is read to completion first. The admission
case asserts a *different* agent in the same tenant is still admitted, so it fails both if the cap is
missing and if it is applied per tenant. The `success_template` `PATCH`-after-the-fact case is named
as the one that fails if update re-validation was never implemented.

### The rotation caveat I raised in round 3 is still NOT stated
See finding 1. The design states the rotation cost as "one claim window of possible redialled
duplicates" and carries a rotation rule into `docs/setup.md`, but nowhere says that because the
exported `idem` key derives from the **same** rotated secret, the downstream API's own dedupe lapses
in the same window — so during a rotation there is no independent second line, only one.

## Findings

1. [low] The rotation rule does not state that the platform claim and the downstream dedupe lapse together, so an operator may believe the downstream API is an independent backstop — `executor.py` step 6 § Rotation, `docs/setup.md` row
   Attack: an operator rotates `TOOLEXEC_ARGS_HMAC_KEY_REF`, reading the stated cost as "our claim table forgets for one window, but the downstream API still dedupes on the idempotency header". It does not: `_derive("idem", …)` uses the same rotated key, so every pre-rotation downstream key changes too. For one `TOOLEXEC_SIDE_EFFECT_CLAIM_TTL` window both mechanisms are blind simultaneously, and a caller who redials in that window gets a second refund. The window is bounded and the primary consequence is disclosed, so this is a documentation defect on a security procedure, not a missing control.
   Fix: one sentence in the Rotation paragraph and in `docs/setup.md` — "both the platform claim and the downstream idempotency key derive from this secret, so a rotation blinds both for one window; drain side-effecting traffic or accept the duplicate risk for that period."

2. [low] Two stated rationales remain factually wrong about the primitive they name (restated, unfixed) — `validate_tenant_ref` docstring, `executor.py` step 5 path rule
   Attack: the k8s rule still claims the regex `[A-Za-z0-9._-]+` makes `..` "unrepresentable" — it matches `..` exactly, and only the `.resolve()`/`relative_to()` containment check rejects `k8s:tenants/<tid>/..`, which the docstring calls mere "belt". The path rule still claims `quote(value, safe="")` "percent-encodes `/ ? # % .`" — it does not encode `.`; the raw-`..` rejection is what stops traversal. Neither is exploitable as written, and the symlink test now protects the `.resolve()` line; but there is still no case for `k8s:tenants/<tid>/..` or for a path param of exactly `..`, so the raw-`..` clause remains removable on the strength of a comment calling it redundant.
   Fix: correct both comments to name the enforcing check; add the two cases.

3. [low] `resolve_tenant_ref` still does not state how the underlying `CompositeSecretResolver` is constructed (restated, unfixed) — `services/toolexec/auth_schemes.py`
   Attack: validation is against `TOOLEXEC_TENANT_SECRET_ROOT`, but the ref passed onward is `k8s:tenants/<tid>/<name>` and `K8sFileResolver` joins it under its own `mount_root`, default `/var/run/secrets` — the platform mount. Validated against one root, read from another. It fails closed today; if an operator ever sets `TOOLEXEC_TENANT_SECRET_ROOT` to `/var/run/secrets`, a tenant-authored ref is validated as in-namespace and read out of the platform secret mount.
   Fix: state `CompositeSecretResolver(k8s_mount_root=TOOLEXEC_TENANT_SECRET_ROOT)`; refuse to start if that root resolves to or inside the platform mount root.

4. [low] The OAuth2 token cache key omits the credential and destination the token was minted for (restated, unfixed) — `executor.py` step 7
   Attack: keyed `(tenant_id, custom_api_id)`. A `PATCH` can change `token_url`, `client_*_ref` or `endpoint_url` while a token minted under the old configuration is still cached and valid, so the previous provider's bearer token is sent to a host that should never have seen it.
   Fix: include a hash of the resolved auth config and the endpoint host in the key; evict on any `PATCH` touching `auth_config` or `endpoint_url`.

5. [low] Run-level `idempotency_key` is client-chosen and is the sole key to a run's recorded outcome (restated, unfixed) — `api_chain_runs`, `executor.py` step 3
   Attack: `UNIQUE (tenant_id, idempotency_key)` with the key from the body. The allow-listed execute subject can replay a key to read back a different run's recorded steps instead of executing, or pre-claim keys so legitimate executions return `chain_already_running`. Low because the reachable actor set is one named service identity.
   Fix: derive it server-side from `(tenant_id, agent_id, session_id, tool_call_id)` plus the caller, or bind the row to the claiming identity.

6. [low] No database-level same-tenant constraint on `agent_custom_apis`, `api_chain_runs`, `api_chain_steps` or `api_side_effect_claims` (restated, unfixed) — Data section
   Attack: `agent_custom_apis(agent_id, custom_api_id)` has FKs to two independently-tenanted tables and no tenant column, so a cross-tenant pairing is representable; isolation rests on `_authorize_agent_api` being called by every present and future write path. `api_side_effect_claims` carries `tenant_id` and `custom_api_id` with nothing tying them either. Not exploitable through any specified path.
   Fix: composite FKs to `(id, tenant_id)` on both parents, so the pairing is unrepresentable rather than merely unwritten.

## Verified controls

1. `custom_apis_tenant_name_key ON (tenant_id, lower(name)) WHERE deleted_at IS NULL` — tenant-scoped uniqueness, so no tenant can squat or block another's name and a soft-deleted name is reusable (lesson 3), with a test asserting both tenants may hold the same name.
2. `_authorize_agent_api` gates the `GET`/`PUT`/`DELETE` agent-enablement routes before any read or write, joining `custom_apis.id = $2 AND custom_apis.tenant_id = agents.tenant_id`, with a row-count assertion (AC 10).
3. Absent-id and wrong-tenant collapse to a 404 with byte-identical detail on every id-addressed route, tested to fail if either the status or the message diverges (lesson 2).
4. Tenant scoping decided by `deps.is_platform_scoped` (`tenant_id IS NULL`) rather than a role comparison — verified `services/config/deps.py:57` (lesson 24).
5. Admin writes behind `require_role("superadmin","admin")`, reads behind `get_current_user`; `CONSOLE_ROLES` (`deps.py:36`) confirms `supervisor`/`agent` cannot reach the service at all (lesson 4).
6. Run claim is `INSERT … ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING id` with an explicit loser path that makes no HTTP call (lesson 8).
7. `policy_resolver`'s enabled-API query joins `agents a ON a.id = aca.agent_id AND a.tenant_id = ca.tenant_id` — a runtime tenant fence independent of the write-time gate.
8. Depth and cycle enforcement recomputes `chain_levels` for the edited row and every transitive dependent inside the params write transaction under a per-tenant `pg_advisory_xact_lock`, with `graph.resolve_order` re-deriving depth at runtime as a backstop (AC 11, both halves).
9. Runtime depth is `min(request.max_chain_depth, 4)` — a client value can only lower the ceiling.
10. All DDL is additive, including `api_side_effect_claims`, the new `CHECK` and the new status values, so the `psql -f`-without-`ON_ERROR_STOP` hazard has nothing to half-apply (lessons 10, 13).
11. Every outbound call carries an explicit `httpx` timeout, `min(api.timeout_ms or 6000, remaining chain budget)`, with no implicit default and no retry layer (lessons 18, 19); deeper steps are recorded `skipped`.
12. Credentials stay references end to end: nothing literal in `auth_config` (tested), the OAuth2 token is never persisted, resolution is at call time, and an unresolvable or out-of-namespace ref fails to `unavailable` with no request sent and the ref absent from the error, the step row and the log — asserted against `caplog`.
13. `data` is populated only when `chain_status == "success"`, so a partial or failed chain returns no success-shaped field to fabricate from; `pipeline.py`'s booking guard is untouched (AC 2, 5, 6).
14. The execution path re-reads `custom_apis`/`agent_custom_apis` with `deleted_at IS NULL`/`enabled` every turn (lesson 16).
15. Barge-in reuses the existing `cancel_event` race with no new uninterruptible path, and there is no resume/poll endpoint (AC 8).
16. The revocation-lag limitation is stated as a known platform issue rather than assumed away (lesson 27).
17. `validate_tenant_ref` confines a tenant-authored ref to a provably tenant-owned namespace — `enc:` always, `env:TENANT_<uuid-hex-upper>_[A-Z0-9_]+`, `k8s:tenants/<tenant_id>/<name>` plus a `.resolve()`/`relative_to()` containment check — and is re-validated **at resolution** from the `custom_apis` row's own `tenant_id`. Checked against the real resolver (`libs/config_sdk/secret_resolver.py:36-44`): `env:JWT_SECRET`, `env:SECRET_ENCRYPTION_KEY` and `k8s:../../proc/self/environ` are unrepresentable, with tests asserting the sentinel platform secret was never read and that the symlink case fails if only the regex is implemented.
18. `_authorize_chain_runs` puts the tenant predicate in the query — `AND ($2::uuid IS NULL OR r.tenant_id = $2)` — with a 404 whose detail is byte-identical to an unknown `session_id`, covered by its own test case.
19. `/internal/chains/execute` is gated on `require_execute_subject` (`user.is_service_account and user.email.lower() in TOOLEXEC_EXECUTE_SUBJECTS`), verified non-forgeable: both claims are real JWT fields (`services/config/auth.py:29,32,47,50,68,71`) and `is_service_account` is set **only** by `scripts/create_service_account.py:37`'s direct `UPDATE` — no API route writes it, and `users.py` refuses to touch service accounts (lines 225, 306).
20. Executor step 1 verifies the whole ownership chain in one query before the run row is claimed and before any HTTP call — `a.id = $2 AND a.tenant_id = $1`, `ca.tenant_id = a.tenant_id`, `aca.enabled` — and writes `api_chain_runs.tenant_id`/`agent_id` from the verified row, never the body.
21. Resolved values are placed, never concatenated: header values must match `^[\x20-\x7E]*$`, path values are one `quote(value, safe="")` segment with raw `..` rejected, query values via `httpx`'s `params=`, body via `json=`/`data=` with no template substitution in the request path — with an injection case asserting the URL the transport received.
22. SSRF validation runs at registration **and** before every call: `https` only unless the host is in `TOOLEXEC_HTTP_HOST_ALLOWLIST`, no userinfo/fragment/non-default port, `getaddrinfo()` for both families with every record normalized through `ipaddress` against a deny-list covering `0.0.0.0/8`, `127/8`, `169.254/16`, RFC1918, CGNAT `100.64/10`, `192.0.0.0/24`, `198.18/15`, multicast/reserved and IPv6 `::/128`, `::1`, `fc00::/7`, `fe80::/10`, `ff00::/8`, `64:ff9b::/96`, plus `.is_global`; a multi-record host is rejected wholesale.
23. `PinnedResolverTransport(allowed_ips)` connects to the pre-validated address while leaving URL, SNI and certificate verification on the hostname, so the rebind window closes with TLS intact and `verify=False` is explicitly forbidden; `follow_redirects=False` is explicit and tested.
24. `arguments_hash` is HMAC-keyed with a platform secret held **by reference** (`TOOLEXEC_ARGS_HMAC_KEY_REF`), resolved once at startup through the platform resolver — explicitly not the tenant-namespaced one — with absence failing the service to start, the same fail-loud posture as `JWT_SECRET` (`services/config/auth.py:13`); the `kid` prefix namespaces rotations so old and new hashes cannot collide into a false match.
25. One derivation serves both the claim key and the downstream idempotency value, so they cannot drift; the previous `sha256(run.idempotency_key:custom_api_id)` is removed with its defect named, and the exported key is now stable across turns, sessions and redials.
26. The claim lives in its own `api_side_effect_claims` table rather than a partial index on the history table — `api_chain_steps` stays append-only for AC 14, and every claim column is `NOT NULL` so no NULL row slips past uniqueness. Scope `UNIQUE (tenant_id, custom_api_id, arguments_hash)` is session-independent, which is what blocks the redial.
27. The claim is one statement — `INSERT … ON CONFLICT … DO UPDATE SET … WHERE status = 'released' OR claimed_at < now() - $6::interval RETURNING id` — taken **before** the outbound call, zero rows as the loser's path (lesson 8), correctly expressing the window without a non-immutable index predicate. Release only on proof of no mutation (a 4xx other than 409); a timeout or any 5xx keeps the claim.
28. The 24h window is argued rather than assumed, and caller-identity keying is rejected for a reason I verified: webcalls have no ANI (`ToolExecutionContext.caller_number` is `""`) and SIP ANI is spoofable. Only byte-identical mutations are ever gated.
29. `CHECK (NOT side_effecting OR arguments_hash IS NOT NULL)` makes the NULL bypass of the uniqueness check unrepresentable, with a DB-level test that fails if the constraint is dropped.
30. `success_template` is constrained at registration (no placeholder equal to, descended from, or an ancestor of a `sensitive_response_paths` entry, none naming a `sensitive` param, re-validated when `PATCH` adds a path) and at runtime (interpolation reads only the redacted projection, control chars stripped, 120-char truncation, unresolved ⇒ `None` rather than a spoken `{{…}}`).
31. Admission is enforced before the run row exists: `chain_budget_ms` is `min(body, TOOLEXEC_MAX_CHAIN_BUDGET_MS)` so a client value can only lower it; `admission.acquire(tenant_id, agent_id)` caps concurrency (4) and runs-per-minute (60) per `(tenant_id, agent_id)`, returning `rate_limited` with no run row and no HTTP call; counters are in-process dicts swept inline with no background task or pool to tear down (lesson 26); `TOOLEXEC_MAX_RESPONSE_BYTES` (1 MiB) caps each step's response, short-circuited on `Content-Length`. `ToolStatus.RATE_LIMITED` is a real pre-existing enum member (`services/conversation/tools/types.py:28`), so the mapping does not need a new status.
32. **(round-3 medium 1)** The sensitive-input log leak is closed end to end, with the plumbing explicit: `p.sensitive AS param_sensitive` in the specialization query → `_specialize_execute_api` returning `(ToolDefinition, frozenset[str])` → `ResolvedToolPolicy.sensitive_arg_keys` → `build_default_chain(…, redact_arg_keys=policy.sensitive_arg_keys)` at `orchestrator.py:194` → `LoggingMiddleware(redact_arg_keys=…)` scrubbing matching keys at any depth of `request.arguments` and `result.payload`. Verified in the repo: `build_default_chain` (`middleware.py:168`) has exactly one non-test call site, inside `_execute_tool_call`, where `policy` is already in scope, and the chain is constructed per tool call so per-agent key state on the middleware is sound. `types.py` is deliberately unchanged, and the executor-side alternative is correctly rejected because `LoggingMiddleware` logs `arguments=%r` *before* `await call_next(request)` (`middleware.py:44-49`). Empty default preserves today's behaviour for every legacy tool; over-redaction across two APIs sharing a key name is accepted and fails safe. The superseded Risks sentence is deleted.
33. **(round-3 low 4)** Stored and exported hashes are domain-separated through a single `_derive(tag, …)` function — `"claim"` stored, `"idem"` exported — so the value handed to a tenant-controlled endpoint is never the value stored beside the redacted arguments. The oracle is removed rather than relabeled (distinct prefixed HMAC messages are computationally independent), and the shared key, input and function keep drift structurally impossible.
34. **(round-3 medium 2)** The `/internal/chains/execute` test case now pins the current gate instead of the superseded one: a non-allow-listed NULL-tenant `viewer` service account 403s (named as the vobiz/SDK case, and noted as failing under an `is_platform_scoped`-only gate), a human `superadmin` 403s, the allow-listed Conversation account succeeds, and a tenant-A/`tenant_id` + tenant-B/`agent_id` body returns `api_not_enabled_for_agent` with zero transport calls and no `api_chain_runs` row.
35. **(round-3 low 3)** Every new control now has a test with a stated failure mode, and two exercise the **deployed** value rather than a reconfigured constant (lesson 25): the post-TTL admission case back-dates `claimed_at` at the default `TOOLEXEC_SIDE_EFFECT_CLAIM_TTL`, and the per-minute cap is tripped by real calls. Also covered: cross-session redial refusal (fails if the claim is session-scoped), keyed-vs-plain and stored-vs-exported hash assertions (four, each failing a bare-sha256 or single-derivation implementation), `success_template` rejection for sensitive/descendant/ancestor/param placeholders plus the `PATCH`-after-the-fact re-validation, the budget clamp asserted on the deadline the transport observes, the concurrency cap asserted to admit a different agent in the same tenant, and the byte cap asserted on bytes actually pulled.

**35 controls verified sound.** 0 critical, 0 high, 0 medium, 6 low open — all six are documentation
accuracy, defence-in-depth hardening or accepted intra-tenant residue with no cross-tenant or
privilege-escalation path. Clear to implement.
