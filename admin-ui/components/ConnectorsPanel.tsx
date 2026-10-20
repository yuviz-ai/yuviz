"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Check, Plus, Search } from "lucide-react";
import { ApiError, getCurrentUser, listCarriers, listTelephonyConfigs, listPhoneNumbers, deleteCarrier, deleteTelephonyConfig } from "@/lib/api";
import { CallingAppModal, CALLING_APPS, CallingAccount } from "@/components/CallingAppModal";
import { AppCard, filterApps, workAppCards } from "@/lib/connectedApps";
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
    { name: "api_key", label: "Secret key", type: "password", hint: "Stored encrypted; it is never shown again" },
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

export function ConnectorsPanel({
  tenantId,
  connectOpen = false,
  onConnectClose,
  manageApp,
}: {
  tenantId: string;
  manageApp?: string;
  connectOpen?: boolean;
  onConnectClose?: () => void;
}) {
  const [callingAccounts, setCallingAccounts] = useState<CallingAccount[]>([]);
  const [callingLoaded, setCallingLoaded] = useState({ carrier: false, telephony_config: false });
  const [isSuperadmin, setIsSuperadmin] = useState(false);
  const [query, setQuery] = useState("");
  const [segment, setSegment] = useState("All");
  const [category, setCategory] = useState("All");
  const [managing, setManaging] = useState<string | null>(manageApp ?? null);
  const [callingForm, setCallingForm] = useState<{ provider: string; account?: CallingAccount } | null>(null);
  const [providers, setProviders] = useState<OAuthProvider[]>([]);
  const [connections, setConnections] = useState<OAuthConnection[]>([]);
  const [presets, setPresets] = useState<ConnectorPreset[]>([]);
  const [apis, setApis] = useState<CustomApi[]>([]);
  const [canManage, setCanManage] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showPhoneNumbersLink, setShowPhoneNumbersLink] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const [applying, setApplying] = useState<ConnectorPreset | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);

  const [pasting, setPasting] = useState<OAuthProvider | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [keyError, setKeyError] = useState<string | null>(null);

  // These reads are independent: one failing must not blank what the
  // others returned.
  const refresh = async () => {
    const [p, c, pr, a, carriers, configs] = await Promise.allSettled([
      listOAuthProviders(),
      listOAuthConnections(tenantId),
      listConnectorPresets(),
      listCustomApis(tenantId),
      listCarriers(tenantId),
      listTelephonyConfigs(tenantId),
    ]);
    if (p.status === "fulfilled") setProviders(p.value);
    if (c.status === "fulfilled") setConnections(c.value);
    if (pr.status === "fulfilled") setPresets(pr.value);
    if (a.status === "fulfilled") setApis(a.value);
    setCallingAccounts((previous) => [
      ...(carriers.status === "fulfilled"
        ? carriers.value.map((record): CallingAccount => ({ kind: "carrier", record }))
        : previous.filter((a) => a.kind === "carrier")),
      ...(configs.status === "fulfilled"
        ? configs.value.map((record): CallingAccount => ({ kind: "telephony_config", record }))
        : previous.filter((a) => a.kind === "telephony_config")),
    ]);
    setCallingLoaded((previous) => ({
      carrier: previous.carrier || carriers.status === "fulfilled",
      telephony_config: previous.telephony_config || configs.status === "fulfilled",
    }));
    const failed = [p, c, pr, a, carriers, configs].find((r) => r.status === "rejected");
    setError(failed ? errorText(failed.reason) : null);
    setLoading(false);
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    getCurrentUser()
      .then((u) => { setCanManage(u.role === "superadmin" || u.role === "admin"); setIsSuperadmin(u.role === "superadmin"); })
      .catch(() => setCanManage(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

  const rowsFor = (presetKey: string) => apis.filter((a) => a.preset_key === presetKey);
  const providerLabel = (key: string) => providers.find((p) => p.key === key)?.label ?? key;
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
      setShowPhoneNumbersLink(false);
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
      setShowPhoneNumbersLink(false);
      setValues({});
      setNotice(`${applying.title} is on. Add it to an agent from the agent’s Tools tab.`);
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

  const cards: AppCard[] = [
    ...CALLING_APPS.map((app): AppCard => {
      const count = callingAccounts.filter((a) => a.record.provider === app.key).length;
      return { id: `calling:${app.key}`, label: app.label, description: app.description,
        icon: app.key === "native" ? "local-phone" : app.key, category: "Calling", connected: count > 0,
        reconnect: false, available: callingLoaded[["twilio", "plivo", "vonage"].includes(app.key) ? "carrier" : "telephony_config"], calling: app.key, count };
    }),
    ...workAppCards(providers, connections, presets, apis),
  ];
  const visible = filterApps(cards, query, segment, category);
  const managed = cards.find((app) => app.id === managing);
  const canEdit = (app: AppCard) => canManage && (app.calling !== "native" || isSuperadmin);
  const startConnect = (app: AppCard) => {
    onConnectClose?.();
    setManaging(null);
    if (app.calling) setCallingForm({ provider: app.calling });
    else if (app.preset) openApply(app.preset);
    else if (app.provider?.auth_kind === "api_key") {
      setPasting(app.provider); setApiKey(""); setKeyError(null);
    } else if (app.provider) void connect(app.provider.key, null);
  };
  const disconnectCalling = async (account: CallingAccount) => {
    if (!window.confirm(`Disconnect “${account.record.name}”? You can connect it again later.`)) return;
    setBusy(`disconnect:${account.record.id}`);
    setError(null);
    try {
      const numbers = await listPhoneNumbers(tenantId);
      if (numbers.some((n) => account.kind === "carrier" ? n.carrier_id === account.record.id : n.telephony_config_id === account.record.id)) {
        setError("Move or remove this connection’s numbers on Phone Numbers before disconnecting it.");
        return;
      }
      if (account.kind === "carrier") await deleteCarrier(account.record.id);
      else await deleteTelephonyConfig(account.record.id);
      setShowPhoneNumbersLink(false);
      setNotice(`${account.record.name} disconnected.`);
      await refresh();
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(null); }
  };

  const catalog = <>
    <div className="connected-apps-toolbar">
      <div className="connected-apps-segments" aria-label="Connection status">
        {["All", "Connected", "Not connected"].map((value) => <button key={value} type="button" aria-pressed={segment === value}
          className={segment === value ? "on" : ""} onClick={() => setSegment(value)}>
          {value} · {cards.filter((app) => value === "All" || (value === "Connected" ? app.connected : !app.connected)).length}
        </button>)}
      </div>
      <label className="connected-apps-search"><Search size={16} aria-hidden="true" />
        <input aria-label="Search apps" placeholder="Search apps…" value={query} onChange={(e) => setQuery(e.target.value)} />
      </label>
    </div>
    <div className="connected-apps-categories" aria-label="App category">
      {["All", "Calling", "Work", "Messaging"].map((value) => <button key={value} type="button" aria-pressed={category === value}
        className={category === value ? "on" : ""} onClick={() => setCategory(value)}>{value}</button>)}
    </div>
    <div className="connected-apps-grid">
      {visible.map((app) => <article key={app.id} className={`connected-app-card${app.connected ? " connected" : ""}`}>
        {/* Brand assets are local and retain their original colors. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <div className="connected-app-icon"><img src={`/connected-apps/${app.icon}.svg`} alt="" width={28} height={28} /></div>
        <div><h2>{app.label}</h2><p>{app.description}</p></div>
        <div className="connected-app-actions">
          {app.connected || app.reconnect ? <>
            <button type="button" className={`connected-app-status${app.reconnect ? " reconnect" : ""}`} onClick={() => setManaging(app.id)} aria-label={`Manage ${app.label}`}>
              {app.reconnect ? "Reconnect needed" : <><Check size={14} />{app.count ? `${app.count} connected` : "Connected"}</>}
            </button>
            {canEdit(app) && app.available && app.calling !== "native" && <button type="button" className="connected-app-add" disabled={busy !== null}
              aria-label={app.calling ? `Connect another ${app.label} account` : `Manage ${app.label}`} onClick={() => app.calling ? startConnect(app) : setManaging(app.id)}><Plus size={16} /></button>}
          </> : !app.available ? <span className="connected-app-soon">{app.calling ? "Unavailable" : "Coming soon"}</span>
          : <button type="button" className="connected-app-connect" disabled={!canEdit(app) || busy !== null} onClick={() => startConnect(app)}>
            <Plus size={14} />{app.preset && app.id !== "whatsapp_confirmation" ? "Turn on" : "Connect"}
          </button>}
        </div>
        {app.calling === "native" && !isSuperadmin && !app.connected && <span className="form-hint">Ask a platform admin to connect.</span>}
      </article>)}
    </div>
    {visible.length === 0 && <div className="empty-state">No apps match your search and filters.<button className="btn btn-ghost btn-sm" onClick={() => { setQuery(""); setSegment("All"); setCategory("All"); }}>Clear filters</button></div>}
  </>;

  return (
    <>
      {error && <div className="error-banner" role="alert">{error} <button className="btn btn-ghost btn-sm" disabled={busy !== null} onClick={() => void refresh()}>Retry</button></div>}
      {notice && <div className="info-banner" role="status">{notice} {showPhoneNumbersLink && <Link href="/telephony">Go to Phone Numbers</Link>}</div>}
      {!(connectOpen && canManage) && catalog}
      <Modal open={connectOpen && canManage} title="Connect an app" onClose={() => onConnectClose?.()}>
        <p className="form-hint" style={{ marginBottom: 12 }}>Choose an app to connect your account.</p>
        {catalog}
      </Modal>
      <Modal open={!!managed} title={`Manage ${managed?.label ?? "app"}`} onClose={() => { if (!busy) setManaging(null); }}>
        {error && <div className="error-banner" role="alert">{error}</div>}
        {managed?.calling ? <>
          <p className="form-hint">Manage your connected accounts here. Go to Phone Numbers to buy, add or route numbers.</p>
          {callingAccounts.filter((a) => a.record.provider === managed.calling).map((account) => <div className="kb-row" key={`${account.kind}:${account.record.id}`}>
            <div style={{ flex: 1, minWidth: 0 }}><b>{account.record.name}</b><div className="form-hint">Connected</div></div>
            {canEdit(managed) && <>
              <button className="btn btn-ghost btn-sm" disabled={busy !== null} onClick={() => { setManaging(null); setCallingForm({ provider: managed.calling!, account }); }}>Manage</button>
              <button className="btn btn-danger btn-sm" disabled={busy !== null} onClick={() => disconnectCalling(account)}>Disconnect</button>
            </>}
            <Link className="btn btn-ghost btn-sm" href={`/telephony?config=${account.kind}:${account.record.id}`}>Phone Numbers</Link>
          </div>)}
          {canEdit(managed) && managed.available && managed.calling !== "native" && <button className="btn btn-primary btn-sm" disabled={busy !== null} onClick={() => startConnect(managed)}>+ Connect another account</button>}
        </> : managed && <>
          {managed.provider && <>
            {connections.filter((c) => c.provider === managed.provider?.key && c.status !== "disconnected").map((c) => <div className="kb-row" key={c.id}>
              <div style={{ flex: 1 }}><b>{c.account_label ?? managed.label}</b><div className="form-hint">{c.status === "connected" ? "Connected" : "Reconnect needed"}</div></div>
              {canManage && <button className="btn btn-danger btn-sm" disabled={busy !== null} onClick={() => disconnect(c)}>Disconnect</button>}
            </div>)}
            {canManage && <button className="btn btn-ghost btn-sm" disabled={busy !== null} onClick={() => startConnect(managed)}>Reconnect {managed.label}</button>}
          </>}
          {presets.filter((p) => managed.preset ? p.key === managed.preset.key : p.provider === managed.provider?.key).map((preset) => <div className="kb-row" key={preset.key}>
            <div style={{ flex: 1 }}><b>{preset.title}</b><div className="form-hint">{rowsFor(preset.key).length ? "Turned on" : "Not turned on"}</div></div>
            {canManage && <>
              <button className="btn btn-primary btn-sm" disabled={busy !== null} onClick={() => { setManaging(null); openApply(preset); }}>{rowsFor(preset.key).length ? "Update" : "Turn on"}</button>
              {rowsFor(preset.key).length > 0 && <button className="btn btn-danger btn-sm" disabled={busy !== null} onClick={() => remove(preset)}>Turn off</button>}
            </>}
          </div>)}
        </>}
      </Modal>
      {callingForm && <CallingAppModal key={callingForm.account?.record.id ?? callingForm.provider} tenantId={tenantId} {...callingForm}
        onClose={() => setCallingForm(null)} onSaved={(name) => {
          setShowPhoneNumbersLink(true);
          setNotice(`${CALLING_APPS.find((app) => app.key === callingForm.provider)?.label} is connected as “${name}”. Next, buy or add a number on Phone Numbers.`);
          setCallingForm(null); void refresh();
        }} />}

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
          <label className="form-label" htmlFor="app-secret-key">
            Secret key<span className="hint"> Stored encrypted; it is never shown again</span>
          </label>
          <input
            className="form-input"
            id="app-secret-key"
            type="password"
            autoComplete="new-password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </div>
      </Modal>

      <Modal
        open={applying !== null}
        title={applying ? (applying.key === "whatsapp_confirmation" ? "Connect WhatsApp" : `Turn on ${applying.title}`) : ""}
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
              {busy?.startsWith("apply:") ? "Saving…" : applying?.key === "whatsapp_confirmation" ? "Connect WhatsApp" : "Turn on"}
            </button>
          </>
        }
      >
        {formError && <div className="error-banner">{formError}</div>}
        {applying?.key === "whatsapp_confirmation" && <div className="info-banner">This key can act as the whole WhatsApp account. Revoke it with your messaging provider to cut access.</div>}
        {applying?.provider && (
          <div className="form-hint" style={{ marginBottom: 10 }}>
            {applying.provider
              ? `Needs a connected ${providerLabel(applying.provider)} account. If it is not connected yet, you will be sent to ${providerLabel(applying.provider)} to approve access, then return here to turn it on.`
              : "Enter the details below to connect and turn on messaging."}
          </div>
        )}
        {(PRESET_FIELDS[applying?.key ?? ""] ?? [])
          .filter((f) => !f.when || f.when(values))
          .map((f) => (
            <div key={f.name} className="form-group">
              <label className="form-label" htmlFor={`preset-${f.name}`}>
                {f.label}
                {f.hint && <span className="hint"> {f.hint}</span>}
              </label>
              {f.type === "select" ? (
                <select
                  id={`preset-${f.name}`}
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
                  id={`preset-${f.name}`}
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
