// Typed client for Config Service's REST API. Shapes mirror services/config/schemas.py.

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
      // FastAPI validation errors (422) send a list of {loc, msg}, not a string.
      const raw = body?.detail;
      detail = (Array.isArray(raw) ? raw[0]?.msg : (raw as string)) || detail;
    } catch {
      // response body wasn't JSON — fall back to statusText
    }
    if (res.status === 401 && typeof window !== "undefined" && path !== "/auth/login") {
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
  // Overrides the gateway's 45s transfer bridge timeout. Bounds: 10000-120000.
  transfer_timeout_ms: number | null;
  default_stt_config_id: string | null;
  default_llm_config_id: string | null;
  default_tts_config_id: string | null;
  // NULL = not configured; never default it client-side (drives the utilization KPI).
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
export const deleteTenant = (tenantId: string, force?: boolean) =>
  request<void>(`/tenants/${tenantId}${force ? "?force=true" : ""}`, { method: "DELETE" });

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
  // Engine-specific knobs, e.g. speed (TTS rate, 0.7-1.2), wpm (macos), model_id, temperature.
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
  // Replaces the whole object (no deep merge); spread the existing extra to change one key.
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
  // May be empty; prefer over labels.language when present.
  verified_languages: ElevenLabsVoiceVerifiedLanguage[];
}

/** Speak `text` in this provider's voice as WAV. Local engines (macos, kokoro) return 400. */
export const previewVoice = async (providerId: string, text: string): Promise<Blob> => {
  const token = getToken();
  const res = await fetch(`${BASE_URL}/providers/${providerId}/preview`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ text }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = ((await res.json())?.detail as string) || detail;
    } catch {
      // not JSON — keep statusText
    }
    throw new ApiError(res.status, detail);
  }
  return res.blob();
};

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
  // null = use the STT/TTS provider's language.
  language: string | null;
  stt_config_id: string | null;
  llm_config_id: string | null;
  tts_config_id: string | null;
  transfer_type: "warm" | "cold" | "none";
  transfer_destination: string | null;
  // Persisted but not yet used by transfer routing.
  queue_id: string | null;
  // No guardrail detector reports violations yet, so this has no live effect.
  escalation_threshold: number | null;
  // Caller ID shown to the human on a warm transfer's agent leg.
  caller_id_policy: "original" | "platform" | "custom";
  platform_did: string | null;      // used when caller_id_policy = "platform"
  custom_caller_id: string | null;  // used when caller_id_policy = "custom"
  // What the caller hears while a warm transfer's agent leg rings.
  transfer_waiting_experience: "announcement_moh" | "announcement_silence";
  // Published graph on agent GET/cache (call-setup). List responses omit
  // graph bodies and use the lean has_workflow* fields instead.
  workflow?: { nodes?: unknown[]; edges?: unknown[] } | null;
  has_workflow?: boolean;
  has_workflow_draft?: boolean;
  workflow_diverged?: boolean;
  workflow_node_count?: number | null;
  // Condition-clause overrides only; the [[END_CALL]]/[[TRANSFER]] tokens stay fixed server-side.
  end_call_prompt: string | null;
  transfer_prompt: string | null;
  // Spoken verbatim when ending/transferring; null = LLM chooses wording.
  farewell_message: string | null;
  transfer_announcement: string | null;
  // Seconds (30-7200); null = unlimited. On expiry the call is wrapped up and ended.
  max_call_duration_s: number | null;
  /** Which call flow answers ahead of this agent (call_flows.id), or null. */
  call_flow_id: string | null;
  // Set only on agents created from a shipped Easy job; null for Advanced.
  template_id: string | null;
  template_version: number | null;
  // True while the stored prompt still equals the one the last accepted fix
  // wrote, so "Undo last change" can be offered. Computed server-side.
  can_undo: boolean;
  // False when the prompt was hand-edited so it can't be fixed automatically.
  prompt_fixable: boolean;
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
  call_flow_id?: string | null;
  status?: AgentStatus;
}

export const listAgents = (tenantSlug: string) => request<Agent[]>(`/tenants/${tenantSlug}/agents`);
export const getAgent = (tenantSlug: string, agentSlug: string) =>
  request<Agent>(`/tenants/${tenantSlug}/agents/${agentSlug}`);
export const createAgent = (tenantSlug: string, body: AgentCreate) =>
  request<Agent>(`/tenants/${tenantSlug}/agents`, { method: "POST", body: JSON.stringify(body) });
