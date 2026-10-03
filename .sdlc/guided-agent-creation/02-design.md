# Design: Easy and Advanced agent creation for non-technical users

## Approach
The server owns everything that must be guaranteed. A Python catalog (`services/config/agent_templates.py`) holds the shipped jobs and renders their prompts, using the verbatim blocks that live beside `_GUARDRAILS` in `system_prompt.py`. A dedicated `from-template` route composes and creates the agent as inactive. One pure function, `enforce_prompt_structure`, is the gate on Generate, Revise and Accept. The obvious alternative was to keep templates in `admin-ui/lib/agentTemplates.ts` and have the client compose an `AgentCreate`. We reject it because the UI cannot import the Python constants and the Config container cannot read `admin-ui/`, so we would end up with two sources of truth for the guardrail text. The server also could not prove criteria 9/13/14 if the client did the substitution. Testing an inactive agent works like this. Config mints an opaque credential and stores it in Redis, bound to tenant, agent and channel. Conversation, not webcall, redeems it on a new `SessionOpenRequest.test_credential` field. Only then does it read the agent with `include_inactive=True`. Chat tests run inside Config: one LLM call per turn using the agent's prompt, written to the existing `calls`/`transcript_entries` rows with direction `test`. Conversation has no text path, and no chat runtime exists to stay faithful to.

## Decisions on the PRD critic's five findings
1. **Freshness after Accept, Undo or Activate.** Activate is the existing `PATCH /agents/{id} {status:"active"}`, so it goes through `update_agent`. Accept and Undo end the same way as `update_agent` (`services/config/agents.py`): after commit they run `cache.invalidate(cache_key(tenant_slug, slug))` and then `call_flows.invalidate_runtime_caches_naming_agent(...)`. Conversation's `CacheAsideConfigProvider` misses Redis, then reads Config over HTTP, which reads Postgres. The Easy UI mints a new credential and opens a new session for every test, so nothing on the client holds an old agent. There is no "bypass the cache" flag: Config's own GET reads through the same cache, so a bypass in Conversation would buy nothing. The leftover race is listed under Risks.
2. **Criterion 6 versus criterion 12.** Criterion 12 wins, and the criterion 6 set grows. Step 3 shows "Voice", "AI service" and "Speech recognition" dropdowns. Each appears only when the tenant has more than one config of that role, and each uses criterion 12's preselect rule. Chat jobs never show Voice or Speech recognition. The exact-set test is parameterised by config counts per role. When a role has exactly one config, it is assigned silently (criterion 11).
3. **Test credential bounds.**
   - Lifetime: voice is 60 s; chat is 900 s, fixed and never extended.
   - Voice is single-use: at session open Conversation does a GET the record, check the channel, then DEL; only the caller whose DEL returns 1 wins (see `redeem` under Interfaces).
   - Chat can be replayed within its lifetime, but only alongside an admin JWT for the same path tenant, the same `agent_id` and the `session_id` bound when it was minted. The JWT, role and tenant are re-checked on every turn.
   - A credential is honoured only on the `direction == "test"` branch of Conversation's `handler_factory` and on Config's chat-turn route. Every other path ignores it, so an inactive agent there resolves as unavailable, exactly as today. The `channel` stored in the record must match the place it is presented, so a voice credential cannot drive a chat turn and the reverse.
   - Per-tenant caps: 20 mints/min, 60 chat turns/min, 40 turns per chat session, 20 revises/hour.
4. **A client-claimed `direction="test"` with no credential.** "Test" never grants anything by itself; only a redeemed credential lifts the inactive check. `services/webcall/__main__.py:276` hardcodes `direction="test"` on every browser session, including the Advanced test page, so the new branch is keyed on a credential being *present*, not on the direction:
   - **Credential present.** This is every Easy test. A missing, expired, wrong-tenant, wrong-agent or wrong-channel credential, or an agent that does not resolve, gets one fatal `ServiceError(code="AGENT_UNAVAILABLE", message="agent unavailable")`. There is never a legacy fallback (criterion 25).
   - **No credential.** Webcall sessions keep today's behaviour exactly: an active agent runs, and an inactive or missing agent falls back to legacy the same way. Inactive and missing therefore stay indistinguishable (criterion 26), and an inactive agent's own config is never reached.
   - **Why not also refuse "inactive, no credential".** That would answer an inactive slug differently from a missing one, letting unauthenticated callers learn which inactive agent slugs exist (lesson 2).
   - **Invariance.** For any one request shape (no credential, or a given invalid credential), the response does not vary with the target's existence or state.
5. **The fix loop on a hand-edited prompt.** The agent payload carries `prompt_fixable`, which is the result of `check_prompt_structure` on the stored prompt. When it is false, the Easy step 5 shows a plain message from `easyCopy.ts`: "These instructions were changed by hand, so they can't be fixed automatically here. You can still edit them on the receptionist's page." It shows no text box, and Continue goes to step 6. If a revise request is sent anyway, the server returns `422 prompt_not_fixable` before it reads a session or calls a model.

