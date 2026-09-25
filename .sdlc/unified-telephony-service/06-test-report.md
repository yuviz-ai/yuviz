# Test report

COMMAND: `source venv/bin/activate && pytest services/telephony/tests libs/telephony_sdk/tests services/config/tests/test_telephony_configs.py services/config/tests/test_telephony_config_listing.py services/campaigns/tests/test_telephony_originate.py services/campaigns/tests/test_worker.py services/campaigns/tests/test_campaigns.py -q`

RESULT: 138 passed, 26 failed (164 total)

## Per-area breakdown

- `services/telephony/tests/` + `libs/telephony_sdk/tests/` + `services/config/tests/test_telephony_configs.py` + `test_telephony_config_listing.py` + `services/campaigns/tests/test_telephony_originate.py` — **112 passed, 0 failed**
- `services/campaigns/tests/test_worker.py` + `test_campaigns.py` — **6 passed, 26 failed**

## Failures (all in campaigns, pre-existing)

All 26 failures raise `libs.tenancy.session.TenantUnresolved: could not resolve a tenant for this connection (tenant=None)` from `tenant_conn(pool)` inside `services/campaigns/campaigns.py` (`create_campaign`, `caller_id_owned_by_tenant`) — every DB-backed test in `services/campaigns/tests/` fails this way, including ones that never touch telephony logic (e.g. `test_within_calling_hours_*` pass because they don't call `create_campaign`; every test that does, fails identically). This reproduces on the pre-feature baseline per the implementer's git-stash check across three rounds this session — **pre-existing, not caused by this feature**. Listed honestly in the raw count above rather than excluded.

FAILURES (all same root cause, `TenantUnresolved`):
- test_worker.py::test_tick_campaign_originates_a_pending_contact
- test_worker.py::test_tick_campaign_respects_pacing
- test_worker.py::test_tick_campaign_respects_concurrency_cap
- test_worker.py::test_tick_campaign_marks_completed_when_no_contacts_remain
- test_worker.py::test_tick_campaign_skips_when_no_caller_id
- test_worker.py::test_originate_failure_marks_contact_failed
- test_worker.py::test_on_job_complete_resolves_contact_and_decrements_in_flight
- test_worker.py::test_on_job_complete_failure_does_not_set_call_session_id
- test_worker.py::test_tick_campaign_skips_outside_calling_hours
- test_worker.py::test_tick_campaign_blocks_dnc_listed_contact
- test_worker.py::test_failed_contact_retried_until_max_attempts_then_exhausted
- test_worker.py::test_same_attempt_retried_through_http_hop_reuses_one_key (AC18)
- test_worker.py::test_requeued_attempt_mints_a_different_key (AC18)
- test_worker.py::test_202_leaves_contact_calling_and_resolves_via_poll_on_next_tick (AC14/AC20)
- test_worker.py::test_unowned_caller_id_refuses_to_dial_never_falls_back_to_esl (AC33)
- test_worker.py::test_owned_did_with_no_rest_binding_still_takes_esl_path (AC33)
- test_worker.py::test_native_or_none_provider_still_takes_esl_path (AC33)
- test_campaigns.py::test_create_and_get_campaign
- test_campaigns.py::test_list_campaigns_scoped_to_tenant
- test_campaigns.py::test_update_campaign_changes_only_given_fields
- test_campaigns.py::test_set_status_transitions
- test_campaigns.py::test_update_unknown_campaign_raises_lookup_error
- test_campaigns.py::test_caller_id_owned_by_tenant_true_for_provisioned_did
- test_campaigns.py::test_caller_id_owned_by_tenant_false_for_another_tenants_did
- test_campaigns.py::test_caller_id_owned_by_tenant_false_for_unprovisioned_number
- test_campaigns.py::test_get_progress_counts_by_status

## Passing tests by area (selected — 112 telephony-area + 6 campaigns)

- `libs/telephony_sdk/tests/test_interface_conformance.py`, `test_transfer_unsupported.py`, `test_check_health_default.py`, `test_vobiz_provider.py`, `test_cloudonix_provider.py`, `test_did_route.py` — pass (AC1-5, AC9)
- `services/telephony/tests/test_inbound_orchestration.py` — pass (AC6-10)
- `services/telephony/tests/test_ratelimit_order.py` — pass (AC11-12)
- `services/telephony/tests/test_idempotency.py` — pass (AC13-17)
- `services/telephony/tests/test_accounts.py` — pass (accounts/decryption)
- `services/telephony/tests/test_outbound_auth.py`, `test_outbound_roles.py`, `test_auth.py`, `test_no_unawaited_guards.py` — pass (auth findings #1-3, R2-1/R2-2)
- `services/telephony/tests/test_status_callback_scoping.py` — pass (finding #5)
- `services/telephony/tests/test_callctx.py` — pass (finding #4)
- `services/telephony/tests/test_hot_path_isolation.py` — pass (Latency claim)
- `services/telephony/tests/test_health.py` — pass (AC25-28)
- `services/telephony/tests/test_outbound.py`, `test_outbound_answer_identity.py`, `test_ownership.py` — pass
- `services/config/tests/test_telephony_configs.py`, `test_telephony_config_listing.py` — pass (AC21-24)
- `services/campaigns/tests/test_telephony_originate.py` — pass (originate retry/backoff, AC20)
- `services/campaigns/tests/test_worker.py::test_on_job_complete_ignores_unknown_job_uuid`, `test_within_calling_hours_*` (x3), `test_tick_holds_no_transaction_between_scan_iterations` — pass (no DB dependency)
- `services/campaigns/tests/test_campaigns.py::test_caller_id_owned_by_tenant_true_when_none` — pass (no DB dependency)

FAILURES: See list above — all 26 are the pre-existing `TenantUnresolved` issue in `services/campaigns/campaigns.py`'s `tenant_conn(pool)` calls, reproduced on baseline, not introduced by this feature.

## PRD acceptance-criteria coverage (35 total)

Covered by a passing test: AC1, AC2, AC3, AC4, AC5, AC6, AC7, AC8, AC9, AC10, AC11, AC12, AC13, AC14, AC15, AC16, AC17, AC21, AC22, AC23, AC24, AC25, AC26, AC27, AC28, AC35 (satisfied by a manual grep with zero hits, run above — no leftover `services.vobiz`/`services.cloudonix`/`:8500`/`:8700` references, but no automated regression test pins this).

UNCOVERED:
- **AC18, AC20, AC33** — tests exist (`test_same_attempt_retried_through_http_hop_reuses_one_key`, `test_requeued_attempt_mints_a_different_key`, `test_202_leaves_contact_calling_and_resolves_via_poll_on_next_tick`, `test_unowned_caller_id_refuses_to_dial_never_falls_back_to_esl`, `test_owned_did_with_no_rest_binding_still_takes_esl_path`, `test_native_or_none_provider_still_takes_esl_path`) but all currently fail on the pre-existing `TenantUnresolved` bug, so these criteria have no test that actually passes today.
- **AC19** — no test found anywhere in the repo for the SMS-tool idempotency key format `sha256(call_session_id:tool_call_id)`. The design's own Risks section notes the `send_sms` tool was retired and this path is "dormant" — consistent with there being no caller code and no test, but the design's Test plan does not list a test for AC19 either, so this is a genuine PRD-to-test-plan gap, not just a currently-failing test.
- **AC29, AC30, AC31** (migration script) — the design's own Test plan specifies a `scripts/` migration test seeded with a 5000-5009 row, a non-5000-range row, and an already-`'native'` row. No such test file exists (`scripts/migrate_telephony_providers.py` has no companion test anywhere in the repo). Uncovered by automated tests.
- **AC32, AC34** — explicitly integration/manual/phase-gated in the design's own Test plan (live inbound call, live Cloudonix call, atomic per-row cutover verification); no automated test is expected or exists for these.

## Notes for the implementer

The campaigns `TenantUnresolved` failures are real, reproducible, and block CI green on `services/campaigns/tests/`, but are pre-existing per the implementer's own git-stash verification against the baseline — not something this report attributes to the telephony feature. AC18/AC20/AC33 tests are well-targeted and would close their criteria the moment the underlying `tenant_conn` bug is fixed; they should not be rewritten or weakened to work around it.
