"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import {
  Agent,
  ApiError,
  AvailableNumber,
  Call,
  Carrier,
  PhoneNumber,
  PhoneNumberCreate,
  PurchasedNumber,
  Tenant,
  TelephonyConfig,
  createPhoneNumber,
  deletePhoneNumber,
  ProviderSync,
  syncPhoneNumber,
  syncTelephonyNumbers,
  listAgents,
  listCalls,
  listCarriers,
  listPhoneNumbers,
  listPurchasedNumbers,
  listTelephonyConfigs,
  purchaseNumber,
  releaseNumber,
  searchAvailableNumbers,
  updatePhoneNumber,
} from "@/lib/api";
import { Modal } from "@/components/Modal";
import { CallingAppChooser } from "@/components/CallingAppChooser";
import { callingAppLabel } from "@/lib/callingApps";
import { useConnectionAccess } from "@/lib/useConnectionAccess";
import { useActiveTenant } from "@/lib/useActiveTenant";

// The platform's own Kamailio + FreeSWITCH: no credentials, superadmin-only.
const NATIVE = "native" as const;

// Recent calls per account used for activity columns; there's no per-DID aggregate endpoint.
const CALL_WINDOW = 200;

const fmtInt = (n: number) => n.toLocaleString("en-IN");
const digits = (s: string | null | undefined) => (s ?? "").replace(/\D/g, "");
const providerLabelFor = (_kind: "carrier" | "telephony_config", provider: string) => callingAppLabel(provider);

type SyncNotice = { ok: boolean; text: string };

// What the provider was told when a number was added or re-synced.
const noticeFor = (provider: string, did: string, sync: ProviderSync): SyncNotice =>
  sync.ok
    ? { ok: true, text: `${did}: ${provider} now sends this number's calls here.` }
    : { ok: false, text: `${did} was saved, but ${provider} wasn't updated: ${sync.message ?? "unknown error"}` };

type TrunkHealth = "healthy" | "degraded" | "standby";

const HEALTH_BADGE: Record<TrunkHealth, { label: string; cls: string }> = {
  healthy: { label: "Healthy", cls: "green" },
  degraded: { label: "Degraded", cls: "amber" },
  standby: { label: "Standby", cls: "gray" },
};

type ConfigKind = "carrier" | "telephony_config";

interface ConfigRow {
  kind: ConfigKind;
  id: string;
  /** `${kind}:${id}` — the value carried in the `?config=` query param. */
  key: string;
  name: string;
  providerLabel: string;
  provider: string;
  tenantId: string;
  tenantName: string;
  accountRef: string | null;
  isDefaultOutbound: boolean;
  dids: number;
  activeDids: number;
  suspendedDids: number;
  calls: number;
  health: TrunkHealth;
  carrier: Carrier | null;
  telephonyConfig: TelephonyConfig | null;
}

interface NumberRow extends PhoneNumber {
  tenantName: string;
  configKind: ConfigKind | null;
  configId: string | null;
  configName: string | null;
  routesTo: string;
  inbound: number;
  outbound: number;
}

function parseConfigKey(key: string | null): { kind: ConfigKind; id: string } | null {
  if (!key) return null;
  const [kind, id] = key.split(":");
  if ((kind === "carrier" || kind === "telephony_config") && id) return { kind, id };
  return null;
}

/** One configuration card: identity, health, and the three counts this
    platform can actually answer. Channel capacity, ASR and MOS are not on
    the carrier/telephony_config record and no telemetry feed writes them —
    the card states that once, below the grid, instead of drawing plausible
    numbers. */
function ConfigCard({
  config, totalCalls, showTenant, onOpen,
}: { config: ConfigRow; totalCalls: number; showTenant: boolean; onOpen: () => void }) {
  const badge = HEALTH_BADGE[config.health];
  const sharePct = totalCalls > 0 ? (config.calls / totalCalls) * 100 : 0;
  return (
    <div
      className="card"
      style={{ padding: "14px 16px", flex: "1 1 260px", minWidth: 240, cursor: "pointer" }}
      onClick={onOpen}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <span
          style={{
            width: 7, height: 7, borderRadius: "50%", flexShrink: 0,
            background: config.health === "healthy" ? "var(--green)" : config.health === "degraded" ? "var(--amber)" : "var(--text-3)",
          }}
        />
        <span style={{ fontSize: ".85rem", fontWeight: 600, color: "var(--text)" }}>{config.name}</span>
        <span style={{ fontSize: ".7rem", color: "var(--text-3)" }}>· {config.providerLabel}</span>
      </div>
      <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 10, flexWrap: "wrap" }}>
        <span className={`badge ${badge.cls}`}>{badge.label}</span>
        {config.isDefaultOutbound && <span className="badge amber">Default outbound</span>}
        {showTenant && <span className="badge gray">{config.tenantName}</span>}
      </div>
      <div style={{ fontFamily: "var(--mono)", fontSize: ".68rem", color: "var(--text-3)", marginBottom: 12, wordBreak: "break-all" }}>
        {config.accountRef ?? "no account reference"}
      </div>
      <div style={{ display: "flex", gap: 18 }}>
        <Metric label="DIDs" value={fmtInt(config.dids)} />
        <Metric label="Active" value={fmtInt(config.activeDids)} />
        <Metric label="Calls" value={fmtInt(config.calls)} />
      </div>
      <div style={{ height: 3, borderRadius: 2, background: "var(--surf-3)", margin: "12px 0 8px" }}>
        {totalCalls > 0 && (
          <div style={{ width: `${Math.min(100, sharePct)}%`, height: "100%", borderRadius: 2, background: "var(--cyan)" }} />
        )}
      </div>
      <div style={{ fontSize: ".68rem", color: "var(--text-3)" }}>
        {totalCalls > 0
          ? `${sharePct.toFixed(0)}% of the account's recent calls matched this configuration's DIDs`
          : "no calls in the recent window"}
        {config.suspendedDids > 0 && ` · ${fmtInt(config.suspendedDids)} suspended DID${config.suspendedDids === 1 ? "" : "s"}`}
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ fontSize: ".64rem", fontWeight: 600, letterSpacing: ".04em", textTransform: "uppercase", color: "var(--text-3)" }}>
        {label}
      </div>
      <div style={{ fontSize: "1.15rem", fontWeight: 600, color: "var(--text)", fontVariantNumeric: "tabular-nums", lineHeight: 1.3 }}>
        {value}
      </div>
    </div>
  );
}

