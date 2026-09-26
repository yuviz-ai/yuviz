"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ApiError, Call, CallSentiment, CallWithTenant, listAllCalls,
} from "@/lib/api";
import { SentimentBadge, SENTIMENT_ORDER, sentimentLabel } from "@/components/SentimentBadge";
import { useActiveTenant } from "@/lib/useActiveTenant";

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

/** Secondary line under the timestamp — "how long ago" is the question a
 *  call log is usually scanned with, and it is tedious to work out from an
 *  absolute time. Both are shown; neither replaces the other. */
function formatRelative(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  if (diffMs < 0) return "just now";
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return days < 30 ? `${days}d ago` : formatTime(iso);
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

type SentimentFilter = CallSentiment | "unscored";

export default function CallsPage() {
  const router = useRouter();
  const { tenant, allTenants, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const targetTenants = useMemo(
    () => (isAllTenants ? allTenants : tenant ? [tenant] : []),
    [tenant, allTenants, isAllTenants],
  );
  const [calls, setCalls] = useState<CallWithTenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [sentimentFilter, setSentimentFilter] = useState<SentimentFilter | null>(null);

  const [pageSize, setPageSize] = useState(25);
  const [page, setPage] = useState(1);

  useEffect(() => {
    if (tenantLoading || targetTenants.length === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    listAllCalls(targetTenants)
      .then((cs) => {
        setCalls(cs);
        setPage(1);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  }, [targetTenants, tenantLoading]);

  // Counts come from the unfiltered set so a chip always shows how many it
  // would select — a count that shrank as you filtered would be useless for
  // deciding what to look at next.
  const sentimentCounts = useMemo(() => {
    const counts = new Map<SentimentFilter, number>();
    for (const c of calls) {
      const key: SentimentFilter = c.sentiment ?? "unscored";
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return counts;
  }, [calls]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return calls.filter((c) => {
      if (sentimentFilter !== null) {
        const key: SentimentFilter = c.sentiment ?? "unscored";
        if (key !== sentimentFilter) return false;
      }
      if (q === "") return true;
      return [
        c.tenantName, c.agent_name, c.caller_number, c.called_number,
        c.disposition, c.sentiment_reason, c.session_id,
      ].some((field) => field != null && field.toLowerCase().includes(q));
    });
  }, [calls, search, sentimentFilter]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const pageCalls = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const rangeStart = filtered.length === 0 ? 0 : (currentPage - 1) * pageSize + 1;
  const rangeEnd = Math.min(currentPage * pageSize, filtered.length);

  const toggleSentiment = (key: SentimentFilter) => {
    setSentimentFilter((current) => (current === key ? null : key));
    setPage(1);
  };

  const openDetail = (call: CallWithTenant) => router.push(`/calls/${call.session_id}`);

  return (
    <>
      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Call Log</div>
          <div className="card-sub">
            {loading
              ? "Loading…"
              : filtered.length === calls.length
                ? `${calls.length} call${calls.length === 1 ? "" : "s"}`
                : `${filtered.length} of ${calls.length} calls`}
          </div>
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

        {!loading && calls.length > 0 && (
          <div className="calls-filters">
            <input
              className="form-input calls-search"
              style={{ padding: "5px 10px", fontSize: ".74rem" }}
              placeholder="Search number, agent, account…"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(1);
              }}
            />
            <div className="calls-filter-group">
              <span className="calls-filter-label">Sentiment</span>
              {SENTIMENT_ORDER.map((s) => (
                <button
                  key={s}
                  type="button"
                  className={`calls-chip${sentimentFilter === s ? " on" : ""}`}
                  onClick={() => toggleSentiment(s)}
                >
                  {sentimentLabel(s)}
                  <span className="calls-chip-count">{sentimentCounts.get(s) ?? 0}</span>
                </button>
              ))}
              <button
                type="button"
                className={`calls-chip${sentimentFilter === "unscored" ? " on" : ""}`}
                onClick={() => toggleSentiment("unscored")}
                title="Calls that were never scored — not the same as neutral"
              >
                Not scored
                <span className="calls-chip-count">{sentimentCounts.get("unscored") ?? 0}</span>
              </button>
            </div>
          </div>
        )}

        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : calls.length === 0 ? (
          <div className="empty-state">No calls yet.</div>
        ) : filtered.length === 0 ? (
          <div className="empty-state">No calls match these filters.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Time</th>
                <th>Account</th>
                {/* Direction and Mode were two columns showing one fact —
                    calls.py derives mode FROM direction (_mode_of), so they
                    could never disagree. Merged into one cell. */}
                <th>Type</th>
                <th>Parties</th>
                <th>Duration</th>
                <th className="col-sentiment">Sentiment</th>
                <th>Status</th>
                <th>Agent</th>
                <th style={{ textAlign: "right" }}>Turns</th>
              </tr>
            </thead>
            <tbody>
              {pageCalls.map((c) => (
                <tr key={c.session_id} onClick={() => openDetail(c)}>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <div className="cell-stack">
                      <span className="cell-primary">{formatTime(c.started_at)}</span>
                      <span className="cell-sub">{formatRelative(c.started_at)}</span>
                    </div>
                  </td>
                  <td>{c.tenantName}</td>
                  <td>
                    <div className="cell-stack">
                      <span className={`badge ${c.direction === "inbound" ? "cyan" : "amber"}`}>
                        {c.direction}
                      </span>
                      <span className="cell-sub">{c.mode}</span>
                    </div>
                  </td>
                  <td className="mono">
                    {c.caller_number || c.called_number ? (
                      <div className="cell-stack">
                        <span>{c.caller_number || "—"}</span>
                        <span className="cell-sub">→ {c.called_number || "—"}</span>
                      </div>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="cell-num">{formatDuration(c.duration_ms)}</td>
                  <td>
                    <SentimentBadge sentiment={c.sentiment} />
                    {c.sentiment_reason && (
                      <span className="calls-reason" title={c.sentiment_reason}>
                        {c.sentiment_reason}
                      </span>
                    )}
                  </td>
                  <td>
                    <span className={`badge ${statusBadgeClass(c.status)}`}>{c.status}</span>
                  </td>
                  <td>{c.agent_name || "—"}</td>
                  {/* turn_count is a summary counter written once at clean
                      call-end — it stays 0 for a call reconciled after a
                      server restart even though its real per-turn data still
                      exists in transcript_entries (found live 2026-08-02).
                      The row is always clickable; the detail page's
                      getTranscript() fetch is the source of truth. */}
                  <td className="cell-num" style={{ textAlign: "right" }}>
                    {c.turn_count > 0 ? c.turn_count : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {!loading && filtered.length > 0 && (
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6, padding: "10px 16px", borderTop: "1px solid var(--border)" }}>
            <div style={{ fontSize: ".71rem", color: "var(--text-3)" }}>
              Showing {rangeStart}–{rangeEnd} of {filtered.length}
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
    </>
  );
}
