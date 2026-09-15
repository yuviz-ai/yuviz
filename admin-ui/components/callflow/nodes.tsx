"use client";

// Call-flow step cards.
//
// Own styles (cf-*), not the agent canvas's wf-node: that card is 300px with
// a large prompt block because a conversation stage IS its prompt. An IVR
// step is mostly structure — what kind of step, what it says in one line,
// where it goes — so these are compact and scannable, and a flow of a dozen
// steps fits on screen instead of three.

import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { CallFlowNodeData, CallFlowNodeType } from "@/lib/callFlowApi";

const TYPE_LABEL: Record<CallFlowNodeType, string> = {
  start: "Entry",
  play: "Play",
  menu: "Menu",
  collect: "Collect",
  dial: "Transfer",
  agent: "Agent",
  hangup: "End",
};

const TYPE_HINT: Record<CallFlowNodeType, string> = {
  start: "The call is answered here",
  play: "Speaks, then continues",
  menu: "Branches on a keypress",
  collect: "Collects digits",
  dial: "Sends the call to a human",
  agent: "Hands over to an AI agent",
  hangup: "Ends the call",
};

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

/** One line under the title: the step's own substance, not its type. */
function subtitle(type: CallFlowNodeType, data: CallFlowNodeData): string {
  if (type === "dial") return data.destination?.trim() || TYPE_HINT[type];
  if (type === "collect") {
    const v = data.variable?.trim();
    return v ? `→ ${v} · ${data.min_digits ?? 1}–${data.max_digits ?? 10} digits` : TYPE_HINT[type];
  }
  if (data.prompt?.trim()) return data.prompt.trim();
  return TYPE_HINT[type];
}

export function CallFlowNode({ id, data, type, selected }: NodeProps) {
  const d = (data || {}) as CallFlowNodeData;
  const t = type as CallFlowNodeType;
  const todo = missingField(t, d);

  return (
    <div className={`cf-node cf-node-${t}${selected ? " selected" : ""}${todo ? " todo" : ""}`}>
      {t !== "start" && <Handle type="target" position={Position.Left} />}

      <div className="cf-node-top">
        <span className={`cf-node-pill cf-pill-${t}`}>{TYPE_LABEL[t]}</span>
        <span className="cf-node-id">{id.length > 10 ? `${id.slice(0, 9)}…` : id}</span>
      </div>

      <div className="cf-node-name">{d.name || TYPE_LABEL[t]}</div>
      <div className="cf-node-sub">{subtitle(t, d)}</div>

      {todo && <div className="cf-node-todo">{todo}</div>}

      {!TERMINAL.includes(t) && <Handle type="source" position={Position.Right} />}
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
