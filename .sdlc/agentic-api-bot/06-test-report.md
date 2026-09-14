# Test report (round 2)

COMMAND: `POSTGRES_DSN="postgresql://chandankumar@localhost:5432/voiceai" /Users/chandankumar/yuviz/venv/bin/python3 -m pytest services/toolexec/tests/ -q`
RESULT: 113 passed, 1 failed (114 total; 113 were passing before this round's 1 new test)

Worktree: `/Users/chandankumar/yuviz/.claude/worktrees/agent-afa560ddeac7c1ed8`, branch
`feature/agentic-api-bot`, on top of `a4f1ad0` + the implementer's round-2 fix diff
(`services/toolexec/{executor,custom_apis,auth_schemes}.py` and their test files).

## Round-1 findings: verified fixed

All three tests the implementer was asked to fix now pass, confirmed by mutation (each reverted
to its pre-fix behaviour and re-run — every one failed for the exact reason its docstring
describes, then restored):

- `test_undecodable_param_row_refuses_the_call_instead_of_dropping_the_field` — pass. Mutation:
  removed the `poisoned.add(...)` line from the param-row `except RuntimeError` handler (restoring
  silent-skip) → test failed with `assert calls == []` false (outbound POST fired with `amount`
  missing). Restored → green.
- `test_malformed_upstream_row_in_chain_is_reported_not_a_raw_exception` — pass. Mutation:
  restored the original (pre-fix) `_build_api_tree` body wholesale from commit `a4f1ad0` (no
  `poisoned` set, no `_UnbuildableApi`) → both this test AND
  `test_one_malformed_custom_api_row_does_not_break_other_apis_chain` AND
  `test_undecodable_param_row_refuses_the_call_instead_of_dropping_the_field` failed, with a bare
  `RuntimeError`/`KeyError` escaping `execute_chain` — exactly the review finding. Restored → all
  three green.
- `test_sensitive_literal_param_value_not_readable_by_every_tenant_role` — pass. Mutation: reverted
  `get_custom_api` to call `_decode_param_row` directly instead of through
  `_redact_sensitive_literals` → `got_param["literal_value"] == secret_value` (test failed on the
  single-item read assertion). Restored → green.

**Ordering claim for finding 1/2 verified by reading, not just running:** in
`services/toolexec/executor.py::execute_chain`, `_build_api_tree` (which raises
`_UnbuildableApi`) is called and its exception handled *before* `admission.acquire()` and before
`_claim_run()` (the only write to `api_chain_runs`). So an undecodable row genuinely takes no
admission slot, writes no claim row, makes no HTTP call, and leaves no `api_chain_runs` row in a
non-terminal state — there is no row to leave non-terminal, because none is ever inserted on this
path.

**Isolation property confirmed non-vacuous:**
`test_one_malformed_custom_api_row_does_not_break_other_apis_chain` is still green, and mutation
(reverting `_build_api_tree` to the pre-fix version) makes it fail too — so it is exercising a real
path, not passing by construction.

## New defect found this round: redacted literal survives the list→edit→PATCH round-trip

Per the task's specific probe ("is the redaction lossy in a way that breaks the edit form"): the
real admin-ui form does exactly this round-trip —
`admin-ui/components/CustomApisPanel.tsx:273` seeds the edit form with `api.params.map(p => ({...p}))`
straight from the list response, and `:321` PATCHes back `params: form.params` unchanged for any
untouched param. Since `list_custom_apis`/`get_custom_api` now serve `"[redacted]"` in place of a
`sensitive=true` literal's real value, and `update_custom_api` writes back whatever `params` array
it is given verbatim (`_replace_params`), saving the form after editing an unrelated field
overwrites the real secret in the database with the literal string `"[redacted]"` — permanent data
destruction of exactly the class defect 3 was.

- `test_sensitive_literal_survives_list_edit_save_round_trip`
  (`services/toolexec/tests/test_custom_apis.py`) — creates a `sensitive=true` literal param,
  reads it back via `list_custom_apis` (confirms it is redacted, as intended), calls
  `update_custom_api(..., params=listed_api["params"])` changing only `description` (mirroring the
  UI's save-with-unrelated-edit path), then re-reads the raw DB row directly — FAIL.
  **Mutation proof:** this test's failure IS the reproduction; running it against the current diff
  fails with `AssertionError: real secret was overwritten with '[redacted]' via the redacted
  round-trip` (`stored_value == '[redacted]'`, not `sk-live-do-not-leak-me`). No source change
  needed to trigger it — left failing per instructions, this is the implementer's fix to make.

Other probed surfaces, checked and clean (no new test needed since the code structurally cannot
leak here):
- **Executor's own `_decode_param_row` calls in `_build_api_tree`** — never routed through
  `_redact_sensitive_literals`; confirmed by reading (only `get_custom_api`/`list_custom_apis` call
  the redaction helper), and every green outbound-call test in `test_chain_execution.py` still
  dispatches the real literal value — the two paths are consistent (real value used to call, only
  the read surface redacts).
- **`GET /calls/{session_id}/chain-runs`** (`agent_apis._authorize_chain_runs` →
  `executor._persist_step`'s `arguments_redacted`) — redacted by `sensitive_param_names` derived
  from `params_by_api` at `executor.py:776/797`, using the real resolved value, before persistence.
  A literal source's value never appears un-redacted here.
- **`argument_sources`** — only ever holds the string label `"literal"`/`"caller"`/
  `"<upstream>:<path>"` (`executor.py:390/397/407`), never the value itself.
- **Agent-enablement list route** (`agent_apis.list_for_agent` → `SELECT aca.*, ca.name,
  ca.chain_levels`) — no params/literal_value column in the query at all; nothing to leak.

## Full result table

| Test | Protects | Result |
|---|---|---|
| `test_undecodable_param_row_refuses_the_call_instead_of_dropping_the_field` | undecodable param row refuses the call before admission/claim/outbound call | PASS (mutation-confirmed) |
| `test_malformed_upstream_row_in_chain_is_reported_not_a_raw_exception` | malformed upstream row reported as `chain_status="failed"`, no bare exception | PASS (mutation-confirmed) |
| `test_one_malformed_custom_api_row_does_not_break_other_apis_chain` | poisoning one API must not break an unrelated chain | PASS (mutation-confirmed, non-vacuous) |
| `test_sensitive_literal_param_value_not_readable_by_every_tenant_role` | sensitive literal not readable on list or single-item read | PASS (mutation-confirmed) |
| `test_sensitive_literal_survives_list_edit_save_round_trip` | redacted placeholder must not overwrite the real secret on save | FAIL (new, real defect) |

FAILURES:
- `services/toolexec/tests/test_custom_apis.py::test_sensitive_literal_survives_list_edit_save_round_trip` — a `sensitive=true` literal param's real value is permanently overwritten with the literal string `"[redacted]"` when the edit form (or any caller) PATCHes back the redacted `params` array `list_custom_apis`/`get_custom_api` returned. Reproduced end-to-end including the real admin-ui code path (`CustomApisPanel.tsx:273,321`). Implementer fix needed: `update_custom_api`/`_replace_params` must reject or ignore a `literal_value` equal to the redaction sentinel for a `sensitive` param whose value hasn't actually changed (e.g. compare against the stored value, or require an explicit "unchanged" marker distinct from the real placeholder string), not accept it as a legitimate new literal.

UNCOVERED:
- **`body_style:"form"`** — still genuinely untested; not added this round (kept to one new test
  per the round's actual defect-probing scope and the "fewer is better" instruction). Real gap.
- **`PUT`/`DELETE` methods** — still genuinely untested for the same reason. Real gap.
- **Per-tenant advisory-lock concurrency** (`pg_advisory_xact_lock` under two overlapping
  `create_custom_api`/`update_custom_api` writes) — still genuinely untested; needs a two-connection
  harness this round's budget did not cover. Real gap, named explicitly, not silently dropped.

---

## Post-fix verification (added by the ship stage, after the tester's round 2)

The `RESULT` line above is the tester's round-2 snapshot, taken **before** the implementer fixed
the defect that round found. It is preserved as written rather than edited, so the sequence stays
legible. The failing test at that point was
`test_sensitive_literal_survives_list_edit_save_round_trip`.

That defect was then fixed at the source (`custom_apis.py`: `_redact_sensitive_literals` now
redacts to `None` rather than a placeholder string, and `_merge_sensitive_literals` preserves the
stored value for any incoming sensitive literal that arrives `None`, before validation and before
the write). Two regression tests were added alongside it —
`test_sensitive_literal_can_be_deliberately_changed` and
`test_sensitive_literal_registry_redaction_never_reaches_outbound_call`.

Re-run at ship time, by the coordinator rather than the tester:

    POSTGRES_DSN="postgresql://chandankumar@localhost:5432/voiceai" \
      venv/bin/python3 -m pytest services/toolexec/tests/ services/conversation/tests/ -q
    => 685 passed, 0 failed

    POSTGRES_DSN="postgresql://chandankumar@localhost:5432/voiceai" \
      venv/bin/python3 -m pytest services/config/tests/ -q
    => 332 passed, 0 failed

Still uncovered, unchanged from round 2: `body_style:"form"`, `PUT`/`DELETE` methods, and the
per-tenant advisory-lock concurrency case.
