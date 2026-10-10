// Tool Execution Service client. Uses the Config Service JWT; toolexec only validates tokens.

import { getToken } from "./auth";
import { ApiError } from "./api";

const BASE_URL = process.env.NEXT_PUBLIC_TOOLEXEC_SERVICE_URL || "http://localhost:8600";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
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

// ── Custom APIs (services/toolexec/schemas.py) ──────────────────────────

export type CustomApiParamLocation = "body" | "query" | "header" | "path";
export type CustomApiParamJsonType = "string" | "number" | "integer" | "boolean" | "object" | "array";
export type CustomApiParamSource = "literal" | "caller" | "upstream" | "caller_id";

export interface CustomApiParamSpec {
  name: string;
  location: CustomApiParamLocation;
  json_type: CustomApiParamJsonType;
  description?: string;
  required?: boolean;
  source: CustomApiParamSource;
  literal_value?: unknown;
  upstream_api_id?: string | null;
  upstream_json_path?: string | null;
  sensitive?: boolean;
}

export type CustomApiMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type CustomApiAuthScheme =
  | "none"
  | "api_key"
  | "bearer"
  | "oauth2_client_credentials"
  | "oauth2_authorization_code";

export interface CustomApi {
  id: string;
  tenant_id: string;
  name: string;
  description: string;
  endpoint_url: string;
  method: CustomApiMethod;
  body_style: "json" | "form";
  auth_scheme: CustomApiAuthScheme;
  auth_config: Record<string, unknown>;
  side_effecting: boolean;
  idempotency_header: string | null;
  timeout_ms: number | null;
  sensitive_response_paths: string[];
  success_template: string | null;
  chain_levels: number;
  created_at: string;
  updated_at: string;
  preset_key: string | null;
  oauth_connection_id: string | null;
  params: CustomApiParamSpec[];
}

// Plaintext credentials, sealed to the tenant by the server. The API never
// returns one: a saved credential reads back as "[stored]" in auth_config,
// and an empty string means it was quarantined and must be entered again.
export type CustomApiAuthSecrets = Partial<
  Record<"key_ref" | "token_ref" | "client_id_ref" | "client_secret_ref", string>
>;

export interface CustomApiCreate {
  name: string;
  description: string;
  endpoint_url: string;
  method: CustomApiMethod;
  body_style?: "json" | "form";
  auth_scheme?: CustomApiAuthScheme;
  auth_config?: Record<string, unknown>;
  auth_secrets?: CustomApiAuthSecrets;
  side_effecting?: boolean;
  idempotency_header?: string | null;
  timeout_ms?: number | null;
  sensitive_response_paths?: string[];
  success_template?: string | null;
  params?: CustomApiParamSpec[];
}

export type CustomApiUpdate = Partial<CustomApiCreate>;

export const listCustomApis = (tenantId: string) => request<CustomApi[]>(`/tenants/${tenantId}/custom-apis`);
export const createCustomApi = (tenantId: string, body: CustomApiCreate) =>
  request<CustomApi>(`/tenants/${tenantId}/custom-apis`, { method: "POST", body: JSON.stringify(body) });
