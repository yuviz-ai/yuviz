"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  ApiError,
  getCurrentUser,
  getLiveCalls,
  InterventionAction,
  LiveCall,
  LiveCallsSnapshot,
  requestIntervention,
  Tenant,
  User,
} from "@/lib/api";
import { ACTIVE_TENANT_STORAGE_KEY } from "@/components/AppShell";
import { ACTIVE_TENANT_EVENT, useActiveTenant } from "@/lib/useActiveTenant";

// Server pool timeout and rate limit are sized to this cadence; don't change it.
const REFRESH_MS = 5000;

function formatElapsed(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

const STAGE_LABEL: Record<LiveCall["live_stage"], string> = {
  ai: "AI",
  waiting_for_human: "Waiting for human",
  human_connected: "Human connected",
};

const STAGE_BADGE: Record<LiveCall["live_stage"], string> = {
  ai: "indigo",
  waiting_for_human: "amber",
  human_connected: "green",
};

function csvEscape(value: string): string {
  return /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

// Mirrors the rendered table's rows and data columns exactly (no re-query; actions column omitted).
function buildCsv(items: LiveCall[]): string {
  const header = ["Agent", "Direction", "From", "To", "Stage", "Elapsed", "Transcript", "Intervention"];
  const rows = items.map((item) => [
    item.agent_name ?? "—",
    item.direction,
    item.caller_number_masked ?? "—",
    item.called_number_masked ?? "—",
    STAGE_LABEL[item.live_stage],
    formatElapsed(item.elapsed_ms),
    item.transcript_withheld ? "—" : item.transcript_snippet ?? "—",
    item.intervention ? `${item.intervention.action} · ${item.intervention.outcome}` : "—",
  ]);
  return [header, ...rows].map((row) => row.map((c) => csvEscape(String(c))).join(",")).join("\n");
}

function downloadCsv(csv: string): void {
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `live-calls-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

export default function LiveCallsPage() {
  const [user, setUser] = useState<User | null>(null);
  const { allTenants, isPlatformScoped, tenant: headerTenant, isAllTenants: headerIsAllTenants } = useActiveTenant();
  // The backend snapshot is always one tenant; "All tenants" means no selection yet.
  const activeTenantSlug = isPlatformScoped ? (headerIsAllTenants ? null : headerTenant?.slug ?? null) : null;

  const [snapshot, setSnapshot] = useState<LiveCallsSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [interveningId, setInterveningId] = useState<string | null>(null);

  const generationRef = useRef(0);

  useEffect(() => {
    getCurrentUser().then(setUser).catch(() => {});
  }, []);

  // Same key + event as AppShell's switcher, so the header follows this picker.
  const selectTenant = (t: Tenant) => {
    try {
      window.localStorage.setItem(ACTIVE_TENANT_STORAGE_KEY, t.slug);
    } catch {
      // Blocked storage: selection just won't survive a reload.
    }
    window.dispatchEvent(new CustomEvent(ACTIVE_TENANT_EVENT, { detail: t.slug }));
  };

  // Superadmin with no tenant selected gets the picker instead of polling.
  const tenantResolved = user != null && (user.role !== "superadmin" || !!activeTenantSlug);

  const fetchSnapshot = useCallback(
    async (gen: number) => {
      try {
        const data = await getLiveCalls(user?.role === "superadmin" ? activeTenantSlug ?? undefined : undefined);
        if (gen !== generationRef.current) return; // superseded by a pause/resume/switch
        setSnapshot(data);
        setError(null);
      } catch (e) {
        if (gen !== generationRef.current) return;
        if (e instanceof ApiError && e.status === 403) {
          // Demoted/deleted mid-session: stop polling instead of retrying into 403s.
          setForbidden(e.detail);
          return;
        }
        // Keep the last good snapshot on screen; only show the banner.
        setError(e instanceof ApiError ? e.detail : String(e));
      }
    },
    [user, activeTenantSlug],
  );

  useEffect(() => {
    if (!tenantResolved || paused || forbidden) return;
    const gen = (generationRef.current += 1);
    fetchSnapshot(gen);
    const id = setInterval(() => {
      fetchSnapshot((generationRef.current += 1));
    }, REFRESH_MS);
    return () => {
      clearInterval(id);
      // Drop late responses after pause/tenant switch/unmount.
      generationRef.current += 1;
    };
  }, [tenantResolved, paused, forbidden, fetchSnapshot]);

  const resume = () => {
    setForbidden(null);
    setPaused(false);
  };

  const handleIntervention = async (sessionId: string, action: InterventionAction) => {
    setInterveningId(sessionId);
    try {
      await requestIntervention(sessionId, action, user?.role === "superadmin" ? activeTenantSlug ?? undefined : undefined);
      // No optimistic update; the next poll shows the server's intervention badge.
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setInterveningId(null);
    }
  };

  if (!user) return null;

  if (user.role === "superadmin" && activeTenantSlug === null) {
    return (
      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Live Calls</div>
        </div>
        <div style={{ padding: 24 }}>
          <div style={{ marginBottom: 12, color: "var(--text-2)", fontSize: ".82rem" }}>
            Select a tenant to monitor its live calls.
          </div>
          {allTenants.length === 0 ? (
            <div className="empty-state">No tenants yet.</div>
          ) : (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {allTenants.map((t) => (
                <button key={t.id} className="btn btn-ghost" onClick={() => selectTenant(t)}>
                  {t.name} <span style={{ color: "var(--text-3)", marginLeft: 6 }}>{t.slug}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  const items = snapshot?.items ?? [];
  const kpis = snapshot?.kpis;
  const utilizationPct = kpis?.utilization_pct ?? null;
  const capSet = kpis?.max_concurrent_calls != null;

  return (
    <>
      {error && <div className="error-banner">{error}</div>}
      {forbidden && (
        <div className="error-banner">
          You no longer have access to Live Calls ({forbidden}). Polling has stopped — sign in again if this
          is unexpected.
        </div>
      )}

      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginBottom: 16 }}>
        <div className="card" style={{ padding: "14px 16px", flex: "1 1 140px" }}>
          <div className="card-sub" style={{ marginLeft: 0, marginBottom: 6 }}>Live calls</div>
          <div style={{ fontSize: "1.6rem", fontWeight: 700 }}>{kpis?.live_calls ?? "—"}</div>
        </div>
        <div className="card" style={{ padding: "14px 16px", flex: "1 1 140px" }}>
          <div className="card-sub" style={{ marginLeft: 0, marginBottom: 6 }}>AI only</div>
          <div style={{ fontSize: "1.6rem", fontWeight: 700 }}>{kpis?.ai_only ?? "—"}</div>
        </div>
        <div className="card" style={{ padding: "14px 16px", flex: "1 1 140px" }}>
          <div className="card-sub" style={{ marginLeft: 0, marginBottom: 6 }}>Waiting for human</div>
          <div style={{ fontSize: "1.6rem", fontWeight: 700 }}>{kpis?.waiting_for_human ?? "—"}</div>
        </div>
        <div className="card" style={{ padding: "14px 16px", flex: "1 1 140px" }}>
          <div className="card-sub" style={{ marginLeft: 0, marginBottom: 6 }}>Human connected</div>
          <div style={{ fontSize: "1.6rem", fontWeight: 700 }}>{kpis?.human_connected ?? "—"}</div>
        </div>
        <div className="card" style={{ padding: "14px 16px", flex: "1 1 140px" }}>
          <div className="card-sub" style={{ marginLeft: 0, marginBottom: 6 }}>Interventions pending</div>
          <div style={{ fontSize: "1.6rem", fontWeight: 700 }}>{kpis?.interventions_pending ?? "—"}</div>
        </div>
        <div className="card" style={{ padding: "14px 16px", flex: "1 1 220px" }}>
          <div className="card-sub" style={{ marginLeft: 0, marginBottom: 6 }}>Utilization</div>
          {!capSet ? (
            <div style={{ fontSize: ".78rem", color: "var(--text-3)" }}>
              Channel cap not set — <Link href="/tenants" style={{ color: "var(--cyan)" }}>set it on the Accounts page</Link>
            </div>
          ) : (
            <>
              <div style={{ fontSize: "1.3rem", fontWeight: 700 }}>
                {utilizationPct}%{utilizationPct != null && utilizationPct > 100 && (
                  <span className="badge red" style={{ marginLeft: 8 }}>Over cap</span>
                )}
              </div>
              <div style={{ height: 6, borderRadius: 3, background: "var(--surf-3)", marginTop: 6, overflow: "hidden" }}>
                <div
                  style={{
                    height: "100%",
                    width: `${Math.min(100, utilizationPct ?? 0)}%`,
                    background: (utilizationPct ?? 0) > 100 ? "var(--red)" : "var(--cyan)",
                  }}
                />
              </div>
            </>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Live Calls</div>
          <div className="card-sub">
            {snapshot ? `${items.length} shown${snapshot.truncated ? " (truncated)" : ""}` : "Loading…"}
          </div>
          <div style={{ display: "flex", gap: 6, marginLeft: 12 }}>
            <button className="btn btn-ghost btn-sm" onClick={() => (paused ? resume() : setPaused(true))}>
              {paused ? "Resume" : "Pause"}
            </button>
            <button
              className="btn btn-ghost btn-sm"
              disabled={items.length === 0}
              onClick={() => downloadCsv(buildCsv(items))}
            >
              Export CSV
            </button>
          </div>
        </div>
        {!snapshot ? (
          <div className="empty-state">Loading…</div>
        ) : items.length === 0 ? (
          <div className="empty-state">No live calls right now.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Agent</th>
                <th>Direction</th>
                <th>From</th>
                <th>To</th>
                <th>Stage</th>
                <th>Elapsed</th>
                <th>Transcript</th>
                <th>Intervention</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.session_id}>
                  <td>{item.agent_name ?? "—"}</td>
                  <td>
                    <span className={`badge ${item.direction === "inbound" ? "cyan" : "amber"}`}>{item.direction}</span>
                  </td>
                  <td className="mono">{item.caller_number_masked ?? "—"}</td>
                  <td className="mono">{item.called_number_masked ?? "—"}</td>
                  <td>
                    <span className={`badge ${STAGE_BADGE[item.live_stage]}`}>{STAGE_LABEL[item.live_stage]}</span>
                  </td>
                  <td className="mono">{formatElapsed(item.elapsed_ms)}</td>
                  <td>{item.transcript_withheld ? "—" : item.transcript_snippet ?? "—"}</td>
                  <td>
                    {item.intervention ? (
                      <span className="badge amber" title={`Requested by ${item.intervention.requested_by_email}`}>
                        {item.intervention.action} · {item.intervention.outcome}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    <div style={{ display: "flex", gap: 6 }}>
                      <button
                        className="btn btn-ghost btn-sm"
                        disabled={interveningId === item.session_id}
                        onClick={() => handleIntervention(item.session_id, "listen")}
                      >
                        Listen
                      </button>
                      <button
                        className="btn btn-ghost btn-sm"
                        disabled={interveningId === item.session_id}
                        onClick={() => handleIntervention(item.session_id, "barge")}
                      >
                        Barge
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
