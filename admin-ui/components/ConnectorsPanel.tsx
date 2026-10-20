"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Check, Plus, Search } from "lucide-react";
import { ApiError, getCurrentUser, listCarriers, listTelephonyConfigs, listPhoneNumbers, deleteCarrier, deleteTelephonyConfig } from "@/lib/api";
import { CallingAppModal, CALLING_APPS, CallingAccount } from "@/components/CallingAppModal";
import { AppCard, availabilityLabel, filterApps, oauthAuthorizationUrl, workAppCards } from "@/lib/connectedApps";
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

  const [oauthIntro, setOauthIntro] = useState<{ provider: OAuthProvider; presetKey: string | null; changing: boolean } | null>(null);
  const [whatsAppStep, setWhatsAppStep] = useState<"key" | "messaging">("key");
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

  useEffect(() => {
    if (applying?.key !== "whatsapp_confirmation") return;
    document.getElementById(whatsAppStep === "key" ? "preset-api_key" : "preset-provider")?.focus();
  }, [applying?.key, whatsAppStep]);

  const rowsFor = (presetKey: string) => apis.filter((a) => a.preset_key === presetKey);
  const providerLabel = (key: string) => providers.find((p) => p.key === key)?.label ?? key;
  const connect = async (provider: string, presetKey: string | null, chooseAccount = false) => {
    setBusy(`connect:${provider}`);
    setError(null);
    try {
      const { authorize_url } = await authorizeOAuthConnection(tenantId, provider, presetKey);
      const context: OAuthContext = { tenantId, provider, returnTo: "/integrations" };
      window.sessionStorage.setItem(OAUTH_CONTEXT_KEY, JSON.stringify(context));
      window.location.assign(oauthAuthorizationUrl(authorize_url, provider, chooseAccount));
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
    setWhatsAppStep("key");
  };

  const apply = async () => {
    if (!applying) return;
    setBusy(`apply:${applying.key}`);
    setFormError(null);
    try {
      const setup = setupFor(applying.key, values);
      await applyConnectorPreset(tenantId, setup);
      setApplying(null);
      setShowPhoneNumbersLink(false);
      setValues({});
      setNotice(`${applying.title} is on. Add it to an agent from the agent’s Tools tab.`);
      await refresh();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && CONNECTOR_REQUIRED.has(e.detail) && applying.provider) {
        // Authorize with this preset's scopes; the admin applies again on return.
        const provider = providers.find((p) => p.key === applying.provider);
        if (provider) { setApplying(null); setValues({}); setOauthIntro({ provider, presetKey: applying.key, changing: false }); }
        else setFormError("This app needs setup by a platform admin before you can connect.");
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
  const choosingApp = connectOpen && canManage && !oauthIntro && !applying && !pasting && !callingForm;
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
    } else if (app.provider) setOauthIntro({ provider: app.provider, presetKey: null, changing: app.id === "google" && !!app.accounts?.length });
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

  const searchInput = <label className="connected-apps-search"><Search size={16} aria-hidden="true" />
    <input type="search" aria-label="Search apps" placeholder="Search apps…" value={query} onChange={(e) => setQuery(e.target.value)} />
  </label>;
  const catalog = <>
    <div className="connected-apps-heading">
      <div><h1>Connected Apps</h1><p>Link the phone lines and work tools your agents use.</p></div>
      {searchInput}
    </div>
    <div className="connected-apps-toolbar">
      <div className="connected-apps-segments" aria-label="Connection status">
        {["All", "Connected", "Not connected"].map((value) => <button key={value} type="button" aria-pressed={segment === value}
          className={segment === value ? "on" : ""} onClick={() => setSegment(value)}>
          {value} · {cards.filter((app) => value === "All" || (value === "Connected" ? app.connected : !app.connected)).length}
        </button>)}
      </div>
      <div className="connected-apps-categories" aria-label="App category">
        {["All", "Calling", "Work", "Messaging"].map((value) => <button key={value} type="button" aria-pressed={category === value}
          className={category === value ? "on" : ""} onClick={() => setCategory(value)}>{value}</button>)}
      </div>
    </div>
    <div className="connected-apps-grid">
      {visible.map((app) => <article key={app.id} className={`connected-app-card${app.connected ? " connected" : ""}`}>
        {/* Brand assets are local and retain their original colors. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <div className="connected-app-icon"><img src={`/connected-apps/${app.icon}.svg`} alt="" width={28} height={28} /></div>
        <div className="connected-app-copy"><h2>{app.label}</h2><p>{app.description}</p></div>
        {!!app.accounts?.length && <div className="connected-app-account" title={app.accounts.map((a) => a.account_label ?? "Account connected").join(", ")}>
          {app.accounts.length === 1 ? app.accounts[0].account_label ?? "Account connected" : `${app.accounts[0].account_label ?? "Account connected"} +${app.accounts.length - 1} more`}
        </div>}
        {!app.available && !app.comingSoon && !app.connected && <span className="connected-app-setup-hint">{app.calling ? "Try refreshing to load this app." : "A platform admin needs to enable this app."}</span>}
        {app.calling === "native" && !isSuperadmin && !app.connected && <span className="form-hint">Ask a platform admin to connect.</span>}
        <div className="connected-app-actions">
          {app.connected || app.reconnect ? <>
            <button type="button" className={`connected-app-status${app.reconnect ? " reconnect" : ""}`} onClick={() => setManaging(app.id)} aria-label={`Manage ${app.label}`}>
              {app.reconnect ? "Reconnect needed" : <><Check size={14} />{app.count && (app.calling || app.count > 1) ? `${app.count} connected` : "Connected"}</>}
            </button>
            {canEdit(app) && app.available && app.calling !== "native" && <button type="button" className="connected-app-add" disabled={busy !== null} title={app.id === "google" ? "Change Google account" : app.calling ? `Connect another ${app.label} account` : `Manage ${app.label}`}
              aria-label={app.calling || app.id === "google" ? (app.id === "google" ? "Change Google account" : `Connect another ${app.label} account`) : `Manage ${app.label}`} onClick={() => app.calling || app.id === "google" ? startConnect(app) : setManaging(app.id)}><Plus size={16} /></button>}
          </> : !app.available ? <span className="connected-app-soon">{availabilityLabel(app)}</span>
          : <button type="button" className="connected-app-connect" disabled={!canEdit(app) || busy !== null} onClick={() => startConnect(app)}>
            <Plus size={14} />{app.preset && app.id !== "whatsapp_confirmation" ? "Turn on" : "Connect"}
          </button>}
        </div>
      </article>)}
    </div>
    {visible.length === 0 && <div className="empty-state">No apps match your search and filters.<button className="btn btn-ghost btn-sm" onClick={() => { setQuery(""); setSegment("All"); setCategory("All"); }}>Clear filters</button></div>}
  </>;

  return (
    <>
      {error && <div className="error-banner" role="alert">{error} <button className="btn btn-ghost btn-sm" disabled={busy !== null} onClick={() => void refresh()}>Retry</button></div>}
      {notice && <div className="info-banner" role="status">{notice} {showPhoneNumbersLink && <Link href="/telephony">Go to Phone Numbers</Link>}</div>}
      {!choosingApp && catalog}
      <Modal className="connected-app-dialog" open={choosingApp} title="Connect an app" onClose={() => onConnectClose?.()}>
        <p className="form-hint" style={{ marginBottom: 12 }}>Choose an app to connect your account.</p>
        {catalog}
      </Modal>
      <Modal className="connected-app-dialog" open={!!managed} title={`Manage ${managed?.label ?? "app"}`} onClose={() => { if (!busy) setManaging(null); }}>
        {error && <div className="error-banner" role="alert">{error}</div>}
        {managed?.calling ? <>
          <p className="form-hint">Manage your connected accounts here. Go to Phone Numbers to buy, add or route numbers.</p>
          {callingAccounts.filter((a) => a.record.provider === managed.calling).map((account) => <div className="kb-row connected-app-manage-row" key={`${account.kind}:${account.record.id}`}>
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
            {connections.filter((c) => c.provider === managed.provider?.key && c.status !== "disconnected").map((c) => <div className="kb-row connected-app-manage-row" key={c.id}>
              <div className="connected-app-manage-identity" style={{ flex: 1 }}><b>{c.account_label ?? managed.label}</b><div className="form-hint">{c.status === "connected" ? "Connected" : "Reconnect needed"}</div></div>
              {canManage && managed.available && <button className="btn btn-ghost btn-sm" disabled={busy !== null} onClick={() => startConnect(managed)}>Reconnect</button>}
              {canManage && <button className="btn btn-danger btn-sm" disabled={busy !== null} onClick={() => disconnect(c)}>Disconnect</button>}
            </div>)}
            {canManage && managed.available && <button className="btn btn-ghost btn-sm" disabled={busy !== null} onClick={() => startConnect(managed)}>{managed.id === "google" ? "Change Google account" : `Reconnect ${managed.label}`}</button>}
          </>}
          {presets.filter((p) => managed.preset ? p.key === managed.preset.key : p.provider === managed.provider?.key).map((preset) => <div className="kb-row connected-app-manage-row" key={preset.key}>
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
        className="connected-app-dialog"
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

      <Modal className="connected-app-dialog connected-app-oauth-dialog" open={oauthIntro !== null}
        title={oauthIntro?.changing ? `Change ${oauthIntro.provider.label} account` : `Connect ${oauthIntro?.provider.label ?? "app"}`}
        onClose={() => { if (!busy) setOauthIntro(null); }}>
        {error && <div className="error-banner" role="alert">{error}</div>}
        <div className="connected-app-oauth">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={`/connected-apps/${oauthIntro?.provider.key ?? "google"}.svg`} alt="" width={44} height={44} />
          <h2>Continue with {oauthIntro?.provider.label}</h2>
          <p>{oauthIntro?.provider.key === "google" ? "Yuviz wants to use Google Calendar and Sheets for this account." : `Connect your ${oauthIntro?.provider.label} account to use it with your agents.`} You’ll approve access on {oauthIntro?.provider.label}, then return here.</p>
          {oauthIntro?.changing && <p>One Google account can be connected at a time. Choose the same account to reconnect, or choose another to replace it. Calendar and Sheets tools will use the account you choose.</p>}
          <button className="btn btn-primary" disabled={busy !== null} onClick={() => { if (oauthIntro) void connect(oauthIntro.provider.key, oauthIntro.presetKey, oauthIntro.changing); }}>
            {busy?.startsWith("connect:") ? "Opening…" : `Continue with ${oauthIntro?.provider.label}`}
          </button>
          <span className="form-hint">Opens in this window · cancel anytime</span>
        </div>
      </Modal>

      <Modal
        className="connected-app-dialog"
        open={applying !== null}
        title={applying ? (applying.key === "whatsapp_confirmation" ? (whatsAppStep === "key" ? "Connect WhatsApp" : "Turn on WhatsApp messaging") : `Turn on ${applying.title}`) : ""}
        onClose={() => { if (!busy) { setApplying(null); setValues({}); } }}
        footer={
          <>
            {applying?.key === "whatsapp_confirmation" && whatsAppStep === "messaging" && <button className="btn btn-ghost btn-sm" disabled={busy !== null} onClick={() => setWhatsAppStep("key")}>Back</button>}
            <button className="btn btn-ghost btn-sm" disabled={busy !== null} onClick={() => { setApplying(null); setValues({}); }}>Cancel</button>
            <button
              className="btn btn-primary btn-sm"
              onClick={() => applying?.key === "whatsapp_confirmation" && whatsAppStep === "key" ? setWhatsAppStep("messaging") : void apply()}
              disabled={busy !== null || (applying?.key === "whatsapp_confirmation" && whatsAppStep === "key" ? !values.api_key?.trim() : missingRequired)}
            >
              {busy?.startsWith("apply:") ? "Saving…" : applying?.key === "whatsapp_confirmation" ? (whatsAppStep === "key" ? "Continue" : "Turn on messaging") : "Turn on"}
            </button>
          </>
        }
      >
        {formError && <div className="error-banner">{formError}</div>}
        {applying?.key === "whatsapp_confirmation" && <>
          <ol className="connected-app-steps" aria-label="WhatsApp setup progress">
            <li aria-current={whatsAppStep === "key" ? "step" : undefined}>1 · Enter secret key</li>
            <li aria-current={whatsAppStep === "messaging" ? "step" : undefined}>2 · Turn on messaging</li>
          </ol>
          {whatsAppStep === "key" ? <div className="info-banner">This key can act as the whole WhatsApp account. Revoke it with your messaging provider to cut access.</div>
            : <div className="info-banner">Key entered. Choose your messaging service and approved template below. Your key and settings are saved when you turn on messaging.</div>}
        </>}
        {applying?.provider && (
          <div className="form-hint" style={{ marginBottom: 10 }}>
            {applying.provider
              ? `Needs a connected ${providerLabel(applying.provider)} account. If it is not connected yet, you will be sent to ${providerLabel(applying.provider)} to approve access, then return here to turn it on.`
              : "Enter the details below to connect and turn on messaging."}
          </div>
        )}
        {applying?.provider === "google" && <p className="connected-app-selected-account">
          Google account: <strong>{connections.find((c) => c.provider === "google" && c.status === "connected")?.account_label ?? "Connect Google to choose an account"}</strong>
        </p>}
        {(PRESET_FIELDS[applying?.key ?? ""] ?? [])
          .filter((f) => (!f.when || f.when(values)) && (applying?.key !== "whatsapp_confirmation" || (whatsAppStep === "key" ? f.name === "api_key" : f.name !== "api_key")))
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
