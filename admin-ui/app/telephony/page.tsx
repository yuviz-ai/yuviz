"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  Agent,
  ApiError,
  AvailableNumber,
  Call,
  Carrier,
  CarrierProvider,
  PhoneNumber,
  PhoneNumberCreate,
  PurchasedNumber,
  Tenant,
  TelephonyConfig,
  createCarrier,
  createPhoneNumber,
  createTelephonyConfig,
  listAgents,
  listCalls,
  listCarriers,
  listPhoneNumbers,
  listPurchasedNumbers,
  listTelephonyConfigs,
  purchaseNumber,
  releaseNumber,
  searchAvailableNumbers,
  updateCarrier,
  updatePhoneNumber,
  updateTelephonyConfig,
} from "@/lib/api";
import { Modal } from "@/components/Modal";
import { useActiveTenant } from "@/lib/useActiveTenant";

const CARRIER_PROVIDER_LABEL: Record<CarrierProvider, string> = {
  twilio: "Twilio",
  plivo: "Plivo",
  vonage: "Vonage",
};
const CARRIER_PROVIDERS: CarrierProvider[] = ["twilio", "plivo", "vonage"];
const TELEPHONY_PROVIDER_LABEL: Record<string, string> = { cloudonix: "Cloudonix", vobiz: "Vobiz" };
const TELEPHONY_PROVIDERS = ["cloudonix", "vobiz"] as const;
type NewConfigProvider = CarrierProvider | (typeof TELEPHONY_PROVIDERS)[number];

// How many calls per account the recent-activity columns are computed over.
// There is no per-DID aggregate endpoint (services/config/calls.py exposes a
// paged list and the dashboard rollups, neither keyed by called_number), so
// the counts below are a window over the most recent calls — labelled as
// such everywhere they are shown rather than presented as an all-time total.
const CALL_WINDOW = 200;

