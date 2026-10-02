"use client";

import { useEffect, useState } from "react";
import {
  AgentToolPolicy,
  ApiError,
  ToolCatalogEngine,
  ToolCatalogEntry,
  createAgentToolPolicy,
  createToolProviderConfig,
  deleteAgentToolPolicy,
  getToolProviderConfig,
  listAgentToolPolicies,
  listToolCatalog,
  updateAgentToolPolicy,
  updateToolProviderConfig,
} from "@/lib/api";
import { SecretRefInput, secretPayload } from "./SecretRefInput";
import { Modal } from "@/components/Modal";

// Converted to real JSON types only when building the save payload.
type ExtraValue = string | boolean;

export function ToolsPanel({ tenantId, agentId }: { tenantId: string; agentId: string }) {
  const [catalog, setCatalog] = useState<ToolCatalogEntry[]>([]);
  const [policies, setPolicies] = useState<AgentToolPolicy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [configuring, setConfiguring] = useState<{ entry: ToolCatalogEntry; engine: ToolCatalogEngine } | null>(null);
  // Set only when editing an existing tool_provider_config (vs. creating a
  // new one) — same modal, different save action.
  const [editingConfigId, setEditingConfigId] = useState<string | null>(null);
  const [configName, setConfigName] = useState("");
  const [configApiKeyRef, setConfigApiKeyRef] = useState("");
  const [configOriginalApiKeyRef, setConfigOriginalApiKeyRef] = useState("");
  const [configExtra, setConfigExtra] = useState<Record<string, ExtraValue>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [loadingExisting, setLoadingExisting] = useState(false);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const [cat, pol] = await Promise.all([listToolCatalog(), listAgentToolPolicies(agentId)]);
      setCatalog(cat);
      setPolicies(pol);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, agentId]);

  const attachedToolNames = new Set(policies.map((p) => p.tool_name));
  const availableEntries = catalog.filter((e) => !attachedToolNames.has(e.tool_name));

  const withErrorHandling = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleToggleEnabled = (policy: AgentToolPolicy, enabled: boolean) =>
    withErrorHandling(() => updateAgentToolPolicy(agentId, policy.tool_name, { enabled }));

  const handleRemove = (policy: AgentToolPolicy) => {
    if (!confirm(`Remove "${policy.tool_provider_config_name}" from this agent's tools?`)) return;
    withErrorHandling(() => deleteAgentToolPolicy(agentId, policy.tool_name));
  };

  const defaultExtraFor = (engine: ToolCatalogEngine): Record<string, ExtraValue> =>
    Object.fromEntries(engine.extra_fields.map((f) => [f.key, f.type === "boolean" ? false : ""]));

  const openConfig = (entry: ToolCatalogEntry, engine: ToolCatalogEngine) => {
    setConfiguring({ entry, engine });
    setEditingConfigId(null);
    setConfigName(`${entry.display_name} (${engine.display_name})`);
    setConfigApiKeyRef("");
    setConfigOriginalApiKeyRef("");
    setConfigExtra(defaultExtraFor(engine));
    setSaveError(null);
  };

  const openEditConfig = async (policy: AgentToolPolicy) => {
    const entry = catalog.find((e) => e.tool_name === policy.tool_name);
    const engine = entry?.engines.find((en) => en.engine === policy.tool_provider_config_engine);
    if (!entry || !engine) return;
    setLoadingExisting(true);
    setError(null);
    try {
      const cfg = await getToolProviderConfig(policy.tool_provider_config_id);
      setConfiguring({ entry, engine });
      setEditingConfigId(cfg.id);
      setConfigName(cfg.name);
      setConfigApiKeyRef(cfg.api_key_ref ?? "");
      setConfigOriginalApiKeyRef(cfg.api_key_ref ?? "");
      const extra = defaultExtraFor(engine);
      for (const field of engine.extra_fields) {
        const stored = cfg.extra?.[field.key];
        if (stored === undefined || stored === null) continue;
        extra[field.key] = field.type === "boolean" ? Boolean(stored) : String(stored);
      }
      setConfigExtra(extra);
      setSaveError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setLoadingExisting(false);
    }
  };

  const handleSaveConfig = async () => {
    if (!configuring) return;
    setSaving(true);
    setSaveError(null);
    try {
      const extra: Record<string, unknown> = {};
      for (const field of configuring.engine.extra_fields) {
        if (field.type === "boolean") {
          extra[field.key] = Boolean(configExtra[field.key]);
          continue;
        }
        const raw = (configExtra[field.key] as string) ?? "";
        if (raw === "") continue;
        extra[field.key] = field.type === "number" ? Number(raw) : raw;
      }

      if (editingConfigId) {
        await updateToolProviderConfig(editingConfigId, {
          name: configName,
          ...secretPayload(configApiKeyRef, configOriginalApiKeyRef),
          extra,
        });
      } else {
        const providerConfig = await createToolProviderConfig(tenantId, {
          name: configName,
          tool_name: configuring.entry.tool_name,
          engine: configuring.engine.engine,
          ...secretPayload(configApiKeyRef),
          extra,
        });
        await createAgentToolPolicy(agentId, {
          tool_name: configuring.entry.tool_name,
          tool_provider_config_id: providerConfig.id,
        });
      }
      setConfiguring(null);
      setEditingConfigId(null);
      await refresh();
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSaving(false);
    }
  };

  const missingRequired =
    !configApiKeyRef.trim() ||
    (configuring?.engine.extra_fields.some(
      (f) => f.type !== "boolean" && f.required && !((configExtra[f.key] as string) ?? "").trim(),
    ) ?? true);

  if (loading) return <div className="empty-state">Loading…</div>;

  return (
    <div className="cols">
      <div className="col-main">
        {error && <div className="error-banner">{error}</div>}

        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Tools</div>
            <div className="card-sub">what this agent can do beyond talking</div>
          </div>

          {policies.map((p) => (
            <div key={p.id} className="kb-row">
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 500 }}>{p.tool_provider_config_name}</div>
                <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                  {p.tool_name} · {p.tool_provider_config_engine}
                </div>
              </div>
              <label className="toggle-switch" title={p.enabled ? "Enabled" : "Disabled — never offered to the LLM"}>
                <input type="checkbox" checked={p.enabled} onChange={(e) => handleToggleEnabled(p, e.target.checked)} />
                <span className="toggle-slider" />
              </label>
              <button
                className="btn btn-ghost btn-sm"
                disabled={loadingExisting}
                title={`Edit ${p.tool_provider_config_name}`}
                onClick={() => openEditConfig(p)}
              >
                Edit
              </button>
              <button className="btn btn-danger btn-sm" onClick={() => handleRemove(p)}>
                Remove
              </button>
            </div>
          ))}

          {availableEntries.map((entry) =>
            entry.engines.map((engine) => (
              <div key={`${entry.tool_name}:${engine.engine}`} className="kb-row">
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 500 }}>{entry.display_name}</div>
                  <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>via {engine.display_name}</div>
                </div>
                <button
                  className="btn btn-primary btn-sm"
                  style={{ width: 26, height: 26, padding: 0, fontSize: "1rem", lineHeight: 1 }}
                  title={`Add ${entry.display_name}`}
                  onClick={() => openConfig(entry, engine)}
                >
                  +
                </button>
              </div>
            )),
          )}

          {policies.length === 0 && availableEntries.length === 0 && (
            <div className="empty-state">No tools available.</div>
          )}
        </div>
      </div>

      <Modal
        open={!!configuring}
        title={
          configuring
            ? `${editingConfigId ? "Edit" : "Configure"} ${configuring.entry.display_name}`
            : ""
        }
        onClose={() => setConfiguring(null)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setConfiguring(null)}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" onClick={handleSaveConfig} disabled={saving || !configName || missingRequired}>
              {saving ? "Saving…" : editingConfigId ? "Save Changes" : "Add Tool"}
            </button>
          </>
        }
      >
        {saveError && <div className="error-banner">{saveError}</div>}
        {configuring && (
          <>
            <div className="form-group">
              <label className="form-label">
                Name <span className="required">*</span>
              </label>
              <input className="form-input" value={configName} onChange={(e) => setConfigName(e.target.value)} />
            </div>
            <div className="form-group">
              <label className="form-label">Provider</label>
              <input className="form-input" value={configuring.engine.display_name} disabled />
            </div>
            <div className="form-group">
              <label className="form-label">
                API Key <span className="required">*</span>
              </label>
              <SecretRefInput value={configApiKeyRef} onChange={setConfigApiKeyRef} canEncrypt />
            </div>
            {configuring.engine.extra_fields.map((field) =>
              field.type === "boolean" ? (
                <div className="form-group" key={field.key} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <label className="toggle-switch">
                    <input
                      type="checkbox"
                      checked={Boolean(configExtra[field.key])}
                      onChange={(e) => setConfigExtra({ ...configExtra, [field.key]: e.target.checked })}
                    />
                    <span className="toggle-slider" />
                  </label>
                  <span className="form-label" style={{ margin: 0 }}>
                    {field.label}
                    {field.help && <span className="hint"> {field.help}</span>}
                  </span>
                </div>
              ) : (
                <div className="form-group" key={field.key}>
                  <label className="form-label">
                    {field.label} {field.required && <span className="required">*</span>}
                    {field.help && <span className="hint"> {field.help}</span>}
                  </label>
                  <input
                    className="form-input"
                    type={field.type === "number" ? "number" : "text"}
                    value={(configExtra[field.key] as string) ?? ""}
                    onChange={(e) => setConfigExtra({ ...configExtra, [field.key]: e.target.value })}
                  />
                </div>
              ),
            )}
          </>
        )}
      </Modal>
    </div>
  );
}