export const updateAgent = (tenantSlug: string, agentId: string, body: AgentUpdate) =>
  request<Agent>(`/tenants/${tenantSlug}/agents/${agentId}`, { method: "PATCH", body: JSON.stringify(body) });

// ── Easy agent creation (shipped jobs, test sessions, prompt fixes) ──────

export type AgentTemplateChannel = "phone_in" | "phone_out" | "chat";

export interface AgentTemplateInfo {
  id: string;
  version: number;
  channel: AgentTemplateChannel;
  label: string;
  blurb: string;
  does: string;
  wont_do: string;
  handoff: string;
  needs: ("llm" | "stt" | "tts")[];
}

export interface AgentFromTemplateRequest {
  template_id: string;
  template_version: number;
  name: string;
  business_name: string;
  // Always sent; "" when the user enters nothing.
  business_facts: string;
  language?: string | null;
  stt_config_id?: string | null;
  llm_config_id?: string | null;
  tts_config_id?: string | null;
}

export type TestChannel = "voice" | "chat";

export interface VoiceTestSession {
  credential: string;
  expires_in: number;
}

export interface ChatTestSession extends VoiceTestSession {
  session_id: string;
  greeting: string;
}

export interface PromptRevision {
  before: string;
  after: string;
  base_prompt_sha256: string;
}

export const listAgentTemplates = () => request<AgentTemplateInfo[]>("/agent-templates");
export const createAgentFromTemplate = (tenantSlug: string, body: AgentFromTemplateRequest) =>
  request<Agent>(`/tenants/${tenantSlug}/agents/from-template`, { method: "POST", body: JSON.stringify(body) });
export const createTestSession = <C extends TestChannel>(tenantSlug: string, agentId: string, channel: C) =>
  request<C extends "chat" ? ChatTestSession : VoiceTestSession>(
    `/tenants/${tenantSlug}/agents/${agentId}/test-sessions`,
    { method: "POST", body: JSON.stringify({ channel }) },
  );
