"use client";

import { createContext, useContext } from "react";

/** Actions a node card can trigger on the canvas.
 *
 *  Passed through context rather than through node.data: React Flow spreads
 *  data into the persisted graph, and putting a callback there would both
 *  pollute the saved JSON and change identity on every render — which would
 *  re-serialize the graph, re-trigger autosave, and never settle. */
export interface WorkflowEditorActions {
  /** Add a stage already wired to this node, so building a flow never
   *  depends on landing a drag on a 12px handle. */
  addConnectedStage: (fromNodeId: string) => void;
}

export const EditorContext = createContext<WorkflowEditorActions>({
  addConnectedStage: () => {},
});

export const useEditorActions = () => useContext(EditorContext);
