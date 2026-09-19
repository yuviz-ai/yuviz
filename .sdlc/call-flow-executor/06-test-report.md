# Test report

## Commands run and real results

```
POSTGRES_DSN="postgresql://chandankumar@localhost:5432/voiceai" REDIS_URL="redis://localhost:6379/0" \
  ./venv/bin/python -m pytest services/conversation/tests/test_callflow_handler.py \
  services/conversation/tests/test_callflow_runner.py services/conversation/tests/test_callflow_resolver.py \
  services/conversation/tests/test_callflow_tts_tripwire.py services/conversation/tests/test_out_responses_tripwire.py \
  services/conversation/tests/test_pipeline.py -q
```
RESULT: 155 passed (was 153 before this task's 2 additions: +1 in test_callflow_runner.py, +1 in test_out_responses_tripwire.py)

```
POSTGRES_DSN="postgresql://chandankumar@localhost:5432/voiceai" REDIS_URL="redis://localhost:6379/0" \
  ./venv/bin/python -m pytest services/vobiz/tests/test_bridge_dtmf.py services/conversation/tests/test_servicer_dtmf.py -q
```
RESULT: 6 passed (unchanged — no new tests added here; audited only)

```
POSTGRES_DSN="postgresql://chandankumar@localhost:5432/voiceai" REDIS_URL="redis://localhost:6379/0" \
  ./venv/bin/python -m pytest services/config/tests/test_call_flows.py -q
```
RESULT (after the narrowing below): **9 passed, 0 failed** (was 6 passed/1 failed before Task 2; 7 passed/2 failed immediately after the first version of the replacement tests — now all green after narrowing the assertion, not the source, per the coordinator's decision).

Individually re-confirmed green:
- `pytest services/conversation/tests/test_callflow_runner.py -k terminator_submits_below_max_digits -q` → 1 passed (Task 1's AC 11 test)
- `pytest services/conversation/tests/test_out_responses_tripwire.py -k conditional_assignment -q` → 1 passed (Task 3's `_sets_instance_attr` fix)

## New/changed tests

- `test_collect_terminator_submits_below_max_digits` (`services/conversation/tests/test_callflow_runner.py`) — pass
- `test_sets_instance_attr_rejects_a_conditional_assignment` (`services/conversation/tests/test_out_responses_tripwire.py`) — pass (new; guards a fixed helper, see Task 3)
- `test_published_route_404_is_invariant_for_tenant_admin_regardless_of_target` (`services/config/tests/test_call_flows.py`) — pass (narrowed, see Task 2 / decision below)
- `test_published_route_404_is_invariant_for_service_account_regardless_of_target` (`services/config/tests/test_call_flows.py`) — pass (narrowed, see Task 2 / decision below)
- `test_published_route_never_403s_for_either_caller_shape` (`services/config/tests/test_call_flows.py`) — pass, unchanged
- Removed: `test_published_route_404s_for_wrong_tenant_path_and_admin_jwt` (the original wrong test — see Task 2)

## Decision: the two 404-body observations are closed, not defects

The first version of the two replacement tests asserted byte-identical 404 bodies across responses that used **different caller-supplied identifiers** (different slugs for the admin test; different `call_flow_id`s for the service-account test). Both failed — not because of a hidden-state leak, but because `deps.assert_tenant_access`'s mismatch/unknown-slug 404 (`services/config/deps.py:179`, `f"tenant {tenant!r} not found"`) and `get_published_call_flow`'s own 404 (`services/config/routers/call_flows.py:101`, `f"call_flow {call_flow_id!r} not found"`) both echo back exactly what the caller typed into the URL. A caller who chose the identifier already knows it, so a body that varies only in the caller's own echoed input is not an oracle — this is the same class of over-wide assertion as the cross-principal comparison removed earlier in Task 2, just found on the identifier axis this time instead of the principal axis.

Per the coordinator/product decision: **narrowed the test, changed no source.** The codebase already carries both conventions side by side (constant-body: `test_live_calls.py:364`; echoing-body: `test_api.py:194-195,225`, `test_deps_tenant_access.py:71,83`) and `assert_tenant_access` is shared by Config/Knowledge/DID/Campaigns (lesson 9), so changing it to stop echoing would mean rewriting five existing, currently-correct assertions elsewhere — out of scope here and not done.

**Property now encoded: invariance across what the caller did NOT supply, holding the caller and the supplied identifiers fixed.**

1. `test_published_route_404_is_invariant_for_tenant_admin_regardless_of_target` — holds the tenant-B admin JWT **and** a single fixed tenant-A slug constant, and varies only the STATE of the flow behind it: a real published flow, a random/nonexistent flow id, a draft (unpublished) flow, an outbound flow, a soft-deleted flow — five different `call_flow_id`s, one fixed slug. Byte-identical, no normalisation needed, because `assert_tenant_access`'s mismatch branch fires on the slug alone and never reaches the flow lookup, so its echoed detail contains only the (fixed) slug. **Still catches:** the published route's flow lookup moving ahead of `bind_path_tenant`/`require_path_tenant_access` (the real-flow case would then leak a 200 while the others stay 404 — status divergence, not just body divergence), and `assert_tenant_access` starting to resolve the flow itself and revealing its existence through a distinct branch (a 403 for one flow-state and a 404 for another, or two differently-shaped 404s). **Does not, and is not meant to, catch:** a body that varies only because the caller supplied a different slug — that axis is deliberately excluded now.
2. `test_published_route_404_is_invariant_for_service_account_regardless_of_target` — holds the tenant slug fixed at `test_tenant`'s own real slug (the platform-scoped service account isn't gated by that slug anyway — `is_platform_scoped()` short-circuits `assert_tenant_access`). For unpublished/outbound/soft-deleted, one flow row's id is held fixed and its DB state is mutated in place between each request (`graph = NULL`, then restored with `direction = 'outbound'`, then `deleted_at = now()`), with an explicit `cache.invalidate()` between each mutation so the cache-aside read actually re-hits Postgres — those three compare byte-for-byte with no normalisation. The fourth sub-case (unknown id) necessarily uses a *different* literal id — there is no DB state that makes an id "not exist" while keeping it the same id — so all four are additionally compared after a shared helper (`_detail_with_id_normalised`) strips any UUID-shaped substring from the `detail` text, per the decision that a caller-supplied id is not part of the property under test. **Still catches:** `get_published_for_runtime()` returning a differently-shaped 404 for one reason than another (e.g. a distinct detail template for "wrong direction" vs "no such id" — the normalisation only strips ids, not wording), or the unknown-id branch changing the response's status/shape. **Does not catch:** a difference that exists solely in which id was echoed.
3. `test_published_route_never_403s_for_either_caller_shape` — left unchanged, still passing.

## Task 1 — AC 11 gap

Added `test_collect_terminator_submits_below_max_digits` to `test_callflow_runner.py`. Uses `min_digits=2, max_digits=4, terminator="#"`, presses `"1"`, `"2"` (buffer at 2, below max_digits=4, no action), then `"#"`. Only the terminator branch (`len(buffer) >= min_digits`) can cause this submission — `max_digits` is never reached. Asserts `Store("pin", "12", False)` is emitted and `runner.node.id == "next"`.

What would make it fail: if the terminator comparison's `min_digits` check were dropped, swapped for a `max_digits` check, or `"#"` were treated as an ordinary buffered digit instead of triggering `_submit_collect()`, the assertion on `Store`/`node.id`/`variables["pin"]` would fail — the runner would still be listening (buffer length 3) instead of having advanced to `"next"`. The pre-existing `test_collect_terminator_at_min_digits_stores_and_excludes_terminator` (`min_digits == max_digits == 4`) cannot distinguish this because its 4th digit alone already hits `max_digits`; the terminator press there is incidental, not causal.

## Task 2 — the one wrong test

Replaced `test_published_route_404s_for_wrong_tenant_path_and_admin_jwt` (compared a service account's 404 to a *different* admin's 404 for the *same* URL — a comparison no attacker can make, per lesson 2 sharpened) with three tests in `services/config/tests/test_call_flows.py`. The first draft of two of those three asserted byte-identical bodies across *different caller-chosen identifiers* and failed for that reason; per the coordinator's decision (see "Decision" above) they were narrowed to compare across the target's hidden STATE instead, holding the caller-supplied identifier fixed. Final state:

1. **`test_published_route_404_is_invariant_for_tenant_admin_regardless_of_target`** — pass.
2. **`test_published_route_404_is_invariant_for_service_account_regardless_of_target`** — pass.
3. **`test_published_route_never_403s_for_either_caller_shape`** — the one cross-caller property actually worth checking: both a tenant admin and the service account get 404, never 403, for a target that isn't theirs. Unchanged, pass.

## Task 3 — audit of existing tests for "cannot fail"

Read every test in `test_callflow_runner.py`, `test_callflow_handler.py`, `test_callflow_resolver.py`, `test_out_responses_tripwire.py`, `test_callflow_tts_tripwire.py`, `test_bridge_dtmf.py`, `test_servicer_dtmf.py`, `test_call_flows.py`.

Found and fixed one: **`_sets_instance_attr()` in `test_out_responses_tripwire.py`**. It used `ast.walk(init)` over the *entire* `__init__` body, so a `self.out_responses = ...` nested inside an `if`/`try`/`for` would satisfy it — but the assertion message it backs claims "unconditional self.out_responses = ...". A handler that only conditionally sets the attribute would pass this tripwire and still raise `AttributeError` on the construction path that skips the assignment — exactly the failure mode this test exists to catch. Fixed the helper to walk only `init.body` (top-level statements), and added `test_sets_instance_attr_rejects_a_conditional_assignment`, a sanity test on the helper itself (mirroring the existing `test_every_implementer_module_defines_on_speech_ended_and_a_handler_class` sanity check pattern in the same file): it asserts the helper returns `False` for a synthetic class whose `__init__` sets the attribute only inside an `if`, and `True` for one that sets it unconditionally. Before the fix, both would have returned `True`.
- **Before**: "asserts self.out_responses is assigned somewhere in `__init__`."
- **After**: "asserts self.out_responses is assigned as one of `__init__`'s own top-level statements — i.e. unconditionally, on every construction path."
- Checked against `CallFlowConversationHandler.__init__` (`handler.py:103`): the real assignment there is already a top-level statement, so this fix changes no other test's result — it only tightens what future handlers must satisfy.

Everything else read as able to fail for the stated reason:
- `test_callflow_runner.py`: every test asserts on the emitted `Action`/`node.id`/`variables`, with explicit negative controls (e.g. `test_menu_unbranched_timeout_replays_then_exhausts`'s "one event shorter still Listens"). The 3a/3b voice/sensitivity tests assert on the emitted action rather than the runner's internal state, matching lesson 12.
- `test_callflow_tts_tripwire.py`: has its own `test_tripwire_itself_can_fail`, proving the regex catches a graph-resident read.
- `test_out_responses_tripwire.py`'s enumeration itself has `test_every_implementer_module_defines_on_speech_ended_and_a_handler_class` as a non-vacuousness check.
- `test_callflow_handler.py`'s race tests (14a and the arm-generation variant) drive the real `_driver_task`/`_events` queue rather than calling `on_timeout()`/`on_digit()` directly, so a regression in the generation-based staleness check would actually be exercised.
- `test_bridge_dtmf.py`/`test_servicer_dtmf.py`: assert on the queue/stream contents, not on mocks of the code under test; the `caplog` digit-leak assertions have a real record to search (`"7" not in record.getMessage()`).
- `test_call_flows.py`'s other (unmodified) tests plant same-id-shaped rows in a second tenant to prove the query is actually tenant-scoped (lesson 29/30 shape: reproduce the leak, don't just assert its absence) rather than asserting an empty result that could also be vacuous.

**Two soft spots named in the task, checked, not additionally exercised (already-known, unfixed by design):**
- `_is_protocol_shaped()` keys on the first two parameter names (`session_id`, `audio`). A handler that renamed those parameters while keeping `on_speech_ended` would silently drop out of the grep+AST enumeration rather than fail it. No new test added for this — it is a known limitation stated in the module's own docstring, not something discoverable by a fixture without inventing a hypothetical third handler; flagged here as residual risk per the task's instruction, not fixed.
- `_sets_instance_attr()`'s conditional-assignment gap — fixed above (this was the one instance where the gap was concretely demonstrable and cheap to close, unlike the parameter-rename case).

## Task 4 — coverage against the PRD's 33 acceptance criteria

**Covered by an executing test:**
- AC 2 (Echo path unaffected) — `test_echo_integration.py` drives a real `EchoConversationHandler` through `Converse` end to end.
- AC 3 partial / AC 4 / AC 5 / AC 6 — `test_graph_for_flow_caches_and_reparses_on_version_bump`, `test_two_runners_over_one_cached_graph_advance_independently`, `test_publish_invalidates_cache_and_bumps_config_version`.
- AC 8 — `_set_voice(None)` short-circuits to `self._default_tts` without calling `voice_for`; exercised implicitly by every `_handler()`-based test that asserts on spoken `tts_payloads` (e.g. `test_dial_node_emits_cold_transfer_request`), plus explicitly by `test_setvoice_carries_constructor_value`/`test_setvoice_carries_only_constructor_value_never_graph_value` at the runner level.
- AC 9, 10, 11 (now), 12, 13, 14 — `test_callflow_runner.py`'s menu/collect/dial/hangup tests.
- AC 15, 17, 20 — `test_menu_explicit_timeout_edge_taken_with_retries_untouched` + the invalid-fallback test.
- AC 16, 18, 19 — `test_menu_unbranched_timeout_replays_then_exhausts`, `test_menu_unmatched_digit_replays_via_invalid_fallback`.
- AC 21, 22, 23 — `test_collect_terminator_below_min_digits_replays_then_exhausts`, `test_collect_timeout_below_min_digits_replays_then_exhausts`.
- AC 24 — `test_menu_digit_handoff_streams_greeting_with_seeded_variables` (handler-level, real `PipelineConversationHandler` delegate).
- AC 25 (config-plane half), AC 26 (config-plane half) — `test_payload_resolves_same_tenant_active_agent_and_omits_cross_tenant_or_inactive` (payload omits cross-tenant/inactive agent ids).
- AC 27, 28 — `test_provider_miss_returns_none`, `test_invalid_graph_returns_none_and_does_not_raise`, `test_arbitrary_provider_exception_returns_none_and_does_not_propagate`.
- AC 29 — `test_handler_exception_mid_node_yields_clean_end_call`.
- AC 30 — `test_collect_overwrites_seeded_variable_last_write_wins`.
- AC 32 — `test_published_route_returns_payload_for_service_account`/the wrong-tenant tests exercise `bind_path_tenant`+RLS-target-before-read structurally, though see the RLS caveat below.

**Closed by decision, not a defect** (recorded so a future reviewer does not re-raise it): the two 404 responses in Task 2 echo the caller's own supplied identifier (tenant slug / call_flow_id) into the `detail` text. This is not an information oracle — the caller already knows what it typed — so a body that differs only because two different self-chosen identifiers were used is not a property either test should assert. Both `deps.assert_tenant_access` (`deps.py:179`) and `get_published_call_flow` (`routers/call_flows.py:101`) keep this convention deliberately; the codebase already has both an echoing convention (`test_api.py:194-195,225`, `test_deps_tenant_access.py:71,83`) and a constant-body convention (`test_live_calls.py:364`) coexisting, and `assert_tenant_access` is shared by Config/Knowledge/DID/Campaigns (lesson 9), so unifying them was explicitly decided against for this task.

**Covered only by a test that cannot fire, or only partially:**
- AC 3 — the design's own test-plan item 10 calls for "assert on a connection/query counter, not on wall time." No such counter assertion exists; the Config-side cache-aside behavior is only checked functionally (cache invalidated on publish/delete, updated version visible after publish), not "Postgres read at most once across N concurrent sessions."
- AC 31, 32 — three cross-tenant assertions in `test_call_flows.py` depend on RLS. Per the task's brief, **two of the three now pass under the local superuser role on the strength of the new explicit `tenant_id` predicates alone** (lesson 36) — RLS itself is unexercised on this path because the local role is `BYPASSRLS` and `yuviz_app`'s password is operator-shell-only. I did not attempt to reset that password or otherwise work around the permission system; this gap is structural to the local test environment, not something a new test here can close.
- AC 25/26, handler-side half — `CallFlowConversationHandler._handoff_to()`'s two degradation branches (agent id absent from `agent_slugs`; `handoff()` returning `None`) have **no test at all**, unit or integration. Inspecting the source while looking for this coverage surfaced a related, already-known-and-listed finding I was explicitly told not to fix: neither branch sets `self._ended = True` before putting the `end_call=True` response, unlike the `Hangup`/`Dial` branches a few lines above — so a stale queued event after a degraded handoff is not guaranteed to be dropped the way it is after an ordinary hangup. Reporting this as a source defect (the `_handoff_to`/`_ended` omission named in this task's own do-not-fix list), not patching it.

**Not covered at all, and not automatable here:**
- AC 18's live-SIP behaviours (T18) — cannot be automated at all per the design's own "Open questions" section; webcall DTMF is deferred, so there is no browser path. The design correctly calls this out as requiring a real vobiz call for first verification.
- AC 1 / AC 2 (pipeline half) — the actual `__main__.py` `handler_factory` branch (`if not runtime_config.agent.call_flow_id: return await _build_pipeline_handler(...)` vs. building `CallFlowRunner`/`CallFlowConversationHandler`) is never exercised end-to-end by any test. `CallFlowRunner`/`CallFlowConversationHandler` are always constructed directly in the unit/handler tests, bypassing this selection line; and unlike the Echo path (driven through a real `Converse` stream in `test_echo_integration.py`), no test drives a real `PipelineConversationHandler` through the actual servicer loop the way the design's own test-plan item 11 asked for ("it drives a real `PipelineConversationHandler` and a real `EchoConversationHandler` through `Converse` to completion"). Only Echo got that treatment; Pipeline's `out_responses`/`on_dtmf` safety is covered structurally (the AST tripwire) but not by a live `AttributeError`-can-abort-the-stream regression test. This is the one gap I'd flag as worth a follow-up test before it's considered closed, though it is outside this task's explicit scope (Tasks 1–3) so I did not add one.
- AC 7, AC 33 — both explicitly "unchanged from today's existing behavior" / structurally guaranteed by call-scoped state rather than new logic; no new test exists for either and the PRD itself scopes them as pre-existing, not this feature's to prove.

## Genuine gaps still open (unchanged by the narrowing above — recorded, not fixed)
- AC 3 — no connection/query-counter assertion proving "Postgres read at most once" across concurrent sessions.
- AC 1 / AC 2 (Pipeline half) — `__main__.py`'s `handler_factory` branch selection, and a real `PipelineConversationHandler` driven through the actual servicer `Converse` loop, are both untested; only Echo gets the full-stream treatment.
- AC 25/26 (handler-side half) — `CallFlowConversationHandler._handoff_to()`'s two degradation branches (missing `agent_slugs` key; `handoff()` returning `None`) have zero test coverage, and neither sets `self._ended = True` (the already-listed, not-to-be-fixed `_handoff_to`/`_ended` omission).
- `_is_protocol_shaped()` keying on parameter names (`session_id`, `audio`) rather than a more robust signature check — a renamed-parameter handler would silently drop out of the enumeration.
- AC 31/32 — RLS itself is unexercised on this path under the local superuser/`BYPASSRLS` role; two of the three cross-tenant assertions pass on the explicit `tenant_id` predicates alone (lesson 36). Did not touch `yuviz_app`'s password or the permission system.

## Files touched
- `/Users/chandankumar/yuviz/services/conversation/tests/test_callflow_runner.py` — added `test_collect_terminator_submits_below_max_digits` (Task 1).
- `/Users/chandankumar/yuviz/services/conversation/tests/test_out_responses_tripwire.py` — fixed `_sets_instance_attr()` to require an unconditional (top-level) assignment; added `test_sets_instance_attr_rejects_a_conditional_assignment` (Task 3).
- `/Users/chandankumar/yuviz/services/config/tests/test_call_flows.py` — removed the wrong test, added three per-caller-invariance tests, then narrowed two of them (holding the caller-supplied identifier fixed and varying only hidden state) per the coordinator's decision (Task 2). Added `import json`, `import re` for the new helpers.

No source files were modified at any point. No test assertion was weakened to force a pass — the two that initially failed were narrowed to encode a different, correct property, not loosened on the same property.