export const sendTestChat = (
  tenantSlug: string,
  agentId: string,
  body: { credential: string; session_id: string; message: string },
) =>
  request<{ reply: string }>(`/tenants/${tenantSlug}/agents/${agentId}/test-chat`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const revisePrompt = (
  tenantSlug: string,
  agentId: string,
  body: { session_id: string; problem: string; llm_config_id?: string },
) =>
  request<PromptRevision>(`/tenants/${tenantSlug}/agents/${agentId}/prompt/revise`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const acceptPrompt = (
  tenantSlug: string,
  agentId: string,
  body: { session_id: string; problem: string; proposed_prompt: string; base_prompt_sha256: string },
) =>
  request<Agent>(`/tenants/${tenantSlug}/agents/${agentId}/prompt/accept`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const undoPrompt = (tenantSlug: string, agentId: string) =>
  request<Agent>(`/tenants/${tenantSlug}/agents/${agentId}/prompt/undo`, { method: "POST" });

export interface SystemPromptGenerateRequest {
  name: string;
  purpose?: string;
  persona?: string;
  tone?: string;
  language?: string | null;
  has_knowledge_base?: boolean;
  transfer_condition?: string | null;
  compliance_instructions?: string;
  fallback_response?: string;
  llm_config_id: string;
}
export const generateSystemPrompt = (tenantSlug: string, body: SystemPromptGenerateRequest) =>
  request<{ system_prompt: string }>(`/tenants/${tenantSlug}/agents/generate-system-prompt`, {
    method: "POST",
    body: JSON.stringify(body),
  });

// No cross-tenant agents endpoint exists; fan out listAgents() per tenant.
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
  telephony_config_id: string | null;
  status: PhoneNumberStatus;
  region: string | null;
  created_at: string;
  updated_at: string;
  // Last provider sync attempt; null if never tried. ok=false: saved but not wired.
  provider_sync?: ProviderSync | null;
}

export interface ProviderSync {
  ok: boolean;
  message: string | null;
  /** When it was last tried; absent on a sync-numbers result row. */
  at?: string;
}

export interface PhoneNumberCreate {
  did: string;
  agent_id?: string;
  fallback_agent_id?: string;
  carrier_id?: string;
  telephony_config_id?: string;
  region?: string;
  status?: PhoneNumberStatus;
}

export interface PhoneNumberUpdate {
  did?: string;
  agent_id?: string | null;
  fallback_agent_id?: string | null;
  carrier_id?: string | null;
  telephony_config_id?: string | null;
  region?: string;
  status?: PhoneNumberStatus;
}

export const listPhoneNumbers = (tenantId: string) => request<PhoneNumber[]>(`/tenants/${tenantId}/phone-numbers`);
export const createPhoneNumber = (tenantId: string, body: PhoneNumberCreate) =>
  request<PhoneNumber>(`/tenants/${tenantId}/phone-numbers`, { method: "POST", body: JSON.stringify(body) });
export const updatePhoneNumber = (phoneNumberId: string, body: PhoneNumberUpdate) =>
  request<PhoneNumber>(`/phone-numbers/${phoneNumberId}`, { method: "PATCH", body: JSON.stringify(body) });
export const deletePhoneNumber = (phoneNumberId: string, opts: { force?: boolean } = {}) =>
  request<void>(`/phone-numbers/${phoneNumberId}${opts.force ? "?force=true" : ""}`, { method: "DELETE" });
export const syncPhoneNumber = (phoneNumberId: string) =>
  request<PhoneNumber>(`/phone-numbers/${phoneNumberId}/sync`, { method: "POST" });

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

export interface CarrierUpdate {
  name?: string;
  auth_id?: string;
  auth_token_ref?: string;
  carrier_account_ref?: string;
}

export const listCarriers = (tenantId: string) => request<Carrier[]>(`/tenants/${tenantId}/carriers`);
export const createCarrier = (tenantId: string, body: CarrierCreate) =>
  request<Carrier>(`/tenants/${tenantId}/carriers`, { method: "POST", body: JSON.stringify(body) });
export const updateCarrier = (carrierId: string, body: CarrierUpdate) =>
  request<Carrier>(`/carriers/${carrierId}`, { method: "PATCH", body: JSON.stringify(body) });

// ── Telephony Configs ────────────────────────────────────────────────────
// Webhook-style providers (Cloudonix/Vobiz), separate from Carriers.

export type TrunkHealth = "healthy" | "degraded" | "standby";

export interface TelephonyConfig {
  id: string;
  tenant_id: string;
  name: string;
  provider: string;
  credentials: Record<string, unknown>;
  is_default_outbound: boolean;
  health?: { status: TrunkHealth; checked_at: string | null };
  created_at: string;
  updated_at: string;
}

export interface TelephonyConfigCreate {
  name: string;
  provider: string;
  credentials: Record<string, unknown>;
  is_default_outbound?: boolean;
}

export interface TelephonyConfigUpdate {
  name?: string;
  credentials?: Record<string, unknown>;
  is_default_outbound?: boolean;
}

export const listTelephonyConfigs = (tenantId: string) =>
  request<TelephonyConfig[]>(`/tenants/${tenantId}/telephony-configs`);
export const createTelephonyConfig = (tenantId: string, body: TelephonyConfigCreate) =>
  request<TelephonyConfig>(`/tenants/${tenantId}/telephony-configs`, { method: "POST", body: JSON.stringify(body) });
export const getTelephonyConfig = (configId: string) =>
  request<TelephonyConfig>(`/telephony-configs/${configId}`);
export const syncTelephonyNumbers = (configId: string) =>
  request<{ results: (ProviderSync & { did: string })[]; application: ProviderSync | null }>(
    `/telephony-configs/${configId}/sync-numbers`, { method: "POST" },
  );
export const updateTelephonyConfig = (configId: string, body: TelephonyConfigUpdate) =>
  request<TelephonyConfig>(`/telephony-configs/${configId}`, { method: "PATCH", body: JSON.stringify(body) });
export const setDefaultOutboundTelephonyConfig = (configId: string) =>
  request<TelephonyConfig>(`/telephony-configs/${configId}/set-default-outbound`, { method: "POST" });
export const listTelephonyProviders = () =>
  request<Record<string, { required: string[]; sensitive: string[] }>>("/telephony-providers");

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

// `sentiment: null` means never scored, not neutral; render it as "—".
export type CallSentiment = "positive" | "neutral" | "negative" | "frustrated";

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
  sentiment: CallSentiment | null;
  sentiment_reason: string | null;
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

export const getCall = (sessionId: string) => request<Call>(`/calls/${sessionId}`);

export const getTranscript = (sessionId: string) =>
  request<TranscriptEntry[]>(`/calls/${sessionId}/transcript`);

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
// Mirrors services/config/routers/live_calls.py.

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
  // Both null when no cap is set; the UI shows a setup prompt instead.
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

// tenantSlug only for a superadmin; other roles are scoped server-side.
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
// Per-agent, per-LLM-engine voice-to-voice percentiles.

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
export interface DashboardStats {
  total_calls: number;
  total_minutes: number;
  live_calls: number;
  success_count: number;
  failed_count: number;
  outbound_count: number;
  // Raw numerators/denominators, not rates, so they can be summed across tenants.
  ended_count: number;
  aht_sample_count: number;
  aht_duration_ms: number;
  handoff_count: number;
  escalated_count: number;
  prev_total_calls: number;
  prev_ended_count: number;
  prev_aht_sample_count: number;
  prev_aht_duration_ms: number;
  prev_handoff_count: number;
  prev_escalated_count: number;
}

const EMPTY_DASHBOARD_STATS: DashboardStats = {
  total_calls: 0, total_minutes: 0, live_calls: 0, success_count: 0, failed_count: 0,
  outbound_count: 0, ended_count: 0, aht_sample_count: 0, aht_duration_ms: 0,
  handoff_count: 0, escalated_count: 0, prev_total_calls: 0, prev_ended_count: 0,
  prev_aht_sample_count: 0, prev_aht_duration_ms: 0, prev_handoff_count: 0,
  prev_escalated_count: 0,
};

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
      ended_count: acc.ended_count + s.ended_count,
      aht_sample_count: acc.aht_sample_count + s.aht_sample_count,
      aht_duration_ms: acc.aht_duration_ms + s.aht_duration_ms,
      handoff_count: acc.handoff_count + s.handoff_count,
      escalated_count: acc.escalated_count + s.escalated_count,
      prev_total_calls: acc.prev_total_calls + s.prev_total_calls,
      prev_ended_count: acc.prev_ended_count + s.prev_ended_count,
      prev_aht_sample_count: acc.prev_aht_sample_count + s.prev_aht_sample_count,
      prev_aht_duration_ms: acc.prev_aht_duration_ms + s.prev_aht_duration_ms,
      prev_handoff_count: acc.prev_handoff_count + s.prev_handoff_count,
      prev_escalated_count: acc.prev_escalated_count + s.prev_escalated_count,
    }),
    { ...EMPTY_DASHBOARD_STATS },
  );
};