const fmtInt = (n: number) => n.toLocaleString("en-IN");
const digits = (s: string | null | undefined) => (s ?? "").replace(/\D/g, "");
const providerLabelFor = (kind: "carrier" | "telephony_config", provider: string) =>
  kind === "carrier" ? CARRIER_PROVIDER_LABEL[provider as CarrierProvider] ?? provider : TELEPHONY_PROVIDER_LABEL[provider] ?? provider;

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
  /** Calls fetched per account id — the denominator behind each config's
   *  share. Derived from the same window the per-DID counts come from, so
   *  the two can be compared; summing the DID counts instead would silently
   *  exclude every call whose numbers match no DID on file. */
  const [callsByTenant, setCallsByTenant] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const refresh = () => setReloadKey((k) => k + 1);

  const [addOpen, setAddOpen] = useState(false);

  // Scoped to whatever the header switcher has selected, same as Agents and
  // IVR Flows — one account by default, every account under "All tenants".
  // Each account's fetch settles independently so one failing tenant leaves
  // the rest of the page intact (lesson 21).
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
            // Routing and recent activity are conveniences on this page, not
            // its subject: either failing degrades a column to "—" rather
            // than losing the configuration and DID inventory with it.
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

        // A DID's traffic: inbound calls are the ones dialled TO it,
        // outbound the ones placed FROM it as caller id. Both numbers are
        // free-text columns written by the gateway, so they are compared
        // on their digits only — "+91 22 6844 0100" and "912268440100"
        // are the same DID and must not split into two rows of counts.
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
            // With the agent list unavailable, an assigned agent_id is a
            // name this page cannot resolve — "—", never "Account default
            // agent", which would assert routing that isn't configured.
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
            // telephony_configs (Vobiz/Cloudonix, served by services/telephony)
            // carry a real, Redis-backed health status (T20/T24) — the
            // DID-count heuristic below stays only for carriers, which have
            // no such probe reporting to Config Service.
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

  const selectedConfig = selectedKey ? configs.find((c) => c.key === `${selectedKey.kind}:${selectedKey.id}`) ?? null : null;

  const accountLine = isAllTenants
    ? `SIP trunks, DID numbers and routing across ${allTenants.length} account${allTenants.length === 1 ? "" : "s"}.`
    : tenant
      ? `SIP trunks, DID numbers and routing for ${tenant.name}.${isPlatformScoped ? " Switch accounts from the header." : ""}`
      : "SIP trunks, DID numbers and routing.";

  if (selectedKey && !loading) {
    return selectedConfig ? (
      <ConfigDetail
        config={selectedConfig}
        numbers={numbers.filter((n) => n.configKind === selectedConfig.kind && n.configId === selectedConfig.id)}
        tenant={allTenants.find((t) => t.id === selectedConfig.tenantId) ?? null}
        agents={agentsByTenant[selectedConfig.tenantId]?.agents ?? []}
        onBack={() => router.push("/telephony")}
        onChanged={refresh}
      />
    ) : (
      <div className="card">
        <div className="empty-state">
          Configuration not found — it may have been removed, or belongs to an account outside the current
          selection.
        </div>
        <button className="btn btn-ghost btn-sm" onClick={() => router.push("/telephony")} style={{ marginTop: 10 }}>
          Back to Configurations
        </button>
      </div>
    );
  }

  return (
    <>
      {error && <div className="error-banner">{error}</div>}

      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
        <div>
          <h1 style={{ fontSize: "1.5rem", fontWeight: 600, letterSpacing: "-.025em", margin: 0, color: "var(--text)" }}>Telephony</h1>
          <div className="form-hint" style={{ marginTop: 4 }}>{accountLine}</div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Link href="/phone-numbers" className="btn btn-sm btn-indigo">Manage DIDs</Link>
          <Link href="/live-calls" className="btn btn-sm btn-indigo">Live calls</Link>
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
              <span className="card-title">Configurations</span>
              <span className="card-sub" style={{ marginLeft: "auto" }}>
                {fmtInt(visibleConfigs.length)} of {fmtInt(configs.length)} configured
              </span>
              <input
                className="form-input"
                style={{ width: 200 }}
                placeholder="Search configurations"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <button
                className="btn btn-primary btn-sm"
                onClick={() => setAddOpen(true)}
                disabled={allTenants.length === 0}
              >
                + Add configuration
              </button>
            </div>

            {visibleConfigs.length === 0 ? (
              <div className="empty-state">
                {configs.length === 0
                  ? "No telephony configurations yet — add a carrier (Twilio/Plivo/Vonage) or a webhook provider (Cloudonix/Vobiz)."
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
        </>
      )}

      <AddConfigModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        allTenants={allTenants}
        defaultTenantId={tenant?.id ?? allTenants[0]?.id ?? ""}
        onCreated={() => {
          setAddOpen(false);
          refresh();
        }}
      />
    </>
  );
}

