"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, CampaignCallAttempt, CampaignCallOutcome, recallCampaign } from "@/lib/api";
import { Modal } from "@/components/Modal";

export const CAMPAIGN_OUTCOME_LABEL: Record<CampaignCallOutcome, string> = {
  answered: "Answered",
  ended: "Answered",
  no_answer: "No answer",
  busy: "Busy",
  declined: "Declined",
  failed: "Failed",
  blocked: "Blocked",
  in_progress: "In progress",
};

export const CAMPAIGN_OUTCOME_TONE: Record<CampaignCallOutcome, string> = {
  answered: "green",
  ended: "green",
  no_answer: "amber",
  busy: "amber",
  declined: "red",
  failed: "red",
  blocked: "gray",
  in_progress: "cyan",
};

const when = (iso: string) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

export function CampaignCallDetail({ attempt, onClose }: { attempt: CampaignCallAttempt; onClose: () => void }) {
  const router = useRouter();
  const [recalling, setRecalling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const outcome: CampaignCallOutcome = attempt.outcome ?? "in_progress";

  const callAgain = async () => {
    setRecalling(true);
    setError(null);
    try {
      const fresh = await recallCampaign(attempt.campaign_id, { contact_ids: [attempt.contact_id] });
      router.push(`/campaigns/${fresh.id}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
      setRecalling(false);
    }
  };

  return (
    <Modal
      open
      title="Campaign call"
      onClose={() => !recalling && onClose()}
      footer={
        <>
          <button className="btn btn-ghost btn-sm" disabled={recalling} onClick={onClose}>Close</button>
          {attempt.can_call_again && (
            <button className="btn btn-primary btn-sm" disabled={recalling} onClick={callAgain}>
              {recalling ? "Creating…" : "Call again"}
            </button>
          )}
        </>
      }
    >
      {error && <div className="error-banner" role="alert" style={{ marginBottom: 12 }}>{error}</div>}
      {!attempt.can_call_again && (
        <div className="form-hint" style={{ marginBottom: 12 }}>
          This lead was reached on a later try, so it isn&apos;t called again.
        </div>
      )}
      <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "8px 16px", margin: 0, fontSize: ".85rem" }}>
        <dt style={{ color: "var(--text-3)" }}>Number</dt>
        <dd className="mono" style={{ margin: 0 }}>{attempt.callee_masked}</dd>
        <dt style={{ color: "var(--text-3)" }}>Campaign</dt>
        <dd style={{ margin: 0 }}>{attempt.campaign_name}</dd>
        <dt style={{ color: "var(--text-3)" }}>Result</dt>
        <dd style={{ margin: 0 }}>
          <span className={`badge ${CAMPAIGN_OUTCOME_TONE[outcome]}`}>{CAMPAIGN_OUTCOME_LABEL[outcome]}</span>
        </dd>
        <dt style={{ color: "var(--text-3)" }}>Attempt</dt>
        <dd style={{ margin: 0 }}>{attempt.attempt}</dd>
        <dt style={{ color: "var(--text-3)" }}>Dialled</dt>
        <dd style={{ margin: 0 }}>{when(attempt.created_at)}</dd>
        {attempt.failure_reason && (
          <>
            <dt style={{ color: "var(--text-3)" }}>Reason</dt>
            <dd style={{ margin: 0 }}>{attempt.failure_reason.replace(/_/g, " ")}</dd>
          </>
        )}
      </dl>
      <p style={{ fontSize: ".78rem", color: "var(--text-3)", marginTop: 14 }}>
        Call again creates a draft campaign with this lead, using the same agent and caller ID. Review it, then start it.
      </p>
    </Modal>
  );
}
