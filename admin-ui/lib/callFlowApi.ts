// Call flows (IVR/OBD) — see services/config/routers/call_flows.py.
// Distinct from lib/workflowApi.ts, which drives an *agent's* conversational
// graph: that one is keyed by agent id and its edges carry natural-language
// conditions; this one is keyed by a flow id of its own and its edges carry
// a keypress.

import { request } from "./api";

export type CallFlowStatus = "active" | "inactive";
export type CallFlowDirection = "inbound" | "outbound" | "both";

export type CallFlowNodeType = "start" | "play" | "menu" | "collect" | "dial" | "agent" | "hangup";

export const DTMF_KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "*", "#"] as const;
export const MENU_FALLBACK_KEYS = ["timeout", "invalid"] as const;

export interface CallFlowNodeData {
  name?: string;
  prompt?: string;
  timeout_ms?: number;
  max_retries?: number;
  variable?: string;
  min_digits?: number;
  max_digits?: number;
  terminator?: string;
  /** Collect step only: don't store or forward the collected value. */
  sensitive?: boolean;
  destination?: string;
  agent_id?: string;
  /** Start step only: the voice every spoken step in this flow uses. */
  tts_config_id?: string;
}

export interface CallFlowGraphNode {
  id: string;
  type: CallFlowNodeType;
  data: CallFlowNodeData;
  position?: { x: number; y: number };
}

export interface CallFlowGraphEdge {
  id: string;
  source: string;
  target: string;
  data?: { key?: string };
}

export interface CallFlowGraph {
  version: number;
  nodes: CallFlowGraphNode[];
  edges: CallFlowGraphEdge[];
}

export interface CallFlowSummary {
  id: string;
  tenant_id: string;
  slug: string;
  name: string;
  description: string;
  status: CallFlowStatus;
  direction: CallFlowDirection;
  config_version: number;
  created_at: string;
  updated_at: string;
  is_published: boolean;
  has_draft: boolean;
}

export interface CallFlow extends Omit<CallFlowSummary, "is_published" | "has_draft"> {
  graph: CallFlowGraph | null;
  graph_draft: CallFlowGraph | null;
  deleted_at: string | null;
}

export interface CallFlowProblem {
  kind: "node" | "edge" | "flow";
  id: string | null;
  field: string | null;
  message: string;
}

export interface CallFlowVersion {
  version: number;
  published_at: string;
  note: string | null;
  published_by_email: string | null;
}

export const listCallFlows = (tenantSlug: string) =>
  request<CallFlowSummary[]>(`/tenants/${tenantSlug}/call-flows`);

export const createCallFlow = (
  tenantSlug: string,
  body: {
    slug: string;
    name: string;
    description?: string;
    direction?: CallFlowDirection;
    /** Copy another flow in the same account; resolved server-side. */
    clone_from_id?: string;
    /** Scaffold from the step picker; ignored when clone_from_id is set. */
    graph?: CallFlowGraph;
  },
) =>
  request<CallFlow>(`/tenants/${tenantSlug}/call-flows`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export const getCallFlow = (callFlowId: string) => request<CallFlow>(`/call-flows/${callFlowId}`);

export const updateCallFlow = (
  callFlowId: string,
  body: { name?: string; description?: string; status?: CallFlowStatus; direction?: CallFlowDirection },
) => request<CallFlow>(`/call-flows/${callFlowId}`, { method: "PATCH", body: JSON.stringify(body) });

export const deleteCallFlow = (callFlowId: string) =>
  request<void>(`/call-flows/${callFlowId}`, { method: "DELETE" });

export const saveCallFlowDraft = (
  callFlowId: string,
  graph: CallFlowGraph,
  expectedVersion?: number,
) =>
  request<CallFlow>(`/call-flows/${callFlowId}/draft`, {
    method: "PUT",
    body: JSON.stringify({ graph, expected_version: expectedVersion }),
  });

// Validation failures come back as a 400 whose body carries per-node/per-edge
// problems, so this resolves either way and the caller reads `.problems`
// rather than having to catch to find out what is wrong with the canvas.
export const validateCallFlow = async (
  callFlowId: string,
  graph: CallFlowGraph,
): Promise<{ valid: boolean; problems: CallFlowProblem[] }> => {
  try {
    const res = await request<{ valid: boolean; warnings: CallFlowProblem[] }>(
      `/call-flows/${callFlowId}/validate`,
      { method: "POST", body: JSON.stringify({ graph }) },
    );
    return { valid: true, problems: res.warnings ?? [] };
  } catch (e) {
    const errors = (e as { body?: { errors?: CallFlowProblem[] } })?.body?.errors;
    if (errors) return { valid: false, problems: errors };
    throw e;
  }
};

export const publishCallFlow = (callFlowId: string, graph: CallFlowGraph, note?: string) =>
  request<CallFlow>(`/call-flows/${callFlowId}/publish`, {
    method: "POST",
    body: JSON.stringify({ graph, note }),
  });

export const listCallFlowVersions = (callFlowId: string) =>
  request<CallFlowVersion[]>(`/call-flows/${callFlowId}/versions`);

export const rollbackCallFlow = (callFlowId: string, version: number) =>
  request<CallFlow>(`/call-flows/${callFlowId}/versions/${version}/rollback`, { method: "POST" });
