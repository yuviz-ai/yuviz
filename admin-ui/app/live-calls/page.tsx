"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  ApiError,
  getCurrentUser,
  getLiveCalls,
  InterventionAction,
  listTenants,
  LiveCall,
  LiveCallsSnapshot,
  requestIntervention,
  Tenant,
  User,
} from "@/lib/api";
import { ACTIVE_TENANT_STORAGE_KEY } from "@/components/AppShell";

// AC5's whole poll budget — never slacken this for testing (lesson 25); the
// pool acquire timeout and rate limit on the server are sized to exactly
// this cadence.
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

// Builds the CSV from the exact rendered `items` array passed in — no
// re-query (AC8). Column order matches the on-screen table exactly.
// Exactly the 8 informational columns rendered in the on-screen table (the
// trailing Listen/Barge column is actions, not data, so it has no CSV
// counterpart) — row/column count must match the on-screen table exactly
// (AC8), not a re-derived/re-queried projection.
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
  const [tenants, setTenants] = useState<Tenant[]>([]);
  // null covers BOTH "not superadmin, irrelevant" and "superadmin, nothing
  // selected yet" — deliberately one value, not undefined-then-null, so a
  // non-superadmin role never causes a real state transition here at all
  // (a transition would recreate fetchSnapshot below and double the very
  // first poll — found by actually driving this in a browser, lesson 23).
  const [activeTenantSlug, setActiveTenantSlug] = useState<string | null>(null);

  const [snapshot, setSnapshot] = useState<LiveCallsSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [interveningId, setInterveningId] = useState<string | null>(null);

  const generationRef = useRef(0);

  useEffect(() => {
    getCurrentUser().then(setUser).catch(() => {});
  }, []);

  // Only a superadmin needs the tenant list (for the picker) or the stored
  // selection at all — supervisor/admin are already scoped server-side, and
  // activeTenantSlug's initial `null` already means "no selection needed"
  // for them, so this effect does nothing at all in that case (no setState,
  // no re-render, no re-fetch).
  useEffect(() => {
    if (!user || user.role !== "superadmin") return;
    listTenants()
      .then((ts) => {
        setTenants(ts);
        const stored = typeof window !== "undefined" ? window.localStorage.getItem(ACTIVE_TENANT_STORAGE_KEY) : null;
        setActiveTenantSlug(ts.find((t) => t.slug === stored)?.slug ?? null);
      })
      .catch(() => setActiveTenantSlug(null));
  }, [user]);

  const selectTenant = (t: Tenant) => {
    setActiveTenantSlug(t.slug);
    try {
      window.localStorage.setItem(ACTIVE_TENANT_STORAGE_KEY, t.slug);
    } catch {
      // Private-mode/blocked storage — the selection just won't survive a
      // reload; strictly worse, not a crash.
    }
  };

  // superadmin with nothing selected yet: no fetch, no interval — the
  // picker renders instead of a table (AC2). Every other role is always
  // "ready" the instant we know it isn't superadmin.
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
          // Demoted/soft-deleted mid-session — stop polling outright
          // rather than retrying into the same 403 every 5s.
          setForbidden(e.detail);
          return;
        }
        // Any other failure keeps the last good snapshot on screen
        // (lesson 21's sibling failure mode) — only the banner appears.
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
      // Invalidates anything still in flight from THIS effect run — the
      // one guard that makes pause/tenant-switch/unmount all safe against
      // a late-arriving response overwriting a frozen or superseded table.
      generationRef.current += 1;
    };
  }, [tenantResolved, paused, forbidden, fetchSnapshot]);

  const resume = () => {
    setForbidden(null);
    setPaused(false);
    // The effect above fires an immediate fetch as soon as `paused` flips —
    // AC7's "no stale pre-pause paint" without a second, redundant call here.
  };

  const handleIntervention = async (sessionId: string, action: InterventionAction) => {
    setInterveningId(sessionId);
    try {
      await requestIntervention(sessionId, action, user?.role === "superadmin" ? activeTenantSlug ?? undefined : undefined);
      // No optimistic state change — the next poll (within one 5s cycle,
      // AC14) surfaces the intervention badge for every operator, this one
      // included, from the server's own record.
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
          {tenants.length === 0 ? (
            <div className="empty-state">No tenants yet.</div>
          ) : (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {tenants.map((t) => (
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
