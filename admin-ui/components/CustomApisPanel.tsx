"use client";

import { useEffect, useState } from "react";
import {
  AgentToolPolicy,
  ApiError,
  ToolProviderConfig,
  createAgentToolPolicy,
  createToolProviderConfig,
  listAgentToolPolicies,
  listToolProviderConfigs,
  updateAgentToolPolicy,
} from "@/lib/api";
import {
  AgentCustomApi,
  CustomApi,
  CustomApiAuthScheme,
  CustomApiMethod,
  CustomApiParamSpec,
  createCustomApi,
  deleteCustomApi,
  detachAgentCustomApi,
  listAgentCustomApis,
  listCustomApis,
  setAgentCustomApiEnabled,
  updateCustomApi,
} from "@/lib/toolexecApi";
import { SecretRefInput } from "./SecretRefInput";
import { Modal } from "@/components/Modal";

// A per-step budget floor consistent with services/toolexec's own default
// (api.timeout_ms IS NULL -> 6000, see database/schema.sql's custom_apis
// comment) — used here only to size the worst-case-chain-total hint, never
// sent to the server as a real value.
const DEFAULT_STEP_TIMEOUT_MS = 6000;
const DEFAULT_CHAIN_BUDGET_MS = 20000;
const EXECUTE_API_TOOL_NAME = "execute_api";

type ParamForm = CustomApiParamSpec;

interface ApiForm {
  name: string;
  description: string;
  endpoint_url: string;
  method: CustomApiMethod;
  auth_scheme: CustomApiAuthScheme;
  key_ref: string;
  token_ref: string;
  client_id_ref: string;
  client_secret_ref: string;
  side_effecting: boolean;
  timeout_ms: string;
  sensitive_response_paths: string;
  params: ParamForm[];
}

const emptyParam = (): ParamForm => ({
  name: "",
  location: "body",
  json_type: "string",
  description: "",
  required: true,
  source: "caller",
  sensitive: false,
});

const emptyForm = (): ApiForm => ({
  name: "",
  description: "",
  endpoint_url: "",
  method: "GET",
  auth_scheme: "none",
  key_ref: "",
  token_ref: "",
  client_id_ref: "",
  client_secret_ref: "",
  side_effecting: true,
  timeout_ms: "",
  sensitive_response_paths: "",
  params: [],
});

// Client-side estimate only — chain_levels itself is a server-computed
// denormalization (services/toolexec/custom_apis.py's
// _recompute_tenant_chain_levels). This mirrors that shape closely enough
// to warn an admin before they save, not to replace the server's own
// AC 11 rejection.
function estimateChainLevels(params: ParamForm[], allApis: CustomApi[]): number {
  const upstreamIds = params
    .filter((p) => p.source === "upstream" && p.upstream_api_id)
    .map((p) => p.upstream_api_id as string);
  if (upstreamIds.length === 0) return 1;
  const upstreamLevels = upstreamIds.map((id) => allApis.find((a) => a.id === id)?.chain_levels ?? 1);
  return 1 + Math.max(...upstreamLevels);
}