// close_reason is free text; unknown values render as their raw string.
const DISPOSITION_LABELS: Record<string, string> = {
  caller_hangup: "Caller hung up",
  stream_ended: "Stream ended",
  close_timeout: "Closed on timeout",
  session_destroyed: "Session destroyed",
  transport_error: "Transport error",
  reconciled_inactive: "Reconciled (node went silent)",
  TRANSFER_SUCCESS: "Transferred to human",
  TRANSFER_FAILED: "Transfer failed",
  TRANSFER_TIMEOUT: "Transfer timed out",
  unknown: "No close reason recorded",
};

export interface DispositionSlice {
  close_reason: string;
  count: number;
}

export const dispositionLabel = (closeReason: string): string =>
  DISPOSITION_LABELS[closeReason] ?? closeReason;

export const getDispositionMix = (tenantSlug: string, hours: number = 24 * 30) =>
  request<DispositionSlice[]>(`/tenants/${tenantSlug}/calls/disposition-mix?hours=${hours}`);

export const listAllDispositionMix = async (
  tenants: Tenant[], hours: number = 24 * 30,
): Promise<DispositionSlice[]> => {
  const perTenant = await Promise.all(tenants.map((t) => getDispositionMix(t.slug, hours)));
  const byReason = new Map<string, number>();
  for (const slices of perTenant) {
    for (const s of slices) byReason.set(s.close_reason, (byReason.get(s.close_reason) || 0) + s.count);
  }
  return [...byReason.entries()]
    .map(([close_reason, count]) => ({ close_reason, count }))
    .sort((a, b) => b.count - a.count);
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
// Admin CRUD for tool_provider_configs and agent_tool_policies. search_knowledge is
// enabled by linking a knowledge base, not here.

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

// Keep in sync with schema.sql's users_role_check.
export type UserRole = "superadmin" | "admin" | "supervisor" | "agent" | "viewer";

// Must match services/config/deps.py's CONSOLE_ROLES.
export const CONSOLE_ROLES: readonly UserRole[] = ["superadmin", "admin", "viewer"];
export const isConsoleRole = (role: UserRole) => (CONSOLE_ROLES as readonly string[]).includes(role);

export interface User {
  id: string;
  tenant_id: string | null;
  email: string;
  role: UserRole;
  password_set: boolean;
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

// Mirrors schemas.py's SignupSource.
export const SIGNUP_SOURCES = [
  { value: "google_ad", label: "Google Ad" },
  { value: "facebook_ad", label: "Facebook Ad" },
  { value: "linkedin", label: "LinkedIn" },
  { value: "x", label: "Twitter / X" },
  { value: "friend", label: "Friend / Colleague" },
  { value: "youtube", label: "YouTube" },
  { value: "blog", label: "Blog / Article" },
  { value: "product_hunt", label: "Product Hunt" },
  { value: "other", label: "Other" },
] as const;

export interface RegisterRequest {
  organization_name: string;
  first_name: string;
  last_name: string;
  email: string;
  phone: string;
  password: string;
  signup_source: string;
}

export const register = (body: RegisterRequest) =>
  request<{ verification_required: true; email: string }>("/auth/register", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const verifyEmail = (email: string, code: string) =>
  request<LoginResponse>("/auth/verify-email", { method: "POST", body: JSON.stringify({ email, code }) });

export const resendVerificationCode = (email: string) =>
  request<{ sent: boolean }>("/auth/resend-code", { method: "POST", body: JSON.stringify({ email }) });

export const forgotPassword = (email: string) =>
  request<{ sent: boolean }>("/auth/forgot-password", { method: "POST", body: JSON.stringify({ email }) });

export const resetPassword = (email: string, code: string, newPassword: string) =>
  request<LoginResponse>("/auth/reset-password", {
    method: "POST",
    body: JSON.stringify({ email, code, new_password: newPassword }),
  });

export const getCurrentUser = () => request<User>("/auth/me");

// Full-page navigation, not fetch: the Config Service redirects to Google and
// back to /login#token=… (or #error=…).
export const googleSignInUrl = (mode: "signin" | "create") =>
  `${BASE_URL}/auth/oauth/google/start?mode=${mode}`;

export const changePassword = (currentPassword: string, newPassword: string) =>
  request<LoginResponse>("/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });

export const changeEmail = (currentPassword: string, newEmail: string) =>
  request<{ email: string }>("/auth/change-email", {
    method: "POST",
    body: JSON.stringify({ current_password: currentPassword, new_email: newEmail }),
  });

export const confirmEmailChange = (code: string) =>
  request<LoginResponse>("/auth/change-email/confirm", { method: "POST", body: JSON.stringify({ code }) });

// ── Users ────────────────────────────────────────────────────────────────

export interface UserUpdate {
  role?: UserRole;
  tenant_id?: string | null;
  password?: string;
}

// `?tenant_id=` is honored only for superadmin; the server overrides it for scoped callers.
export const listUsers = (tenantId?: string) =>
  request<User[]>(`/users${tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ""}`);
export const updateUser = (userId: string, body: UserUpdate) =>
  request<User>(`/users/${userId}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteUser = (userId: string) =>
  request<void>(`/users/${userId}`, { method: "DELETE" });

// ── Invites ──────────────────────────────────────────────────────────────
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
  // Only on create/resend responses; a failed send leaves the invite pending.
  email_sent?: boolean;
}

export interface InviteCreate {
  email: string;
  role: InviteRole;
  tenant_id?: string | null;
  team?: string | null;
}

// Same `?tenant_id=` scoping rule as listUsers().
export const listInvites = (tenantId?: string) =>
  request<Invite[]>(`/invites${tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ""}`);
export const createInvite = (body: InviteCreate) =>
  request<Invite>("/invites", { method: "POST", body: JSON.stringify(body) });
export const resendInvite = (inviteId: string) =>
  request<Invite>(`/invites/${inviteId}/resend`, { method: "POST" });
export const revokeInvite = (inviteId: string) =>
  request<Invite>(`/invites/${inviteId}/revoke`, { method: "POST" });

// ── Invite accept (public — no JWT) ─────────────────────────────────────
// Token goes in the X-Invite-Token header only, never in a path or query string.

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
// Same bearer token as Config Service.

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

export const releaseNumber = (purchasedNumberId: string) =>
  didRequest<PurchasedNumber>(`/numbers/${purchasedNumberId}/release`, { method: "POST" });

// ── Campaign Service (services/campaigns/, port 8400) ───────────────────
// Same bearer token as Config Service.

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

