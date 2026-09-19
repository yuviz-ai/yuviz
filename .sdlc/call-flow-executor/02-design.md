# Design: Call-flow executor (IVR runtime for published call flows)

## Approach

The executor is a *handler*, not a new layer. `services/conversation/__main__.py`'s
`handler_factory(ctx)` already resolves the DID-routed agent into a `RuntimeConfig` +
`ProviderBundle` via `resolve_handler_deps()`; if that agent carries a `call_flow_id` we build
`CallFlowConversationHandler` — an `IConversationHandler` that walks the pinned graph and
*delegates* to a freshly built `PipelineConversationHandler` the moment the flow reaches an
`agent` node. `ConversationSession`, the FSM and every existing branch of `servicer.py`'s turn
loop stay unaware of the swap, so the IVR costs nothing on the (majority) no-flow path: AC 2 is a
single `if runtime_config.agent.call_flow_id is None` check.

Graph fetching follows the SDK's existing cache-aside shape verbatim — a new
`IConfigProvider.get_call_flow()` served by `RedisConfigRepository`/`HttpConfigRepository`
against a new `callflow:{tenant_slug}:{call_flow_id}` key written by Config Service, plus a
module-level in-process `_FLOW_CACHE` keyed by `(tenant_slug, call_flow_id)` and versioned by
`call_flows.config_version`, exactly mirroring `WorkflowRunner`'s `_GRAPH_CACHE`. Conversation
never opens a Postgres connection for this: the RLS-protected read lives in Config Service,
reached through a **Tier 2** `/tenants/{tenant_slug}/call-flows/{id}/published` route, so the
DID-resolved tenant is a *path segment* that `bind_path_tenant` turns into the RLS target before
the row is looked up. A flow id in hand is therefore never sufficient to load a flow.

