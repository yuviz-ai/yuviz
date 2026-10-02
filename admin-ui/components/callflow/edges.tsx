"use client";

// Edge labelled with the keypress that takes it; a blank label on a menu is a hard error.

import { BaseEdge, EdgeLabelRenderer, getSmoothStepPath, type EdgeProps } from "@xyflow/react";

const KEY_LABEL: Record<string, string> = {
  timeout: "no answer",
  invalid: "wrong key",
};

export function KeypressEdge({
  id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, selected,
}: EdgeProps) {
  const [path, labelX, labelY] = getSmoothStepPath({
    sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition,
    borderRadius: 8, offset: 20,
  });
  const key = (data as { key?: string; __fromMenu?: boolean } | undefined)?.key;
  const fromMenu = (data as { __fromMenu?: boolean } | undefined)?.__fromMenu;
  // Only a menu's branches need a key; everything else just continues, and
  // labelling that "press nothing" would be noise.
  const state = fromMenu && !key ? "unfinished" : "ok";
  const label = key ? (KEY_LABEL[key] ?? `press ${key}`) : fromMenu ? "needs a key" : "then";

  return (
    <>
      <BaseEdge id={id} path={path} interactionWidth={20} />
      <EdgeLabelRenderer>
        <div
          className={`wf-edge-pill wf-edge-${state}${selected ? " selected" : ""}`}
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
        >
          {label}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

export const edgeTypes = { keypress: KeypressEdge };
