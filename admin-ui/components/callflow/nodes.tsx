"use client";

// The seven IVR step renderers. Same card anatomy and the same wf-node-*
// classes as components/workflow/nodes.tsx, deliberately — the two canvases
// should feel like one product — but a separate file because the vocabulary
// is different: these steps branch on a keypress, not on an LLM's reading of
// a natural-language condition.

import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { CallFlowNodeData, CallFlowNodeType } from "@/lib/callFlowApi";

const TYPE_LABEL: Record<CallFlowNodeType, string> = {
  start: "Start",
  play: "Play message",
  menu: "Menu",
  collect: "Collect digits",
  dial: "Transfer",
  agent: "AI agent",
  hangup: "Hang up",
};

const TYPE_HINT: Record<CallFlowNodeType, string> = {
  start: "The call is answered here",
  play: "Speaks, then continues",
  menu: "Plays options, branches on a keypress",
  collect: "Collects digits into a value",
  dial: "Sends the call to a number or SIP address",
  agent: "Hands the call to an AI agent",
  hangup: "Ends the call",
};

// Terminal steps take the call out of the flow, so they have an input but no
// output handle — the canvas itself makes "nothing follows this" obvious.
const TERMINAL: CallFlowNodeType[] = ["dial", "agent", "hangup"];

function missingField(type: CallFlowNodeType, data: CallFlowNodeData): string | null {
  if (type === "dial" && !data.destination?.trim()) return "needs a destination";
  if (type === "agent" && !data.agent_id?.trim()) return "needs an agent";
  if (type === "collect" && !data.variable?.trim()) return "needs a value name";
  if ((type === "play" || type === "menu" || type === "collect") && !data.prompt?.trim()) {
    return "needs something to say";
  }
  return null;
}

export function CallFlowNode({ data, type, selected }: NodeProps) {
  const d = (data || {}) as CallFlowNodeData;
  const t = type as CallFlowNodeType;
  const todo = missingField(t, d);

  return (
    <div className={`wf-node wf-node-${t === "hangup" ? "end" : t === "dial" ? "transfer" : t}${selected ? " selected" : ""}${todo ? " wf-node-todo" : ""}`}>
      {t !== "start" && <Handle type="target" position={Position.Top} />}

      <div className="wf-node-pill">{TYPE_LABEL[t]}</div>
      <div className="wf-node-hdr">
        <div className="wf-node-name">{d.name || TYPE_LABEL[t]}</div>
      </div>

      <div className="wf-node-body">
        {d.prompt?.trim() ? (
          <>
            <div className="wf-node-label">Says</div>
            <div className="wf-node-prompt">{d.prompt}</div>
          </>
        ) : (
          <div className="wf-node-prompt" style={{ opacity: 0.6 }}>{TYPE_HINT[t]}</div>
        )}

        {t === "collect" && d.variable && (
          <div className="wf-badges">
            <span className="wf-badge">→ {d.variable}</span>
            <span className="wf-badge">{d.min_digits ?? 1}–{d.max_digits ?? 10} digits</span>
          </div>
        )}
        {t === "dial" && d.destination && (
          <div className="wf-badges"><span className="wf-badge">{d.destination}</span></div>
        )}
        {t === "menu" && (
          <div className="wf-badges">
            <span className="wf-badge">{(d.timeout_ms ?? 5000) / 1000}s to answer</span>
            <span className="wf-badge">{d.max_retries ?? 2} retries</span>
          </div>
        )}
        {todo && <div className="wf-badges"><span className="wf-badge wf-check">{todo}</span></div>}
      </div>

      {!TERMINAL.includes(t) && <Handle type="source" position={Position.Bottom} />}
    </div>
  );
}

export const nodeTypes = {
  start: CallFlowNode,
  play: CallFlowNode,
  menu: CallFlowNode,
  collect: CallFlowNode,
  dial: CallFlowNode,
  agent: CallFlowNode,
  hangup: CallFlowNode,
};
