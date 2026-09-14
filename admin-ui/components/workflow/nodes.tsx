"use client";

// The four node renderers. Four hardcoded types, not a spec-driven generic
// renderer (docs/workflow.md §2.2) — a registry to avoid writing four small
// components would be more framework than form.
//
// Card anatomy follows the Dograh editor (ui/src/components/flow/nodes/
// common/NodeContent.tsx): a coloured type pill straddling the top-left
// corner, a titled header rule, then a labelled prompt block. The type is
// then readable at a glance from across the canvas instead of being a
// 9px uppercase word in a corner.
//
// Every badge here answers "what does this stage do" without clicking into
// it: how many tools it can reach, whether it has a knowledge base, whether
// it extracts anything. A stage that is still missing something says so on
// the card, so an operator doesn't have to click each one to find the gap.

import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { WorkflowNodeData, WorkflowNodeType } from "@/lib/workflowApi";
import { useEditorActions } from "./editorContext";

const TYPE_LABEL: Record<WorkflowNodeType, string> = {
  start: "Start node",
  agent: "Stage",
  transfer: "Transfer",
  end: "End node",
  global: "Always applies",
};

const TYPE_HINT: Record<WorkflowNodeType, string> = {
  start: "The call begins here",
  agent: "A stage of the conversation",
  transfer: "Hands the call to a human",
  end: "Hangs up",
  global: "Added to every step's instructions",
};

const TYPE_ICON: Record<WorkflowNodeType, React.ReactNode> = {
  start: <path d="M4.5 3.2v9.6l8-4.8z" fill="currentColor" stroke="none" />,
  agent: <path d="M2 3.5h12v7H6.5L3.5 13v-2.5H2z" />,
  transfer: <path d="M2 8h9M8 4.5L11.5 8 8 11.5M12.5 3v10" />,
  end: <rect x="3.5" y="3.5" width="9" height="9" rx="1.5" />,
  global: (
    <>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M2.5 8h11M8 2.5c1.6 1.7 2.4 3.5 2.4 5.5S9.6 11.8 8 13.5C6.4 11.8 5.6 10 5.6 8S6.4 4.2 8 2.5z" />
    </>
  ),
};

function Badges({ data, type }: { data: WorkflowNodeData; type: WorkflowNodeType }) {
  const tools = data.tools?.length ?? 0;
  const kbs = data.knowledge_base_ids?.length ?? 0;
  const extracts = data.extraction?.enabled ? data.extraction.variables.length : 0;
  return (
    <div className="wf-badges">
      {tools > 0 && (
        <span className="wf-badge" title={`Can use: ${data.tools?.join(", ")}`}>
          {tools} tool{tools === 1 ? "" : "s"}
        </span>
      )}
      {kbs > 0 && <span className="wf-badge" title="Looks things up in a knowledge base">knowledge</span>}
      {extracts > 0 && (
        <span className="wf-badge" title="Captures details from what the caller says">
          captures {extracts}
        </span>
      )}
      {type === "end" && data.disposition && (
        <span className="wf-badge" title="Recorded on the call when it ends here">
          {String(data.disposition)}
        </span>
      )}
      {type === "transfer" && (
        <span className="wf-badge" title="Where this transfer goes">
          → {String(data.transfer_destination || "agent default")}
        </span>
      )}
    </div>
  );
}

function NodeShell({
  id, type, data, selected, invalid,
}: {
  id: string; type: WorkflowNodeType; data: WorkflowNodeData;
  selected: boolean; invalid: boolean;
}) {
  const { addConnectedStage } = useEditorActions();
  const unwired = type === "global";
  // No source handle: terminal steps end the call, and a global node was
  // never in the flow to begin with.
  const terminal = type === "end" || type === "transfer" || unwired;
  const label = `${TYPE_LABEL[type]}: ${data.name || "unnamed"}`;

  return (
    <div
      className={`wf-node wf-node-${type}${selected ? " selected" : ""}` +
        `${invalid ? " invalid" : ""}`}
      // Reachable and announced for keyboard/screen-reader users — a bare
      // div of prompt text tells them nothing about what it is.
      tabIndex={0}
      role="button"
      aria-label={label}
      title={TYPE_HINT[type]}
    >
      {type !== "start" && !unwired && <Handle type="target" position={Position.Top} />}

      <span className="wf-node-pill" aria-hidden>
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6">
          {TYPE_ICON[type]}
        </svg>
        {TYPE_LABEL[type]}
      </span>

      <div className="wf-node-hdr">
        <span className="wf-node-name">{data.name || <em>unnamed</em>}</span>
      </div>

      <div className="wf-node-body">
        <div className="wf-node-label">Prompt:</div>
        {data.prompt ? (
          <div className="wf-node-prompt">{data.prompt}</div>
        ) : (
          <div className="wf-node-prompt wf-node-todo">No instructions yet</div>
        )}
        <Badges data={data} type={type} />
      </div>

      {/* end and transfer are terminal by definition — no source handle at
          all, so a dangling edge out of one can't even be drawn. */}
      {!terminal && (
        <>
          <Handle type="source" position={Position.Bottom} />
          {/* Building a flow shouldn't depend on landing a drag on a 12px
              handle. This adds the next stage already connected. */}
          <button
            type="button"
            className="wf-node-add nodrag"
            title="Add a stage after this one"
            aria-label={`Add a stage after ${data.name || "this node"}`}
            onClick={(e) => {
              e.stopPropagation();
              addConnectedStage(id);
            }}
          >
            +
          </button>
        </>
      )}
    </div>
  );
}

function make(type: WorkflowNodeType) {
  const Component = ({ id, data, selected }: NodeProps) => (
    <NodeShell
      id={id}
      type={type}
      data={data as unknown as WorkflowNodeData}
      selected={!!selected}
      invalid={!!(data as { __invalid?: boolean }).__invalid}
    />
  );
  Component.displayName = `${type}Node`;
  return Component;
}

// Defined once at module scope, not inline in the panel — React Flow
// remounts every node when this object's identity changes.
export const nodeTypes = {
  start: make("start"),
  agent: make("agent"),
  transfer: make("transfer"),
  end: make("end"),
  global: make("global"),
};
