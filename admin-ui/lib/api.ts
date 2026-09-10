// Typed client for Config Service's REST API (services/config/, port 8000).
// Field shapes mirror services/config/schemas.py exactly — one place these
// are defined, matching that file's own "no separate response schema" stance
// (responses are the plain dicts tenants.py/agents.py/etc. already return).

import { clearToken, getToken } from "./auth";

const BASE_URL = process.env.NEXT_PUBLIC_CONFIG_SERVICE_URL || "http://localhost:8000";

export class ApiError extends Error {
  /** Full parsed error JSON when present (workflow publish returns `errors`). */
  constructor(
    public status: number,
    public detail: string,
    public body?: Record<string, unknown>,
  ) {
    super(detail);
  }
}

// Exported for lib/workflowApi.ts (same auth / 401 redirect).
export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options?.headers,
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    let body: Record<string, unknown> | undefined;
    try {
      body = await res.json();
      detail = (body?.detail as string) || detail;
    } catch {
      // response body wasn't JSON — fall back to statusText
    }
    if (res.status === 401 && typeof window !== "undefined" && path !== "/auth/login") {
      // Token missing/expired/invalid — clear it and send the user back to
      // login rather than leaving every page silently failing its fetches.
      clearToken();
      if (window.location.pathname !== "/login") window.location.href = "/login";
    }
    throw new ApiError(res.status, detail, body);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// ── Tenants ──────────────────────────────────────────────────────────────

export interface Tenant {
  id: string;
  name: string;
  slug: string;
  region: string;
  vad_engine: string | null;
  vad_onset_ms: number | null;
  vad_hold_ms: number | null;
  no_speech_timeout_ms?: number | null;
  stt_timeout_ms?: number | null;
  llm_timeout_ms?: number | null;
  // Overrides the gateway's 45s default for how long a transfer waits for
  // CHANNEL_BRIDGE/CHANNEL_HANGUP before failing. Bounds: 10000-120000.
  transfer_timeout_ms: number | null;
  default_stt_config_id: string | null;
  default_llm_config_id: string | null;
  default_tts_config_id: string | null;
  // Live Calls Monitoring's utilization KPI's only source column — NULL
  // means "not configured yet" and must never be defaulted client-side any
  // more than server-side (see live-calls/page.tsx's setup prompt).
  max_concurrent_calls: number | null;
  config_version: number;
  created_at: string;
  updated_at: string;
}

export interface TenantCreate {
  name: string;
  slug: string;
  region?: string;
}

export interface TenantUpdate {
  name?: string;
  region?: string;
  transfer_timeout_ms?: number | null;
  no_speech_timeout_ms?: number | null;
}

export const listTenants = () => request<Tenant[]>("/tenants");
export const createTenant = (body: TenantCreate) =>
  request<Tenant>("/tenants", { method: "POST", body: JSON.stringify(body) });
