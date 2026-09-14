"use client";

// One edge renderer, for the label.
//
// React Flow's built-in `label` draws SVG text with an opaque white box
// behind it, which on a dark canvas reads as a sticker stuck over the wire.
// Dograh renders the label as HTML through EdgeLabelRenderer instead
// (ui/src/components/flow/edges/CustomEdge.tsx) — a pill that can be
// coloured by state, which is what the label needs to do here: an amber
// "unfinished" pill is the editor's loudest warning.
//
// Smooth-step rather than bezier for the same reason Dograh uses it —
// elbows read as a state machine, curves read as a mind map.

import { BaseEdge, EdgeLabelRenderer, getSmoothStepPath, type EdgeProps } from "@xyflow/react";
import type { WorkflowEdgeData } from "@/lib/workflowApi";

export function ConditionEdge({
  id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, selected,
}: EdgeProps) {
  const [path, labelX, labelY] = getSmoothStepPath({
    sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition,
    borderRadius: 8, offset: 20,
  });
  const d = data as WorkflowEdgeData | undefined;
  const unfinished = !d?.condition?.trim();
  const state = d?.__invalid ? "invalid" : unfinished ? "unfinished" : "ok";

  return (
    <>
      <BaseEdge id={id} path={path} interactionWidth={20} />
      <EdgeLabelRenderer>
        <div
          className={`wf-edge-pill wf-edge-${state}${selected ? " selected" : ""}`}
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
        >
          {d?.label || (unfinished ? "needs a condition" : "unnamed")}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

export const edgeTypes = { condition: ConditionEdge };
