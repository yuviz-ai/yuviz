"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, PhoneNumber, listPhoneNumbers } from "@/lib/api";

// Carrier/telephony-config CRUD and the Buy-a-Number flow that used to live
// here moved to the unified Telephony console (admin-ui/app/telephony/) —
// this panel is now read-only: which DID (if any) routes to this agent, and
// a link to the configuration that owns it. See that page for editing.
export function SipPanel({ tenantId, agentId }: { tenantId: string; agentId: string }) {
  const [numbers, setNumbers] = useState<PhoneNumber[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    listPhoneNumbers(tenantId)
      .then((n) => { if (!cancelled) setNumbers(n); })
      .catch((e) => { if (!cancelled) setError(e instanceof ApiError ? e.detail : String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [tenantId, agentId]);

  if (loading) return <div className="empty-state">Loading…</div>;

  const assigned = numbers.filter((n) => n.agent_id === agentId);

  return (
    <div className="card">
      <div className="card-hdr">
        <div className="card-title">Assigned Number</div>
        <div className="card-sub">which DID routes calls to this agent</div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {assigned.length === 0 ? (
        <div className="empty-state">
          No number assigned to this agent yet — assign one from its telephony configuration.
        </div>
      ) : (
        assigned.map((n) => {
          const configKey = n.carrier_id
            ? `carrier:${n.carrier_id}`
            : n.telephony_config_id
              ? `telephony_config:${n.telephony_config_id}`
              : null;
          return (
            <div key={n.id} className="kb-row">
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 500 }}>{n.did}</div>
                <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>Registered · {n.status}</div>
              </div>
              <span className={`badge ${n.status === "active" ? "green" : n.status === "suspended" ? "red" : "gray"}`}>
                {n.status}
              </span>
              {configKey ? (
                <Link href={`/telephony?config=${configKey}`} className={`badge ${n.status === "active" ? "green" : n.status === "suspended" ? "red" : "gray"}`}>
                  View configuration
                </Link>
              ) : (
                <Link href="/telephony" className="badge amber">
                  Not linked to a provider — assign one
                </Link>
              )}
            </div>
          );
        })
      )}
    </div>
  );
}