const STATUS_BADGE: Record<PhoneNumber["status"], string> = { active: "green", inactive: "gray", suspended: "amber" };
const statusBadge = (s: PhoneNumber["status"]) => <span className={`badge ${STATUS_BADGE[s]}`}>{s}</span>;

const typeBadge = (n: NumberRow) => {
  if (n.inbound > 0 && n.outbound > 0) return <span className="badge cyan">Both</span>;
  if (n.inbound > 0) return <span className="badge green">Inbound</span>;
  if (n.outbound > 0) return <span className="badge amber">Outbound</span>;
  return <span className="badge gray">No traffic</span>;
};

export default function TelephonyPage() {
  const { tenant, allTenants, isPlatformScoped, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const router = useRouter();
  const searchParams = useSearchParams();
  const selectedKey = parseConfigKey(searchParams.get("config"));

  const [configs, setConfigs] = useState<ConfigRow[]>([]);
  const [numbers, setNumbers] = useState<NumberRow[]>([]);
  const [agentsByTenant, setAgentsByTenant] = useState<Record<string, { agents: Agent[]; ok: boolean }>>({});
  /** Calls fetched per account id; denominator for each config's share (same window as DID counts). */
  const [callsByTenant, setCallsByTenant] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const refresh = () => setReloadKey((k) => k + 1);

  const [numberSearch, setNumberSearch] = useState("");
  const [editNumber, setEditNumber] = useState<NumberRow | null>(null);
  // Lives here, not in ConfigDetail: a refresh briefly unmounts the detail view.
  const [syncNotice, setSyncNotice] = useState<SyncNotice | null>(null);
  const access = useConnectionAccess();
  const { isSuperadmin } = access;

  // Each account's fetch settles independently so one failing tenant doesn't blank the page.
  useEffect(() => {
    if (tenantLoading) return;
    const targets = isAllTenants ? allTenants : tenant ? [tenant] : [];
    if (targets.length === 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setConfigs([]);
      setNumbers([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    let cancelled = false;
    (async () => {
      const results = await Promise.allSettled(
        targets.map(async (t) => {
          const [carriers, telephonyConfigs, phoneNumbers, agentList, calls] = await Promise.all([
            listCarriers(t.id),
            listTelephonyConfigs(t.id),
            listPhoneNumbers(t.id),
            // Routing/activity failures degrade to "—" instead of failing the page.
            listAgents(t.slug).then((a) => ({ ok: true, agents: a })).catch(() => ({ ok: false, agents: [] as Agent[] })),
            listCalls(t.slug, { limit: CALL_WINDOW }).then((r) => r.items).catch((): Call[] => []),
          ]);
          return { t, carriers, telephonyConfigs, phoneNumbers, agents: agentList.agents, agentsOk: agentList.ok, calls };
        }),
      );
      if (cancelled) return;

      const configRows: ConfigRow[] = [];
      const numberRows: NumberRow[] = [];
      const callTotals: Record<string, number> = {};
      const agentsMap: Record<string, { agents: Agent[]; ok: boolean }> = {};
      const errs: string[] = [];

      results.forEach((r, i) => {
        if (r.status !== "fulfilled") {
          errs.push(`${targets[i].name}: ${r.reason instanceof ApiError ? r.reason.detail : String(r.reason)}`);
          return;
        }
        const { t, carriers, telephonyConfigs, phoneNumbers, agents, agentsOk, calls } = r.value;
        callTotals[t.id] = calls.length;
        agentsMap[t.id] = { agents, ok: agentsOk };
        const agentName = (id: string | null) => (id ? agents.find((a) => a.id === id)?.name ?? null : null);

        // Numbers are free-text from the gateway; compare digits only.
        const inboundByDid = new Map<string, number>();
        const outboundByDid = new Map<string, number>();
        for (const c of calls) {
          const to = digits(c.called_number);
          const from = digits(c.caller_number);
          if (to) inboundByDid.set(to, (inboundByDid.get(to) ?? 0) + 1);
          if (from) outboundByDid.set(from, (outboundByDid.get(from) ?? 0) + 1);
        }

        const rowsForTenant = phoneNumbers.map((n): NumberRow => {
          const key = digits(n.did);
          const carrier = carriers.find((c) => c.id === n.carrier_id) ?? null;
          const telephonyConfig = telephonyConfigs.find((c) => c.id === n.telephony_config_id) ?? null;
          const primary = agentName(n.agent_id);
          const fallback = agentName(n.fallback_agent_id);
          return {
            ...n,
            tenantName: t.name,
            configKind: carrier ? "carrier" : telephonyConfig ? "telephony_config" : null,
            configId: carrier?.id ?? telephonyConfig?.id ?? null,
            configName: carrier?.name ?? telephonyConfig?.name ?? null,
            // Unresolvable agent_id shows "—", never "Account default agent".
            routesTo: !agentsOk && (n.agent_id || n.fallback_agent_id)
              ? "—"
              : primary ?? (fallback ? `${fallback} (fallback)` : "Account default agent"),
            inbound: inboundByDid.get(key) ?? 0,
            outbound: outboundByDid.get(key) ?? 0,
          };
        });
        numberRows.push(...rowsForTenant);

        const buildConfig = (
          kind: ConfigKind, id: string, name: string, provider: string, accountRef: string | null,
          isDefaultOutbound: boolean, dids: NumberRow[], carrier: Carrier | null, telephonyConfig: TelephonyConfig | null,
        ): ConfigRow => {
          const suspended = dids.filter((d) => d.status === "suspended").length;
          const active = dids.filter((d) => d.status === "active").length;
          return {
            kind, id, key: `${kind}:${id}`, name, providerLabel: providerLabelFor(kind, provider), provider,
            tenantId: t.id, tenantName: t.name, accountRef, isDefaultOutbound,
            dids: dids.length, activeDids: active, suspendedDids: suspended,
            calls: dids.reduce((sum, d) => sum + d.inbound + d.outbound, 0),
            // telephony_configs have real health status; carriers fall back to the DID-count heuristic.
            health: telephonyConfig
              ? (telephonyConfig.health?.status ?? "standby")
              : dids.length === 0 ? "standby" : suspended > 0 ? "degraded" : active > 0 ? "healthy" : "standby",
            carrier, telephonyConfig,
          };
        };

        for (const c of carriers) {
          configRows.push(
            buildConfig("carrier", c.id, c.name, c.provider, c.carrier_account_ref ?? c.auth_id ?? null, false,
              rowsForTenant.filter((n) => n.carrier_id === c.id), c, null),
          );
        }
        for (const tc of telephonyConfigs) {
          const domain = tc.credentials?.domain;
          configRows.push(
            buildConfig("telephony_config", tc.id, tc.name, tc.provider, typeof domain === "string" ? domain : null,
              tc.is_default_outbound, rowsForTenant.filter((n) => n.telephony_config_id === tc.id), null, tc),
          );
        }
      });

      setConfigs(configRows);
      setNumbers(numberRows);
      setCallsByTenant(callTotals);
      setAgentsByTenant(agentsMap);
      setError(errs.length > 0 ? errs.join("; ") : null);
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [tenant, allTenants, isAllTenants, tenantLoading, reloadKey]);

  const visibleConfigs = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = q
      ? configs.filter((c) => `${c.name} ${c.providerLabel} ${c.tenantName}`.toLowerCase().includes(q))
      : configs;
    return [...matched].sort((a, b) => b.calls - a.calls || a.name.localeCompare(b.name));
  }, [configs, search]);

  const configFor = (n: NumberRow) => configs.find((c) => c.kind === n.configKind && c.id === n.configId) ?? null;

  const visibleNumbers = useMemo(() => {
    const q = numberSearch.trim().toLowerCase();
    const qDigits = digits(q);
    const matched = q
      ? numbers.filter((n) =>
          `${n.did} ${n.region ?? ""} ${n.configName ?? ""} ${n.routesTo} ${n.tenantName} ${n.status}`.toLowerCase().includes(q)
          || (qDigits.length > 0 && digits(n.did).includes(qDigits)))
      : numbers;
    return [...matched].sort((a, b) => a.tenantName.localeCompare(b.tenantName) || a.did.localeCompare(b.did));
  }, [numbers, numberSearch]);

  const selectedConfig = selectedKey ? configs.find((c) => c.key === `${selectedKey.kind}:${selectedKey.id}`) ?? null : null;
  const wantsAdd = searchParams.get("add") === "1";
  const connectCalling = wantsAdd && !selectedKey && !loading && !access.loading
    && !configs.some((config) => isSuperadmin || !(config.kind === "telephony_config" && config.provider === NATIVE));

  // "+ Add Number" from the top bar: open the first connection's add-number dialog, or connect a carrier first.
  useEffect(() => {
    if (!wantsAdd || loading || access.loading) return;
    if (selectedKey) {
      router.replace(`/telephony?config=${encodeURIComponent(`${selectedKey.kind}:${selectedKey.id}`)}`);
      return;
    }
    const target = configs.find(
      (c) => isSuperadmin || !(c.kind === "telephony_config" && c.provider === NATIVE),
    );
    if (target) {
      router.replace(`/telephony?config=${encodeURIComponent(target.key)}&add=1`);
    }
  }, [wantsAdd, loading, selectedKey, configs, isSuperadmin, access.loading, router]);

  const accountLine = isAllTenants
    ? `Phone numbers, call activity and routing across ${allTenants.length} account${allTenants.length === 1 ? "" : "s"}.`
    : tenant
      ? `Phone numbers, call activity and routing for ${tenant.name}.${isPlatformScoped ? " Switch accounts from the header." : ""}`
      : "Phone numbers, call activity and routing.";

  if (selectedKey && !loading) {
    return selectedConfig ? (
      <ConfigDetail
        config={selectedConfig}
        numbers={numbers.filter((n) => n.configKind === selectedConfig.kind && n.configId === selectedConfig.id)}
        tenant={allTenants.find((t) => t.id === selectedConfig.tenantId) ?? null}
        agents={agentsByTenant[selectedConfig.tenantId]?.agents ?? []}
        isSuperadmin={isSuperadmin}
        openAddNumber={wantsAdd}
        syncNotice={syncNotice}
        onSyncNotice={setSyncNotice}
        onBack={() => { setSyncNotice(null); router.push("/telephony"); }}
        onChanged={refresh}
      />
    ) : (
      <div className="card">
        <div className="empty-state">
          Configuration not found — it may have been removed, or belongs to an account outside the current
          selection.
        </div>
        <button className="btn btn-ghost btn-sm" onClick={() => router.push("/telephony")} style={{ marginTop: 10 }}>
          Back to Phone Numbers
        </button>
      </div>
    );
  }

  return (
    <>
      {error && <div className="error-banner">{error}</div>}
      {syncNotice && <div className={syncNotice.ok ? "info-banner" : "error-banner"} role="status">{syncNotice.text}</div>}
      {access.error && !connectCalling && <div className="error-banner" role="alert">{access.error} <button className="btn btn-ghost btn-sm" disabled={access.loading} onClick={() => void access.retry()}>Retry permissions</button></div>}
      {connectCalling && <CallingAppChooser tenants={allTenants} tenantId={tenant?.id} access={access} onClose={() => router.replace("/telephony")} onSaved={(name) => {
        router.replace("/telephony");
        setSyncNotice({ ok: true, text: `${name} connected. Open the connection below to buy or add a phone number.` });
        refresh();
      }} />}

      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
        <div>
          <h1 style={{ fontSize: "1.5rem", fontWeight: 600, letterSpacing: "-.025em", margin: 0, color: "var(--text)" }}>Phone Numbers</h1>
          <div className="form-hint" style={{ marginTop: 4 }}>{accountLine}</div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {isSuperadmin && <Link href="/live-calls" className="btn btn-ghost btn-sm">Live calls</Link>}
        </div>
      </div>

      {loading ? (
        <div className="card">
          <div className="empty-state">Loading telephony…</div>
        </div>
      ) : (
        <>
          <div className="card">
            <div className="card-hdr">
              <span className="card-title">Connected calling apps</span>
              <span className="card-sub" style={{ marginLeft: "auto" }}>
                {fmtInt(visibleConfigs.length)} of {fmtInt(configs.length)} configured
              </span>
              <input
                className="form-input"
                style={{ width: 200 }}
                placeholder="Search calling apps"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <Link className="btn btn-ghost btn-sm" href="/integrations">Manage connected apps</Link>
            </div>

            {visibleConfigs.length === 0 ? (
              <div className="empty-state">
                {configs.length === 0
                  ? "No calling apps connected yet. Connect an app on Connected Apps, then come back to buy or add a number."
                  : `No configurations match "${search}".`}
              </div>
            ) : (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 12, padding: 16 }}>
                {visibleConfigs.map((c) => (
                  <ConfigCard
                    key={c.key}
                    config={c}
                    totalCalls={callsByTenant[c.tenantId] ?? 0}
                    showTenant={isAllTenants}
                    onOpen={() => router.push(`/telephony?config=${c.key}`)}
                  />
                ))}
              </div>
            )}
          </div>
          <div className="form-hint" style={{ margin: "10px 0 18px" }}>
            Counts cover the most recent {fmtInt(CALL_WINDOW)} calls per account. Channel capacity, ASR and MOS need a
            carrier telemetry feed, which is not connected to this console yet.
          </div>

          <div className="card">
            <div className="card-hdr">
              <span className="card-title">All numbers</span>
              <span className="card-sub" style={{ marginLeft: "auto" }}>
                {fmtInt(visibleNumbers.length)} of {fmtInt(numbers.length)}
              </span>
              <input
                className="form-input"
                style={{ width: 200 }}
                placeholder="Search numbers or agents"
                value={numberSearch}
                onChange={(e) => setNumberSearch(e.target.value)}
              />
            </div>
            {visibleNumbers.length === 0 ? (
              <div className="empty-state">
                {numbers.length === 0
                  ? "No numbers yet. Choose a connected calling app above to buy or add one."
                  : `No numbers match "${numberSearch}".`}
              </div>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Number</th>
                      {isAllTenants && <th>Account</th>}
                      <th>Calling app</th>
                      <th>Routes to</th>
                      <th>Status</th>
                      <th>Provider routing</th>
                      <th style={{ textAlign: "right" }}>Calls (recent)</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleNumbers.map((n) => {
                      const cfg = configFor(n);
                      return (
                        <tr key={n.id}>
                          <td className="bold mono" style={{ color: "var(--text)" }}>
                            {n.did}
                            {n.region && <div style={{ fontFamily: "inherit", fontWeight: 400, fontSize: ".7rem", color: "var(--text-3)" }}>{n.region}</div>}
                          </td>
                          {isAllTenants && <td>{n.tenantName}</td>}
                          <td>
                            {cfg ? (
                              <Link href={`/telephony?config=${cfg.key}`} style={{ color: "var(--text)" }}>
                                {cfg.name} <span style={{ color: "var(--text-3)" }}>· {cfg.providerLabel}</span>
                              </Link>
                            ) : (
                              <i style={{ color: "var(--text-3)" }}>none</i>
                            )}
                          </td>
                          <td>{n.routesTo}</td>
                          <td>{statusBadge(n.status)}</td>
                          <td>{syncBadge(n, cfg)}</td>
                          <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{fmtInt(n.inbound + n.outbound)}</td>
                          <td>
                            <button className="btn btn-ghost btn-sm" onClick={() => setEditNumber(n)}>Edit</button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}

      <EditNumberModal
        target={editNumber}
        config={editNumber ? configFor(editNumber) : null}
        agents={editNumber ? agentsByTenant[editNumber.tenant_id]?.agents ?? [] : []}
        isSuperadmin={isSuperadmin}
        onClose={() => setEditNumber(null)}
        onChanged={refresh}
      />

    </>
  );
}

function webhookUrlFor(config: ConfigRow): string | null {
  if (config.kind !== "telephony_config") return null;
  if (config.provider === "cloudonix") {
    const base = process.env.NEXT_PUBLIC_CLOUDONIX_SERVICE_URL || "";
    return `${base}/cloudonix/voice/${config.id}`;
  }
  if (config.provider === "vobiz") {
    // Route shape: /{provider}/voice/{account_ref} (docs/telephony.md).
    const base = process.env.NEXT_PUBLIC_VOBIZ_SERVICE_URL || "";
    return `${base}/vobiz/voice/${config.id}`;
  }
  return null;
}

function ConfigDetail({
  config, numbers, tenant, agents, isSuperadmin, openAddNumber, syncNotice, onSyncNotice, onBack, onChanged,
}: {
  config: ConfigRow; numbers: NumberRow[]; tenant: Tenant | null; agents: Agent[]; isSuperadmin: boolean;
  openAddNumber: boolean;
  syncNotice: SyncNotice | null; onSyncNotice: (n: SyncNotice | null) => void;
  onBack: () => void; onChanged: () => void;
}) {
  const isNative = config.kind === "telephony_config" && config.provider === NATIVE;
  const syncsWithProvider = config.kind === "telephony_config" && !isNative;
  // REST providers whose inbound routing the platform sets up itself.
  const autoSyncs = syncsWithProvider && !MANUAL_SETUP_PROVIDERS.has(config.provider);
  const [resyncing, setResyncing] = useState(false);

  const handleResync = async () => {
    setResyncing(true);
    onSyncNotice(null);
    try {
      const { results, application } = await syncTelephonyNumbers(config.id);
      const failed = results.filter((r) => !r.ok);
      onSyncNotice(
        application && !application.ok
          ? { ok: false, text: `${config.providerLabel} kept the old address for every number here: ${application.message ?? "unknown error"}` }
          : results.length === 0
          ? { ok: true, text: "No numbers on this configuration to sync." }
          : failed.length === 0
            ? { ok: true, text: `${results.length} number${results.length === 1 ? "" : "s"} re-synced with ${config.providerLabel}.` }
            : { ok: false, text: failed.map((r) => noticeFor(config.providerLabel, r.did, r).text).join(" ") },
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setResyncing(false);
    }
  };
  const [error, setError] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(openAddNumber);
  const [purchased, setPurchased] = useState<PurchasedNumber[]>([]);
  const [editTarget, setEditTarget] = useState<NumberRow | null>(null);

  useEffect(() => {
    if (config.kind !== "carrier" || !tenant) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setPurchased([]);
      return;
    }
    let cancelled = false;
    listPurchasedNumbers(tenant.id)
      .then((p) => { if (!cancelled) setPurchased(p); })
      .catch(() => { if (!cancelled) setPurchased([]); });
    return () => { cancelled = true; };
  }, [config.kind, config.id, tenant]);

  const purchasedFor = (n: NumberRow) =>
    purchased.find((p) => p.carrier_id === config.id && digits(p.phone_number) === digits(n.did));

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
        <button className="btn btn-ghost btn-sm" onClick={onBack}><ArrowLeft size={13} /> Phone Numbers</button>
        <h1 style={{ fontSize: "1.3rem", fontWeight: 600, letterSpacing: "-.025em", margin: 0, color: "var(--text)" }}>
          {config.name}
        </h1>
        <span className="badge indigo">{config.providerLabel}</span>
        {config.isDefaultOutbound && <span className="badge amber">Default outbound</span>}
      </div>

      {error && <div className="error-banner">{error}</div>}
      {syncNotice && <div className={syncNotice.ok ? "info-banner" : "error-banner"}>{syncNotice.text}</div>}

      <div className="info-banner" style={{ marginBottom: 16 }}>
        {config.providerLabel} is connected. {isNative && "Phone numbers are assigned by the platform; account admins can manage each number’s agent and status. "}
        <Link href={`/integrations?manage=calling:${config.provider}`}>Manage this app on Connected Apps</Link>.
        {webhookUrlFor(config) && <div style={{ marginTop: 8 }}>
          <strong>Inbound webhook (POST): </strong><code style={{ overflowWrap: "anywhere" }}>{webhookUrlFor(config)}</code>
        </div>}
      </div>

      <div className="card">
        <div className="card-hdr">
          <span className="card-title">Phone numbers</span>
          <span className="card-sub">{fmtInt(numbers.length)} DID{numbers.length === 1 ? "" : "s"}</span>
          {autoSyncs && numbers.length > 0 && (
            <button
              className="btn btn-ghost btn-sm"
              style={{ marginLeft: "auto" }}
              onClick={handleResync}
              disabled={resyncing}
              title={`Point every number here at this platform in ${config.providerLabel} again, e.g. after the public URL changed`}
            >
              {resyncing ? "Re-syncing…" : "Re-sync numbers"}
            </button>
          )}
          {(!isNative || isSuperadmin) && (
            <button
              className="btn btn-primary btn-sm"
              style={{ marginLeft: autoSyncs && numbers.length > 0 ? 8 : "auto" }}
              onClick={() => setAddOpen(true)}
            >
              + Add phone number
            </button>
          )}
        </div>
        {numbers.length === 0 ? (
          <div className="empty-state">No phone numbers on this configuration yet.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Address</th>
                <th>Label</th>
                <th>Status</th>
                <th>Inbound agent</th>
                {syncsWithProvider && <th>{config.providerLabel} routing</th>}
                <th>Traffic</th>
                <th style={{ textAlign: "right" }}>Calls (recent)</th>
                {config.kind === "carrier" && <th>Purchased</th>}
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {numbers.map((n) => {
                const p = config.kind === "carrier" ? purchasedFor(n) : undefined;
                return (
                  <tr key={n.id}>
                    <td className="bold mono" style={{ color: "var(--text)" }}>{n.did}</td>
                    <td>{n.region || <i style={{ color: "var(--text-3)" }}>none</i>}</td>
                    <td>{statusBadge(n.status)}</td>
                    <td>{n.routesTo}</td>
                    {syncsWithProvider && <td>{syncBadge(n, config)}</td>}
                    <td>{typeBadge(n)}</td>
                    <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                      {fmtInt(n.inbound + n.outbound)}
                    </td>
                    {config.kind === "carrier" && (
                      <td>{p ? new Date(p.purchased_at).toLocaleDateString() : "—"}</td>
                    )}
                    <td>
                      <button className="btn btn-ghost btn-sm" onClick={() => setEditTarget(n)}>Edit</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>


      {tenant && (
        <AddNumberModal
          open={addOpen}
          onClose={() => setAddOpen(false)}
          config={config}
          tenant={tenant}
          agents={agents}
          onCreated={(did, sync) => {
            setAddOpen(false);
            onSyncNotice(sync ? noticeFor(config.providerLabel, did, sync) : null);
            onChanged();
          }}
        />
      )}

      <EditNumberModal
        target={editTarget}
        config={config}
        agents={agents}
        isSuperadmin={isSuperadmin}
        onClose={() => setEditTarget(null)}
        onChanged={onChanged}
      />
    </>
  );
}

function AddNumberModal({
  open, onClose, config, tenant, agents, onCreated,
}: {
  open: boolean; onClose: () => void; config: ConfigRow; tenant: Tenant; agents: Agent[];
  onCreated: (did: string, sync?: ProviderSync) => void;
}) {
  const [tab, setTab] = useState<"manual" | "buy">("manual");
  const [error, setError] = useState<string | null>(null);

  // Manual tab
  const [did, setDid] = useState("");
  const [region, setRegion] = useState("");
  const [agentId, setAgentId] = useState("");
  const [active, setActive] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  // Buy tab (carrier-backed only) — moved from SipPanel unchanged in behavior.
  const [buyCountry, setBuyCountry] = useState("US");
  const [buyAreaCode, setBuyAreaCode] = useState("");
  const [searchResults, setSearchResults] = useState<AvailableNumber[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [purchasingNumber, setPurchasingNumber] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setTab("manual");
    setError(null);
    setDid("");
    setRegion("");
    setAgentId("");
    setActive(true);
    setBuyCountry("US");
    setBuyAreaCode("");
    setSearchResults(null);
  }, [open]);

  const handleManualCreate = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const body: PhoneNumberCreate = {
        did,
        region: region || undefined,
        agent_id: agentId || undefined,
        status: active ? "active" : "inactive",
        ...(config.kind === "carrier" ? { carrier_id: config.id } : { telephony_config_id: config.id }),
      };
      const created = await createPhoneNumber(tenant.id, body);
      onCreated(created.did, created.provider_sync ?? undefined);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const handleSearch = async () => {
    setSearching(true);
    setError(null);
    setSearchResults(null);
    try {
      const results = await searchAvailableNumbers(tenant.id, {
        carrier_id: config.id, country: buyCountry, area_code: buyAreaCode || undefined, limit: 10,
      });
      setSearchResults(results);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSearching(false);
    }
  };

  const handlePurchase = async (phoneNumber: string) => {
    if (!window.confirm(`Purchase ${phoneNumber}? This charges your carrier account for real.`)) return;
    setPurchasingNumber(phoneNumber);
    setError(null);
    try {
      await purchaseNumber(tenant.id, { carrier_id: config.id, phone_number: phoneNumber });
      const created = await createPhoneNumber(tenant.id, { did: phoneNumber, carrier_id: config.id, status: "active" });
      onCreated(created.did, created.provider_sync ?? undefined);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setPurchasingNumber(null);
    }
  };

  return (
    <Modal
      open={open}
      title="Add Phone Number"
      onClose={onClose}
      footer={
        tab === "manual" ? (
          <>
            <button className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>
            <button className="btn btn-primary btn-sm" onClick={handleManualCreate} disabled={submitting || !did}>
              {submitting ? "Adding…" : "Add Number"}
            </button>
          </>
        ) : (
          <button className="btn btn-ghost btn-sm" onClick={onClose}>Close</button>
        )
      }
    >
      {error && <div className="error-banner">{error}</div>}

      <p style={{ fontSize: ".78rem", color: "var(--text-3)", marginTop: -4, marginBottom: 14 }}>
        {config.provider === NATIVE
          ? "A local SIP number or extension on the platform's FreeSWITCH. It only receives calls if the platform's SIP proxy routes it there (locally: 5000–5009)."
          : "PSTN numbers (E.164), SIP URIs (sip:user@host), and SIP extensions are all supported."}
      </p>

      {config.kind === "carrier" && (
        <div style={{ display: "flex", gap: 6, marginBottom: 14 }}>
          <button className={`btn btn-sm ${tab === "manual" ? "btn-primary" : "btn-ghost"}`} onClick={() => setTab("manual")}>
            Enter manually
          </button>
          <button className={`btn btn-sm ${tab === "buy" ? "btn-primary" : "btn-ghost"}`} onClick={() => setTab("buy")}>
            Buy a new number
          </button>
        </div>
      )}

      {tab === "manual" ? (
        <>
          <div className="form-group">
            <label className="form-label">
              Address <span className="required">*</span>
              <span className="hint">E.164, SIP URI or extension — e.g. 5000, +14085551000</span>
            </label>
            <input
              className="form-input" style={{ fontFamily: "var(--mono)" }} value={did} onChange={(e) => setDid(e.target.value)}
              placeholder="+19781899185, sip:101@asterisk.local, or 101"
            />
          </div>
          <div className="form-group">
            <label className="form-label">Label <span className="hint">optional — free text, e.g. a region or team name</span></label>
            <input className="form-input" value={region} onChange={(e) => setRegion(e.target.value)} />
          </div>
          <div className="form-group">
            <label className="form-label">Inbound agent</label>
            <select className="form-select" value={agentId} onChange={(e) => setAgentId(e.target.value)}>
              <option value="">— none (routes to default) —</option>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>{a.name}</option>
              ))}
            </select>
          </div>
          <div
            className="form-group"
            style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              padding: "12px 0", borderTop: "1px solid var(--border)", borderBottom: "1px solid var(--border)",
            }}
          >
            <label className="form-label" style={{ marginBottom: 0 }}>Active</label>
            <label className="toggle-switch">
              <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
              <span className="toggle-slider" />
            </label>
          </div>
        </>
      ) : (
        <>
          <div className="form-group">
            <label className="form-label">Country</label>
            <input className="form-input" value={buyCountry} onChange={(e) => setBuyCountry(e.target.value.toUpperCase())} maxLength={2} />
          </div>
          <div className="form-group">
            <label className="form-label">Area Code (optional)</label>
            <input className="form-input" value={buyAreaCode} onChange={(e) => setBuyAreaCode(e.target.value)} placeholder="415" />
          </div>
          <button className="btn btn-primary btn-sm" onClick={handleSearch} disabled={searching}>
            {searching ? "Searching…" : "Search"}
          </button>

          {searchResults !== null && (
            <div style={{ marginTop: 16 }}>
              {searchResults.length === 0 ? (
                <div className="empty-state">No numbers found — try a different area code.</div>
              ) : (
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Number</th>
                      <th>Region</th>
                      <th>Capabilities</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {searchResults.map((r) => (
                      <tr key={r.phone_number}>
                        <td className="bold mono">{r.phone_number}</td>
                        <td>{r.region ?? "—"}</td>
                        <td>
                          {r.capabilities.map((cap) => (
                            <span key={cap} className="badge gray" style={{ marginRight: 4 }}>{cap}</span>
                          ))}
                        </td>
                        <td style={{ textAlign: "right" }}>
                          <button className="btn btn-primary btn-sm" onClick={() => handlePurchase(r.phone_number)} disabled={purchasingNumber !== null}>
                            {purchasingNumber === r.phone_number ? "Purchasing…" : "Purchase"}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </>
      )}
    </Modal>
  );
}

type SyncState = "synced" | "failed" | "pending" | "manual" | "none";

// Providers whose inbound routing the platform can't set itself yet.
const MANUAL_SETUP_PROVIDERS = new Set(["cloudonix"]);

// Whether the provider has been told to send this number's calls here.
function syncStateOf(n: PhoneNumber, config: ConfigRow | null): SyncState {
  if (!config || config.kind !== "telephony_config" || config.provider === NATIVE) return "none";
  if (MANUAL_SETUP_PROVIDERS.has(config.provider)) return "manual";
  if (!n.provider_sync) return "pending";
  return n.provider_sync.ok ? "synced" : "failed";
}

const SYNC_BADGE: Record<Exclude<SyncState, "none">, { label: string; cls: string }> = {
  synced: { label: "Synced", cls: "green" },
  failed: { label: "Sync failed", cls: "red" },
  pending: { label: "Not synced", cls: "gray" },
  manual: { label: "Manual setup", cls: "amber" },
};

const syncBadge = (n: PhoneNumber, config: ConfigRow | null) => {
  const state = syncStateOf(n, config);
  if (state === "none") return <span style={{ color: "var(--text-3)" }}>—</span>;
  const b = SYNC_BADGE[state];
  return <span className={`badge ${b.cls}`} title={n.provider_sync?.message ?? undefined}>{b.label}</span>;
};

type RemoveMode = "remove_rest" | "release" | "remove_local" | "remove_plain" | "local_locked";

function removeModeFor(config: ConfigRow | null, purchase: PurchasedNumber | undefined, isSuperadmin: boolean): RemoveMode {
  const local = !config || (config.kind === "telephony_config" && config.provider === NATIVE);
  if (local) return isSuperadmin ? "remove_local" : "local_locked";
  if (config.kind === "telephony_config") return "remove_rest";
  return purchase ? "release" : "remove_plain";
}

/** Everything about one number: identity, routing, status, provider sync,
    and removing it. Opened from a configuration's table or All numbers. */
function EditNumberModal({
  target, config, agents, isSuperadmin, onClose, onChanged,
}: {
  target: NumberRow | null; config: ConfigRow | null; agents: Agent[]; isSuperadmin: boolean;
  onClose: () => void; onChanged: () => void;
}) {
  const [agentId, setAgentId] = useState("");
  const [fallbackAgentId, setFallbackAgentId] = useState("");
  const [region, setRegion] = useState("");
  const [status, setStatus] = useState<PhoneNumber["status"]>("active");
  const [sync, setSync] = useState<ProviderSync | null>(null);
  const [purchase, setPurchase] = useState<PurchasedNumber | undefined>(undefined);
  // Whether this carrier number was bought through us; unknown until loaded.
  const [purchaseLookup, setPurchaseLookup] = useState<"loading" | "failed" | "done">("done");
  // Set when the provider refused to detach: the admin may remove it anyway.
  const [detachRefused, setDetachRefused] = useState<string | null>(null);
  const [busy, setBusy] = useState<null | "save" | "sync" | "remove">(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  // A retry already changed the stored row: refresh the list on close.
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!target) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setAgentId(target.agent_id ?? "");
    setFallbackAgentId(target.fallback_agent_id ?? "");
    setRegion(target.region ?? "");
    setStatus(target.status);
    setSync(target.provider_sync ?? null);
    setPurchase(undefined);
    setConfirmRemove(false);
    setDetachRefused(null);
    setDirty(false);
    setError(null);
    setPurchaseLookup(config?.kind === "carrier" ? "loading" : "done");
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
    onClose();
    if (dirty) onChanged();
  };
  const fail = (e: unknown) => setError(e instanceof ApiError ? e.detail : String(e));

  const handleSave = async () => {
    if (!target) return;
    setBusy("save");
    setError(null);
    try {
      await updatePhoneNumber(target.id, {
        agent_id: agentId || null,
        fallback_agent_id: fallbackAgentId || null,
        region: region || undefined,
        status,
      });
      onClose();
      onChanged();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(null);
    }
  };

  const handleRetry = async () => {
    if (!target) return;
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
    if (!target) return;
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
  const rowStyle = { display: "flex", gap: 10, padding: "5px 0", fontSize: ".8rem" } as const;
  const labelStyle = { color: "var(--text-3)", width: 120, flexShrink: 0 } as const;

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
      open={target !== null}
      title={`Edit DID — ${target?.did ?? ""}`}
      onClose={close}
      footer={
        <>
          <button className="btn btn-ghost btn-sm" onClick={close}>Cancel</button>
          <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={busy !== null}>
            {busy === "save" ? "Saving…" : "Save Changes"}
          </button>
        </>
      }
    >
      {error && <div className="error-banner">{error}</div>}

      <div style={{ ...sectionTitle, marginTop: 0 }}>Number</div>
      <div style={rowStyle}>
        <div style={labelStyle}>Address</div>
        <div style={{ fontFamily: "var(--mono)", color: "var(--text)" }}>{target?.did}</div>
      </div>
      <div style={rowStyle}>
        <div style={labelStyle}>Configuration</div>
        <div style={{ color: "var(--text)" }}>
          {config ? <>{config.name} <span style={{ color: "var(--text-3)" }}>· {config.providerLabel}</span></> : <i style={{ color: "var(--text-3)" }}>none (local SIP)</i>}
        </div>
      </div>
      <div style={rowStyle}>
        <div style={labelStyle}>Account</div>
        <div style={{ color: "var(--text)" }}>{target?.tenantName}</div>
      </div>
      {purchase && (
        <div style={rowStyle}>
          <div style={labelStyle}>Purchased</div>
          <div style={{ color: "var(--text)" }}>{new Date(purchase.purchased_at).toLocaleDateString()}</div>
        </div>
      )}

      <div style={sectionTitle}>Routing</div>
      <div className="form-group">
        <label className="form-label">Label <span className="hint">free text, e.g. a region or team name</span></label>
        <input className="form-input" value={region} onChange={(e) => setRegion(e.target.value)} />
      </div>
      <div className="form-group">
        <label className="form-label">Inbound agent</label>
        <select className="form-select" value={agentId} onChange={(e) => setAgentId(e.target.value)}>
          <option value="">— none (routes to default) —</option>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>{a.name}</option>
          ))}
        </select>
      </div>
      <div className="form-group">
        <label className="form-label">
          Fallback agent <span className="hint">used if the primary agent is unavailable or inactive</span>
        </label>
        <select className="form-select" value={fallbackAgentId} onChange={(e) => setFallbackAgentId(e.target.value)}>
          <option value="">— none —</option>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>{a.name}</option>
          ))}
        </select>
      </div>
      <div className="form-group">
        <label className="form-label">
          Status <span className="hint">inactive and suspended numbers don&apos;t take calls</span>
        </label>
        <select className="form-select" value={status} onChange={(e) => setStatus(e.target.value as PhoneNumber["status"])}>
          <option value="active">active</option>
          <option value="inactive">inactive</option>
          <option value="suspended">suspended</option>
        </select>
      </div>

      {syncsWithProvider && manualSetup && config && (
        <>
          <div style={sectionTitle}>{providerLabel} routing</div>
          <div className="form-hint" style={{ lineHeight: 1.6 }}>
            Set up once in {providerLabel}: add this number to your Voice Application and set the application&apos;s URL to{" "}
            <span style={{ fontFamily: "var(--mono)", color: "var(--text)", wordBreak: "break-all" }}>{webhookUrlFor(config)}</span> (POST).
          </div>
        </>
      )}

      {syncsWithProvider && !manualSetup && (
        <>
          <div style={sectionTitle}>{providerLabel} routing</div>
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

      <div style={{ ...sectionTitle, color: "var(--red)" }}>Danger zone</div>
      {mode === "local_locked" ? (
        <div className="form-hint" style={{ lineHeight: 1.6 }}>
          Local numbers are assigned and removed by the platform. To stop taking calls on it, set its status to inactive.
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "center", gap: 12, border: "1px solid var(--border)", borderRadius: 8, padding: "10px 12px" }}>
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
          ) : confirmRemove ? (
            <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
              <button className="btn btn-ghost btn-sm" onClick={() => setConfirmRemove(false)} disabled={busy !== null}>Keep</button>
              <button className="btn btn-danger btn-sm" onClick={() => handleRemove()} disabled={busy !== null || removeBlocked}>
                {busy === "remove" ? "Removing…" : `Yes, ${REMOVE_COPY[mode].button.toLowerCase()}`}
              </button>
            </div>
          ) : (
            <button className="btn btn-danger btn-sm" style={{ flexShrink: 0 }} onClick={() => setConfirmRemove(true)} disabled={busy !== null || removeBlocked}>
              {REMOVE_COPY[mode].button}
            </button>
          )}
        </div>
      )}
    </Modal>
  );
}
