"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  Agent,
  ApiError,
  Call,
  Carrier,
  CarrierProvider,
  PhoneNumber,
  listAgents,
  listCalls,
  listCarriers,
  listPhoneNumbers,
} from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";

const PROVIDER_LABEL: Record<CarrierProvider, string> = {
  twilio: "Twilio",
  plivo: "Plivo",
  vonage: "Vonage",
};

// How many calls per account the recent-activity columns are computed over.
// There is no per-DID aggregate endpoint (services/config/calls.py exposes a
// paged list and the dashboard rollups, neither keyed by called_number), so
// the counts below are a window over the most recent calls — labelled as
// such everywhere they are shown rather than presented as an all-time total.
const CALL_WINDOW = 200;

const fmtInt = (n: number) => n.toLocaleString("en-IN");

type TrunkHealth = "healthy" | "degraded" | "standby";

const HEALTH_BADGE: Record<TrunkHealth, { label: string; cls: string }> = {
  healthy: { label: "Healthy", cls: "green" },
  degraded: { label: "Degraded", cls: "amber" },
  standby: { label: "Standby", cls: "gray" },
};

interface NumberRow extends PhoneNumber {
  tenantName: string;
  trunkName: string | null;
  routesTo: string;
  inbound: number;
  outbound: number;
}

interface TrunkRow {
  /** null for the synthetic "no trunk assigned" card. */
  carrier: Carrier | null;
  key: string;
  name: string;
  /** Grouping key for per-account totals. Tenant NAMES are not unique —
   *  nothing constrains two accounts from sharing one — so anything that
   *  aggregates per account keys on the id and only displays the name. */
  tenantId: string;
  tenantName: string;
  providerLabel: string;
  accountRef: string | null;
  dids: number;
  activeDids: number;
  suspendedDids: number;
  calls: number;
  health: TrunkHealth;
}

/** One trunk/carrier card: identity, health, and the three counts this
    platform can actually answer. Channel capacity, ASR and MOS are not on
    the carrier record and no telemetry feed writes them — the card states
    that once, below the grid, instead of drawing plausible numbers. */
