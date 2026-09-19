Call-flow executor: execute a published IVR call flow against a live inbound call.

Context gathered from the codebase:
- The call-flow graph model + validator already exist at libs/config_sdk/callflow.py (nodes: start/play/menu/collect/dial/agent/hangup; terminal: dial/agent/hangup; DTMF keys plus menu timeout/invalid branches). Authoring, draft/publish and version history already exist (services/config/call_flows.py, call_flows + call_flow_versions tables).
- The gap: the published flow never reaches the runtime. libs/config_sdk/interfaces.py IConfigProvider has get_runtime_config/get_prompt/get_voice/get_tools but no get_call_flow.
- DID -> tenant/agent resolution already exists Redis-first with write-through and ttl=None under key did:{did} (services/config/phone_numbers.py get_by_did, read by the C++ gateway's PhoneRoute::from_redis).
- Number -> flow binding exists transitively: phone_numbers.agent_id -> agents.call_flow_id.
- The conversational WorkflowRunner (services/conversation/workflow/runner.py) is the shape to mirror: per-call instance holding the active node, module-level graph cache keyed by (agent, config_version).

Decisions already made by the user (do not re-litigate):
- Python, in-process in services/conversation as a CallFlowRunner sibling to WorkflowRunner. NOT a separate service and NOT Java. Reason: the `agent` node hands the live media stream to a conversational agent in-process, RLS enforcement lives in libs/tenancy, and the validator lives in config_sdk.
- Concurrency: cache the graph, NO lock. One DB load per published flow version, shared from Redis; many simultaneous calls on the same number each get their own runner instance and own node pointer. Do not design a per-flow or per-caller lock.
- The flow version must be pinned at session open so a mid-call publish cannot move a live caller onto a new graph.

Scope to cover: caller dials a number -> tenant + flow resolved (Redis first, DB only on miss) -> flow walked node by node, speaking prompts via TTS, branching on DTMF keypress, collecting digits into variables, and handing off at the `agent` node to the conversational agent. Config-plane failures must degrade to a default rather than rejecting a live call, matching agent_resolver.py's contract.