export const getCustomApi = (customApiId: string) => request<CustomApi>(`/custom-apis/${customApiId}`);
export const updateCustomApi = (customApiId: string, body: CustomApiUpdate) =>
  request<CustomApi>(`/custom-apis/${customApiId}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteCustomApi = (customApiId: string) =>
  request<void>(`/custom-apis/${customApiId}`, { method: "DELETE" });

// ── OAuth connectors (services/toolexec/routers/oauth_connections.py) ───

export type OAuthProviderKey = "google" | "zoho" | "microsoft" | "salesforce" | "hubspot" | "calcom";
export type OAuthConnectionStatus = "connected" | "reconnect_needed" | "disconnected";

export interface OAuthProvider {
  key: OAuthProviderKey;
  label: string;
  auth_kind: "oauth2" | "api_key";
}

export interface OAuthConnection {
  id: string;
  provider: OAuthProviderKey;
  status: OAuthConnectionStatus;
  account_label: string | null;
  scopes: string[];
  updated_at: string;
}

export const listOAuthProviders = () => request<OAuthProvider[]>("/oauth-providers");
export const listOAuthConnections = (tenantId: string) =>
  request<OAuthConnection[]>(`/tenants/${tenantId}/oauth-connections`);
export const authorizeOAuthConnection = (tenantId: string, provider: string, presetKey: string | null) =>
  request<{ authorize_url: string }>(`/tenants/${tenantId}/oauth-connections/${provider}/authorize`, {
    method: "POST",
    body: JSON.stringify({ preset_key: presetKey }),
  });
// A provider with no consent redirect (cal.com): the key is sent once, sealed by the server, never returned.
export const connectApiKey = (tenantId: string, provider: string, apiKey: string) =>
  request<OAuthConnection>(`/tenants/${tenantId}/oauth-connections/${provider}/api-key`, {
    method: "POST",
    body: JSON.stringify({ api_key: apiKey }),
  });
export const completeOAuthCallback = (
  tenantId: string,
  body: { state: string; code: string; accounts_server: string | null },
) =>
  request<OAuthConnection>(`/tenants/${tenantId}/oauth-connections/callback`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const disconnectOAuthConnection = (tenantId: string, connectionId: string) =>
  request<{ disconnected: boolean }>(`/tenants/${tenantId}/oauth-connections/${connectionId}`, {
    method: "DELETE",
  });

// ── Connector presets (services/toolexec/routers/connector_presets.py) ──

export interface ConnectorPreset {
  key: string;
  title: string;
  provider: OAuthProviderKey | null;
  setup_schema: Record<string, unknown>;
}

// Each preset's own setup model; preset_key is the discriminator the server routes on.
export type PresetSetup = { preset_key: string } & Record<string, string | number>;

export const listConnectorPresets = () => request<ConnectorPreset[]>("/connector-presets");
export const applyConnectorPreset = (tenantId: string, setup: PresetSetup) =>
  request<CustomApi[]>(`/tenants/${tenantId}/connector-presets/${setup.preset_key}/apply`, {
    method: "POST",
    body: JSON.stringify(setup),
  });
export const removeConnectorPreset = (tenantId: string, presetKey: string) =>
  request<void>(`/tenants/${tenantId}/connector-presets/${presetKey}`, { method: "DELETE" });

// ── Agent ↔ Custom API enablement (services/toolexec/agent_apis.py) ─────

export interface AgentCustomApi {
  id: string;
  agent_id: string;
  custom_api_id: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  name: string;
  chain_levels: number;
}

export const listAgentCustomApis = (agentId: string) => request<AgentCustomApi[]>(`/agents/${agentId}/custom-apis`);
export const setAgentCustomApiEnabled = (agentId: string, customApiId: string, enabled: boolean) =>
  request<AgentCustomApi>(`/agents/${agentId}/custom-apis/${customApiId}`, {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  });
export const detachAgentCustomApi = (agentId: string, customApiId: string) =>
  request<void>(`/agents/${agentId}/custom-apis/${customApiId}`, { method: "DELETE" });

// ── Chain history (services/toolexec/routers/chain_runs.py, AC 14) ──────

export interface ChainStep {
  id: number;
  run_id: string;
  step_index: number;
  custom_api_id: string;
  api_name: string;
  level: number;
  session_id: string | null;
  status: "claimed" | "success" | "failed" | "timeout" | "skipped" | "invalid_argument" | "unavailable";
  http_status: number | null;
  error: string | null;
  arguments_redacted: Record<string, unknown> | null;
  response_redacted: Record<string, unknown> | null;
  argument_sources: Record<string, string> | null;
  side_effecting: boolean;
  idempotency_key: string | null;
  duration_ms: number | null;
  created_at: string;
}

export interface ChainRun {
  id: string;
  tenant_id: string;
  agent_id: string;
  call_id: string | null;
  session_id: string | null;
  turn_id: string | null;
  tool_call_id: string;
  idempotency_key: string;
  target_api_id: string;
  status: "running" | "success" | "partial" | "failed" | "timeout" | "invalid_argument" | "unavailable";
  error: string | null;
  started_at: string;
  finished_at: string | null;
  steps: ChainStep[];
}

export const getChainRuns = (sessionId: string) => request<ChainRun[]>(`/calls/${sessionId}/chain-runs`);
