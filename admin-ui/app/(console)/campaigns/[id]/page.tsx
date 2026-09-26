"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  Agent,
  ApiError,
  Campaign,
  CampaignContact,
  CampaignProgress,
  CampaignStatus,
  getAgent,
  getCampaign,
  getCampaignProgress,
  listCampaignContacts,
  listTenants,
  pauseCampaign,
  resumeCampaign,
  startCampaign,
  Tenant,
  uploadCampaignContacts,
} from "@/lib/api";

function statusBadgeClass(status: CampaignStatus): string {
  switch (status) {
    case "running":
      return "green";
    case "paused":
      return "amber";
    case "completed":
      return "cyan";
    default:
      return "gray";
  }
}

function contactBadgeClass(status: CampaignContact["status"]): string {
  switch (status) {
    case "completed":
      return "green";
    case "calling":
      return "amber";
    case "failed":
    case "no_answer":
      return "red";
    case "blocked":
      return "indigo";
    default:
      return "gray";
  }
}

export default function CampaignDetailPage() {
  const { id } = useParams<{ id: string }>();

  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [agent, setAgent] = useState<Agent | null>(null);
  const [progress, setProgress] = useState<CampaignProgress | null>(null);
  const [contacts, setContacts] = useState<CampaignContact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadNotice, setUploadNotice] = useState<string | null>(null);
  const [actionSubmitting, setActionSubmitting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    getCampaign(id)
      .then(async (c) => {
        setCampaign(c);
        const [tenants, p, cs] = await Promise.all([
          listTenants(),
          getCampaignProgress(c.id),
          listCampaignContacts(c.id),
        ]);
        setProgress(p);
        setContacts(cs);
        const t = tenants.find((t) => t.id === c.tenant_id) || null;
        setTenant(t);
        if (t) {
          const a = await getAgent(t.slug, c.agent_id).catch(() => null);
          setAgent(a);
        }
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(load, [id]);

  const handleUpload = async (file: File) => {
    if (!campaign) return;
    setUploading(true);
    setError(null);
    setUploadNotice(null);
    try {
      const result = await uploadCampaignContacts(campaign.id, file);
      setUploadNotice(
        result.skipped_dnc > 0
          ? `Added ${result.inserted} contact${result.inserted === 1 ? "" : "s"} — skipped ${result.skipped_dnc} on the do-not-call list.`
          : `Added ${result.inserted} contact${result.inserted === 1 ? "" : "s"}.`,
      );
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const runAction = async (action: (id: string) => Promise<Campaign>) => {
    if (!campaign) return;
    setActionSubmitting(true);
    setError(null);
    try {
      const updated = await action(campaign.id);
      setCampaign(updated);
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setActionSubmitting(false);
    }
  };

  if (loading && !campaign) return <div className="empty-state">Loading…</div>;

  if (!campaign) {
    return (
      <>
        <Link href="/campaigns" style={{ color: "var(--text-3)", fontSize: ".8rem" }}>
          ← Campaigns
        </Link>
        <div className="error-banner" style={{ marginTop: 12 }}>{error || "Campaign not found."}</div>
      </>
    );
  }

  return (
    <>
      <div style={{ marginBottom: 18 }}>
        <Link href="/campaigns" style={{ color: "var(--text-3)", fontSize: ".8rem" }}>
          ← Campaigns
        </Link>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 6, flexWrap: "wrap" }}>
          <h1 style={{ fontSize: "1.4rem", fontWeight: 700, color: "var(--text)", margin: 0 }}>{campaign.name}</h1>
          <span className={`badge ${statusBadgeClass(campaign.status)}`}>{campaign.status}</span>
        </div>
        <div style={{ fontSize: ".8rem", color: "var(--text-3)", marginTop: 2 }}>
          {tenant?.name || campaign.tenant_id} · {agent?.name || campaign.agent_id}
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-hdr">
          <div className="card-title">Settings</div>
          <div className="card-sub" style={{ display: "flex", gap: 8 }}>
            {campaign.status === "draft" && (
              <button
                className="btn btn-primary btn-sm"
                disabled={actionSubmitting || !campaign.caller_id}
                title={!campaign.caller_id ? "Set a caller ID before starting" : undefined}
                onClick={() => runAction(startCampaign)}
              >
                Start
              </button>
            )}
            {campaign.status === "running" && (
              <button className="btn btn-ghost btn-sm" disabled={actionSubmitting} onClick={() => runAction(pauseCampaign)}>
                Pause
              </button>
            )}
            {campaign.status === "paused" && (
              <button className="btn btn-primary btn-sm" disabled={actionSubmitting} onClick={() => runAction(resumeCampaign)}>
                Resume
              </button>
            )}
          </div>
        </div>
        <div style={{ padding: 16, display: "flex", gap: 20, flexWrap: "wrap" }}>
          <span>
            Caller ID <strong className="mono" style={{ color: "var(--text)" }}>{campaign.caller_id || "not set"}</strong>
          </span>
          <span>
            Pacing <strong style={{ color: "var(--text)" }}>{campaign.pacing_seconds}s gap</strong>
          </span>
          <span>
            Concurrency <strong style={{ color: "var(--text)" }}>{campaign.max_concurrent_calls}</strong>
          </span>
          <span>
            Max attempts <strong style={{ color: "var(--text)" }}>{campaign.max_attempts}</strong>
          </span>
          <span>
            Calling hours{" "}
            <strong style={{ color: "var(--text)" }}>
              {campaign.calling_hours_start && campaign.calling_hours_end
                ? `${campaign.calling_hours_start}–${campaign.calling_hours_end} (${campaign.calling_hours_timezone})`
                : "any time"}
            </strong>
          </span>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-hdr">
          <div className="card-title">Progress</div>
        </div>
        <div style={{ padding: 16 }}>
          {progress ? (
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <span>Total <strong style={{ color: "var(--text)" }}>{progress.total}</strong></span>
              <span><span className="badge gray">pending</span> {progress.pending}</span>
              <span><span className="badge amber">calling</span> {progress.calling}</span>
              <span><span className="badge green">completed</span> {progress.completed}</span>
              <span><span className="badge red">failed</span> {progress.failed}</span>
              <span><span className="badge red">no_answer</span> {progress.no_answer}</span>
              <span><span className="badge indigo">blocked</span> {progress.blocked}</span>
            </div>
          ) : (
            <div className="empty-state">Loading…</div>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Contacts</div>
          <label className="btn btn-ghost btn-sm" style={{ cursor: "pointer", marginLeft: "auto" }}>
            {uploading ? "Uploading…" : "Upload CSV"}
            <input
              ref={fileInputRef}
              type="file"
              accept=".csv"
              style={{ display: "none" }}
              disabled={uploading}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) handleUpload(file);
              }}
            />
          </label>
        </div>
        <div style={{ padding: 16 }}>
          <div className="form-hint" style={{ marginBottom: 8 }}>
            CSV with a <code>phone_number</code> column (and optional <code>name</code>). Numbers on the do-not-call list are skipped automatically.
          </div>
          {uploadNotice && <div className="form-hint" style={{ marginBottom: 8, color: "var(--green)" }}>{uploadNotice}</div>}
          {contacts.length === 0 ? (
            <div className="empty-state">No contacts uploaded yet.</div>
          ) : (
            <div style={{ maxHeight: 400, overflowY: "auto" }}>
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Phone</th>
                    <th>Name</th>
                    <th>Status</th>
                    <th>Attempts</th>
                    <th>Call</th>
                  </tr>
                </thead>
                <tbody>
                  {contacts.map((ct) => (
                    <tr key={ct.id}>
                      <td className="mono">{ct.phone_number}</td>
                      <td>{ct.name || "—"}</td>
                      <td>
                        <span className={`badge ${contactBadgeClass(ct.status)}`}>{ct.status}</span>
                      </td>
                      <td>{ct.attempt_count}</td>
                      <td className="mono" style={{ fontSize: ".68rem" }} title={ct.call_session_id || undefined}>
                        {ct.call_session_id ? `${ct.call_session_id.slice(0, 8)}…` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
