"use client";

import { createContext, useContext } from "react";

/** Actions a node card can trigger on the canvas.
 *  Passed via context, not node.data, so callbacks don't leak into the saved graph or retrigger autosave. */
export interface WorkflowEditorActions {
  /** Add a stage already wired to this node. */
  addConnectedStage: (fromNodeId: string) => void;
}

export const EditorContext = createContext<WorkflowEditorActions>({
  addConnectedStage: () => {},
});

export const useEditorActions = () => useContext(EditorContext);