function AddConfigModal({
  open, onClose, allTenants, defaultTenantId, onCreated,
}: { open: boolean; onClose: () => void; allTenants: Tenant[]; defaultTenantId: string; onCreated: () => void }) {
  const [tenantId, setTenantId] = useState(defaultTenantId);
  const [provider, setProvider] = useState<NewConfigProvider>("twilio");
  const [name, setName] = useState("");
  const [authId, setAuthId] = useState("");
  const [authTokenRef, setAuthTokenRef] = useState("");
  const [carrierAccountRef, setCarrierAccountRef] = useState("");
  const [domain, setDomain] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [vobizAuthId, setVobizAuthId] = useState("");
  const [vobizAuthToken, setVobizAuthToken] = useState("");
  const [isDefaultOutbound, setIsDefaultOutbound] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setTenantId(defaultTenantId);
    setProvider("twilio");
    setName("");
    setAuthId("");
    setAuthTokenRef("");
    setCarrierAccountRef("");
    setDomain("");
    setApiKey("");
    setVobizAuthId("");
    setVobizAuthToken("");
    setIsDefaultOutbound(false);
    setFormError(null);
  }, [open, defaultTenantId]);

  const isCarrier = (CARRIER_PROVIDERS as string[]).includes(provider);

  const handleSubmit = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      if (isCarrier) {
        await createCarrier(tenantId, {
          name,
          provider: provider as CarrierProvider,
          auth_id: authId || undefined,
          auth_token_ref: authTokenRef || undefined,
          carrier_account_ref: carrierAccountRef || undefined,
        });
      } else if (provider === "cloudonix") {
        await createTelephonyConfig(tenantId, {
          name,
          provider: "cloudonix",
          credentials: { domain, api_keys: [apiKey] },
          is_default_outbound: isDefaultOutbound,
        });
      } else {
        await createTelephonyConfig(tenantId, {
          name,
          provider: "vobiz",
          credentials: { auth_id: vobizAuthId, auth_token: vobizAuthToken },
          is_default_outbound: isDefaultOutbound,
        });
      }
      onCreated();
    } catch (e) {
      setFormError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const canSubmit =
    !!name &&
    (isCarrier
      ? true
      : provider === "cloudonix"
        ? !!domain && !!apiKey
        : !!vobizAuthId && !!vobizAuthToken);

  const allProviders: NewConfigProvider[] = [...CARRIER_PROVIDERS, ...TELEPHONY_PROVIDERS];
  const providerLabel = (p: NewConfigProvider) =>
    (CARRIER_PROVIDER_LABEL as Record<string, string>)[p] ?? (TELEPHONY_PROVIDER_LABEL as Record<string, string>)[p] ?? p;
  const docsUrl: Record<string, string> = {
    twilio: "https://www.twilio.com/docs",
    plivo: "https://www.plivo.com/docs/",
    vonage: "https://developer.vonage.com/",
    cloudonix: "https://developers.cloudonix.com",
    // vobiz has no public docs — it's this platform's own internal provider, not a third-party product.
  };

  return (
    <Modal
      open={open}
      title="Add telephony configuration"
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary btn-sm" onClick={handleSubmit} disabled={submitting || !canSubmit}>
            {submitting ? "Adding…" : "Create"}
          </button>
        </>
      }
    >
      <div className="hint" style={{ display: "block", marginBottom: 14, lineHeight: 1.5 }}>
        Connect a telephony provider account. Phone numbers are added after the configuration is created.
      </div>

      {formError && <div className="error-banner">{formError}</div>}

      {allTenants.length > 1 && (
        <div className="form-group">
          <label className="form-label">Account</label>
          <select className="form-select" value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
            {allTenants.map((t) => (
              <option key={t.id} value={t.id}>{t.name}</option>
            ))}
          </select>
        </div>
      )}

      <div className="form-group">
        <label className="form-label">
          Name <span className="required">*</span>
        </label>
        <input className="form-input" value={name} onChange={(e) => setName(e.target.value)} />
      </div>

      <div className="form-group">
        <label className="form-label">Provider</label>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {allProviders.map((p) => (
            <button
              key={p}
              type="button"
              className={`btn btn-sm ${provider === p ? "btn-primary" : "btn-ghost"}`}
              onClick={() => setProvider(p)}
            >
              {providerLabel(p)}
            </button>
          ))}
        </div>
        {docsUrl[provider] && (
          <a href={docsUrl[provider]} target="_blank" rel="noreferrer" className="hint" style={{ display: "inline-block", marginTop: 6 }}>
            {providerLabel(provider)} docs ↗
          </a>
        )}
      </div>

      <div className="form-group">
        <label className="form-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <input
            type="checkbox"
            checked={isDefaultOutbound}
            disabled={isCarrier}
            onChange={(e) => setIsDefaultOutbound(e.target.checked)}
          />
          Set as default for outbound calls
        </label>
        <div
          style={{
            fontSize: ".7rem", color: "var(--text-3)", lineHeight: 1.5, marginTop: 6,
            padding: "8px 10px", borderLeft: "2px solid var(--cyan-border)", background: "var(--surf-2)",
          }}
        >
          {isCarrier ? (
            <>Carriers (Twilio/Plivo/Vonage) don&apos;t have a default-outbound column yet — this stays disabled
              until that&apos;s added, rather than silently accepting a setting that won&apos;t save.</>
          ) : (
            <>Used by test calls and campaigns when no specific configuration is selected.</>
          )}
        </div>
      </div>

      <div style={{ borderTop: "1px solid var(--border)", margin: "4px 0 14px" }} />

      {isCarrier && (
        <>
          <div className="form-group">
            <label className="form-label">
              Account SID / Auth ID <span className="hint">e.g. AC... for Twilio, Auth ID for Plivo</span>
            </label>
            <input className="form-input" value={authId} onChange={(e) => setAuthId(e.target.value)} />
          </div>
          <div className="form-group">
            <label className="form-label">
              Auth Token Reference <span className="hint">e.g. env:TWILIO_AUTH_TOKEN — never a raw secret</span>
            </label>
            <input
              className="form-input"
              style={{ fontFamily: "var(--mono)" }}
              value={authTokenRef}
              onChange={(e) => setAuthTokenRef(e.target.value)}
              placeholder="env:TWILIO_AUTH_TOKEN"
            />
          </div>
          <div className="form-group">
            <label className="form-label">Carrier Account Ref <span className="hint">optional</span></label>
            <input className="form-input" value={carrierAccountRef} onChange={(e) => setCarrierAccountRef(e.target.value)} />
          </div>
        </>
      )}

      {provider === "cloudonix" && (
        <>
          <div className="form-group">
            <label className="form-label">
              Domain <span className="required">*</span>
            </label>
            <input className="form-input" value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="myaccount.cloudonix.net" />
          </div>
          <div className="form-group">
            <label className="form-label">
              API Key <span className="required">*</span>
            </label>
            <input className="form-input" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
          </div>
        </>
      )}

      {provider === "vobiz" && (
        <>
          <div className="form-group">
            <label className="form-label">
              Auth ID <span className="required">*</span>
            </label>
            <input className="form-input" value={vobizAuthId} onChange={(e) => setVobizAuthId(e.target.value)} />
          </div>
          <div className="form-group">
            <label className="form-label">
              Auth Token <span className="required">*</span>
            </label>
            <input className="form-input" type="password" value={vobizAuthToken} onChange={(e) => setVobizAuthToken(e.target.value)} />
          </div>
        </>
      )}

      <div className="form-group">
        <div className="form-label" style={{ color: "var(--text-3)", fontWeight: 500 }}>Phone Numbers</div>
        <div className="hint" style={{ display: "block" }}>Managed separately, on the configuration page after it&apos;s created.</div>
      </div>
    </Modal>
  );
}

