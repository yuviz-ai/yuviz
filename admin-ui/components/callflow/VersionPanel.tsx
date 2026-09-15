"use client";

// Publish history and rollback for a call flow.
//
// Same shape as components/workflow/VersionPanel.tsx, and the same reasoning:
// restoring republishes an old graph as a NEW version rather than moving a
// pointer back, so the log stays append-only and "what was live when that
// call came in" stays answerable. No structural diff — the need this serves
// is undoing a bad publish, not auditing a graph line by line.

import { useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import { CallFlowVersion, listCallFlowVersions, rollbackCallFlow } from "@/lib/callFlowApi";

export function CallFlowVersionPanel({
  callFlowId,
  refreshKey,
  onRolledBack,
}: {
  callFlowId: string;
  refreshKey: number;
  onRolledBack: () => void;
}) {
  const [versions, setVersions] = useState<CallFlowVersion[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  useEffect(() => {
    listCallFlowVersions(callFlowId)
      .then(setVersions)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, [callFlowId, refreshKey]);

  const restore = async (version: number) => {
    setBusy(version);
    setError(null);
    try {
      await rollbackCallFlow(callFlowId, version);
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
        <span className="card-sub">
          {versions.length} publish{versions.length === 1 ? "" : "es"}
        </span>
      </div>
      {error && <div className="card-body"><div className="error-banner">{error}</div></div>}
      {versions.length === 0 ? (
        <div className="empty-state">Nothing published yet.</div>
      ) : (
        <table className="tbl">
          <thead>
            <tr>
              <th>Version</th><th>Published</th><th>By</th><th>Note</th><th />
            </tr>
          </thead>
          <tbody>
            {versions.map((v, i) => (
              <tr key={v.version}>
                <td className="bold">v{v.version}</td>
                <td>{new Date(v.published_at).toLocaleString()}</td>
                <td>{v.published_by_email || "—"}</td>
                <td>{v.note || (i === 0 ? "live" : "")}</td>
                <td style={{ textAlign: "right" }}>
                  {i > 0 && (
                    <button
                      className="btn btn-ghost btn-sm"
                      disabled={busy !== null}
                      onClick={() => restore(v.version)}
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
