# Tasks: Easy and Advanced agent creation for non-technical users

Line format: `- [ ] Tn <verb> <what> — files owned — deps — batch — done when`.

Execution rules:
- Tasks in one batch own disjoint files and run in parallel in a single message (lesson 40). A batch starts only when every earlier batch is done.
- Only one task per batch runs the Postgres-backed suite. Those tasks are marked **[DB]**. T1 (B1), T7 (B2), T8 (B3) and T9 (B4) are alone in their batch, so no two ever share the database. "Redis" tasks use a Redis instance only. T18 runs the whole suite once.
- After the last batch, T18 runs the full verification and the critic once on the merged tree (lesson 41). The per-task checks do not stand in for it.
- Test files are split per task so that no file has two owners. The design's `test_guided_agent_creation.py` is split into three (T7, T8, T9), and the design's servicer test moves to its own file in T12. The cases keep the design's wording.

Folded-in findings:
- **Security low, first-frame credential never logged.** T10 owns the test and the `:132` fix. The existing `malformed control message=%r` line logs whole frames, so a credential frame sent without `test=1` would be logged. T10 changes that line to log only the frame's length and type.
- **Design-critic minor, single-use credential on reconnect.** `02-design.review.md` as it stands has no such item. Its five items are all closed in the design, and the point appears only in this brief. It is covered here anyway. T2 and T11 prove a voice credential redeems once. T14 makes the hook one-shot, so a reconnect never resends a spent credential. T17 proves every Start or reconnect mints a fresh credential.
- **Design-critic minor, is Playwright needed.** `@playwright/test` is a devDependency in `admin-ui/package.json`, but there is no `playwright.config.ts` and no `e2e/` directory. Criteria 2, 6 and 7 and the reconnect mint count are DOM behaviours, and no component-test harness exists. So T17 is kept and scoped to those four checks only.

## Phase 1: Schema and shared libraries (each piece is additive and inert on its own)

- [x] T1 Add the four nullable `agents` columns and two both-or-neither CHECKs after the `call_flow_id` ALTER — `database/schema.sql` — deps: none — batch B1 **[DB]** — done when: apply `schema.sql` twice to a database with seeded `agents` rows and both applies end with the columns present and the constraints present once. Also (a) `INSERT` with `template_id` set and `template_version` NULL fails `agents_template_pair_check`, and (b) `UPDATE` with `prompt_undo_previous` set and the sha256 NULL fails `agents_prompt_undo_pair_check`. Both statements must be run and seen to fail, not assumed.
- [x] T2 Create the credential module (`TestGrant`, `mint_test_credential`, `redeem_test_credential`, `VOICE_TTL_S=60`, `CHAT_TTL_S=900`) with tests — `libs/config_sdk/test_credentials.py`, `libs/config_sdk/tests/test_test_credentials.py` — deps: none — batch B1 (Redis) — done when: against real Redis, (a) the Redis key is `testcred:<sha256hex>` and the plaintext token appears in no key; (b) two `consume=True` redeems give a grant, then `None`; (c) a channel mismatch gives `None` and, in the same test, a following correct-channel redeem still succeeds, which proves a mismatch does not consume; (d) a corrupt JSON record gives `None`; (e) `consume=False` can be repeated until `PEXPIRE` expires the key. TTLs are never shrunk (lesson 25).
- [x] T3 Thread `include_inactive` through the protocol, the cache-aside provider and the mock, and test it — `libs/config_sdk/interfaces.py`, `libs/config_sdk/providers/cache_aside.py`, `libs/config_sdk/providers/mock_provider.py` (plus the existing `libs/config_sdk/tests/test_cache_aside.py`, so 4 files; test edits only) — deps: none — batch B1 (Redis) — done when: `test_cache_aside.py` has cases showing that an inactive agent gives `None` by default and a runtime config with `include_inactive=True`. The flag affects only the `status != "active"` check (a missing agent is still `None` with the flag), and the existing cases pass unchanged.
- [x] T4 Add `string test_credential = 13;` to `SessionOpenRequest`, regenerate the stubs, and pass `include_inactive` through the resolver — `proto/voiceai/v1/conversation.proto`, `services/conversation/generated/` (regenerated), `services/conversation/agent_resolver.py` — deps: none — batch B1 — done when: `SessionOpenRequest(test_credential="x").test_credential == "x"` and the default is `""`. A check of the committed stubs for field 13 passes. `resolve_handler_deps(...)` without the keyword behaves as before in the existing conversation tests. A grep of the C++ gateway shows it never references the field.
- [x] T5 Add the heading and speech constants, the prompt rules, `revise_system_prompt`, `chat_test_reply` and `_load_tenant_llm_config`. Enforce structure on generate output, remove `resp.text` from the vendor-error log lines, and make a cross-tenant or non-UUID `llm_config_id` raise `LookupError("provider_config not found")` — `services/config/system_prompt.py`, `services/config/tests/test_prompt_rules.py` (new, pure, with the DB lookup and the model call mocked) — deps: none — batch B1 — done when: the pure tests cover the following.
  - `check_prompt_structure` and `enforce_prompt_structure`: a missing block is inserted at the end of its own section; a missing or misordered heading raises; fewer than 3 job lines raises; a bare `Guardrails` line inside facts is content.
  - `find_customer_data`: a positive and an exempt case for each token class, including a 40-character caller-line window.
  - Brace count: a proposal that adds `{{ caller_number }}` is rejected.
  - Lookup errors: the foreign, missing and non-UUID ids raise a `LookupError` with identical text.
  - Logging: no log record carries the mocked vendor body.
  - Max tokens: 1500 for generate and revise, 300 for a chat turn.
  - Run `max_tokens` and the log assertions against a mock that would fail them.

