"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { CirclePlay, ListChecks, Megaphone, Phone, Search } from "lucide-react";
import {
  addDncNumber,
  AgentWithTenant,
  ApiError,
  Campaign,
  CampaignProgress,
  CampaignStatus,
  CampaignWithTenant,
  DncNumber,
  getCampaignProgress,
  listAllAgents,
  listAllCampaigns,
  listDncNumbers,
  pauseCampaign,
  removeDncNumber,
  resumeCampaign,
  startCampaign,
} from "@/lib/api";
import { rateFromPacing } from "@/components/campaigns/CampaignDialingFields";
import { CreateCampaignModal } from "@/components/campaigns/CreateCampaignModal";
import { Modal } from "@/components/Modal";
import { useActiveTenant } from "@/lib/useActiveTenant";

const STATUS_TABS: { label: string; value: CampaignStatus | "all" }[] = [
  { label: "All status", value: "all" },
  { label: "Draft", value: "draft" },
  { label: "Scheduled", value: "scheduled" },
  { label: "Running", value: "running" },
  { label: "Paused", value: "paused" },
  { label: "Completed", value: "completed" },
  { label: "Cancelled", value: "cancelled" },
];

// One width for every row action, sized for the longest label ("Start now").
const ACTION_BUTTON = { minWidth: 92, justifyContent: "center" } as const;

function KpiIcon({ children, tone }: { children: React.ReactNode; tone: string }) {
  return (
    <span
      aria-hidden="true"
      style={{
        position: "absolute", top: 12, right: 12, width: 28, height: 28, borderRadius: 8,
        display: "grid", placeItems: "center", color: tone,
        background: `color-mix(in srgb, ${tone} 12%, transparent)`,
      }}
    >
      {children}
    </span>
  );
}

function statusBadgeClass(status: CampaignStatus): string {
  switch (status) {
    case "running":
      return "green";
    case "paused":
      return "amber";
    case "completed":
    case "scheduled":
      return "cyan";
    case "cancelled":
      return "red";
    default:
      return "gray";
  }
}

const OUTCOMES: { key: keyof CampaignProgress; label: string }[] = [
  { key: "completed", label: "completed" },
  { key: "no_answer", label: "no answer" },
  { key: "busy", label: "busy" },
  { key: "declined", label: "declined" },
  { key: "failed", label: "failed" },
  { key: "blocked", label: "blocked" },
];

const dialled = (p: CampaignProgress) => p.completed + p.failed + p.no_answer + p.busy + p.declined + p.calling;
const fmtDate = (iso: string) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

