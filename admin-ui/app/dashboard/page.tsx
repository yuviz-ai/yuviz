"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  AgentWithTenant,
  ApiError,
  DashboardStats,
  DispositionSlice,
  dispositionLabel,
  LatencyStatWithTenant,
  listAllAgents,
  listAllDashboardStats,
  listAllDispositionMix,
  listAllLatencyStats,
  listAllTodaysActivity,
  listAllUsageTrend,
  listTenants,
  TodaysActivityPoint,
  Tenant,
  UsageTrendPoint,
} from "@/lib/api";

const RANGE_OPTIONS = [
  { label: "7 Days", hours: 24 * 7, days: 7 },
  { label: "30 Days", hours: 24 * 30, days: 30 },
  { label: "90 Days", hours: 24 * 90, days: 90 },
];

// Explicit "en-IN" rather than the browser default: this renders inside a
// client component that Next also prerenders on the server, and a
// locale-dependent group separator that differs between the two is a
// hydration mismatch. Indian digit grouping (1,36,650) is also what this
// product's operators read numbers in.
const fmtInt = (n: number) => n.toLocaleString("en-IN");

function fmtDuration(ms: number | null): string {
  if (ms == null) return "—";
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

// AHT's progress bar needs a ceiling to fill against. 2 minutes is the
// target the tile states out loud rather than an invisible constant, so a
// bar that looks "nearly full" always means "nearly at the stated target".
const AHT_TARGET_MS = 120_000;

type Tone = "good" | "bad" | "flat";

const TONE_COLOR: Record<Tone, string> = {
  good: "var(--green)",
  bad: "var(--red)",
  flat: "var(--text-3)",
};

/** One headline tile: label, big value, a fill bar, and a delta + context line.
    The value carries the same accent colour as its bar — the StatCard this
    replaced coloured its number per metric, and keeping that means a tile
    still reads at a glance without tracing the thin bar underneath. */
function Kpi({
  label, value, fillPct, fillColor, delta, deltaTone, footnote, live,
}: {
  label: string;
  value: string;
  fillPct: number | null;
  fillColor: string;
  delta: string | null;
  deltaTone: Tone;
  footnote: string;
  live?: boolean;
}) {
  return (
    <div className="card" style={{ padding: "14px 16px", flex: "1 1 200px", minWidth: 180 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <span style={{ fontSize: ".72rem", fontWeight: 600, color: "var(--text-2)" }}>{label}</span>
        {live && (
          <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 4, fontSize: ".64rem", color: "var(--green)" }}>
            <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--green)", display: "inline-block" }} />
            LIVE
          </span>
        )}
      </div>
      <div style={{
        fontSize: "1.75rem", fontWeight: 600, letterSpacing: "-.025em",
        color: fillColor, fontVariantNumeric: "tabular-nums", lineHeight: 1.1,
      }}>
        {value}
      </div>
      <div style={{ height: 3, borderRadius: 2, background: "var(--surf-3)", margin: "12px 0 8px" }}>
        {/* A null fill means the ratio has no denominator yet (no calls in
            the window). An empty track is honest there; a full or zeroed
            bar would both read as a real measurement. */}
        {fillPct !== null && (
          <div style={{
            width: `${Math.min(100, Math.max(0, fillPct))}%`, height: "100%",
            borderRadius: 2, background: fillColor,
          }} />
        )}
      </div>
      <div style={{ display: "flex", gap: 6, alignItems: "baseline", fontSize: ".7rem" }}>
        {delta && <span style={{ color: TONE_COLOR[deltaTone], fontWeight: 600 }}>{delta}</span>}
        <span style={{ color: "var(--text-3)" }}>{footnote}</span>
      </div>
    </div>
  );
}

/** Stacked hourly volume. Plain divs rather than SVG — every bar is a
    simple proportion of the tallest hour, and the stack only ever has two
    segments (see get_todays_activity: 'web' is hardcoded 0, not a real
    channel on this platform, so stacking it would draw a permanent
    zero-height lie into the legend). */