## Phase 2: Config backend (every route is new and admin-only, and the existing agent routes behave as before)

- [x] T6 Build the 8-job catalog, `render`, `slugify`, `public_catalog` and the new request schemas — `services/config/agent_templates.py`, `services/config/schemas.py`, `services/config/tests/test_agent_templates.py` (new, pure) — deps: T5 — batch B2 — done when: the pure tests pass for every item in the design's Unit list except the `easyCopy.ts` scan (added by T13).
  - Catalog shape and the criterion 15–18 structure checks. `render` uses `HUMAN_SPEECH_VOICE` and `HUMAN_SPEECH_CHAT` from `system_prompt.py` verbatim; the tests assert exact substring equality against the imported constants, not copies.
  - Rendering edge cases: a name or facts of `{agent_name}`, `{x}` or `${secret}` render literally.
  - Double braces: `AgentFromTemplate` rejects `{{`, `}}` and `{{secret}}` in `name`, `business_name` and `business_facts`, and the catalog contains neither token.
  - Runtime round-trip: every template rendered with the facts of every brace and punctuation character except `{{`/`}}`, passed through the real `libs.config_sdk.workflow.render`, is byte-identical. The test must go red if the renderer is made to touch tenant text.
  - The 4 existing jobs match the key/label pairs parsed from `admin-ui/lib/agentTemplates.ts`, and the parsed pair count is asserted greater than 0.
  - The composed greeting and prompt contain zero `{{`/`}}`, which is the security review's hardening note.
