"use client";

import { useEffect, useRef, useState } from "react";
import { AlertCircle, X } from "lucide-react";
import Link from "next/link";
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
  listAgentCustomApis,
  listCustomApis,
  setAgentCustomApiEnabled,
} from "@/lib/toolexecApi";

// toolexec's default when timeout_ms is NULL; only used for the chain-budget warning.
const DEFAULT_STEP_TIMEOUT_MS = 6000;
const DEFAULT_CHAIN_BUDGET_MS = 20000;
const EXECUTE_API_TOOL_NAME = "execute_api";

// Attach-only multi-select of the tenant's custom APIs for one agent, plus the execute_api switch and
// chain budget. Create/edit/delete live in CustomApisPanel.
export function AgentCustomApisPanel({ tenantId, agentId }: { tenantId: string; agentId: string }) {
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
  const [budgetDraft, setBudgetDraft] = useState(String(DEFAULT_CHAIN_BUDGET_MS / 1000));
  const [pickerOpen, setPickerOpen] = useState(false);
  const [query, setQuery] = useState("");
  const pickerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!pickerOpen) return;
    const close = (e: MouseEvent) => {
      if (!pickerRef.current?.contains(e.target as Node)) setPickerOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [pickerOpen]);

  // Each source is caught independently so one 403 doesn't blank the others.
  const refresh = async () => {
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
          setBudgetDraft(String((policy?.timeout_ms ?? DEFAULT_CHAIN_BUDGET_MS) / 1000));
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
      if (enabled && !executeApiPolicy?.enabled) await handleToggleMasterSwitch(true);
      else await refresh();
    } catch (e) {
      setAgentCustomApisError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  // execute_api = an agent_tool_policies row pointing at a per-tenant engine='toolexec'
  // tool_provider_config, created lazily on first enable.
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
          timeout_ms: Math.round(Number(budgetDraft) * 1000) || DEFAULT_CHAIN_BUDGET_MS,
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
      const timeout_ms = Math.round(Number(budgetDraft) * 1000) || DEFAULT_CHAIN_BUDGET_MS;
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

  const budgetMs = executeApiPolicy?.timeout_ms ?? DEFAULT_CHAIN_BUDGET_MS;
  const paused = !executeApiPolicy?.enabled && agentCustomApis.some((a) => a.enabled);
  const selected = customApis.filter((api) => agentCustomApiByApiId.get(api.id)?.enabled);
  const choices = customApis.filter(
    (api) => !agentCustomApiByApiId.get(api.id)?.enabled && api.name.toLowerCase().includes(query.trim().toLowerCase()),
  );
  const slow = selected.filter((api) => api.chain_levels * (api.timeout_ms ?? DEFAULT_STEP_TIMEOUT_MS) > budgetMs);

  const add = (api: CustomApi) => {
    setQuery("");
    void handleToggleAgentApi(api, true);
  };

  if (loading) return <div className="empty-state">Loading…</div>;

  return (
    <section className="card">
      <div className="card-hdr">
        <div>
          <div className="card-title">Tools</div>
          <div className="card-sub" style={{ marginLeft: 0 }}>
            Let the agent check your own systems during calls, like an order or a booking. Changes save right away.
          </div>
        </div>
      </div>
      {switchError && <div className="error-banner">{switchError}</div>}
      {executeApiPolicyError && <div className="error-banner">{executeApiPolicyError}</div>}
      {customApisError && <div className="error-banner">{customApisError}</div>}
      {agentCustomApisError && <div className="error-banner">{agentCustomApisError}</div>}

      {paused && (
        <div className="kb-row" style={{ background: "var(--amber-bg, transparent)" }}>
          <div style={{ flex: 1, fontSize: ".78rem" }}>Tools are paused for this agent.</div>
          <button className="btn btn-primary btn-sm" disabled={switchSaving} onClick={() => handleToggleMasterSwitch(true)}>
            Turn on
          </button>
        </div>
      )}

      <div className="card-body">
        {customApis.length === 0 && !customApisError ? (
          <div className="empty-state">
            No tools yet. <Link href="/integrations#custom-tools">Set up a connection</Link>, then pick it here.
          </div>
        ) : (
          <div className="kb-picker" ref={pickerRef}>
            <div className="kb-chips" onClick={() => setPickerOpen(true)}>
              {selected.map((api) => (
                <span key={api.id} className="kb-chip" title={api.description ?? undefined}>
                  <span className="kb-chip-title">{api.name}</span>
                  <button
                    type="button"
                    className="kb-chip-remove"
                    aria-label={`Remove ${api.name}`}
                    onClick={(e) => { e.stopPropagation(); void handleToggleAgentApi(api, false); }}
                  >
                    <X size={13} />
                  </button>
                </span>
              ))}
              <input
                className="kb-chips-input"
                placeholder={selected.length ? "" : "Add a tool…"}
                role="combobox"
                aria-label="Add a tool"
                aria-expanded={pickerOpen}
                aria-controls="tool-picker-menu"
                value={query}
                onFocus={() => setPickerOpen(true)}
                onChange={(e) => { setQuery(e.target.value); setPickerOpen(true); }}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setPickerOpen(false);
                  if (e.key === "Backspace" && !query && selected.length) void handleToggleAgentApi(selected[selected.length - 1], false);
                }}
              />
            </div>
            {pickerOpen && (
              <div id="tool-picker-menu" className="kb-picker-menu" role="listbox" aria-label="Tools to add">
                {choices.length === 0 ? (
                  <div className="kb-picker-empty">{query ? "No tools match." : "Every tool is selected."}</div>
                ) : (
                  choices.map((api) => (
                    <button key={api.id} type="button" role="option" aria-selected={false} className="kb-picker-option"
                      onClick={() => add(api)}>
                      <span>{api.name}</span>
                      {api.description && <span className="kb-picker-desc">{api.description}</span>}
                    </button>
                  ))
                )}
              </div>
            )}
            <div className="kb-picker-hint">Click a tool to add it. Connections are set up on the Integrations page.</div>
          </div>
        )}
        {slow.map((api) => (
          <div key={api.id} className="voice-missing">
            <AlertCircle size={13} />
            <span>
              {api.name} may be too slow: it can take up to {(api.chain_levels * (api.timeout_ms ?? DEFAULT_STEP_TIMEOUT_MS)) / 1000}s,
              but the agent only waits {budgetMs / 1000}s.
            </span>
          </div>
        ))}
        {customApis.length > 0 && (
          <details style={{ marginTop: 12 }}>
            <summary style={{ cursor: "pointer", fontSize: ".78rem", color: "var(--text-2)" }}>Advanced</summary>
            <div className="form-group" style={{ marginTop: 12 }}>
              <label className="form-label">
                Longest wait for an answer <span className="hint">in seconds</span>
              </label>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  className="form-input"
                  type="number"
                  min={1}
                  step={0.5}
                  value={budgetDraft}
                  onChange={(e) => setBudgetDraft(e.target.value)}
                  style={{ maxWidth: 160 }}
                />
                <button className="btn btn-ghost btn-sm" disabled={switchSaving} onClick={handleSaveBudget}>
                  Save
                </button>
              </div>
            </div>
          </details>
        )}
      </div>
      {customApis.length > 0 && (
        <div className="kb-picker-count">
          <strong>{selected.length}</strong> of {customApis.length} selected ·{" "}
          <Link href="/integrations#custom-tools" style={{ color: "var(--cyan)" }}>Manage connections</Link>
        </div>
      )}
    </section>
  );
}