The runner itself is provider-free and synchronous (same posture as `WorkflowRunner`: "no
audio/providers, dry-run friendly"): it takes digits and timeouts in and returns a list of
`Action`s out. All audio, timers and gRPC egress live in the handler. Two pieces of plumbing are
genuinely new and unavoidable: a `dtmf` arm on `GatewayMessage` (+ vobiz forwarding), and an
out-of-band egress queue so a node can speak or hang up when *no* inbound message triggered it
(a menu timeout). The alternative — clocking the IVR off inbound `audio_chunk` frames, which the
servicer already turns into responses — was rejected: vobiz drops/holds frames around TTS
playback and barge-in, so flow correctness would depend on media cadence.

## Changes

| File | Change | Why |
|---|---|---|
| `proto/voiceai/v1/conversation.proto` | New `message DtmfDigit`; ninth `oneof payload` arm `DtmfDigit dtmf = 9` | A keypress has no wire representation today (PRD Constraints); eight arms used, 9 is next free |
| `services/conversation/generated/voiceai/v1/*` | Regenerate from the proto (existing `scripts/` codegen step) | Generated sources are committed in this repo |
| `services/vobiz/bridge.py` | `dtmf` branch (~342) calls `_flush_pending_audio_now()` then `self._grpc_write_queue.put_nowait(pb.GatewayMessage(dtmf=...))`; **the existing `log.info("vobiz: dtmf digit=%s …")` is changed to log presence only** | `_grpc_write_queue` is documented as the only writer; flushing first keeps digit-after-audio ordering |
| `libs/config_sdk/callflow.py` | `CallFlowNode.sensitive: bool = False` (collect only) + `parse_graph()` read + `to_dict()` write | A collected PIN/PAN has no way to be marked today, so it cannot be kept out of persistence |
| `admin-ui/components/callflow/CallFlowPanel.tsx` | One checkbox on the `collect` inspector ("this value is sensitive — don't store it") | A flag no author can set is a control that never engages |
| `libs/config_sdk/models.py` | `Agent.call_flow_id: str | None = None`; new frozen `CallFlow` DTO | `agents.call_flow_id` already ships in the cached agent payload (`get_agent` does `SELECT a.*`); only the DTO mapping is missing |
| `libs/config_sdk/providers/cache_aside.py` | Map `call_flow_id=row.get("call_flow_id")` in `_agent()`; implement `get_call_flow()` (Redis-first, HTTP fallback, dict→`CallFlow`) | The accessor belongs on the SDK, not in a direct `services/config` import |
| `libs/config_sdk/interfaces.py` | `IConfigProvider.get_call_flow(tenant_slug, call_flow_id)`; `IConfigRepository.fetch_call_flow(...)` | Constraint 4: the executor depends only on `IConfigProvider` |
| `libs/config_sdk/repositories/redis_repository.py` | `fetch_call_flow()` → `callflow:{tenant_slug}:{call_flow_id}` | Same key-per-object shape as `agent:{tenant}:{slug}` |
| `libs/config_sdk/repositories/http_repository.py` | `fetch_call_flow()` → `GET /tenants/{tenant_slug}/call-flows/{id}/published` | Same `_get()` (service-account login/401-retry) path as `fetch_agent` |
| `services/config/call_flows.py` | `get_published_for_runtime(tenant_slug, call_flow_id)` (cache-aside read + payload build); `_runtime_cache_key()`; invalidate that key in `publish()`, `update_call_flow()`, `delete_call_flow()` | One tenant-scoped read that resolves agent-node slugs and the start voice *inside* RLS |
| `services/config/routers/call_flows.py` | `GET /{call_flow_id}/published` on the **tenant-scoped** router | Tier 2 binds the path tenant as the RLS target (AC 31/32) |
| `services/conversation/callflow/__init__.py` (new) | Package marker | Sibling of `workflow/`, per the request's binding decision |
| `services/conversation/callflow/runner.py` (new) | `CallFlowRunner` + `Action` types + `_FLOW_CACHE`/`graph_for_flow()` | The graph walk, retry accounting and digit buffer — no audio, no I/O |
| `services/conversation/callflow/handler.py` (new) | `CallFlowConversationHandler` — TTS, timers, egress queue, delegation | Where the runner's `Action`s become audio/`EndCall`/`TransferRequest` |
| `services/conversation/callflow/resolver.py` (new) | `resolve_call_flow(runtime_config, config) -> tuple[CallFlowGraph, CallFlow] | None` — the `CallFlow` is what carries `resolved_tts_config_id` to the construction site | The single never-raises seam (ACs 27, 28) mirroring `agent_resolver.resolve_handler_deps()` |
| `services/conversation/session.py` | `HandlerResponse` unchanged; `IConversationHandler` gains `on_dtmf()` and optional `out_responses`; `ConversationSession.push_dtmf()` | Session stays the only thing the servicer talks to |
| `services/conversation/servicer.py` | `dtmf` case in the outer loop; `_emit_response()` helper; `asyncio.wait` over `msg_q` + `handler.out_responses` when that queue exists | A menu timeout must produce audio with no inbound message |
| `services/conversation/pipeline.py` | `initial_variables: dict[str, Any] | None = None` ctor param merged over the call-context `variables` dict; no-op `on_dtmf()`; explicit class attribute `out_responses: asyncio.Queue[HandlerResponse] | None = None` | Reuses `WorkflowRunner`'s existing `variables` channel (AC 24, AC 30) |
| `services/conversation/echo.py` | No-op `on_dtmf()`; explicit class attribute `out_responses: asyncio.Queue[HandlerResponse] | None = None` | Protocol implementers are `echo.py` + `pipeline.py` (rule below) |
| `services/conversation/__main__.py` | In `handler_factory`: if `agent.call_flow_id` → `resolved = await resolve_call_flow(runtime_config, config)`; on `None` today's path unchanged; otherwise `graph, flow = resolved` and construct **exactly** `CallFlowRunner(graph, tts_config_id=flow.resolved_tts_config_id, variables=<call-context dict>)`, wrapped in `CallFlowConversationHandler(runner, ..., agent_slugs=flow.agent_slugs, handoff=<closure building a PipelineConversationHandler for a named agent>)` | One branch, one place — and the only place the validated voice id is passed, written out so there is nothing to infer |
| `services/config/tests/test_call_flows.py` | Cases for the new route/read (see Test plan) | Existing home for call-flow service tests |
| `services/conversation/tests/test_callflow_runner.py` (new) | Runner unit tests | Mirrors `test_workflow_runner.py` |
| `services/conversation/tests/test_callflow_handler.py` (new) | Handler/integration tests with fake TTS + fake clock | Mirrors `test_echo_integration.py`'s servicer-level shape |
| `services/conversation/tests/conftest.py` | Clear `_FLOW_CACHE` alongside `_GRAPH_CACHE` | Module-level cache bleeding across tests is an existing, solved problem here |
| `docs/call-flows.md` | Runtime section: wire arm, resolution order, degradation table | Matches `docs/workflow.md`'s precedent |

**Enumeration rule, not a hand list** (lesson 29): every `IConversationHandler` implementer must
gain `on_dtmf`. The set is derived mechanically, not asserted: `grep -rln "on_speech_ended" services/conversation`
minus the protocol/servicer/session modules → `echo.py`, `pipeline.py`, plus any test doubles the
same grep turns up under `services/conversation/tests/`. A tripwire test enumerates
`IConversationHandler.__annotations__`/method names against each implementer so a future handler
that forgets `on_dtmf` fails at import-time in the suite rather than with a mid-call
`AttributeError`.

## Data

**None.** No DDL. `call_flows`, `call_flow_versions`, `agents.call_flow_id` and their RLS policies
(`database/rls.sql:459-481`) already exist and are sufficient; `call_flow_versions` is not read by
this feature (the executor always wants the current published `call_flows.graph`, pinned by
`config_version` at session open, and AC 4 is satisfied by holding the parsed object — not by
re-reading a version row).

New Redis key (cache only, no schema): `callflow:{tenant_slug}:{call_flow_id}`, written with
`cache.set_json(...)` at `cache.DEFAULT_TTL_SECONDS` (60s) — the same TTL `agent:{t}:{slug}` uses,
with write-through invalidation on `publish`/`update`/`delete`. Explicitly **not** touched:
`did:{did}`, its shape, or its no-TTL policy.

Payload (built once, server-side, under the tenant's RLS):

```json
{
  "id": "<uuid>", "tenant_slug": "acme", "config_version": 7,
  "status": "published", "direction": "inbound",
  "graph": { "version": 1, "nodes": [...], "edges": [...] },
  "agent_slugs": { "<agent-uuid>": "support-bot" },
  "resolved_tts_config_id": "<uuid-or-null>"
}
```

`agent_slugs` maps each `agent`-node `agent_id` to a slug **only** when that id resolves to a row
in `agents` that is same-tenant (RLS does this, not a WHERE clause we could forget),
`deleted_at IS NULL` and `status = 'active'`. An absent key is how ACs 25 and 26 are enforced:
the conversation side never sees an id it could hand a call to, so a cross-tenant `agent_id` is
structurally unreachable rather than rejected by a comparison someone can delete.
`resolved_tts_config_id` echoes `start.tts_config_id` only when it names a same-tenant
`provider_configs` row with `role = 'tts'`, else `null` (AC 8's fallback). This check must live
here: `libs/config_sdk`'s `ProviderConfig` DTO carries no `tenant_id` and
`get_provider_config(provider_id)` takes no tenant argument (`cache_aside.py:173`), so the
conversation side *cannot* validate an id it fetches — a cross-tenant id would resolve to another
tenant's engine, voice and `api_key_ref`, i.e. its secret and its vendor bill.

**The validated id is the only one that reaches the runtime, and it arrives by constructor, not by
graph.** `parse_graph()` keeps the author-supplied `start.tts_config_id` on the parsed node
(`libs/config_sdk/callflow.py:100,138,268`), so the unvalidated value is the *convenient* one and
must be fenced off explicitly: `CallFlowRunner.__init__` takes `tts_config_id` as a **required**
keyword, sourced only from `CallFlow.resolved_tts_config_id`, and `SetVoice` carries that value.
`CallFlowRunner` MUST NOT read `graph.start.tts_config_id` anywhere — the handler's `voice_for()`
is likewise only ever called with the constructor value. Making the keyword required (no default)
is the point: a future call site cannot omit it and silently fall back to the graph. A tripwire
test greps **all of `services/conversation/callflow/` and `__main__.py`'s `handler_factory` block**
(not just `runner.py`) for any `tts_config_id` read off a node or graph object — the call site is
where the wrong value is in scope, so watching only the runner would miss it — and the negative test
below asserts a graph whose `start.tts_config_id` names another tenant's `tts` row yields
`SetVoice(None)` — because Config returned `resolved_tts_config_id = null`, not because the runner
compared anything.

## Interfaces

### Wire

```protobuf
// A single keypress from the caller. The gateway/vobiz side detects it
// (RFC2833 / SIP INFO / websocket "dtmf" event) — the service never derives
// digits from audio.
message DtmfDigit {
  string session_id = 1;
  string digit      = 2;  // exactly one of "0".."9", "*", "#"
  string trace_id   = 3;
}
// inside GatewayMessage.oneof payload:
  DtmfDigit dtmf = 9;
```

Field 9 is the next free number in the oneof (1-8 in use). Adding an arm is backward compatible:
an older peer never sends it; an older *server* would put it in `WhichOneof` → `None` and hit the
existing "Unexpected" log path. `ServiceMessage` is unchanged — nothing new flows S→C.

### Config Service

```
GET /tenants/{tenant_slug}/call-flows/{call_flow_id}/published
  -> 200 {payload above}   | 404 (no such flow, soft-deleted, not published, wrong tenant, unknown tenant)
```
Mounted on `tenant_scoped_router` (`services/config/routers/call_flows.py:29`), i.e. it inherits
`Depends(bind_path_tenant)` + `Depends(require_path_tenant_access)`, `require_role("viewer")`
like the sibling read routes, and `_parse_id()` for the UUID guard. It deliberately does **not**
use `_authorize_flow()` (the Tier 3 helper), because that helper resolves the row *first* and then
pins the RLS target to the row's own tenant — the exact "trust a `call_flow_id` in hand" shape AC
31 forbids. Every negative case returns the same bare 404 with no distinguishing detail text
(lesson 2): a call-flow id must not become a cross-tenant existence oracle.

```python
# services/config/call_flows.py
async def get_published_for_runtime(tenant_slug: str, call_flow_id: Any) -> dict[str, Any] | None
```
Reads `cache.get_json(_runtime_cache_key(tenant_slug, call_flow_id))`; on a miss opens
`tenant_conn(pool)` (ambient target already set by `bind_path_tenant`) and runs one statement
joining `call_flows` → `agents` (for `agent_slugs`) → `provider_configs` (for the voice check),
`WHERE id = $1 AND deleted_at IS NULL AND graph IS NOT NULL AND direction = 'inbound'`; returns
`None` otherwise. No `platform_conn` variant — this function has no legitimate platform-scoped
caller, and not offering one removes the bypass.

### SDK

```python
# libs/config_sdk/models.py
@dataclass(frozen=True)
class CallFlow:
    id: str
    tenant_slug: str
    config_version: int
    graph: dict[str, Any]
    agent_slugs: dict[str, str] = field(default_factory=dict)
    resolved_tts_config_id: str | None = None

# libs/config_sdk/interfaces.py
class IConfigProvider(Protocol):
    async def get_call_flow(self, tenant_slug: str, call_flow_id: str) -> CallFlow | None: ...
class IConfigRepository(Protocol):
    async def fetch_call_flow(self, tenant_slug: str, call_flow_id: str) -> dict[str, Any] | None: ...
```

### Conversation

```python
# services/conversation/callflow/resolver.py
async def resolve_call_flow(
    runtime_config: RuntimeConfig, config: IConfigProvider,
) -> tuple[CallFlowGraph, CallFlow] | None
```
Never raises (bare `except Exception: log.exception(...); return None`), byte-for-byte the
degradation contract of `agent_resolver.resolve_handler_deps()`. Returns `None` for: no
`call_flow_id`; provider miss (deleted / soft-deleted / unpublished / non-inbound / wrong tenant →
404 → `None`); `CallFlowValidationError` from `parse_graph()`; anything else. `None` is the single
branch `__main__.py` keys off, so the "what happens on failure" policy is one `if`.

```python
# services/conversation/callflow/runner.py
_FLOW_CACHE_MAX = 256
_FLOW_CACHE: dict[tuple[str, str], tuple[int, CallFlowGraph]] = {}
def graph_for_flow(flow: CallFlow) -> CallFlowGraph            # raises CallFlowValidationError; resolver catches

@dataclass(frozen=True) class Speak:   text: str
@dataclass(frozen=True) class SetVoice: tts_config_id: str | None
@dataclass(frozen=True) class Listen:  timeout_ms: int; digits_expected: bool
@dataclass(frozen=True) class Store:   name: str; value: str; sensitive: bool = False
@dataclass(frozen=True) class Handoff: agent_id: str
@dataclass(frozen=True) class Dial:    destination: str
@dataclass(frozen=True) class Hangup:  reason: str            # "flow_complete" | "retries_exhausted" | "flow_error" | "flow_budget"
Action = Speak | SetVoice | Listen | Store | Handoff | Dial | Hangup

class CallFlowRunner:
    def __init__(
        self, graph: CallFlowGraph, *,
        tts_config_id: str | None,                 # REQUIRED keyword: CallFlow.resolved_tts_config_id
        variables: dict[str, Any] | None = None,
    ) -> None
    @property
    def node(self) -> CallFlowNode
    @property
    def variables(self) -> dict[str, Any]          # copy, like WorkflowRunner.variables; OMITS sensitive keys
    @property
    def flow_variables(self) -> dict[str, Any]     # copy incl. sensitive keys — in-flow rendering only
    def update_variables(self, values: dict[str, Any]) -> None
    def open(self) -> list[Action]                 # start node -> SetVoice, then advance
    def on_digit(self, digit: str) -> list[Action]
    def on_timeout(self) -> list[Action]
```
`on_digit`/`on_timeout` are pure state transitions returning the actions to perform; the runner
holds `_node`, `_retries` (reset on every node *entry*, per AC's "node-visit" wording),
`_buffer: list[str]`, `_transitions: int`. Prompts are rendered through
`libs.config_sdk.workflow.render(text, vars)` — already the repo's one templating helper, reused
rather than re-implemented. A digit outside `callflow.DTMF_KEYS` is counted and ignored (it neither
branches, buffers, nor consumes a retry) — and logged *without its value*, per the rule below.

**Digit values are never logged, anywhere on this path.** A `collect` node exists precisely to take
PINs, card numbers and account numbers, so a per-digit log line reconstructs the secret in order for
anyone with log-read access. Three concrete places: `services/vobiz/bridge.py`'s existing
`log.info("vobiz: dtmf digit=%s call=%s", …)` becomes `log.info("vobiz: dtmf received call=%s", …)`
(this is a change to a line that exists today, not just a new forward beneath it); the runner's
invalid-digit branch logs `"callflow: non-DTMF input ignored node=%s"` with no value; and the
servicer's `dtmf` case and any `GatewayMessage` trace logging log the arm name and `session_id`
only. Nothing constructs a redacted-but-recoverable form either (no "last digit", no length-only
running log). A grep tripwire in the test suite fails on `digit=%s`-shaped format strings in
`bridge.py`, the servicer and the callflow package.

```python
# services/conversation/callflow/handler.py
class CallFlowConversationHandler:                 # structurally an IConversationHandler
    def __init__(
        self, runner: CallFlowRunner, *, tts: ITTS, sample_rate: int,
        session_id: str, tenant_id: str, call_id: str,
        handoff: Callable[[str, dict[str, Any]], Awaitable[IConversationHandler]],
        voice_for: Callable[[str], Awaitable[ITTS | None]],
        agent_slugs: dict[str, str],
    ) -> None
    out_responses: asyncio.Queue[HandlerResponse]  # the out-of-band egress channel
    async def on_dtmf(self, session_id: str, digit: str) -> None
```
Every other `IConversationHandler` method forwards to `self._delegate` when set, and is an inert
`HandlerResponse()` / no-op before that (so no STT, LLM, transcript or guardrail work happens
while the caller is in the IVR). `handoff(agent_slug, variables)` is a closure over
`__main__.py`'s already-constructed `provider_registry`, `config`, `transcripts`, orchestrator etc.
— it calls the same `resolve_handler_deps()` a normal call uses and passes
`initial_variables=variables`; a `None` result means AC 26 degradation.

```python
# services/conversation/session.py
class IConversationHandler(Protocol):
    async def on_dtmf(self, session_id: str, digit: str) -> None: ...
    out_responses: "asyncio.Queue[HandlerResponse] | None"   # None on handlers that never speak unprompted

class ConversationSession:
    async def push_dtmf(self, digit: str) -> None             # -> handler.on_dtmf, guarded by try/except
    @property
    def out_responses(self) -> "asyncio.Queue[HandlerResponse] | None":
        return getattr(self._handler, "out_responses", None)   # belt: see below
```

`out_responses` is declared on the Protocol *and* landed as an explicit
`out_responses: asyncio.Queue[HandlerResponse] | None = None` class attribute on both existing
handlers (`echo.py`, `pipeline.py` — the Changes table rows above). Neither file has the symbol
today, so without that attribute the property would raise `AttributeError` on every call on the
no-flow majority path (AC 2) instead of reading `None` — the opposite of inert. The `getattr`
default is a second line of defence for a test double or a future handler that forgets the
attribute; the explicit class attribute is what makes the two shipped handlers correct, and the
attribute/`on_dtmf` tripwire test below enumerates the Protocol's members against every
implementer so a missing one fails at suite import rather than mid-call.

Servicer outer loop: when `session.out_responses is None` (echo, pipeline — i.e. every call today)
the loop is byte-identical to now. When it is not None, the single `msg = await msg_q.get()`
becomes an `asyncio.wait({msg_fut, out_fut}, return_when=FIRST_COMPLETED)` over two *persistent*
futures (never re-created while pending, so a completed-but-unread item can't be dropped), and an
out-of-band `HandlerResponse` is emitted through the new `_emit_response()` helper — which
reproduces the `speech_ended` branch's exact egress order: `TtsStarted` → `TtsChunk`s → terminal
empty `is_final=True` chunk → `EndCall` / held `TransferRequest`. That order is load-bearing, not
cosmetic: `servicer.py:437-457` documents that the gateway only consumes a pending end-call when
that turn's TTS finishes playing, so a `hangup` node with no prompt must still emit a zero-length
`is_final` chunk before `EndCall` or the call would hang open until the gateway's own timeout.

### Node semantics (the implementable table)

| Node | Actions | Notes |
|---|---|---|
| `start` | `SetVoice(self._tts_config_id)`, then immediately the next node's actions | Speaks nothing (AC 8); `None` → keep the agent's own TTS. **Never** `graph.start.tts_config_id` |
| `play` | `Speak(prompt)`, then next node's actions | No wait (AC 9) |
| `menu` | `Speak(prompt)`, `Listen(timeout_ms, digits_expected=False)` | Matching edge → that edge, immediately (AC 10) |
| `collect` | `Speak(prompt)`, `Listen(timeout_ms, digits_expected=True)` | Buffer digits; see rules below |
| `dial` | `Speak(prompt)` if set, `Dial(destination)` | Terminal; cold `TransferRequest` |
| `agent` | `Handoff(agent_slugs[agent_id])` or `Hangup`-free degradation | Missing key → resolver-style degradation (ACs 25/26) |
| `hangup` | `Speak(prompt)` if set, `Hangup("flow_complete")` | AC 14 |

`menu`: explicit `timeout`/`invalid` edge → taken immediately, retry counter untouched (ACs 15,
17, 20). No such edge → `Speak(prompt)` + `Listen(...)` again and `_retries += 1`; on
`_retries > max_retries` → `Hangup("retries_exhausted")` (ACs 16, 18, 19).

The two failure philosophies here are deliberately different, and the line between them is
whether a caller is already on the call: a `collect`/`menu` exhaustion happens with the session
open and the caller listening, where looping forever is worse than a clean goodbye, whereas a
config-plane resolution failure happens *before* the flow ever opens, where the DID's
conversational agent is still a perfectly good answer and hanging up would discard a working
call. Same rule stated once: never drop a live caller into a loop, and never refuse a call that
something else can still serve.

`collect`: a `DTMF_KEYS` digit that is not the terminator appends to the buffer; `len(buffer) ==
max_digits` submits immediately (AC 12); terminator with `len >= min_digits` submits (AC 11);
terminator with `len < min_digits`, or timeout with `len < min_digits`, is an invalid attempt using
the menu's un-branched accounting (ACs 21-23). Submit = `Store(variable, "".join(buffer))` +
the single out-edge's actions; `Store` is applied via `runner.update_variables()`, giving
last-write-wins over seeded call-context keys for free (AC 30).

**Sensitive collects never leave the runner.** `collect` gains one field in the SDK node model —
`sensitive: bool = False` (`libs/config_sdk/callflow.py`, read in `parse_graph()`, written in
`to_dict()`, surfaced as a checkbox on the editor's `collect` inspector). `Store` carries it:
`Store(name, value, sensitive)`. When `sensitive` is true the value is held only in the runner's own
`_vars` for edge matching and prompt rendering *inside this flow*, and is excluded from (a) the
`variables` dict passed as `initial_variables` to the delegate at handoff, (b) therefore
`WorkflowRunner.variables`/`extracted_variables()` (`workflow/runner.py:77-80`) and the
`conversation_sessions.extracted_variables` jsonb column
(`transcript_builder.py:359-371`), and (c) any log line or metric label. The default is `false`
because the whole point of a non-sensitive `collect` is to hand the value to the agent (AC 24) —
so the flag is opt-in, and the design states plainly that an unflagged `collect` of a PAN *is*
persisted and rendered into an LLM prompt. `runner.variables` (the property the handler reads to
build `initial_variables`) omits sensitive keys; a second property `runner.flow_variables` returns
them for in-flow rendering, so the redaction is a property boundary rather than a filter every
call site has to remember. **No masked copy is kept.** No consumer in this feature needs one, and a
"last 4" of a 4-digit PIN or a 6-digit OTP is the credential itself — so the single rule is the one
stated for logging above: a sensitive value exists in the runner's `_vars` for the life of the call
and nowhere else, in no form, partial or whole. A support surface that later wants "did the caller
enter something" gets the boolean, never characters. `max_digits` is clamped to
`callflow.MAX_COLLECT_DIGITS`, and a node with `variable` unset stores nothing but still advances.

### Timing

**Exactly one task ever mutates a `CallFlowRunner`.** The timer task and `on_dtmf` (called on the
servicer's loop task) would otherwise both call runner methods and then `await` TTS inside the
resulting action loop, so a keypress landing in the same tick as an expiring timer could advance
twice — matched edge *and* timeout edge, a double retry increment, or a `Handoff` followed by a
`Hangup` for the same node. The handler therefore owns a per-session **flow driver task** reading a
private `asyncio.Queue[_FlowEvent]`, where `_FlowEvent` is `("digit", digit)` or
`("timeout", node_id)`. `on_dtmf()` only does `put_nowait(("digit", digit))`; the timer task only
does `put_nowait(("timeout", self._node_id_at_arm))`. All runner mutation and all action execution
happen in the driver task alone, and a `("timeout", node_id)` whose `node_id` is not the runner's
current node is dropped as stale — which is also what makes cancelling a timer best-effort rather
than load-bearing. This is not the locking the request ruled out: that decision is about per-flow
and per-caller locks *across* calls, and this introduces no lock at all — it removes the second
writer instead. The driver task lives in the same owning `set` as the timers and is cancelled in
`on_session_end`.

`Listen` does not depend on `playback_finished` (the webcall path has no such guarantee, and the
gateway can drop it on barge-in). The handler computes the prompt's duration from the synthesized
PCM it already holds — `len(pcm) / (sample_rate * 2)` seconds, `ITTS` output is S16LE mono
throughout `pipeline.py` — and arms one `asyncio.Task` for `audio_s + timeout_ms/1000`. A digit
pressed *during* the prompt is honoured at once and cancels the timer (Twilio-equivalent barge-in
on keypress). Every timer task is held in one `set`, cancelled on each node transition and in
`on_session_end` (lesson 26: the tasks are owned and torn down by the thing whose lifetime they
share, and the session's own teardown is the join point).

## Open questions — disposition

1. **Live node position — decided, nothing built.** `IConversationHandler.record_live_stage(session_id, stage)`
   already exists for exactly this (`calls.live_stage`, Live Calls Monitoring), so no new surface is
   needed and none is added: `CallFlowConversationHandler.record_live_stage()` is a no-op before
   handoff (it owns no transcript writer — that lives in `PipelineConversationHandler`), and the
   runner exposes `node`/`visited`. A later monitoring change is then a call site, not a runner
   change. Out of scope per PRD; the seam is pre-existing, not invented here.
Items 2-4 below are **design-proposed defaults pending product sign-off** — the PRD asks for
confirmation on each, and each changes what a real caller hears, so this design proposes an
answer and builds the seam, but does not close the question. The proposal is what gets
implemented if no one objects before the plan stage locks; the requester named in the PRD should
confirm or reverse them.

2. **Collect auto-submit / retry exhaustion — proposed: exactly as the PRD assumed** (ACs 12, 21,
   23), pending sign-off. Reasoning: `libs/config_sdk/callflow.py` gives `collect` the same
   `timeout_ms`/`max_retries` pair as `menu` and no failure branch, so the menu's already-documented
   "replay, then give up" is the only reading that doesn't invent a node field, and proceeding past a
   short value would contradict `min_digits` being a validated constraint. Reversal without rework:
   the alternative ("submit a short value and advance anyway") is a different return from
   `CallFlowRunner._submit_collect()`/`_invalid_attempt()` — the same two functions AC 21-23 already
   route through — with no change to the handler, the actions, the SDK or the wire.
3. **Blank/empty `terminator` — proposed: "no terminator"**, pending sign-off. Submission is then on
   `max_digits` (AC 12) or, at timeout, on `len >= min_digits`; below that it is an invalid attempt.
   Reasoning: the runtime must have *some* defined rule for a value the author can type. Note the
   narrowness here: `parse_graph()` coerces a falsy `terminator` to `"#"`
   (`libs/config_sdk/callflow.py:265`, `str(d.get("terminator") or "#")`), so today the empty case
   cannot reach a parsed graph at all and this rule is a guard for a graph that bypassed
   `parse_graph()` or a future coercion change — not a behaviour an author can currently trigger. Reversal
   without rework: the only other sane answers (treat as `"#"`, or reject at publish time) are,
   respectively, one line in the runner's terminator comparison or a validator change in
   `callflow.py` that never reaches the runtime at all.
4. **Default on call-flow resolution failure — proposed: fall through to the conversational agent**
   (ACs 27, 28), pending sign-off, and the one item here where the caller experience genuinely
   differs (silent, working call vs. apology-then-hangup). Reasoning: consistency with AC 2 and with
   `agent_resolver`'s never-reject contract — the DID already resolved a working agent, so playing an
   apology and hanging up turns a config-plane hiccup into a lost customer call. Reversal without
   rework: it is exactly one branch, `resolve_call_flow() is None` in `handler_factory`, which would
   instead build a `CallFlowConversationHandler` over a one-node synthetic `hangup` graph carrying
   the apology prompt — i.e. the alternative is expressible in the primitives this design already
   ships, with no change to the runner, handler, SDK or wire. The apology's wording would then need
   a product answer too (there is no schema column for it), which is the second reason not to close
   this here.

**Webcall DTMF — genuinely deferrable, and deferred.** Nothing in `services/webcall/`,
`admin-ui/lib/useWebCall.ts` or `gateway/src/` mentions DTMF today (verified by grep), so browser
testing of an IVR would need a keypad UI, a webcall control message and a browser→webcall channel
— none of which the PRD's inbound-caller scope requires, and all of which are additive on top of
the `DtmfDigit` arm this design lands. Deferring it does mean the first end-to-end verification of
this feature has to be a real SIP call through vobiz; that is called out in the test plan.

## Risks

- **A cross-tenant flow load.** Mitigation: the only read path is a Tier 2 route whose
  `{tenant_slug}` is the DID-resolved tenant and becomes the RLS target via `bind_path_tenant`
  before the row is fetched; the Tier 3 `_authorize_flow()` helper (which would pin the target to
  the row's own tenant) is deliberately not used, `get_published_for_runtime()` exposes no
  `platform_scoped=True` variant, and the Redis cache key is namespaced by `tenant_slug` so a
  wrong-tenant request cannot be served a right-tenant entry.
- **A cross-tenant agent handoff.** Mitigation: the conversation side never receives a raw
  `agent_id` it could resolve — it only receives `agent_slugs[...]`, populated under the flow
  tenant's RLS, so an `agent_id` from another tenant is absent rather than refused, and it then
  resolves that slug through `resolve_handler_deps(tenant_slug, agent_slug)` scoped to the same
  tenant.
- **A cross-tenant voice — i.e. another tenant's TTS secret and vendor bill.** Mitigation:
  `start.tts_config_id` is honoured only via `resolved_tts_config_id`, validated same-tenant and
  `role='tts'` server-side (the SDK's `ProviderConfig` DTO has no `tenant_id` and
  `get_provider_config()` no tenant argument, so the client cannot check it), and that validated
  value reaches the runtime *only* as `CallFlowRunner`'s required `tts_config_id` keyword. The
  residual risk is a future call site reading the graph-resident id instead; mitigated by the
  keyword having no default, the stated MUST-NOT rule, a grep tripwire, and the negative test that a
  cross-tenant `start.tts_config_id` yields `SetVoice(None)`.
- **Caller-entered digits reconstructable from logs.** Mitigation: no digit value is logged on any
  leg — the pre-existing `bridge.py` line is changed, not extended; runner and servicer log presence
  and `session_id` only; a grep tripwire fails the suite on a `digit=%s`-shaped format string.
- **A PIN or card number persisted in `conversation_sessions.extracted_variables` and shipped to an
  LLM vendor.** Mitigation: the opt-in `collect.sensitive` flag plus the `variables` /
  `flow_variables` property split, so a sensitive value has no route into `initial_variables`,
  `extracted_variables()` or the transcript. Residual and stated plainly: an author who does not
  set the flag still gets today's persistence — the default cannot be flipped without breaking AC
  24's handoff seeding for every ordinary `collect`.
- **A keypress and an expiring timer advancing the same runner twice.** Mitigation: a single flow
  driver task is the only mutator; `on_dtmf` and the timer only enqueue events, and a timeout event
  carrying a stale `node_id` is dropped. No lock is added — the second writer is removed.
- **The 404-for-everything rule leaks nothing but also debugs nothing.** Mitigation: the
  distinguishing reason is logged server-side with `tenant_slug`/`call_flow_id` at INFO; the
  response body stays uniform (lesson 2).
- **The `asyncio.wait` change sits in the servicer's hot loop for every call, and the attribute it
  keys off does not exist on the shipped handlers today.** Mitigation: the new branch is entered only
  when `session.out_responses is not None`, which requires `out_responses = None` to be landed as an
  explicit class attribute on both `echo.py` and `pipeline.py` (Changes table) plus a `getattr`
  default on the session property — without both, the no-flow path raises `AttributeError` rather
  than reading `None`. Test 11 drives real echo and pipeline handlers end-to-end (so the exception
  fails the assertion) and a tripwire enumerates the attribute across every implementer.
- **A dropped out-of-band response on future re-entry.** Mitigation: the two futures are persistent
  locals, never re-created while pending — the classic `asyncio.wait` item-loss bug — and the loop's
  exit path drains `out_responses` before `session.close()`.
- **A timer task outliving its call.** Mitigation: one owning `set`, cancelled on every node
  transition and in `on_session_end`; the handler holds no thread pool and no non-daemon threads
  (lesson 26).
- **A looping flow (menu → menu) burning a channel forever.** Mitigation: `_transitions` budget of
  `callflow.MAX_NODES` (200) per call → `Hangup("flow_budget")`, logged at ERROR. This is a
  constant, not a config knob.
- **An exception mid-node killing the session silently** (AC 29). Mitigation: the handler's
  action-execution loop wraps every runner call and every TTS call in `try/except Exception` →
  `Hangup("flow_error")` + `log.exception`; per-session state only, so one call cannot affect
  another in the same process.
- **DTMF arriving ahead of the audio recorded at the same instant** (the digit bypasses vobiz's
  150 ms `_audio_delay_buf`). Mitigation: forward with `_flush_pending_audio_now()` first — the
  existing barge-in helper — so held audio is released before the digit is enqueued on the
  single-writer queue; nothing in this feature correlates digits with audio anyway.
- **A stale `agent:` cache entry without `call_flow_id`** (60 s TTL, or a payload cached before this
  ships). Mitigation: it reads as `None` → AC 2's untouched path → the call still works, and
  self-heals on the next fetch. It is a delay, not a failure mode.
- **A publish mid-call is invisible to that call** — intended (AC 4), but it also means a broken
  flow republished as a fix does not rescue calls already in it. Accepted: the calls end within
  minutes, and the alternative contradicts the binding pin-at-open decision.
- **Two new modules with no browser-driveable path** (lessons 23, 28): without webcall DTMF the only
  real-world exercise is a SIP call. Mitigation: the handler tests drive the full servicer with a
  fake TTS and an injected clock, and the feature's QA stage must include one live vobiz call.

## Test plan

**Unit — `CallFlowRunner`** (no providers, no I/O; `parse_graph()` fixtures in the file, like
`test_workflow_runner.py`):
1. `menu`: matching digit branches immediately; explicit `timeout`/`invalid` edges are taken with
   the retry counter unchanged even on the Nth visit (ACs 10, 15, 17, 20); un-branched
   timeout/invalid replays and increments; the `max_retries + 1`-th event yields
   `Hangup("retries_exhausted")` — the assertion is on the action, and the negative control is that
   the same sequence one event shorter yields `Speak` (lesson 12: state what makes it fail).
2. `collect`: terminator at `>= min_digits` stores the buffer with the terminator excluded;
   `max_digits` auto-submits without a terminator and without a timeout; terminator below
   `min_digits` and timeout below `min_digits` both replay-and-increment, then hang up at
   exhaustion; empty `terminator` submits on `max_digits`/timeout only (ACs 11, 12, 21-23, Q3).
3. Variable seeding: a `collect` into a name already seeded by call context overwrites it, and
   `runner.variables` shows last-write-wins (AC 30). Non-`DTMF_KEYS` input changes nothing.
3a. Voice: `SetVoice` carries the constructor's `tts_config_id`, and a graph whose
   `start.tts_config_id` names a *different* id than the constructor value (the cross-tenant case,
   where Config returned `resolved_tts_config_id = null`) emits `SetVoice(None)` — the assertion is
   on the emitted action, so it fails if the runner ever reads the node. Paired with a source-level
   tripwire that no runner statement reads `tts_config_id` off a node object.
3b. Sensitive collect: with `sensitive=True`, `runner.variables` omits the key while
   `flow_variables` contains it, and a prompt on a later in-flow node still renders it; with
   `sensitive=False` both contain it. The failure this catches is a filter applied at one call site
   instead of at the property.
4. Terminal-node actions: `dial` emits `Speak` then `Dial`; `hangup` emits `Speak` then `Hangup`;
   the transition budget trips at `MAX_NODES` on a self-looping menu.

**Unit — resolution & cache:**
5. `resolve_call_flow()` returns `None`, and logs, for each of: no `call_flow_id`, provider returns
   `None` (deleted/unpublished/non-inbound), `parse_graph()` raising, and the provider raising an
   arbitrary exception — with an assertion that nothing propagates (ACs 27, 28).
6. `_FLOW_CACHE`: two sessions on the same `(tenant, flow, config_version)` parse once (count
   `parse_graph` calls); a bumped `config_version` re-parses (AC 5); an already-open runner keeps
   walking its own pinned `CallFlowGraph` object after the cache entry is replaced (AC 4); the cache
   evicts at `_FLOW_CACHE_MAX`.
7. Two runners over one cached graph advance independently and share no mutable state — one steps to
   a `collect` and stores a value while the other stays on the menu (AC 6). Failure mode this must
   catch: a runner mutating `CallFlowNode`/`CallFlowGraph` in place.

**Integration — Config Service** (existing httpx/ASGI + RLS test harness):
8. `GET /tenants/{A}/call-flows/{flow_of_A}/published` as the conversation *service account*
   (`tenant_id IS NULL`, `role='viewer'` — lesson 24) returns the payload; the same id under
   `/tenants/{B}/...` returns 404; a tenant-B admin JWT gets 404; both 404s are byte-identical
   (lesson 2). A soft-deleted flow, an unpublished flow and an `outbound` flow each 404.
9. The payload omits `agent_slugs` entries for an agent that is another tenant's, soft-deleted, or
   `status='inactive'`, and nulls `resolved_tts_config_id` for a cross-tenant or non-`tts` provider
   id (ACs 25, 26). Failure mode: a query that resolves agents by id without the tenant scope —
   which this fixture reproduces by planting same-id-shaped rows in both tenants.
10. `publish()` invalidates `callflow:{slug}:{id}`; a read after publish sees the new
    `config_version` (AC 5) and a read before it does not re-hit Postgres (AC 3 — assert on a
    connection/query counter, not on wall time).

**Integration — Conversation servicer** (drive `Converse` with a scripted `GatewayMessage` stream,
fake `ITTS`, injected clock; shape borrowed from `test_echo_integration.py`):
11. An agent with no `call_flow_id` produces the exact same message sequence as today (AC 2). The
    regression this must catch is the `AttributeError` one, so the assertion is not just
    `session.out_responses is None` on a mock: it drives a real `PipelineConversationHandler` and a
    real `EchoConversationHandler` through `Converse` to completion (an `AttributeError` raised from
    the loop's first iteration would abort the stream and fail the message-sequence assertion), and
    a separate parametrized tripwire asserts `"out_responses" in vars(type(h))` for every
    `IConversationHandler` implementer the enumeration rule above yields — which fails today,
    before the attribute is added, and fails again for any handler added later that omits it.
12. A flow whose graph is `start → play → menu(1 → agent) `: the caller's `dtmf("1")` reaches the
    handler, the delegate is built with the collected variables, and the target agent's greeting is
    streamed (ACs 1, 24). Assert the seeded variables land on the delegate's `WorkflowRunner`.
13. A menu timeout with no inbound message emits `TtsStarted`/`TtsChunk`s out of band, and a
    `hangup` node emits the terminal empty `is_final=True` chunk *before* `EndCall` (the ordering
    `servicer.py`'s own comment says the gateway depends on).
14. A `dial` node emits a cold `TransferRequest` with the node's `destination`; a handler exception
    injected mid-node yields a clean `EndCall` and the stream closes normally (AC 29).
14a. Race: a `("digit", "1")` and an already-expired `("timeout", node_id)` enqueued in the same
    tick produce exactly **one** transition — assert the runner's `visited` length and that no
    `Handoff`+`Hangup` pair is emitted. A timeout event carrying a stale `node_id` is dropped
    silently. This test must drive the handler through its real driver task, not call
    `on_timeout()`/`on_dtmf()` inline, or it cannot fail.
14b. Redaction: with a `sensitive` `collect` feeding an `agent` node, the delegate's
    `WorkflowRunner.variables` does not contain the collected value and `extracted_variables()`
    returns nothing for it; and `caplog` over the whole servicer+bridge exercise contains no
    substring equal to the digits keyed in (the negative control: the same test with a
    non-sensitive collect *does* show the value on the delegate, proving the assertion is live).
15. `on_session_end` leaves no pending tasks — assert `asyncio.all_tasks()` returns to its
    pre-call set, which fails if a `Listen` timer is not cancelled.

**Wire:**
16. `bridge.py`: a `{"event":"dtmf","dtmf":{"digit":"7"}}` websocket event enqueues exactly one
    `GatewayMessage(dtmf=DtmfDigit(digit="7"))` on `_grpc_write_queue`, after any held audio, and an
    event with a missing/empty digit enqueues nothing. `caplog` records emitted by this
    branch — filtered to the bridge logger and the assertion made against each record's own
    `getMessage()`, so a session/call uuid containing a `7` cannot make it flaky — contain no `"7"`
    (fails against today's `dtmf digit=%s` line), and a grep tripwire over `bridge.py`,
    `servicer.py` and `services/conversation/callflow/` rejects `digit=%s`-shaped format strings. Failure mode this catches: the current
    log-only branch — the test asserts on the queue, which is empty today.
17. Proto round-trip: `GatewayMessage(dtmf=...).WhichOneof("payload") == "dtmf"`, and a serialized
    message from the eight pre-existing arms still parses unchanged.