function TrunkCard({ trunk, totalCalls, showTenant }: { trunk: TrunkRow; totalCalls: number; showTenant: boolean }) {
  const badge = HEALTH_BADGE[trunk.health];
  const sharePct = totalCalls > 0 ? (trunk.calls / totalCalls) * 100 : 0;
  return (
    <div className="card" style={{ padding: "14px 16px", flex: "1 1 260px", minWidth: 240 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <span
          style={{
            width: 7, height: 7, borderRadius: "50%", flexShrink: 0,
            background: trunk.health === "healthy" ? "var(--green)" : trunk.health === "degraded" ? "var(--amber)" : "var(--text-3)",
          }}
        />
        <span style={{ fontSize: ".85rem", fontWeight: 600, color: "var(--text)" }}>{trunk.name}</span>
        <span style={{ fontSize: ".7rem", color: "var(--text-3)" }}>· {trunk.providerLabel}</span>
      </div>
      <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 10 }}>
        <span className={`badge ${badge.cls}`}>{badge.label}</span>
        {showTenant && <span className="badge gray">{trunk.tenantName}</span>}
      </div>
      <div style={{ fontFamily: "var(--mono)", fontSize: ".68rem", color: "var(--text-3)", marginBottom: 12, wordBreak: "break-all" }}>
        {trunk.accountRef ?? "no account reference"}
      </div>
      <div style={{ display: "flex", gap: 18 }}>
        <Metric label="DIDs" value={fmtInt(trunk.dids)} />
        <Metric label="Active" value={fmtInt(trunk.activeDids)} />
        <Metric label="Calls" value={fmtInt(trunk.calls)} />
      </div>
      <div style={{ height: 3, borderRadius: 2, background: "var(--surf-3)", margin: "12px 0 8px" }}>
        {totalCalls > 0 && (
          <div style={{ width: `${Math.min(100, sharePct)}%`, height: "100%", borderRadius: 2, background: "var(--cyan)" }} />
        )}
      </div>
      <div style={{ fontSize: ".68rem", color: "var(--text-3)" }}>
        {totalCalls > 0
          ? `${sharePct.toFixed(0)}% of the account's recent calls matched this trunk's DIDs`
          : "no calls in the recent window"}
        {trunk.suspendedDids > 0 && ` · ${fmtInt(trunk.suspendedDids)} suspended DID${trunk.suspendedDids === 1 ? "" : "s"}`}
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

export default function TelephonyPage() {
  const { tenant, allTenants, isPlatformScoped, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const [trunks, setTrunks] = useState<TrunkRow[]>([]);
  const [numbers, setNumbers] = useState<NumberRow[]>([]);
  /** Calls fetched per account id — the denominator behind each trunk's
   *  share. Derived from the same window the per-DID counts come from, so
   *  the two can be compared; summing the DID counts instead would silently
   *  exclude every call whose numbers match no DID on file. */
  const [callsByTenant, setCallsByTenant] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  // Scoped to whatever the header switcher has selected, same as Agents and
  // IVR Flows — one account by default, every account under "All tenants".
  // Each account's fetch settles independently so one failing tenant leaves
  // the rest of the page intact (lesson 21).
  useEffect(() => {
    if (tenantLoading) return;
    const targets = isAllTenants ? allTenants : tenant ? [tenant] : [];
    if (targets.length === 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setTrunks([]);
      setNumbers([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    let cancelled = false;
    (async () => {
      const results = await Promise.allSettled(
        targets.map(async (t) => {
          const [carriers, phoneNumbers, agentList, calls] = await Promise.all([
            listCarriers(t.id),
            listPhoneNumbers(t.id),
            // Routing and recent activity are conveniences on this page, not
            // its subject: either failing degrades a column to "—" rather
            // than losing the trunk and DID inventory with it.
            listAgents(t.slug).then((a) => ({ ok: true, agents: a })).catch(() => ({ ok: false, agents: [] as Agent[] })),
            listCalls(t.slug, { limit: CALL_WINDOW }).then((r) => r.items).catch((): Call[] => []),
          ]);
          return { t, carriers, phoneNumbers, agents: agentList.agents, agentsOk: agentList.ok, calls };
        }),
      );
      if (cancelled) return;

      const trunkRows: TrunkRow[] = [];
      const numberRows: NumberRow[] = [];
      const callTotals: Record<string, number> = {};
      const errs: string[] = [];

      results.forEach((r, i) => {
        if (r.status !== "fulfilled") {
          errs.push(`${targets[i].name}: ${r.reason instanceof ApiError ? r.reason.detail : String(r.reason)}`);
          return;
        }
        const { t, carriers, phoneNumbers, agents, agentsOk, calls } = r.value;
        callTotals[t.id] = calls.length;
        const agentName = (id: string | null) => (id ? agents.find((a) => a.id === id)?.name ?? null : null);

        // A DID's traffic: inbound calls are the ones dialled TO it,
        // outbound the ones placed FROM it as caller id. Both numbers are
        // free-text columns written by the gateway, so they are compared
        // on their digits only — "+91 22 6844 0100" and "912268440100"
        // are the same DID and must not split into two rows of counts.
        const digits = (s: string | null) => (s ?? "").replace(/\D/g, "");
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
          const primary = agentName(n.agent_id);
          const fallback = agentName(n.fallback_agent_id);
          return {
            ...n,
            tenantName: t.name,
            trunkName: carrier?.name ?? null,
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

        const buildTrunk = (carrier: Carrier | null, dids: NumberRow[]): TrunkRow => {
          const suspended = dids.filter((d) => d.status === "suspended").length;
          const active = dids.filter((d) => d.status === "active").length;
          return {
            carrier,
            key: carrier?.id ?? `${t.id}:unassigned`,
            name: carrier?.name ?? "No trunk assigned",
            tenantId: t.id,
            tenantName: t.name,
            providerLabel: carrier ? PROVIDER_LABEL[carrier.provider] : "unrouted DIDs",
            accountRef: carrier?.carrier_account_ref ?? carrier?.auth_id ?? null,
            dids: dids.length,
            activeDids: active,
            suspendedDids: suspended,
            calls: dids.reduce((sum, d) => sum + d.inbound + d.outbound, 0),
            // Health is derived from what this console owns — the DIDs on
            // the trunk — not from carrier-side signalling, which nothing
            // reports here.
            health: dids.length === 0 ? "standby" : suspended > 0 ? "degraded" : active > 0 ? "healthy" : "standby",
          };
        };

        for (const c of carriers) {
          trunkRows.push(buildTrunk(c, rowsForTenant.filter((n) => n.carrier_id === c.id)));
        }
        // DIDs with no carrier still take calls — surface them as their own
        // card rather than letting them vanish from the trunk view.
        const orphans = rowsForTenant.filter((n) => !n.carrier_id || !carriers.some((c) => c.id === n.carrier_id));
        if (orphans.length > 0) trunkRows.push(buildTrunk(null, orphans));
      });

      setTrunks(trunkRows);
      setNumbers(numberRows);
      setCallsByTenant(callTotals);
      setError(errs.length > 0 ? errs.join("; ") : null);
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [tenant, allTenants, isAllTenants, tenantLoading]);

  const visibleNumbers = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = q
      ? numbers.filter((n) => `${n.did} ${n.routesTo} ${n.trunkName ?? ""} ${n.tenantName}`.toLowerCase().includes(q))
      : numbers;
    return [...matched].sort((a, b) => b.inbound + b.outbound - (a.inbound + a.outbound) || a.did.localeCompare(b.did));
  }, [numbers, search]);

  const accountLine = isAllTenants
    ? `SIP trunks, DID numbers and routing across ${allTenants.length} account${allTenants.length === 1 ? "" : "s"}.`
    : tenant
      ? `SIP trunks, DID numbers and routing for ${tenant.name}.${isPlatformScoped ? " Switch accounts from the header." : ""}`
      : "SIP trunks, DID numbers and routing.";

  const typeBadge = (n: NumberRow) => {
    if (n.inbound > 0 && n.outbound > 0) return <span className="badge cyan">Both</span>;
    if (n.inbound > 0) return <span className="badge green">Inbound</span>;
    if (n.outbound > 0) return <span className="badge amber">Outbound</span>;
    return <span className="badge gray">No traffic</span>;
  };

  return (
    <>
      {error && <div className="error-banner">{error}</div>}

      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
        <div>
          <h1 style={{ fontSize: "1.5rem", fontWeight: 600, letterSpacing: "-.025em", margin: 0, color: "var(--text)" }}>Telephony</h1>
          <div className="form-hint" style={{ marginTop: 4 }}>{accountLine}</div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Link href="/phone-numbers" className="btn btn-sm btn-ghost">Manage DIDs</Link>
          <Link href="/live-calls" className="btn btn-sm btn-primary">Live calls</Link>
        </div>
      </div>

      {loading ? (
        <div className="card">
          <div className="empty-state">Loading telephony…</div>
        </div>
      ) : (
        <>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginBottom: 8 }}>
            {trunks.length === 0 ? (
              <div className="card" style={{ flex: 1 }}>
                <div className="empty-state">
                  No carriers configured for this account yet — add one from an agent&apos;s SIP panel, then assign DIDs to it.
                </div>
              </div>
            ) : (
              trunks.map((t) => (
                <TrunkCard
                  key={t.key}
                  trunk={t}
                  totalCalls={callsByTenant[t.tenantId] ?? 0}
                  showTenant={isAllTenants}
                />
              ))
            )}
          </div>
          <div className="form-hint" style={{ marginBottom: 18 }}>
            Counts cover the most recent {fmtInt(CALL_WINDOW)} calls per account. Channel capacity, ASR and MOS need a
            carrier telemetry feed, which is not connected to this console yet.
          </div>

          <div className="card">
            <div className="card-hdr">
              <span className="card-title">Numbers</span>
              <span className="card-sub">
                {fmtInt(visibleNumbers.length)} of {fmtInt(numbers.length)} DID{numbers.length === 1 ? "" : "s"}
              </span>
              <input
                className="form-input"
                style={{ width: 200, marginLeft: 10 }}
                placeholder="Search numbers"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            {visibleNumbers.length === 0 ? (
              <div className="empty-state">
                {numbers.length === 0 ? "No DIDs assigned to this account yet." : `No numbers match "${search}".`}
              </div>
            ) : (
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Number</th>
                    <th>Type</th>
                    <th>Routes to</th>
                    <th>Trunk</th>
                    {isAllTenants && <th>Account</th>}
                    <th>Status</th>
                    <th style={{ textAlign: "right" }}>Calls (recent)</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleNumbers.map((n) => (
                    <tr key={n.id}>
                      <td className="bold mono" style={{ color: "var(--text)" }}>{n.did}</td>
                      <td>{typeBadge(n)}</td>
                      <td>{n.routesTo}</td>
                      <td>{n.trunkName ?? <i style={{ color: "var(--text-3)" }}>unassigned</i>}</td>
                      {isAllTenants && <td>{n.tenantName}</td>}
                      <td>
                        <span className={`badge ${n.status === "active" ? "green" : n.status === "suspended" ? "red" : "gray"}`}>
                          {n.status}
                        </span>
                      </td>
                      <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                        {fmtInt(n.inbound + n.outbound)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}
    </>
  );
}
