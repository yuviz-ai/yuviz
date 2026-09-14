"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  addDncNumber,
  AgentWithTenant,
  ApiError,
  CampaignProgress,
  CampaignStatus,
  CampaignWithTenant,
  DncNumber,
  getCampaignProgress,
  listAllAgents,
  listAllCampaigns,
  listDncNumbers,
  listTenants,
  removeDncNumber,
  Tenant,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

// STATE tabs shown in the mockup order. "draft" is labelled "Scheduled"
// here — a draft campaign has no caller_id/contacts requirement yet met
// and simply hasn't been started, which is what "scheduled" means to an
// operator; the underlying status value is unchanged.
const STATUS_TABS: { label: string; value: CampaignStatus }[] = [
  { label: "Running", value: "running" },
  { label: "Scheduled", value: "draft" },
  { label: "Paused", value: "paused" },
  { label: "Completed", value: "completed" },
];

function statusBadgeClass(status: CampaignStatus): string {
  switch (status) {
    case "running":
      return "green";
    case "paused":
      return "amber";
    case "completed":
      return "cyan";
    default:
      return "gray";
  }
}

// Connect % / Intent % have no backing data anywhere in this codebase — no
// disposition classification, no connect-vs-no-answer distinction beyond
// what PROGRESS already shows, no intent detection. Mocked deterministically
// per campaign id (not Math.random()) so the numbers don't jump on every
// refresh, and clearly labelled — never presented as real telemetry.
function mockPercent(seed: string, salt: number): number {
  let h = salt;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return 20 + (h % 60); // 20–79%, avoids implausible 0%/100% extremes
}

