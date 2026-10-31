"use client";

import { useContext, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Phone, Search } from "lucide-react";
import {
  Agent, ApiError, AvailableNumber, Call, PhoneNumber, PurchasedNumber, ProviderSync,
  createPhoneNumber, deletePhoneNumber, listAgents, listCalls, listCarriers,
  listPhoneNumbers, listPurchasedNumbers, listTelephonyConfigs, purchaseNumber,
  releaseNumber, searchAvailableNumbers, syncPhoneNumber, syncTelephonyNumbers, updatePhoneNumber,
} from "@/lib/api";
import { COUNTRY_DIAL_CODES } from "@/lib/countries";
import { Modal } from "@/components/Modal";
import { CALLING_APPS, callingAppLabel } from "@/lib/callingApps";
import { useConnectionAccess } from "@/lib/useConnectionAccess";
import { PhoneNumberActionContext } from "@/lib/phoneNumberAction";
import { useActiveTenant } from "@/lib/useActiveTenant";

const NATIVE = "native";
const CONNECTED_CALLING_APPS_URL = "/integrations?category=calling";
const CALL_WINDOW = 200;
const regionNames = new Intl.DisplayNames(["en"], { type: "region" });
const COUNTRIES = COUNTRY_DIAL_CODES.map(([code]) => ({ code, name: regionNames.of(code) ?? code }))
  .sort((a, b) => a.name.localeCompare(b.name, "en"));
const MANUAL_SETUP_PROVIDERS = new Set(["cloudonix"]);
const digits = (s: string | null | undefined) => (s ?? "").replace(/\D/g, "");
const fmtInt = (n: number) => n.toLocaleString("en-IN");
const errorText = (e: unknown) => e instanceof ApiError ? e.detail : String(e);

type ConfigKind = "carrier" | "telephony_config";
interface ConfigRow {
  kind: ConfigKind;
  id: string;
  key: string;
  name: string;
  provider: string;
  providerLabel: string;
  tenantId: string;
  tenantName: string;
}
interface NumberRow extends PhoneNumber {
  tenantName: string;
  configKey: string | null;
  configName: string | null;
  providerLabel: string;
  answers: string | null;
  /** Agents that show this number as their outbound caller ID. */
  callerIdFor: string[];
  recentCalls: number | null;
}
type Notice = { ok: boolean; text: string };
type NumberMode = "manual" | "buy";

function CallingAppIdentity({ config }: { config: ConfigRow | null }) {
  const app = CALLING_APPS.find((a) => a.key === (config?.provider ?? NATIVE));
  return (
    <span className="phone-number-app">
      {app && <span className="phone-number-app-icon">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={`/connected-apps/${app.icon}.svg`} alt="" width={14} height={14} />
      </span>}
      <span>{config?.providerLabel ?? "Native"}{config && config.name !== config.providerLabel && <span className="phone-number-muted"> · {config.name}</span>}</span>
    </span>
  );
}