export const updateTenant = (tenantId: string, body: TenantUpdate) =>
  request<Tenant>(`/tenants/${tenantId}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteTenant = (tenantId: string) =>
  request<void>(`/tenants/${tenantId}`, { method: "DELETE" });

// ── Provider Configs ─────────────────────────────────────────────────────

export type ProviderRole = "stt" | "llm" | "tts" | "embedding";
export type ProviderEnvironment = "prod" | "staging" | "dev";

export interface ProviderConfig {
  id: string;
  tenant_id: string;
  name: string;
  role: ProviderRole;
  engine: string;
  model: string | null;
  voice: string | null;
  language: string | null;
  region: string | null;
  environment: ProviderEnvironment;
  api_key_ref: string | null;
  extra: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface ProviderConfigCreate {
  name: string;
  role: ProviderRole;
  engine: string;
  environment?: ProviderEnvironment;
  model?: string;
  voice?: string;
  language?: string;
  region?: string;
  api_key_ref?: string;
  /** The credential itself; the server encrypts it into an enc: api_key_ref. */
  api_key?: string;
  // Engine-specific knobs (jsonb). Known keys: speed (TTS rate multiplier,
  // 0.7-1.2, default 1.0 — all engines), wpm (macos legacy absolute rate),
  // model_id/lang_code/temperature/… per engine.
  extra?: Record<string, unknown>;
}

export const listProviders = (tenantId: string, filters?: { role?: ProviderRole; environment?: ProviderEnvironment }) => {
  const params = new URLSearchParams();
  if (filters?.role) params.set("role", filters.role);
  if (filters?.environment) params.set("environment", filters.environment);
  const qs = params.toString();
  return request<ProviderConfig[]>(`/tenants/${tenantId}/providers${qs ? `?${qs}` : ""}`);
};

export const createProvider = (tenantId: string, body: ProviderConfigCreate) =>
  request<ProviderConfig>(`/tenants/${tenantId}/providers`, { method: "POST", body: JSON.stringify(body) });

export interface ProviderConfigUpdate {
  name?: string;
  engine?: string;
  environment?: ProviderEnvironment;
  model?: string;
  voice?: string;
  language?: string | null;
  region?: string;
  api_key_ref?: string;
  api_key?: string;
  // Replaces the whole extra object — spread the existing extra when
  // changing one key (the PATCH endpoint does not deep-merge).
  extra?: Record<string, unknown>;
}

export const updateProvider = (providerId: string, body: ProviderConfigUpdate) =>
  request<ProviderConfig>(`/providers/${providerId}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteProvider = (providerId: string) =>
  request<void>(`/providers/${providerId}`, { method: "DELETE" });

export interface ElevenLabsVoiceVerifiedLanguage {
  language: string; // validated ISO 639-1 code — unlike labels.language, which is arbitrary free text
  model_id: string;
  accent: string | null;
  locale: string | null;
  preview_url: string | null;
}

export interface ElevenLabsVoice {
  voice_id: string;
  name: string;
  category: string | null;
  labels: Record<string, string>;
  preview_url: string | null;
  // May be empty — not every voice has been through ElevenLabs' language
  // verification. Prefer this over labels.language when present (see
  // ElevenLabsVoicePicker's voicePrimaryLanguage()).
  verified_languages: ElevenLabsVoiceVerifiedLanguage[];
}

export const listElevenLabsVoices = (providerId: string) =>
  request<ElevenLabsVoice[]>(`/providers/${providerId}/voices`);

// ── Agents ───────────────────────────────────────────────────────────────

export type AgentStatus = "active" | "inactive";

export interface Agent {
  id: string;
  tenant_id: string;
  slug: string;
  name: string;
  greeting: string;
  system_prompt: string;
  goodbye_grace_ms: number;
  // null = derive from the STT/TTS provider's own language setting (see
  // libs/config_sdk's MediaInfo resolution order) — the behavior before
  // this field existed.
  language: string | null;
  stt_config_id: string | null;
  llm_config_id: string | null;
  tts_config_id: string | null;
  transfer_type: "warm" | "cold" | "none";
  // AI-to-human transfer (see project's transfer architecture phases) —
  // both "cold" and "warm" are fully implemented and live-verified.
  transfer_destination: string | null;
  // Accepted and persisted, but not read by any transfer-routing logic yet —
  // reserved for future queue-based routing.
  queue_id: string | null;
  // Consecutive guardrail violations before auto-escalating. The counting/
  // threshold mechanism exists (PipelineConversationHandler.
  // record_guardrail_violation), but no guardrail/content-safety detector
  // calls it yet — setting this has no effect on live calls until one does.
  escalation_threshold: number | null;
  // What caller ID the human agent sees on a warm transfer's agent leg
  // (no equivalent for cold transfer). Resolved entirely by the
  // Conversation Service — the gateway never sees this policy.
  caller_id_policy: "original" | "platform" | "custom";
  platform_did: string | null;      // used when caller_id_policy = "platform"
  custom_caller_id: string | null;  // used when caller_id_policy = "custom"
  // What the caller experiences while a warm transfer's agent leg rings
  // (no equivalent for cold transfer).
  transfer_waiting_experience: "announcement_moh" | "announcement_silence";
  // Published graph on agent GET/cache (call-setup). List responses omit
  // graph bodies and use the lean has_workflow* fields instead.
  workflow?: { nodes?: unknown[]; edges?: unknown[] } | null;
  has_workflow?: boolean;
  has_workflow_draft?: boolean;
  workflow_diverged?: boolean;
  workflow_node_count?: number | null;
  // Condition-clause overrides for the built-in end-call / transfer trigger
  // instructions (null/empty = defaults). Only the condition is
  // configurable — the [[END_CALL]]/[[TRANSFER]] token mechanics are fixed
  // server-side so a custom prompt can't break directive parsing.
  end_call_prompt: string | null;
  transfer_prompt: string | null;
  // Exact scripted lines the agent speaks when ending/transferring —
  // synthesized verbatim (never LLM-paraphrased). null/empty = the LLM
  // chooses its own wording.
  farewell_message: string | null;
  transfer_announcement: string | null;
  // Admin-configured hard ceiling on how long a caller may stay on this
  // agent, in seconds (30-7200). null = unlimited — the pre-existing
  // behavior. Enforced by the Conversation Service: once exceeded, the
  // pipeline skips the LLM, speaks a fixed wrap-up line, and ends the call.
  max_call_duration_s: number | null;
  status: AgentStatus;
  config_version: number;
  created_at: string;
  updated_at: string;
}

export interface AgentCreate {
  slug: string;
  name: string;
  greeting?: string;
  system_prompt?: string;
  stt_config_id?: string | null;
  llm_config_id?: string | null;
  tts_config_id?: string | null;
}

export interface AgentUpdate {
  name?: string;
  greeting?: string;
  system_prompt?: string;
  goodbye_grace_ms?: number;
  language?: string | null;
  stt_config_id?: string | null;
  llm_config_id?: string | null;
  tts_config_id?: string | null;
  transfer_type?: "warm" | "cold" | "none";
  transfer_destination?: string | null;
  queue_id?: string | null;
  escalation_threshold?: number | null;
  caller_id_policy?: "original" | "platform" | "custom";
  platform_did?: string | null;
  custom_caller_id?: string | null;
  transfer_waiting_experience?: "announcement_moh" | "announcement_silence";
  end_call_prompt?: string | null;
  transfer_prompt?: string | null;
  farewell_message?: string | null;
  transfer_announcement?: string | null;
  max_call_duration_s?: number | null;
  status?: AgentStatus;
}

export const listAgents = (tenantSlug: string) => request<Agent[]>(`/tenants/${tenantSlug}/agents`);
export const getAgent = (tenantSlug: string, agentSlug: string) =>
  request<Agent>(`/tenants/${tenantSlug}/agents/${agentSlug}`);
export const createAgent = (tenantSlug: string, body: AgentCreate) =>
  request<Agent>(`/tenants/${tenantSlug}/agents`, { method: "POST", body: JSON.stringify(body) });
export const updateAgent = (tenantSlug: string, agentId: string, body: AgentUpdate) =>
  request<Agent>(`/tenants/${tenantSlug}/agents/${agentId}`, { method: "PATCH", body: JSON.stringify(body) });

// There's no "list agents across all tenants" endpoint on Config Service —
// agents are always tenant-scoped there (see routers/agents.py). Composing
// listAgents() per tenant client-side avoids adding new backend surface for
// what's purely an Admin UI convenience (an aggregate view + a Tenant
// column), matching "prefer extending existing abstractions."
export interface AgentWithTenant extends Agent {
  tenantName: string;
  tenantSlug: string;
}

export const listAllAgents = async (tenants: Tenant[]): Promise<AgentWithTenant[]> => {
  const perTenant = await Promise.all(
    tenants.map(async (t) => {
      const agents = await listAgents(t.slug);
      return agents.map((a) => ({ ...a, tenantName: t.name, tenantSlug: t.slug }));
    }),
  );
  return perTenant.flat();
};

export const deleteAgent = (tenantSlug: string, agentId: string) =>
  request<void>(`/tenants/${tenantSlug}/agents/${agentId}`, { method: "DELETE" });

// ── Phone Numbers ────────────────────────────────────────────────────────

export type PhoneNumberStatus = "active" | "inactive" | "suspended";

export interface PhoneNumber {
  id: string;
  tenant_id: string;
  did: string;
  agent_id: string | null;
  fallback_agent_id: string | null;
  carrier_id: string | null;
  status: PhoneNumberStatus;
  region: string | null;
  created_at: string;
  updated_at: string;
}

export interface PhoneNumberCreate {
  did: string;
  agent_id?: string;
  fallback_agent_id?: string;
  carrier_id?: string;
  region?: string;
  status?: PhoneNumberStatus;
}

export interface PhoneNumberUpdate {
  did?: string;
  agent_id?: string | null;
  fallback_agent_id?: string | null;
  status?: PhoneNumberStatus;
}

export const listPhoneNumbers = (tenantId: string) => request<PhoneNumber[]>(`/tenants/${tenantId}/phone-numbers`);
export const createPhoneNumber = (tenantId: string, body: PhoneNumberCreate) =>
  request<PhoneNumber>(`/tenants/${tenantId}/phone-numbers`, { method: "POST", body: JSON.stringify(body) });
export const updatePhoneNumber = (phoneNumberId: string, body: PhoneNumberUpdate) =>
  request<PhoneNumber>(`/phone-numbers/${phoneNumberId}`, { method: "PATCH", body: JSON.stringify(body) });
export const deletePhoneNumber = (phoneNumberId: string) =>
  request<void>(`/phone-numbers/${phoneNumberId}`, { method: "DELETE" });

// ── Carriers ─────────────────────────────────────────────────────────────

export type CarrierProvider = "twilio" | "plivo" | "vonage";

export interface Carrier {
  id: string;
  tenant_id: string;
  name: string;
  provider: CarrierProvider;
  auth_id: string | null;
  auth_token_ref: string | null;
  carrier_account_ref: string | null;
  created_at: string;
  updated_at: string;
}

export interface CarrierCreate {
  name: string;
  provider: CarrierProvider;
  auth_id?: string;
  auth_token_ref?: string;
  carrier_account_ref?: string;
}

export const listCarriers = (tenantId: string) => request<Carrier[]>(`/tenants/${tenantId}/carriers`);
export const createCarrier = (tenantId: string, body: CarrierCreate) =>
  request<Carrier>(`/tenants/${tenantId}/carriers`, { method: "POST", body: JSON.stringify(body) });

// Same reasoning as listAllAgents() — no cross-tenant list endpoint exists
// server-side, composed client-side for the Admin UI's aggregate view.
export interface PhoneNumberWithTenant extends PhoneNumber {
  tenantName: string;
}

export const listAllPhoneNumbers = async (tenants: Tenant[]): Promise<PhoneNumberWithTenant[]> => {
  const perTenant = await Promise.all(
    tenants.map(async (t) => {
      const nums = await listPhoneNumbers(t.id);
      return nums.map((n) => ({ ...n, tenantName: t.name }));
    }),
  );
  return perTenant.flat();
};

// ── Calls ────────────────────────────────────────────────────────────────

export type CallDirection = "inbound" | "outbound";
export type CallStatus = "live" | "completed";
export type CallMode = "AI" | "WebRTC";

export interface Call {
  session_id: string;
  tenant_id: string;
  call_id: string | null;
  direction: CallDirection;
  caller_number: string | null;
  called_number: string | null;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  close_reason: string | null;
  turn_count: number;
  barge_in_count: number;
  agent_id: string | null;
  agent_name: string | null;
  status: CallStatus;
  mode: CallMode;
  disposition: string | null;
  nodes_visited: string[] | null;
  extracted_variables: Record<string, unknown> | null;
}

export interface CallListResult {
  total: number;
  limit: number;
  offset: number;
  items: Call[];
}

export interface TranscriptEntry {
  id: number;
  session_id: string;
  turn_number: number;
  caller_text: string | null;
  ai_response: string | null;
  interrupted: boolean;
  created_at: string;
}

export const listCalls = (
  tenantSlug: string,
  opts?: { limit?: number; offset?: number; direction?: CallDirection },
) => {
  const params = new URLSearchParams();
  if (opts?.limit) params.set("limit", String(opts.limit));
  if (opts?.offset) params.set("offset", String(opts.offset));
  if (opts?.direction) params.set("direction", opts.direction);
  const qs = params.toString();
  return request<CallListResult>(`/tenants/${tenantSlug}/calls${qs ? `?${qs}` : ""}`);
};

export const getTranscript = (sessionId: string) =>
  request<TranscriptEntry[]>(`/calls/${sessionId}/transcript`);

// Same reasoning as listAllAgents()/listAllPhoneNumbers() — calls.tenant_id
// is a slug (see services/config/calls.py), and there's no cross-tenant list
// endpoint server-side, so the aggregate view is composed client-side.
export interface CallWithTenant extends Call {
  tenantName: string;
}

export const listAllCalls = async (tenants: Tenant[]): Promise<CallWithTenant[]> => {
  const perTenant = await Promise.all(
    tenants.map(async (t) => {
      const result = await listCalls(t.slug, { limit: 200 });
      return result.items.map((c) => ({ ...c, tenantName: t.name }));
    }),
  );
  return perTenant.flat().sort((a, b) => b.started_at.localeCompare(a.started_at));
};

// ── Live Calls Monitoring ────────────────────────────────────────────────
// Mirrors services/config/routers/live_calls.py's response shape exactly —
// see that file's docstring/design doc for the full field-by-field rationale
// (masked numbers, withheld transcript, nullable cap).

export type LiveStage = "ai" | "waiting_for_human" | "human_connected";
export type InterventionAction = "listen" | "barge";
export type InterventionOutcome = "granted" | "denied" | "unavailable";

export interface LiveCallIntervention {
  action: InterventionAction;
  outcome: InterventionOutcome;
  requested_by_email: string;
  requested_at: string;
}

export interface LiveCall {
  session_id: string;
  agent_name: string | null;
  direction: CallDirection;
  caller_number_masked: string | null;
  called_number_masked: string | null;
  live_stage: LiveStage;
  started_at: string;
  elapsed_ms: number;
  transcript_snippet: string | null;
  transcript_withheld: boolean;
  intervention: LiveCallIntervention | null;
}

export interface LiveCallsKpis {
  live_calls: number;
  ai_only: number;
  waiting_for_human: number;
  human_connected: number;
  interventions_pending: number;
  // null (not 0, not a fallback) when the tenant hasn't set a cap yet —
  // the UI renders a setup prompt for both fields together, never a number.
  max_concurrent_calls: number | null;
  utilization_pct: number | null;
}

export interface LiveCallsSnapshot {
  tenant_slug: string;
  generated_at: string;
  refresh_seconds: number;
  truncated: boolean;
  kpis: LiveCallsKpis;
  items: LiveCall[];
}

// tenantSlug is omitted for supervisor/admin (server scopes to their own
// tenant) and required for a superadmin with a tenant selected — see
// live-calls/page.tsx.
export const getLiveCalls = (tenantSlug?: string) => {
  const qs = tenantSlug ? `?tenant_slug=${encodeURIComponent(tenantSlug)}` : "";
  return request<LiveCallsSnapshot>(`/live-calls${qs}`);
};

export interface InterventionResult {
  action: InterventionAction;
  outcome: InterventionOutcome;
  detail: string;
  requested_at: string;
}

export const requestIntervention = (sessionId: string, action: InterventionAction, tenantSlug?: string) =>
  request<InterventionResult>(`/live-calls/${encodeURIComponent(sessionId)}/interventions`, {
    method: "POST",
    body: JSON.stringify({ action, tenant_slug: tenantSlug ?? null }),
  });

export const updateTenantConcurrency = (tenantId: string, maxConcurrentCalls: number) =>
  request<Tenant>(`/tenants/${tenantId}/concurrency`, {
    method: "PATCH",
    body: JSON.stringify({ max_concurrent_calls: maxConcurrentCalls }),
  });

// ── Latency stats ────────────────────────────────────────────────────────
// Per-agent, per-LLM-engine voice-to-voice percentiles — see
// services/config/calls.py's get_latency_stats() for exactly what's
// computed and why turns with no voice_to_voice_ms are excluded.

export interface LatencyStat {
  agent_id: string | null;
  agent_name: string | null;
  llm_engine: string | null;
  sample_count: number;
  p50_voice_to_voice_ms: number | null;
  p95_voice_to_voice_ms: number | null;
  p50_stt_ms: number | null;
  p50_llm_ms: number | null;
  p50_tts_ms: number | null;
}

export const getLatencyStats = (tenantSlug: string, hours: number = 24) =>
  request<LatencyStat[]>(`/tenants/${tenantSlug}/calls/latency-stats?hours=${hours}`);

export interface LatencyStatWithTenant extends LatencyStat {
  tenantName: string;
}

export const listAllLatencyStats = async (
  tenants: Tenant[], hours: number = 24,
): Promise<LatencyStatWithTenant[]> => {
  const perTenant = await Promise.all(
    tenants.map(async (t) => {
      const stats = await getLatencyStats(t.slug, hours);
      return stats.map((s) => ({ ...s, tenantName: t.name }));
    }),
  );
  return perTenant.flat();
};

// ── Dashboard aggregates ─────────────────────────────────────────────────
// Backs admin-ui/app/dashboard/page.tsx. Same "no cross-tenant endpoint
// server-side, composed client-side" pattern as listAllCalls()/
// listAllCampaigns()/listAllLatencyStats() above.

export interface DashboardStats {
  total_calls: number;
  total_minutes: number;
  live_calls: number;
  success_count: number;
  failed_count: number;
  outbound_count: number;
}

export const getDashboardStats = (tenantSlug: string, hours: number = 24 * 30) =>
  request<DashboardStats>(`/tenants/${tenantSlug}/calls/dashboard-stats?hours=${hours}`);

export const listAllDashboardStats = async (tenants: Tenant[], hours: number = 24 * 30): Promise<DashboardStats> => {
  const perTenant = await Promise.all(tenants.map((t) => getDashboardStats(t.slug, hours)));
  return perTenant.reduce<DashboardStats>(
    (acc, s) => ({
      total_calls: acc.total_calls + s.total_calls,
      total_minutes: Math.round((acc.total_minutes + s.total_minutes) * 100) / 100,
      live_calls: acc.live_calls + s.live_calls,
      success_count: acc.success_count + s.success_count,
      failed_count: acc.failed_count + s.failed_count,
      outbound_count: acc.outbound_count + s.outbound_count,
    }),
    { total_calls: 0, total_minutes: 0, live_calls: 0, success_count: 0, failed_count: 0, outbound_count: 0 },
  );
};

export interface UsageTrendPoint {
  date: string;
  calls: number;
  minutes: number;
}

export const getUsageTrend = (tenantSlug: string, days: number = 30) =>
  request<UsageTrendPoint[]>(`/tenants/${tenantSlug}/calls/usage-trend?days=${days}`);

export const listAllUsageTrend = async (tenants: Tenant[], days: number = 30): Promise<UsageTrendPoint[]> => {
  const perTenant = await Promise.all(tenants.map((t) => getUsageTrend(t.slug, days)));
  const byDate = new Map<string, UsageTrendPoint>();
  for (const points of perTenant) {
    for (const p of points) {
      const existing = byDate.get(p.date);
      byDate.set(p.date, {
        date: p.date,
        calls: (existing?.calls || 0) + p.calls,
        minutes: Math.round(((existing?.minutes || 0) + p.minutes) * 100) / 100,
      });
    }
  }
  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
};

export interface TodaysActivityPoint {
  hour: number;
  inbound: number;
  outbound: number;
  web: number;
}

export const getTodaysActivity = (tenantSlug: string) =>
  request<TodaysActivityPoint[]>(`/tenants/${tenantSlug}/calls/todays-activity`);

export const listAllTodaysActivity = async (tenants: Tenant[]): Promise<TodaysActivityPoint[]> => {
  const perTenant = await Promise.all(tenants.map((t) => getTodaysActivity(t.slug)));
  const byHour = new Map<number, TodaysActivityPoint>();
  for (const points of perTenant) {
    for (const p of points) {
      const existing = byHour.get(p.hour);
      byHour.set(p.hour, {
        hour: p.hour,
        inbound: (existing?.inbound || 0) + p.inbound,
        outbound: (existing?.outbound || 0) + p.outbound,
        web: (existing?.web || 0) + p.web,
      });
    }
  }
  return [...byHour.values()].sort((a, b) => a.hour - b.hour);
};

// ── Tools ────────────────────────────────────────────────────────────────
// Config Service surface for services/conversation/tools/ (Tool Execution
// Framework): tool_provider_configs (a credentialed engine instance, e.g.
// "our Cal.com account") and agent_tool_policies (which agent may use which
// tool_provider_config). Resolved at call time by the Conversation
// Service's own ToolPolicyResolver, not read through this API — this is
// cold-path admin CRUD only.

export interface ToolCatalogExtraField {
  key: string;
  label: string;
  type: "text" | "number" | "boolean";
  required: boolean;
  help?: string;
}

export interface ToolCatalogEngine {
  engine: string;
  display_name: string;
  extra_fields: ToolCatalogExtraField[];
}

export interface ToolCatalogEntry {
  tool_name: string;
  display_name: string;
  description: string;
  category: string;
  engines: ToolCatalogEngine[];
}

export const listToolCatalog = () => request<ToolCatalogEntry[]>("/tools/catalog");

export interface ToolProviderConfig {
  id: string;
  tenant_id: string;
  name: string;
  tool_name: string;
  engine: string;
  api_key_ref: string | null;
  extra: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface ToolProviderConfigCreate {
  name: string;
  tool_name: string;
  engine: string;
  api_key_ref?: string;
  api_key?: string;
  extra?: Record<string, unknown>;
}

export interface ToolProviderConfigUpdate {
  name?: string;
  engine?: string;
  api_key_ref?: string;
  api_key?: string;
  extra?: Record<string, unknown>;
}

export const listToolProviderConfigs = (tenantId: string, filters?: { toolName?: string }) => {
  const params = new URLSearchParams();
  if (filters?.toolName) params.set("tool_name", filters.toolName);
  const qs = params.toString();
  return request<ToolProviderConfig[]>(`/tenants/${tenantId}/tool-providers${qs ? `?${qs}` : ""}`);
};

export const getToolProviderConfig = (toolProviderConfigId: string) =>
  request<ToolProviderConfig>(`/tool-providers/${toolProviderConfigId}`);

export const createToolProviderConfig = (tenantId: string, body: ToolProviderConfigCreate) =>
  request<ToolProviderConfig>(`/tenants/${tenantId}/tool-providers`, { method: "POST", body: JSON.stringify(body) });

export const updateToolProviderConfig = (toolProviderConfigId: string, body: ToolProviderConfigUpdate) =>
  request<ToolProviderConfig>(`/tool-providers/${toolProviderConfigId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const deleteToolProviderConfig = (toolProviderConfigId: string) =>
  request<void>(`/tool-providers/${toolProviderConfigId}`, { method: "DELETE" });

export interface AgentToolPolicy {
  id: string;
  agent_id: string;
  tool_name: string;
  tool_provider_config_id: string;
  tool_provider_config_name: string;
  tool_provider_config_engine: string;
  enabled: boolean;
  timeout_ms: number | null;
  max_calls_per_turn: number | null;
  created_at: string;
  updated_at: string;
}

export interface AgentToolPolicyCreate {
  tool_name: string;
  tool_provider_config_id: string;
  enabled?: boolean;
  timeout_ms?: number;
  max_calls_per_turn?: number;
}

export interface AgentToolPolicyUpdate {
  enabled?: boolean;
  timeout_ms?: number | null;
  max_calls_per_turn?: number | null;
}

export const listAgentToolPolicies = (agentId: string) =>
  request<AgentToolPolicy[]>(`/agents/${agentId}/tool-policies`);

export const createAgentToolPolicy = (agentId: string, body: AgentToolPolicyCreate) =>
  request<AgentToolPolicy>(`/agents/${agentId}/tool-policies`, { method: "POST", body: JSON.stringify(body) });

export const updateAgentToolPolicy = (agentId: string, toolName: string, body: AgentToolPolicyUpdate) =>
  request<AgentToolPolicy>(`/agents/${agentId}/tool-policies/${toolName}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const deleteAgentToolPolicy = (agentId: string, toolName: string) =>
  request<void>(`/agents/${agentId}/tool-policies/${toolName}`, { method: "DELETE" });

// ── Auth ─────────────────────────────────────────────────────────────────

// Widened alongside services/config's users_role_check (schema.sql) — a
// real user row can now hold "supervisor"/"agent" (invite-only account
// roles with no Config API surface of their own), not just the three
// console roles. Keep this in sync with that CHECK constraint.
export type UserRole = "superadmin" | "admin" | "supervisor" | "agent" | "viewer";

// Mirrors services/config/deps.py's CONSOLE_ROLES exactly — supervisor/agent
// have no Config API surface at all in this build, so a route decision here
// (e.g. where to land someone right after login) has to agree with the
// server's own gate, not just with what the sidebar happens to show.
export const CONSOLE_ROLES: readonly UserRole[] = ["superadmin", "admin", "viewer"];
export const isConsoleRole = (role: UserRole) => (CONSOLE_ROLES as readonly string[]).includes(role);

export interface User {
  id: string;
  tenant_id: string | null;
  email: string;
  role: UserRole;
  created_at: string;
  updated_at: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

export const login = (email: string, password: string) =>
  request<LoginResponse>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });

export const getSetupStatus = () =>
  request<{ setup_required: boolean }>("/auth/setup-status");

export const bootstrap = (email: string, password: string) =>
  request<LoginResponse>("/auth/bootstrap", { method: "POST", body: JSON.stringify({ email, password }) });

export const getCurrentUser = () => request<User>("/auth/me");

export const changePassword = (currentPassword: string, newPassword: string) =>
  request<void>("/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });

// ── Users ────────────────────────────────────────────────────────────────

export interface UserUpdate {
  role?: UserRole;
  tenant_id?: string | null;
  password?: string;
}

// `?tenant_id=` is honored by the server only for a super_admin caller — a
// tenant-scoped caller passing it has it silently overridden to their own
// tenant_id (services/config/routers/users.py). Passed through as-is here;
// the UI must not rely on omitting it for correctness.
export const listUsers = (tenantId?: string) =>
  request<User[]>(`/users${tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ""}`);
export const updateUser = (userId: string, body: UserUpdate) =>
  request<User>(`/users/${userId}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteUser = (userId: string) =>
  request<void>(`/users/${userId}`, { method: "DELETE" });

// ── Invites ──────────────────────────────────────────────────────────────
// Backs the invite-based onboarding flow (services/config/routers/invites.py).
// `supervisor`/`agent` are real account roles an invite can grant, but have
// no Config API surface of their own in this build (see design doc) — they
// only ever appear here as an invite's/user's `role`, never as a route guard.

// Same five roles as UserRole — kept as its own name because an invite's
// role and a user's role are conceptually different fields, not because the
// value sets differ.
export type InviteRole = UserRole;
export type InviteStatus = "pending" | "accepted" | "revoked";

export interface Invite {
  id: string;
  tenant_id: string | null;
  email: string;
  role: InviteRole;
  team: string | null;
  status: InviteStatus;
  expires_at: string;
  invited_by: string | null;
  accepted_at: string | null;
  accepted_user_id: string | null;
  last_sent_at: string | null;
  created_at: string;
  updated_at: string;
  // Present only on the create/resend responses (AC12) — a failed SMTP send
  // still leaves the invite row pending and resendable.
  email_sent?: boolean;
}

export interface InviteCreate {
  email: string;
  role: InviteRole;
  tenant_id?: string | null;
  team?: string | null;
}

// Same `?tenant_id=` server-side scoping rule as listUsers() above (AC11).
export const listInvites = (tenantId?: string) =>
  request<Invite[]>(`/invites${tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ""}`);
export const createInvite = (body: InviteCreate) =>
  request<Invite>("/invites", { method: "POST", body: JSON.stringify(body) });
export const resendInvite = (inviteId: string) =>
  request<Invite>(`/invites/${inviteId}/resend`, { method: "POST" });
export const revokeInvite = (inviteId: string) =>
  request<Invite>(`/invites/${inviteId}/revoke`, { method: "POST" });

// ── Invite accept (public — no JWT) ─────────────────────────────────────
// The raw token lives only in the URL fragment (never sent to a server) and
// is carried on these two requests as the X-Invite-Token header — see
// app/invite/page.tsx. Never put it in a path or query segment.

export interface InviteAcceptInfo {
  email: string;
  tenant_name: string;
  role: InviteRole;
  status: string;
}

export const getInvite = (token: string) =>
  request<InviteAcceptInfo>("/invites/accept", { headers: { "X-Invite-Token": token } });

export const acceptInvite = (token: string, password: string) =>
  request<User>("/invites/accept", {
    method: "POST",
    headers: { "X-Invite-Token": token },
    body: JSON.stringify({ password }),
  });

// ── Audit Log ────────────────────────────────────────────────────────────

export type AuditAction = "created" | "updated" | "deleted";

export interface AuditLogEntry {
  id: number;
  entity_type: string;
  entity_id: string;
  user_id: string | null;
  user_email: string | null;
  action: AuditAction;
  old_value: string | null;
  new_value: string | null;
  changed_at: string;
  ip_address: string | null;
}

export interface AuditLogFilters {
  entity_type?: string;
  entity_id?: string;
  user_email?: string;
  action?: AuditAction;
  limit?: number;
  offset?: number;
}

export interface AuditLogResult {
  total: number;
  limit: number;
  offset: number;
  items: AuditLogEntry[];
}

export const listAuditLog = (filters: AuditLogFilters = {}) => {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([k, v]) => {
    if (v !== undefined && v !== "") params.set(k, String(v));
  });
  const qs = params.toString();
  return request<AuditLogResult>(`/audit-log${qs ? `?${qs}` : ""}`);
};

// ── DID Service (services/did/, port 8200) ──────────────────────────────
// A separate service/port from Config Service — carrier search/purchase is
// cold-path, cost-affecting admin action, deliberately kept out of Config
// Service's own process (see project memory did-management-platform-architecture).
// Reuses the same bearer token (both services trust the same JWT issuer).

const DID_BASE_URL = process.env.NEXT_PUBLIC_DID_SERVICE_URL || "http://localhost:8200";

async function didRequest<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch(`${DID_BASE_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
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

export interface AvailableNumber {
  phone_number: string;
  region: string | null;
  monthly_price: string | null;
  capabilities: string[];
}

export interface PurchasedNumber {
  id: string;
  tenant_id: string;
  carrier_id: string;
  phone_number: string;
  carrier_number_sid: string;
  phone_number_id: string | null;
  purchased_at: string;
  released_at: string | null;
}

export const searchAvailableNumbers = (
  tenantId: string,
  params: { carrier_id: string; country: string; area_code?: string; limit?: number },
) => {
  const q = new URLSearchParams({
    carrier_id: params.carrier_id,
    country: params.country,
    ...(params.area_code ? { area_code: params.area_code } : {}),
    ...(params.limit ? { limit: String(params.limit) } : {}),
  });
  return didRequest<AvailableNumber[]>(`/tenants/${tenantId}/numbers/search?${q.toString()}`);
};

export const purchaseNumber = (tenantId: string, body: { carrier_id: string; phone_number: string }) =>
  didRequest<PurchasedNumber>(`/tenants/${tenantId}/numbers/purchase`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export const listPurchasedNumbers = (tenantId: string) =>
  didRequest<PurchasedNumber[]>(`/tenants/${tenantId}/numbers`);

export const assignPurchasedNumber = (purchasedNumberId: string, phoneNumberId: string) =>
  didRequest<PurchasedNumber>(
    `/numbers/${purchasedNumberId}/assign?phone_number_id=${encodeURIComponent(phoneNumberId)}`,
    { method: "PATCH" },
  );

// ── Campaign Service (services/campaigns/, port 8400) ───────────────────
// Own service/port, same reasoning as DID Service above — outbound calling
// is a distinct cold-path admin surface (campaign lifecycle + CSV upload),
// kept out of Config Service's own process. Reuses the same bearer token.

const CAMPAIGNS_BASE_URL = process.env.NEXT_PUBLIC_CAMPAIGNS_SERVICE_URL || "http://localhost:8400";

async function campaignsRequest<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken();
  const isFormData = options?.body instanceof FormData;
  const res = await fetch(`${CAMPAIGNS_BASE_URL}${path}`, {
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

export type CampaignStatus = "draft" | "running" | "paused" | "completed";

export interface Campaign {
  id: string;
  tenant_id: string;
  agent_id: string;
  name: string;
  status: CampaignStatus;
  caller_id: string | null;
  max_concurrent_calls: number;
  pacing_seconds: number;
  max_attempts: number;
  calling_hours_start: string | null;
  calling_hours_end: string | null;
  calling_hours_timezone: string;
  created_at: string;
  updated_at: string;
}

export interface CampaignCreate {
  agent_id: string;
  name: string;
  caller_id?: string | null;
  max_concurrent_calls?: number;
  pacing_seconds?: number;
  max_attempts?: number;
  calling_hours_start?: string | null;
  calling_hours_end?: string | null;
  calling_hours_timezone?: string;
}

export interface CampaignUpdate {
  name?: string;
  caller_id?: string | null;
  max_concurrent_calls?: number;
  pacing_seconds?: number;
  max_attempts?: number;
}

export interface CampaignProgress {
  total: number;
  pending: number;
  calling: number;
  completed: number;
  failed: number;
  no_answer: number;
  blocked: number;
}

export type ContactStatus = "pending" | "calling" | "completed" | "failed" | "no_answer" | "blocked";

export interface CampaignContact {
  id: string;
  campaign_id: string;
  phone_number: string;
  name: string | null;
  status: ContactStatus;
  attempt_count: number;
  last_attempted_at: string | null;
  call_session_id: string | null;
  created_at: string;
}

export const listCampaigns = (tenantId: string) => campaignsRequest<Campaign[]>(`/tenants/${tenantId}/campaigns`);

export const createCampaign = (tenantId: string, body: CampaignCreate) =>
  campaignsRequest<Campaign>(`/tenants/${tenantId}/campaigns`, { method: "POST", body: JSON.stringify(body) });

export const getCampaign = (campaignId: string) => campaignsRequest<Campaign>(`/campaigns/${campaignId}`);

export const updateCampaign = (campaignId: string, body: CampaignUpdate) =>
  campaignsRequest<Campaign>(`/campaigns/${campaignId}`, { method: "PATCH", body: JSON.stringify(body) });

export const getCampaignProgress = (campaignId: string) =>
  campaignsRequest<CampaignProgress>(`/campaigns/${campaignId}/progress`);

export const listCampaignContacts = (campaignId: string) =>
  campaignsRequest<CampaignContact[]>(`/campaigns/${campaignId}/contacts`);

export const uploadCampaignContacts = (campaignId: string, file: File) => {
  const form = new FormData();
  form.append("file", file);
  return campaignsRequest<{ inserted: number; skipped_dnc: number }>(`/campaigns/${campaignId}/contacts/upload`, {
    method: "POST",
    body: form,
  });
};

export const startCampaign = (campaignId: string) =>
  campaignsRequest<Campaign>(`/campaigns/${campaignId}/start`, { method: "POST" });

export const pauseCampaign = (campaignId: string) =>
  campaignsRequest<Campaign>(`/campaigns/${campaignId}/pause`, { method: "POST" });

export const resumeCampaign = (campaignId: string) =>
  campaignsRequest<Campaign>(`/campaigns/${campaignId}/resume`, { method: "POST" });

// Same reasoning as listAllAgents()/listAllPhoneNumbers() — no cross-tenant
// list endpoint exists server-side, composed client-side for the Admin
// UI's aggregate view.
export interface CampaignWithTenant extends Campaign {
  tenantName: string;
}

export const listAllCampaigns = async (tenants: Tenant[]): Promise<CampaignWithTenant[]> => {
  const perTenant = await Promise.all(
    tenants.map(async (t) => {
      const campaigns = await listCampaigns(t.id);
      return campaigns.map((c) => ({ ...c, tenantName: t.name }));
    }),
  );
  return perTenant.flat();
};

// ── Do-not-call list (per tenant) ────────────────────────────────────────

export interface DncNumber {
  id: string;
  tenant_id: string;
  phone_number: string;
  reason: string | null;
  created_at: string;
}

export const listDncNumbers = (tenantId: string) => campaignsRequest<DncNumber[]>(`/tenants/${tenantId}/dnc`);

export const addDncNumber = (tenantId: string, phoneNumber: string, reason?: string) =>
  campaignsRequest<DncNumber>(`/tenants/${tenantId}/dnc`, {
    method: "POST",
    body: JSON.stringify({ phone_number: phoneNumber, reason: reason || null }),
  });

export const removeDncNumber = (dncId: string) =>
  campaignsRequest<void>(`/dnc/${dncId}`, { method: "DELETE" });

