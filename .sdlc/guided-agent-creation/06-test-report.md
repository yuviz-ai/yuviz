# Test report: guided-agent-creation

Date: 2026-10-03. Tree: feature/guided-agent-creation at 4b36dc0 plus uncommitted test-only additions (listed below). No source file was changed.

## Commands and results

1. `POSTGRES_DSN=postgresql://chandankumar@localhost:5432/voiceai_gac_test timeout 1500 pytest libs/config_sdk/tests services/config/tests services/conversation/tests services/webcall/tests -q -rfE` (Python /Users/chandankumar/yuviz/venv/bin/python, nothing ignored)
   RESULT: 1638 passed, 0 failed, 0 errors (509 s). No DROP DATABASE hang.
2. `cd admin-ui && npx tsc --noEmit` — exit 0, no errors.
3. `cd admin-ui && E2E_EMAIL=e2e-superadmin@example.com E2E_PASSWORD='***' npx playwright test` — 9 passed, 0 failed (reused the running :8010 and :3010; it did not start a server).

## Tests added in this stage (Playwright, in `admin-ui/e2e/easy-create.spec.ts`)
Seeds two more throwaway accounts per run (C: AI service only; D: nothing).
- a tenant missing a setup sees the plain message and link, and no create request is sent — criteria 4 (UI half) and 10 — pass. Asserts the chat job stays enabled, the voice job is disabled, "Set up speech recognition to continue" and "Connect an AI service to continue" show, the link goes to /ai-voice, no job is selectable for the empty account, the banned-word scan holds, and no POST to /agents is sent.
- business facts over 1,000 characters show an inline error and send no create request — criterion 8 (facts half) — pass.
- Not seen red: the production build on :3010 cannot be mutated without a rebuild, and the user is using it. Both assertions can fail by construction (an enabled button, a missing message, or a POST all fail them), but I did not plant a regression.

Other uncommitted test-only additions already in the working tree before this stage (found in `git diff`; not mine): `test_an_advanced_create_stays_active_with_no_template` and `test_a_superadmin_acts_only_inside_the_path_tenant` in `services/config/tests/test_guided_agent_creation.py`, and `test_revise_instructs_the_model_to_generalise_rather_than_copy_caller_details` in `services/config/tests/test_prompt_rules.py`. They are included in the 1638. They map to criteria 1/22/23, 52 and 37.

## Criterion to test map
Abbreviations: GAC = services/config/tests/test_guided_agent_creation.py; TPR = test_prompt_rules.py; TAT = test_agent_templates.py; TAPR = test_agent_prompt_revision.py; TATC = test_agent_test_chat.py; TCRED = libs/config_sdk/tests/test_test_credentials.py; CA = libs/config_sdk/tests/test_cache_aside.py; TS = conversation/tests/test_test_sessions.py; SAU = conversation/tests/test_servicer_agent_unavailable.py; FF = webcall/tests/test_first_frame.py; E2E = admin-ui/e2e/easy-create.spec.ts.

