# Call flows

## Runtime

An agent whose `call_flow_id` is set is driven by the IVR runtime
(`services/conversation/callflow/`) instead of its own conversational
workflow — until the flow hands the call to an `agent` node, at which point
a freshly built `PipelineConversationHandler` takes over exactly as if that
agent had been dialed directly, seeded with whatever the flow collected.

### Wire arm

A caller keypress arrives as a `DtmfDigit` message on `GatewayMessage`'s
`oneof payload` (field 9):

```protobuf
message DtmfDigit {
  string session_id = 1;
  string digit      = 2;  // exactly one of "0".."9", "*", "#"
  string trace_id   = 3;
}
```

The gateway/vobiz side detects the keypress (RFC2833 / SIP INFO / websocket
`"dtmf"` event) — the service never derives digits from audio. `vobiz`
flushes any pending audio (`_flush_pending_audio_now()`) before forwarding
the digit, so digit-after-audio ordering is preserved on the single
`_grpc_write_queue` writer. Nothing new flows server → client:
`ServiceMessage` is unchanged, and all flow-driven audio (including a
keypress's own response) leaves through the same `TtsStarted`/`TtsChunk`/
`EndCall`/`TransferRequest` messages every other handler uses, sent
out-of-band when no inbound message drove them (a menu timeout, for
example).

### Resolution order

1. `handler_factory` resolves the DID-routed agent into a `RuntimeConfig` +
   `ProviderBundle` via `resolve_handler_deps()`, exactly as it does today.
2. If `runtime_config.agent.call_flow_id` is unset, nothing changes — the
   agent's own conversational workflow runs. This is the majority path and
   costs one `if` check.
3. Otherwise, `resolve_call_flow(runtime_config, config)` resolves the
   published flow through `IConfigProvider.get_call_flow()` (Redis-first,
   HTTP fallback to Config Service's `GET /tenants/{tenant_slug}/call-flows/
   {call_flow_id}/published` — a Tier 2 route whose path-segment tenant is
   what RLS binds against, never a tenant taken from the flow row itself)
   and parses the graph (`graph_for_flow()`, cached in-process by
   `(tenant_slug, call_flow_id)` and versioned by `config_version`, mirroring
   `WorkflowRunner`'s `_GRAPH_CACHE`).

   **Tenant isolation on this read is two layers, not one.** The Conversation
   service account is platform-scoped (`tenant_id IS NULL`), so it passes the
   route's own tenant-access check for *any* `{tenant_slug}` in the path —
   RLS is what would otherwise be the only thing standing between tenant B
   and tenant A's flow. `get_published_for_runtime()` (`services/config/
   call_flows.py`) therefore also carries an explicit `tenant_id = $N`
   predicate on each of its three reads (`call_flows`, `agents`,
   `provider_configs`), resolved from the path's `{tenant_slug}` before any
   of them run — a wrong-tenant `call_flow_id`, `agent_id` or
   `tts_config_id` is rejected by that predicate whether or not RLS is also
   in effect. **This is defence in depth, not a replacement for RLS**: the
   predicate protects a misconfigured or bypassed database role; RLS is
   still what protects every other statement Config Service runs, including
   ones this feature doesn't touch. Operationally, both layers assume
   Config Service's own database connection is the app role
   (`yuviz_app`, `NOBYPASSRLS` — see `docs/rls-tenant-isolation.md`), never a
   superuser: a superuser connection bypasses row-level security outright,
   and the explicit predicate is what still holds in that case, not a
   substitute for connecting correctly in the first place.
4. A successful resolution returns `(graph, flow)`. `flow.resolved_tts_config_id`
   — already validated same-tenant and `role='tts'` by Config Service — is
   the *only* value that may reach `CallFlowRunner(graph, tts_config_id=...)`;
   the graph's own author-supplied `start.tts_config_id` is never read by the
   runtime. `flow.agent_slugs` is likewise the only channel an `agent` node's
   handoff may resolve through — a cross-tenant `agent_id` is structurally
   absent from that map, not rejected by a comparison.
5. `CallFlowConversationHandler` wraps the runner and drives it: synthesizing
   prompts, arming/cancelling `Listen` timers off the synthesized PCM's own
   duration, and delegating to a `PipelineConversationHandler` built via the
   same `resolve_handler_deps()` path once the flow reaches an `agent` node.

### Degradation table

| Failure | Behavior |
|---|---|
| No `call_flow_id` on the agent | Conversational workflow runs, unchanged (AC 2). |
| `get_call_flow()` misses (deleted, unpublished, non-inbound, wrong tenant — rejected by `get_published_for_runtime()`'s explicit `tenant_id` predicate, backed by RLS) | `resolve_call_flow()` returns `None`, logged; falls through to the conversational agent (OQ4, see below). |
| `parse_graph()` raises `CallFlowValidationError` | Same as above — `resolve_call_flow()` never raises, `None` is the one signal `handler_factory` keys off. |
| Any other exception during resolution | Caught, logged (`log.exception`), `None` returned — mirrors `agent_resolver.resolve_handler_deps()`'s own contract byte-for-byte. |
| `menu`/`collect` retries exhausted | `Hangup("retries_exhausted")` — the caller is already on the line, so looping forever is worse than a clean goodbye. |
| Transition budget (`MAX_NODES`) exceeded | `Hangup("flow_budget")`, logged at ERROR — a looping flow cannot hold a channel forever. |
| `agent` node names an id absent from `agent_slugs` (cross-tenant — rejected by the same explicit `tenant_id` predicate, soft-deleted, or inactive) | Handoff cannot resolve; call ends rather than reaching another tenant's agent. |
| Handler exception mid-node (TTS provider down, etc.) | Caught by the driver task; ends the call cleanly rather than hanging the stream. |
| A publish mid-call | Invisible to that call — the graph is pinned at session open (AC 4); the call finishes on the version it started with. |

### Open questions — proposed defaults, pending sign-off

These three defaults are implemented and shipped, but each is a genuine
product decision the PRD asked to have confirmed — not something this
runtime doc closes on its own:

- **OQ2 — collect auto-submit / retry exhaustion.** Proposed: exactly as the
  PRD assumed — a terminator or `max_digits` submits, a short/timed-out
  attempt replays, and exhausting `max_retries` hangs up
  (`CallFlowRunner._submit_collect()` / `_invalid_attempt()`).
- **OQ3 — blank/empty `terminator`.** Proposed: "no terminator" — submission
  is then on `max_digits`, or at timeout on `len(buffer) >= min_digits`
  (`CallFlowRunner._collect_digit()`'s terminator comparison). Note:
  `parse_graph()` coerces a falsy `terminator` to `"#"`
  (`libs/config_sdk/callflow.py`), so this branch is not reachable through
  a graph an author can currently publish — it is a guard for a graph that
  bypassed `parse_graph()`, or a future coercion change, not a behavior in
  production today.
- **OQ4 — call-flow resolution failure.** Proposed: fall through to the
  conversational agent rather than playing an apology and hanging up — the
  DID already resolved a working agent, and a config-plane hiccup should
  not cost a live call. This is the one default here where the caller
  experience genuinely differs from the alternative (silent working call
  vs. apology-then-hangup), and reversing it is confined to the
  `resolve_call_flow() is None` branch in `handler_factory`
  (`services/conversation/__main__.py`).