export default function CampaignsPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { tenant, allTenants, isAllTenants, loading: tenantLoading } = useActiveTenant();
  // Campaign list follows the header switcher; the DNC modal deliberately uses allTenants.
  const targetTenants = useMemo(
    () => (isAllTenants ? allTenants : tenant ? [tenant] : []),
    [tenant, allTenants, isAllTenants],
  );
  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [campaigns, setCampaigns] = useState<CampaignWithTenant[]>([]);
  const [progressById, setProgressById] = useState<Record<string, CampaignProgress>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusTab, setStatusTab] = useState<CampaignStatus | "all">("all");
  const [actionId, setActionId] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const [dncModalOpen, setDncModalOpen] = useState(false);
  const [dncTenantId, setDncTenantId] = useState("");
  const [dncNumbers, setDncNumbers] = useState<DncNumber[]>([]);
  const [dncLoading, setDncLoading] = useState(false);
  const [dncError, setDncError] = useState<string | null>(null);
  const [dncPhone, setDncPhone] = useState("");
  const [dncReason, setDncReason] = useState("");
  const [dncSubmitting, setDncSubmitting] = useState(false);

  const refresh = () => {
    if (tenantLoading || targetTenants.length === 0) return;
    setLoading(true);
    Promise.all([listAllCampaigns(targetTenants), listAllAgents(targetTenants)])
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
  useEffect(refresh, [targetTenants, tenantLoading]);

  const agentName = (id: string) => agents.find((a) => a.id === id)?.name || id;

  const statusCounts = STATUS_TABS.reduce<Record<string, number>>((acc, tab) => {
    acc[tab.value] = tab.value === "all" ? campaigns.length : campaigns.filter((c) => c.status === tab.value).length;
    return acc;
  }, {});

  const progressList = Object.values(progressById);
  const totalCalls = progressList.reduce((sum, p) => sum + dialled(p), 0);
  const outcomeTotals = OUTCOMES.map((o) => ({
    ...o,
    count: progressList.reduce((sum, p) => sum + (p[o.key] as number), 0),
  }));

  // A start that fails readiness opens the campaign, whose readiness panel says why.
  const runAction = async (c: Campaign, action: (id: string) => Promise<Campaign>) => {
    setActionId(c.id);
    try {
      const updated = await action(c.id);
      setCampaigns((cs) => cs.map((x) => (x.id === c.id ? { ...x, ...updated } : x)));
    } catch {
      router.push(`/campaigns/${c.id}`);
    } finally {
      setActionId(null);
    }
  };

  const visibleCampaigns = campaigns
    .filter((c) => statusTab === "all" || c.status === statusTab)
    .filter((c) => {
      const q = search.trim().toLowerCase();
      if (!q) return true;
      return c.name.toLowerCase().includes(q) || c.tenantName.toLowerCase().includes(q) || agentName(c.agent_id).toLowerCase().includes(q);
    });

  const openDncModal = () => {
    const tenantId = dncTenantId || allTenants[0]?.id || "";
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
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, marginBottom: 20, flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
          <Megaphone size={22} aria-hidden="true" style={{ color: "var(--cyan)", marginTop: 6 }} />
          <div>
            <h1 style={{ fontSize: "1.6rem", fontWeight: 700, color: "var(--text)", margin: 0 }}>Campaigns</h1>
            <div style={{ fontSize: ".82rem", color: "var(--text-2)", marginTop: 4 }}>
              Manage and monitor your lead outreach campaigns.
            </div>
          </div>
        </div>
      </div>

      <div className="dash">
        <div className="d-row r4">
          <div className="d-card" style={{ position: "relative" }}>
            <KpiIcon tone="var(--cyan)"><Megaphone size={14} /></KpiIcon>
            <div className="kpi-l">Total campaigns</div>
            <div className="kpi-v">{loading ? "—" : campaigns.length.toLocaleString()}</div>
          </div>
          <div className="d-card" style={{ position: "relative" }}>
            <KpiIcon tone="var(--green)"><CirclePlay size={14} /></KpiIcon>
            <div className="kpi-l">Active</div>
            <div className="kpi-v">{loading ? "—" : (statusCounts.running || 0).toLocaleString()}</div>
            <div className="kpi-d muted">Currently running</div>
          </div>
          <div className="d-card" style={{ position: "relative" }}>
            <KpiIcon tone="var(--chart-inbound, var(--cyan))"><Phone size={14} /></KpiIcon>
            <div className="kpi-l">Total calls</div>
            <div className="kpi-v">{loading ? "—" : totalCalls.toLocaleString()}</div>
            <div className="kpi-d muted">Across all campaigns</div>
          </div>
          <div className="d-card" style={{ position: "relative" }}>
            <KpiIcon tone="var(--amber)"><ListChecks size={14} /></KpiIcon>
            <div className="kpi-l">Call outcomes</div>
            <div className="kpi-v">{loading ? "—" : outcomeTotals[0].count.toLocaleString()}</div>
            <div className="kpi-d muted">
              {outcomeTotals.slice(0, 1).map((o) => o.label)}
              {outcomeTotals.slice(1).filter((o) => o.count > 0).map((o) => ` · ${o.count.toLocaleString()} ${o.label}`)}
            </div>
          </div>
        </div>
      </div>

      <div className="card" style={{ padding: 12, marginBottom: 14, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <label className="phone-numbers-search">
          <Search size={14} aria-hidden="true" />
          <input type="search" aria-label="Search campaigns" placeholder="Search campaigns…" value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
        <select
          aria-label="Filter by status"
          className="form-select"
          style={{ width: 190 }}
          value={statusTab}
          onChange={(e) => setStatusTab(e.target.value as CampaignStatus | "all")}
        >
          {STATUS_TABS.map((tab) => (
            <option key={tab.value} value={tab.value}>
              {tab.label} ({statusCounts[tab.value] || 0})
            </option>
          ))}
        </select>
        <button className="btn btn-ghost btn-sm" style={{ marginLeft: "auto" }} onClick={openDncModal} disabled={allTenants.length === 0}>
          Do-Not-Call List
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card" style={{ overflowX: "auto" }}>
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : visibleCampaigns.length === 0 ? (
          <div className="empty-state">
            {campaigns.length === 0 ? "No campaigns yet." : "No campaigns match this filter or search."}
          </div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Campaign</th>
                <th>Status</th>
                <th>Progress</th>
                <th>Start time</th>
                <th>Results</th>
                <th>Created</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {visibleCampaigns.map((c) => {
                const p = progressById[c.id];
                const done = p ? p.total - p.pending - p.calling : 0;
                const pct = p && p.total > 0 ? Math.round((done / p.total) * 100) : 0;
                const busy = actionId === c.id;
                return (
                  <tr key={c.id} onClick={() => router.push(`/campaigns/${c.id}`)} style={{ cursor: "pointer" }}>
                    <td>
                      <div className="bold" style={{ color: "var(--text)" }}>{c.name}</div>
                      <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                        {agentName(c.agent_id)} · {rateFromPacing(c.pacing_seconds)}/min, up to {c.max_concurrent_calls} at once
                      </div>
                    </td>
                    <td>
                      <span className={`badge ${statusBadgeClass(c.status)}`}>{c.status}</span>
                    </td>
                    <td style={{ minWidth: 130 }}>
                      {p ? (
                        <>
                          <div style={{ height: 6, borderRadius: 4, background: "var(--surf-3)", overflow: "hidden", marginBottom: 4 }}>
                            <div style={{ height: "100%", width: `${pct}%`, background: "var(--green)", borderRadius: 4 }} />
                          </div>
                          <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                            {done.toLocaleString()} / {p.total.toLocaleString()} leads
                          </div>
                        </>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      {c.started_at ? fmtDate(c.started_at) : c.scheduled_start_at ? (
                        <span title="Scheduled">{fmtDate(c.scheduled_start_at)}</span>
                      ) : "—"}
                    </td>
                    <td style={{ fontSize: ".76rem", color: "var(--text-2)" }}>
                      {p
                        ? OUTCOMES.filter((o) => (p[o.key] as number) > 0).map((o) => `${(p[o.key] as number).toLocaleString()} ${o.label}`).join(" · ") || "—"
                        : "—"}
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>{fmtDate(c.created_at)}</td>
                    <td onClick={(e) => e.stopPropagation()} style={{ whiteSpace: "nowrap" }}>
                      {(c.status === "draft" || c.status === "scheduled") && (
                        <button className="btn btn-primary btn-sm" disabled={busy} style={ACTION_BUTTON} onClick={() => runAction(c, startCampaign)}>
                          {c.status === "scheduled" ? "Start now" : "Start"}
                        </button>
                      )}
                      {c.status === "running" && (
                        <button className="btn btn-ghost btn-sm" disabled={busy} style={ACTION_BUTTON} onClick={() => runAction(c, pauseCampaign)}>
                          Pause
                        </button>
                      )}
                      {c.status === "paused" && (
                        <button className="btn btn-primary btn-sm" disabled={busy} style={ACTION_BUTTON} onClick={() => runAction(c, resumeCampaign)}>
                          Resume
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {searchParams.get("add") === "1" && (
        <CreateCampaignModal
          tenant={tenant}
          isAllTenants={isAllTenants}
          onClose={() => router.replace("/campaigns")}
          onCreated={(created, problem) => {
            // Readiness and upload problems are shown on the campaign's own page.
            if (problem) router.push(`/campaigns/${created.id}`);
            else {
              router.replace("/campaigns");
              refresh();
            }
          }}
        />
      )}

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
            {allTenants.map((t) => (
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
