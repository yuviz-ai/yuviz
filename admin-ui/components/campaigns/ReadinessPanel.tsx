"use client";

import { CampaignProgress, CampaignReadiness, CostEstimate } from "@/lib/api";

const WAIT_REASONS: Record<string, string> = {
  campaign_cap: "the campaign is at its own concurrent-call limit",
  tenant_cap: "the account is at its outbound call cap",
  platform_cap: "the platform is at its outbound call cap",
  trunk_cps: "the SIP trunk is at its calls-per-second limit",
  provider_cps: "the carrier account is at its calls-per-second limit",
  calling_hours: "it is outside the campaign's calling hours",
  pacing: "waiting out the pacing gap between calls",
  registry: "the do-not-call registry could not be checked",
  insufficient_credit: "the account has insufficient credit",
  budget_reached: "the campaign budget has been reached",
  billing_unavailable: "billing could not be reached",
};

// Targets on the detail page; an unknown field, or a viewer (no write controls), gets text without a link.
const FIELD_TARGETS: Record<string, string> = {
  caller_id: "campaign-agent",
  max_concurrent_calls: "campaign-field-max_concurrent_calls",
  calling_hours_timezone: "campaign-field-calling_hours_timezone",
  contacts: "campaign-upload-csv",
  agent_id: "campaign-agent",
};

function money(minor: number, currency: string): string {
  const fmt = new Intl.NumberFormat(undefined, { style: "currency", currency });
  return fmt.format(minor / 10 ** (fmt.resolvedOptions().maximumFractionDigits ?? 2));
}

function CostLine({ estimate }: { estimate: CostEstimate }) {
  return (
    <div className="form-hint" style={{ marginTop: 8 }}>
      Estimated cost: {money(estimate.per_call_minor, estimate.currency)} per contact,{" "}
      {money(estimate.total_minor, estimate.currency)} for the campaign.
    </div>
  );
}

function timeSince(iso: string): string {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h`;
}

interface Props {
  readiness: CampaignReadiness | null;
  progress: CampaignProgress | null;
  canManage: boolean;
}

export function ReadinessPanel({ readiness, progress, canManage }: Props) {
  const checks = readiness?.checks ?? [];
  const waiting = progress?.wait_reason ? progress : null;
  if (checks.length === 0 && !waiting && !readiness?.estimate) return null;

  const focusField = (field: string) => document.getElementById(FIELD_TARGETS[field])?.focus();
  const linkable = (field: string) => field in FIELD_TARGETS && (canManage || field === "agent_id");

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="card-hdr">
        <div className="card-title">Readiness</div>
      </div>
      <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 6 }}>
        {checks.map((c, i) => (
          <div key={`${c.id}-${i}`} style={{ color: c.level === "block" ? "var(--red)" : "var(--amber)", fontSize: ".85rem" }}>
            <span className={`badge ${c.level === "block" ? "red" : "amber"}`}>{c.level === "block" ? "blocked" : "warning"}</span>{" "}
            {linkable(c.field) ? (
              <button
                type="button"
                onClick={() => focusField(c.field)}
                style={{ all: "unset", cursor: "pointer", textDecoration: "underline" }}
              >
                {c.message}
              </button>
            ) : (
              c.message
            )}
          </div>
        ))}
        {waiting && (
          <div style={{ fontSize: ".85rem", color: "var(--text-2)" }}>
            <span className="badge amber">waiting</span>{" "}
            {WAIT_REASONS[waiting.wait_reason!] ?? waiting.wait_reason}
            {" — "}
            {waiting.waiting_count} contact{waiting.waiting_count === 1 ? "" : "s"} due
            {waiting.wait_since ? `, for ${timeSince(waiting.wait_since)}` : ""}, limit {waiting.effective_limit}.
          </div>
        )}
        {readiness?.estimate && <CostLine estimate={readiness.estimate} />}
      </div>
    </div>
  );
}
