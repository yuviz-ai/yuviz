// Typed client for Knowledge Service's REST API (services/knowledge/, port
// 8100 in this dev setup) — a separate service from Config Service, but the
// same JWT (see lib/auth.ts) is valid against both: Knowledge Service only
// validates tokens (services.config.deps.get_current_user), it never mints
// them, so no separate login flow exists or is needed here.

import { getToken } from "./auth";
import { ApiError } from "./api";

const BASE_URL = process.env.NEXT_PUBLIC_KNOWLEDGE_SERVICE_URL || "http://localhost:8100";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken();
  const isFormData = options?.body instanceof FormData;
  const res = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: {
      ...(isFormData ? {} : { "Content-Type": "application/json" }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options?.headers,
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      // response body wasn't JSON — fall back to statusText
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// ── Knowledge Bases ──────────────────────────────────────────────────────

export type KnowledgeBaseStatus = "active" | "inactive";

export interface KnowledgeBase {
  id: string;
  tenant_id: string;
  slug: string;
  name: string;
  description: string;
  embedding_config_id: string | null;
  status: KnowledgeBaseStatus;
  config_version: number;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeBaseCreate {
  slug: string;
  name: string;
  description?: string;
  embedding_config_id?: string | null;
}

export interface KnowledgeBaseUpdate {
  name?: string;
  description?: string;
  embedding_config_id?: string | null;
  status?: KnowledgeBaseStatus;
}

export const listKnowledgeBases = (tenantId: string) =>
  request<KnowledgeBase[]>(`/tenants/${tenantId}/knowledge-bases`);
export const createKnowledgeBase = (tenantId: string, body: KnowledgeBaseCreate) =>
  request<KnowledgeBase>(`/tenants/${tenantId}/knowledge-bases`, { method: "POST", body: JSON.stringify(body) });
export const updateKnowledgeBase = (kbId: string, body: KnowledgeBaseUpdate) =>
  request<KnowledgeBase>(`/knowledge-bases/${kbId}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteKnowledgeBase = (kbId: string) =>
  request<void>(`/knowledge-bases/${kbId}`, { method: "DELETE" });

// ── Documents ────────────────────────────────────────────────────────────

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";
export type UsageMode = "auto" | "prompt";

export interface KbDocument {
  id: string;
  kb_id: string;
  tenant_id: string;
  title: string;
  source_ref: string;
  content_type: string;
  language: string | null;
  tags: Record<string, unknown>;
  status: DocumentStatus;
  error: string | null;
  version: number;
  usage_mode: UsageMode;
  chunk_count: number;
  created_at: string;
  updated_at: string;
}

export const listDocuments = (kbId: string) => request<KbDocument[]>(`/knowledge-bases/${kbId}/documents`);

export const uploadDocument = (kbId: string, file: File, title: string) => {
  const form = new FormData();
  form.append("file", file);
  form.append("title", title);
  return request<KbDocument & { ingestion_job_id: string }>(`/knowledge-bases/${kbId}/documents`, {
    method: "POST",
    body: form,
  });
};

export interface DocumentUpdate {
  title?: string;
  language?: string;
  tags?: Record<string, unknown>;
  usage_mode?: UsageMode;
}

export const updateDocument = (documentId: string, body: DocumentUpdate) =>
  request<KbDocument>(`/documents/${documentId}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteDocument = (documentId: string) =>
  request<void>(`/documents/${documentId}`, { method: "DELETE" });

// ── Reverse lookup: which agents use this KB ────────────────────────────

export interface KbAgent {
  agent_id: string;
  kb_id: string;
  enabled: boolean;
  created_at: string;
  agent_slug: string;
  agent_name: string;
  tenant_id: string;
}

export const listKbAgents = (kbId: string) => request<KbAgent[]>(`/knowledge-bases/${kbId}/agents`);

// ── Agent ↔ Knowledge Base assignment ───────────────────────────────────

export interface AgentKnowledgeBase {
  agent_id: string;
  kb_id: string;
  enabled: boolean;
  created_at: string;
  kb_slug: string;
  kb_name: string;
}

export const listAgentKnowledgeBases = (agentId: string) =>
  request<AgentKnowledgeBase[]>(`/agents/${agentId}/knowledge-bases`);
export const assignKnowledgeBase = (agentId: string, kbId: string, enabled = true) =>
  request<AgentKnowledgeBase>(`/agents/${agentId}/knowledge-bases`, {
    method: "POST",
    body: JSON.stringify({ kb_id: kbId, enabled }),
  });
export const setKnowledgeBaseEnabled = (agentId: string, kbId: string, enabled: boolean) =>
  request<AgentKnowledgeBase>(`/agents/${agentId}/knowledge-bases/${kbId}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  });
export const detachKnowledgeBase = (agentId: string, kbId: string) =>
  request<void>(`/agents/${agentId}/knowledge-bases/${kbId}`, { method: "DELETE" });

// ── Retrieval Policy ─────────────────────────────────────────────────────
// Fields are all optional here on purpose — an unset field means "fall
// back to the system default", never a fixed number baked into this
// client. See services/knowledge/retrieval.py's _resolve_policy().

export interface RetrievalPolicy {
  agent_id: string;
  top_k?: number | null;
  max_tokens?: number | null;
  minimum_score?: number | null;
  rerank?: boolean | null;
  hybrid_search?: boolean | null;
  include_citations?: boolean | null;
}

export const getRetrievalPolicy = (agentId: string) =>
  request<RetrievalPolicy>(`/agents/${agentId}/retrieval-policy`);
export const setRetrievalPolicy = (agentId: string, body: Omit<RetrievalPolicy, "agent_id">) =>
  request<RetrievalPolicy>(`/agents/${agentId}/retrieval-policy`, { method: "PUT", body: JSON.stringify(body) });