## Changes
| File | Change | Why |
|---|---|---|
| `database/schema.sql` | Add four nullable `agents` columns plus two both-or-neither CHECKs (see Data) | Template id/version and the undo slot (PRD's only schema additions) |
| `services/config/schemas.py` | Add `AgentFromTemplate`, `TestSessionCreate`, `TestChatTurn`, `PromptRevise`, `PromptAccept` with length bounds. `AgentFromTemplate` validators reject `{{`/`}}` in `name`, `business_name` and `business_facts` | Request shapes; `AgentCreate` stays as it is |
| `services/config/agent_templates.py` (new) | Frozen `AgentTemplate` dataclass and `CATALOG` of 8 jobs. `get_template(id, version)`, `render(template, *, name, business_name, facts) -> (greeting, system_prompt)`, `slugify(name)`, `public_catalog()` | The one shipped catalog, reachable by the server and, through the API, by the UI |
| `services/config/system_prompt.py` | Add `HEADING_SPEAK/HEADING_GUARDRAILS/HEADING_JOB`, `HUMAN_SPEECH_VOICE`, `HUMAN_SPEECH_CHAT` beside `_GUARDRAILS`. Add `check_prompt_structure`, `enforce_prompt_structure`, `find_customer_data`, `revise_system_prompt`, `chat_test_reply` and `_load_tenant_llm_config`. Callers take a message list, an optional system message and `max_tokens`. Meta-prompts demand the three headings and exact copies. Generate output goes through `enforce_prompt_structure`. Cross-tenant and non-UUID `llm_config_id` now raise `LookupError` with the same text as a missing id. Drop `resp.text` from the vendor-error log lines | One place for every block (criteria 16/17/19). Criterion 47: today a cross-tenant config id gives a 400 "belongs to a different tenant" while a missing one gives a 404, which is a lesson-2 leak. Criterion 50 |
| `services/config/agents.py` | `_validate_provider_assignments` returns one message, `f"{field} not found"`, for a missing id (today's "does not exist", `:211`) and a foreign-tenant id (today's "belongs to a different tenant", `:221`). The query and its `FOR SHARE` are unchanged, so both paths cost one lookup. Role-mismatch and unusable-voice errors are reachable only for the tenant's own rows and stay as they are. `create_agent` gains `language`, `status`, `template_id`, `template_version` kwargs. Add `accept_prompt_revision` and `undo_prompt_revision`. Add one helper, `_strip_slot(row) -> dict`, which pops `prompt_undo_previous` and `prompt_undo_accepted_sha256`; it is the only code that knows those column names outside SQL. `_public_agent` computes `can_undo` and `prompt_fixable` from the raw row, then calls `_strip_slot`. `_audit_view` calls `_strip_slot`. `update_agent`'s mirrored-graph branch (`agents.py:377-378`) changes from `old_audit = old` / `new_audit = new` to `_strip_slot(old)` / `_strip_slot(new)`. Accept and Undo audit with `_strip_slot(...)` on both rows, mirrored or not | Single conditional updates (lesson 8). Every sink that receives an `agents` row now gets the slot stripped (lesson 33; see the reader inventory under Data). One message for missing and foreign config ids closes the existence oracle (lesson 2) for `from-template`, `POST ""` and `PATCH` together |
| `services/config/agent_testing.py` (new) | `mint_test_session`, `run_chat_turn`, `load_test_transcript`. `run_chat_turn` picks its LLM config as the agent's `llm_config_id`, else the tenant's `default_llm_config_id`, always through `_load_tenant_llm_config(tenant["id"], ...)` | The test path and the transcript reader for revise, with explicit predicates (lesson 36). The chat path gets the same cross-tenant config gate as revise |
| `services/config/routers/agents.py` | Routes: `POST /from-template`, `POST /{agent_id}/test-sessions`, `POST /{agent_id}/test-chat`, `POST /{agent_id}/prompt/revise`, `POST /{agent_id}/prompt/accept`, `POST /{agent_id}/prompt/undo`. Add a second `catalog_router` (`GET /agent-templates`). All mutating routes use `require_role("superadmin","admin")` plus `_resolve_tenant` | Thin wrappers matching the existing routes in this file. The catalog sits outside `/agents/{agent_slug}` so it cannot shadow an agent slug |
| `services/config/app.py` | Add `AgentAssistThrottle` (three `FixedWindowCounter`s keyed by tenant id), `app.state.agent_assist_throttle`, and `include_router(catalog_router)` | Rate caps (finding 3), matching `InviteThrottle` |
| `libs/config_sdk/test_credentials.py` (new) | `TestGrant` dataclass, `mint_test_credential(redis, grant, ttl_s) -> str`, `redeem_test_credential(redis, token, *, channel, consume) -> TestGrant \| None`, constants `VOICE_TTL_S=60`, `CHAT_TTL_S=900` | One key format shared by Config (mint and chat check) and Conversation (redeem) |
| `libs/config_sdk/interfaces.py` | `get_runtime_config(..., *, include_inactive: bool = False)` | Protocol signature |
| `libs/config_sdk/providers/cache_aside.py` | Honour `include_inactive` in the `status != "active"` check, and only there | The test path's sole way past the inactive check |
| `libs/config_sdk/providers/mock_provider.py` | Same keyword | Keeps the protocol in step |
| `services/conversation/agent_resolver.py` | `resolve_handler_deps(..., *, include_inactive: bool = False)` passes it through | Keeps today's contract for every other caller |
| `services/conversation/session.py` | `SessionContext.test_credential: str = field(default="", repr=False)`, plus `class AgentUnavailable(Exception)` (owned by T11) | Carries the credential to `handler_factory` without logging it. The exception lives here, the module both `servicer.py` and `__main__.py` already depend on, so neither imports the other |
| `services/conversation/servicer.py` | Copy `open_req.test_credential` into `ctx`. Import `AgentUnavailable` from `.session`, not from `__main__`, which would be an import cycle. Wrap `self._handler_factory(ctx)` in a catch for `AgentUnavailable` that yields one fatal `ServiceError(code="AGENT_UNAVAILABLE", message="agent unavailable")` and returns | Criterion 25/26 and finding 4: a refusal, not a fallback |
| `services/conversation/__main__.py` | Import `AgentUnavailable` from `.session`. Add a `credential_redis` client from `REDIS_URL`. Add the test branch at the top of `handler_factory` (literal code under Interfaces) | Conversation decides "test" from the credential, not from the direction field |
| `proto/voiceai/v1/conversation.proto` and the regenerated stubs under `services/conversation/generated/` | `string test_credential = 13;` on `SessionOpenRequest`. Regenerate `conversation_pb2.py`, `.pyi` and `_grpc.py` with `python -m grpc_tools.protoc` from the repo's pinned `grpcio-tools==1.83.0` (requirements.txt:50; it bundles protoc), and do not hand-edit them. The output must load under the pinned runtime, `protobuf==7.35.1` (requirements.txt:79). T4's regeneration result in `approvals.md` supersedes this if it names a different protoc version | Additive. The C++ gateway never sets it |
| `services/webcall/__main__.py` | When the query has `test=1` (a flag, not a secret), wait at most 5 s for the first WebSocket frame. It must be text of the form `{"type":"test_credential","credential":"…"}`. Copy the credential into `SessionOpenRequest.test_credential` before `session_open` is written. Any other frame, or a timeout, closes with 1008 and sends no `session_open`. The frame is never logged. Without `test=1`, nothing changes. Add `"session_id"` to the `service_ready` JSON frame | The credential never appears in a URL, so ingress and proxy request-line logs cannot capture it (security finding 2). The Easy test step needs the session id for revise. Credential-less sessions are untouched |
| `admin-ui/app/(console)/agents/new/page.tsx` | Add `mode` state, default `"easy"`. Easy returns early with `<EasyAgentFlow onAdvanced=…/>`. The existing wizard renders unchanged when `mode==="advanced"` | Criterion 1, with the smallest diff to the Advanced wizard |
| `admin-ui/components/EasyAgentFlow.tsx` (new) | The six steps, the criterion 4/10 gating, criterion 11/12 assignment, Fix (before/after, Accept, Discard, Undo) and the Put it to work confirmation | The Easy flow |
| `admin-ui/components/EasyTestStep.tsx` (new) | Voice variant (`useWebCall` plus a credential, "Start talking"/"Stop", live transcript) and chat variant (text conversation via `/test-chat`). Reports `sessionId` upward | Criteria 24, 28, 29 |
| `admin-ui/lib/easyCopy.ts` (new) | Every user-visible Easy string, including the criterion 10/36 messages and the server-error-token-to-text map | Criterion 7 can be tested mechanically. Easy never renders a server `detail` |
| `admin-ui/lib/api.ts` | Client functions for the new routes, plus `AgentRow.can_undo/prompt_fixable/template_id/template_version` | Same module the wizard imports |
| `admin-ui/lib/useWebCall.ts` | Optional `testCredential` argument. When it is set, the URL gets `&test=1` and the first frame sent in `onopen` is `{"type":"test_credential","credential":…}`, before any audio. Expose `sessionId` from the `service_ready` frame | Reuses the voice engine, so there is no second WebSocket client |
| `admin-ui/app/(console)/agents/[tenantSlug]/[agentSlug]/page.tsx` | Show "Undo last change" only when `agent.can_undo` | Criteria 42 and 44 on the agent page |
| `services/config/tests/test_agent_templates.py` (new) | Pure tests of the catalog, rendering and prompt rules | Criteria 3, 5, 13–20, 35 |
| `services/config/tests/test_guided_agent_creation.py` (new) | Route and DB tests | Criteria 4, 9–11, 21–23, 26, 28, 30, 32–53 |
| `services/conversation/tests/test_test_sessions.py` (new) | `handler_factory` test-branch tests | Criteria 24–27, finding 4 |
| `libs/config_sdk/tests/test_cache_aside.py` | `include_inactive` cases | Criterion 27 regression |
| `admin-ui/playwright.config.ts` (new) and `admin-ui/e2e/easy-create.spec.ts` (new) | Exact control-set test and criterion 7 DOM text scan on a running stack | Criteria 2, 6, 7, 29. `@playwright/test` is already a devDependency |

## Data
`database/schema.sql`, placed after the existing `ALTER TABLE agents ADD COLUMN IF NOT EXISTS call_flow_id ...` line, using the file's idempotent ALTER plus `DO $$ ... duplicate_object` convention. There is no backfill: every existing row is NULL in all four columns, so neither CHECK can fail on live rows (lessons 5, 10, 13 do not bite).

```sql
ALTER TABLE agents ADD COLUMN IF NOT EXISTS template_id                 TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS template_version            INT;
-- Undo slot: prompt in force before the latest accepted revision, and the
-- sha256 hex of the prompt that Accept wrote. Never returned by the API.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS prompt_undo_previous        TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS prompt_undo_accepted_sha256 TEXT;
DO $$ BEGIN
    ALTER TABLE agents ADD CONSTRAINT agents_template_pair_check
        CHECK ((template_id IS NULL) = (template_version IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE agents ADD CONSTRAINT agents_prompt_undo_pair_check
        CHECK ((prompt_undo_previous IS NULL) = (prompt_undo_accepted_sha256 IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
```

- **The slot lives on `agents`, not a side table.** The row is the agent, so it already carries `tenant_id` and is bound to `(id, tenant_id)` by being that row. This lets Accept change the prompt and the slot in one conditional `UPDATE`, which a side table could not. Every slot read and write goes through `WHERE id = $1 AND tenant_id = $2`. Existing RLS on `agents` already covers the new columns; no `rls.sql` change is needed.
- **No UNIQUE index or new table.** Lesson 3 does not apply.
- **Every `agents` row reader, and where its slot columns end up** (lesson 33). Found by `grep -rn "FROM agents\|INTO agents\|UPDATE agents" services libs` filtered to `*`/`RETURNING *`. No other service selects `*` from `agents`. Conversation gets agents only via the Config payload or the Redis cache, both of which `_public_agent` builds.
  - `agents.py` `get_agent` (`:138`), `get_agent_by_id` (`:155`), `list_agents` (`:164`): go through `_public_agent`, which strips.
  - `create_agent` `RETURNING *` (`:268`): goes through `_audit_view` and `_public_agent`. The slot is NULL on insert in any case.
  - `update_agent` old row (`:328`) and `RETURNING *` (`:364`): `_audit_view`/`_strip_slot` on both audit branches, and `_public_agent` for the response.
  - `soft_delete_agent` (`:421`): goes through `_audit_view`.
  - New `accept_prompt_revision`/`undo_prompt_revision` `RETURNING *`: `_strip_slot` for the audit, `_public_agent` for the response.
  - `workflows.py` `_locked_agent` (`:90`) and the publish `RETURNING *`: read only `workflow`/`workflow_draft`/`slug`/`config_version`. They audit `{"workflow": ...}` and return lean dicts, so no change.
- **`append_version` cannot carry the slot.** It stores only the graph argument. The graph comes from `_mirror_prompts_into_graph`, which writes only `greeting`/`system_prompt` into node `data`. It never sees the slot columns, so `agent_workflow_versions` holds the then-current prompt, never the slot.
- **Hash rule.** Python uses `hashlib.sha256(prompt.encode("utf-8")).hexdigest()`. SQL uses `encode(sha256(convert_to(coalesce(system_prompt,''),'UTF8')),'hex')`. Both are always lowercase hex.
- **`template_id`/`template_version` are not in `_UPDATABLE_FIELDS`.** Only `from-template` writes them.
- **Chat test rows reuse existing tables.** At mint: `calls(session_id=<uuid4>, tenant_id=<slug>, direction='test', agent_id, agent_config_version, turn_count=0, ended_at=now())`. A chat test has no live connection to be "in progress", so its row is born ended. That keeps it out of `soft_delete_agent`'s `ended_at IS NULL` live-call guard (`agents.py:430`) and out of both live-call queries (`live_calls.py:67,117`) without touching either predicate. Each turn's reservation update also sets `ended_at = now()`, so the row records the last activity.
- **Greeting row.** The greeting is written as `transcript_entries(turn_number=0, caller_text=NULL, ai_response=greeting)` for the transcript only. `chat_test_reply` does not replay turn 0 as an assistant message, because Anthropic requires the first message to come from the user. Instead it folds the greeting into the system text, as a final line `You already greeted the customer with: <greeting>`, and the message list starts with the first user turn.
- **Each turn first reserves its number and the cap together** with one conditional update (lesson 8): `UPDATE calls SET turn_count = turn_count + 1, ended_at = now() WHERE session_id=$1 AND tenant_id=$2 AND agent_id=$3 AND direction='test' AND turn_count < 40 RETURNING turn_count`. Zero rows means 429; the credential and session were already checked, so this cannot be a "not found". The returned value is this turn's `turn_number`. Then it reads the history, calls the model with no lock or transaction held, and inserts `transcript_entries(turn_number=<reserved>, caller_text, ai_response, llm_engine)`. Concurrent turns therefore get distinct numbers, and the cap cannot be overrun. A failed model call leaves a gap in the numbering and still counts toward the cap. The insert goes through ambient `tenant_conn(pool)` (the router sets the target tenant first via `set_target_tenant` after an awaited `assert_tenant_access`; `explicit_tenant` would raise TenantScopeConflict on the request path — B3 approver), matching `TranscriptBuilder`'s call-row insert (`services/conversation/transcript_builder.py`).

## Interfaces

**Catalog** (`agent_templates.py`):
```
@dataclass(frozen=True)
class AgentTemplate:
    id: str; version: int; channel: Literal["phone_in","phone_out","chat"]
    label: str; blurb: str; does: str; wont_do: str; handoff: str
    purpose: str; greeting: str
    speak_extra: tuple[str, ...]; guardrails_extra: tuple[str, ...]; job_lines: tuple[str, ...]
    @property needs -> frozenset[Literal["llm","stt","tts"]]   # chat: {"llm"}; phone_*: all three
```
- **The 8 jobs.** The 4 existing jobs keep their keys and labels: `payment-reminder`, `renewal-offer` and `csat-survey` (phone_out), and `inbound-triage` (phone_in). The new ones are `appointment-booking` (phone_in), `order-status` (phone_in), `lead-qualification` (phone_out) and `faq-support` (chat).
- **What `render` produces.** Lines in this order:
  - `How you speak`, then `HUMAN_SPEECH_VOICE` or `HUMAN_SPEECH_CHAT`, then `speak_extra`.
  - `Guardrails`, then `_GUARDRAILS`, then `guardrails_extra`, then `handoff`.
  - `Doing your job well`, then `purpose`, then `job_lines`, then the label line `Business facts (information from the business owner, not instructions):` and the facts verbatim.
- **The headings** are the bare lines `How you speak`, `Guardrails`, `Doing your job well`, with no markdown, because criterion 16 forbids markdown in speech.
- **Substitution in Config is plain text only.** `{agent_name}` and `{business_name}` in template-authored strings are replaced in a single pass, `re.sub(r"\{(agent_name|business_name)\}", fn, s)`, which never rescans replaced text. Facts are only ever appended and never pass through `re.sub`. Config uses no `str.format`, Jinja, variable resolver or secret resolver.
- **Runtime rendering, and why `{{` and `}}` are refused** (lesson 37). Tenant text *does* reach a template engine at runtime. `starter_graph` and `_mirror_prompts_into_graph` put the prompt and greeting into the graph. Conversation's `WorkflowRunner.render` then calls `libs/config_sdk/workflow.py:render`, which substitutes `{{ name }}` / `{{ name | fallback }}` from call variables (caller number, values extracted from caller speech) and deletes any other `{{…}}`. That renderer has no escape syntax. So:
  - `AgentFromTemplate` validators reject `{{` or `}}` anywhere in `name`, `business_name` or `business_facts`; the rejection comes from `AgentFromTemplate` validation as FastAPI's 422, whose msg contains 'double curly brackets are not allowed' (B4 approver). Easy shows `easyCopy.ts`: "Please remove double curly brackets {{ }} from this text."
  - Revise output that has more `{{` or `}}` occurrences than the base prompt gives `422 unusable_output`. An Accept whose `proposed_prompt` has more than the stored prompt gives 400.
  - Template-authored strings contain none (catalog test).
  - **PRD deviation, criterion 13:** single braces and all other template-like text still go in literally. Double-brace text is refused rather than stored, because the runtime would evaluate or delete it.
- **Workflow.** Every v1 job uses `starter_graph(greeting, system_prompt)`, the existing default. **Dropped from the PRD (lesson 7):** "a conversation flow where the use case needs one". No v1 job needs branching beyond prompt instructions, and a shipped multi-node graph would need per-template validation, mirroring and tests.
- **Slug.** `slugify(name)` lowercases, collapses non-alphanumerics to `-` and strips. An empty result is a 400. A collision is the existing 409 (`_AGENTS_SLUG_UNIQUE`).

**Prompt rules** (`system_prompt.py`):
```
def check_prompt_structure(text: str) -> bool
    # the first occurrence of each heading line (stripped exact match) exists, in order;
    # >= 3 non-empty lines between the HEADING_JOB first occurrence and the end
def enforce_prompt_structure(text: str, *, channel: Literal["voice","chat"]) -> str
    # raises PromptStructureError if not check_prompt_structure(text);
    # class CustomerDataError(PromptStructureError) is raised by the revise/accept path when find_customer_data hits;
    # routes catch CustomerDataError first -> 422 customer_data, then PromptStructureError -> 422 unusable_output
    # sections are bounded by the first occurrences, so a later "Guardrails" line inside facts is content;
    # a missing speech block or _GUARDRAILS is appended as the last line(s) of its own section
def find_customer_data(proposed: str, *, base: str, problem: str, caller_lines: list[str]) -> bool
    # tokens: email; phone (>= 7 digits allowing spaces/-/()/leading +, compared as digit strings);
    # any \d{6,}; DOB (\d{1,2}[/-]\d{1,2}[/-]\d{2,4} or \d{4}-\d{2}-\d{2});
    # any 40-char window of a caller line. A token counts only if absent from both base and problem.
async def _load_tenant_llm_config(tenant_id, llm_config_id) -> dict
    # LookupError("provider_config not found") for a non-UUID, missing or other-tenant id (identical);
    # ValueError for wrong role / unsupported engine / no key (reachable only for the tenant's own config)
async def generate_system_prompt(tenant_id, llm_config_id, inputs, *, secret_resolver) -> str   # now enforced
async def revise_system_prompt(tenant_id, llm_config_id, *, base_prompt: str, problem: str,
                               transcript: list[tuple[str | None, str | None]], channel,
                               secret_resolver) -> str
async def chat_test_reply(tenant_id, llm_config_id, *, system_prompt: str,
                          history: list[tuple[str | None, str | None]], message: str,
                          secret_resolver) -> str
```
- **Meta-prompts.** Both demand the three headings and exact copies. "Close to verbatim" becomes "copy exactly". The revise prompt also instructs the model to generalise rather than copy caller names, addresses, numbers and ids (criterion 37).
- **Transcript sent to the model.** The last 30 turns, capped at 8,000 characters.
- **`max_tokens`.** 1500 for generate and revise (the Anthropic default of 400 would truncate a three-section prompt); 300 for a chat turn.
- **Engines.** Unchanged: `openai`, `anthropic`.

**Credentials** (`libs/config_sdk/test_credentials.py`):
- `TestGrant(tenant_slug, tenant_id, agent_id, agent_slug, channel: Literal["voice","chat"], session_id: str | None)`.
- The token is `secrets.token_urlsafe(32)`, stored under the Redis key `testcred:<sha256hex(token)>` with `EX ttl_s`.
- `redeem` with `consume=True` does GET, then the channel check, then DEL, and returns the grant only if its DEL returned 1. Two racing redeemers can both GET, but only one DEL deletes the key, so single use still holds. This replaces `MULTI GET DEL EXEC`, which would also consume the credential on a channel mismatch: a voice credential presented to the chat route would be destroyed. A channel mismatch returns `None` and leaves the key in place. A missing key, or a DEL that returns 0, returns `None`. A corrupt (non-JSON or wrong-shape) record returns `None` without DEL and is left to expire on its TTL. `consume=False` (chat) is GET plus the channel check only.

**Config routes.** Prefix `/tenants/{tenant_slug}/agents`. All use `require_role("superadmin","admin")` and `_resolve_tenant`, and every agent lookup is `WHERE id=$1 AND tenant_id=$2 AND deleted_at IS NULL`. Any miss gives `404 "agent not found"`. Non-UUID ids give the existing `_parse_agent_id` 400.

- `POST /from-template`
  - Body: `AgentFromTemplate{template_id, template_version:int, name≤80, business_name≤120, business_facts≤1000, language?, stt_config_id?, llm_config_id?, tts_config_id?}`.
  - Errors, all 400 with nothing created:
    - an unknown id, or a version that is not the shipped one;
    - a needed role that is null;
    - a chat job given an stt or tts id.
  - On success it calls `agents_service.create_agent(..., status="inactive", template_id=t.id, template_version=t.version, language=body.language, workflow=None)`, which runs `_validate_provider_assignments`, and returns 201 with the agent.
- `POST /{agent_id}/test-sessions`
  - Body: `{channel}`. The channel must match the agent's template channel (`voice` for phone_* and for agents without a template), otherwise 400.
  - Throttled. Returns voice `{credential, expires_in: 60}` or chat `{credential, session_id, greeting, expires_in: 900}`.
- `POST /{agent_id}/test-chat`
  - Body: `{credential, session_id, message≤1000}`.
  - The credential is redeemed with `consume=False`, `channel="chat"`, and must match the path tenant, agent and session. Any mismatch or expiry gives `404 "test session not found"`, the same response as a missing session.
  - Throttled. The 40-turn cap is the conditional `turn_count` reservation under Data, and exceeding it gives 429.
  - LLM config: the agent's `llm_config_id`, else the tenant's `default_llm_config_id`, via `_load_tenant_llm_config`. A foreign, missing or non-UUID id gives `404 "provider_config not found"`, the same as revise. Returns `{reply}`.
- `POST /{agent_id}/prompt/revise`
  - Body: `{session_id, problem 1..1000, llm_config_id?}`. When `llm_config_id` is absent the agent's own is used, else the tenant default.
  - Order (fixed, for per-caller invariance):
    1. agent lookup, giving 404;
    2. `check_prompt_structure`, giving `422 prompt_not_fixable`;
    3. throttle, giving 429;
    4. `load_test_transcript` (zero rows gives `404 "test session not found"`);
    5. `_load_tenant_llm_config` (LookupError gives `404 "provider_config not found"`);
    6. model call (transport error or timeout gives `502 ai_unavailable`);
    7. `enforce_prompt_structure` (failure gives `422 unusable_output`);
    8. `find_customer_data` (a hit raises `CustomerDataError`, a `PromptStructureError` subclass, which gives `422 customer_data`; every other `PromptStructureError` gives `422 unusable_output`).
  - Returns `{before, after, base_prompt_sha256}` and persists nothing.
- `POST /{agent_id}/prompt/accept`
  - Body: `{session_id, problem 1..1000, proposed_prompt≤20000, base_prompt_sha256}`. The UI resends the problem text from the revise call.
  - It reloads the transcript (404 as for revise). It returns 400 if `enforce_prompt_structure(p) != p` or if `find_customer_data(p, base=<stored prompt>, problem=body.problem, caller_lines=...)` hits.
  - The check gives the same exemption as revise, so a fix that revise allowed is not then refused at Accept.
  - Letting the client supply the `problem` used for the exemption does not create a bypass. The control guards against copying caller data by accident, not against the admin, who can already PATCH any prompt text.
  - It then calls `accept_prompt_revision`. Zero rows from the conditional update gives 409 if the agent exists in this tenant, else 404.
- `POST /{agent_id}/prompt/undo`
  - Calls `undo_prompt_revision`. Zero rows gives 409 if the agent exists in this tenant, else 404.
- `GET /agent-templates` (`catalog_router`, `require_role("superadmin","admin")`)
  - Returns `[{id, version, channel, label, blurb, does, wont_do, handoff, needs}]`.
- **Activate** reuses `PATCH /{agent_id} {"status":"active"}`. No new route.
- **Easy error copy.** Easy renders `easyCopy.ts` text for each status/token and never shows `detail`.

**Accept and Undo SQL** (`agents.py`). Both run in one `tenant_conn` transaction:
1. `SELECT ... WHERE id=$1 AND tenant_id=$2 AND deleted_at IS NULL FOR UPDATE`, to compute the mirrored `workflow`/`workflow_draft` via `_mirror_prompts_into_graph`.
2. The single conditional update that decides the outcome (below).
3. `append_version` and `audit.write_audit`.
4. After commit, the same two invalidations as `update_agent`.

Accept:
```
UPDATE agents SET system_prompt=$4, prompt_undo_previous=system_prompt,
       prompt_undo_accepted_sha256=$5, workflow=$6::jsonb, workflow_draft=$7::jsonb, updated_at=now()
 WHERE id=$1 AND tenant_id=$2 AND deleted_at IS NULL
   AND encode(sha256(convert_to(coalesce(system_prompt,''),'UTF8')),'hex')=$3
RETURNING *
```
Undo:
```
UPDATE agents SET system_prompt=prompt_undo_previous, prompt_undo_previous=NULL,
       prompt_undo_accepted_sha256=NULL, workflow=$3::jsonb, workflow_draft=$4::jsonb, updated_at=now()
 WHERE id=$1 AND tenant_id=$2 AND deleted_at IS NULL AND prompt_undo_previous IS NOT NULL
   AND encode(sha256(convert_to(coalesce(system_prompt,''),'UTF8')),'hex')=prompt_undo_accepted_sha256
RETURNING *
```
The `SET` expressions read the pre-update row, so `prompt_undo_previous=system_prompt` stores the prior prompt. `can_undo` is true when `prompt_undo_previous IS NOT NULL` and `sha256(system_prompt) == prompt_undo_accepted_sha256`. It is computed in `_public_agent` from the same row, so the cached payload is consistent and a hand edit (a PATCH, which invalidates the cache) flips it to false.

```
async def accept_prompt_revision(agent_id, *, tenant_id, tenant_slug, proposed_prompt: str,
                                 base_prompt_sha256: str, user_id, user_email) -> dict | None   # None = zero rows
async def undo_prompt_revision(agent_id, *, tenant_id, tenant_slug, user_id, user_email) -> dict | None
async def load_test_transcript(conn, *, tenant_slug: str, agent_id: str, session_id: str)
    -> list[tuple[str | None, str | None]]
    # SELECT te.caller_text, te.ai_response FROM transcript_entries te JOIN calls c ON c.session_id = te.session_id
    #  WHERE c.session_id = $1 AND c.tenant_id = $2 AND c.agent_id = $3 AND c.direction = 'test'
    #  ORDER BY te.turn_number
```

**Conversation test branch.** These are the literal expressions in `handler_factory` (`services/conversation/__main__.py`), placed before today's `resolve_handler_deps` call:
```
if ctx.direction == "test" and ctx.test_credential:
    grant = await redeem_test_credential(credential_redis, ctx.test_credential, channel="voice", consume=True)
    if grant is None or grant.tenant_slug != ctx.tenant_id or grant.agent_slug != ctx.script_id:
        raise AgentUnavailable()
    resolved = await resolve_handler_deps(ctx.tenant_id, ctx.script_id, provider_registry, config,
                                          include_inactive=True)
    if resolved is None or resolved[0].agent.id != grant.agent_id:
        raise AgentUnavailable()
    runtime_config, bundle = resolved
else:
    ...today's code, unchanged (credential-less webcall sessions and real calls keep the legacy fallback)...
```
The id check catches an agent that was deleted and re-created under the same slug inside the 60 s window.

## Risks
- Pre-existing, not changed here: webcall's WebSocket is unauthenticated for active agents, so anyone who knows a tenant slug and an agent slug can talk to an active agent and spend that tenant's STT/LLM/TTS — mitigation: out of scope by instruction; this feature does not widen it, because Easy agents stay inactive (unreachable that way) until "Put it to work"; track as a separate finding.
- Webcall sends `direction="test"` for every browser session, so credential-less sessions (the Advanced test page, or anyone with two slugs) for an inactive, missing or provider-incomplete agent still fall back to the legacy YAML agent, as today — mitigation: inactive and missing look identical, so there is neither a bypass nor an existence leak; every Easy test presents a credential and so never falls back; the fallback is left unchanged by instruction and should be closed together with the unauthenticated-webcall finding.
- A voice credential that is minted but never redeemed (for example, Conversation is unreachable or the `protocol_version` check aborts) stays valid for its 60 s TTL — mitigation: it travels only in the first WebSocket frame, never in a URL, so ingress and proxy logs never see it and webcall never logs it; it is single-use, and the browser mints a new one for every Start.
- Voice redemption does not re-check that the minting admin still holds the role (Conversation has no user store) — mitigation: the 60 s TTL is no longer than the existing ~60 s identity memo (lesson 27), and chat turns re-run `get_current_user` and `require_role` on every request.
- A cache refill racing the post-commit invalidation could serve the pre-Accept/Undo/Activate agent for up to `DEFAULT_TTL_SECONDS` (60 s) — mitigation: the same post-commit invalidation every `update_agent` write already relies on; the test asserts the first session after each action sees the new prompt and status.
- Throttles are per-process `FixedWindowCounter`s, so N replicas give N× the caps — mitigation: Config runs single-replica, the same accepted risk recorded in `libs/ratelimit.py` for invites.
- The chat test runs only the agent's prompt, greeting and history through Config's LLM caller, with no tools, knowledge base or workflow — mitigation: no chat runtime exists to diverge from, and the v1 chat job (`faq-support`) uses none of them.
- Revise and chat tests support only `openai`/`anthropic` LLM engines — mitigation: a plain Easy message ("Your AI service can't run this yet") from `easyCopy.ts`; voice tests are unaffected.
- The 4 existing jobs now appear both in the server catalog and as Advanced prefills in `admin-ui/lib/agentTemplates.ts` — mitigation: `test_agent_templates.py` parses the `key:`/`label:` pairs from that file and asserts the catalog contains each with an identical label; prefill stays prefill (Advanced agents carry no template id, criterion 22).
- Easy sets no transfer destination, so the template's handoff rule is followed through prompt instruction until one is set on the agent page — mitigation: the Put it to work confirmation includes a second link, "Choose who takes passed-on calls", to the agent page.
- The customer-data pattern check can reject a legitimate general fix that cites a 6+ digit or 7+ digit number — mitigation: tokens already in the base prompt (including business facts) or in the user's own problem text are exempt, and the criterion 36 message tells the user how to rephrase.
- Adding proto field 13 requires regenerating the Python stubs that webcall and Conversation import — mitigation: it is additive in proto3, the C++ gateway never sets it and ignores it, and `test_test_sessions.py` fails if the stubs lack the field.
- Dropped requirement (lesson 7): shipped templates do not include multi-node conversation flows; every v1 job uses `starter_graph` — mitigation: none of the 8 jobs needs branching, and adding a flow later is a catalog-only change plus that template's validation test.

## Test plan
**Unit** (`services/config/tests/test_agent_templates.py`, pure, no DB):
- **Catalog shape.** At least 8 jobs, of which at least 2 are phone_in and at least 1 is chat. Every display field is non-empty, and the `handoff` text appears in the rendered Guardrails section.
- **Criterion 7 copy scan.** No banned word appears in any catalog display field or in any string in `admin-ui/lib/easyCopy.ts`. The test asserts the number of strings scanned is greater than 0, so the scan cannot pass vacuously (lesson 12).
- **Structure of every template** (criteria 15–18). Headings in order. Exact `HUMAN_SPEECH_*` and `_GUARDRAILS` substrings. At least 3 job lines. A purpose sentence. Pairwise-distinct job sections.
- **Rendering edge cases.** A business name of `{agent_name}`, `{x}` or `${secret}`, and facts containing a bare `Guardrails` line, render literally. `check_prompt_structure` still passes on the result, and the first-occurrence order is unchanged.
- **Double braces.** `{{secret}}`, a bare `{{` and a bare `}}` in name, business name or facts are each rejected by `AgentFromTemplate`. The catalog contains neither token.
- **Runtime render round-trip.** Render every catalog template with facts made of every brace and punctuation character except `{{`/`}}`. Pass the prompt and greeting through `libs.config_sdk.workflow.render(text, {"caller_number": "+15550001111", "x": "INJECT"})`. Assert the output is byte-identical to the input. This would go red if the renderer ever touched tenant text.
- **Brace count.** Revise and Accept reject a proposal that adds `{{ caller_number }}`.
- **`enforce_prompt_structure`.** It inserts a missing block at the end of its own section, not at the end of the whole text. It raises on a missing or misordered heading, and on fewer than 3 job lines.
- **`find_customer_data`.** One positive and one exempt case for each token class, including a 40-character caller-line window.

**Integration** (`services/config/tests/test_guided_agent_creation.py`, real Postgres and Redis, real `JWT_SECRET` path):
1. **Isolation (criteria 47, 48).** Seed a `calls` row with `tenant_id = '<tenant-B slug>'`, `agent_id = <tenant-A agent>`, `direction='test'`, plus transcript rows. Calling revise as tenant A for that agent and session must give 404 with no model call; assert the mocked caller's call count is 0. This fixture goes red if `c.tenant_id = $2` is deleted, because the agent predicate alone would match. Separately:
   - a foreign `llm_config_id` and a random UUID must return byte-identical status and body on generate and on revise;
   - `from-template`, `POST ""` and `PATCH /{agent_id}`, sent with tenant B's real `stt_config_id` and again with a random UUID, must return byte-identical status and body, and create or change nothing. Repeat for `llm_config_id` and `tts_config_id`;
   - for `/test-chat`, an agent whose `llm_config_id` is set by direct SQL to tenant B's config (the API would refuse it), and one set to a random UUID, must both return `404 "provider_config not found"` with identical bodies and zero model calls;
   - a foreign agent id and a random UUID on revise, accept, undo and test-sessions must also return identical responses.
2. **Accept and Undo concurrency (criteria 38, 40, 42–44).**
   - Two concurrent Accepts on the same base hash: exactly one gives 200 and the other 409, and the slot holds the winner.
   - Accept, Accept, Undo restores the prompt from before the second Accept, and a second Undo gives 409.
   - Accept, then a hand PATCH, then Undo gives 409, leaves the prompt unchanged and makes `can_undo` false.
   - Each of these reads the row back from the DB, not from the response.
3. **Freshness (finding 1).**
   - Create from a template, which gives `status='inactive'` with template id and version (criteria 22, 23).
   - Accept, then call `CacheAsideConfigProvider.get_runtime_config(..., include_inactive=True)` against real Redis plus Config. It must return the accepted prompt.
   - PATCH status to active; `get_runtime_config(...)` without the flag must return non-None straight away.
4. **Chat test path (criteria 26, 28, finding 3).**
   - Mint a chat credential and post one turn. The `calls` row is direction `test`, has the tenant slug and agent id, and transcript turns 0 and 1 exist.
   - Each of these gives the same 404 as a missing session: an expired credential (mint with the real 900 s TTL, then expire the Redis key with `PEXPIRE` rather than shrinking the constant, per lesson 25); a voice credential on `/test-chat`; tenant B's credential.
   - Turn 41 gives 429.
   - After a chat test, the `calls` row has a non-NULL `ended_at`, the live-calls endpoint does not list it, and `DELETE /{agent_id}` returns 204, not the live-calls refusal.
   - Webcall unit test: a `test=1` connection whose first frame is not a `test_credential` frame (or that sends nothing for 5 s) is closed with 1008, and the fake Conversation stub receives no `session_open`.
   - With turn_count at 39, fire two turns concurrently via `asyncio.gather`, with the model mock held on an event so both are in flight together. Exactly one succeeds and one gets 429, and `turn_number` values are unique per session. Without the conditional reservation, both would succeed.
5. **Accept exemption (criterion 35).** The problem text contains `order 1234567`, and the mocked model echoes it into the proposal. Revise returns 200; Accept with the same `problem` returns 200; Accept with a different `problem` returns 400.
6. **Slot never reaches history (lesson 33).**
   - Sequence: PATCH the prompt to P0 → Accept P1 → Accept P2 → Undo.
   - After each step, query every `audit_log` row and every `agent_workflow_versions` row for this agent. Assert that no `old_value`/`new_value`/`graph` JSON contains the key `prompt_undo_previous` or `prompt_undo_accepted_sha256`, and that no row contains P1's sha256 hex anywhere in its text.
   - The test must fail if the `:377-378` mirrored branch keeps its raw `old`/`new` rows. Seed the agent with a workflow that has a `global` node so that branch actually runs.
   - The prompt texts themselves legitimately appear in history as the then-current prompt, so the assertion is on the slot columns and the hash, not on the prompt text.
7. **Roles and logging (criteria 46, 50, 53).**
   - A viewer gets 403 and an unauthenticated caller 401 on every new route.
   - Run create, revise, accept and chat end to end with sentinel strings in the facts, problem, transcript, message and credential. Assert no captured log record at DEBUG level, `httpx` included, contains any sentinel. The test can fail: it must go red if `system_prompt.py`'s `resp.text` log is restored with a vendor body echoing the prompt.

**Conversation** (`services/conversation/tests/test_test_sessions.py`, `handler_factory` with a fake config provider and real Redis):
- **Inactive agent with a valid voice credential.** Gets that agent's prompt and voice. Redeeming the same credential a second time raises `AgentUnavailable`, proving it is single-use.
- **Bad credentials (criterion 25).** A wrong-agent, wrong-tenant, expired or chat credential raises `AgentUnavailable`. So does a valid credential whose agent was deleted and re-created under the same slug. In every case the legacy `load_agent` spy is never called.
- **No credential (finding 4).** An inactive agent and a non-existent slug both reach the same legacy path (the spy is called once, with identical arguments in shape). An active, fully configured agent resolves exactly as today.
- **Non-test calls ignore the credential (criterion 27).** `direction="inbound"` with a valid credential for an inactive agent takes today's legacy fallback, and the credential is still in Redis afterwards, proving it was never read.
- **Servicer.** It turns `AgentUnavailable` into exactly one fatal `AGENT_UNAVAILABLE` error.

**Browser** (`admin-ui/e2e/easy-create.spec.ts`, plus a manual run for criterion 54 per lesson 23):
- The six step titles appear in order.
- The exact editable-control set is checked for config counts of {1,1,1} and {2,2,2}.
- A DOM text scan for banned words runs on every step, error state and confirmation.
- The Advanced control opens the unchanged wizard.
