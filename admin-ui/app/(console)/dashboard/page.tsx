"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  AgentWithTenant,
  ApiError,
  CallWithTenant,
  CampaignProgress,
  CampaignWithTenant,
  DashboardStats,
  DispositionSlice,
  getCampaignProgress,
  listAllAgents,
  listAllCalls,
  listAllCampaigns,
  listAllDashboardStats,
  listAllDispositionMix,
  listAllPhoneNumbers,
  listAllTodaysActivity,
  listAllUsageTrend,
  PhoneNumberWithTenant,
} from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";
import { Outcome, Tone, outcomeOf } from "@/lib/callOutcome";
import { hasInboundNumber, isChatAgent } from "@/lib/agentReadiness";

const RANGE_OPTIONS = [
  { label: "Today", hours: 24, days: 0, prev: "previous 24h", period: "in the last 24 hours" },
  { label: "7 days", hours: 24 * 7, days: 7, prev: "previous 7 days", period: "in the last 7 days" },
  { label: "30 days", hours: 24 * 30, days: 30, prev: "previous 30 days", period: "in the last 30 days" },
];
type Range = (typeof RANGE_OPTIONS)[number];

// Fixed locale avoids SSR/client hydration mismatch; operators read Indian grouping (1,36,650).
const fmtInt = (n: number) => n.toLocaleString("en-IN");

