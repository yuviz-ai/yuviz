"use client";

import { CampaignAttempt } from "@/lib/api";

export function AttemptsList({ attempts }: { attempts: CampaignAttempt[] }) {
  if (attempts.length === 0) return <div className="form-hint">No attempts yet.</div>;
  return (
    <table className="tbl" style={{ fontSize: ".76rem" }}>
      <thead>
        <tr>
          <th>#</th>
          <th>Callee</th>
          <th>State</th>
          <th>Outcome</th>
          <th>Disposition</th>
          <th>Started</th>
        </tr>
      </thead>
      <tbody>
        {attempts.map((a) => (
          <tr key={a.id}>
            <td>{a.attempt}</td>
            <td className="mono">{a.callee_masked ?? "—"}</td>
            <td>{a.state}</td>
            <td title={a.failure_reason ?? undefined}>{a.outcome ?? "—"}</td>
            <td>{a.disposition ?? "—"}</td>
            <td>{new Date(a.created_at).toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