export function CustomApisPanel({ tenantId, agentId }: { tenantId: string; agentId: string }) {
  const [customApis, setCustomApis] = useState<CustomApi[]>([]);
  const [customApisError, setCustomApisError] = useState<string | null>(null);
  const [agentCustomApis, setAgentCustomApis] = useState<AgentCustomApi[]>([]);
  const [agentCustomApisError, setAgentCustomApisError] = useState<string | null>(null);
  const [executeApiPolicy, setExecuteApiPolicy] = useState<AgentToolPolicy | null>(null);
  const [executeApiPolicyError, setExecuteApiPolicyError] = useState<string | null>(null);
  const [toolexecConfig, setToolexecConfig] = useState<ToolProviderConfig | null>(null);
  const [loading, setLoading] = useState(true);

  const [switchSaving, setSwitchSaving] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);
  const [budgetDraft, setBudgetDraft] = useState(String(DEFAULT_CHAIN_BUDGET_MS));

  const [editing, setEditing] = useState<CustomApi | null>(null);
  const [form, setForm] = useState<ApiForm>(emptyForm());
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  // Each source fetched and caught independently (lesson 21): a viewer's
  // page must still show the APIs list even if a write-scoped sibling
  // fetch failed, and one 403 must never blank data the API already
  // returned for the others.
  const refresh = async () => {
    setLoading(true);
    await Promise.allSettled([
      listCustomApis(tenantId)
        .then(setCustomApis)
        .then(() => setCustomApisError(null))
        .catch((e) => setCustomApisError(e instanceof ApiError ? e.detail : String(e))),
      listAgentCustomApis(agentId)
        .then(setAgentCustomApis)
        .then(() => setAgentCustomApisError(null))
        .catch((e) => setAgentCustomApisError(e instanceof ApiError ? e.detail : String(e))),
      listAgentToolPolicies(agentId)
        .then((policies) => {
          const policy = policies.find((p) => p.tool_name === EXECUTE_API_TOOL_NAME) ?? null;
          setExecuteApiPolicy(policy);
          setBudgetDraft(String(policy?.timeout_ms ?? DEFAULT_CHAIN_BUDGET_MS));
        })
        .then(() => setExecuteApiPolicyError(null))
        .catch((e) => setExecuteApiPolicyError(e instanceof ApiError ? e.detail : String(e))),
      listToolProviderConfigs(tenantId, { toolName: EXECUTE_API_TOOL_NAME })
        .then((configs) => setToolexecConfig(configs.find((c) => c.engine === "toolexec") ?? null))
        .catch(() => setToolexecConfig(null)),
    ]);
    setLoading(false);
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, agentId]);

  const agentCustomApiByApiId = new Map(agentCustomApis.map((a) => [a.custom_api_id, a]));

  const handleToggleAgentApi = async (api: CustomApi, enabled: boolean) => {
    try {
      await setAgentCustomApiEnabled(agentId, api.id, enabled);
      await refresh();
    } catch (e) {
      setAgentCustomApisError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleDetach = async (api: CustomApi) => {
    if (!confirm(`Remove "${api.name}" from this agent?`)) return;
    try {
      await detachAgentCustomApi(agentId, api.id);
      await refresh();
    } catch (e) {
      setAgentCustomApisError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleDelete = async (api: CustomApi) => {
    if (!confirm(`Delete "${api.name}" for the whole tenant? This cannot be undone.`)) return;
    try {
      await deleteCustomApi(api.id);
      await refresh();
    } catch (e) {
      setCustomApisError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  // The execute_api master switch: an agent_tool_policies row for
  // tool_name='execute_api', pointed at a per-tenant engine='toolexec'
  // tool_provider_config (internal infrastructure — no api_key_ref, see
  // services/config/routers/tool_provider_configs.py). Created lazily on
  // first enable, same shape as ToolsPanel's own configure-then-attach flow.
  const handleToggleMasterSwitch = async (enabled: boolean) => {
    setSwitchSaving(true);
    setSwitchError(null);
    try {
      if (executeApiPolicy) {
        await updateAgentToolPolicy(agentId, EXECUTE_API_TOOL_NAME, { enabled });
      } else {
        let config = toolexecConfig;
        if (!config) {
          config = await createToolProviderConfig(tenantId, {
            name: "Custom API execution",
            tool_name: EXECUTE_API_TOOL_NAME,
            engine: "toolexec",
          });
        }
        await createAgentToolPolicy(agentId, {
          tool_name: EXECUTE_API_TOOL_NAME,
          tool_provider_config_id: config.id,
          enabled,
          timeout_ms: Number(budgetDraft) || DEFAULT_CHAIN_BUDGET_MS,
        });
      }
      await refresh();
    } catch (e) {
      setSwitchError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSwitchSaving(false);
    }
  };

  const handleSaveBudget = async () => {
    setSwitchSaving(true);
    setSwitchError(null);
    try {
      const timeout_ms = Number(budgetDraft) || DEFAULT_CHAIN_BUDGET_MS;
      if (executeApiPolicy) {
        await updateAgentToolPolicy(agentId, EXECUTE_API_TOOL_NAME, { timeout_ms });
      } else {
        let config = toolexecConfig;
        if (!config) {
          config = await createToolProviderConfig(tenantId, {
            name: "Custom API execution",
            tool_name: EXECUTE_API_TOOL_NAME,
            engine: "toolexec",
          });
        }
        await createAgentToolPolicy(agentId, {
          tool_name: EXECUTE_API_TOOL_NAME,
          tool_provider_config_id: config.id,
          enabled: false,
          timeout_ms,
        });
      }
      await refresh();
    } catch (e) {
      setSwitchError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSwitchSaving(false);
    }
  };

  const openCreate = () => {
    setEditing(null);
    setCreating(true);
    setForm(emptyForm());
    setSaveError(null);
  };

  const openEdit = (api: CustomApi) => {
    setEditing(api);
    setCreating(true);
    setForm({
      name: api.name,
      description: api.description,
      endpoint_url: api.endpoint_url,
      method: api.method,
      auth_scheme: api.auth_scheme,
      key_ref: (api.auth_config.key_ref as string) ?? "",
      token_ref: (api.auth_config.token_ref as string) ?? "",
      client_id_ref: (api.auth_config.client_id_ref as string) ?? "",
      client_secret_ref: (api.auth_config.client_secret_ref as string) ?? "",
      side_effecting: api.side_effecting,
      timeout_ms: api.timeout_ms != null ? String(api.timeout_ms) : "",
      sensitive_response_paths: api.sensitive_response_paths.join("\n"),
      params: api.params.map((p) => ({ ...p })),
    });
    setSaveError(null);
  };

  const closeForm = () => {
    setCreating(false);
    setEditing(null);
  };

  const setParam = (index: number, patch: Partial<ParamForm>) => {
    setForm((f) => ({ ...f, params: f.params.map((p, i) => (i === index ? { ...p, ...patch } : p)) }));
  };

  const addParam = () => setForm((f) => ({ ...f, params: [...f.params, emptyParam()] }));
  const removeParam = (index: number) =>
    setForm((f) => ({ ...f, params: f.params.filter((_, i) => i !== index) }));

  const authConfigFor = (f: ApiForm): Record<string, unknown> => {
    switch (f.auth_scheme) {
      case "api_key":
        return { key_ref: f.key_ref };
      case "bearer":
        return { token_ref: f.token_ref };
      case "oauth2_client_credentials":
        return { client_id_ref: f.client_id_ref, client_secret_ref: f.client_secret_ref };
      default:
        return {};
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const body = {
        name: form.name,
        description: form.description,
        endpoint_url: form.endpoint_url,
        method: form.method,
        auth_scheme: form.auth_scheme,
        auth_config: authConfigFor(form),
        side_effecting: form.side_effecting,
        timeout_ms: form.timeout_ms.trim() ? Number(form.timeout_ms) : null,
        sensitive_response_paths: form.sensitive_response_paths
          .split("\n")
          .map((p) => p.trim())
          .filter(Boolean),
        params: form.params,
      };
      if (editing) {
        await updateCustomApi(editing.id, body);
      } else {
        await createCustomApi(tenantId, body);
      }
      closeForm();
      await refresh();
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSaving(false);
    }
  };

  const missingRequired =
    !form.name.trim() ||
    !form.description.trim() ||
    !form.endpoint_url.trim() ||
    (form.auth_scheme === "api_key" && !form.key_ref.trim()) ||
    (form.auth_scheme === "bearer" && !form.token_ref.trim()) ||
    (form.auth_scheme === "oauth2_client_credentials" && (!form.client_id_ref.trim() || !form.client_secret_ref.trim())) ||
    form.params.some((p) => !p.name.trim() || (p.source === "upstream" && (!p.upstream_api_id || !p.upstream_json_path)));

  // AC-agnostic UI hint (design's stated interim mitigation, not an
  // authoritative check): chain_levels(this api) * a per-step timeout
  // floor of 6000ms, compared against the execute_api master switch's own
  // whole-chain budget — the same calculation an admin needs to see BEFORE
  // saving a 4th level onto a chain that a 20s budget cannot fit.
  const estimatedLevels = estimateChainLevels(form.params, customApis);
  const perStepMs = form.timeout_ms.trim() ? Number(form.timeout_ms) : DEFAULT_STEP_TIMEOUT_MS;
  const worstCaseMs = estimatedLevels * perStepMs;
  const budgetMs = executeApiPolicy?.timeout_ms ?? DEFAULT_CHAIN_BUDGET_MS;
  const worstCaseExceedsBudget = worstCaseMs > budgetMs;

  if (loading) return <div className="empty-state">Loading…</div>;

  return (
    <div className="cols">
      <div className="col-main">
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">execute_api</div>
            <div className="card-sub">master switch — required before any custom API below can run for this agent</div>
          </div>
          {switchError && <div className="error-banner">{switchError}</div>}
          {executeApiPolicyError && <div className="error-banner">{executeApiPolicyError}</div>}
          <div className="kb-row">
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 500 }}>Enable custom API execution</div>
              <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                Turns off entirely, this agent gets zero execute_api tool calls regardless of the toggles below.
              </div>
            </div>
            <label className="toggle-switch" title={executeApiPolicy?.enabled ? "Enabled" : "Disabled"}>
              <input
                type="checkbox"
                checked={!!executeApiPolicy?.enabled}
                disabled={switchSaving}
                onChange={(e) => handleToggleMasterSwitch(e.target.checked)}
              />
              <span className="toggle-slider" />
            </label>
          </div>
          <div className="form-group">
            <label className="form-label">
              Whole-chain budget (ms)
              <span className="hint"> the wall-clock ceiling for one execute_api call, across every step in its chain</span>
            </label>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                className="form-input"
                type="number"
                value={budgetDraft}
                onChange={(e) => setBudgetDraft(e.target.value)}
                style={{ maxWidth: 160 }}
              />
              <button className="btn btn-ghost btn-sm" disabled={switchSaving} onClick={handleSaveBudget}>
                Save
              </button>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Custom APIs</div>
            <div className="card-sub">endpoints this tenant has registered</div>
            <button className="btn btn-primary btn-sm" onClick={openCreate}>
              + New API
            </button>
          </div>
          {customApisError && <div className="error-banner">{customApisError}</div>}
          {agentCustomApisError && <div className="error-banner">{agentCustomApisError}</div>}

          {customApis.map((api) => {
            const assignment = agentCustomApiByApiId.get(api.id);
            return (
              <div key={api.id} className="kb-row">
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 500 }}>{api.name}</div>
                  <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                    {api.method} {api.endpoint_url} · chain_levels={api.chain_levels}
                  </div>
                </div>
                <label
                  className="toggle-switch"
                  title={assignment?.enabled ? "Enabled for this agent" : "Disabled for this agent"}
                >
                  <input
                    type="checkbox"
                    checked={!!assignment?.enabled}
                    onChange={(e) => handleToggleAgentApi(api, e.target.checked)}
                  />
                  <span className="toggle-slider" />
                </label>
                <button className="btn btn-ghost btn-sm" onClick={() => openEdit(api)}>
                  Edit
                </button>
                {assignment && (
                  <button className="btn btn-ghost btn-sm" onClick={() => handleDetach(api)}>
                    Detach
                  </button>
                )}
                <button className="btn btn-danger btn-sm" onClick={() => handleDelete(api)}>
                  Delete
                </button>
              </div>
            );
          })}

          {customApis.length === 0 && !customApisError && <div className="empty-state">No custom APIs registered yet.</div>}
        </div>
      </div>

      <Modal
        open={creating}
        title={editing ? `Edit ${editing.name}` : "New Custom API"}
        onClose={closeForm}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={closeForm}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={saving || missingRequired}>
              {saving ? "Saving…" : editing ? "Save Changes" : "Create API"}
            </button>
          </>
        }
      >
        {saveError && <div className="error-banner">{saveError}</div>}

        {worstCaseExceedsBudget && (
          <div className="error-banner">
            Worst-case chain total: {estimatedLevels} step(s) × {perStepMs}ms = {worstCaseMs}ms — exceeds the
            execute_api whole-chain budget of {budgetMs}ms. Raise the budget above, lower this API&apos;s timeout, or
            shorten the chain.
          </div>
        )}

        <div className="form-group">
          <label className="form-label">
            Name <span className="required">*</span>
            <span className="hint"> LLM-facing api_name, snake_case</span>
          </label>
          <input className="form-input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </div>
        <div className="form-group">
          <label className="form-label">
            Description <span className="required">*</span>
          </label>
          <input
            className="form-input"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
        </div>
        <div className="form-group">
          <label className="form-label">
            Endpoint URL <span className="required">*</span>
          </label>
          <input
            className="form-input"
            value={form.endpoint_url}
            onChange={(e) => setForm({ ...form, endpoint_url: e.target.value })}
          />
        </div>
        <div className="form-group">
          <label className="form-label">Method</label>
          <select
            className="form-select"
            value={form.method}
            onChange={(e) => setForm({ ...form, method: e.target.value as CustomApiMethod })}
          >
            {(["GET", "POST", "PUT", "PATCH", "DELETE"] as const).map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group" style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <label className="toggle-switch">
            <input
              type="checkbox"
              checked={form.side_effecting}
              onChange={(e) => setForm({ ...form, side_effecting: e.target.checked })}
            />
            <span className="toggle-slider" />
          </label>
          <span className="form-label" style={{ margin: 0 }}>
            Side-effecting <span className="hint">never retried; off only for a pure GET lookup</span>
          </span>
        </div>
        <div className="form-group">
          <label className="form-label">
            Per-step timeout (ms) <span className="hint">blank = 6000</span>
          </label>
          <input
            className="form-input"
            type="number"
            value={form.timeout_ms}
            onChange={(e) => setForm({ ...form, timeout_ms: e.target.value })}
            style={{ maxWidth: 160 }}
          />
        </div>
        <div className="form-group">
          <label className="form-label">Auth scheme</label>
          <select
            className="form-select"
            value={form.auth_scheme}
            onChange={(e) => setForm({ ...form, auth_scheme: e.target.value as CustomApiAuthScheme })}
          >
            <option value="none">None</option>
            <option value="api_key">API key</option>
            <option value="bearer">Bearer token</option>
            <option value="oauth2_client_credentials">OAuth2 client credentials</option>
          </select>
        </div>
        {form.auth_scheme === "api_key" && (
          <div className="form-group">
            <label className="form-label">
              Key ref <span className="required">*</span>
              <span className="hint"> tenant-namespaced only — enc:/env:/k8s:</span>
            </label>
            <SecretRefInput value={form.key_ref} onChange={(v) => setForm({ ...form, key_ref: v })} canEncrypt />
          </div>
        )}
        {form.auth_scheme === "bearer" && (
          <div className="form-group">
            <label className="form-label">
              Token ref <span className="required">*</span>
            </label>
            <SecretRefInput value={form.token_ref} onChange={(v) => setForm({ ...form, token_ref: v })} canEncrypt />
          </div>
        )}
        {form.auth_scheme === "oauth2_client_credentials" && (
          <>
            <div className="form-group">
              <label className="form-label">
                Client ID ref <span className="required">*</span>
              </label>
              <SecretRefInput
                value={form.client_id_ref}
                onChange={(v) => setForm({ ...form, client_id_ref: v })}
                canEncrypt
              />
            </div>
            <div className="form-group">
              <label className="form-label">
                Client secret ref <span className="required">*</span>
              </label>
              <SecretRefInput
                value={form.client_secret_ref}
                onChange={(v) => setForm({ ...form, client_secret_ref: v })}
                canEncrypt
              />
            </div>
          </>
        )}
        <div className="form-group">
          <label className="form-label">Sensitive response paths <span className="hint">one JSON path per line, e.g. $.customer.ssn</span></label>
          <textarea
            className="form-input"
            rows={2}
            value={form.sensitive_response_paths}
            onChange={(e) => setForm({ ...form, sensitive_response_paths: e.target.value })}
          />
        </div>

        <div className="form-group">
          <label className="form-label">Parameters</label>
          {form.params.map((p, i) => (
            <div key={i} className="kb-row" style={{ flexWrap: "wrap", gap: 8 }}>
              <input
                className="form-input"
                placeholder="name"
                value={p.name}
                onChange={(e) => setParam(i, { name: e.target.value })}
                style={{ maxWidth: 140 }}
              />
              <select
                className="form-select"
                value={p.location}
                onChange={(e) => setParam(i, { location: e.target.value as ParamForm["location"] })}
                style={{ maxWidth: 110 }}
              >
                {(["body", "query", "header", "path"] as const).map((l) => (
                  <option key={l} value={l}>
                    {l}
                  </option>
                ))}
              </select>
              <select
                className="form-select"
                value={p.json_type}
                onChange={(e) => setParam(i, { json_type: e.target.value as ParamForm["json_type"] })}
                style={{ maxWidth: 110 }}
              >
                {(["string", "number", "integer", "boolean", "object", "array"] as const).map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
              <select
                className="form-select"
                value={p.source}
                onChange={(e) => setParam(i, { source: e.target.value as ParamForm["source"] })}
                style={{ maxWidth: 110 }}
              >
                <option value="caller">caller</option>
                <option value="literal">literal</option>
                <option value="upstream">upstream</option>
              </select>
              {p.source === "literal" && (
                <input
                  className="form-input"
                  placeholder="literal value (JSON)"
                  value={p.literal_value != null ? JSON.stringify(p.literal_value) : ""}
                  onChange={(e) => {
                    try {
                      setParam(i, { literal_value: JSON.parse(e.target.value) });
                    } catch {
                      setParam(i, { literal_value: e.target.value });
                    }
                  }}
                  style={{ maxWidth: 160 }}
                />
              )}
              {p.source === "upstream" && (
                <>
                  <select
                    className="form-select"
                    value={p.upstream_api_id ?? ""}
                    onChange={(e) => setParam(i, { upstream_api_id: e.target.value })}
                    style={{ maxWidth: 160 }}
                  >
                    <option value="">select upstream API…</option>
                    {customApis
                      .filter((a) => a.id !== editing?.id)
                      .map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.name}
                        </option>
                      ))}
                  </select>
                  <input
                    className="form-input"
                    placeholder="$.data.id"
                    value={p.upstream_json_path ?? ""}
                    onChange={(e) => setParam(i, { upstream_json_path: e.target.value })}
                    style={{ maxWidth: 140 }}
                  />
                </>
              )}
              <label className="toggle-switch" title="Sensitive — redacted in logs and chain history">
                <input
                  type="checkbox"
                  checked={!!p.sensitive}
                  onChange={(e) => setParam(i, { sensitive: e.target.checked })}
                />
                <span className="toggle-slider" />
              </label>
              <button className="btn btn-danger btn-sm" onClick={() => removeParam(i)}>
                ✕
              </button>
            </div>
          ))}
          <button className="btn btn-ghost btn-sm" onClick={addParam}>
            + Add parameter
          </button>
        </div>
      </Modal>
    </div>
  );
}