function fmtDuration(ms: number | null): string {
  if (ms == null) return "—";
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

// <input type="datetime-local"> format, in local time — what the call log's custom range takes.
function toLocalInput(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function maskNumber(n: string | null): string {
  if (!n) return "Unknown";
  return n.length <= 6 ? n : `${n.slice(0, Math.min(6, n.length - 4))}••• ${n.slice(-4)}`;
}

// How-calls-ended bars in the brand palette, one hue per outcome.
const OUTCOME_COLOR: Record<string, string> = {
  done: "var(--chart-inbound)", to_person: "var(--chart-ai)", transfer_failed: "var(--chart-outbound)",
  dropped: "var(--chart-rose)", unrecorded: "var(--text-3)",
};

const TONE_COLOR: Record<Tone, string> = {
  g: "var(--green)", a: "var(--amber)", r: "var(--red)", n: "var(--text-3)",
};

interface ChartPoint { label: string; inbound: number; outbound: number; aiCalls: number; aiPct: number | null }

const DIRECTION_TAG: Record<string, { label: string; tone: Tone }> = {
  inbound: { label: "Incoming", tone: "g" },
  outbound: { label: "Outgoing", tone: "a" },
  test: { label: "Test", tone: "n" },
};

const toChartPoint = (label: string, inbound: number, outbound: number, ended: number, escalated: number): ChartPoint => ({
  label,
  inbound,
  outbound,
  aiCalls: Math.max(0, ended - escalated),
  // null = calls in progress, nothing measured yet; 0 only when there were no calls at all.
  aiPct: inbound + outbound === 0 ? 0 : ended === 0 ? null : ((ended - escalated) / ended) * 100,
});

function niceStep(max: number): number {
  const raw = Math.max(1, max / 5);
  const mag = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 5, 10].map((m) => m * mag).find((m) => m >= raw) ?? 10 * mag;
}

function TrendChart({ points }: { points: ChartPoint[] }) {
  const [hover, setHover] = useState<number | null>(null);
  if (points.length === 0) return <div className="d-empty">No calls in this period yet.</div>;
  const w = 400;
  const h = 160;
  const peak = Math.max(...points.map((p) => Math.max(p.inbound, p.outbound, p.aiCalls)));
  const step = niceStep(peak);
  const top = Math.max(step, Math.ceil(peak / step) * step);
  const yTicks = Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step);
  const xAt = (i: number) => (points.length === 1 ? w / 2 : (i / (points.length - 1)) * w);
  const yAt = (v: number) => h - (v / top) * h;
  const series = [
    { key: "inbound", label: "Inbound", color: "var(--chart-inbound)", dash: undefined, xy: points.map((p, i) => [xAt(i), yAt(p.inbound)]) },
    { key: "outbound", label: "Outbound", color: "var(--chart-outbound)", dash: "6 5", xy: points.map((p, i) => [xAt(i), yAt(p.outbound)]) },
  ] as const;
  const pts = (xy: readonly (readonly number[])[]) => xy.map(([x, y]) => `${x},${y}`).join(" ");
  const aiXy = points.map((p, i) => [xAt(i), yAt(p.aiCalls)]);
  const calls = (n: number) => `${n} call${n === 1 ? "" : "s"}`;
  const aiText = (p: ChartPoint) => `${calls(p.aiCalls)}${p.aiPct == null || p.inbound + p.outbound === 0 ? "" : ` (${Math.round(p.aiPct)}%)`}`;
  const summary = (p: ChartPoint) =>
    `${p.label}: inbound ${calls(p.inbound)}, outbound ${calls(p.outbound)}, handled by AI ${aiText(p)}`;
  const colW = points.length === 1 ? 100 : 100 / (points.length - 1);
  const pctX = (i: number) => (xAt(i) / w) * 100;
  const every = Math.max(1, Math.ceil(points.length / 6));
  const xTicks = points.map((_, i) => i).filter((i) => i % every === 0);
  const last = points.length - 1;
  // The last label is right-aligned, so a tick less than one step before it would overlap.
  if (xTicks[xTicks.length - 1] !== last) {
    if (last - xTicks[xTicks.length - 1] < every && xTicks.length > 1) xTicks.pop();
    xTicks.push(last);
  }
  return (
    <>
      <div className="d-plot">
        <div className="d-yaxis" aria-hidden="true">
          {yTicks.map((v) => <span key={v} style={{ top: `${(yAt(v) / h) * 100}%` }}>{v}</span>)}
        </div>
        <div className="d-chart">
          <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label="Inbound and outbound calls over time">
            {yTicks.map((v) => (
              <line key={v} x1="0" x2={w} y1={yAt(v)} y2={yAt(v)} stroke={v === 0 ? "var(--text-2)" : "color-mix(in srgb, var(--text) 30%, transparent)"} strokeWidth="1" vectorEffect="non-scaling-stroke" />
            ))}
            {series.map((s) => (
              <polyline key={s.key} points={pts(s.xy)} fill="none" stroke={s.color} strokeWidth="2" strokeDasharray={s.dash}
                strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
            ))}
            {aiXy.length > 1 && (
              <polyline points={pts(aiXy)} fill="none" stroke="var(--chart-ai)" strokeWidth="2" strokeDasharray="1 4"
                strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
            )}
          </svg>
          {points.length === 1 && [...series.map((s) => [s.key, s.color, s.xy[0]] as const), ["ai", "var(--chart-ai)", aiXy[0]] as const].map(([key, color, [x, y]]) => (
            <span key={`single-${key}`} className="d-dot" style={{ left: `${(x / w) * 100}%`, top: `${(y / h) * 100}%`, background: color }} />
          ))}
          {hover !== null && series.map((s) => (
            <span key={s.key} className="d-dot" style={{ left: `${(s.xy[hover][0] / w) * 100}%`, top: `${(s.xy[hover][1] / h) * 100}%`, background: s.color }} />
          ))}
          {points.map((p, i) => {
            const left = Math.max(0, pctX(i) - colW / 2);
            const right = Math.min(100, pctX(i) + colW / 2);
            const clear = () => setHover((cur) => (cur === i ? null : cur));
            return (
              <span
                key={`hover-${i}`}
                className="d-hover"
                tabIndex={0}
                aria-label={summary(p)}
                style={{ left: `${left}%`, width: `${right - left}%` }}
                onMouseEnter={() => setHover(i)}
                onMouseLeave={clear}
                onFocus={() => setHover(i)}
                onBlur={clear}
              />
            );
          })}
          {hover !== null && points[hover] && (
            <div
              className="d-tip"
              role="tooltip"
              style={{
                left: `${pctX(hover)}%`,
                transform: `translateX(${pctX(hover) < 20 ? "0" : pctX(hover) > 80 ? "-100%" : "-50%"})`,
              }}
            >
              <b>{points[hover].label}</b>
              <span><i style={{ background: "var(--chart-inbound)" }} />Inbound: {calls(points[hover].inbound)}</span>
              <span><i style={{ background: "var(--chart-outbound)" }} />Outbound: {calls(points[hover].outbound)}</span>
              <span><i style={{ background: "var(--chart-ai)" }} />Handled by AI: {aiText(points[hover])}</span>
            </div>
          )}
        </div>
        <div className="d-xaxis" aria-hidden="true">
          {xTicks.map((i) => <span key={i} style={{ left: `${pctX(i)}%` }}>{points[i].label}</span>)}
        </div>
      </div>
      <div className="d-legend">
        {series.map((s) => <span key={s.key}><i className={s.dash ? "dashed" : undefined} style={{ ["--c" as string]: s.color }} />{s.label}</span>)}
        <span><i className="dotted" style={{ ["--c" as string]: "var(--chart-ai)" }} />Handled by AI</span>
      </div>
    </>
  );
}

