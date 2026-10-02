"use client";

// Label rendered as HTML via EdgeLabelRenderer so it can be coloured by state;
// React Flow's built-in SVG label has an opaque white box.

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