export default function TelephonyPage() {
  const { tenant, allTenants, isAllTenants, loading: tenantLoading, error: tenantError } = useActiveTenant();
  const access = useConnectionAccess();
  const setTopbarActionEnabled = useContext(PhoneNumberActionContext);
  const searchParams = useSearchParams();
  const selectedKey = searchParams.get("config");
  const mode: NumberMode | null = searchParams.get("buy") === "1" ? "buy" : searchParams.get("add") === "1" ? "manual" : null;
  const [configs, setConfigs] = useState<ConfigRow[]>([]);
  const [numbers, setNumbers] = useState<NumberRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const [editId, setEditId] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [resyncing, setResyncing] = useState(false);
  const refresh = () => setReloadKey((k) => k + 1);

  useEffect(() => {
    if (tenantLoading) return;
    const targets = isAllTenants ? allTenants : tenant ? [tenant] : [];
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    let cancelled = false;
    (async () => {
      const results = await Promise.allSettled(targets.map(async (t) => {
        const [carriers, connections, phoneNumbers, agents, calls] = await Promise.all([
          listCarriers(t.id), listTelephonyConfigs(t.id), listPhoneNumbers(t.id),
          listAgents(t.slug).then((agents) => ({ agents, ok: true })).catch(() => ({ agents: [] as Agent[], ok: false })),
          listCalls(t.slug, { limit: CALL_WINDOW }).then((r): Call[] | null => r.items).catch(() => null),
        ]);
        return { t, carriers, connections, phoneNumbers, agents, calls };
      }));
      if (cancelled) return;
      const configRows: ConfigRow[] = [];
      const numberRows: NumberRow[] = [];
      const errors: string[] = [];
      results.forEach((result, i) => {
        if (result.status !== "fulfilled") {
          errors.push(`${targets[i].name}: ${errorText(result.reason)}`);
          return;
        }
        const { t, carriers, connections, phoneNumbers, agents, calls } = result.value;
        const tenantConfigs: ConfigRow[] = [
          ...carriers.map((c) => ({ kind: "carrier" as const, id: c.id, name: c.name, provider: c.provider })),
          ...connections.map((c) => ({ kind: "telephony_config" as const, id: c.id, name: c.name, provider: c.provider })),
        ].map((c) => ({ ...c, key: `${c.kind}:${c.id}`, providerLabel: callingAppLabel(c.provider), tenantId: t.id, tenantName: t.name }));
        configRows.push(...tenantConfigs);
        const activity = new Map<string, number>();
        for (const call of calls ?? []) {
          for (const key of new Set([digits(call.called_number), digits(call.caller_number)])) {
            if (key) activity.set(key, (activity.get(key) ?? 0) + 1);
          }
        }
        numberRows.push(...phoneNumbers.map((n): NumberRow => {
          const config = tenantConfigs.find((c) => c.kind === "carrier" ? c.id === n.carrier_id : c.id === n.telephony_config_id);
          const agent = agents.agents.find((a) => a.id === n.agent_id);
          return {
            ...n, tenantName: t.name, configKey: config?.key ?? null, configName: config?.name ?? null,
            providerLabel: config?.providerLabel ?? "Native",
            answers: !agents.ok ? "Unavailable" : agent?.name ?? (n.agent_id ? "Unavailable agent" : null),
            callerIdFor: agents.agents
              .filter((a) => a.outbound_caller_id && digits(a.outbound_caller_id) === digits(n.did))
              .map((a) => a.name),
            recentCalls: calls === null ? null : activity.get(digits(n.did)) ?? 0,
          };
        }));
      });
      setConfigs(configRows);
      setNumbers(numberRows);
      setError(errors.length ? errors.join("; ") : null);
      setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [tenant, allTenants, isAllTenants, tenantLoading, reloadKey]);

  const selectedConfig = configs.find((c) => c.key === selectedKey);
  const availableConfigs = configs.filter((c) => access.isSuperadmin || !(c.kind === "telephony_config" && c.provider === NATIVE));
  // A superadmin turns buying on per account; superadmins can always buy.
  const buyingAllowed = access.isSuperadmin || !!tenant?.number_purchase_enabled;
  const canBuy = buyingAllowed && availableConfigs.some((c) => c.kind === "carrier");
  const visibleNumbers = useMemo(() => {
    const q = search.trim().toLowerCase();
    const qDigits = digits(q);
    return numbers.filter((n) => (!selectedKey || n.configKey === selectedKey) && (!q ||
      `${n.did} ${n.region ?? ""} ${n.configName ?? ""} ${n.providerLabel} ${n.answers ?? "not set"} ${n.callerIdFor.join(" ")} ${n.tenantName} ${n.status === "active" ? "on" : "off"}`.toLowerCase().includes(q) ||
      (qDigits.length > 0 && digits(n.did).includes(qDigits))))
      .sort((a, b) => a.tenantName.localeCompare(b.tenantName) || a.did.localeCompare(b.did));
  }, [numbers, search, selectedKey]);
  const editNumber = numbers.find((n) => n.id === editId) ?? null;
  const configFor = (n: NumberRow) => configs.find((c) => c.key === n.configKey) ?? null;
  const busy = loading || tenantLoading;
  const canAddNumber = !busy && access.canManage && availableConfigs.length > 0;
  useEffect(() => {
    setTopbarActionEnabled?.(canAddNumber);
    return () => setTopbarActionEnabled?.(false);
  }, [canAddNumber, setTopbarActionEnabled]);
  const listUrl = selectedKey ? `/telephony?config=${encodeURIComponent(selectedKey)}` : "/telephony";
  const canResync = access.canManage && !!selectedConfig && autoSyncs(selectedConfig) && visibleNumbers.length > 0;
  const resync = async (config: ConfigRow) => {
    setResyncing(true);
    setNotice(null);
    try {
      const { results, application } = await syncTelephonyNumbers(config.id);
      const failed = results.filter((r) => !r.ok);
      setNotice(application && !application.ok
        ? { ok: false, text: `${config.providerLabel} kept the old address for every number here: ${application.message ?? "unknown error"}` }
        : failed.length === 0
          ? { ok: true, text: `${fmtInt(results.length)} number${results.length === 1 ? "" : "s"} re-synced with ${config.providerLabel}.` }
          : { ok: false, text: failed.map((r) => `${r.did}: ${r.message ?? "unknown error"}`).join(" ") });
      refresh();
    } catch (e) {
      setNotice({ ok: false, text: errorText(e) });
    } finally {
      setResyncing(false);
    }
  };
  const openAdd = (mode: NumberMode) => window.history.pushState(null, "", `${listUrl}${selectedKey ? "&" : "?"}${mode === "buy" ? "buy" : "add"}=1`);

  return (
    <>
      <div className="phone-numbers-heading">
        <div>
          <h1>Phone Numbers</h1>
          <p>Numbers your agents can answer. Need Twilio or another line? Connect it on <Link href={CONNECTED_CALLING_APPS_URL}>Connected Apps</Link>.</p>
        </div>
      </div>
      {(error || tenantError) && <div className="error-banner" role="alert">{error || tenantError} <button className="btn btn-ghost btn-sm" onClick={refresh}>Retry</button></div>}
      {access.error && <div className="error-banner" role="alert">{access.error} <button className="btn btn-ghost btn-sm" disabled={access.loading} onClick={() => void access.retry()}>Retry permissions</button></div>}
      {notice && <div className={notice.ok ? "info-banner" : "error-banner"} role="status">{notice.text}</div>}

      {busy ? <div className="card"><div className="empty-state" role="status">Loading phone numbers…</div></div> : (
        <>
          {configs.length > 0 && <div className="info-banner phone-numbers-connection-note">
            <span>{configs.length === 1 ? `${configs[0].providerLabel} is connected as “${configs[0].name}”.` : `${fmtInt(configs.length)} calling apps connected${isAllTenants ? " across your accounts" : ""}.`}</span>
            <Link href={CONNECTED_CALLING_APPS_URL}>Manage on Connected Apps</Link>
          </div>}
          <div className="card phone-numbers-card">
            {configs.length === 0 && numbers.length === 0 && !error && !tenantError ? (
              <div className="empty-state phone-numbers-empty">
                <Phone size={24} aria-hidden="true" />
                <h2>Connect a calling app first</h2>
                <p>Link Twilio, Plivo, Vobiz or Native on Connected Apps. Then come back here to buy or add a number.</p>
                <Link className="btn btn-primary btn-sm" href={CONNECTED_CALLING_APPS_URL}>Go to Connected Apps</Link>
              </div>
            ) : (
              <>
                <div className="card-hdr">
                  <h2 className="card-title">Your numbers</h2>
                  <span className="card-sub">{search || selectedKey ? `${fmtInt(visibleNumbers.length)} of ${fmtInt(numbers.length)}` : `${fmtInt(numbers.length)} number${numbers.length === 1 ? "" : "s"}`}</span>
                  <label className="phone-numbers-search"><Search size={14} aria-hidden="true" /><input type="search" aria-label="Search numbers or agents" placeholder="Search numbers or agents" value={search} onChange={(e) => setSearch(e.target.value)} /></label>
                </div>
                {selectedKey && <div className="phone-numbers-filter">
                  <span>{selectedConfig ? `Through ${selectedConfig.providerLabel} · ${selectedConfig.name}` : "This calling app is unavailable in the selected account."}</span>
                  <span className="phone-numbers-actions">
                    {canResync && <button className="btn btn-ghost btn-sm" disabled={resyncing} title={`Point every number here at this platform in ${selectedConfig.providerLabel} again, e.g. after the public URL changed`} onClick={() => void resync(selectedConfig)}>{resyncing ? "Re-syncing…" : "Re-sync numbers"}</button>}
                    <Link href="/telephony" className="btn btn-ghost btn-sm">Show all numbers</Link>
                  </span>
                </div>}
                {visibleNumbers.length === 0 ? <div className="empty-state phone-numbers-empty">
                  <Phone size={24} aria-hidden="true" />
                  <h2>{search || selectedKey ? "No matching numbers" : "Your first number starts here"}</h2>
                  <p>{search ? `No numbers match “${search}”. Try another number or agent.` : selectedKey ? "This calling app has no numbers to show." : "Buy a new number or add one you already have, then choose which agent answers."}</p>
                  {search ? <button className="btn btn-ghost btn-sm" onClick={() => setSearch("")}>Clear search</button> : access.canManage && availableConfigs.length > 0 ? <button className="btn btn-primary btn-sm" onClick={() => openAdd(canBuy ? "buy" : "manual")}>{canBuy ? "Buy number" : "Add number"}</button> : configs.length > 0 ? <p>{access.canManage ? "Local numbers are added by the platform. You can choose who answers once a number is assigned." : "Ask an account admin to add a number."}</p> : null}
                </div> : <div className="phone-numbers-table-wrap">
                  <table className="tbl">
                    <thead><tr>
                      <th scope="col">Number</th>{isAllTenants && <th scope="col">Account</th>}
                      <th scope="col">Through</th><th scope="col">Status</th>
                      <th scope="col" className="phone-number-calls" title={`Based on the latest ${CALL_WINDOW} calls per account`}>Recent calls</th><th scope="col" aria-label="Actions" />
                    </tr></thead>
                    <tbody>{visibleNumbers.map((n) => <tr key={n.id}>
                      <td><span className="phone-number-address">{n.did}</span><div className="phone-number-sublabel">{n.region || "No label yet"}</div></td>
                      {isAllTenants && <td>{n.tenantName}</td>}
                      <td><CallingAppIdentity config={configFor(n)} /></td>
                      <td><span className={`badge ${n.status === "active" ? "green" : "gray"}`} title={n.status === "suspended" ? "Suspended — this number cannot take calls" : undefined}>{n.status === "active" ? "On" : "Off"}</span>{routingIssue(configFor(n)) && <div className="phone-number-sublabel"><span className="badge amber" title="Open Edit to see how calls reach this number">{routingIssue(configFor(n))}</span></div>}</td>
                      <td className="phone-number-calls" title={`Based on the latest ${CALL_WINDOW} calls per account`}>{n.recentCalls === null ? "—" : fmtInt(n.recentCalls)}</td>
                      <td className="phone-number-calls"><button className="btn btn-ghost btn-sm" disabled={!access.canManage} aria-label={`Edit ${n.did}`} onClick={() => setEditId(n.id)}>Edit</button></td>
                    </tr>)}</tbody>
                  </table>
                </div>}
              </>
            )}
          </div>
        </>
      )}
      {!busy && !editNumber && mode && access.canManage && availableConfigs.length > 0 && <AddNumberModal
        key={`${tenant?.id ?? "all"}:${mode}:${selectedKey ?? ""}`}
        initialMode={canBuy ? mode : "manual"} buyingAllowed={buyingAllowed} configs={availableConfigs} initialConfigKey={selectedKey}
        showTenant={isAllTenants}
        onClose={() => window.history.replaceState(null, "", listUrl)}
        onCreated={(created, config) => {
          window.history.replaceState(null, "", "/telephony");
          setNotice(created.provider_sync?.ok === false
            ? { ok: false, text: `${created.did} was added, but ${config.providerLabel} couldn't route its calls to us yet. Open Edit to see why and retry.` }
            : { ok: true, text: `${created.did} was added. Attach it to an agent from that agent's Settings.` });
          if (created.provider_sync?.ok === false) setEditId(created.id);
          refresh();
        }}
      />}
      {!busy && editNumber && <EditNumberModal
        key={editNumber.id} target={editNumber} config={configFor(editNumber)}
        isSuperadmin={access.isSuperadmin} onClose={() => setEditId(null)}
        onChanged={refresh} onSaved={(text) => setNotice({ ok: true, text })}
      />}
    </>
  );
}

function webhookUrlFor(config: ConfigRow): string | null {
  if (config.kind !== "telephony_config") return null;
  if (config.provider === "cloudonix") return `${process.env.NEXT_PUBLIC_CLOUDONIX_SERVICE_URL || ""}/cloudonix/voice/${config.id}`;
  return null;
}

// REST calling apps whose inbound routing the platform sets up itself.
const autoSyncs = (config: ConfigRow | null) =>
  config?.kind === "telephony_config" && config.provider !== NATIVE && !MANUAL_SETUP_PROVIDERS.has(config.provider);

function routingIssue(config: ConfigRow | null): string | null {
  return config?.kind === "telephony_config" && MANUAL_SETUP_PROVIDERS.has(config.provider) ? "Manual setup" : null;
}

function AddNumberModal({ initialMode, buyingAllowed, configs, initialConfigKey, showTenant, onClose, onCreated }: {
  initialMode: NumberMode; buyingAllowed: boolean; configs: ConfigRow[]; initialConfigKey: string | null;
  showTenant: boolean; onClose: () => void;
  onCreated: (created: PhoneNumber, config: ConfigRow) => void;
}) {
  const [mode, setMode] = useState(initialMode);
  const [configKey, setConfigKey] = useState(initialConfigKey ?? "");
  const [locked, setLocked] = useState(false);
  const [pending, setPending] = useState(false);
  const choices = configs.filter((c) => mode !== "buy" || c.kind === "carrier");
  const config = choices.find((c) => c.key === configKey) ?? choices[0];
  return <Modal open title={mode === "buy" ? "Buy a number" : "Add a number"} className="phone-number-dialog" trapFocus onClose={() => { if (!pending) onClose(); }}>
    <div className="phone-number-tabs" role="group" aria-label="How to add a number">
      <button className={`btn btn-sm ${mode === "buy" ? "btn-primary" : "btn-ghost"}`} aria-pressed={mode === "buy"} disabled={locked || !buyingAllowed || !configs.some((c) => c.kind === "carrier")} title={!buyingAllowed ? "Buying numbers isn't turned on for this account. Ask your platform admin." : !configs.some((c) => c.kind === "carrier") ? "Connect Twilio or Plivo on Connected Apps to buy numbers." : undefined} onClick={() => setMode("buy")}>Buy a new one</button>
      <button className={`btn btn-sm ${mode === "manual" ? "btn-primary" : "btn-ghost"}`} aria-pressed={mode === "manual"} disabled={locked} onClick={() => setMode("manual")}>I already have one</button>
    </div>
    {config ? <>
      <div className="form-group">
        <label className="form-label" htmlFor="number-connection">{mode === "buy" ? "Buy through" : "Through"}</label>
        <select id="number-connection" className="form-select" value={config.key} disabled={locked} onChange={(e) => setConfigKey(e.target.value)}>
          {choices.map((c) => <option key={c.key} value={c.key}>{c.providerLabel} · {c.name}{showTenant ? ` · ${c.tenantName}` : ""}</option>)}
        </select>
      </div>
      <NumberForm key={`${config.key}:${mode}`} mode={mode} config={config} onClose={onClose} onCreated={(n) => onCreated(n, config)} onLock={(locked, pending) => { setLocked(locked); setPending(pending); }} />
    </> : <div className="empty-state phone-numbers-empty"><h2>{mode === "buy" ? "Connect a calling app to buy numbers" : "Connect a calling app first"}</h2><p>{mode === "buy" ? "Connect Twilio, Plivo, or Vonage to buy a number here. You can add an existing number through your other connected apps." : "Connect an app before adding a number."}</p><Link className="btn btn-primary btn-sm" href={CONNECTED_CALLING_APPS_URL}>Go to Connected Apps</Link></div>}
  </Modal>;
}

function NumberForm({ mode, config, onClose, onCreated, onLock }: {
  mode: NumberMode; config: ConfigRow;
  onClose: () => void; onCreated: (created: PhoneNumber) => void; onLock: (locked: boolean, pending: boolean) => void;
}) {
  const [did, setDid] = useState("");
  const [region, setRegion] = useState("");
  const [country, setCountry] = useState("US");
  const [areaCode, setAreaCode] = useState("");
  const [results, setResults] = useState<AvailableNumber[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [buying, setBuying] = useState<string | null>(null);
  const [bought, setBought] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<string | null>(null);
  const connection = config.kind === "carrier" ? { carrier_id: config.id } : { telephony_config_id: config.id };
  const lock = (value: boolean) => { setBusy(value); onLock(value, value); };

  const add = async (phone: string) => createPhoneNumber(config.tenantId, {
    ...connection, did: phone.trim(), region: region.trim() || undefined, status: "active",
  });
  const handleAdd = async () => {
    lock(true); setError(null);
    try { onCreated(await add(did)); }
    catch (e) { setError(errorText(e)); }
    finally { lock(false); }
  };
  const handleSearch = async () => {
    lock(true); setError(null); setResults(null); setConfirmation(null);
    try { setResults(await searchAvailableNumbers(config.tenantId, { carrier_id: config.id, country, area_code: areaCode.trim() || undefined, limit: 10 })); }
    catch (e) { setError(errorText(e)); }
    finally { lock(false); }
  };
  const handleBuy = async (phone: string) => {
    lock(true); setBuying(phone); setError(null); setConfirmation(null);
    let purchased = bought;
    try {
      if (!purchased) {
        const result = await purchaseNumber(config.tenantId, { carrier_id: config.id, phone_number: phone });
        purchased = result.phone_number;
        setBought(purchased);
      }
      onCreated(await add(purchased));
    } catch (e) { setError(purchased ? `${purchased} is already bought, but could not be added to your list. Finish adding it below; you won't be charged again. ${errorText(e)}` : errorText(e)); }
    finally { setBusy(false); setBuying(null); onLock(purchased !== null, false); }
  };

  return <>
    {error && <div className="error-banner" role="alert">{error}</div>}
    {mode === "manual" ? <>
      <div className="form-group">
        <label className="form-label" htmlFor="number-address">Phone number <span className="required">*</span></label>
        <input id="number-address" className="form-input phone-number-address" required value={did} disabled={busy} onChange={(e) => setDid(e.target.value)} placeholder={config.provider === NATIVE ? "5001" : "+14155550199"} aria-describedby="number-address-hint" />
        <p id="number-address-hint" className="form-hint">{config.provider === NATIVE ? "Enter a local extension or phone address assigned by the platform." : "Include the country code, like +1… You can also use a phone extension or SIP address."}</p>
      </div>
      <div className="form-group">
        <label className="form-label" htmlFor="number-label">Label <span className="hint">optional</span></label>
        <input id="number-label" className="form-input" value={region} disabled={busy} onChange={(e) => setRegion(e.target.value)} placeholder="e.g. West coast sales" />
      </div>
      <div className="phone-number-form-actions"><button className="btn btn-ghost btn-sm" disabled={busy} onClick={onClose}>Cancel</button><button className="btn btn-primary btn-sm" disabled={busy || !did.trim()} onClick={handleAdd}>{busy ? "Adding…" : "Add number"}</button></div>
    </> : <>
      <p className="phone-number-buy-hint">This uses your {config.providerLabel} account and may charge that account.</p>
      <div className="phone-number-country-row">
        <div className="form-group"><label className="form-label" htmlFor="number-country">Country</label><select id="number-country" className="form-select" value={country} disabled={busy || !!bought} onChange={(e) => { setCountry(e.target.value); setResults(null); setConfirmation(null); }}>{COUNTRIES.map((c) => <option key={c.code} value={c.code}>{c.name}</option>)}</select></div>
        <div className="form-group"><label className="form-label" htmlFor="number-area">Area code <span className="hint">optional</span></label><input id="number-area" className="form-input" value={areaCode} disabled={busy || !!bought} onChange={(e) => { setAreaCode(e.target.value); setResults(null); setConfirmation(null); }} placeholder="415" /></div>
      </div>
      <button className="btn btn-primary btn-sm" disabled={busy || !!bought || !/^[A-Z]{2}$/.test(country)} onClick={handleSearch}>{busy && !buying ? "Searching…" : "Show available numbers"}</button>
      {results !== null && <div className="phone-number-results" aria-live="polite">
        {results.length === 0 ? <p className="empty-state">No numbers found. Try a different country or area code.</p> : results.map((r) => <div className="phone-number-buy-row" key={r.phone_number}>
          <div><span className="phone-number-address">{r.phone_number}</span><div className="phone-number-sublabel">{[r.region, r.monthly_price ? `${r.monthly_price} / month` : null].filter(Boolean).join(" · ")}</div></div>
          <button className="btn btn-primary btn-sm" disabled={busy || !!bought} onClick={() => setConfirmation(r.phone_number)} aria-label={`Buy ${r.phone_number}`}>{buying === r.phone_number ? "Buying…" : "Buy"}</button>
        </div>)}
      </div>}
      {confirmation && <div className="phone-number-buy-confirm" role="status"><p>Buy <strong className="phone-number-address">{confirmation}</strong>? Your {config.providerLabel} account will be charged.</p><div className="phone-numbers-actions"><button className="btn btn-ghost btn-sm" onClick={() => setConfirmation(null)}>Cancel</button><button className="btn btn-primary btn-sm" onClick={() => void handleBuy(confirmation)}>Confirm buy</button></div></div>}
      {bought && !busy && <button className="btn btn-primary btn-sm" onClick={() => void handleBuy(bought)}>Finish adding {bought}</button>}
      <div className="phone-number-form-actions"><p className="form-hint">Then attach it from an agent&apos;s Settings</p><button className="btn btn-ghost btn-sm" disabled={busy} onClick={onClose}>{bought ? "Close" : "Cancel"}</button></div>
    </>}
  </>;
}
type RemoveMode = "remove_rest" | "release" | "remove_local" | "remove_plain" | "local_locked";

function removeModeFor(config: ConfigRow | null, purchase: PurchasedNumber | undefined, isSuperadmin: boolean): RemoveMode {
  const local = !config || (config.kind === "telephony_config" && config.provider === NATIVE);
  if (local) return isSuperadmin ? "remove_local" : "local_locked";
  if (config.kind === "telephony_config") return "remove_rest";
  return purchase ? "release" : "remove_plain";
}

/** Number routing and removal reuse the existing provider-specific APIs. */
function EditNumberModal({
  target, config, isSuperadmin, onClose, onChanged, onSaved,
}: {
  target: NumberRow; config: ConfigRow | null; isSuperadmin: boolean;
  onClose: () => void; onChanged: () => void; onSaved: (text: string) => void;
}) {
  const [region, setRegion] = useState(target.region ?? "");
  const [status, setStatus] = useState<PhoneNumber["status"]>(target.status);
  const [sync, setSync] = useState<ProviderSync | null>(target.provider_sync ?? null);
  const [purchase, setPurchase] = useState<PurchasedNumber | undefined>(undefined);
  // Whether this carrier number was bought through us; unknown until loaded.
  const [purchaseLookup, setPurchaseLookup] = useState<"loading" | "failed" | "done">(config?.kind === "carrier" ? "loading" : "done");
  // Set when the provider refused to detach: the admin may remove it anyway.
  const [detachRefused, setDetachRefused] = useState<string | null>(null);
  const [busy, setBusy] = useState<null | "save" | "sync" | "remove">(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  // A retry already changed the stored row: refresh the list on close.
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (config?.kind !== "carrier") return;
    let cancelled = false;
    listPurchasedNumbers(target.tenant_id)
      .then((all) => {
        if (cancelled) return;
        setPurchase(all.find((p) => p.carrier_id === config.id && !p.released_at && digits(p.phone_number) === digits(target.did)));
        setPurchaseLookup("done");
      })
      .catch((e) => {
        if (cancelled) return;
        setPurchaseLookup("failed");
        setError(`Couldn't check whether this number was purchased, so it can't be removed safely: ${e instanceof ApiError ? e.detail : String(e)}`);
      });
    return () => { cancelled = true; };
  }, [target, config]);

  const close = () => {
    if (busy !== null) return;
    onClose();
    if (dirty) onChanged();
  };
  const fail = (e: unknown) => setError(e instanceof ApiError ? e.detail : String(e));

  const handleSave = async () => {
    setBusy("save");
    setError(null);
    try {
      await updatePhoneNumber(target.id, { region: region.trim(), status });
      onSaved(`${target.did} was updated.`);
      onClose();
      onChanged();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(null);
    }
  };

  const handleRetry = async () => {
    setBusy("sync");
    setError(null);
    try {
      const updated = await syncPhoneNumber(target.id);
      setSync(updated.provider_sync ?? null);
      setDirty(true);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(null);
    }
  };

  const mode = removeModeFor(config, purchase, isSuperadmin);

  const handleRemove = async (force = false) => {
    setBusy("remove");
    setError(null);
    try {
      // Release first: if the carrier refuses, the number keeps routing here.
      if (mode === "release" && purchase && !force) await releaseNumber(purchase.id);
      await deletePhoneNumber(target.id, { force });
      onClose();
      onChanged();
    } catch (e) {
      setConfirmRemove(false);
      if (e instanceof ApiError && e.status === 502 && mode === "remove_rest") setDetachRefused(e.detail);
      else fail(e);
    } finally {
      setBusy(null);
    }
  };
  const removeBlocked = purchaseLookup !== "done";

  const providerLabel = config?.providerLabel ?? "the provider";
  const syncsWithProvider = config?.kind === "telephony_config" && config.provider !== NATIVE;
  const manualSetup = config !== null && MANUAL_SETUP_PROVIDERS.has(config.provider);
  const sectionTitle = { fontSize: ".68rem", fontWeight: 600, letterSpacing: ".05em", textTransform: "uppercase", color: "var(--text-3)", margin: "18px 0 8px" } as const;

  const REMOVE_COPY: Record<Exclude<RemoveMode, "local_locked">, { button: string; explain: string }> = {
    remove_rest: {
      button: `Remove from Yuviz`,
      explain: manualSetup
        ? `Stops routing this number's calls here. Remove it from your ${providerLabel} Voice Application yourself; the number stays in your account.`
        : `Stops routing this number's calls here and detaches it in ${providerLabel}. The number stays in your ${providerLabel} account.`,
    },
    release: {
      button: "Release to carrier",
      explain: `Returns the number to ${providerLabel} and removes it here. You stop paying for it and may not get it back.`,
    },
    remove_plain: {
      button: "Remove from Yuviz",
      explain: `Stops routing this number's calls here. Nothing changes in your ${providerLabel} account.`,
    },
    remove_local: {
      button: "Remove number",
      explain: "Frees this local number. It can be assigned again to any account afterwards.",
    },
  };

  return (
    <Modal
      open
      title={target.did}
      className="phone-number-dialog"
      trapFocus
      onClose={close}
      footer={
        <>
          {mode !== "local_locked" && <button className="btn btn-ghost btn-sm phone-number-remove" onClick={() => setConfirmRemove(true)} disabled={busy !== null || removeBlocked}>Remove</button>}
          <button className="btn btn-ghost btn-sm" onClick={close} disabled={busy !== null}>Cancel</button>
          <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={busy !== null}>
            {busy === "save" ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      {error && <div className="error-banner" role="alert">{error}</div>}

      <div className="phone-number-editor-through"><span className="phone-number-muted">Through </span><CallingAppIdentity config={config} /></div>
      <fieldset className="phone-number-fields" disabled={busy !== null}>
      <div className="form-group">
        <label className="form-label" htmlFor="edit-number-label">Label <span className="hint">optional</span></label>
        <input id="edit-number-label" className="form-input" value={region} onChange={(e) => setRegion(e.target.value)} />
      </div>
      <div className="phone-number-status-row">
        <label htmlFor="edit-number-on">This number is {status === "active" ? "on" : "off"}</label>
        <label className="toggle-switch">
          <input id="edit-number-on" type="checkbox" checked={status === "active"} onChange={(e) => setStatus(e.target.checked ? "active" : "inactive")} />
          <span className="toggle-slider" />
        </label>
      </div>
      {status === "suspended" && <p className="form-hint">This number is suspended. Turn it on to resume calls.</p>}
      </fieldset>

      {syncsWithProvider && manualSetup && config && (
        <>
          <div style={sectionTitle}>Finish setting up calls</div>
          <div className="form-hint" style={{ lineHeight: 1.6 }}>
            Set up once in {providerLabel}: add this number to your Voice Application and set the application&apos;s URL to{" "}
            <span style={{ fontFamily: "var(--mono)", color: "var(--text)", wordBreak: "break-all" }}>{webhookUrlFor(config)}</span> (POST).
          </div>
        </>
      )}

      {syncsWithProvider && !manualSetup && (
        <>
          <div style={sectionTitle}>Calls from {providerLabel}</div>
          <div
            className={sync?.ok ? "info-banner" : sync ? "error-banner" : undefined}
            style={{ display: "flex", alignItems: "flex-start", gap: 10, ...(sync ? {} : { fontSize: ".8rem", color: "var(--text-2)" }) }}
          >
            <div style={{ flex: 1, lineHeight: 1.5 }}>
              {!sync
                ? `${providerLabel} hasn't been told to send this number's calls here yet.`
                : sync.ok
                  ? `${providerLabel} sends this number's calls here.`
                  : `${providerLabel} isn't sending this number's calls here: ${sync.message ?? "unknown error"}`}
              {sync?.at && (
                <div style={{ fontSize: ".7rem", opacity: 0.75 }}>Last checked {new Date(sync.at).toLocaleString()}</div>
              )}
            </div>
            <button className="btn btn-ghost btn-sm" onClick={handleRetry} disabled={busy !== null}>
              {busy === "sync" ? "Syncing…" : sync?.ok ? "Sync again" : "Retry"}
            </button>
          </div>
        </>
      )}

      {mode === "local_locked" ? (
        <div className="form-hint" style={{ lineHeight: 1.6 }}>
          Local numbers are assigned and removed by the platform. To stop taking calls on it, turn it off above.
        </div>
      ) : (confirmRemove || detachRefused) && (
        <div className="phone-number-remove-confirm">
          <div className="form-hint" style={{ flex: 1, lineHeight: 1.5, margin: 0 }}>
            {detachRefused ? (
              <span style={{ color: "var(--red)" }}>
                {detachRefused}. Remove it anyway? {providerLabel} will keep sending this number&apos;s calls here until you
                change it there.
              </span>
            ) : purchaseLookup === "loading" ? "Checking whether this number was purchased…" : REMOVE_COPY[mode].explain}
          </div>
          {detachRefused ? (
            <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
              <button className="btn btn-ghost btn-sm" onClick={() => setDetachRefused(null)} disabled={busy !== null}>Keep</button>
              <button className="btn btn-danger btn-sm" onClick={() => handleRemove(true)} disabled={busy !== null}>
                {busy === "remove" ? "Removing…" : "Remove anyway"}
              </button>
            </div>
          ) : (
            <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
              <button className="btn btn-ghost btn-sm" onClick={() => setConfirmRemove(false)} disabled={busy !== null}>Keep</button>
              <button className="btn btn-danger btn-sm" onClick={() => handleRemove()} disabled={busy !== null || removeBlocked}>
                {busy === "remove" ? "Removing…" : `Yes, ${REMOVE_COPY[mode].button.toLowerCase()}`}
              </button>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}
