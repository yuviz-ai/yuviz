# QA report: Agentic API Task Execution (multi-level tool chains)

Built from: `a4f1ad0` on branch `feature/agentic-api-bot`, in the worktree
`/Users/chandankumar/yuviz/.claude/worktrees/agent-afa560ddeac7c1ed8`.

Environment: everything below was driven against **running processes**, not read out of the code.

- Baseline suites re-run first and green: `services/toolexec/tests` + `services/conversation/tests`
  = **671 passed**; `services/config/tests` = **332 passed**.
- Tool Execution Service (this feature's new service) started from the worktree on **:8600** via
  `python3 -m services.toolexec`, with `TOOLEXEC_TENANT_SECRET_ROOT`, `TOOLEXEC_ARGS_HMAC_KEY_REF=env:TOOLEXEC_HMAC_KEY`
  and the same `JWT_SECRET` the local Config Service uses. Logs: `…/scratchpad/qa/logs/toolexec*.log`.
- Config Service started from the worktree on **:8010** (the pre-existing :8000 instance runs the
  *main* checkout and lacks this commit's `max_chain_depth` / `engine='toolexec'` changes; it was
  left untouched).
- Admin UI started from the worktree on **:3000** (`NEXT_PUBLIC_CONFIG_SERVICE_URL=http://localhost:8010`,
  `NEXT_PUBLIC_TOOLEXEC_SERVICE_URL=http://localhost:8600`) and driven with **Playwright/Chromium**,
  screenshotting every step. Screenshots: `…/scratchpad/qa/shots/*.png`.
- Real outbound HTTP: `https://httpbin.org/...` (`/anything` echoes the request, so an upstream
  value landing in a downstream request body is directly observable; `/status/500`, `/delay/N`,
  `/bytes/N`, `/html`, `/redirect-to` cover the failure shapes). The SSRF deny-list makes a
  loopback/RFC1918 mock server unusable by design, so a public echo endpoint was the only way to
  exercise real chains.
  Note: this machine's Python trust store could not verify public TLS (corporate interception CA);
  `SSL_CERT_FILE` was pointed at the exported macOS system roots for the service process. That is
  environment configuration, not a code change.
- Seeded fixtures (via SQL + the real APIs): tenants `qa-alpha`/`qa-beta`, agents `qa-agent-a`/`qa-agent-b`,
  users `qa-super` (superadmin), `qa-admin-a`, `qa-viewer-a`, `qa-supervisor-a`, `qa-admin-b` (all
  password `QaPass123!`), plus ~30 custom APIs in `qa-alpha`.
- Roles were driven both through the REST APIs (`curl`, every role × every route) and through the
  browser (where each role *lands*, per lesson 22, and whether the panel blanks, per lesson 21).

Conversation-side behaviour was exercised by driving the real `ToolPolicyResolver` and the real
`ApiExecExecutor` + `ToolExecClient` against the live Config and Tool Execution services (login,
HTTP, JSON mapping, `ToolResult`), so "what the agent hears" below is observed, not inferred.

---

## Defects

1. **[high] A chain whose failure lands on a side-effecting step returns HTTP 500 and the run is
   never finalized — AC 5 / AC 6 are broken in the default configuration.**
   `side_effecting` defaults to `true` for a registered API, and `_persist_step` always writes
   `side_effecting = api_row["side_effecting"]` while passing `arguments_hash = NULL` for any step
   that is `skipped`, or that fails *before* the hash is derived. That violates the
   `api_chain_steps_side_effect_keyed` CHECK constraint, so the whole request dies.
   Steps (reproduced 4/4 times):
   1. As `qa-admin-a`, register `c1_upstream_500` = `GET https://httpbin.org/status/500`,
      `side_effecting=false`.
   2. Register `c2_side_effecting_target` = `POST https://httpbin.org/anything`,
      `side_effecting=true`, with one param `ref` sourced `upstream` from `c1_upstream_500` at `$.id`.
   3. Enable both on `qa-agent-a`.
   4. `POST /internal/chains/execute` as the conversation service account targeting
      `c2_side_effecting_target`.
   Expected: `chain_status:"partial"` (or `failed`) naming the failed upstream and the skipped
   downstream, run row finalized, the caller told the lookup failed so nothing was refunded.
   Actual: `HTTP 500 Internal Server Error`; `api_chain_runs.status` stays `'running'` forever; only
   the level-1 step row exists. Driven through the real `ApiExecExecutor` the agent receives
   `status=failed error=toolexec_unavailable payload={}` — no false success, but the honest
   "what did and didn't complete" story AC 5/AC 6 require is gone, and so is the AC 14 record.
   Second repro of the same root cause, no chain needed: a **missing required caller argument on a
   side-effecting API** (`a3_refund_order` invoked without `amount`) also 500s, where the identical
   call against a non-side-effecting API correctly returns `invalid_argument`.
   Evidence: `asyncpg.exceptions.CheckViolationError: new row for relation "api_chain_steps"
   violates check constraint "api_chain_steps_side_effect_keyed"` at `executor.py:641` in
   `…/logs/toolexec2.log`; DB rows shown above under session ids `s-crash1..3`, `sess-fail1`.

2. **[high] One string `literal_value` parameter bricks the row *and* every chain execution in that
   tenant.** `_replace_params` stores `json.dumps("ACC-42")` → JSONB `"ACC-42"`, and
   `db.json_col` treats any decoded JSON string as `"double-encoded JSON string"` and raises
   `RuntimeError` → 500.
   Steps:
   1. As `qa-admin-a`, `POST /tenants/{tenant}/custom-apis` with
      `params:[{"name":"account_id","location":"body","json_type":"string","source":"literal","literal_value":"ACC-42"}]`
      → **201** (looks fine; the response echoes the submitted params).
   2. `GET /custom-apis/{id}` → **500**. `PATCH /custom-apis/{id}` (any field) → **500**.
      `DELETE /custom-apis/{id}` → **500** (the authorize helper reads the row first).
   3. `POST /internal/chains/execute` for **any** api in that tenant → **500**, because
      `_build_api_tree` loads the params of every non-deleted API in the tenant, not just the target's
      subtree.
   Expected: a string literal is an ordinary JSONB scalar; the API should read, edit, delete and run.
   Actual: the row is unreadable, uneditable and undeletable through the API (only direct
   `UPDATE custom_apis SET deleted_at` recovered it), and while it exists **no `execute_api` chain in
   that tenant can run at all** — a tenant-wide outage caused by a normal admin action. Numeric and
   boolean literals are unaffected (`json.loads("5")` is not a `str`).
   Evidence: `RuntimeError: toolexec JSONB column is double-encoded JSON string` at
   `custom_apis.py:343` / `db.py:38`, from `_build_api_tree` (`executor.py:125`), `get_custom_api`
   (`custom_apis.py:358`) and `delete_custom_api` (`routers/custom_apis.py:52`).

3. **[high] "Edit" in the APIs sub-tab opens a blank form and saving it silently destroys the API's
   declared parameters, method, side-effecting flag and timeout.** `listCustomApis` (the panel's
   only source) does not return `params`; `openEdit` does `api.params.map(...)` → `TypeError`, so
   `setForm` never runs while `setEditing(api)`/`setCreating(true)` already have.
   Steps (from a clean state):
   1. Log into the Admin UI as `qa-admin-a`, open `/agents/qa-alpha/qa-agent-a` → Knowledge Base → APIs.
   2. Click **Edit** on `e_bad_path_target` (`POST`, `timeout_ms=8000`, `side_effecting=false`, one
      `upstream` param).
   3. The modal titled "Edit e_bad_path_target" appears with **every field empty** (Method reset to
      GET, Side-effecting reset to on, no parameters). Console: `Cannot read properties of undefined
      (reading 'map')`.
   4. Re-type Name / Description / Endpoint URL (the only fields the Save guard requires) and click
      **Save Changes** → saves with no error.
   Expected: the form is pre-filled with the stored API, or the Edit action fails visibly without
   offering a destructive save.
   Actual: `PATCH` sends `params: []`, `method: "GET"`, `side_effecting: true`, `timeout_ms: null`.
   Verified in the DB: the upstream param row is **gone** (`0 rows`), `chain_levels` 2→1,
   `side_effecting` f→t, `timeout_ms` 8000→NULL, `method` POST→GET. A declared dependency edge — the
   whole point of the feature — is destroyed by one click plus three keystrokes, with no warning.
   Evidence: `shots/edit-01-click-edit.png` (blank modal), `shots/edit-02-retyped.png`,
   `shots/edit-03-after-save.png` (row now reads `GET … chain_levels=1`).

4. **[medium] `PATCH /custom-apis/{id}` always 500s when the stored credential ref is `env:` or
   `k8s:` — an authenticated API can never be edited after creation.** `update_custom_api` re-validates
   the merged state with `tenant_id` taken from the DB row (an `asyncpg UUID` object), and
   `validate_tenant_ref` calls `uuid.UUID(tenant_id)`.
   Steps: create an API with `auth_scheme:"api_key"`, `auth_config.key_ref:"env:TENANT_<hex>_KEY"`
   (201) → `PATCH {"description":"y"}` → **500**.
   Expected: 200. Actual: `AttributeError: 'asyncpg.pgproto.pgproto.UUID' object has no attribute
   'replace'` at `custom_apis.py:173`. Scoped by test: `auth_scheme:"none"` → 200,
   `key_ref:"enc:…"` → 200 (the `enc:` branch returns before the `uuid.UUID` call), `env:`/`k8s:` → 500.
   Create works because the path parameter arrives as a string.

5. **[medium] An upstream 200 response whose body is not UTF-8-safe text crashes step persistence →
   500, no step row, run stuck `running`.** `body.decode("utf-8", errors="replace")` can yield NUL /
   unpaired escapes, which Postgres rejects inside JSONB.
   Steps: register `GET https://httpbin.org/bytes/1000` (`side_effecting=false`), enable, execute.
   Expected: the step is recorded (or fails cleanly as an unparseable response) and the caller is
   told the call failed. Actual: `asyncpg.exceptions.UntranslatableCharacterError: unsupported
   Unicode escape sequence` at `executor.py:799` from the **success** path persist
   (`executor.py:729`); HTTP 500; `api_chain_runs.status = 'running'`. Any upstream that can return
   a PDF/image/binary error page takes the whole turn down this path.

6. **[medium] A caller value that does not parse as the declared `json_type` escapes as an
   unhandled exception, leaks a raw Python message, and poisons the idempotency key.**
   Steps: register `m_int_param` with one `caller` param `qty` of `json_type:"integer"`; execute with
   `caller_arguments:{"qty":"three"}`.
   Expected: `chain_status:"invalid_argument"` naming `qty`, run finalized.
   Actual: `HTTP 400 {"detail":"invalid literal for int() with base 10: 'three'"}` (a raw Python
   error through `app.py`'s blanket `ValueError` handler), no step row, run left `'running'` — so the
   corrected retry **under the same idempotency key** is answered
   `{"chain_status":"failed","error":"chain_already_running"}` forever. With `{"qty":{"a":1}}` the
   same path raises `TypeError` and returns **500**. The LLM supplies these values, so "three items"
   is an everyday input.

7. **[medium] A duplicate API name returns 500 instead of a clear conflict.**
   Steps: `POST /tenants/{t}/custom-apis` with `name:"dup_probe"` twice → 201 then **500**. Same for
   the case variant `DUP_PROBE` (the unique index is on `lower(name)`).
   Expected: 409/400 with something like "an API named dup_probe already exists".
   Actual: `asyncpg.exceptions.UniqueViolationError … "custom_apis_tenant_name_key"` at
   `custom_apis.py:409`; in the UI the error banner would read "Internal Server Error". (The
   Config Service's equivalent duplicate — a second `execute_api` tool policy — correctly returns
   409, which is what the handling here should look like.)

8. **[medium] Runs are never finalized after a crash or a restart; there is no reaper.**
   Every 500 above, and killing the service mid-call, leaves `api_chain_runs.status = 'running'`
   with `finished_at NULL`. Consequences observed: (a) AC 14's history shows a run that ran forever
   with no error; (b) `_claim_run`'s loser path answers that idempotency key
   `"chain_already_running"` permanently.
   Steps for the restart case: register a side-effecting `POST https://httpbin.org/delay/10`
   (`timeout_ms=15000`), execute it, `pkill -f services.toolexec` after 4 s, restart.
   Expected: on restart (or on the next read) an abandoned run is marked failed/unknown.
   Actual: run `s-kill` is still `running`. The side-effect claim correctly stayed `claimed`
   (fail-closed — that part is right).

9. **[medium] `missing_fields` is computed and then dropped, so the agent cannot ask for the input
   it is missing.** `_resolve_arguments` collects every missing required `caller` field into
   `_StepFailure.missing_fields`, but `_run_steps` never copies it into `ChainExecuteResponse`
   (always `[]`), and `ApiExecExecutor` returns `payload={}`.
   Observed: `a1_lookup_account` with `caller_arguments:{}` →
   `{"chain_status":"invalid_argument","missing_fields":[],"error":"missing_fields"}`; through the
   real executor: `status=invalid_argument error='missing_fields' payload={}`.
   Expected: the names/descriptions of the missing fields reach the turn so the agent can ask for
   them. Actual: the agent knows only that *something* was missing.

10. **[medium] A `partial` chain tells the agent nothing about what did complete (AC 6).**
    `ApiExecExecutor` maps `partial` to `FAILED` with `payload={"partial": true}` and deliberately
    drops `steps`/`completed_steps`/`failed_step`.
    Observed through the real executor: a 3-step chain where step 1 succeeded and step 2 timed out
    →`status=failed error='step_timeout' payload={'partial': True}`.
    Expected (AC 6): "the turn's response … reflect[s] what did and didn't complete". Actual: only
    the persisted record does; the turn's response cannot name the completed step, so the agent
    cannot say "I found your account but couldn't place the refund".

11. **[medium] No validation of the LLM-facing `name` at all.** Observed, each returning 201:
    `name:""` (renders as a **nameless row** in the UI with live Edit/Delete buttons — see
    `shots/final-02-delete-dependency.png`, first row — and enters `execute_api`'s enum as `""`);
    `name:"  g_ui_created  "` (leading/trailing spaces preserved, and since the unique index is on
    `lower(name)` the untrimmed and trimmed forms are *different* APIs that look identical in HTML —
    created straight from the UI form, `shots/create-01-form.png`); `name:" dup_probe"` accepted
    beside `dup_probe`; a 1000-character name accepted. `description:""` also accepted, which puts a
    bare `- : ` line into the tool description the LLM reads.
    Expected: trim, non-empty, length-capped, and the `snake_case` shape the UI hint promises.

12. **[medium] A model-supplied `path` parameter can span multiple URL path segments, defeating the
    documented single-segment confinement (lesson 31).** `quote(value, safe="")` does encode `/`,
    but httpx normalizes `%2F` back to `/` before the request goes out.
    Steps: register `i_inject_probe` with `endpoint_url:"https://httpbin.org/anything/{seg}"` and a
    `path` param `seg`; execute with `caller_arguments:{"seg":"a/b/c"}`.
    Expected: one path segment (`/anything/a%2Fb%2Fc`) or a rejection.
    Actual: `chain_status:"success"`, and the echoed url is `https://httpbin.org/anything/a/b/c`.
    Confirmed the transport is responsible: `httpx.get("https://httpbin.org/anything/a%2Fb%2Fc")`
    also reports `…/anything/a/b/c`. Practical impact: an `endpoint_url` of
    `https://api.example.com/orders/{order_id}` can be re-targeted to `/orders/123/refund` by a
    spoken value. `..` is still blocked by the explicit substring check, so this is segment
    *appending*, not traversal.

13. **[medium] Header parameters reject all non-ASCII, so an ordinary accented name fails the whole
    chain.** `_HEADER_VALUE_RE = ^[\x20-\x7E]*$`.
    Steps: `i_inject_probe` with `{"seg":"ok","X-Note":"José"}` →
    `{"chain_status":"invalid_argument","error":"illegal_header_value"}` (same for `Müller`, emoji,
    Arabic). CR/LF rejection is correct and verified; the collateral is every European name.
    Expected: encode (RFC 8187 / percent-encoding) or at minimum fail only the header rather than
    the turn. Actual: the caller is told the operation could not be completed because their name has
    an accent. A `path` param with the same value works fine (`/anything/café-Ω` → success).

14. **[medium] The whole-chain budget field and its warning ignore the platform ceiling, so raising
    the budget to silence the warning changes nothing.** The UI compares the worst case against
    `agent_tool_policies.timeout_ms` with no knowledge of `TOOLEXEC_MAX_CHAIN_BUDGET_MS` (30 000 ms,
    the deployed default), and the number input has no maximum or hint.
    Steps: three chained `https://httpbin.org/delay/10` APIs (`timeout_ms=15000` each), execute with
    `chain_budget_ms:45000`.
    Expected: either the 45 s budget is honoured, or the UI tells the admin 30 s is the hard ceiling.
    Actual: step durations 11235 ms, 10510 ms, then step 3 was given the remaining ~8.2 s and timed
    out → `partial`. An admin who "raises the budget above" as the warning instructs still gets a
    mid-chain timeout.

15. **[low] The Tool Execution Service does not enforce the `execute_api` master switch itself.**
    `_verify_ownership` LEFT JOINs `agent_tool_policies … AND atp.enabled`, so a disabled policy
    only removes the tool from the LLM's list on the conversation side.
    Steps: switch **Enable custom API execution** off in the UI (verified: `agent_tool_policies.enabled=f`
    and the resolver no longer returns `execute_api`), then `POST /internal/chains/execute` as the
    conversation service account → `chain_status:"success"`, real outbound call made.
    Expected, given the UI copy "this agent gets zero execute_api tool calls regardless of the
    toggles below": the execute route refuses. Actual: it runs. Only reachable by the allow-listed
    service identity, hence low.

16. **[low] A viewer is shown fully enabled write controls that all 403 on click.**
    Steps: log in as `qa-viewer-a`, open the APIs sub-tab. The list renders correctly (lesson 21
    satisfied — no blanking, `shots/viewer-apis.png`), but "+ New API", "Edit", "Detach", "Delete",
    the per-API toggles and the master switch are all interactive; clicking a toggle yields the
    banner `role 'viewer' cannot perform this action` (`shots/viewer-after-toggle.png`).
    Expected: read-only affordances for a read-only role. Actual: authority discovered by clicking.

17. **[low] AC 14's chain history has no console surface, and its client helper is dead code.**
    `getChainRuns`/`ChainRun` in `admin-ui/lib/toolexecApi.ts` are referenced by nothing
    (`grep -rn getChainRuns admin-ui` → the definition only). Inspecting a failed chain requires
    `curl /calls/{session_id}/chain-runs`. The API itself works and is correctly tenant-scoped.

18. **[low] The APIs form cannot configure `idempotency_header`, `body_style`, `success_template` or
    the per-agent `max_chain_depth`,** all of which the schema, the API and AC 11/AC 15 treat as
    first-class (the downstream dedupe header of AC 15 is only reachable via `curl`). Everything in
    AC 16's explicit list is present, so this is a completeness gap, not a missed AC.

19. **[low] `timeout_ms` accepts nonsense values.** `-5000` → 201, and every execution of that API
    then returns `chain_status:"timeout"` with nothing to explain why; `0` → 201 and is silently
    treated as the 6000 ms default (`or 6000`); `99999999` → 201. The UI's number input has no
    `min`.

20. **[low] Silent type coercion.** `4.7` into an `integer` param is truncated to `4` and sent as
    `{"qty": 4}` (a quantity or amount silently changed); `null` satisfies a **required** param and
    is sent as JSON `null` (required-ness only checks key presence).

21. **[low] `start_toolexec_service()` does not guard `TOOLEXEC_ARGS_HMAC_KEY_REF`.**
    `docs/setup.md` documents it as "required, no default", and `JWT_SECRET` /
    `SECRET_ENCRYPTION_KEY` get `${VAR:?…see docs/setup.md}` one-line hints in the same file — this
    one does not, so an operator following the launcher gets uvicorn's ~40-line traceback ending in
    the (good) message `TOOLEXEC_ARGS_HMAC_KEY_REF is not set — side-effect claims and downstream
    idempotency keys cannot be derived.` Likewise `auth_schemes.py` does `os.environ["TOOLEXEC_TENANT_SECRET_ROOT"]`
    at import, so following `app.py`'s own `uvicorn services.toolexec.app:app` line without that
    variable gives a bare `KeyError`. Sourcing `scripts/start_local.sh` under `set -euo pipefail`
    does **not** kill the shell (lesson 15 re-verified).

---

## Verified working

34 scenarios exercised against the running stack and found correct:

1. Tool Execution Service starts, `/health` 200, and **fails loudly at startup** with a precise
   message when `TOOLEXEC_ARGS_HMAC_KEY_REF` is unset (never passes health and then fails on the
   first chain).
2. `scripts/start_local.sh` sources cleanly into a fresh `set -euo pipefail` shell and returns 0.
3. Register a real custom API through the REST API and through the **UI form**, with params of every
   source kind (`caller`, `literal`, `upstream`) and locations `body`/`query`/`header`/`path`.
4. AC 16: the agent detail page's Knowledge Base tab shows **Documents** and **APIs** sub-tabs;
   Documents is unchanged (retrieval settings render as before); APIs shows the master switch, the
   whole-chain budget, and the tenant's registered APIs with per-agent toggles
   (`shots/admin-04-apis-subtab.png`).
5. AC 1: with **7** custom APIs enabled, exactly **one** LLM-facing entry (`execute_api`) is produced,
   with the api names in its `enum` and their caller inputs appended to its description.
6. AC 1 additive: with `book_appointment` also enabled the LLM sees
   `['execute_api','book_appointment','cancel_appointment','reschedule_appointment']` — legacy tools
   unchanged, still separate entries.
7. AC 9: an agent with an `execute_api` policy but **zero** enabled custom APIs gets the policy
   dropped entirely (no empty enum, no tool).
8. Master switch off → `execute_api` disappears from the resolved tool list; legacy tools unaffected.
9. AC 3: an `upstream`-sourced parameter is fetched server-side — the LLM supplied only `phone`, and
   `a2_lookup_order` was called with `account_ref` extracted from `a1_lookup_account`'s response.
10. AC 4: a genuine **4-level** chain (`f1→f2→f3→f4`) ran in declared order and the level-1 value
    `SEED-1` arrived in level 4's request body; `from_prior_step` names the sourced params per step.
11. AC 4: a 3-level chain with a `success_template` produced
    `"Refund of 42.5 on order +15551234567 is confirmed."` interpolated from the redacted projection.
12. AC 11 registration: a 5th chain level is rejected before it can be enabled —
    `400 {"detail":"chain_depth_exceeded"}`; levels 1-4 store `chain_levels` 1,2,3,4 correctly.
13. AC 11 per-agent override: `max_chain_depth=2` makes enabling a `chain_levels=4` API fail with
    `chain_depth_exceeds_agent_ceiling: api chain_levels=4 > effective max_chain_depth=2`, **and** at
    runtime an already-enabled deep chain stops with `error:"depth_limit_exceeded"` and no HTTP call.
14. The caller's requested `max_chain_depth` is clamped server-side (asked 4 with an override of 2 →
    refused).
15. AC 7: per-step timeout — `https://httpbin.org/delay/10` with `timeout_ms=2000` →
    `timeout`/`step_timeout`, chain `partial`, no retry, no hang.
16. Whole-chain budget: 5000 ms across three 2 s steps → step 1 success, step 2 given the remainder
    and timing out, step 3 `skipped`, `partial`. The platform ceiling clamps a larger request.
17. Upstream 500 mid-chain → that step `failed` with `http_status_500`, downstream `skipped`
    (when not side-effecting), chain `partial` — honest, never a success.
18. A JSON path that does not match the upstream response shape → `upstream_value_missing`, chain
    `partial`, no fabricated value.
19. AC 12: an unresolvable-but-well-formed credential ref → `chain_status:"unavailable"`,
    `error:"credential_unavailable"`, **no request sent**, and the ref appears nowhere in the
    response or the log (grepped).
20. AC 12/lesson 29: credential refs outside the tenant namespace are rejected at registration —
    `env:JWT_SECRET`, another tenant's `env:TENANT_<otherhex>_TOK`, `k8s:tenants/<me>/../../etc/passwd`
    → `credential_ref_outside_tenant_namespace`; a literal `sk-live-…` → `credential_ref_not_a_reference`.
21. AC 15 redial: after a side-effecting step succeeded, the same arguments from a **new session with
    a new idempotency key** are refused — `side_effecting_step_already_completed`, no second outbound
    call. A genuinely different amount is allowed through.
22. AC 15 concurrency: two simultaneous requests with the *same* idempotency key → one run, the loser
    gets `chain_already_running`; two simultaneous requests with *different* keys but identical
    arguments → exactly one fired, the other refused (lesson 8 holds under real concurrency).
23. A side-effecting step that timed out **keeps** its claim (`status='claimed'`), including when the
    service is killed mid-call; a non-409 4xx **releases** it and an immediate legitimate retry is
    allowed (verified with `status/404`).
24. The derived idempotency key is sent downstream in the configured header
    (`Idempotency-Key: k1:625d4e…`) and is domain-separated from the stored `arguments_hash`.
25. AC 14: `GET /calls/{session_id}/chain-runs` returns each run with every step's `api_name`,
    `level`, `status`, `http_status`, `duration_ms`, `argument_sources`
    (`{"card":"caller","note":"caller"}`) and redacted arguments/response.
26. Redaction: a `sensitive` param and a `sensitive_response_paths` entry both render as
    `"[redacted]"` in the step row, in the `data` projection returned to the LLM, and the raw card
    number never reached the log; a `success_template` that names a sensitive path is rejected at
    registration.
27. AC 10 tenant isolation, driven as `qa-admin-b` against tenant A: list → 403;
    `GET`/`PATCH`/`DELETE /custom-apis/{A's id}` → identical `404 {"detail":"not found"}`;
    enable A's API on A's agent → 404; enable A's API on B's **own** agent → 404; list A's agent's
    APIs → 404; A's chain history → 404; declaring A's API as an upstream of B's own API → 400
    `unknown_upstream_api` (AC 17). Mismatched `tenant_id`+`agent_id` in an execute body →
    `api_not_enabled_for_agent`.
28. Role matrix on every route: `viewer` reads 200 / writes 403 with a clear message; `supervisor`
    is refused everywhere by the console gate and **lands on `/no-access`** with an explanatory card
    (lesson 22, `shots/supervisor-apis.png`); `superadmin` full access; no token → 401 "missing or
    malformed Authorization header"; a garbage bearer → 401.
29. `/internal/chains/execute` is gated on a **named service identity**: a human `superadmin` token
    gets `403 identity may not execute API chains`; the conversation service account succeeds
    (lesson 24's inverse holds).
30. Soft-delete: deleting an API that is still `enabled=true` on an agent makes it immediately
    unexecutable (`api_not_enabled_for_agent`), drops it from the LLM enum and from the agent's list,
    with no cascade write. Deleting an API another API still depends on → 409 naming the dependent,
    surfaced in the UI banner (`shots/final-02-delete-dependency.png`).
31. SSRF: `http://127.0.0.1:8600`, `https://localhost`, `http://169.254.169.254`, `https://10.0.0.5`,
    `https://[::1]`, `https://0x7f000001`, `https://2130706433`, userinfo, fragment, non-default
    port, `file://` and an unresolvable host are each rejected at registration with a code that
    reveals no hostname or resolved IP; a registered `redirect-to` endpoint is **not followed**
    (302 → `failed`, `http_status_302`).
32. Response cap at the deployed 1 MiB: a 1.2 MB echoed response → step `failed`, chain refused
    rather than read unbounded.
33. Admission control: 7 concurrent runs for one (tenant, agent) → 4 `success`, 3 `rate_limited`
    (mapped to `ToolStatus.RATE_LIMITED`), no run rows or HTTP calls for the refused ones.
34. Double-submit in the UI: double-clicking **Create API** issues exactly one POST (only one row
    created); XSS/unicode in name and description is escaped and rendered as text
    (`<img src=x onerror=…>`, emoji, RTL and an apostrophe all safe, `window.__xss` never set);
    query-parameter values are correctly percent-encoded (`1&admin=true#frag` stays one value);
    the conversation-side executor reports `failed / toolexec_unavailable` when the Tool Execution
    Service is down.

---

## Not covered

- **AC 8 (barge-in mid-chain).** Requires a live call through Envoy/gateway/FreeSWITCH with real
  STT/TTS to produce an interruption; no `cancel_event` was driven, so the claim that a chain step
  cancels like any other tool call is **untested here**. The chain does keep running server-side by
  design, which makes this worth an explicit test.
- **A real LLM choosing `execute_api`.** The tool schema, the enum and the executor were driven
  directly; no Ollama turn was run, so "the model picks the right api_name and supplies only leaf
  inputs" is unverified, as is the pipeline fabrication guard's behaviour on a chain result.
- **OAuth2 client-credentials happy path.** No OAuth2 token endpoint was available; only the
  registration-time ref validation and the failure mapping (`credential_unavailable`) were
  exercised. The token cache, its 60 s early refresh, and refresh-on-expiry are untested.
- **`enc:` credential resolution end to end.** An `enc:` ref is accepted at registration but no
  Fernet-encrypted tenant value was minted and resolved through an outbound call.
- **`k8s:` refs against a populated `TOOLEXEC_TENANT_SECRET_ROOT`.** Only the rejection paths were
  driven; no secret file was planted, so a successful tenant-namespaced file read is unverified.
- **`body_style:"form"`, and `PUT`/`DELETE` methods.** All executions used JSON bodies with
  `GET`/`POST`.
- **Multi-replica behaviour.** Admission caps are per-process by design; only one replica ran.
- **Anything requiring a loopback or RFC1918 upstream** (a local mock API, a connection-refused
  peer, TLS failures, slow-loris responses): the SSRF deny-list blocks every private address with no
  allow-list exemption, so all upstream behaviour was tested against `httpbin.org`. The unhandled
  `httpx.ConnectError → 500` I hit accidentally (before fixing this machine's CA bundle) is
  therefore reported only as part of defect 8's "runs never finalize" pattern rather than as a
  reproducible connection-failure defect.
- **Concurrent registry edits** (two admins saving overlapping dependency graphs at once). The
  per-tenant advisory lock was not exercised under real contention.
- **The pre-existing `:8000` Config Service and `:3000` main-checkout UI** were not tested; QA ran
  against worktree instances on `:8010`/`:3000`.

## Environment left behind

Stopped: the Tool Execution Service on :8600, the worktree Config Service on :8010, and the
worktree Admin UI on :3000, all of which this report started. The user's own `next dev` in
`/Users/chandankumar/yuviz/admin-ui` (which had to be stopped to free :3000) was restarted; the
`:8000` Config Service, `:8100` Knowledge Service and `:8300` webcall process were never touched.

Left in the `voiceai` database (isolated to new rows, nothing existing modified): tenants
`qa-alpha`/`qa-beta`, their two agents, five `qa-*@example.com` users, ~30 `custom_apis` rows in
`qa-alpha` with their params/enablements, and the `api_chain_runs`/`api_chain_steps`/
`api_side_effect_claims` rows those executions produced. Two rows bricked by defect 2
(`lookup_account`, `lookup_order`) were soft-deleted with SQL to make any chain in that tenant
runnable again — that is the only direct DB write outside seeding.