| # | Covered by |
|---|---|
| 1 | E2E "Advanced opens the existing wizard with its six titles" and the Easy-default open in every E2E test; GAC `test_an_advanced_create_stays_active_with_no_template` (Advanced create is unchanged and active). The wizard's request body is not asserted field by field. |
| 2 | E2E "the six titles appear in order" (hardcoded list, seen red); TAT `test_easy_copy_has_the_messages_the_design_fixes_verbatim` |
| 3 | TAT `test_catalog_shape`, `test_needs_by_channel` |
| 4 | Server: GAC `test_400_creates_nothing`, `test_a_chat_job_refuses_voice_configs`. UI disabled job: new E2E missing-setup test (this stage). |
| 5 | TAT `test_public_catalog_exposes_only_display_fields`, `test_handoff_text_is_in_rendered_guardrails`; E2E waits for "What it does" and scans it. The three blocks are not individually asserted in the DOM. |
| 6 | E2E `controls()` assertions at every step ({1,1,1} and {2,2,2}) |
| 7 | TAT `test_no_banned_word_in_catalog_display_fields`, `test_no_banned_word_in_easy_copy`, `test_e2e_banned_list_matches`; E2E `scan()` on every step and state including 429/502 and not-fixable |
| 8 | Name and business-name errors: E2E journey. Facts over 1,000: new E2E test (this stage). Server bounds: TAT `test_length_bounds`, `test_length_bound_edges_accepted`. "No create request" is asserted for facts only. |
| 9 | GAC `test_creates_an_inactive_templated_agent_with_rendered_text`; TAT `test_template_placeholders_are_filled` |
| 10 | New E2E missing-setup test (this stage) |
| 11 | E2E journey ({1,1,1} creates with no question) |
| 12 | Dropdown set and Continue gating: E2E "two of each setup". UNCOVERED: preselect to the newest agent's voice (see below). |
| 13 | TAT `test_hostile_names_render_literally`, `test_double_braces_rejected_in_every_text_field`, `test_runtime_render_leaves_rendered_text_untouched`; GAC `test_double_braces_are_refused`; E2E double-brace message |
| 14 | TAT `test_facts_with_bare_heading_lines_do_not_move_the_headings`, `test_substituted_text_is_never_rescanned`; TPR `test_bare_heading_line_inside_facts_is_content` |
| 15 | TAT `test_template_structure`; TPR `test_check_accepts_complete_prompt`, `test_check_rejects_missing_and_misordered_headings` |
| 16 | TAT `test_template_structure`, `test_voice_jobs_do_not_carry_the_chat_speech_block_and_the_reverse`; TPR `test_enforce_inserts_missing_blocks_at_end_of_own_section`, `test_generate_restores_dropped_blocks` |
| 17 | TAT `test_handoff_text_is_in_rendered_guardrails`, `test_template_structure`; TPR `test_enforce_*` |
| 18 | TAT `test_template_structure`, `test_job_sections_are_pairwise_distinct`; TPR `test_check_requires_three_job_lines` |
| 19 | TPR `test_check_rejects_missing_and_misordered_headings`, `test_check_requires_three_job_lines`, `test_enforce_inserts_after_existing_section_lines_and_before_blank_gap`, `test_enforce_raises_on_bad_structure`; TAT `test_enforce_restores_a_removed_block_inside_its_own_section`, `test_enforce_raises_when_a_heading_is_missing_or_job_is_short` |
| 20 | TPR `test_generate_restores_dropped_blocks` (a model that drops the blocks is repaired). No test feeds an "ignore all previous rules" description; the guarantee is server-side post-processing of the output. Treated as covered by 16/17/19. |
| 21 | GAC `test_returns_before_after_and_hash_and_persists_nothing` (revise); TAPR/GAC accept tests. Advanced generate persists nothing by the existing design. |
| 22 | GAC `test_creates_an_inactive_templated_agent_with_rendered_text`, `test_400_creates_nothing`, `test_an_advanced_create_stays_active_with_no_template`; TAT `test_get_template_needs_exact_version` |
| 23 | GAC `test_creates_an_inactive_templated_agent_with_rendered_text`, `test_an_advanced_create_stays_active_with_no_template` |
| 24 | TS `test_inactive_agent_with_voice_credential_gets_its_prompt_and_voice` (resolution only); CA `test_runtime_config_include_inactive_*`. No live voice session. See "Not exercised". |
| 25 | TS `test_bad_credential_is_refused_without_legacy_fallback`, `test_credential_for_a_missing_agent_is_refused`; SAU `test_agent_unavailable_yields_one_fatal_error_and_ends` |
| 26 | TCRED (single use, channel, TTL, hashed key); GAC `test_a_tenant_b_credential_is_a_missing_session_on_tenant_a`, `test_expired_voice_and_foreign_credentials_all_match_a_missing_session`, `test_voice_mint_shape_and_a_mismatched_channel`; TATC `test_credential_for_another_session_agent_or_channel_is_session_not_found`; TS `test_no_credential_inactive_and_missing_agent_reach_the_same_legacy_path`; FF first-frame tests |
| 27 | CA `test_runtime_config_inactive_agent_default_is_none_even_when_complete`; TS `test_non_test_direction_ignores_the_credential` |
| 28 | TATC `test_chat_mint_writes_a_born_ended_test_call_and_the_greeting_row`, `test_a_turn_stores_turn_one_and_the_model_sees_the_greeting_then_a_user_message`, `test_load_test_transcript_returns_caller_agent_tuples_in_turn_order`; GAC `test_one_turn_writes_a_test_call_with_turns_zero_and_one`; E2E chat journey |
| 29 | E2E voice test (Start talking / Stop, transcript, status words, scan). The existing Advanced test page is not asserted unchanged. |
| 30 | E2E journey (Put it to work, confirmation title, link); GAC `test_accept_and_activate_reach_conversations_config_provider`. "Never activated by any other action" is not asserted separately. |
| 31 | No automated test. Manual run (approvals.md:192) clicked Undo on the existing agent page and read the DB, which exercises that page; it did not edit every field. |
| 32 | E2E voice test (Fix opens only with a session id); E2E chat journey. The 1,000-character `maxLength` is not asserted. |
| 33 | E2E voice test (`testFirst` shown, no textarea, no revise request while gated). The chat "no transcript" variant is not separately tested. |
| 34 | GAC `test_returns_before_after_and_hash_and_persists_nothing`; E2E journey (before/after labels visible) |
| 35 | TPR `test_token_class_positive_and_exempt`, `test_phone_exempt_compares_digit_strings_not_formatting`, `test_short_digit_runs_are_not_data`, `test_caller_line_window_of_40_chars`, `test_caller_data_in_revision_raises_customer_data_error` |
| 36 | TAT `test_easy_copy_has_the_messages_the_design_fixes_verbatim` (message text); `easyErrorText` maps `customer_data`; GAC `test_the_model_error_tokens` (422 `customer_data`). The DOM rendering, kept problem text and "does not echo the token" are not driven in a browser. |
| 37 | TPR `test_revise_instructs_the_model_to_generalise_rather_than_copy_caller_details` |
| 38 | TAPR `test_accept_on_stale_base_is_none_and_changes_nothing`, `test_two_concurrent_accepts_on_one_base_give_one_winner`; GAC `test_a_stale_base_hash_is_a_409_and_changes_nothing`, `test_two_concurrent_accepts_on_one_base_have_one_winner` |
| 39 | GAC `test_an_unacceptable_proposal_is_a_400`, `test_the_accept_exemption_follows_the_problem_text`; TAPR `test_accept_brace_rule_refuses_only_added_template_braces` |
| 40 | TAPR `test_accept_accept_undo_restores_prior_prompt_and_second_undo_is_none`, `test_accept_and_undo_mirror_the_prompt_into_the_graphs`; GAC `test_accept_accept_undo_restores_the_middle_prompt_and_a_second_undo_is_409` |
| 41 | E2E journey (Discard, then no Undo button). The "agent and slot unchanged" DB read is manual (approvals.md:192 read the DB after the proposal). |
| 42 | TAPR `test_can_undo_and_prompt_fixable_follow_the_stored_row`; GAC accept/undo tests; E2E journey (Undo) |
| 43 | TAPR `test_accept_accept_undo_restores_prior_prompt_and_second_undo_is_none`; GAC `test_accept_accept_undo_restores_the_middle_prompt_and_a_second_undo_is_409` |
| 44 | TAPR `test_accept_then_hand_patch_then_undo_is_none_and_can_undo_false`; GAC `test_a_hand_edit_after_accept_makes_undo_a_409_and_clears_can_undo`; E2E not-fixable |
| 45 | GAC `test_the_model_error_tokens` (502 `ai_unavailable`, agent unchanged); E2E revise 502 shows Easy text. Retry after failure is not driven in a browser. |
| 46 | GAC `test_every_new_route_is_401_unauthenticated_and_403_for_a_viewer` |
| 47 | GAC `test_foreign_llm_config_id_matches_a_random_one_on_generate_and_revise`, `test_chat_turn_with_a_foreign_or_dangling_llm_config_is_one_404`; TPR `test_unknown_foreign_and_malformed_ids_raise_identical_lookup_error`; TATC `test_foreign_and_random_llm_config_ids_raise_the_same_error_with_no_model_call` |
| 48 | GAC `test_revise_and_accept_never_read_another_tenants_session`, `test_foreign_agent_id_matches_a_random_one_on_every_new_agent_route`, `test_foreign_provider_id_matches_a_random_one_and_changes_nothing`; TATC `test_load_test_transcript_ignores_another_tenants_calls_row` |
| 49 | TATC `test_load_test_transcript_ignores_another_tenants_calls_row`; GAC `test_revise_and_accept_never_read_another_tenants_session`; TPR own-config tests (config is the tenant's own, resolved by tenant) |
| 50 | GAC `test_no_log_record_holds_a_facts_problem_message_or_credential_sentinel`; TPR `test_vendor_error_body_is_not_logged`; SAU `test_open_log_never_contains_credential`; TS `test_repr_of_session_context_omits_the_credential`; TAPR `test_slot_never_reaches_audit_log_or_workflow_versions` |
| 51 | TPR `test_revise_instructs_the_model_to_generalise_rather_than_copy_caller_details` plus the Accept gate (39). Output is always a draft (21/34). No test plants "reveal the API key" in a transcript and checks the output for a key, because the model is mocked. |
| 52 | GAC `test_a_superadmin_acts_only_inside_the_path_tenant` |
| 53 | GAC `test_every_new_route_is_401_unauthenticated_and_403_for_a_viewer`; `test_every_agents_router_route_runs_both_path_tenant_dependencies`, `test_every_guard_call_is_awaited` |
| 54 | E2E chat journey (six Easy steps through the confirmation, Easy text only) and the manual run in approvals.md:192 (chat path, DB read-back after every write). Voice half and real LLM: see below. |

## Uncovered criteria and whether the T18 manual run covers them
- Criterion 12, preselect to the newest agent's voice only if it still exists: NOT covered by any automated test and NOT covered by the T18 manual run (only the dropdown set and the Continue gating are tested). The code is at `admin-ui/components/EasyAgentFlow.tsx:118-122` (`recentAgent`). Closing it needs a seeded agent plus a {2,2,2} account in the Playwright spec; I did not add it because the test needs an agent created with a chosen voice config through the API and a fixture change in `beforeAll`. Recommend a follow-up.
- Criterion 31 (every field editable on the agent page): no assertion. T18 exercised the agent page (Undo) only.
- Criterion 24 (a live voice session runs the inactive agent's own prompt, greeting and voice): resolution is tested (TS, CA), but there is no live session. Not covered by T18: approvals.md:193 records that no STT/TTS/LLM keys exist on the build machine.
- Real-LLM half of criterion 54 and the voice half: not covered by T18 (the manual run was chat only with a canned LLM). Known limitation, approvals.md:193 and :204.
- Partial gaps, no manual cover: 5 (three blocks not asserted individually in the DOM), 29 ("existing test page unchanged" not asserted), 30 ("never activated by any other action"), 33 (chat variant), 36 (browser rendering of the customer-data message), 45 (retry), 51 (a model that obeys a planted instruction is not simulated).

## Anything still failing
None.

## Not exercised
- No live voice call and no real LLM (see above).
- RLS is not a verified control on this DSN (lesson 36); isolation rests on the explicit predicates, which were seen red during build (approvals.md:132-135, :165).
- The two new Playwright tests were not seen red.

## Cleanup note
The seeded superadmin `e2e-superadmin@example.com` and the throwaway `e2e-*` tenants created by every Playwright run are still in the database that Config on :8010 uses. approvals.md:228 asks for the superadmin to be deleted from voiceai_gac_test at the end of the test stage. I did not delete it: the user's running Config may be connected to that database and I was told not to disturb their servers. The orchestrator should delete it once the user is finished.

## Round 2 (test-gate fixes), 2026-10-03

Final single run on the working tree (test files only differ from HEAD):
- `POSTGRES_DSN=postgresql://chandankumar@localhost:5432/voiceai_gac_test timeout 1500 pytest libs/config_sdk/tests services/config/tests services/conversation/tests services/webcall/tests -q -rfE` — 1639 passed, 0 failed, 0 errors (533 s), nothing ignored.
- `cd admin-ui && npx tsc --noEmit` — exit 0.
- `cd admin-ui && E2E_EMAIL=... E2E_PASSWORD=... npx playwright test` — 11 passed, 0 failed (includes the strengthened missing-setup test).

Red proofs (each plant restored with cmp against a scratchpad backup; no production file changed):
- Missing-setup test: (a) account C -> complete account, red at `toBeDisabled()` on "Appointment booking" (Expected disabled, Received enabled). (b) D -> complete account, red at `getByText('Connect an AI service to continue')` not found. (b2) found that the "no job selectable" check ran before the list rendered and passed vacuously; it is now `not.toHaveCount(0)` plus `toHaveCount(0)` on enabled jobs, and with D -> complete account it is red: `Locator: button[aria-pressed]:not([disabled]) Expected: 0 Received: 8`.
- Facts test: (c) 1000 characters -> red, factsTooLong not found. (d) 1000 characters, inline-error expectation commented out, plus one more Continue so the agent is created -> red: `Expected: [] Received: [".../tenants/e2e-a-.../agents/from-template"]`. The agent it created was deleted via the API.
- pytest: (a) `if agent is None:` at routers/agents.py:69 -> `assert (200, ...) == (404, ...)` at test_guided_agent_creation.py:495. (c) plain POST inserting 'inactive' -> `assert ('inactive', None, None) == ('active', None, None)` at :482. (d) Generalise sentence removed -> `assert "Generalise from the transcript" in sent` at test_prompt_rules.py:294.
- (b) the llm_config ownership check removed at agents.py:222 first stayed green, because the composite FK on agents(llm_config_id, tenant_id) rejects it with a different 400 body. The test now also sends a random UUID id as the same caller and asserts byte-identical (status, body). With `if False:` at agents.py:222 it is red: `assert (400, b'{"det... not exist"}') == (400, b'{"det... not found"}')` — `request references an id that does not exist` != `llm_config_id not found`.
- Criterion 30: new `test_no_test_revise_accept_or_undo_step_ever_activates_the_agent` reads status after test-sessions, test-chat, revise, accept and undo. Red with `status = 'active'` added to the accept UPDATE (`assert 'active' == 'inactive'` at :637) and, separately, the undo UPDATE (same assertion at :640).
- Criterion 12: new Playwright test "the voice dropdown preselects the voice of the newest existing receptionist" ({2,2,2} account E with an agent created via API on its second voice; AI and speech recognition stay unselected). Red when expecting the first voice: `Expected: "7b491eee-..." Received: "22823120-..."`. The "only if the config still exists" half is not tested.
- Stop -> Start -> Back test: two fresh distinct mints, Fix hidden until the next session reports service_ready plus a line, Back creates no agent, Back absent while a proposal is pending. Red with distinct-credential expectation changed (`expect(new Set(minted).size).toBe(1)`: Expected 1, Received 2) and, separately, with Back-absent changed to count 1 (`Expected: 1 Received: 0`). Coming back to Test leaves a dead call, so a third Start mints again (asserted).

Criterion 12 now has coverage (except "only if the config still exists"); criterion 30 "never activated by any other action" now has coverage.
