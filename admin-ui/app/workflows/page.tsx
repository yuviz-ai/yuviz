"use client";

// IVR Flows — named IVR/OBD flows, each its own object (call_flows table),
// separate from Agent Studio (/agents) which owns an agent's identity, voice
// and knowledge.
//
// A flow is NOT one agent's conversation graph: it branches on a keypress,
// it has a name and a slug of its own, and several agents can point at the
// same one (agents.call_flow_id). The agent's own conversational graph still
// lives at /workflows/{tenant}/{agent} — reachable from Agent Studio, not
// listed here.

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError } from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";
import { CallFlowSummary, deleteCallFlow, listCallFlows } from "@/lib/callFlowApi";

interface FlowRow extends CallFlowSummary {
  tenantSlug: string;
  tenantName: string;
}

export default function CallFlowsPage() {
  const router = useRouter();
  const { tenant, allTenants, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const [flows, setFlows] = useState<FlowRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);


  // One account at a time by default, or every account under "All tenants"
  // — each tenant's fetch fails independently below, so one bad account
  // never blanks the rest (fanning out unconditionally is what broke this
  // page at scale before the switcher existed).
  useEffect(() => {
    if (tenantLoading) return;
    const targets = isAllTenants ? allTenants : tenant ? [tenant] : [];
    if (targets.length === 0) {
      setFlows([]);
      setLoading(false);
      return;
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    Promise.allSettled(targets.map((t) => listCallFlows(t.slug).then((rows) => ({ t, rows }))))
      .then((results) => {
        const nextFlows: FlowRow[] = [];
        const errs: string[] = [];
        results.forEach((r, i) => {
          if (r.status === "fulfilled") {
            nextFlows.push(
              ...r.value.rows.map((f) => ({ ...f, tenantSlug: r.value.t.slug, tenantName: r.value.t.name })),
            );
          } else {
            errs.push(`${targets[i].name}: ${r.reason instanceof ApiError ? r.reason.detail : String(r.reason)}`);
          }
        });
        setFlows(nextFlows);
        setError(errs.length > 0 ? errs.join("; ") : null);
      })
      .finally(() => setLoading(false));
  }, [tenant, allTenants, isAllTenants, tenantLoading]);

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = q
      ? flows.filter((f) => `${f.name} ${f.tenantName}`.toLowerCase().includes(q))
      : flows;
    return [...matched].sort((a, b) => a.name.localeCompare(b.name));
  }, [flows, search]);

  const open = (f: FlowRow) => router.push(`/workflows/flows/${f.id}`);

  const remove = async (f: FlowRow) => {
    const msg =
      `Delete "${f.name}"? Any agents attached to it go back to answering directly, ` +
      `and its published versions go with it.`;
    if (!window.confirm(msg)) return;
    setDeleting(f.id);
    try {
      await deleteCallFlow(f.id);
      setFlows((fs) => fs.filter((x) => x.id !== f.id));
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setDeleting(null);
    }
  };

  return (
    <>
      <div className="card">
        <div className="card-hdr">
          <span className="card-title">IVR Flows</span>
          <input
            className="form-input"
            style={{ width: 200, marginLeft: "auto" }}
            placeholder="Search flows…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button className="btn btn-primary btn-sm" onClick={() => router.push("/workflows/new")}>
            + New flow
          </button>
        </div>

        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : error ? (
          <div className="card-body"><div className="error-banner">{error}</div></div>
        ) : rows.length === 0 ? (
          <div className="empty-state">
            No call flows yet. A flow answers the call, plays a menu and routes on a keypress —
            create one to draw it.
          </div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Flow</th><th>Account</th><th>Type</th><th>State</th><th>Version</th><th />
              </tr>
            </thead>
            <tbody>
              {rows.map((f) => (
                <tr key={f.id} onClick={() => open(f)}>
                  <td className="bold">{f.name}</td>
                  <td>{f.tenantName}</td>
                  <td className="mono" style={{ textTransform: "capitalize" }}>{f.direction}</td>
                  <td>
                    <span className={`badge ${f.status === "active" ? "green" : "gray"}`}>
                      {f.status === "active" ? "Active" : "Paused"}
                    </span>
                  </td>
                  <td className="mono">v{f.config_version}</td>
                  <td style={{ textAlign: "right" }}>
                    <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); open(f); }}>
                      Open
                    </button>{" "}
                    <button
                      className="btn btn-danger btn-sm"
                      disabled={deleting === f.id}
                      onClick={(e) => { e.stopPropagation(); remove(f); }}
                    >
                      {deleting === f.id ? "Deleting…" : "Delete"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="form-hint" style={{ marginTop: 10 }}>
        A call flow answers before any AI agent does: it plays prompts, collects keypresses, and
        routes the caller — to a human, to an AI agent, or to hangup. Open a flow to pick which
        agents answer behind it.
      </div>

    </>
  );
}
