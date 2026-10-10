"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  ArrowRight, Check, ChevronDown, ChevronLeft, ChevronRight, PhoneIncoming, PhoneOutgoing, RefreshCw, Search, X,
} from "lucide-react";
import { ALL_CALLS_LIMIT, ApiError, CallTimeRange, CallWithTenant, listAllCalls } from "@/lib/api";
import { SentimentBadge, SENTIMENT_ORDER, sentimentLabel } from "@/components/SentimentBadge";
import { useActiveTenant } from "@/lib/useActiveTenant";
import { OUTCOMES, Tone, outcomeOf } from "@/lib/callOutcome";

// Filterable outcomes of a finished call; "Not recorded" isn't one anyone looks for.
const OUTCOME_FILTERS = [OUTCOMES.done, OUTCOMES.to_person, OUTCOMES.transfer_failed, OUTCOMES.dropped];
const TONE_BADGE: Record<Tone, string> = { g: "green", a: "amber", r: "red", n: "gray" };

// Normal endings keep the plain "Completed" badge; only outcomes that need a look stand out.
function StatusBadge({ reason }: { reason: string | null }) {
  const o = outcomeOf(reason);
  if (o.key === "done" || o.key === "unrecorded") return <span className="badge gray">Completed</span>;
  return <span className={`badge ${TONE_BADGE[o.tone]}`} title={o.label}>{o.short}</span>;
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

/** "How long ago" line shown under the absolute timestamp. */
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

const PAGE_SIZE_OPTIONS = [10, 25, 50, 100];
const HOUR = 3_600_000;

type ColKey = "time" | "account" | "parties" | "agent" | "duration" | "sentiment" | "status" | "turns";
/** `hidden` options are set from the menu's extra slot, not a row. */
type FilterOption = { value: string; label: string; test: (c: CallWithTenant) => boolean; hidden?: boolean };
type Column = { key: ColKey; label: string; options: FilterOption[]; align?: "right" };
type TimeRange = { from: string; to: string };

const ageMs = (c: CallWithTenant) => Date.now() - new Date(c.started_at).getTime();

function timeRangeFor(filter: string | undefined, custom: TimeRange): CallTimeRange {
  const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
  switch (filter) {
    case "1h": return { startedAfter: ago(HOUR) };
    case "today": return { startedAfter: new Date(new Date().setHours(0, 0, 0, 0)).toISOString() };
    case "7d": return { startedAfter: ago(7 * 24 * HOUR) };
    case "30d": return { startedAfter: ago(30 * 24 * HOUR) };
    case "custom": return {
      startedAfter: custom.from ? new Date(custom.from).toISOString() : undefined,
      startedBefore: custom.to ? new Date(new Date(custom.to).getTime() + 59_999).toISOString() : undefined,
    };
    default: return {};
  }
}

function formatRangeEnd(v: string): string {
  return new Date(v).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function CustomRange({ range, onApply }: { range: TimeRange; onApply: (r: TimeRange) => void }) {
  const [draft, setDraft] = useState(range);
  const invalid = draft.from !== "" && draft.to !== "" && draft.from > draft.to;
  return (
    <form
      className="th-filter-custom"
      onSubmit={(e) => {
        e.preventDefault();
        if (!invalid && (draft.from || draft.to)) onApply(draft);
      }}
    >
      <div className="th-filter-custom-title">Custom range</div>
      <label>
        From
        <input type="datetime-local" className="form-input" value={draft.from}
          max={draft.to || undefined} onChange={(e) => setDraft({ ...draft, from: e.target.value })} />
      </label>
      <label>
        To
        <input type="datetime-local" className="form-input" value={draft.to}
          min={draft.from || undefined} onChange={(e) => setDraft({ ...draft, to: e.target.value })} />
      </label>
      {invalid && <div className="th-filter-custom-error">“From” must be before “To”.</div>}
      <button type="submit" className="btn btn-primary btn-sm" disabled={invalid || (!draft.from && !draft.to)}>
        Apply
      </button>
    </form>
  );
}

function ColumnFilter({
  column, value, counts, onChange, extra,
}: {
  column: Column;
  value: string | null;
  counts: Map<string, number>;
  onChange: (value: string | null) => void;
  extra?: (close: () => void) => React.ReactNode;
}) {
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const selected = column.options.find((o) => o.value === value);

  useEffect(() => {
    if (!pos) return;
    const close = () => setPos(null);
    const onScroll = (e: Event) => {
      if (!menuRef.current?.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [pos]);

  const pick = (v: string | null) => {
    onChange(v);
    setPos(null);
  };

  return (
    <>
      <button
        type="button"
        className={`th-filter${selected ? " on" : ""}`}
        aria-haspopup="menu"
        aria-expanded={pos !== null}
        onClick={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          // Fixed positioning: the table's horizontal scroll wrapper would clip an absolute menu.
          setPos(pos ? null : { top: r.bottom + 4, left: Math.min(r.left, window.innerWidth - 260) });
        }}
      >
        {column.label}
        {selected && <span className="th-filter-value">· {selected.label}</span>}
        <ChevronDown size={11} />
      </button>
      {pos && (
        <>
          <div className="wf-menu-scrim" onClick={() => setPos(null)} />
          <div ref={menuRef} className="th-filter-menu" role="menu" style={{ top: pos.top, left: pos.left }}>
            <button type="button" role="menuitemradio" aria-checked={!selected}
              className={`th-filter-row${!selected ? " active" : ""}`} onClick={() => pick(null)}>
              <span>All</span>
              {!selected && <Check size={13} className="th-filter-check" />}
            </button>
            {column.options.filter((o) => !o.hidden).map((o) => (
              <button key={o.value} type="button" role="menuitemradio" aria-checked={o.value === value}
                className={`th-filter-row${o.value === value ? " active" : ""}`} onClick={() => pick(o.value)}>
                <span>{o.label}</span>
                {counts.has(o.value) && <span className="th-filter-count">{counts.get(o.value)}</span>}
                {o.value === value && <Check size={13} className="th-filter-check" />}
              </button>
            ))}
            {extra?.(() => setPos(null))}
          </div>
        </>
      )}
    </>
  );
}

// Deep links from the dashboard, e.g. /calls?status=dropped&time=30d or ?from=2026-10-08T11:20.
const URL_STATUS = new Set(["live", "completed", ...OUTCOME_FILTERS.map((o) => o.key)]);
const URL_TIME = new Set(["1h", "today", "7d", "30d"]);
const LOCAL_INPUT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;

function customFromUrl(params: URLSearchParams): string {
  const from = params.get("from") ?? "";
  return LOCAL_INPUT.test(from) ? from : "";
}

function filtersFromUrl(params: URLSearchParams): Partial<Record<ColKey, string>> {
  const f: Partial<Record<ColKey, string>> = {};
  const status = params.get("status");
  if (status && URL_STATUS.has(status)) f.status = status;
  const time = params.get("time");
  if (customFromUrl(params)) f.time = "custom";
  else if (time && URL_TIME.has(time)) f.time = time;
  return f;
}

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

  const searchParams = useSearchParams();
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<Partial<Record<ColKey, string>>>(() => filtersFromUrl(searchParams));
  const [customRange, setCustomRange] = useState<TimeRange>(() => ({ from: customFromUrl(searchParams), to: "" }));

  const [truncated, setTruncated] = useState(false);

  const [pageSize, setPageSize] = useState(25);
  const [page, setPage] = useState(1);

  const timeFilter = filters.time;

  const load = useCallback(() => {
    if (tenantLoading || targetTenants.length === 0) return;
    setLoading(true);
    setError(null);
    listAllCalls(targetTenants, timeRangeFor(timeFilter, customRange))
      .then((result) => {
        setCalls(result.calls);
        setTruncated(result.truncated);
        setPage(1);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  }, [targetTenants, tenantLoading, timeFilter, customRange]);

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(load, [load]);

  const columns = useMemo<Column[]>(() => {
    const distinct = (pick: (c: CallWithTenant) => string | null) =>
      [...new Set(calls.map(pick).filter((v): v is string => !!v))].sort((a, b) => a.localeCompare(b));
    const cols: Column[] = [
      {
        key: "time", label: "Time", options: [
          { value: "1h", label: "Last hour", test: (c) => ageMs(c) <= HOUR },
          { value: "today", label: "Today", test: (c) => new Date(c.started_at).toDateString() === new Date().toDateString() },
          { value: "7d", label: "Last 7 days", test: (c) => ageMs(c) <= 7 * 24 * HOUR },
          { value: "30d", label: "Last 30 days", test: (c) => ageMs(c) <= 30 * 24 * HOUR },
          {
            value: "custom",
            hidden: true,
            label: customRange.from && customRange.to
              ? `${formatRangeEnd(customRange.from)} – ${formatRangeEnd(customRange.to)}`
              : customRange.from ? `From ${formatRangeEnd(customRange.from)}` : `Until ${formatRangeEnd(customRange.to)}`,
            test: (c) => {
              const t = new Date(c.started_at).getTime();
              return (!customRange.from || t >= new Date(customRange.from).getTime())
                && (!customRange.to || t <= new Date(customRange.to).getTime() + 59_999);
            },
          },
        ],
      },
      {
        key: "account", label: "Account",
        options: distinct((c) => c.tenantName).map((n) => ({ value: n, label: n, test: (c) => c.tenantName === n })),
      },
      {
        key: "parties", label: "Parties", options: [
          { value: "inbound", label: "Inbound", test: (c) => c.direction === "inbound" },
          { value: "outbound", label: "Outbound", test: (c) => c.direction === "outbound" },
          { value: "AI", label: "Phone (AI)", test: (c) => c.mode === "AI" },
          { value: "WebRTC", label: "Browser (WebRTC)", test: (c) => c.mode === "WebRTC" },
        ],
      },
      {
        key: "agent", label: "Agent", options: [
          ...distinct((c) => c.agent_name).map((n) => ({ value: n, label: n, test: (c: CallWithTenant) => c.agent_name === n })),
          { value: "__none", label: "No agent", test: (c) => !c.agent_name },
        ],
      },
      {
        key: "duration", label: "Duration", options: [
          { value: "short", label: "Under 30s", test: (c) => c.duration_ms != null && c.duration_ms < 30_000 },
          { value: "mid", label: "30s – 2m", test: (c) => c.duration_ms != null && c.duration_ms >= 30_000 && c.duration_ms < 120_000 },
          { value: "long", label: "Over 2m", test: (c) => c.duration_ms != null && c.duration_ms >= 120_000 },
          { value: "none", label: "Not recorded", test: (c) => c.duration_ms == null },
        ],
      },
      {
        key: "sentiment", label: "Sentiment", options: [
          ...SENTIMENT_ORDER.map((s) => ({ value: s, label: sentimentLabel(s), test: (c: CallWithTenant) => c.sentiment === s })),
          { value: "unscored", label: "Not scored", test: (c) => c.sentiment === null },
        ],
      },
      {
        key: "status", label: "Status", options: [
          { value: "live", label: "Live", test: (c) => c.status === "live" },
          { value: "completed", label: "Completed", test: (c) => c.status === "completed" },
          ...OUTCOME_FILTERS.map((o) => ({
            value: o.key, label: o.label,
            test: (c: CallWithTenant) => c.status === "completed" && outcomeOf(c.close_reason).key === o.key,
          })),
        ],
      },
      {
        key: "turns", label: "Turns", align: "right", options: [
          { value: "0", label: "No turns", test: (c) => c.turn_count === 0 },
          { value: "few", label: "1 – 5", test: (c) => c.turn_count >= 1 && c.turn_count <= 5 },
          { value: "many", label: "More than 5", test: (c) => c.turn_count > 5 },
        ],
      },
    ];
    return isAllTenants ? cols : cols.filter((c) => c.key !== "account");
  }, [calls, isAllTenants, customRange]);

  const searched = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (q === "") return calls;
    return calls.filter((c) => [
      c.tenantName, c.agent_name, c.caller_number, c.called_number,
      c.disposition, c.sentiment_reason, c.session_id,
    ].some((field) => field != null && field.toLowerCase().includes(q)));
  }, [calls, search]);

  const activeTests = useMemo(
    () => columns.flatMap((col) => {
      const opt = col.options.find((o) => o.value === filters[col.key]);
      return opt ? [{ key: col.key, test: opt.test }] : [];
    }),
    [columns, filters],
  );

  const filtered = useMemo(
    () => searched.filter((c) => activeTests.every((f) => f.test(c))),
    [searched, activeTests],
  );

  // Each option's count respects every other active filter, so it shows what picking it would leave.
  const optionCounts = useMemo(() => {
    const out = new Map<ColKey, Map<string, number>>();
    for (const col of columns) {
      // Time ranges are fetched server-side, so the loaded calls can't count the other ranges.
      if (col.key === "time") continue;
      const others = activeTests.filter((f) => f.key !== col.key);
      const base = searched.filter((c) => others.every((f) => f.test(c)));
      out.set(col.key, new Map(col.options.map((o) => [o.value, base.filter(o.test).length])));
    }
    return out;
  }, [columns, activeTests, searched]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const pageCalls = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const rangeStart = filtered.length === 0 ? 0 : (currentPage - 1) * pageSize + 1;
  const rangeEnd = Math.min(currentPage * pageSize, filtered.length);
  const hasFilters = search.trim() !== "" || activeTests.length > 0;

  const setFilter = (key: ColKey, value: string | null) => {
    setFilters((f) => {
      const next = { ...f };
      if (value === null) delete next[key];
      else next[key] = value;
      return next;
    });
    setPage(1);
  };
  const clearFilters = () => {
    setSearch("");
    setFilters({});
    setCustomRange({ from: "", to: "" });
    setPage(1);
  };

  const openDetail = (call: CallWithTenant) => router.push(`/calls/${call.session_id}`);

  return (
    <>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
        <div>
          <h1 style={{ fontSize: "1.5rem", fontWeight: 600, letterSpacing: "-.025em", margin: 0, color: "var(--text)" }}>Calls</h1>
          <div className="form-hint" style={{ marginTop: 4 }}>
            Every finished and in-progress call. Open one to read its transcript and how it ended.
          </div>
        </div>
        <button className="btn btn-ghost" onClick={load} disabled={loading}>
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        <div className="card-hdr" style={{ flexWrap: "wrap", gap: 10 }}>
          <div className="card-title">Call log</div>
          <div className="card-sub">
            {loading
              ? "Loading…"
              : filtered.length === calls.length
                ? `${calls.length} call${calls.length === 1 ? "" : "s"}`
                : `${filtered.length} of ${calls.length} calls`}
            {!loading && truncated && ` · showing the latest ${ALL_CALLS_LIMIT} per account, narrow the time range to see older calls`}
          </div>
          {!loading && (calls.length > 0 || timeFilter) && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginLeft: "auto" }}>
              {hasFilters && (
                <button type="button" className="btn btn-ghost btn-sm" onClick={clearFilters}>
                  <X size={12} /> Clear filters
                </button>
              )}
              <div className="calls-search">
                <Search size={13} className="calls-search-icon" />
                <input
                  className="form-input"
                  placeholder="Search number, agent, account…"
                  aria-label="Search calls"
                  value={search}
                  onChange={(e) => {
                    setSearch(e.target.value);
                    setPage(1);
                  }}
                />
              </div>
            </div>
          )}
        </div>

        {loading ? (
          <div className="empty-state">Loading calls…</div>
        ) : calls.length === 0 && !timeFilter ? (
          <div className="empty-state">
            No calls yet. Calls appear here as soon as an agent answers or places one.
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table className="tbl">
              <thead>
                <tr>
                  {columns.map((col) => (
                    <th
                      key={col.key}
                      className={col.key === "sentiment" ? "col-sentiment" : undefined}
                      style={col.align === "right" ? { textAlign: "right" } : undefined}
                    >
                      <ColumnFilter
                        column={col}
                        value={filters[col.key] ?? null}
                        counts={optionCounts.get(col.key) ?? new Map()}
                        onChange={(v) => setFilter(col.key, v)}
                        extra={col.key === "time" ? (close) => (
                          <CustomRange
                            range={customRange}
                            onApply={(r) => {
                              setCustomRange(r);
                              setFilter("time", "custom");
                              close();
                            }}
                          />
                        ) : undefined}
                      />
                    </th>
                  ))}
                  <th aria-hidden="true" />
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 && (
                  <tr>
                    <td colSpan={columns.length + 1} style={{ cursor: "default" }}>
                      <div className="empty-state">
                        No calls match these filters.
                        <button type="button" className="btn btn-ghost btn-sm" style={{ marginLeft: 8 }} onClick={clearFilters}>
                          Clear filters
                        </button>
                      </div>
                    </td>
                  </tr>
                )}
                {pageCalls.map((c) => (
                  <tr
                    key={c.session_id}
                    className="calls-row"
                    tabIndex={0}
                    onClick={() => openDetail(c)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        openDetail(c);
                      }
                    }}
                  >
                    <td style={{ whiteSpace: "nowrap" }}>
                      <div className="cell-stack">
                        <span className="cell-primary">{formatTime(c.started_at)}</span>
                        <span className="cell-sub">{formatRelative(c.started_at)}</span>
                      </div>
                    </td>
                    {isAllTenants && <td>{c.tenantName}</td>}
                    <td>
                      <div className="calls-parties">
                        <span
                          className={`calls-dir ${c.direction}`}
                          title={`${c.direction === "inbound" ? "Inbound" : "Outbound"} · ${c.mode}`}
                        >
                          {c.direction === "inbound" ? <PhoneIncoming size={13} /> : <PhoneOutgoing size={13} />}
                        </span>
                        {c.caller_number || c.called_number ? (
                          <div className="cell-stack mono">
                            <span className="cell-primary">{c.caller_number || "—"}</span>
                            <span className="cell-sub" style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
                              <ArrowRight size={11} /> {c.called_number || "—"}
                            </span>
                          </div>
                        ) : (
                          <span className="cell-sub">{c.mode} call</span>
                        )}
                      </div>
                    </td>
                    <td>{c.agent_name || "—"}</td>
                    <td className="cell-num">{formatDuration(c.duration_ms)}</td>
                    <td>
                      <SentimentBadge sentiment={c.sentiment} />
                    </td>
                    <td>
                      {c.status === "live"
                        ? <span className="badge green">Live</span>
                        : <StatusBadge reason={c.close_reason} />}
                    </td>
                    {/* turn_count stays 0 for calls reconciled after a restart; the detail page reads the real transcript. */}
                    <td className="cell-num" style={{ textAlign: "right" }}>
                      {c.turn_count > 0 ? c.turn_count : "—"}
                    </td>
                    <td className="calls-row-chevron"><ChevronRight size={14} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!loading && filtered.length > 0 && (
          <div className="calls-footer">
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <span>Showing {rangeStart}–{rangeEnd} of {filtered.length}</span>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                Rows
                <select
                  className="form-select"
                  style={{ width: 70, padding: "3px 8px", fontSize: ".72rem" }}
                  value={pageSize}
                  onChange={(e) => {
                    setPageSize(Number(e.target.value));
                    setPage(1);
                  }}
                >
                  {PAGE_SIZE_OPTIONS.map((n) => (
                    <option key={n} value={n}>{n}</option>
                  ))}
                </select>
              </label>
            </div>
            {pageCount > 1 && (
              <div style={{ display: "flex", gap: 6 }}>
                <button
                  className="btn btn-ghost btn-sm btn-icon"
                  aria-label="Previous page"
                  disabled={currentPage <= 1}
                  onClick={() => setPage((p) => p - 1)}
                >
                  <ChevronLeft size={14} />
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
                      <span key={`gap-${i}`} style={{ padding: "4px 6px" }}>…</span>
                    ) : (
                      <button
                        key={n}
                        className={`btn btn-sm btn-icon ${n === currentPage ? "btn-primary" : "btn-ghost"}`}
                        aria-current={n === currentPage ? "page" : undefined}
                        onClick={() => setPage(n)}
                      >
                        {n}
                      </button>
                    ),
                  )}
                <button
                  className="btn btn-ghost btn-sm btn-icon"
                  aria-label="Next page"
                  disabled={currentPage >= pageCount}
                  onClick={() => setPage((p) => p + 1)}
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </>
  );
}