- [x] T7 Add `_strip_slot`, the single "not found" message in `_validate_provider_assignments`, the `create_agent` kwargs, `accept_prompt_revision`, `undo_prompt_revision` and the `_public_agent` `can_undo` and `prompt_fixable` fields. Strip the slot in `_audit_view` and the `update_agent` branch at `:377-378` — `services/config/agents.py`, `services/config/tests/test_agent_prompt_revision.py` (new) — deps: T1, T5 — batch B2 **[DB]** — done when: the DB tests pass, and each reads the row back from the DB, not the response.
  - Two concurrent Accepts on one base hash give one winner and one `None`.
  - Accept, Accept, Undo restores the prompt from before the second Accept, and a second Undo gives `None`.
  - Accept, a hand PATCH, then Undo gives `None`, leaves the prompt unchanged and makes `can_undo` false.
  - A foreign or random provider id on `create_agent` and `update_agent` gives an identical error message.
  - The slot test seeds a workflow with a `global` node so the mirrored branch runs. After PATCH(P0), Accept(P1), Accept(P2) and Undo, no `audit_log` or `agent_workflow_versions` row contains the slot key names or P1's sha256 hex. The test must fail when the `:377-378` branch is reverted to raw rows; reverting it and seeing red is part of done.
  - Accept's 400 brace check uses `adds_template_braces(proposed, base)`; a proposal that adds `{{` or `}}` over the stored prompt is refused, and one that does not add any is not.
  - `CustomerDataError` (a subclass of `PromptStructureError` in `system_prompt.py`) maps to 422 `customer_data` and must be caught before `PromptStructureError`, which maps to 422 `unusable_output`. Any handler T7 adds orders the `except` clauses that way, and a test with a customer-data proposal shows `customer_data` is not swallowed by the parent handler.
- [x] T8 Create the test-session service: `mint_test_session`, `run_chat_turn` and `load_test_transcript` (transcript tuples are `(caller_text, agent_text)`), with the conditional turn reservation, the born-ended `calls` row and the greeting row — `services/config/agent_testing.py`, `services/config/tests/test_agent_test_chat.py` (new) — deps: T2, T5, T7 — batch B3 **[DB]** — done when: DB tests call the service functions directly and each reads the DB back.
  - A chat mint creates a `calls` row with direction `test`, the tenant slug, the agent id and a non-NULL `ended_at`, plus a greeting row at turn 0.
  - A turn creates transcript turns 0 and 1.
  - Turn 41 raises the cap error.
  - With `turn_count` at 39, two turns are fired through `asyncio.gather` with the model mock held on an event so both are in flight. Exactly one succeeds, and `turn_number` values are unique.
  - The live-calls queries do not list the row, and `soft_delete_agent` succeeds on a chat-tested agent.
  - `load_test_transcript` with tenant B's `calls` row (`agent_id` = tenant A's agent) returns nothing. The test must go red when `c.tenant_id = $2` is deleted.
  - An agent whose `llm_config_id` is set by direct SQL to tenant B's config, and one set to a random UUID, both raise `LookupError("provider_config not found")` with zero model calls.
- [x] T9 Add the six routes and `GET /agent-templates`, the `AgentAssistThrottle`, and mount `catalog_router` — `services/config/routers/agents.py`, `services/config/app.py`, `services/config/tests/test_guided_agent_creation.py` (new) — deps: T6, T7, T8 — batch B4 **[DB]** — done when: real-Postgres, real-Redis, real-JWT tests pass for these. Every guard call is `await`ed (lesson 38), and a mechanical AST check asserts it for the new routes.
  - Isolation (design Integration 1): foreign and random ids give byte-identical status and body for the same caller on every route and field listed there, with zero model calls. The tenant-B `calls` row fixture goes red if the tenant predicate is removed.
  - Revise order and statuses: 404, 422 `prompt_not_fixable` before any session or model access, 429, 404, 404, 502, 422 `unusable_output`, 422 `customer_data`. The routes catch `CustomerDataError` (422 `customer_data`) before `PromptStructureError` (422 `unusable_output`); a customer-data hit must return `customer_data`, not `unusable_output`, on revise; Accept returns 400 'proposed prompt is not acceptable' for every failed check (criterion 39; B4 approver).
  - Accept exemption (criterion 35): same `problem` gives 200, different `problem` gives 400.
  - Accept and Undo 409 versus 404.
  - Freshness (finding 1): after Accept, `CacheAsideConfigProvider.get_runtime_config(..., include_inactive=True)` returns the new prompt, and after PATCH to active a flagless call returns non-None at once.
  - Chat credential cases: an expired credential (`PEXPIRE`), a voice credential and tenant B's credential each give the same 404 as a missing session.
  - A voice credential minted via the route redeems once, then `None`.
  - Every new route gives 403 for a viewer and 401 without a token.
  - The DEBUG-level sentinel log test, with `httpx` included, covers create, revise, accept and chat. It must go red when the `resp.text` log is restored.

