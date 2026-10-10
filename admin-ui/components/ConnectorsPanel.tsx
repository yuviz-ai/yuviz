"use client";

import { useEffect, useMemo, useState } from "react";
import { ApiError, getCurrentUser } from "@/lib/api";
import {
  ConnectorPreset,
  CustomApi,
  OAuthConnection,
  OAuthProvider,
  PresetSetup,
  applyConnectorPreset,
  authorizeOAuthConnection,
  connectApiKey,
  disconnectOAuthConnection,
  listConnectorPresets,
  listCustomApis,
  listOAuthConnections,
  listOAuthProviders,
  removeConnectorPreset,
} from "@/lib/toolexecApi";
import { Modal } from "@/components/Modal";

// What the callback page needs after the provider sends the browser back: the
// tenant is the one this page was showing, and returnTo is where to land.
export const OAUTH_CONTEXT_KEY = "yuviz_oauth_context";
export interface OAuthContext {
  tenantId: string;
  provider: string;
  returnTo: string;
}

const STATUS_BADGE: Record<OAuthConnection["status"], { label: string; cls: string }> = {
  connected: { label: "Connected", cls: "green" },
  reconnect_needed: { label: "Reconnect needed", cls: "amber" },
  disconnected: { label: "Disconnected", cls: "gray" },
};

// Catalogue entries that ship dark: each is shown as "Not available" until the
// server lists it, so an operator's env switch is the only thing that turns it on.
const DARK_ENTRIES = [
  { label: "Cal.com", isLive: (providers: OAuthProvider[]) => providers.some((p) => p.key === "calcom") },
  { label: "Dynamics 365", isLive: (_: OAuthProvider[], presets: ConnectorPreset[]) => presets.some((p) => p.key === "dynamics_crm") },
];

// The preset 409s the server uses to say a Google grant is missing or too narrow.
const CONNECTOR_REQUIRED = new Set(["connector_required", "connector_scope_required"]);

type FieldDef = {
  name: string;
  label: string;
  hint?: string;
  type?: "text" | "password" | "number" | "select";
  options?: string[];
  // Hidden fields are neither shown nor sent: the server rejects a field that
  // does not belong to the chosen provider.
  when?: (values: Record<string, string>) => boolean;
};

// Read off services/toolexec/schemas.py's setup models; the server validates
// every value again, so these only decide what to ask for.
const PRESET_FIELDS: Record<string, FieldDef[]> = {
  calendar_booking: [
    { name: "calendar_id", label: "Calendar ID", hint: "\"primary\" is the connected account's own calendar" },
    { name: "timezone", label: "Timezone", hint: "IANA name, e.g. Asia/Kolkata" },
    { name: "day_start", label: "Day starts (HH:MM)" },
    { name: "day_end", label: "Day ends (HH:MM)" },
    { name: "slot_minutes", label: "Slot length (minutes)", type: "number" },
  ],
  whatsapp_confirmation: [
    { name: "provider", label: "WhatsApp provider", type: "select", options: ["gupshup", "interakt", "meta"] },
    { name: "api_key", label: "API key", type: "password", hint: "Stored encrypted; it is never shown again" },
    { name: "template", label: "Approved template name" },
    { name: "language", label: "Template language", hint: "e.g. en or en_US" },
    { name: "param_count", label: "Template variables", type: "number", hint: "0 to 5, filled in by the agent" },
    { name: "source_number", label: "Source number", hint: "Digits only", when: (v) => v.provider === "gupshup" },
    { name: "app_name", label: "Gupshup app name", when: (v) => v.provider === "gupshup" },
    { name: "phone_number_id", label: "Phone number ID", when: (v) => v.provider === "meta" },
  ],
  sheets_lead_capture: [
    { name: "title", label: "Spreadsheet title", hint: "A new sheet with this name is created in the connected account" },
  ],
};

const presetDefaults = (key: string): Record<string, string> => {
  switch (key) {
    case "calendar_booking":
      return {
        calendar_id: "primary",
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        day_start: "09:00",
        day_end: "17:00",
        slot_minutes: "30",
      };
    case "whatsapp_confirmation":
      return {
        provider: "gupshup", api_key: "", template: "", language: "en", param_count: "3",
        source_number: "", app_name: "", phone_number_id: "",
      };
    case "sheets_lead_capture":
      return { title: "Yuviz leads" };
    default:
      return {};
  }
};

const setupFor = (key: string, values: Record<string, string>): PresetSetup => {
  const setup: PresetSetup = { preset_key: key };
  for (const field of PRESET_FIELDS[key] ?? []) {
    if (field.when && !field.when(values)) continue;
    const value = values[field.name].trim();
    if (value) setup[field.name] = field.type === "number" ? Number(value) : value;
  }
  return setup;
};