export default function CampaignsPage() {
  const router = useRouter();
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [campaigns, setCampaigns] = useState<CampaignWithTenant[]>([]);
  const [progressById, setProgressById] = useState<Record<string, CampaignProgress>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusTab, setStatusTab] = useState<CampaignStatus>("running");
  const [search, setSearch] = useState("");

  const [dncModalOpen, setDncModalOpen] = useState(false);
  const [dncTenantId, setDncTenantId] = useState("");
  const [dncNumbers, setDncNumbers] = useState<DncNumber[]>([]);
  const [dncLoading, setDncLoading] = useState(false);
  const [dncError, setDncError] = useState<string | null>(null);
  const [dncPhone, setDncPhone] = useState("");
  const [dncReason, setDncReason] = useState("");
  const [dncSubmitting, setDncSubmitting] = useState(false);

  useEffect(() => {
    listTenants().then(setTenants);
  }, []);

  const refresh = () => {
    if (tenants.length === 0) return;
    setLoading(true);
    Promise.all([listAllCampaigns(tenants), listAllAgents(tenants)])
      .then(async ([cs, ags]) => {
        setCampaigns(cs);
        setAgents(ags);
        const entries = await Promise.all(
          cs.map(async (c) => [c.id, await getCampaignProgress(c.id)] as const),
        );
        setProgressById(Object.fromEntries(entries));
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(refresh, [tenants]);

  const agentName = (id: string) => agents.find((a) => a.id === id)?.name || id;

  const statusCounts = STATUS_TABS.reduce<Record<string, number>>((acc, tab) => {
    acc[tab.value] = campaigns.filter((c) => c.status === tab.value).length;
    return acc;
  }, {});

  const visibleCampaigns = campaigns
    .filter((c) => c.status === statusTab)
    .filter((c) => {
      const q = search.trim().toLowerCase();
      if (!q) return true;
      return c.name.toLowerCase().includes(q) || c.tenantName.toLowerCase().includes(q) || agentName(c.agent_id).toLowerCase().includes(q);
    });

  const openDncModal = () => {
    const tenantId = dncTenantId || tenants[0]?.id || "";
    setDncTenantId(tenantId);
    setDncModalOpen(true);
    setDncError(null);
    setDncPhone("");
    setDncReason("");
    if (tenantId) loadDncNumbers(tenantId);
  };

  const loadDncNumbers = (tenantId: string) => {
    setDncLoading(true);
    listDncNumbers(tenantId)
      .then(setDncNumbers)
      .catch((e) => setDncError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setDncLoading(false));
  };

  const handleAddDnc = async () => {
    if (!dncPhone.trim()) return;
    setDncSubmitting(true);
    setDncError(null);
    try {
      await addDncNumber(dncTenantId, dncPhone.trim(), dncReason.trim() || undefined);
      setDncPhone("");
      setDncReason("");
      loadDncNumbers(dncTenantId);
    } catch (e) {
      setDncError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setDncSubmitting(false);
    }
  };

  const handleRemoveDnc = async (id: string) => {
    try {
      await removeDncNumber(id);
      loadDncNumbers(dncTenantId);
    } catch (e) {
      setDncError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20, gap: 12, flexWrap: "wrap" }}>
        <div>
          <h1 style={{ fontSize: "1.6rem", fontWeight: 700, color: "var(--text)", margin: 0 }}>OBD campaigns</h1>
          <div style={{ fontSize: ".82rem", color: "var(--text-2)", marginTop: 4 }}>
            Outbound dialing with per-campaign pacing, retry ladders and DNC scrubbing.
          </div>
        </div>
        <Link href="/campaigns/new" className="btn btn-primary btn-sm" style={{ padding: "8px 16px" }}>
          New campaign
        </Link>
      </div>

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14, gap: 10, flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 6 }}>
          {STATUS_TABS.map((tab) => (
            <button
              key={tab.value}
              className="btn btn-sm"
              style={
                statusTab === tab.value
                  ? { background: "var(--cyan)", color: "#fff", borderColor: "var(--cyan)" }
                  : { background: "var(--surf)", color: "var(--text-2)", borderColor: "var(--border-2)" }
              }
              onClick={() => setStatusTab(tab.value)}
            >
              {tab.label} <span style={{ opacity: 0.7 }}>{statusCounts[tab.value] || 0}</span>
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            className="form-input"
            style={{ width: 220 }}
            placeholder="Search campaigns…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button className="btn btn-ghost btn-sm" onClick={openDncModal} disabled={tenants.length === 0}>
            Do-Not-Call List
          </button>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : visibleCampaigns.length === 0 ? (
          <div className="empty-state">
            {campaigns.length === 0 ? "No outbound campaigns yet." : "No campaigns match this tab or search."}
          </div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Campaign</th>
                <th>AI Agent</th>
                <th>Pacing</th>
                <th>Progress</th>
                <th title="No connect/intent analytics wired up yet — sample data">
                  Connect % <span className="badge gray" style={{ marginLeft: 4 }}>sample</span>
                </th>
                <th title="No connect/intent analytics wired up yet — sample data">
                  Intent % <span className="badge gray" style={{ marginLeft: 4 }}>sample</span>
                </th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {visibleCampaigns.map((c) => {
                const p = progressById[c.id];
                const pct = p && p.total > 0 ? Math.round((p.completed / p.total) * 100) : 0;
                return (
                  <tr key={c.id} onClick={() => router.push(`/campaigns/${c.id}`)} style={{ cursor: "pointer" }}>
                    <td>
                      <div className="bold" style={{ color: "var(--text)" }}>{c.name}</div>
                      <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                        {p ? `${p.total.toLocaleString()} contacts` : "…"} · created{" "}
                        {new Date(c.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                      </div>
                    </td>
                    <td>{agentName(c.agent_id)}</td>
                    <td>
                      <div>{c.max_concurrent_calls} concurrent</div>
                      <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>{c.pacing_seconds}s gap</div>
                    </td>
                    <td style={{ minWidth: 130 }}>
                      {p ? (
                        <>
                          <div style={{ height: 6, borderRadius: 4, background: "var(--surf-3)", overflow: "hidden", marginBottom: 4 }}>
                            <div style={{ height: "100%", width: `${pct}%`, background: "var(--green)", borderRadius: 4 }} />
                          </div>
                          <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                            {p.completed.toLocaleString()} / {p.total.toLocaleString()}
                          </div>
                        </>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td style={{ color: "var(--green)", fontWeight: 600 }}>{mockPercent(c.id, 17)}%</td>
                    <td style={{ color: "var(--green)", fontWeight: 600 }}>{mockPercent(c.id, 41)}%</td>
                    <td>
                      <span className={`badge ${statusBadgeClass(c.status)}`}>{c.status}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <Modal open={dncModalOpen} title="Do-Not-Call List" onClose={() => setDncModalOpen(false)}>
        {dncError && <div className="error-banner">{dncError}</div>}
        <div className="form-group">
          <label className="form-label">Account</label>
          <select
            className="form-select"
            value={dncTenantId}
            onChange={(e) => {
              setDncTenantId(e.target.value);
              loadDncNumbers(e.target.value);
            }}
          >
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </div>

        <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
          <input
            className="form-input"
            style={{ flex: 1, fontFamily: "var(--mono)" }}
            placeholder="Phone number"
            value={dncPhone}
            onChange={(e) => setDncPhone(e.target.value)}
          />
          <input
            className="form-input"
            style={{ flex: 1 }}
            placeholder="Reason (optional)"
            value={dncReason}
            onChange={(e) => setDncReason(e.target.value)}
          />
          <button className="btn btn-primary btn-sm" onClick={handleAddDnc} disabled={dncSubmitting || !dncPhone.trim()}>
            {dncSubmitting ? "Adding…" : "Add"}
          </button>
        </div>

        {dncLoading ? (
          <div className="empty-state">Loading…</div>
        ) : dncNumbers.length === 0 ? (
          <div className="empty-state">No numbers on this account&apos;s do-not-call list.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Phone</th>
                <th>Reason</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {dncNumbers.map((d) => (
                <tr key={d.id}>
                  <td className="mono">{d.phone_number}</td>
                  <td>{d.reason || "—"}</td>
                  <td>
                    <button className="btn btn-danger btn-sm" onClick={() => handleRemoveDnc(d.id)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Modal>
    </>
  );
}