## Phase 3: Voice test path (credential-less sessions are untouched)

- [x] T10 Add the first-frame credential handshake to webcall, add `session_id` to `service_ready`, and stop `:132` logging frame content — `services/webcall/__main__.py`, `services/webcall/tests/test_first_frame.py` (new; the directory does not exist yet) — deps: T4 — batch B2 — done when: unit tests with a fake Conversation stub pass.
  - With `test=1`, a good first frame results in `session_open` carrying the credential, written once and after the frame.
  - A wrong `type`, malformed JSON containing a sentinel, a binary frame and a 5 s timeout each close with 1008 and the stub receives no `session_open`.
  - A credential frame sent without `test=1` leaves behavior as before.
  - Under DEBUG `caplog`, the sentinel appears in no record on any of those paths, and the first-frame handler never calls `_browser_to_grpc`.
  - The mutation check is part of done: temporarily restore `%r` of the frame at `:132`, and the credential-without-`test=1` case must go red. Report it as exercised only if it did.
  - The 5 s wait is exercised at its real value, or with a mocked clock and not a shrunk constant (lesson 25).
  - The `service_ready` JSON frame sent to the browser carries a non-empty `session_id` equal to the one on `session_open`.
- [x] T11 Add `SessionContext.test_credential` (`field(default="", repr=False)`) and define `AgentUnavailable` in `session.py`. Add the `credential_redis` client and the credential branch at the top of `handler_factory`, importing `AgentUnavailable` from `session.py` — `services/conversation/session.py`, `services/conversation/__main__.py`, `services/conversation/tests/test_test_sessions.py` (new) — deps: T2, T3, T4 — batch B2 (Redis, fake config provider) — done when: tests pass for the following. The legacy `load_agent` spy is asserted called zero times in each refusal case, and called once in each no-credential case.
  - Inactive agent plus valid voice credential gets that agent's prompt and voice. Redeeming the same credential a second time raises `AgentUnavailable`.
  - A wrong-agent, wrong-tenant, expired or chat credential raises `AgentUnavailable`, and so does a credential for an agent deleted and re-created under the same slug.
  - No credential: an inactive agent and a missing slug reach the same legacy path, and an active agent resolves as today.
  - `direction="inbound"` with a valid credential takes the legacy path, and the credential is still in Redis afterwards.
  - The stub-field check from T4 is exercised.
  - `repr(ctx)` of a `SessionContext` built with a sentinel `test_credential` does not contain the sentinel. The check goes red if `repr=False` is removed.
- [x] T12 Copy `open_req.test_credential` into `ctx` and yield one fatal `AGENT_UNAVAILABLE` error from the servicer, importing `AgentUnavailable` from `session.py` — `services/conversation/servicer.py`, `services/conversation/tests/test_servicer_agent_unavailable.py` (new) — deps: T11 — batch B3 — done when: a servicer test shows `AgentUnavailable` from the factory produces exactly one `ServiceError(code="AGENT_UNAVAILABLE", message="agent unavailable")` and the stream ends. The servicer's open-log output (captured at DEBUG) contains no sentinel credential. A non-test session still opens.
  - Ownership note (B3 approver): the servicer.py change landed in B2 WIP commit 00b7c7a without an owner; T12 supplied its tests and mutation proof. From B4 on, each gate diffs every agent's touched files against its owned list.

## Phase 4: Admin UI (Easy is the default and Advanced is the unchanged wizard)