export function ConnectorsPanel({ tenantId }: { tenantId: string }) {
  const [providers, setProviders] = useState<OAuthProvider[]>([]);
  const [connections, setConnections] = useState<OAuthConnection[]>([]);
  const [presets, setPresets] = useState<ConnectorPreset[]>([]);
  const [apis, setApis] = useState<CustomApi[]>([]);
  const [canManage, setCanManage] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const [applying, setApplying] = useState<ConnectorPreset | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);

  const [pasting, setPasting] = useState<OAuthProvider | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [keyError, setKeyError] = useState<string | null>(null);

  // The four reads are independent: one failing must not blank what the
  // others returned.
  const refresh = async () => {
    const [p, c, pr, a] = await Promise.allSettled([
      listOAuthProviders(),
      listOAuthConnections(tenantId),
      listConnectorPresets(),
      listCustomApis(tenantId),
    ]);
    if (p.status === "fulfilled") setProviders(p.value);
    if (c.status === "fulfilled") setConnections(c.value);
    if (pr.status === "fulfilled") setPresets(pr.value);
    if (a.status === "fulfilled") setApis(a.value);
    const failed = [p, c, pr, a].find((r) => r.status === "rejected");
    setError(failed ? errorText(failed.reason) : null);
    setLoading(false);
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    getCurrentUser()
      .then((u) => setCanManage(u.role === "superadmin" || u.role === "admin"))
      .catch(() => setCanManage(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

  const connectionFor = (provider: string) => connections.find((c) => c.provider === provider && c.status !== "disconnected");
  const rowsFor = (presetKey: string) => apis.filter((a) => a.preset_key === presetKey);
  const providerLabel = (key: string) => providers.find((p) => p.key === key)?.label ?? key;
  // Presets that need a provider this deployment has not configured cannot work.
  const usablePresets = useMemo(
    () => presets.filter((p) => p.provider === null || providers.some((x) => x.key === p.provider)),
    [presets, providers],
  );

  const connect = async (provider: string, presetKey: string | null) => {
    setBusy(`connect:${provider}`);
    setError(null);
    try {
      const { authorize_url } = await authorizeOAuthConnection(tenantId, provider, presetKey);
      const context: OAuthContext = { tenantId, provider, returnTo: "/integrations" };
      window.sessionStorage.setItem(OAUTH_CONTEXT_KEY, JSON.stringify(context));
      window.location.assign(authorize_url);
    } catch (e) {
      setError(errorText(e));
      setBusy(null);
    }
  };

  const submitApiKey = async () => {
    if (!pasting) return;
    setBusy(`connect:${pasting.key}`);
    setKeyError(null);
    try {
      await connectApiKey(tenantId, pasting.key, apiKey.trim());
      setPasting(null);
      setApiKey("");
      await refresh();
    } catch (e) {
      setKeyError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async (connection: OAuthConnection) => {
    if (!window.confirm(`Disconnect ${providerLabel(connection.provider)}? Tools built on it stop working until you reconnect.`)) {
      return;
    }
    setBusy(`disconnect:${connection.id}`);
    setError(null);
    try {
      await disconnectOAuthConnection(tenantId, connection.id);
      setNotice(
        `${providerLabel(connection.provider)} disconnected. To finish, also remove Yuviz from the connected apps in that provider account.`,
      );
      await refresh();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  const openApply = (preset: ConnectorPreset) => {
    setApplying(preset);
    setValues(presetDefaults(preset.key));
    setFormError(null);
  };

  const apply = async () => {
    if (!applying) return;
    setBusy(`apply:${applying.key}`);
    setFormError(null);
    try {
      await applyConnectorPreset(tenantId, setupFor(applying.key, values));
      setApplying(null);
      setNotice(`${applying.title} is set up. Attach its tools to an agent from the agent's Tools tab.`);
      await refresh();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && CONNECTOR_REQUIRED.has(e.detail) && applying.provider) {
        // Authorize with this preset's scopes; the admin applies again on return.
        await connect(applying.provider, applying.key);
        return;
      }
      setFormError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  const remove = async (preset: ConnectorPreset) => {
    if (!window.confirm(`Remove ${preset.title}? Its tools are deleted and agents using them lose them.`)) return;
    setBusy(`remove:${preset.key}`);
    setError(null);
    try {
      await removeConnectorPreset(tenantId, preset.key);
      setNotice(null);
      await refresh();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  if (loading) {
    return (
      <div className="card">
        <div className="empty-state">Loading integrations…</div>
      </div>
    );
  }

  const missingRequired = (PRESET_FIELDS[applying?.key ?? ""] ?? []).some(
    (f) => (!f.when || f.when(values)) && !values[f.name]?.trim(),
  );

  return (
    <>
      {error && <div className="error-banner">{error}</div>}
      {notice && <div className="info-banner">{notice}</div>}

      <div id="connect" className="card" style={{ marginBottom: 14, scrollMarginTop: 16 }}>
        <div className="card-hdr">
          <div className="card-title">Connected accounts</div>
        </div>
        <div className="card-body">
          {providers.length === 0 && (
            <div className="empty-state">
              No account providers are configured on this platform yet. Ask a platform operator to set one up.
            </div>
          )}
          {providers.map((provider) => {
            const connection = connectionFor(provider.key);
            const status = connection ? STATUS_BADGE[connection.status] : null;
            return (
              <div key={provider.key} className="kb-row">
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 500 }}>{provider.label}</div>
                  <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                    {connection ? (connection.account_label ?? "Account connected") : "Not connected"}
                  </div>
                </div>
                {status && <span className={`badge ${status.cls}`}>{status.label}</span>}
                {canManage && (
                  <>
                    <button
                      className="btn btn-indigo btn-sm"
                      disabled={busy !== null}
                      onClick={() => {
                        if (provider.auth_kind !== "api_key") return connect(provider.key, null);
                        setPasting(provider);
                        setApiKey("");
                        setKeyError(null);
                      }}
                    >
                      {connection ? "Reconnect" : "Connect"}
                    </button>
                    {connection && (
                      <button
                        className="btn btn-danger btn-sm"
                        disabled={busy !== null}
                        onClick={() => disconnect(connection)}
                      >
                        Disconnect
                      </button>
                    )}
                  </>
                )}
              </div>
            );
          })}
          {DARK_ENTRIES.filter((entry) => !entry.isLive(providers, presets)).map((entry) => (
            <div key={entry.label} className="kb-row">
              <div style={{ flex: 1, fontWeight: 500 }}>{entry.label}</div>
              <span className="badge gray">Not available</span>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Ready-made tools</div>
        </div>
        <div className="card-body">
          {usablePresets.length === 0 && <div className="empty-state">No ready-made tools are available yet.</div>}
          {usablePresets.map((preset) => {
            const rows = rowsFor(preset.key);
            const connection = preset.provider ? connectionFor(preset.provider) : null;
            const needsReconnect = preset.provider !== null && rows.length > 0 && connection?.status !== "connected";
            return (
              <div key={preset.key} className="kb-row">
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 500 }}>{preset.title}</div>
                  <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                    {rows.length > 0 ? rows.map((r) => r.name).join(", ") : "Not set up"}
                    {preset.provider && ` · uses ${providerLabel(preset.provider)}`}
                  </div>
                </div>
                {needsReconnect && preset.provider && (
                  <span className="badge amber">Disabled: reconnect {providerLabel(preset.provider)}</span>
                )}
                {rows.length > 0 && <span className="badge green">Set up</span>}
                {canManage && (
                  <button
                    className={`btn btn-sm ${rows.length > 0 ? "btn-ghost" : "btn-indigo"}`}
                    disabled={busy !== null}
                    onClick={() => openApply(preset)}
                  >
                    {rows.length > 0 ? "Apply again" : "Apply"}
                  </button>
                )}
                {canManage && rows.length > 0 && (
                  <button className="btn btn-danger btn-sm" disabled={busy !== null} onClick={() => remove(preset)}>
                    Remove
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </div>

      <Modal
        open={pasting !== null}
        title={pasting ? `Connect ${pasting.label}` : ""}
        onClose={() => setPasting(null)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setPasting(null)}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" onClick={submitApiKey} disabled={busy !== null || !apiKey.trim()}>
              {busy?.startsWith("connect:") ? "Connecting…" : "Connect"}
            </button>
          </>
        }
      >
        {keyError && <div className="error-banner">{keyError}</div>}
        <div className="info-banner">
          A {pasting?.label} API key is not limited to one thing: it acts as the whole account, so anyone who can
          use it can read and change everything that account can. Revoke it in {pasting?.label} to cut access.
        </div>
        <div className="form-group">
          <label className="form-label">
            API key<span className="hint"> Stored encrypted; it is never shown again</span>
          </label>
          <input
            className="form-input"
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </div>
      </Modal>

      <Modal
        open={applying !== null}
        title={applying ? `Set up ${applying.title}` : ""}
        onClose={() => setApplying(null)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setApplying(null)}>
              Cancel
            </button>
            <button
              className="btn btn-primary btn-sm"
              onClick={apply}
              disabled={busy !== null || missingRequired}
            >
              {busy?.startsWith("apply:") ? "Applying…" : "Apply"}
            </button>
          </>
        }
      >
        {formError && <div className="error-banner">{formError}</div>}
        {applying && (
          <div className="form-hint" style={{ marginBottom: 10 }}>
            {applying.provider
              ? `Needs a connected ${providerLabel(applying.provider)} account. If it is not connected yet, you will be sent to ${providerLabel(applying.provider)} to approve access, then come back and apply again.`
              : "Needs no connected account."}
          </div>
        )}
        {(PRESET_FIELDS[applying?.key ?? ""] ?? [])
          .filter((f) => !f.when || f.when(values))
          .map((f) => (
            <div key={f.name} className="form-group">
              <label className="form-label">
                {f.label}
                {f.hint && <span className="hint"> {f.hint}</span>}
              </label>
              {f.type === "select" ? (
                <select
                  className="form-select"
                  value={values[f.name] ?? ""}
                  onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
                >
                  {f.options?.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  className="form-input"
                  type={f.type ?? "text"}
                  autoComplete="off"
                  value={values[f.name] ?? ""}
                  onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
                />
              )}
            </div>
          ))}
      </Modal>
    </>
  );
}

const errorText = (e: unknown) => (e instanceof ApiError ? e.detail : String(e));
