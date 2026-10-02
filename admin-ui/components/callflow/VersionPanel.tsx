"use client";

// Publish history and rollback for a call flow. Restore republishes as a new version (append-only log).

import { useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import { CallFlowVersion, listCallFlowVersions, rollbackCallFlow } from "@/lib/callFlowApi";

// A refused restore's `detail` is generic; the reasons are in `errors`.
function errorText(e: unknown): string {
  if (!(e instanceof ApiError)) return String(e);
  const errors = e.body?.errors;
  if (Array.isArray(errors)) {
    const messages = errors
      .map((err) => (err && typeof err === "object" ? (err as { message?: unknown }).message : null))
      .filter((m): m is string => typeof m === "string" && m.length > 0);
    if (messages.length > 0) return `${e.detail}: ${Array.from(new Set(messages)).join(" ")}`;
  }
  return e.detail;
}

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
      setError(errorText(e));
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
