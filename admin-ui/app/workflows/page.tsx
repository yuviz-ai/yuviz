"use client";

// Call Flows — named IVR/OBD flows, each its own object (call_flows table),
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
  const { tenant, loading: tenantLoading } = useActiveTenant();
  const [flows, setFlows] = useState<FlowRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);


  // One account at a time — the header switcher picks which. Fanning out
  // across every tenant is what broke this page at scale.
  useEffect(() => {
    if (tenantLoading) return;
    if (!tenant) {
      setFlows([]);
      setLoading(false);
      return;
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    listCallFlows(tenant.slug)
      .then((rows) =>
        setFlows(rows.map((f) => ({ ...f, tenantSlug: tenant.slug, tenantName: tenant.name }))),
      )
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  }, [tenant, tenantLoading]);

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
          <span className="card-title">Call Flows</span>
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