- [x] T13 Add the Easy copy module and the API client functions and types, and extend the criterion 7 scan — `admin-ui/lib/easyCopy.ts` (new), `admin-ui/lib/api.ts`, `services/config/tests/test_agent_templates.py` (append) — deps: T6 — batch B3 — done when: `tsc --noEmit` passes in `admin-ui` and the new Python test scans every `easyCopy.ts` string and every catalog display field for banned words. The test asserts the scanned count is greater than 0, and goes red when a banned word is planted in `easyCopy.ts`. `easyCopy.ts` contains the `{{ }}` message and the "can't be fixed automatically" message verbatim from the design.
- [x] T14 Make `useWebCall` accept an optional one-shot `testCredential` — `admin-ui/lib/useWebCall.ts` — deps: T10 — batch B3 — done when: `tsc --noEmit` passes. With the argument set, the URL has `&test=1` and the first frame sent in `onopen` is the credential frame, before any audio. The credential is cleared from the hook after it is sent, and no auto-reconnect path reuses it (the critic's reconnect minor). `sessionId` is exposed from `service_ready`. Without the argument, the Advanced test page's URL and frames are unchanged. The browser check is T17.
- [x] T15 Create the test step, voice and chat variants, and show Undo on the agent page — `admin-ui/components/EasyTestStep.tsx` (new), `admin-ui/app/(console)/agents/[tenantSlug]/[agentSlug]/page.tsx` — deps: T9, T13, T14 — batch B4 — done when: `tsc --noEmit` passes, then run against a running stack in a browser (lesson 23). A chat test shows greeting, reply and live transcript. A voice test connects, with each Start minting a fresh credential. The agent page shows "Undo last change" only when `can_undo` is true. Easy shows only `easyCopy.ts` text on an induced 404, 429 and 502. The `api.ts` types from T13 are re-checked against the live T9 responses (a real call to each new route, with field names and nullability compared against `AgentRow` and the new response types), and any mismatch is fixed in `api.ts` before the task is done.
- [x] T16 Build the six-step flow and add the Easy/Advanced mode switch — `admin-ui/components/EasyAgentFlow.tsx` (new), `admin-ui/app/(console)/agents/new/page.tsx` — deps: T13, T15 — batch B5 — done when: `tsc --noEmit` and the build pass, and in a browser the six steps run end to end.
  - Ownership note (UI approver): T15/T16 appended strings to admin-ui/lib/easyCopy.ts (T13's file) in 0c14a4a; T13's scans re-run, 69 passed.
  - Control gating follows criteria 4 and 10, and the {1,1,1} and {2,2,2} config-count cases show the control sets in the design.
  - Fix shows before and after with Accept, Discard and Undo, and the `prompt_fixable=false` message shows with no text box.
  - Put it to work activates via PATCH and shows the "Choose who takes passed-on calls" link.
  - The Advanced control opens the existing wizard with no visible change.
- [x] T17 Add the Playwright config and the Easy-create spec — `admin-ui/playwright.config.ts` (new), `admin-ui/e2e/easy-create.spec.ts` (new) — deps: T16, T9 — batch B6 — done when: `npx playwright test` passes on a running stack and covers four things.
  - Ownership note (build-end approver): T17 adds `test_e2e_banned_list_matches` to services/config/tests/test_agent_templates.py (T13's file) as a drift guard for the duplicated banned-word list.
  - The six titles appear in order.
  - The exact editable-control set holds for config counts {1,1,1} and {2,2,2}.
  - The banned-word DOM scan runs on every step, error state and confirmation, and asserts the scanned text is non-empty.
  - Stop then Start, and a forced reconnect, each trigger a new mint call, and the mint count equals the number of Starts. The Advanced control opens the unchanged wizard.
  - For a voice test the spec asserts the WebSocket URL contains `&test=1` and no credential, that the first frame sent is the `test_credential` frame, and that after a forced reconnect the first frame carries a different credential from the first one.
  - At least one check is shown to go red by planting a banned word or a second control.
- [x] T18 Run the join verification on the merged tree, then the manual browser run for criterion 54 — no files owned (read-only) — deps: T1–T17 — batch B7 **[DB]** — done when: the full Config, Conversation, webcall and `config_sdk` suites pass once on the combined tree, `tsc` and the Playwright spec pass, and the T10 and T7 mutation checks are re-run on the merged tree. The critic re-runs on the merged state (lesson 41). The manual run covers create, test (voice and chat), Fix, Accept, Undo and Put it to work, and any defect found becomes a new task.