function webhookUrlFor(config: ConfigRow): string | null {
  if (config.kind !== "telephony_config") return null;
  if (config.provider === "cloudonix") {
    const base = process.env.NEXT_PUBLIC_CLOUDONIX_SERVICE_URL || "";
    return `${base}/cloudonix/voice/${config.id}`;
  }
  if (config.provider === "vobiz") {
    // Fixed per Vobiz account, not per-config — services/vobiz/app.py answers
    // every inbound call on this one path and resolves the DID from Redis.
    const base = process.env.NEXT_PUBLIC_VOBIZ_SERVICE_URL || "";
    return `${base}/vobiz/answer`;
  }
  return null;
}

function ConfigDetail({
  config, numbers, tenant, agents, onBack, onChanged,
}: {
  config: ConfigRow; numbers: NumberRow[]; tenant: Tenant | null; agents: Agent[];
  onBack: () => void; onChanged: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
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

  const handleRelease = async (n: NumberRow) => {
    const p = purchasedFor(n);
    if (!p) {
      setError(`No purchase record found for ${n.did} — nothing to release.`);
      return;
    }
    if (!window.confirm(`Release ${n.did} back to the carrier? This cannot be undone.`)) return;
    try {
      await releaseNumber(p.id);
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleStatusChange = async (n: NumberRow, status: PhoneNumber["status"]) => {
    try {
      await updatePhoneNumber(n.id, { status });
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const webhookUrl = webhookUrlFor(config);

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
        <button className="btn btn-ghost btn-sm" onClick={onBack}>← Configurations</button>
        <h1 style={{ fontSize: "1.3rem", fontWeight: 600, letterSpacing: "-.025em", margin: 0, color: "var(--text)" }}>
          {config.name}
        </h1>
        <span className="badge indigo">{config.providerLabel}</span>
        {config.isDefaultOutbound && <span className="badge amber">Default outbound</span>}
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-hdr">
          <span className="card-title">Credentials</span>
          <span className="card-sub">masked — use Edit to change</span>
          <button className="btn btn-ghost btn-sm" style={{ marginLeft: "auto" }} onClick={() => setEditing(true)}>
            Edit credentials
          </button>
        </div>
        <div className="card-body" style={{ padding: "0 16px" }}>
          <CredentialsRows config={config} webhookUrl={webhookUrl} />
        </div>
      </div>

      <div className="card">
        <div className="card-hdr">
          <span className="card-title">Phone numbers</span>
          <span className="card-sub">{fmtInt(numbers.length)} DID{numbers.length === 1 ? "" : "s"}</span>
          <button className="btn btn-primary btn-sm" style={{ marginLeft: "auto" }} onClick={() => setAddOpen(true)}>
            + Add phone number
          </button>
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
                    <td onClick={(e) => e.stopPropagation()}>
                      <select
                        className={`form-select status-select ${n.status}`}
                        style={{ width: 110, padding: "3px 8px", fontSize: ".71rem" }}
                        value={n.status}
                        onChange={(e) => handleStatusChange(n, e.target.value as PhoneNumber["status"])}
                      >
                        <option value="active">active</option>
                        <option value="inactive">inactive</option>
                        <option value="suspended">suspended</option>
                      </select>
                    </td>
                    <td>{n.routesTo}</td>
                    <td>{typeBadge(n)}</td>
                    <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                      {fmtInt(n.inbound + n.outbound)}
                    </td>
                    {config.kind === "carrier" && (
                      <td>{p ? new Date(p.purchased_at).toLocaleDateString() : "—"}</td>
                    )}
                    <td style={{ display: "flex", gap: 6 }}>
                      <button className="btn btn-ghost btn-sm" onClick={() => setEditTarget(n)}>Edit</button>
                      {config.kind === "carrier" && (
                        <button className="btn btn-danger btn-sm" onClick={() => handleRelease(n)}>Release</button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <EditCredentialsModal open={editing} onClose={() => setEditing(false)} config={config} onSaved={() => { setEditing(false); onChanged(); }} />

      {tenant && (
        <AddNumberModal
          open={addOpen}
          onClose={() => setAddOpen(false)}
          config={config}
          tenant={tenant}
          agents={agents}
          onCreated={() => { setAddOpen(false); onChanged(); }}
        />
      )}

      <EditNumberModal
        target={editTarget}
        agents={agents}
        onClose={() => setEditTarget(null)}
        onSaved={() => { setEditTarget(null); onChanged(); }}
      />
    </>
  );
}

function CredentialsRows({ config, webhookUrl }: { config: ConfigRow; webhookUrl: string | null }) {
  const rowStyle = { display: "flex", gap: 10, padding: "6px 0", borderBottom: "1px solid var(--border)", fontSize: ".82rem" } as const;
  const labelStyle = { color: "var(--text-3)", width: 190, flexShrink: 0 } as const;
  const valueStyle = { fontFamily: "var(--mono)", color: "var(--text)", wordBreak: "break-all" } as const;
  const mask = (v: string | null | undefined) => (v ? "•".repeat(Math.min(20, Math.max(8, v.length))) : "—");

  if (config.kind === "carrier" && config.carrier) {
    const c = config.carrier;
    return (
      <>
        <div style={rowStyle}><div style={labelStyle}>Configuration ID</div><div style={valueStyle}>{c.id}</div></div>
        <div style={rowStyle}><div style={labelStyle}>Provider</div><div style={valueStyle}>{config.providerLabel}</div></div>
        <div style={rowStyle}><div style={labelStyle}>Account SID / Auth ID</div><div style={valueStyle}>{c.auth_id ?? "—"}</div></div>
        <div style={rowStyle}><div style={labelStyle}>Auth Token</div><div style={valueStyle}>{mask(c.auth_token_ref)}</div></div>
        <div style={{ ...rowStyle, borderBottom: "none" }}>
          <div style={labelStyle}>Carrier Account Ref</div><div style={valueStyle}>{c.carrier_account_ref ?? "—"}</div>
        </div>
      </>
    );
  }
  if (config.kind === "telephony_config" && config.telephonyConfig) {
    const tc = config.telephonyConfig;
    const creds = tc.credentials as Record<string, unknown>;
    return (
      <>
        <div style={rowStyle}><div style={labelStyle}>Configuration ID</div><div style={valueStyle}>{tc.id}</div></div>
        <div style={rowStyle}><div style={labelStyle}>Provider</div><div style={valueStyle}>{config.providerLabel}</div></div>
        {tc.provider === "cloudonix" && (
          <>
            <div style={rowStyle}><div style={labelStyle}>Domain</div><div style={valueStyle}>{String(creds.domain ?? "—")}</div></div>
            <div style={rowStyle}><div style={labelStyle}>API Key</div><div style={valueStyle}>{mask(Array.isArray(creds.api_keys) ? String(creds.api_keys[0]) : null)}</div></div>
          </>
        )}
        {tc.provider === "vobiz" && (
          <>
            <div style={rowStyle}><div style={labelStyle}>Auth ID</div><div style={valueStyle}>{String(creds.auth_id ?? "—")}</div></div>
            <div style={rowStyle}><div style={labelStyle}>Auth Token</div><div style={valueStyle}>{mask(creds.auth_token as string | undefined)}</div></div>
          </>
        )}
        {webhookUrl && (
          <div style={{ ...rowStyle, borderBottom: "none", flexDirection: "column", gap: 4 }}>
            <div style={{ display: "flex", gap: 10 }}>
              <div style={labelStyle}>Inbound webhook URL</div><div style={valueStyle}>{webhookUrl}</div>
            </div>
            {webhookUrl.startsWith("/") && (
              <div style={{ fontSize: ".68rem", color: "var(--text-3)", marginLeft: 200 }}>
                Shown as a relative path — set{" "}
                {tc.provider === "cloudonix" ? "NEXT_PUBLIC_CLOUDONIX_SERVICE_URL" : "NEXT_PUBLIC_VOBIZ_SERVICE_URL"}{" "}
                to show the full, copyable URL.
              </div>
            )}
          </div>
        )}
      </>
    );
  }
  return null;
}

function EditCredentialsModal({
  open, onClose, config, onSaved,
}: { open: boolean; onClose: () => void; config: ConfigRow; onSaved: () => void }) {
  const [name, setName] = useState("");
  const [authId, setAuthId] = useState("");
  const [authTokenRef, setAuthTokenRef] = useState("");
  const [carrierAccountRef, setCarrierAccountRef] = useState("");
  const [domain, setDomain] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [vobizAuthId, setVobizAuthId] = useState("");
  const [vobizAuthToken, setVobizAuthToken] = useState("");
  const [isDefaultOutbound, setIsDefaultOutbound] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setFormError(null);
    if (config.kind === "carrier" && config.carrier) {
      setName(config.carrier.name);
      setAuthId(config.carrier.auth_id ?? "");
      setAuthTokenRef(config.carrier.auth_token_ref ?? "");
      setCarrierAccountRef(config.carrier.carrier_account_ref ?? "");
    } else if (config.kind === "telephony_config" && config.telephonyConfig) {
      const tc = config.telephonyConfig;
      const creds = tc.credentials as Record<string, unknown>;
      setName(tc.name);
      setIsDefaultOutbound(tc.is_default_outbound);
      if (tc.provider === "cloudonix") {
        setDomain(String(creds.domain ?? ""));
        setApiKey(Array.isArray(creds.api_keys) ? String(creds.api_keys[0]) : "");
      } else {
        setVobizAuthId(String(creds.auth_id ?? ""));
        setVobizAuthToken(String(creds.auth_token ?? ""));
      }
    }
  }, [open, config]);

  const handleSave = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      if (config.kind === "carrier") {
        await updateCarrier(config.id, {
          name,
          auth_id: authId || undefined,
          auth_token_ref: authTokenRef || undefined,
          carrier_account_ref: carrierAccountRef || undefined,
        });
      } else {
        const credentials = config.provider === "cloudonix"
          ? { domain, api_keys: [apiKey] }
          : { auth_id: vobizAuthId, auth_token: vobizAuthToken };
        await updateTelephonyConfig(config.id, { name, credentials, is_default_outbound: isDefaultOutbound });
      }
      onSaved();
    } catch (e) {
      setFormError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      title={`Edit Credentials — ${config.name}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={submitting || !name}>
            {submitting ? "Saving…" : "Save Changes"}
          </button>
        </>
      }
    >
      {formError && <div className="error-banner">{formError}</div>}
      <div className="form-group">
        <label className="form-label">Name <span className="required">*</span></label>
        <input className="form-input" value={name} onChange={(e) => setName(e.target.value)} />
      </div>

      {config.kind === "carrier" && (
        <>
          <div className="form-group">
            <label className="form-label">Account SID / Auth ID</label>
            <input className="form-input" value={authId} onChange={(e) => setAuthId(e.target.value)} />
          </div>
          <div className="form-group">
            <label className="form-label">Auth Token Reference</label>
            <input className="form-input" style={{ fontFamily: "var(--mono)" }} value={authTokenRef} onChange={(e) => setAuthTokenRef(e.target.value)} />
          </div>
          <div className="form-group">
            <label className="form-label">Carrier Account Ref</label>
            <input className="form-input" value={carrierAccountRef} onChange={(e) => setCarrierAccountRef(e.target.value)} />
          </div>
        </>
      )}

      {config.kind === "telephony_config" && config.provider === "cloudonix" && (
        <>
          <div className="form-group">
            <label className="form-label">Domain</label>
            <input className="form-input" value={domain} onChange={(e) => setDomain(e.target.value)} />
          </div>
          <div className="form-group">
            <label className="form-label">API Key</label>
            <input className="form-input" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
          </div>
        </>
      )}

      {config.kind === "telephony_config" && config.provider === "vobiz" && (
        <>
          <div className="form-group">
            <label className="form-label">Auth ID</label>
            <input className="form-input" value={vobizAuthId} onChange={(e) => setVobizAuthId(e.target.value)} />
          </div>
          <div className="form-group">
            <label className="form-label">Auth Token</label>
            <input className="form-input" type="password" value={vobizAuthToken} onChange={(e) => setVobizAuthToken(e.target.value)} />
          </div>
        </>
      )}

      {config.kind === "telephony_config" && (
        <div className="form-group">
          <label className="form-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input type="checkbox" checked={isDefaultOutbound} onChange={(e) => setIsDefaultOutbound(e.target.checked)} />
            Set as default for outbound calls
          </label>
        </div>
      )}
    </Modal>
  );
}

function AddNumberModal({
  open, onClose, config, tenant, agents, onCreated,
}: { open: boolean; onClose: () => void; config: ConfigRow; tenant: Tenant; agents: Agent[]; onCreated: () => void }) {
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
      await createPhoneNumber(tenant.id, body);
      onCreated();
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
      await createPhoneNumber(tenant.id, { did: phoneNumber, carrier_id: config.id, status: "active" });
      onCreated();
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
        PSTN numbers (E.164), SIP URIs (sip:user@host), and SIP extensions are all supported.
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

function EditNumberModal({
  target, agents, onClose, onSaved,
}: { target: NumberRow | null; agents: Agent[]; onClose: () => void; onSaved: () => void }) {
  const [agentId, setAgentId] = useState("");
  const [fallbackAgentId, setFallbackAgentId] = useState("");
  const [region, setRegion] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!target) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setAgentId(target.agent_id ?? "");
    setFallbackAgentId(target.fallback_agent_id ?? "");
    setRegion(target.region ?? "");
    setError(null);
  }, [target]);

  const handleSave = async () => {
    if (!target) return;
    setSubmitting(true);
    setError(null);
    try {
      await updatePhoneNumber(target.id, {
        agent_id: agentId || null,
        fallback_agent_id: fallbackAgentId || null,
        region: region || undefined,
      });
      onSaved();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={target !== null}
      title={`Edit DID — ${target?.did ?? ""}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={submitting}>
            {submitting ? "Saving…" : "Save Changes"}
          </button>
        </>
      }
    >
      {error && <div className="error-banner">{error}</div>}
      <div className="form-group">
        <label className="form-label">Label</label>
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
    </Modal>
  );
}