function StackedBars({ points }: { points: TodaysActivityPoint[] }) {
  const max = Math.max(1, ...points.map((p) => p.inbound + p.outbound));
  return (
    <div>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 6, height: 190 }}>
        {points.map((p) => {
          const total = p.inbound + p.outbound;
          return (
            <div
              key={p.hour}
              style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "flex-end", height: "100%" }}
              title={`${String(p.hour).padStart(2, "0")}:00 — ${p.outbound} outbound, ${p.inbound} inbound`}
            >
              <div style={{
                height: `${(total / max) * 100}%`, display: "flex", flexDirection: "column",
                justifyContent: "flex-end", borderRadius: "4px 4px 0 0", overflow: "hidden", minHeight: total > 0 ? 2 : 0,
              }}>
                {/* inbound=cyan / outbound=amber is the convention the Calls
                    page already sets (app/calls/page.tsx's direction badge).
                    Do not re-pick these per screen — the same colour has to
                    mean the same direction everywhere in the console. */}
                <div style={{ flex: p.inbound, background: "var(--cyan)" }} />
                <div style={{ flex: p.outbound, background: "var(--amber)" }} />
              </div>
            </div>
          );
        })}
      </div>
      <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
        {points.map((p) => (
          <span key={p.hour} style={{ flex: 1, textAlign: "center", fontSize: ".65rem", color: "var(--text-3)" }}>
            {String(p.hour).padStart(2, "0")}
          </span>
        ))}
      </div>
    </div>
  );
}

// Disposition bars are coloured by what the reason MEANS, not by rank — a
// clean caller hangup and a transport error should never read as the same
// kind of outcome just because they happen to sit next to each other.
// Returns one of globals.css's existing .badge tones rather than a raw
// colour, so these rows use the same pills as every other status in the
// console instead of a palette invented for this one card.
type BadgeTone = "green" | "amber" | "red" | "gray" | "cyan";

const BADGE_VAR: Record<BadgeTone, string> = {
  green: "var(--green)",
  amber: "var(--amber)",
  red: "var(--red)",
  gray: "var(--text-3)",
  cyan: "var(--cyan)",
};

function dispositionTone(closeReason: string): BadgeTone {
  if (closeReason.startsWith("TRANSFER")) {
    return closeReason === "TRANSFER_SUCCESS" ? "amber" : "red";
  }
  if (closeReason === "caller_hangup") return "green";
  if (closeReason === "transport_error" || closeReason === "close_timeout") return "red";
  if (closeReason === "reconciled_inactive" || closeReason === "unknown") return "gray";
  return "cyan";
}

// Rough, named bands rather than a bare number — Retell/Vapi's own
// published benchmarks cluster around 500-600ms as "good" for a managed
// voice AI platform; this project has never gotten close to that on a
// full turn (STT + LLM + tool calls + TTS all sequential today), so the
// bands are calibrated to what's actually achievable on this stack, not
// an arbitrary universal target.
function latencyBand(ms: number | null): { label: string; badge: string } {
  if (ms == null) return { label: "—", badge: "gray" };
  if (ms < 1500) return { label: "Good", badge: "green" };
  if (ms < 3500) return { label: "Slow", badge: "amber" };
  return { label: "Very slow", badge: "red" };
}

