"use client";

// Call Flows (call_flows table), shareable by several agents via agents.call_flow_id.
// An agent's own conversation graph lives at /workflows/{tenant}/{agent} and isn't listed here.

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


  // Each tenant's fetch fails independently so one bad account never blanks the rest.
  useEffect(() => {
    if (tenantLoading) return;
    const targets = isAllTenants ? allTenants : tenant ? [tenant] : [];
    if (targets.length === 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setFlows([]);
      setLoading(false);
      return;
    }
     
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

  const open = (f: FlowRow) => router.push(`/ivr-call/${f.id}`);

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
          <span className="card-title">IVR-Call</span>
          <input
            className="form-input"
            style={{ width: 200, marginLeft: "auto" }}
            placeholder="Search menus…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : error ? (
          <div className="card-body"><div className="error-banner">{error}</div></div>
        ) : rows.length === 0 ? (
          <div className="empty-state">
            No IVR call menus yet. An IVR call menu answers the call, plays options like &ldquo;press 1 for
            sales&rdquo; and sends the caller on — create one to draw it.
          </div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Menu</th><th>Account</th><th>Type</th><th>State</th><th>Version</th><th />
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
        An IVR call menu answers before any AI agent does: it plays messages, listens for keypresses,
        and sends the caller to a person, to an AI agent, or ends the call. Open a menu to pick
        which agents answer behind it.
      </div>

    </>
  );
}
