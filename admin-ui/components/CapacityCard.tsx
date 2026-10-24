"use client";

import { useEffect, useState } from "react";
import {
  ApiError,
  CapacityUsage,
  getCapacityUsage,
  getPlatformLimits,
  listTenantCaps,
  PlatformLimits,
  TenantCap,
  updateCpsLimit,
  updatePlatformLimits,
  updateTenantCap,
} from "@/lib/api";

const errText = (e: unknown) => (e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));

function toInt(draft: string): number {
  if (!/^\d+$/.test(draft.trim())) throw new Error("Enter a whole number.");
  return Number(draft);
}

// Blank means "no limit" (null); anything else must be a whole number, so a typo can never clear a limit.
const toCap = (draft: string): number | null => (draft.trim() === "" ? null : toInt(draft));

function PlatformLimitsForm({ limits, onSaved }: { limits: PlatformLimits; onSaved: (l: PlatformLimits) => void }) {
  const [total, setTotal] = useState(String(limits.total_call_capacity ?? ""));
  const [reserve, setReserve] = useState(String(limits.inbound_reserve));
  const [outbound, setOutbound] = useState(String(limits.max_outbound_concurrent_calls ?? ""));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      onSaved(
        await updatePlatformLimits({
          total_call_capacity: toCap(total),
          inbound_reserve: toInt(reserve),
          max_outbound_concurrent_calls: toCap(outbound),
        }),
      );
    } catch (e) {
      setError(errText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <div className="form-group" style={{ flex: 1 }}>
          <label className="form-label">Total call capacity <span className="hint">blank = no limit</span></label>
          <input className="form-input" inputMode="numeric" value={total} onChange={(e) => setTotal(e.target.value)} />
        </div>
        <div className="form-group" style={{ flex: 1 }}>
          <label className="form-label">Inbound reserve</label>
          <input className="form-input" inputMode="numeric" value={reserve} onChange={(e) => setReserve(e.target.value)} />
        </div>
        <div className="form-group" style={{ flex: 1 }}>
          <label className="form-label">Max outbound calls <span className="hint">blank = no limit</span></label>
          <input className="form-input" inputMode="numeric" value={outbound} onChange={(e) => setOutbound(e.target.value)} />
        </div>
      </div>
      {error && <div className="error-banner" style={{ marginBottom: 8 }}>{error}</div>}
      <button className="btn btn-primary btn-sm" disabled={saving} onClick={save}>
        {saving ? "Saving…" : "Save platform limits"}
      </button>
    </div>
  );
}

function CpsRow({ scope, cps, sends, onSaved }: { scope: string; cps: number | null; sends: number; onSaved: () => void }) {
  const [draft, setDraft] = useState(String(cps ?? ""));
  const [stored, setStored] = useState(cps);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const updated = await updateCpsLimit(scope, toInt(draft));
      setStored(updated.cps);
      onSaved();
    } catch (e) {
      setError(errText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <tr>
      <td className="mono">{scope}</td>
      <td>{stored ?? "no limit"}</td>
      <td>{sends}</td>
      <td>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input className="form-input" style={{ width: 80 }} inputMode="numeric" value={draft} onChange={(e) => setDraft(e.target.value)} />
          <button className="btn btn-ghost btn-sm" disabled={saving} onClick={save}>Save</button>
        </div>
        {error && <div className="form-hint" style={{ color: "var(--red)" }}>{error}</div>}
      </td>
    </tr>
  );
}

function TenantCapRow({ tenant }: { tenant: TenantCap }) {
  const [stored, setStored] = useState(tenant.max_outbound_concurrent_calls);
  const [draft, setDraft] = useState(String(tenant.max_outbound_concurrent_calls ?? ""));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const updated = await updateTenantCap(tenant.tenant_id, toCap(draft));
      setStored(updated.max_outbound_concurrent_calls);
    } catch (e) {
      setError(errText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <tr>
      <td>{tenant.name}</td>
      <td>{stored ?? "no cap"}</td>
      <td>{tenant.in_flight}</td>
      <td>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input
            className="form-input"
            style={{ width: 80 }}
            inputMode="numeric"
            placeholder="no cap"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <button className="btn btn-ghost btn-sm" disabled={saving} onClick={save}>Save</button>
        </div>
        {error && <div className="form-hint" style={{ color: "var(--red)" }}>{error}</div>}
      </td>
    </tr>
  );
}

export function CapacityCard() {
  const [limits, setLimits] = useState<PlatformLimits | null>(null);
  const [limitsError, setLimitsError] = useState<string | null>(null);
  const [usage, setUsage] = useState<CapacityUsage | null>(null);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [tenants, setTenants] = useState<TenantCap[] | null>(null);
  const [tenantsError, setTenantsError] = useState<string | null>(null);

  useEffect(() => {
    getPlatformLimits().then(setLimits).catch((e) => setLimitsError(errText(e)));
    listTenantCaps().then(setTenants).catch((e) => setTenantsError(errText(e)));
  }, []);

  useEffect(() => {
    const tick = () => {
      if (document.hidden) return;
      getCapacityUsage().then(setUsage).catch((e) => setUsageError(errText(e)));
    };
    tick();
    const timer = setInterval(tick, 10_000);
    return () => clearInterval(timer);
  }, []);

  const refreshUsage = () => getCapacityUsage().then(setUsage).catch((e) => setUsageError(errText(e)));
  const sendsFor = (scope: string) => usage?.cps.find((c) => c.scope === scope)?.sends_last_second ?? 0;
  const cpsScopes = [...new Set([...(limits?.cps_limits ?? []).map((c) => c.scope), ...(usage?.cps ?? []).map((c) => c.scope)])].sort();
  const cpsFor = (scope: string) => limits?.cps_limits.find((c) => c.scope === scope)?.cps ?? null;

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="card-hdr">
        <div className="card-title">Outbound capacity</div>
        <div className="card-sub">
          {usage ? `${usage.platform_in_flight} outbound call${usage.platform_in_flight === 1 ? "" : "s"} in flight` : ""}
        </div>
      </div>
      {limitsError && <div className="error-banner" style={{ margin: 16 }}>{limitsError}</div>}
      {limits && <PlatformLimitsForm limits={limits} onSaved={setLimits} />}

      <div style={{ padding: "0 16px 16px" }}>
        <div className="card-title" style={{ marginBottom: 6 }}>Calls per second</div>
        {usageError && <div className="form-hint" style={{ color: "var(--red)" }}>{usageError}</div>}
        {cpsScopes.length === 0 ? (
          <div className="form-hint">No CPS scopes yet; a scope appears here after its first send.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr><th>Scope</th><th>Limit</th><th>Sent last second</th><th>Set limit</th></tr>
            </thead>
            <tbody>
              {cpsScopes.map((scope) => (
                <CpsRow key={`${scope}:${cpsFor(scope)}`} scope={scope} cps={cpsFor(scope)} sends={sendsFor(scope)} onSaved={refreshUsage} />
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div style={{ padding: "0 16px 16px" }}>
        <div className="card-title" style={{ marginBottom: 6 }}>Per-tenant outbound cap</div>
        {tenantsError && <div className="error-banner">{tenantsError}</div>}
        {tenants && (
          <table className="tbl">
            <thead>
              <tr><th>Tenant</th><th>Cap</th><th>In flight</th><th>Set cap</th></tr>
            </thead>
            <tbody>
              {tenants.map((t) => <TenantCapRow key={t.tenant_id} tenant={t} />)}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
