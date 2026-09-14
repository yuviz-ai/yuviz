"use client";

import { useEffect, useState } from "react";
import {
  ApiError, Call, CallWithTenant, listAllCalls,
  listTenants, Tenant, TranscriptEntry, getTranscript,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function formatDuration(ms: number | null): string {
  if (ms == null) return "—";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

function statusBadgeClass(status: Call["status"]): string {
  return status === "live" ? "green" : "gray";
}

const PAGE_SIZE_OPTIONS = [10, 25, 50, 100];

export default function CallsPage() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [calls, setCalls] = useState<CallWithTenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [detailCall, setDetailCall] = useState<CallWithTenant | null>(null);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [transcriptLoading, setTranscriptLoading] = useState(false);

  const [pageSize, setPageSize] = useState(25);
  const [page, setPage] = useState(1);

  useEffect(() => {
    listTenants().then(setTenants).catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  useEffect(() => {
    if (tenants.length === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    listAllCalls(tenants)
      .then((cs) => {
        setCalls(cs);
        setPage(1);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  }, [tenants]);

  const pageCount = Math.max(1, Math.ceil(calls.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const pageCalls = calls.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const rangeStart = calls.length === 0 ? 0 : (currentPage - 1) * pageSize + 1;
  const rangeEnd = Math.min(currentPage * pageSize, calls.length);

  const openDetail = (call: CallWithTenant) => {
    setDetailCall(call);
    setTranscript([]);
    setTranscriptLoading(true);
    getTranscript(call.session_id)
      .then(setTranscript)
      .catch(() => setTranscript([]))
      .finally(() => setTranscriptLoading(false));
  };

  return (
    <>
      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Call Log</div>
          <div className="card-sub">{loading ? "Loading…" : `${calls.length} call${calls.length === 1 ? "" : "s"}`}</div>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginLeft: 12 }}>
            <label htmlFor="calls-page-size" style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
              Rows per page
            </label>
            <select
              id="calls-page-size"
              className="form-select"
              style={{ width: 76, padding: "3px 8px", fontSize: ".72rem" }}
              value={pageSize}
              onChange={(e) => {
                setPageSize(Number(e.target.value));
                setPage(1);
              }}
            >
              {PAGE_SIZE_OPTIONS.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </div>
        </div>
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : calls.length === 0 ? (
          <div className="empty-state">No calls yet.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Time</th>
                <th>Account</th>
                <th>Direction</th>
                <th>Mode</th>
                <th>From</th>
                <th>To</th>
                <th>Duration</th>
                <th>Status</th>
                <th>Transcript</th>
                <th>Owner</th>
              </tr>
            </thead>
            <tbody>
              {pageCalls.map((c) => (
                <tr key={c.session_id} onClick={() => openDetail(c)}>
                  <td style={{ whiteSpace: "nowrap" }}>{formatTime(c.started_at)}</td>
                  <td>{c.tenantName}</td>
                  <td>
                    <span className={`badge ${c.direction === "inbound" ? "cyan" : "amber"}`}>{c.direction}</span>
                  </td>
                  <td>
                    <span className="badge indigo">{c.mode}</span>
                  </td>
                  <td className="mono">{c.caller_number || "—"}</td>
                  <td className="mono">{c.called_number || "—"}</td>
                  <td>{formatDuration(c.duration_ms)}</td>
                  <td>
                    <span className={`badge ${statusBadgeClass(c.status)}`}>{c.status}</span>
                  </td>
                  <td>
                    <button className="diff-link" onClick={(e) => { e.stopPropagation(); openDetail(c); }}>
                      {/* turn_count is a summary counter only written once, at clean
                          call-end — it stays 0 for a call reconciled after a server
                          restart even though its real per-turn data still exists in
                          transcript_entries (found live 2026-08-02). Always show this
                          as clickable; openDetail()'s getTranscript() fetch is the
                          actual source of truth and already handles the empty case. */}
                      {c.turn_count > 0 ? `Transcript · ${c.turn_count}` : "Transcript"}
                    </button>
                  </td>
                  <td>{c.agent_name || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {!loading && calls.length > 0 && (
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6, padding: "10px 16px", borderTop: "1px solid var(--border)" }}>
            <div style={{ fontSize: ".71rem", color: "var(--text-3)" }}>
              Showing {rangeStart}–{rangeEnd} of {calls.length}
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <button className="btn btn-ghost btn-sm" disabled={currentPage <= 1} onClick={() => setPage((p) => p - 1)}>
                ‹
              </button>
              {Array.from({ length: pageCount }, (_, i) => i + 1)
                .filter((n) => n === 1 || n === pageCount || Math.abs(n - currentPage) <= 1)
                .reduce<number[]>((acc, n) => {
                  if (acc.length > 0 && n - acc[acc.length - 1] > 1) acc.push(-1);
                  acc.push(n);
                  return acc;
                }, [])
                .map((n, i) =>
                  n === -1 ? (
                    <span key={`gap-${i}`} style={{ padding: "4px 6px", color: "var(--text-3)", fontSize: ".71rem" }}>
                      …
                    </span>
                  ) : (
                    <button
                      key={n}
                      className={`btn btn-sm ${n === currentPage ? "btn-primary" : "btn-ghost"}`}
                      onClick={() => setPage(n)}
                    >
                      {n}
                    </button>
                  ),
                )}
              <button className="btn btn-ghost btn-sm" disabled={currentPage >= pageCount} onClick={() => setPage((p) => p + 1)}>
                ›
              </button>
            </div>
          </div>
        )}
      </div>

      <Modal open={detailCall !== null} title="Call Details" onClose={() => setDetailCall(null)}>
        {detailCall && (
          <>
            <div style={{ fontSize: ".65rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".09em", color: "var(--text-3)", marginBottom: 6 }}>
              Call
            </div>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 14 }}>
              <span>Direction <span className="badge cyan" style={{ marginLeft: 4 }}>{detailCall.direction}</span></span>
              <span>Mode <span className="badge indigo" style={{ marginLeft: 4 }}>{detailCall.mode}</span></span>
              <span>Status <span className={`badge ${statusBadgeClass(detailCall.status)}`} style={{ marginLeft: 4 }}>{detailCall.status}</span></span>
              <span>Duration <strong style={{ color: "var(--text)" }}>{formatDuration(detailCall.duration_ms)}</strong></span>
            </div>
            <div style={{ fontSize: ".65rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".09em", color: "var(--text-3)", marginBottom: 6 }}>
              Parties
            </div>
            <div style={{ marginBottom: 14 }}>
              From <span className="mono" style={{ color: "var(--text)" }}>{detailCall.caller_number || "—"}</span>
              &nbsp;→&nbsp;
              To <span className="mono" style={{ color: "var(--text)" }}>{detailCall.called_number || "—"}</span>
            </div>
            <div style={{ fontSize: ".65rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".09em", color: "var(--text-3)", marginBottom: 6 }}>
              Owner
            </div>
            <div style={{ marginBottom: 14 }}>{detailCall.agent_name || "—"}</div>
            <div style={{ fontSize: ".65rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".09em", color: "var(--text-3)", marginBottom: 6 }}>
              Timeline
            </div>
            <div style={{ marginBottom: 14 }}>
              Started <span className="mono" style={{ color: "var(--text)" }}>{formatTime(detailCall.started_at)}</span>
              {detailCall.ended_at && (
                <>
                  &nbsp;·&nbsp;Ended <span className="mono" style={{ color: "var(--text)" }}>{formatTime(detailCall.ended_at)}</span>
                </>
              )}
            </div>
            {/* The path this call took through its workflow, and how it
                ended — the questions a single-prompt agent simply can't
                answer (docs/workflow.md §7.1). Absent entirely for one. */}
            {detailCall.nodes_visited && detailCall.nodes_visited.length > 0 && (
              <>
                <div style={{ fontSize: ".65rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".09em", color: "var(--text-3)", marginBottom: 6 }}>
                  Path
                </div>
                <div style={{ marginBottom: 14, display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, fontSize: ".75rem" }}>
                  {detailCall.nodes_visited.map((node, i) => (
                    <span key={`${node}-${i}`} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      {i > 0 && <span style={{ color: "var(--text-3)" }}>→</span>}
                      <span className="badge gray">{node}</span>
                    </span>
                  ))}
                  {detailCall.disposition && (
                    <span className="badge indigo" style={{ marginLeft: 6 }}>{detailCall.disposition}</span>
                  )}
                </div>
              </>
            )}
            {detailCall.extracted_variables && Object.keys(detailCall.extracted_variables).length > 0 && (
              <>
                <div style={{ fontSize: ".65rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".09em", color: "var(--text-3)", marginBottom: 6 }}>
                  Captured
                </div>
                <div style={{ marginBottom: 14, fontSize: ".75rem" }}>
                  {Object.entries(detailCall.extracted_variables).map(([k, v]) => (
                    <div key={k}>
                      <span style={{ color: "var(--text-3)" }}>{k}</span>{" "}
                      <span className="mono" style={{ color: "var(--text)" }}>{String(v)}</span>
                    </div>
                  ))}
                </div>
              </>
            )}
            <div style={{ fontSize: ".65rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".09em", color: "var(--text-3)" }}>
              Transcript
            </div>
            {transcriptLoading ? (
              <div style={{ color: "var(--text-3)", fontSize: ".75rem", marginTop: 4 }}>Loading…</div>
            ) : transcript.length === 0 ? (
              <div style={{ color: "var(--text-3)", fontSize: ".75rem", marginTop: 4 }}>No transcript available.</div>
            ) : (
              <div style={{ background: "var(--surf-2)", border: "1px solid var(--border)", borderRadius: "var(--rs)", padding: "10px 12px", fontSize: ".75rem", lineHeight: 1.7, marginTop: 4 }}>
                {transcript.map((t) => (
                  <div key={t.id} style={{ marginBottom: 8 }}>
                    <div>Caller: {t.caller_text || "—"}</div>
                    <div>Agent: {t.ai_response || "—"}</div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </Modal>
    </>
  );
}
