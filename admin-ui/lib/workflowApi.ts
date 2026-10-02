// Agent workflow draft/publish/versions client (services/config/routers/agents.py).

import { ApiError, request } from "./api";

// React Flow save format; libs/config_sdk/workflow.py is the source of truth.
// "global" is unwired and holds the instruction prepended to every step (max one per flow).
export type WorkflowNodeType = "start" | "agent" | "transfer" | "end" | "global";

export interface ExtractionVariable {
  name: string;
  type: "string" | "number" | "boolean";
  prompt: string;
}

export interface Extraction {
  enabled: boolean;
  prompt: string;
  variables: ExtractionVariable[];
}

export interface WorkflowNodeData {
  name: string;
  prompt: string;
  greeting?: string;
  delayed_start_ms?: number;
  tools?: string[];
  knowledge_base_ids?: string[];
  extraction?: Extraction;
  transfer_destination?: string | null;
  disposition?: string | null;
  [key: string]: unknown;
}

export interface WorkflowEdgeData {
  label: string;
  condition: string;
  transition_speech?: string;
  [key: string]: unknown;
}

export interface WorkflowGraph {
  version: number;
  nodes: {
    id: string;
    type: WorkflowNodeType;
    position: { x: number; y: number };
    data: WorkflowNodeData;
  }[];
  edges: {
    id: string;
    source: string;
    target: string;
    data: WorkflowEdgeData;
  }[];
}

export interface WorkflowError {
  kind: "node" | "edge" | "workflow";
  id: string | null;
  field: string | null;
  message: string;
}

export interface WorkflowState {
  workflow: WorkflowGraph | null;       // live — what calls execute
  workflow_draft: WorkflowGraph | null; // the editor's autosave
  published: boolean;
  config_version: number;
}

export interface WorkflowVersion {
  id: string;
  version: number;
  published_at: string;
  note: string | null;
  published_by_email: string | null;
  node_count: number;
  edge_count: number;
}

export interface PublishResult {
  version: number;
  config_version: number;
  warnings: WorkflowError[];
}

export interface ValidateResult {
  valid: boolean;
  errors?: WorkflowError[];
  warnings: WorkflowError[];
}

const base = (tenantSlug: string, agentId: string) =>
  `/tenants/${tenantSlug}/agents/${agentId}/workflow`;

export const getWorkflow = (tenantSlug: string, agentId: string) =>
  request<WorkflowState>(base(tenantSlug, agentId));

export const saveWorkflowDraft = (
  tenantSlug: string,
  agentId: string,
  graph: WorkflowGraph,
  opts?: { signal?: AbortSignal; baseConfigVersion?: number },
) =>
  request<{ saved: boolean; config_version: number }>(`${base(tenantSlug, agentId)}/draft`, {
    method: "PUT",
    body: JSON.stringify({
      graph,
      base_config_version: opts?.baseConfigVersion,
    }),
    signal: opts?.signal,
  });

export const validateWorkflow = (tenantSlug: string, agentId: string, graph: WorkflowGraph) =>
  request<ValidateResult>(`${base(tenantSlug, agentId)}/validate`, {
    method: "POST",
    body: JSON.stringify({ graph }),
  });

export const publishWorkflow = (
  tenantSlug: string, agentId: string, graph?: WorkflowGraph, note?: string,
) =>
  request<PublishResult>(`${base(tenantSlug, agentId)}/publish`, {
    method: "POST",
    body: JSON.stringify({ graph, note }),
  });

export const listWorkflowVersions = (tenantSlug: string, agentId: string) =>
  request<WorkflowVersion[]>(`${base(tenantSlug, agentId)}/versions`);

export const getWorkflowVersion = (tenantSlug: string, agentId: string, version: number) =>
  request<{ version: number; graph: WorkflowGraph }>(
    `${base(tenantSlug, agentId)}/versions/${version}`,
  );

export const rollbackWorkflow = (tenantSlug: string, agentId: string, version: number) =>
  request<PublishResult>(`${base(tenantSlug, agentId)}/versions/${version}/rollback`, {
    method: "POST",
  });

/** Extract per-node/per-edge errors from a failed publish. */
export function publishErrors(e: unknown): { message: string; errors: WorkflowError[] } {
  if (e instanceof ApiError) {
    return { message: e.detail, errors: (e.body?.errors as WorkflowError[]) ?? [] };
  }
  return { message: String(e), errors: [] };
}

/** Mirrors libs/config_sdk/workflow.starter_graph; used only when draft and live are both missing. */
export function starterGraph(greeting = "", systemPrompt = ""): WorkflowGraph {
  return {
    version: 1,
    nodes: [
      {
        id: "global", type: "global", position: { x: 330, y: 0 },
        data: { name: "always applies", prompt: systemPrompt },
      },
      {
        id: "start", type: "start", position: { x: 0, y: 0 },
        data: {
          name: "greeting",
          prompt: "Greet the caller and find out what they need.",
          greeting,
          tools: [],
          knowledge_base_ids: [],
        },
      },
      {
        id: "end", type: "end", position: { x: 0, y: 230 },
        data: {
          name: "goodbye",
          prompt: "Confirm anything outstanding and close warmly.",
          disposition: "completed",
        },
      },
    ],
    edges: [
      {
        id: "e-start-end", source: "start", target: "end",
        data: { label: "conversation finished", condition: "The caller has no further questions." },
      },
    ],
  };
}