const REFRESH_MS = 30_000;

function LiveStatus({ updatedAt }: { updatedAt: number | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  if (updatedAt === null) return null;
  const ago = Math.max(0, Math.floor((now - updatedAt) / 1000));
  return (
    <span className="d-live" title="This page updates itself every 30 seconds">
      <i />Live updates on · Updated {ago < 60 ? `${ago}s` : `${Math.floor(ago / 60)}m`} ago
    </span>
  );
}

export default function DashboardPage() {
  const { tenant, allTenants, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const targetTenants = useMemo(
    () => (isAllTenants ? allTenants : tenant ? [tenant] : []),
    [tenant, allTenants, isAllTenants],
  );

  const [range, setRange] = useState<Range>(RANGE_OPTIONS[1]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [trend, setTrend] = useState<ChartPoint[]>([]);
  const [dispositions, setDispositions] = useState<DispositionSlice[]>([]);
  const [calls, setCalls] = useState<CallWithTenant[]>([]);
  const [callsTruncated, setCallsTruncated] = useState(false);

  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [numbers, setNumbers] = useState<PhoneNumberWithTenant[]>([]);
  const [campaigns, setCampaigns] = useState<CampaignWithTenant[]>([]);
  const [progress, setProgress] = useState<Record<string, CampaignProgress>>({});

  // Bumping `tick` reloads everything in the background, without the "Loading…" placeholders.
  const [tick, setTick] = useState(0);
  const lastTick = useRef(0);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);

  // A hidden tab waits and refreshes as soon as it is shown again.
  useEffect(() => {
    if (updatedAt === null) return;
    let onVisible: (() => void) | null = null;
    const timer = setTimeout(() => {
      if (!document.hidden) return setTick((t) => t + 1);
      onVisible = () => {
        if (document.hidden) return;
        document.removeEventListener("visibilitychange", onVisible!);
        setTick((t) => t + 1);
      };
      document.addEventListener("visibilitychange", onVisible);
    }, REFRESH_MS);
    return () => {
      clearTimeout(timer);
      if (onVisible) document.removeEventListener("visibilitychange", onVisible);
    };
  }, [updatedAt]);

  useEffect(() => {
    if (tenantLoading || targetTenants.length === 0) return;
    let cancelled = false;
    const since = new Date(Date.now() - range.hours * 3_600_000).toISOString();
    const background = tick !== lastTick.current;
    lastTick.current = tick;
    if (!background) setLoading(true);
    Promise.allSettled([
      listAllDashboardStats(targetTenants, range.hours),
      range.days
        ? listAllUsageTrend(targetTenants, range.days).then((pts) => {
            // API omits zero-call days; draw every day of the range so today is always the last point.
            const byDate = new Map(pts.map((p) => [p.date.slice(0, 10), p]));
            const dense: ChartPoint[] = [];
            for (let i = range.days - 1; i >= 0; i--) {
              const d = new Date();
              d.setDate(d.getDate() - i);
              const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
              const p = byDate.get(key);
              dense.push(toChartPoint(
                d.toLocaleDateString("en-IN", { day: "numeric", month: "short" }),
                p?.inbound ?? 0, p?.outbound ?? 0, p?.ended ?? 0, p?.escalated ?? 0,
              ));
            }
            return dense;
          })
        : listAllTodaysActivity(targetTenants).then((pts) => {
            // API omits zero-call hours; draw every hour from midnight up to now.
            const byHour = new Map(pts.map((p) => [p.hour, p]));
            const hours = pts.map((p) => p.hour);
            const dense: ChartPoint[] = [];
            for (let hr = 0; hr <= Math.max(new Date().getHours(), ...hours); hr++) {
              const p = byHour.get(hr);
              dense.push(toChartPoint(
                `${String(hr).padStart(2, "0")}:00`,
                p?.inbound ?? 0, p?.outbound ?? 0, p?.ended ?? 0, p?.escalated ?? 0,
              ));
            }
            return dense;
          }),
      listAllDispositionMix(targetTenants, range.hours),
      listAllCalls(targetTenants, { startedAfter: since }),
    ]).then(([s, t, d, c]) => {
      if (cancelled) return;
      if (s.status === "fulfilled") setStats(s.value);
      if (t.status === "fulfilled") setTrend(t.value);
      if (d.status === "fulfilled") setDispositions(d.value);
      if (c.status === "fulfilled") {
        setCalls(c.value.calls);
        setCallsTruncated(c.value.truncated);
      }
      const failed = [s, t, d, c].find((r): r is PromiseRejectedResult => r.status === "rejected");
      setError(failed ? (failed.reason instanceof ApiError ? failed.reason.detail : String(failed.reason)) : null);
      setLoading(false);
      setUpdatedAt(Date.now());
    });
    return () => { cancelled = true; };
  }, [targetTenants, tenantLoading, range, tick]);

  useEffect(() => {
    if (tenantLoading || targetTenants.length === 0) return;
    let cancelled = false;
    listAllAgents(targetTenants).then((a) => !cancelled && setAgents(a)).catch(() => {});
    listAllPhoneNumbers(targetTenants).then((n) => !cancelled && setNumbers(n)).catch(() => {});
    // Campaigns run in a separate service; if it's down the card just stays empty.
    listAllCampaigns(targetTenants)
      .then(async (cs) => {
        if (cancelled) return;
        setCampaigns(cs);
        const active = cs.filter((c) => c.status === "running" || c.status === "paused").slice(0, 3);
        const results = await Promise.allSettled(active.map((c) => getCampaignProgress(c.id)));
        if (cancelled) return;
        const next: Record<string, CampaignProgress> = {};
        results.forEach((r, i) => { if (r.status === "fulfilled") next[active[i].id] = r.value; });
        setProgress(next);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [targetTenants, tenantLoading, tick]);

  const kpi = useMemo(() => {
    if (!stats) return null;
    const pctChange = (cur: number, prev: number) => (prev === 0 ? null : ((cur - prev) / prev) * 100);
    const ratio = (num: number, den: number) => (den === 0 ? null : num / den);
    const contained = ratio(stats.ended_count - stats.escalated_count, stats.ended_count);
    const prevContained = ratio(stats.prev_ended_count - stats.prev_escalated_count, stats.prev_ended_count);
    const aht = ratio(stats.aht_duration_ms, stats.aht_sample_count);
    const prevAht = ratio(stats.prev_aht_duration_ms, stats.prev_aht_sample_count);
    return {
      callsDeltaPct: pctChange(stats.total_calls, stats.prev_total_calls),
      aiPct: contained === null ? null : contained * 100,
      // Percentage points, not percent-of-a-percent.
      aiDeltaPt: contained !== null && prevContained !== null ? (contained - prevContained) * 100 : null,
      aht,
      ahtDeltaSec: aht !== null && prevAht !== null ? (aht - prevAht) / 1000 : null,
      failedTransfers: stats.escalated_count - stats.handoff_count,
    };
  }, [stats]);

  const outcomes = useMemo(() => {
    const byLabel = new Map<string, { outcome: Outcome; count: number }>();
    for (const d of dispositions) {
      const outcome = outcomeOf(d.close_reason);
      const entry = byLabel.get(outcome.label) ?? { outcome, count: 0 };
      entry.count += d.count;
      byLabel.set(outcome.label, entry);
    }
    const total = dispositions.reduce((sum, d) => sum + d.count, 0);
    return { rows: [...byLabel.values()].sort((a, b) => b.count - a.count), total };
  }, [dispositions]);

  const mood = useMemo(() => {
    const scored = calls.filter((c) => c.sentiment);
    const count = (pred: (s: string) => boolean) => scored.filter((c) => pred(c.sentiment!)).length;
    const n = scored.length;
    const pct = (v: number) => (n === 0 ? 0 : Math.round((v / n) * 100));
    return {
      n,
      happy: pct(count((s) => s === "positive")),
      neutral: pct(count((s) => s === "neutral")),
      unhappy: pct(count((s) => s === "negative" || s === "frustrated")),
    };
  }, [calls]);

  const agentRows = useMemo(() => {
    const perAgent = new Map<string, { calls: number; ended: number; toPerson: number }>();
    for (const c of calls) {
      if (!c.agent_id) continue;
      const row = perAgent.get(c.agent_id) ?? { calls: 0, ended: 0, toPerson: 0 };
      row.calls += 1;
      if (c.ended_at) row.ended += 1;
      if (c.close_reason?.startsWith("TRANSFER")) row.toPerson += 1;
      perAgent.set(c.agent_id, row);
    }
    return agents
      .map((a) => {
        const s = perAgent.get(a.id) ?? { calls: 0, ended: 0, toPerson: 0 };
        const noNumber = a.status === "active" && a.direction !== "outbound" &&
          !isChatAgent(a) && !hasInboundNumber(a, numbers);
        return {
          agent: a,
          calls: s.calls,
          aiPct: s.ended === 0 ? null : Math.round(((s.ended - s.toPerson) / s.ended) * 100),
          status: a.status !== "active"
            ? { label: "Paused", tone: "n" as Tone }
            : noNumber ? { label: "No number", tone: "a" as Tone } : { label: "Live", tone: "g" as Tone },
          noNumber,
        };
      })
      .sort((a, b) => b.calls - a.calls);
  }, [agents, numbers, calls]);

  const attention = useMemo(() => {
    const items: { tone: Tone; text: string; action: string; href: string }[] = [];
    // Same window the counts came from: rolling 7/30 days, or the 24 hours before the last refresh.
    const callsHref = (status: string) => `/calls?status=${status}&${range.days || updatedAt === null
      ? `time=${range.days ? `${range.days}d` : "today"}`
      : `from=${toLocalInput(new Date(updatedAt - range.hours * 3_600_000))}`}`;
    if (kpi && kpi.failedTransfers > 0) {
      items.push({
        tone: "r",
        text: `${fmtInt(kpi.failedTransfers)} transfer${kpi.failedTransfers === 1 ? "" : "s"} to a person failed`,
        action: "Review",
        href: callsHref("transfer_failed"),
      });
    }
    const dropped = outcomes.rows.find((r) => r.outcome.key === "dropped")?.count ?? 0;
    if (dropped > 0) {
      items.push({ tone: "r", text: `${fmtInt(dropped)} call${dropped === 1 ? "" : "s"} dropped`, action: "Review", href: callsHref("dropped") });
    }
    const unnumbered = agentRows.filter((r) => r.noNumber);
    if (unnumbered.length === 1) {
      items.push({ tone: "a", text: `"${unnumbered[0].agent.name}" has no number`, action: "Add", href: "/telephony" });
    } else if (unnumbered.length > 1) {
      items.push({ tone: "a", text: `${unnumbered.length} agents have no number`, action: "Add", href: "/telephony" });
    }
    const paused = campaigns.filter((c) => c.status === "paused");
    if (paused.length > 0) {
      items.push({
        tone: "a",
        text: paused.length === 1 ? `"${paused[0].name}" is paused` : `${paused.length} campaigns are paused`,
        action: "Open",
        href: paused.length === 1 ? `/campaigns/${paused[0].id}` : "/campaigns",
      });
    }
    const finished = campaigns
      .filter((c) => c.status === "completed")
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0];
    if (finished) {
      items.push({ tone: "g", text: `"${finished.name}" finished`, action: "Results", href: `/campaigns/${finished.id}` });
    }
    return items;
  }, [kpi, outcomes, agentRows, campaigns, range, updatedAt]);

  const runningCampaigns = campaigns.filter((c) => progress[c.id]);

  const greeting = useMemo(() => {
    const h = new Date().getHours();
    return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  }, []);
  const who = isAllTenants ? "" : tenant ? `, ${tenant.name}` : "";
  const sampleNote = callsTruncated ? " · based on the most recent calls" : "";

  return (
    <div className="dash">
      {error && <div className="error-banner">{error}</div>}

      <div className="d-head">
        <div>
          <b>{greeting}{who}</b>
          <small>
            {loading || !stats
              ? "Loading…"
              : `Your agents handled ${fmtInt(stats.total_calls)} call${stats.total_calls === 1 ? "" : "s"} ${range.period}`}
            {stats && (
              <>
                {" · "}<span className="live-dot" />{fmtInt(stats.live_calls)} live now
              </>
            )}
          </small>
        </div>
        <div className="d-head-right">
          <LiveStatus updatedAt={updatedAt} />
          <div className="d-seg" role="group" aria-label="Time range">
            {RANGE_OPTIONS.map((o) => (
              <button
                key={o.label}
                type="button"
                className={range.label === o.label ? "on" : ""}
                aria-pressed={range.label === o.label}
                onClick={() => setRange(o)}
              >
                {o.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="d-row r4">
        <div className="d-card">
          <div className="kpi-l">Calls</div>
          <div className="kpi-v">{stats ? fmtInt(stats.total_calls) : "—"}</div>
          <div className={`kpi-d ${(kpi?.callsDeltaPct ?? 0) >= 0 ? "up" : "down"}`}>
            {kpi?.callsDeltaPct != null
              ? `${kpi.callsDeltaPct >= 0 ? "▲" : "▼"} ${Math.abs(kpi.callsDeltaPct).toFixed(0)}% vs ${range.prev}`
              : <span className="muted">No earlier data to compare</span>}
          </div>
          {stats && (
            <div className="kpi-d muted">
              {fmtInt(stats.inbound_count)} incoming · {fmtInt(stats.outbound_count)} outgoing
              {stats.total_calls > stats.inbound_count + stats.outbound_count &&
                ` · ${fmtInt(stats.total_calls - stats.inbound_count - stats.outbound_count)} test`}
            </div>
          )}
        </div>
        <div className="d-card">
          <div className="kpi-l">Handled by AI</div>
          <div className="kpi-v">
            {kpi && stats ? (kpi.aiPct != null ? `${Math.round(kpi.aiPct)}%` : stats.total_calls === 0 ? "0%" : "—") : "—"}
          </div>
          <div className={`kpi-d ${(kpi?.aiDeltaPt ?? 0) >= 0 ? "up" : "down"}`}>
            {kpi?.aiDeltaPt != null
              ? `${kpi.aiDeltaPt >= 0 ? "▲" : "▼"} ${Math.abs(kpi.aiDeltaPt).toFixed(1)} pts`
              : <span className="muted">Calls finished without a transfer</span>}
          </div>
        </div>
        <div className="d-card">
          <div className="kpi-l">Avg call length</div>
          <div className="kpi-v">
            {kpi && stats ? (kpi.aht != null ? fmtDuration(kpi.aht) : stats.total_calls === 0 ? "0s" : "—") : "—"}
          </div>
          <div className={`kpi-d ${(kpi?.ahtDeltaSec ?? 0) <= 0 ? "up" : "down"}`}>
            {kpi?.ahtDeltaSec != null && Math.round(kpi.ahtDeltaSec) !== 0
              ? `${kpi.ahtDeltaSec < 0 ? "▼" : "▲"} ${Math.abs(Math.round(kpi.ahtDeltaSec))}s ${kpi.ahtDeltaSec < 0 ? "faster" : "slower"}`
              : <span className="muted">No change</span>}
          </div>
        </div>
        <div className="d-card">
          <div className="kpi-l">Sent to a person</div>
          <div className="kpi-v">{stats ? fmtInt(stats.handoff_count) : "—"}</div>
          <div className={`kpi-d ${kpi && kpi.failedTransfers > 0 ? "down" : "muted"}`}>
            {kpi && kpi.failedTransfers > 0
              ? `${fmtInt(kpi.failedTransfers)} transfer${kpi.failedTransfers === 1 ? "" : "s"} failed`
              : "Every transfer connected"}
          </div>
        </div>
      </div>

      <div className="d-row r21">
        <div className="d-card">
          <div className="d-title">Calls over time</div>
          {loading ? <div className="d-empty">Loading…</div> : <TrendChart points={trend} />}
        </div>
        <div className="d-card">
          <div className="d-title">Needs your attention</div>
          {attention.length === 0 ? (
            <div className="d-empty">Nothing needs you right now.</div>
          ) : (
            attention.map((item) => (
              <div className="att" key={item.text}>
                <span className="dot" style={{ background: TONE_COLOR[item.tone] }} />
                <span>{item.text}</span>
                <Link href={item.href} className="act">{item.action}</Link>
              </div>
            ))
          )}
        </div>
      </div>

      <div className="d-row r3">
        <div className="d-card">
          <div className="d-title">How calls ended</div>
          {outcomes.total === 0 ? (
            <div className="d-empty">{loading ? "Loading…" : "No calls ended in this period."}</div>
          ) : (
            outcomes.rows.map(({ outcome, count }) => {
              const pct = (count / outcomes.total) * 100;
              return (
                <div className="bar-row" key={outcome.label}>
                  <div className="lbl">
                    <span>{outcome.label}</span>
                    <b>{pct < 1 ? "<1%" : `${Math.round(pct)}%`}</b>
                  </div>
                  <div className="bar"><i style={{ width: `${Math.max(pct, 1)}%`, background: OUTCOME_COLOR[outcome.key] ?? TONE_COLOR[outcome.tone] }} /></div>
                </div>
              );
            })
          )}
        </div>

        <div className="d-card">
          <div className="d-title">Caller mood</div>
          {mood.n === 0 ? (
            <div className="d-empty">{loading ? "Loading…" : "No scored calls in this period yet."}</div>
          ) : (
            <>
              <div className="mood">
                <i style={{ width: `${mood.happy}%`, background: "var(--green)" }} />
                <i style={{ width: `${mood.neutral}%`, background: "var(--surf-3)" }} />
                <i style={{ width: `${mood.unhappy}%`, background: "var(--red)" }} />
              </div>
              <div className="mood-keys">
                <div><b className="up">{mood.happy}%</b><span>Happy</span></div>
                <div><b>{mood.neutral}%</b><span>Neutral</span></div>
                <div><b className="down">{mood.unhappy}%</b><span>Unhappy</span></div>
              </div>
              <div className="d-foot">From {fmtInt(mood.n)} scored call{mood.n === 1 ? "" : "s"}{sampleNote}</div>
            </>
          )}
        </div>

        <div className="d-card">
          <div className="d-title">
            Running campaigns
            <Link href="/campaigns" className="link">View all</Link>
          </div>
          {runningCampaigns.length === 0 ? (
            <div className="d-empty">
              No campaigns running. <Link href="/campaigns?add=1" className="act">Start one</Link>
            </div>
          ) : (
            runningCampaigns.map((c) => {
              const p = progress[c.id];
              const done = p.total - p.pending - p.calling;
              return (
                <Link href={`/campaigns/${c.id}`} className="bar-row" key={c.id}>
                  <div className="lbl">
                    <span>{c.name}</span>
                    <span className="muted">{c.status === "paused" ? "Paused" : `${fmtInt(done)} / ${fmtInt(p.total)}`}</span>
                  </div>
                  <div className="bar">
                    <i style={{
                      width: `${p.total === 0 ? 0 : (done / p.total) * 100}%`,
                      background: c.status === "paused" ? "var(--text-3)" : "var(--cyan)",
                    }} />
                  </div>
                </Link>
              );
            })
          )}
        </div>
      </div>

      <div className="d-row r11">
        <div className="d-card">
          <div className="d-title">
            Your agents
            <Link href="/agents" className="link">View all</Link>
          </div>
          {agentRows.length === 0 ? (
            <div className="d-empty">No agents yet. <Link href="/agents/new" className="act">Create one</Link></div>
          ) : (
            <table className="d-tbl">
              <thead>
                <tr><th>Agent</th><th>Calls</th><th>Handled by AI</th><th>Status</th></tr>
              </thead>
              <tbody>
                {agentRows.slice(0, 5).map((r) => (
                  <tr key={r.agent.id}>
                    <td>
                      <Link href={`/agents/${r.agent.tenantSlug}/${r.agent.slug}`}>{r.agent.name}</Link>
                    </td>
                    <td>{fmtInt(r.calls)}</td>
                    <td>{r.aiPct == null ? "—" : `${r.aiPct}%`}</td>
                    <td><span className={`st ${r.status.tone}`}>{r.status.label}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="d-card">
          <div className="d-title">
            Recent calls
            <Link href="/calls" className="link">All calls</Link>
          </div>
          {calls.length === 0 ? (
            <div className="d-empty">{loading ? "Loading…" : "No calls in this period yet."}</div>
          ) : (
            <table className="d-tbl">
              <thead>
                <tr><th>Number</th><th>Type</th><th>Agent</th><th>Length</th><th>Result</th></tr>
              </thead>
              <tbody>
                {calls.slice(0, 5).map((c) => {
                  const outcome = c.ended_at ? outcomeOf(c.close_reason) : { short: "Live", tone: "g" as Tone };
                  const dir = DIRECTION_TAG[c.direction] ?? { label: c.direction, tone: "n" as Tone };
                  return (
                    <tr key={c.session_id}>
                      <td>
                        <Link href={`/calls/${c.session_id}`}>
                          {maskNumber(c.direction === "outbound" ? c.called_number : c.caller_number)}
                        </Link>
                      </td>
                      <td><span className={`st ${dir.tone}`}>{dir.label}</span></td>
                      <td>{c.agent_name ?? "—"}</td>
                      <td>{fmtDuration(c.duration_ms)}</td>
                      <td><span className={`st ${outcome.tone}`}>{outcome.short}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
