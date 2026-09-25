"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  AgentWithTenant,
  ApiError,
  Carrier,
  CarrierProvider,
  deletePhoneNumber,
  listAllAgents,
  listAllPhoneNumbers,
  listCarriers,
  listTelephonyConfigs,
  PhoneNumberStatus,
  PhoneNumberWithTenant,
  Tenant,
  TelephonyConfig,
  updatePhoneNumber,
} from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";

const STATUSES: PhoneNumberStatus[] = ["active", "inactive", "suspended"];

const CARRIER_PROVIDER_LABEL: Record<CarrierProvider, string> = {
  twilio: "Twilio",
  plivo: "Plivo",
  vonage: "Vonage",
};
const TELEPHONY_PROVIDER_LABEL: Record<string, string> = { cloudonix: "Cloudonix", vobiz: "Vobiz" };

interface ProviderInfo {
  label: string;
  href: string;
}

const listAllProviderConfigs = async (
  tenants: Tenant[],
): Promise<{ carriers: Carrier[]; telephonyConfigs: TelephonyConfig[] }> => {
  const perTenant = await Promise.all(
    tenants.map(async (t) => {
      const [carriers, telephonyConfigs] = await Promise.all([listCarriers(t.id), listTelephonyConfigs(t.id)]);
      return { carriers, telephonyConfigs };
    }),
  );
  return {
    carriers: perTenant.flatMap((r) => r.carriers),
    telephonyConfigs: perTenant.flatMap((r) => r.telephonyConfigs),
  };
};

export default function PhoneNumbersPage() {
  const { allTenants, isAllTenants, tenant, loading: tenantLoading } = useActiveTenant();
  const targetTenants = useMemo(
    () => (isAllTenants ? allTenants : tenant ? [tenant] : []),
    [tenant, allTenants, isAllTenants],
  );
  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [numbers, setNumbers] = useState<PhoneNumberWithTenant[]>([]);
  const [carriers, setCarriers] = useState<Carrier[]>([]);
  const [telephonyConfigs, setTelephonyConfigs] = useState<TelephonyConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    if (tenantLoading || targetTenants.length === 0) return;
    setLoading(true);
    Promise.all([listAllPhoneNumbers(targetTenants), listAllAgents(targetTenants), listAllProviderConfigs(targetTenants)])
      .then(([nums, ags, providerConfigs]) => {
        setNumbers(nums);
        setAgents(ags);
        setCarriers(providerConfigs.carriers);
        setTelephonyConfigs(providerConfigs.telephonyConfigs);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(refresh, [targetTenants, tenantLoading]);

  const agentName = (id: string | null) => {
    if (!id) return <i style={{ color: "var(--text-3)" }}>none</i>;
    const a = agents.find((a) => a.id === id);
    return a ? a.name : id;
  };

  const providerFor = (n: PhoneNumberWithTenant): ProviderInfo | null => {
    if (n.carrier_id) {
      const c = carriers.find((c) => c.id === n.carrier_id);
      if (!c) return null;
      return { label: CARRIER_PROVIDER_LABEL[c.provider] ?? c.provider, href: `/telephony?config=carrier:${c.id}` };
    }
    if (n.telephony_config_id) {
      const tc = telephonyConfigs.find((tc) => tc.id === n.telephony_config_id);
      if (!tc) return null;
      return { label: TELEPHONY_PROVIDER_LABEL[tc.provider] ?? tc.provider, href: `/telephony?config=telephony_config:${tc.id}` };
    }
    return null;
  };

  const handleStatusChange = async (n: PhoneNumberWithTenant, status: PhoneNumberStatus) => {
    try {
      await updatePhoneNumber(n.id, { status });
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleRemove = async (n: PhoneNumberWithTenant) => {
    if (!confirm(`Remove DID ${n.did}?`)) return;
    try {
      await deletePhoneNumber(n.id);
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  return (
    <>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 14, gap: 10 }}>
        <div className="form-hint">To add a number, open its telephony configuration.</div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : numbers.length === 0 ? (
          <div className="empty-state">No phone numbers assigned yet.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>DID</th>
                <th>Account</th>
                <th>Agent</th>
                <th>Fallback Agent</th>
                <th>Status</th>
                <th>Region</th>
                <th>Provider</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {numbers.map((n) => {
                const provider = providerFor(n);
                return (
                  <tr key={n.id}>
                    <td className="bold mono" style={{ color: "var(--text)" }}>
                      {n.did}
                    </td>
                    <td>{n.tenantName}</td>
                    <td>{agentName(n.agent_id)}</td>
                    <td>{agentName(n.fallback_agent_id)}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <select
                        className={`form-select status-select ${n.status}`}
                        style={{ width: 110, padding: "3px 8px", fontSize: ".71rem" }}
                        value={n.status}
                        onChange={(e) => handleStatusChange(n, e.target.value as PhoneNumberStatus)}
                      >
                        {STATUSES.map((s) => (
                          <option key={s} value={s}>
                            {s}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>{n.region || "—"}</td>
                    <td>
                      {provider ? (
                        <Link href={provider.href} className="badge green">
                          {provider.label}
                        </Link>
                      ) : (
                        <span className="badge amber">unassigned</span>
                      )}
                    </td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <button
                        className="btn-icon"
                        style={{ color: "var(--text-2)", cursor: "pointer" }}
                        onClick={() => handleRemove(n)}
                        title="Remove DID"
                      >
                        ✕
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
