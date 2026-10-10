# Security review: .sdlc/one-click-crm-integrations/02-design.md (design mode, round 4 — final)
VERDICT: GREEN

Scope: the four round-3 findings re-verified against the **shipped** `connector-presets-oauth` code (merged in
PR #64, in-tree on `redesign`) rather than against another document; the declared change to a round-3 verified
control (`{"outcome","items"}` → `{"outcome","items","spoken"}`); the 21-row reconciliation table's
load-bearing citations; and a threat model of what the revision introduces (`spoken`, `revoke_style="path"`,
the `access_token_for` 4-tuple, CRM content in the model's context).

**All four round-3 findings are closed.** F1 is closed as *accepted residual under OQ9 Option A*, which is the
legitimate outcome here, because the trace the architect offers is true on the shipped code: there is no
channel to the caller's ears that does not also reach the model's history, so the deleted fence was never a
fence. F2 and F4 are fixed as claimed. F3's four containment layers hold against `graph.extract`,
`_interpolate_success_template` and `json.dumps` as they actually are; its residual (meaning, not form) is
accepted in writing. The widened three-key return is still a closed, asserted enumeration. What remains below
is three lows — one design self-contradiction, one misstated mechanism, one unnamed second call site. None is
exploitable as designed; none blocks implementation.

**F1 [high, round 3] — closed as accepted residual, trace verified line by line.**
`data = final_redacted_response` (`executor.py:949`) is both the input to `_interpolate_success_template`
(`:955`) and the `data` field of `ChainExecuteResponse` (`:959-961`); `api_exec_executor.py:87`
(`services/conversation/tools/executors/api_exec_executor.py`) does `payload = dict(response.get("data") or {})`
on the SUCCESS branch; `orchestrator.py:163` calls `_fold_tool_result_into_history`, which `json.dumps`es
`{"status": …, **result.payload}` into a `role="tool"` ChatMessage (`:302-315`) — **before** the
`if result.deterministic_response is not None` short-circuit at `:164`. Confirmed in-tree. A fence that
withheld fields from `spoken` while leaving them in `items` would therefore have constrained nothing, and
Option B (match signal only) would have had to suppress the spoken line too — exactly as the design says. The
replacement control is an upper bound on the whole channel, and it is single-sited and mechanical: the
transform is the closed three-key/four-field literal, it is total and fail-closed, it runs only inside the 2xx
branch (`executor.py:857-858` guard above the `:870-876` hook — verified), and `redaction.redact`
(`redaction.py:55-59`, `_redact_one` at `:24-52`) deep-copies and only **replaces**: every path that cannot be
walked `return`s early, so there is no code path that adds a key. The residual — a spoofed-CLI caller hears
name, employer and account owner, repeatable one number per call — is written out in the OQ9 section, the
Risks list and the findings-closure section, and was decided by the user. Accepted, not missed.

**F3 [medium, round 3] — containment verified; residual accepted.** (a) `items` travels through
`json.dumps`, which escapes `"` and `\` even before the step-3a filter removes them, so no value terminates its
own JSON string. (b) `_TEMPLATE_PLACEHOLDER_RE.sub(_resolve, template)` scans the **template** once and, with a
callable `repl`, uses the returned string verbatim and never rescans it — so an injected `{{…}}` inside a value
is not expanded even before the filter drops `{`/`}`; and `graph.extract` (`graph.py:79-109`) walks a parsed
path over the dict and cannot be steered by a value's content. (c) `spoken` is a `presets.py`-authored carrier
sentence, built to ≤120 so the executor's own cut never lands mid-word. (d) `_interpolate_success_template`'s
`_CONTROL_CHAR_RE.sub` + `text[:120]` are real and independent (`executor.py:659-660`). Four layers, each
survives deletion of the others.

**F2 / F4 [low, round 3] — fixed.** The per-provider table now carries the *(match only, never projected)*
`record_phone` row with the three literal paths, the scalar rule is written to cover it, and a non-`str`
phone drops the record rather than raising; the survivor count is stated once as taken **after** the
non-`str` `contact_id` drop. Both have matching test-plan blocks that can actually fail.

**The declared change to a verified control — the defect is real, its stated failure mode is not (finding 2).**
A two-key return does break `{{$.spoken}}`: `graph.extract` returns `MISSING` and `_resolve` raises. The
widened key set is still closed and asserted: `match_count` absent on every path, `items` entries exactly the
four projected keys, `spoken` carrying no field the four do not, `""` on every non-`match` path so no outcome
is distinguishable by its presence, and test 3 asserts the enumeration's own size as well as its members
(lesson 12). That control is intact.

## Findings
1. [low] Three places still return/assert the superseded **two-key** literal while step 4 says three keys on
   every path — including the one path (step 0) an implementer reads while writing the fail-closed branch.
   — design projection step 0 (`:492`), Data sink 2 (`:212`), Test plan test 3 fail-closed block (`:906`)
   Step 4: "Return exactly `{…, "spoken": …}` — **three keys, always, on every path including step 0's**".
   Step 0, the Data section and the test plan all still say `{"outcome": "no_match", "items": []}`, and the
   test-plan paragraph contradicts itself inside four lines: it asserts "the result is exactly
   `{"outcome": "no_match", "items": []}`" and then "the key set of **every** return value across all cases is
   exactly `{"outcome","items","spoken"}`". Two sources of truth for the key set the whole model-facing upper
   bound rests on (lesson 32); the implementer reads the one next to the branch they are writing.
   Attack: a caller dials a DID whose lookup returns no match. The implementer followed step 0, so `spoken` is
   absent; `{{$.spoken}}` resolves `MISSING`, `_interpolate_success_template` returns `None`, no
   `DeterministicSpokenEvent` is yielded, and the model narrates that turn itself from the tool message in
   history instead of the fixed line — the deterministic-response control (criterion 22's shape) silently
   stops applying on exactly the path it was written for. No PII leaks, because `items` is empty and step 0
   copies no fragment of the body; the loss is determinism, plus a test pair that cannot both pass.
   Fix: delete the two-key literal at `:212`, `:492` and `:906` and write `{"outcome": "no_match", "items": [],
   "spoken": ""}` in all three, or state the key set once in step 4 and have the other three defer to it.
2. [low] The design states `_interpolate_success_template` "**fails the whole step** with
   `unresolved_placeholder`" on a `MISSING` placeholder. It does not — it fails **soft to `None`**.
   — design reconciliation table (`{{$.spoken}}` row), step 4 rationale, findings-closure note; verified at
   `services/toolexec/executor.py:662-665`
   `_resolve` raises `_StepFailure("failed","unresolved_placeholder")` at `:658`, but `:662-665` wraps the
   `re.sub` in `try/except _StepFailure: return None`. The chain status was already computed as `success`
   before the template runs (`:952`), so the step and the chain stay successful; only
   `deterministic_response` becomes `None`. The design's claim that "the two-key shape made every lookup
   **fail**" overstates it: the lookup succeeds and the model gets the turn.
   Attack: a tester writes the assertion the design implies — "a missing `spoken` fails the step with
   `unresolved_placeholder`" — and it is red against correct code, or is softened to something vacuous
   (lesson 12). Operationally, any future template/transform drift degrades a deterministic spoken line into
   free model narration of whatever is in `history`, with no error, no failed step and no alert; under
   Option A that history holds the contact's name, employer and owner.
   Fix: restate the mechanism as "returns `None`, so the step still succeeds and the model narrates the turn"
   — and say that this fail-soft is why `spoken` must be present on every path rather than merely desirable.
3. [low] Only one of `access_token_for`'s two production call sites has its post-widening behaviour
   specified. — design Changes row for `services/toolexec/oauth.py` ("both existing call sites are updated in
   the same change"); `services/toolexec/oauth.py:311` (`post_json`), `services/toolexec/auth_schemes.py:223`
   Mechanically confirmed there are exactly two non-test call sites, so the count is right. The design then
   specifies the new rule for one of them only (`apply()` → `provider_host_allowed(..., base_source=
   api["endpoint_base_source"])`, `api["endpoint_url"]` deleted). `post_json` — the preset setup-call helper —
   carries its own host binding, `urlsplit(url).hostname not in provider.api_hosts` (`oauth.py:312`), and the
   design never says whether that rule changes. It must not: `post_json` has no `custom_apis` row and so no
   `endpoint_base_source` to branch on, which is the predicate that makes the call-time check exact
   (lesson 37 — a widened shared utility carries its old trust boundary into every existing consumer).
   Attack: an implementer "updating both call sites" for Salesforce makes `post_json` accept the connection's
   `api_base_url` as an allowed host too, since that is the obvious symmetry. `post_json`'s host check is then
   satisfied by any origin stored on the connection row rather than by the provider's fixed `api_hosts`, and
   the per-row predicate that bounds it in `apply()` has no analogue here — so a future preset setup call
   gains a reachable-origin set validated only at connect time. No tenant-authored URL reaches `post_json`
   today, which is why this is low and not higher.
   Fix: one line in the Changes row — `post_json` takes the widened tuple but keeps
   `hostname in provider.api_hosts` unchanged; composed origins are reachable only through a `custom_apis` row
   whose `endpoint_base_source = 'oauth_connection'`.

**Carry-over note for the implementer (not a finding, no attacker):** the signature change to
`auth_schemes.apply()` breaks three shipped tests that guard the control D1 rewrites —
`test_apply_sets_the_bearer_for_a_provider_host`, `test_apply_refuses_an_off_provider_host_before_setting_the_header`
and the fresh-interpreter `test_the_function_local_import_resolves_in_a_fresh_interpreter` tripwire
(`services/toolexec/tests/test_oauth.py:728-781`), all of which call `apply(api, headers, {})` positionally and
rely on `api["endpoint_url"]` being the dialed URL. They must be **migrated to pass `effective_url`**, not
deleted to go green (lesson 42). Design test 2 re-asserts the control, so the coverage is replaced rather than
lost, but the three names belong in the Changes table.

## Verified controls
- **Round-3 F1's trace, in-tree:** `executor.py:949` → `:955`/`:959-961` → `api_exec_executor.py:87` →
  `orchestrator.py:163` fold → `:302-315` `json.dumps` → `:164` short-circuit. No channel reaches speech
  without the model, so deleting the `spoken` fence loses nothing and OQ9 Option A is the whole disclosure
  decision. Accepted residual, stated three times in the document.
- **The replacement upper bound is enforceable where it is claimed:** `redaction.redact` (`redaction.py:55-59`)
  deep-copies and replaces only — `_redact_one` returns early on a malformed path, a non-dict container, an
  absent key or an out-of-range index (`:33-48`), so it can never add a key to the transform's output. The
  bound therefore covers `items`, `spoken`, `prior_responses`, `response_redacted` and the chat history alike.
- **Placement verified, not deferred:** `if not (200 <= status_code < 300): raise` at `executor.py:857-858` is
  above the transform hook at `:870-876`; the `{"_raw": …}` construction is at `:855`; the failure branches
  write `response_redacted=None` (`:902`, `:924`). Step 0 and the placement remain independent.
- **F3 containment, each layer checked against the real function:** `json.dumps` escaping; `re.sub` with a
  callable repl not rescanning its own output and the template being scanned once; `graph.extract` walking a
  parsed path (`graph.py:79-109`) so no value can steer it; `_CONTROL_CHAR_RE.sub` + `[:120]` at
  `executor.py:659-660`. The codepoint allowlist additionally drops `"`, `\`, `{`, `}` before any of them.
- **Widened key set is still a closed, asserted enumeration:** three keys on every path (modulo finding 1's
  stale literals), `match_count` absent everywhere, `items` entries exactly the four projected keys, `spoken`
  introducing no new field and `""` on every non-`match` path so presence is not an outcome oracle; test 3
  asserts the enumeration's size as well as its members.
- **Reconciliation spot-checks, all four load-bearing rows true on `redesign`:** `preset_managed` —
  `raise ValueError("preset_managed")` for `old["preset_key"] is not None`, before the advisory lock or any
  write (`services/toolexec/custom_apis.py:599-600`). `caller_id` exclusion — `policy_resolver.py:186` joins
  `custom_api_params` on `p.source = 'caller'` only, so `source='caller_id'` params are absent from the
  model's tool schema; the value is filled from `presets.remote_party_number(...)` at `executor.py:70`.
  `_revoke_upstream`'s shared-grant probe — `oauth.py:450-463`, a `platform_conn(reason=
  "oauth-shared-grant-check")` `EXISTS` on `(provider, provider_sub, status='connected', deleted_at IS NULL,
  tenant_id <> $3)` with `$2` taken from the disconnecting row's own subject, inside the post-response
  `BackgroundTasks` callback, with `disconnect` returning a constant body — no body or latency varies with
  another tenant's state (lesson 2). `connect_api_key()` not using `resolve_api_key_input` — that helper is
  Config's (`services/config/provider_configs.py:57-71`) and its `allow_pointer_schemes` is required and
  defaultless by design, so a tenant-pasted cal.com key sealed with `encrypt_tenant_secret` can reach no
  `env:`/`k8s:` scheme (lesson 37).
- **`value_digits_only` is the shipped column, applied where the design says:** `executor.py:379`
  (`remote_party.lstrip("+") if param["value_digits_only"]`, before `value_prefix`), authored through
  `presets._caller_id(..., digits_only=True)` (`presets.py:118-123`), DDL at `database/schema.sql:988-989`
  with the `source='caller_id'` CHECK in the shared `DO $$` block. *(Naming nit: the constraint is
  `custom_api_params_value_digits_shape`, not `custom_api_params_value_digits_only_shape` as the design's
  prose has it; the design only cites it, never re-creates it, so nothing is built against the wrong name.)*
- **`apply_response_transform`'s shipped shape matches the design's delta:** `presets.py:366-378`, dispatching
  on `transform["kind"]` with `validate_response_transform` enforcing `set(transform) == _TRANSFORM_KEYS[kind]`
  — a closed set, writable only from `presets.py`. Its leading `if not isinstance(response, dict): raise
  ValueError` means a non-dict upstream body fails the step as `response_transform_failed` rather than
  returning `no_match`; either way it is fail-closed and no body fragment escapes (the test-plan line that
  expects `no_match` for a list response will need that one case written as a step failure, which is a tester
  detail, not a control gap).
- **Call-site enumeration is mechanically right, not hand-waved:** exactly two non-test `access_token_for`
  call sites (`auth_schemes.py:223`, `oauth.py:311`) and exactly one non-test `auth_schemes.apply` call site
  (`executor.py:781`) — the design's counts hold (finding 3 is about the second site's *rule*, not its count).
- **`revoke_style="path"` (HubSpot):** the refresh token in a URL path is a real exposure class, and the
  design names it rather than hiding it. Verified: `httpx`/`httpcore` are pinned to WARNING at
  `services/toolexec/__main__.py:14` and held by `tests/test_logging_config.py`; `_revoke_upstream`'s
  `except Exception` logs `oauth_revoke_failed` with `extra={"connection_id": …}` and **no** `exc_info`, so the
  `httpx.HTTPStatusError` string — which does carry the full URL — never reaches a log line; it is the only
  caller of the revoke, and the shared-grant skip runs before the branch. The token being revoked is the one
  being invalidated, so the residual window is nil (lesson 20 satisfied).
- **Tenant isolation on the new reads/writes:** `connection_api_base` and `access_token_for` both carry an
  explicit `tenant_id = $1 AND id = $2` predicate (RLS is the second, inert layer — lesson 36); the
  composite `(oauth_connection_id, tenant_id)` FK and `custom_apis_endpoint_base_shape` stop a row pointing
  at another tenant's connection (lesson 46); `tenant_id` at the composition site is the row's own
  `_resolve_tenant_uuid` value, never a request field; test 1 deletes each predicate one at a time and
  requires red (lesson 12).
- **Per-caller invariance on the new route and on DELETE:** generic fixed error bodies, `assert_tenant_access`
  awaited as the handler's first statement (lesson 38), `require_role("superadmin","admin")`, and the
  constant `{"disconnected": true}` with the shared-grant probe moved behind the response (lesson 2).
- Unchanged and re-confirmed: D1 `effective_url` host binding with `api["endpoint_url"]` deleted from
  `apply()`; `provider_host_allowed` branching on the **row's** `endpoint_base_source`; the stale-origin
  `DO UPDATE SET` nulling a vanished claim; no `auth_config` for these rows and no `*_ref` returned by any
  route (lesson 43 non-reintroduction); SSRF via `resolve_and_validate_endpoint` + `PinnedResolverTransport`
  at connect and per call; the non-PKCE replay control being the single-use, 10-minute, sha256-stored,
  `(tenant_id, user_id, provider)`-bound state row; the closed injection surface at the providers
  (`normalize_ani` → `+[0-9]{8,15}`, `parameterizedSearch` rather than concatenated SOQL); the quota-abuse
  bullet still saying honestly that there is no mitigation today; and the console nav/redirect gate with no
  `Promise.all` (lessons 21, 22).
