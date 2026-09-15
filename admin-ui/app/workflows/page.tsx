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
import { ApiError, Tenant, listTenants } from "@/lib/api";
import { CallFlowSummary, createCallFlow, listCallFlows } from "@/lib/callFlowApi";
import { Modal } from "@/components/Modal";

interface FlowRow extends CallFlowSummary {
  tenantSlug: string;
  tenantName: string;
}

function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

export default function CallFlowsPage() {
  const router = useRouter();
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [flows, setFlows] = useState<FlowRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newTenant, setNewTenant] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    listTenants()
      .then(async (ts) => {
        setTenants(ts);
        if (ts.length > 0) setNewTenant(ts[0].slug);
        const perTenant = await Promise.all(
          ts.map(async (t) => {
            const rows = await listCallFlows(t.slug).catch(() => [] as CallFlowSummary[]);
            return rows.map((f) => ({ ...f, tenantSlug: t.slug, tenantName: t.name }));
          }),
        );
        setFlows(perTenant.flat());
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  }, []);

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = q
      ? flows.filter((f) => `${f.name} ${f.tenantName}`.toLowerCase().includes(q))
      : flows;
    return [...matched].sort((a, b) => a.name.localeCompare(b.name));
  }, [flows, search]);

  const open = (f: FlowRow) => router.push(`/workflows/flows/${f.id}`);

  const handleCreate = async () => {
    const slug = slugify(newName);
    if (!slug || !newTenant) return;
    setBusy(true);
    setCreateError(null);
    try {
      const flow = await createCallFlow(newTenant, { slug, name: newName.trim() });
      router.push(`/workflows/flows/${flow.id}`);
    } catch (e) {
      setCreateError(e instanceof ApiError ? e.detail : String(e));
      setBusy(false);
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
          <button className="btn btn-primary btn-sm" onClick={() => setCreating(true)}>
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
                <th>Flow</th><th>Account</th><th>State</th><th>Version</th><th />
              </tr>
            </thead>
            <tbody>
              {rows.map((f) => (
                <tr key={f.id} onClick={() => open(f)}>
                  <td className="bold">{f.name}</td>
                  <td>{f.tenantName}</td>
                  <td>
                    <span className={`badge ${f.status === "active" ? "green" : "gray"}`}>
                      {f.status === "active" ? "Active" : "Paused"}
                    </span>
                  </td>
                  <td className="mono">v{f.config_version}</td>
                  <td style={{ textAlign: "right" }}>
                    <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); open(f); }}>
                      Open
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
        routes the caller — to a human, to an AI agent, or to hangup. Attach one to an agent from
        that agent&apos;s <strong>Advanced</strong> tab in Agent Studio.
      </div>

      <Modal
        open={creating}
        title="New call flow"
        onClose={() => { if (!busy) setCreating(false); }}
        footer={
          <>
            <button className="btn btn-ghost" disabled={busy} onClick={() => setCreating(false)}>
              Cancel
            </button>
            <button
              className="btn btn-primary"
              disabled={busy || !slugify(newName) || !newTenant}
              onClick={handleCreate}
            >
              {busy ? "Creating…" : "Create"}
            </button>
          </>
        }
      >
        {createError && <div className="error-banner">{createError}</div>}
        <div className="form-group">
          <label className="form-label">Name <span className="required">*</span></label>
          <input
            className="form-input"
            autoFocus
            value={newName}
            placeholder="Main line IVR"
            onChange={(e) => setNewName(e.target.value)}
          />
          {newName.trim() !== "" && (
            <div className="form-hint">Address: <span className="mono">{slugify(newName) || "—"}</span></div>
          )}
        </div>
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label">Account <span className="required">*</span></label>
          <select className="form-select" value={newTenant} onChange={(e) => setNewTenant(e.target.value)}>
            {tenants.map((t) => (
              <option key={t.id} value={t.slug}>{t.name}</option>
            ))}
          </select>
          <div className="form-hint">
            You land on the canvas with a starter flow drawn: answer, greet, hang up.
          </div>
        </div>
      </Modal>
    </>
  );
}