function formatMs(ms: number | null): string {
  if (ms == null) return "—";
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

// A small dependency-free multi-series line chart — this is an internal
// admin tool with no charting library installed; two call sites (Usage
// Trends, Today's Activity) share this rather than each hand-rolling SVG.
// Each series is normalized to ITS OWN max, not a shared scale — Calls and
// Minutes (or Inbound/Outbound/Web) are different units, and letting one
// flatten to invisible near the x-axis because another series is 100x
// larger would be misleading, not honest.
function LineChart({
  series, xLabels, height = 160,
}: {
  series: { name: string; color: string; values: number[] }[];
  xLabels: string[];
  height?: number;
}) {
  const width = 100; // percentage-based viewBox, scales via SVG width=100%
  const n = xLabels.length;
  if (n === 0) return <div className="empty-state">No data in this window yet.</div>;

  const points = (values: number[]) => {
    const max = Math.max(1, ...values);
    return values.map((v, i) => {
      const x = n === 1 ? width / 2 : (i / (n - 1)) * width;
      const y = height - (v / max) * (height - 20) - 10;
      return { x, y };
    });
  };

  const xTickIdx = n <= 6 ? xLabels.map((_, i) => i) : [0, Math.floor((n - 1) / 2), n - 1];

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} preserveAspectRatio="none" style={{ overflow: "visible" }}>
        {[0.25, 0.5, 0.75].map((f) => (
          <line key={f} x1={0} x2={width} y1={height * f} y2={height * f} stroke="var(--border)" strokeWidth={0.3} />
        ))}
        {series.map((s) => {
          const pts = points(s.values);
          const path = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`).join(" ");
          return (
            <g key={s.name}>
              <path d={path} fill="none" stroke={s.color} strokeWidth={0.6} vectorEffect="non-scaling-stroke" />
              {pts.map((p, i) => (
                <circle key={i} cx={p.x} cy={p.y} r={0.8} fill={s.color} />
              ))}
            </g>
          );
        })}
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4 }}>
        {xTickIdx.map((i) => (
          <span key={i} style={{ fontSize: ".65rem", color: "var(--text-3)" }}>
            {xLabels[i]}
          </span>
        ))}
      </div>
      <div style={{ display: "flex", gap: 16, marginTop: 10 }}>
        {series.map((s) => (
          <div key={s.name} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: ".72rem", color: "var(--text-2)" }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%", background: s.color, display: "inline-block" }} />
            {s.name}
          </div>
        ))}
      </div>
    </div>
  );
}

export default function DashboardPage() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [range, setRange] = useState(RANGE_OPTIONS[1]); // 30 days default
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [statsLoading, setStatsLoading] = useState(true);

  const [trend, setTrend] = useState<UsageTrendPoint[]>([]);
  const [trendLoading, setTrendLoading] = useState(true);

  const [activity, setActivity] = useState<TodaysActivityPoint[]>([]);
  const [activityLoading, setActivityLoading] = useState(true);

  const [latencyStats, setLatencyStats] = useState<LatencyStatWithTenant[]>([]);
  const [latencyLoading, setLatencyLoading] = useState(true);
  const [latencyHours, setLatencyHours] = useState(24);

  const [dispositions, setDispositions] = useState<DispositionSlice[]>([]);
  const [dispositionsLoading, setDispositionsLoading] = useState(true);

  useEffect(() => {
    listTenants().then(setTenants).catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  useEffect(() => {
    if (tenants.length === 0) return;
    listAllAgents(tenants).then(setAgents).catch(() => {});
  }, [tenants]);

  useEffect(() => {
    if (tenants.length === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStatsLoading(true);
    listAllDashboardStats(tenants, range.hours)
      .then(setStats)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setStatsLoading(false));
  }, [tenants, range]);

  useEffect(() => {
    if (tenants.length === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setTrendLoading(true);
    listAllUsageTrend(tenants, range.days)
      .then(setTrend)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setTrendLoading(false));
  }, [tenants, range]);

  useEffect(() => {
    if (tenants.length === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setActivityLoading(true);
    listAllTodaysActivity(tenants)
      .then(setActivity)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setActivityLoading(false));
  }, [tenants]);

  useEffect(() => {
    if (tenants.length === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setDispositionsLoading(true);
    listAllDispositionMix(tenants, range.hours)
      .then(setDispositions)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setDispositionsLoading(false));
  }, [tenants, range]);

  useEffect(() => {
    if (tenants.length === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLatencyLoading(true);
    listAllLatencyStats(tenants, latencyHours)
      .then(setLatencyStats)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLatencyLoading(false));
  }, [tenants, latencyHours]);

  const activeAgents = agents.filter((a) => a.status === "active").length;

  // Every headline number is derived here from raw counts rather than read
  // off the API, so a zero denominator stays null (rendered "—") instead of
  // turning into NaN%, 0% or Infinity. With the window set to 90 days on a
  // fresh install all four of these are legitimately null.
  const kpi = useMemo(() => {
    const pctChange = (cur: number, prev: number): number | null =>
      prev === 0 ? null : ((cur - prev) / prev) * 100;
    const ratePct = (num: number, den: number): number | null =>
      den === 0 ? null : (num / den) * 100;
    const mean = (sum: number, n: number): number | null => (n === 0 ? null : sum / n);

    if (!stats) return null;

    const containment = ratePct(stats.ended_count - stats.escalated_count, stats.ended_count);
    const prevContainment = ratePct(
      stats.prev_ended_count - stats.prev_escalated_count, stats.prev_ended_count,
    );
    const aht = mean(stats.aht_duration_ms, stats.aht_sample_count);
    const prevAht = mean(stats.prev_aht_duration_ms, stats.prev_aht_sample_count);

    return {
      containment,
      // Percentage POINTS, not a percent-of-a-percent — 80% to 83% is
      // "+3.0pt", never "+3.75%".
      containmentDeltaPt: containment !== null && prevContainment !== null
        ? containment - prevContainment : null,
      aht,
      ahtDeltaSec: aht !== null && prevAht !== null ? (aht - prevAht) / 1000 : null,
      callsDeltaPct: pctChange(stats.total_calls, stats.prev_total_calls),
      handoffDeltaPct: pctChange(stats.handoff_count, stats.prev_handoff_count),
      // Share of started calls that actually reached an ended state — the
      // closest honest analogue to "handled of attempted", since nothing in
      // the schema records a dial attempt that never became a call row.
      handledPct: ratePct(stats.ended_count, stats.total_calls),
      handoffPct: ratePct(stats.handoff_count, stats.ended_count),
    };
  }, [stats]);

  const signed = (n: number, unit: string, digits = 1) =>
    `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(digits)}${unit}`;

  const dispositionTotal = dispositions.reduce((sum, d) => sum + d.count, 0);

  const trendSeries = useMemo(
    () => [
      { name: "Calls", color: "var(--cyan)", values: trend.map((p) => p.calls) },
      { name: "Minutes", color: "var(--indigo)", values: trend.map((p) => p.minutes) },
    ],
    [trend],
  );
  const trendLabels = trend.map((p) => new Date(p.date).toLocaleDateString(undefined, { month: "short", day: "numeric" }));

  const todayLabel = new Date().toLocaleDateString("en-IN", {
    weekday: "long", day: "numeric", month: "short",
  });

  // The API only returns hours that had at least one call, so a quiet hour
  // comes back missing rather than zero. Rendering that raw would silently
  // close the gap and draw 14:00 flush against 16:00 as if 15:00 never
  // existed — the dead hour is exactly what an operator is looking for.
  // Filled between the first and last active hour only; padding out to a
  // full 00-23 would bury a short business window in empty columns.
  const hourly = useMemo(() => {
    if (activity.length === 0) return [];
    const byHour = new Map(activity.map((p) => [p.hour, p]));
    const hours = activity.map((p) => p.hour);
    const dense: TodaysActivityPoint[] = [];
    for (let h = Math.min(...hours); h <= Math.max(...hours); h++) {
      dense.push(byHour.get(h) ?? { hour: h, inbound: 0, outbound: 0, web: 0 });
    }
    return dense;
  }, [activity]);

  return (
    <>
      {error && <div className="error-banner">{error}</div>}

      <div style={{
        display: "flex", alignItems: "flex-start", justifyContent: "space-between",
        gap: 16, flexWrap: "wrap", marginBottom: 18,
      }}>
        <div>
          <h1 style={{ fontSize: "1.55rem", fontWeight: 600, letterSpacing: "-.025em", margin: 0, color: "var(--text)" }}>
            Today across {tenants.length === 0 ? "your accounts" : `${tenants.length} account${tenants.length === 1 ? "" : "s"}`}
          </h1>
          {/* The date is computed from the viewer's clock, which need not
              match the prerender host's — suppressed rather than deferred to
              an effect so the line does not pop in after paint. */}
          <div suppressHydrationWarning style={{ fontSize: ".8rem", color: "var(--text-2)", marginTop: 4 }}>
            {todayLabel} · last {range.label.toLowerCase()} ·{" "}
            {statsLoading ? "…" : `${fmtInt(stats?.live_calls ?? 0)} live now`} ·{" "}
            {fmtInt(activeAgents)} active agent{activeAgents === 1 ? "" : "s"}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <div style={{ display: "flex", gap: 4, marginRight: 4 }}>
            {RANGE_OPTIONS.map((o) => (
              <button
                key={o.label}
                className={`btn btn-sm ${range.label === o.label ? "btn-primary" : "btn-ghost"}`}
                onClick={() => setRange(o)}
              >
                {o.label}
              </button>
            ))}
          </div>
          <Link href="/calls" className="btn">Open live monitor</Link>
          <Link href="/campaigns" className="btn btn-primary">New campaign</Link>
        </div>
      </div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginBottom: 16 }}>
        <Kpi
          label="Calls handled"
          value={statsLoading || !stats ? "—" : fmtInt(stats.ended_count)}
          fillPct={kpi?.handledPct ?? null}
          fillColor="var(--cyan)"
          delta={kpi?.callsDeltaPct != null ? signed(kpi.callsDeltaPct, "%") : null}
          deltaTone={(kpi?.callsDeltaPct ?? 0) >= 0 ? "good" : "bad"}
          footnote={stats ? `of ${fmtInt(stats.total_calls)} started` : "no calls yet"}
          // The old Live Calls StatCard owned this pulse; that tile is gone,
          // so the signal rides the volume tile rather than disappearing.
          live={(stats?.live_calls ?? 0) > 0}
        />
        <Kpi
          label="Containment"
          value={kpi?.containment == null ? "—" : `${Math.round(kpi.containment)}%`}
          fillPct={kpi?.containment ?? null}
          fillColor="var(--green)"
          delta={kpi?.containmentDeltaPt != null ? signed(kpi.containmentDeltaPt, "pt") : null}
          deltaTone={(kpi?.containmentDeltaPt ?? 0) >= 0 ? "good" : "bad"}
          footnote="resolved without a human"
        />
        <Kpi
          label="Avg handle time"
          value={fmtDuration(kpi?.aht ?? null)}
          fillPct={kpi?.aht != null ? (kpi.aht / AHT_TARGET_MS) * 100 : null}
          fillColor={kpi?.aht != null && kpi.aht > AHT_TARGET_MS ? "var(--red)" : "var(--amber)"}
          // Faster is better, so a negative delta is the good one — the only
          // tile where the sign/tone mapping inverts.
          delta={kpi?.ahtDeltaSec != null ? signed(kpi.ahtDeltaSec, "s", 0) : null}
          deltaTone={(kpi?.ahtDeltaSec ?? 0) <= 0 ? "good" : "bad"}
          footnote={stats && stats.aht_sample_count < stats.ended_count
            ? `${fmtInt(stats.aht_sample_count)} of ${fmtInt(stats.ended_count)} calls report duration`
            : "target under 2m"}
        />
        <Kpi
          label="Human handoffs"
          value={statsLoading || !stats ? "—" : fmtInt(stats.handoff_count)}
          fillPct={kpi?.handoffPct ?? null}
          // Amber, not red, and the same amber dispositionTone() gives
          // TRANSFER_SUCCESS below: a handoff is an escalation worth
          // watching, not a failure. Failed transfers are the red ones.
          fillColor="var(--amber)"
          delta={kpi?.handoffDeltaPct != null ? signed(kpi.handoffDeltaPct, "%") : null}
          deltaTone={(kpi?.handoffDeltaPct ?? 0) <= 0 ? "good" : "bad"}
          footnote={stats && stats.escalated_count > stats.handoff_count
            ? `${fmtInt(stats.escalated_count - stats.handoff_count)} transfer${stats.escalated_count - stats.handoff_count === 1 ? "" : "s"} failed`
            : "reached a human agent"}
        />
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-hdr">
          <div className="card-title">Usage Trends</div>
          <div className="card-sub">Last {range.days} days</div>
        </div>
        <div style={{ padding: 16 }}>
          {trendLoading ? <div className="empty-state">Loading…</div> : <LineChart series={trendSeries} xLabels={trendLabels} />}
        </div>
      </div>

      <div style={{ display: "flex", gap: 16, marginBottom: 16, flexWrap: "wrap" }}>
        <div className="card" style={{ flex: "2 1 420px" }}>
          <div className="card-hdr">
            <div className="card-title">Call volume by hour</div>
            <div className="card-sub">Today</div>
            <div style={{ marginLeft: "auto", display: "flex", gap: 14 }}>
              {[
                { name: "Inbound", color: "var(--cyan)" },
                { name: "Outbound", color: "var(--amber)" },
              ].map((s) => (
                <span key={s.name} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: ".72rem", color: "var(--text-2)" }}>
                  <span style={{ width: 8, height: 8, borderRadius: 2, background: s.color, display: "inline-block" }} />
                  {s.name}
                </span>
              ))}
            </div>
          </div>
          <div style={{ padding: 16 }}>
            {activityLoading ? (
              <div className="empty-state">Loading…</div>
            ) : activity.length === 0 ? (
              <div className="empty-state">No calls yet today.</div>
            ) : (
              <StackedBars points={hourly} />
            )}
          </div>
        </div>

        <div className="card" style={{ flex: "1 1 300px" }}>
          <div className="card-hdr">
            <div className="card-title">Disposition mix</div>
            <div className="card-sub">
              {dispositionsLoading ? "Loading…" : `${fmtInt(dispositionTotal)} ended call${dispositionTotal === 1 ? "" : "s"}`}
            </div>
          </div>
          <div style={{ padding: 16 }}>
            {dispositionsLoading ? (
              <div className="empty-state">Loading…</div>
            ) : dispositions.length === 0 ? (
              <div className="empty-state">No calls have ended in this window yet.</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                {dispositions.map((d) => {
                  const pct = (d.count / dispositionTotal) * 100;
                  const tone = dispositionTone(d.close_reason);
                  return (
                    <div key={d.close_reason}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, marginBottom: 6 }}>
                        <span className={`badge ${tone}`}>{dispositionLabel(d.close_reason)}</span>
                        <span style={{ fontSize: ".8rem", color: "var(--text-2)", fontVariantNumeric: "tabular-nums" }}>
                          {/* Sub-1% slices round to "<1%" rather than "0%",
                              which would contradict the row existing at all. */}
                          {pct < 1 ? "<1%" : `${Math.round(pct)}%`}
                        </span>
                      </div>
                      <div style={{ height: 4, borderRadius: 2, background: "var(--surf-3)" }}>
                        <div style={{
                          width: `${Math.max(pct, 1)}%`, height: "100%", borderRadius: 2,
                          background: BADGE_VAR[tone],
                        }} />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Voice Latency</div>
          <div className="card-sub">
            {latencyLoading ? "Loading…" : `${latencyStats.length} agent/engine combination${latencyStats.length === 1 ? "" : "s"}`}
          </div>
          <select
            className="form-select"
            style={{ marginLeft: 12, width: 150, padding: "3px 8px", fontSize: ".72rem" }}
            value={latencyHours}
            onChange={(e) => setLatencyHours(Number(e.target.value))}
          >
            <option value={24}>Last 24 hours</option>
            <option value={24 * 7}>Last 7 days</option>
            <option value={24 * 30}>Last 30 days</option>
          </select>
        </div>
        {latencyLoading ? (
          <div className="empty-state">Loading…</div>
        ) : latencyStats.length === 0 ? (
          <div className="empty-state">No turns with recorded latency in this window yet.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Account</th>
                <th>Agent</th>
                <th>LLM Engine</th>
                <th>Turns</th>
                <th>p50 STT</th>
                <th>p50 LLM</th>
                <th>p50 TTS</th>
                <th>p50 Voice-to-Voice</th>
                <th>p95 Voice-to-Voice</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {latencyStats.map((s, i) => {
                const band = latencyBand(s.p50_voice_to_voice_ms);
                return (
                  <tr key={`${s.agent_id}-${s.llm_engine}-${i}`}>
                    <td>{s.tenantName}</td>
                    <td>{s.agent_name || "—"}</td>
                    <td className="mono">{s.llm_engine || "—"}</td>
                    <td>{s.sample_count}</td>
                    <td className="mono">{formatMs(s.p50_stt_ms)}</td>
                    <td className="mono">{formatMs(s.p50_llm_ms)}</td>
                    <td className="mono">{formatMs(s.p50_tts_ms)}</td>
                    <td className="mono" style={{ fontWeight: 600 }}>{formatMs(s.p50_voice_to_voice_ms)}</td>
                    <td className="mono">{formatMs(s.p95_voice_to_voice_ms)}</td>
                    <td>
                      <span className={`badge ${band.badge}`}>{band.label}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
