"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  ApiError,
  UsageTrendPoint,
  getDashboardStats,
  getUsageTrend,
} from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";

// Enough history to cover the running cycle plus the one before it, which
// is the only comparison this page draws. usage-trend is day-grained, so
// 62 days always spans both months whatever today's date is.
const TREND_DAYS = 62;

// What a voice minute costs. Nothing in this platform's schema stores a
// price, a plan or an invoice (see the design note in the cycle panel), so
// the rate is the operator's own input rather than a number invented here —
// persisted per browser so the estimate survives a reload.
const RATE_STORAGE_KEY = "yuviz.billing.ratePerMinute";
const DEFAULT_RATE = 0.42;

const fmtInt = (n: number) => Math.round(n).toLocaleString("en-IN");
const fmtRupees = (n: number) =>
  `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const monthKey = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;

/** One metered line: name, value against its ceiling, a fill bar, footnote.
    A null ceiling means nothing on this platform defines one — the bar
    stays empty rather than inventing a denominator to fill against. */
function Meter({
  label, value, ceiling, color, footnote,
}: {
  label: string;
  value: number;
  ceiling: number | null;
  color: string;
  footnote: string;
}) {
  const pct = ceiling !== null && ceiling > 0 ? Math.min(100, (value / ceiling) * 100) : null;
  // Past 85% of whatever ceiling applies is the point an operator needs to
  // act on, so the bar changes colour rather than only getting longer.
  const fill = pct !== null && pct >= 85 ? "var(--amber)" : color;
  return (
    <div style={{ marginBottom: 18 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 7 }}>
        <span style={{ fontSize: ".8rem", fontWeight: 600, color: "var(--text)" }}>{label}</span>
        <span
          style={{
            marginLeft: "auto", fontFamily: "var(--mono)", fontSize: ".78rem",
            color: "var(--text-2)", fontVariantNumeric: "tabular-nums",
          }}
        >
          {fmtInt(value)}
          {ceiling !== null && <span style={{ color: "var(--text-3)" }}> / {fmtInt(ceiling)}</span>}
        </span>
      </div>
      <div style={{ height: 6, borderRadius: 3, background: "var(--surf-3)" }}>
        {pct !== null && <div style={{ width: `${pct}%`, height: "100%", borderRadius: 3, background: fill }} />}
      </div>
      <div className="form-hint" style={{ marginTop: 5 }}>{footnote}</div>
    </div>
  );
}

interface Cycle {
  minutes: number;
  calls: number;
  prevMinutes: number;
  prevCalls: number;
}

export default function BillingPage() {
  const { tenant, allTenants, isPlatformScoped, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const [cycle, setCycle] = useState<Cycle | null>(null);
  /** Calls in progress right now, summed over the selected accounts.
   *  Only this one figure is read off dashboard-stats — keeping the whole
   *  payload would leave every other field holding one arbitrary account's
   *  numbers under a heading that reads as a total. */
  const [liveCalls, setLiveCalls] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [rate, setRate] = useState<number>(DEFAULT_RATE);

  // Which accounts this page is reporting on — derived, never stored: the
  // header switcher is the single source of truth and a copy in state would
  // lag it by a render.
  const targets = useMemo(
    () => (isAllTenants ? allTenants : tenant ? [tenant] : []),
    [isAllTenants, allTenants, tenant],
  );

  // Read after mount, not in a lazy initialiser: this component is
  // prerendered on the server, where localStorage does not exist, and a
  // first client render that already used the stored rate would not match
  // the prerendered default.
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(RATE_STORAGE_KEY);
      // Number("") is 0, not NaN — an empty or blank entry has to be
      // rejected explicitly or it reads back as a free minute.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      if (stored !== null && stored.trim() !== "" && Number.isFinite(Number(stored))) setRate(Number(stored));
    } catch {
      // Private-mode/blocked storage: the rate just falls back to the
      // default each visit, which is a worse-but-safe outcome, not a crash.
    }
  }, []);

  const saveRate = (next: number) => {
    setRate(next);
    try {
      window.localStorage.setItem(RATE_STORAGE_KEY, String(next));
    } catch {
      // Same non-fatal fallback as above.
    }
  };

  useEffect(() => {
    if (tenantLoading) return;
    const picked = targets;
    if (picked.length === 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setCycle(null);
      setLiveCalls(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    let cancelled = false;
    (async () => {
      const results = await Promise.allSettled(
        picked.map(async (t) => {
          const [trend, dash] = await Promise.all([
            getUsageTrend(t.slug, TREND_DAYS),
            // live_calls filters on ended_at IS NULL and ignores this window
            // on purpose (services/config/calls.py), and it is the only field
            // read — so ask for the cheapest window, not a month of rollups.
            getDashboardStats(t.slug, 24),
          ]);
          return { trend, dash };
        }),
      );
      if (cancelled) return;

      const now = new Date();
      const thisMonth = monthKey(now);
      const prevMonth = monthKey(new Date(now.getFullYear(), now.getMonth() - 1, 1));
      const acc: Cycle = { minutes: 0, calls: 0, prevMinutes: 0, prevCalls: 0 };
      let live = 0;
      const errs: string[] = [];

      results.forEach((r, i) => {
        if (r.status !== "fulfilled") {
          errs.push(`${picked[i].name}: ${r.reason instanceof ApiError ? r.reason.detail : String(r.reason)}`);
          return;
        }
        // A trend point's date is a plain YYYY-MM-DD string, so the month is
        // its first seven characters — parsing it into a Date would re-read
        // it in the browser's timezone and move a month-boundary day into
        // the wrong cycle.
        const inMonth = (p: UsageTrendPoint, m: string) => p.date.slice(0, 7) === m;
        for (const p of r.value.trend) {
          if (inMonth(p, thisMonth)) {
            acc.minutes += p.minutes;
            acc.calls += p.calls;
          } else if (inMonth(p, prevMonth)) {
            acc.prevMinutes += p.minutes;
            acc.prevCalls += p.calls;
          }
        }
        live += r.value.dash.live_calls;
      });

      setCycle(acc);
      setLiveCalls(live);
      setError(errs.length > 0 ? errs.join("; ") : null);
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [targets, tenantLoading]);

  // A cap is per-account and nullable (max_concurrent_calls IS NULL means
  // "not configured", never "unlimited" — see live-calls/page.tsx). Summing
  // across accounts is only meaningful when every selected account has one.
  const channelCap = useMemo(() => {
    if (targets.length === 0) return null;
    if (targets.some((t) => t.max_concurrent_calls == null)) return null;
    return targets.reduce((sum, t) => sum + (t.max_concurrent_calls ?? 0), 0);
  }, [targets]);

  const voiceCost = (cycle?.minutes ?? 0) * rate;

  const cycleLabel = new Date().toLocaleDateString("en-IN", { month: "long", year: "numeric" });
  const scopeLabel = isAllTenants
    ? `${allTenants.length} account${allTenants.length === 1 ? "" : "s"}`
    : tenant?.name ?? "this account";

  return (
    <>
      {error && <div className="error-banner">{error}</div>}

      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
        <div>
          <h1 style={{ fontSize: "1.5rem", fontWeight: 600, letterSpacing: "-.025em", margin: 0, color: "var(--text)" }}>
            Billing &amp; usage
          </h1>
          {/* The cycle label comes from the viewer's clock, which need not
              match the prerender host's — suppressed rather than deferred to
              an effect so the line does not pop in after paint. */}
          <div className="form-hint" style={{ marginTop: 4 }} suppressHydrationWarning>
            {cycleLabel} cycle · metered from call records for {scopeLabel}.
            {isPlatformScoped && !isAllTenants && " Switch accounts from the header."}
          </div>
        </div>
        <Link href="/calls" className="btn btn-sm btn-ghost">Call records</Link>
      </div>

      {loading ? (
        <div className="card">
          <div className="empty-state">Loading usage…</div>
        </div>
      ) : (
        <>
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "stretch", marginBottom: 16 }}>
            <div className="card" style={{ flex: "2 1 380px", minWidth: 300 }}>
              <div className="card-hdr">
                <span className="card-title">Usage this cycle</span>
                <span className="card-sub" suppressHydrationWarning>month to date</span>
              </div>
              <div className="card-body">
                <Meter
                  label="Voice minutes"
                  value={cycle?.minutes ?? 0}
                  ceiling={cycle && cycle.prevMinutes > 0 ? cycle.prevMinutes : null}
                  color="var(--cyan)"
                  footnote={
                    cycle && cycle.prevMinutes > 0
                      ? `Measured against ${fmtInt(cycle.prevMinutes)} minutes in the previous cycle — no plan allowance is stored on this platform.`
                      : "No previous cycle to compare against yet."
                  }
                />
                <Meter
                  label="Calls handled"
                  value={cycle?.calls ?? 0}
                  ceiling={cycle && cycle.prevCalls > 0 ? cycle.prevCalls : null}
                  color="var(--cyan)"
                  footnote={
                    cycle && cycle.prevCalls > 0
                      ? `${fmtInt(cycle.prevCalls)} in the previous cycle.`
                      : "No previous cycle to compare against yet."
                  }
                />
                <Meter
                  label="Concurrent channels"
                  value={liveCalls ?? 0}
                  ceiling={channelCap}
                  color="var(--green)"
                  footnote={
                    channelCap !== null
                      ? "Live right now against the configured concurrency cap."
                      : "Live right now. No concurrency cap is configured for this account, so there is nothing to measure against."
                  }
                />
                <div style={{ borderTop: "1px solid var(--border)", paddingTop: 14, marginTop: 4 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: ".78rem", marginBottom: 6 }}>
                    <span style={{ color: "var(--text)", fontWeight: 600 }}>LLM tokens</span>
                    <span className="badge gray">not metered</span>
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: ".78rem" }}>
                    <span style={{ color: "var(--text)", fontWeight: 600 }}>TTS characters</span>
                    <span className="badge gray">not metered</span>
                  </div>
                  <div className="form-hint" style={{ marginTop: 8 }}>
                    Neither model tokens nor synthesised characters are recorded against a call anywhere in this
                    platform, so this page cannot show them. They are listed to make the gap explicit.
                  </div>
                </div>
              </div>
            </div>

            <div
              className="card"
              style={{ flex: "1 1 280px", minWidth: 260, background: "var(--sidebar)", borderColor: "transparent" }}
            >
              <div className="card-body">
                <div style={{ fontSize: ".64rem", fontWeight: 700, letterSpacing: ".1em", textTransform: "uppercase", color: "var(--sidebar-text-dim)" }}>
                  Current cycle · estimate
                </div>
                <div
                  style={{
                    fontSize: "2.1rem", fontWeight: 600, letterSpacing: "-.03em", color: "var(--sidebar-text)",
                    fontVariantNumeric: "tabular-nums", margin: "10px 0 6px",
                  }}
                >
                  {fmtRupees(voiceCost)}
                </div>
                <div style={{ fontSize: ".74rem", color: "var(--sidebar-text-dim)", lineHeight: 1.5 }}>
                  {fmtInt(cycle?.minutes ?? 0)} voice minutes at the rate below. This platform stores no plan, price or
                  invoice — the figure is arithmetic on call records, not a bill.
                </div>
                <div style={{ height: 1, background: "rgba(233,229,220,.12)", margin: "16px 0" }} />
                <label style={{ fontSize: ".7rem", fontWeight: 600, color: "var(--sidebar-text-dim)", display: "block", marginBottom: 6 }}>
                  Rate per voice minute (₹)
                </label>
                <input
                  className="form-input"
                  type="number"
                  min={0}
                  step={0.01}
                  value={rate}
                  // A cleared field parses to NaN, which propagated all the
                  // way to "₹NaN" in the headline — treated as 0 instead.
                  onChange={(e) => {
                    const next = Number(e.target.value);
                    saveRate(Number.isFinite(next) ? Math.max(0, next) : 0);
                  }}
                  style={{ background: "rgba(233,229,220,.06)", borderColor: "rgba(233,229,220,.18)", color: "var(--sidebar-text)" }}
                />
                <div style={{ marginTop: 16, display: "flex", flexDirection: "column", gap: 8 }}>
                  <CycleRow label="Voice minutes" value={fmtRupees(voiceCost)} />
                  <CycleRow label="LLM inference" value="not metered" dim />
                  <CycleRow label="TTS" value="not metered" dim />
                  <CycleRow label="STT" value="not metered" dim />
                </div>
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-hdr">
              <span className="card-title">Invoices</span>
              <span className="card-sub">none issued from this console</span>
            </div>
            <div className="empty-state" style={{ lineHeight: 1.7 }}>
              No invoices to show.
              <br />
              Invoicing lives outside this platform — there is no invoice, payment or plan record in its database, so
              this console deliberately shows nothing here rather than a placeholder statement.
            </div>
          </div>
        </>
      )}
    </>
  );
}

function CycleRow({ label, value, dim }: { label: string; value: string; dim?: boolean }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", fontSize: ".78rem" }}>
      <span style={{ color: "var(--sidebar-text-dim)" }}>{label}</span>
      <span
        style={{
          color: dim ? "var(--sidebar-text-dim)" : "var(--sidebar-text)",
          fontWeight: dim ? 400 : 600,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {value}
      </span>
    </div>
  );
}
