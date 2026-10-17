import { AgentCallStats } from "@/lib/api";

export type CallStats = Omit<AgentCallStats, "agent_id">;

export const NO_CALLS: CallStats = { calls: 0, tests: 0, avg_duration_ms: null, finished: 0, failed: 0 };

const fmtDuration = (ms: number) => {
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

/** Sums several agents' totals; the average is weighted by each agent's call count. */
export function sumCallStats(list: CallStats[]): CallStats {
  const timed = list.filter((s) => s.avg_duration_ms != null && s.calls > 0);
  const timedCalls = timed.reduce((n, s) => n + s.calls, 0);
  return {
    calls: list.reduce((n, s) => n + s.calls, 0),
    tests: list.reduce((n, s) => n + s.tests, 0),
    finished: list.reduce((n, s) => n + s.finished, 0),
    failed: list.reduce((n, s) => n + s.failed, 0),
    avg_duration_ms: timedCalls
      ? Math.round(timed.reduce((n, s) => n + s.avg_duration_ms! * s.calls, 0) / timedCalls)
      : null,
  };
}

export function CallStatsGrid({ stats }: { stats: CallStats }) {
  // Same rule as the calls page: only transfer-failed and dropped calls count as failed.
  const pct = stats.finished ? Math.round((100 * (stats.finished - stats.failed)) / stats.finished) : null;
  return (
    <div className="agent-card-stats">
      <div><b>{stats.calls}</b><span>Calls</span></div>
      <div><b>{stats.tests}</b><span>Tests</span></div>
      <div><b>{stats.avg_duration_ms == null ? "—" : fmtDuration(stats.avg_duration_ms)}</b><span>Avg length</span></div>
      <div>
        <b className={pct == null ? "" : pct >= 80 ? "good" : pct >= 50 ? "warn" : "bad"}>
          {pct == null ? "—" : `${pct}%`}
        </b>
        <span>Success</span>
      </div>
    </div>
  );
}
