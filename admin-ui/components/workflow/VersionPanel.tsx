"use client";

// Publish history and rollback. Rollback republishes an old version as a
// new one, so the log stays append-only and "what was live at 3pm
// yesterday" stays answerable.
//
// No structural diff view (docs/workflow.md §6.3): the need this actually
// serves is undoing a bad publish, not auditing a graph line by line.

import { useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import {
  listWorkflowVersions, rollbackWorkflow, type WorkflowVersion,
} from "@/lib/workflowApi";

export function VersionPanel({
  tenantSlug, agentId, refreshKey, onRolledBack,
}: {
  tenantSlug: string;
  agentId: string;
  refreshKey: number;
  onRolledBack: () => void;
}) {
  const [versions, setVersions] = useState<WorkflowVersion[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  useEffect(() => {
    listWorkflowVersions(tenantSlug, agentId)
      .then(setVersions)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, [tenantSlug, agentId, refreshKey]);

  const rollback = async (version: number) => {
    setBusy(version);
    setError(null);
    try {
      await rollbackWorkflow(tenantSlug, agentId, version);
      onRolledBack();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="card-hdr">
        <span className="card-title">Published versions</span>
        <span className="card-sub">{versions.length} publish{versions.length === 1 ? "" : "es"}</span>
      </div>
      {error && <div className="card-body"><div className="error-banner">{error}</div></div>}
      {versions.length === 0 ? (
        <div className="empty-state">Nothing published yet.</div>
      ) : (
        <table className="tbl">
          <thead>
            <tr>
              <th>Version</th><th>Published</th><th>By</th><th>Size</th><th>Note</th><th />
            </tr>
          </thead>
          <tbody>
            {versions.map((v, i) => (
              <tr key={v.id}>
                <td className="bold">v{v.version}</td>
                <td>{new Date(v.published_at).toLocaleString()}</td>
                <td>{v.published_by_email || "—"}</td>
                <td className="mono">{v.node_count} nodes, {v.edge_count} edges</td>
                <td>{v.note || (i === 0 ? "live" : "")}</td>
                <td style={{ textAlign: "right" }}>
                  {i > 0 && (
                    <button
                      className="btn btn-ghost btn-sm"
                      disabled={busy !== null}
                      onClick={() => rollback(v.version)}
                    >
                      {busy === v.version ? "Restoring…" : "Restore"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
