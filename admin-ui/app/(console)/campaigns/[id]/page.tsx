"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import {
  Agent,
  ApiError,
  Campaign,
  CampaignAttempt,
  CampaignContact,
  CampaignProgress,
  CampaignReadiness,
  CampaignStatus,
  listAgents,
  getCampaign,
  getCampaignProgress,
  cancelCampaign,
  deleteCampaign,
  getCampaignReadiness,
  listCampaignAttempts,
  listCampaignContacts,
  listTenants,
  pauseCampaign,
  resumeCampaign,
  startCampaign,
  Tenant,
  updateCampaign,
  uploadCampaignContacts,
} from "@/lib/api";
import { AttemptsList } from "@/components/campaigns/AttemptsList";
import {
  CampaignDialingFields,
  DEFAULT_RETRY_GAP_MINUTES,
  DialingValues,
  pacingFromRate,
  rateFromPacing,
  serverFieldError,
  validateDialing,
} from "@/components/campaigns/CampaignDialingFields";
import { ReadinessPanel } from "@/components/campaigns/ReadinessPanel";
import { Modal } from "@/components/Modal";
import { TimezoneSelect } from "@/components/TimezoneSelect";
import { useConnectionAccess } from "@/lib/useConnectionAccess";

const POLL_MS = 5000;

const errText = (e: unknown) => (e instanceof ApiError ? e.detail : String(e));

function SettingsSection({ campaign, onSaved }: { campaign: Campaign; onSaved: (c: Campaign) => void }) {
  const [name, setName] = useState(campaign.name);
  const [description, setDescription] = useState(campaign.description ?? "");
  const [timezone, setTimezone] = useState(campaign.calling_hours_timezone);
  const [dialing, setDialing] = useState<DialingValues>({
    calls_per_minute: rateFromPacing(campaign.pacing_seconds),
    max_attempts: campaign.max_attempts,
    retry_gap_minutes: campaign.retry_gap_minutes ?? "",
    max_concurrent_calls: campaign.max_concurrent_calls,
  });
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const valid = Object.keys(validateDialing(dialing)).length === 0 && name.trim() !== "";

  const save = async () => {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      onSaved(
        await updateCampaign(campaign.id, {
          name: name.trim(),
          description: description.trim(),
          pacing_seconds: pacingFromRate(dialing.calls_per_minute),
          max_attempts: dialing.max_attempts,
          retry_gap_minutes: dialing.retry_gap_minutes === "" ? DEFAULT_RETRY_GAP_MINUTES : dialing.retry_gap_minutes,
          max_concurrent_calls: dialing.max_concurrent_calls,
          calling_hours_timezone: timezone,
        }),
      );
      setServerErrors({});
      setSaved(true);
    } catch (e) {
      const { field, message } = serverFieldError(e);
      if (field) setServerErrors({ [field]: message });
      else setError(message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="card-hdr">
        <div className="card-title">Edit settings</div>
      </div>
      <div style={{ padding: 16 }}>
        <div className="form-group">
          <label className="form-label" htmlFor="campaign-field-name">Name</label>
          <input id="campaign-field-name" className="form-input" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="form-group">
          <label className="form-label" htmlFor="campaign-field-description">Description</label>
          <textarea
            id="campaign-field-description"
            className="form-input"
            rows={3}
            maxLength={1000}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
        <CampaignDialingFields
          values={dialing}
          onChange={(v) => {
            setDialing(v);
            setServerErrors({});
            setSaved(false);
          }}
          errors={serverErrors}
        />
        <div className="form-group">
          <label className="form-label" htmlFor="campaign-field-calling_hours_timezone">Lead&apos;s timezone</label>
          <TimezoneSelect id="campaign-field-calling_hours_timezone" value={timezone} onChange={setTimezone} />
        </div>
        {error && <div className="error-banner" style={{ marginBottom: 8 }}>{error}</div>}
        <button className="btn btn-primary btn-sm" disabled={saving || !valid} onClick={save}>
          {saving ? "Saving…" : "Save settings"}
        </button>
        {saved && <span className="form-hint" style={{ marginLeft: 10, color: "var(--green)" }}>Saved.</span>}
      </div>
    </div>
  );
}

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
    case "busy":
    case "declined":
      return "red";
    case "blocked":
      return "indigo";
    default:
      return "gray";
  }
}

export default function CampaignDetailPage() {
  const { id } = useParams<{ id: string }>();

  const { checked, canManage } = useConnectionAccess();
  const writable = checked && canManage;

  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [agent, setAgent] = useState<Agent | null>(null);
  const [progress, setProgress] = useState<CampaignProgress | null>(null);
  const [readiness, setReadiness] = useState<CampaignReadiness | null>(null);
  const [contacts, setContacts] = useState<CampaignContact[]>([]);
  const [attempts, setAttempts] = useState<CampaignAttempt[]>([]);
  const [sectionErrors, setSectionErrors] = useState<Record<string, string>>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadNotice, setUploadNotice] = useState<string | null>(null);
  const [actionSubmitting, setActionSubmitting] = useState(false);
  const [confirming, setConfirming] = useState<"cancel" | "delete" | null>(null);
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const status = campaign?.status;
  const campaignId = campaign?.id;

  // Each section loads on its own so one 403 or 500 leaves the others rendered.
  const fetchSection = useCallback(<T,>(key: string, request: Promise<T>, set: (value: T) => void) => {
    request
      .then((value) => {
        set(value);
        setSectionErrors((prev) => ({ ...prev, [key]: "" }));
      })
      .catch((e) => setSectionErrors((prev) => ({ ...prev, [key]: errText(e) })));
  }, []);

  const refreshLive = useCallback(
    (cid: string) => {
      fetchSection("progress", getCampaignProgress(cid), (p) => {
        setProgress(p);
        setCampaign((c) => (c ? { ...c, status: p.status } : c));
      });
      fetchSection("attempts", listCampaignAttempts(cid), setAttempts);
    },
    [fetchSection],
  );

  const load = () => {
    setLoading(true);
    setError(null);
    getCampaign(id)
      .then((c) => {
        setCampaign(c);
        fetchSection("readiness", getCampaignReadiness(c.id), setReadiness);
        fetchSection("contacts", listCampaignContacts(c.id), setContacts);
        refreshLive(c.id);
        listTenants()
          .then((tenants) => {
            const t = tenants.find((t) => t.id === c.tenant_id) || null;
            setTenant(t);
            // The agent API looks agents up by slug; a campaign stores the agent's id.
            return t ? listAgents(t.slug).then((agents) => agents.find((a) => a.id === c.agent_id) ?? null) : null;
          })
          .then(setAgent)
          .catch(() => {});
      })
      .catch((e) => setError(errText(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/set-state-in-effect, react-hooks/exhaustive-deps
  useEffect(load, [id]);

  // Polls progress and attempts (never readiness, which probes telephony) only while running and visible.
  useEffect(() => {
    if (status !== "running" || !campaignId) return;
    let timer: ReturnType<typeof setInterval> | undefined;
    const start = () => {
      timer ??= setInterval(() => refreshLive(campaignId), POLL_MS);
    };
    const stop = () => {
      clearInterval(timer);
      timer = undefined;
    };
    const onVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        refreshLive(campaignId);
        start();
      }
    };
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [status, campaignId, refreshLive]);

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
      setError(errText(e));
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const blocked = readiness?.checks.some((c) => c.level === "block") ?? false;

  const confirmAndRun = async () => {
    if (!campaign || !confirming) return;
    setActionSubmitting(true);
    setError(null);
    try {
      if (confirming === "delete") {
        await deleteCampaign(campaign.id);
        router.push("/campaigns");
        return;
      }
      setCampaign(await cancelCampaign(campaign.id));
      setConfirming(null);
      load();
    } catch (e) {
      setError(errText(e));
    } finally {
      setActionSubmitting(false);
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
      if (e instanceof ApiError && e.status === 409 && e.body?.checks) setReadiness(e.body as unknown as CampaignReadiness);
      else setError(errText(e));
    } finally {
      setActionSubmitting(false);
    }
  };

  if (loading && !campaign) return <div className="empty-state">Loading…</div>;

  if (!campaign) {
    return (
      <>
        <Link href="/campaigns" className="detail-back">
          <ArrowLeft size={13} /> Campaigns
        </Link>
        <div className="error-banner" style={{ marginTop: 12 }}>{error || "Campaign not found."}</div>
      </>
    );
  }

  return (
    <>
      {confirming && (
        <Modal
          open
          title={confirming === "delete" ? "Delete this draft?" : "Cancel this campaign?"}
          onClose={() => !actionSubmitting && setConfirming(null)}
          footer={
            <>
              <button className="btn btn-ghost btn-sm" disabled={actionSubmitting} onClick={() => setConfirming(null)}>Keep it</button>
              <button className="btn btn-danger btn-sm" disabled={actionSubmitting} onClick={confirmAndRun}>
                {actionSubmitting ? "Working…" : confirming === "delete" ? "Delete draft" : "Cancel campaign"}
              </button>
            </>
          }
        >
          <p style={{ fontSize: ".85rem", color: "var(--text-2)", margin: 0 }}>
            {confirming === "delete"
              ? "The draft is removed from the list. Nothing has been called."
              : "No new calls start. Calls already in progress finish. The campaign stays in history and can't be restarted. Use Call again in Call logs to reach the same leads."}
          </p>
        </Modal>
      )}
      <div style={{ marginBottom: 18 }}>
        <Link href="/campaigns" className="detail-back">
          <ArrowLeft size={13} /> Campaigns
        </Link>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 6, flexWrap: "wrap" }}>
          <h1 style={{ fontSize: "1.4rem", fontWeight: 700, color: "var(--text)", margin: 0 }}>{campaign.name}</h1>
          <span className={`badge ${statusBadgeClass(campaign.status)}`}>{campaign.status}</span>
        </div>
        <div style={{ fontSize: ".8rem", color: "var(--text-3)", marginTop: 2 }}>
          {tenant?.name || campaign.tenant_id} ·{" "}
          {agent && tenant ? (
            <Link id="campaign-agent" href={`/agents/${tenant.slug}/${agent.slug}`} style={{ color: "var(--cyan)", fontWeight: 600, textDecoration: "none" }}>
              {agent.name}
            </Link>
          ) : (
            <span id="campaign-agent" tabIndex={-1}>{campaign.agent_id}</span>
          )}
        </div>
        {campaign.description && (
          <p style={{ fontSize: ".85rem", color: "var(--text-2)", margin: "8px 0 0", maxWidth: "65ch", whiteSpace: "pre-wrap" }}>
            {campaign.description}
          </p>
        )}
      </div>

      {error && <div className="error-banner">{error}</div>}

      {campaign.status === "paused" && (
        <div className="form-hint" style={{ marginBottom: 12 }}>
          Paused: calls already in progress finish, and no new calls are placed until the campaign is resumed.
        </div>
      )}

      {sectionErrors.readiness && <div className="form-hint" style={{ color: "var(--red)", marginBottom: 8 }}>Readiness unavailable: {sectionErrors.readiness}</div>}
      <ReadinessPanel readiness={readiness} progress={progress} canManage={writable} />

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-hdr">
          <div className="card-title">Settings</div>
          {writable && (
            <div className="card-sub" style={{ display: "flex", gap: 8 }}>
              {campaign.status === "draft" && (
                <button className="btn btn-danger btn-sm" disabled={actionSubmitting} onClick={() => setConfirming("delete")}>
                  Delete
                </button>
              )}
              {["draft", "scheduled", "running", "paused"].includes(campaign.status) && (
                <button className="btn btn-ghost btn-sm" disabled={actionSubmitting} onClick={() => setConfirming("cancel")}>
                  Cancel campaign
                </button>
              )}
              {(campaign.status === "draft" || campaign.status === "scheduled") && (
                <button
                  className="btn btn-primary btn-sm"
                  disabled={actionSubmitting || blocked}
                  title={blocked ? "Resolve the blocking checks below first" : undefined}
                  onClick={() => runAction(startCampaign)}
                >
                  {campaign.status === "scheduled" ? "Start now" : "Start"}
                </button>
              )}
              {campaign.status === "running" && (
                <button className="btn btn-ghost btn-sm" disabled={actionSubmitting} onClick={() => runAction(pauseCampaign)}>
                  Pause
                </button>
              )}
              {campaign.status === "paused" && (
                <button
                  className="btn btn-primary btn-sm"
                  disabled={actionSubmitting || blocked}
                  title={blocked ? "Resolve the blocking checks below first" : undefined}
                  onClick={() => runAction(resumeCampaign)}
                >
                  Resume
                </button>
              )}
            </div>
          )}
        </div>
        <div style={{ padding: 16, display: "flex", gap: 20, flexWrap: "wrap" }}>
          <span>
            Agent{" "}
            {agent && tenant ? (
              <Link href={`/agents/${tenant.slug}/${agent.slug}`} style={{ color: "var(--cyan)", fontWeight: 700, textDecoration: "none" }}>
                {agent.name}
              </Link>
            ) : (
              <strong style={{ color: "var(--text)" }}>…</strong>
            )}
          </span>
          <span>
            Caller ID <strong className="mono" style={{ color: "var(--text)" }}>{campaign.caller_id || "not set"}</strong>
          </span>
          {campaign.status === "scheduled" && campaign.scheduled_start_at && (
            <span>
              Starts <strong style={{ color: "var(--text)" }}>{new Date(campaign.scheduled_start_at).toLocaleString()}</strong>
            </span>
          )}
          <span>
            Calls per minute <strong style={{ color: "var(--text)" }}>{rateFromPacing(campaign.pacing_seconds)}</strong>
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

      {writable && <SettingsSection key={campaign.updated_at} campaign={campaign} onSaved={(c) => { setCampaign(c); fetchSection("readiness", getCampaignReadiness(c.id), setReadiness); }} />}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-hdr">
          <div className="card-title">Progress</div>
        </div>
        <div style={{ padding: 16 }}>
          {sectionErrors.progress && <div className="form-hint" style={{ color: "var(--red)", marginBottom: 8 }}>{sectionErrors.progress}</div>}
          {progress ? (
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <span>Total <strong style={{ color: "var(--text)" }}>{progress.total}</strong></span>
              <span><span className="badge gray">pending</span> {progress.pending}</span>
              <span><span className="badge amber">calling</span> {progress.calling}</span>
              <span><span className="badge green">completed</span> {progress.completed}</span>
              <span><span className="badge red">failed</span> {progress.failed}</span>
              <span><span className="badge red">no_answer</span> {progress.no_answer}</span>
              <span><span className="badge red">busy</span> {progress.busy}</span>
              <span><span className="badge red">declined</span> {progress.declined}</span>
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
          {writable && (
          <label id="campaign-upload-csv" tabIndex={-1} className="btn btn-ghost btn-sm" style={{ cursor: "pointer", marginLeft: "auto" }}>
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
          )}
        </div>
        <div style={{ padding: 16 }}>
          <div className="form-hint" style={{ marginBottom: 8 }}>
            CSV with a <code>phone_number</code> column (and optional <code>name</code>). Numbers on the do-not-call list are skipped automatically.
          </div>
          {uploadNotice && <div className="form-hint" style={{ marginBottom: 8, color: "var(--green)" }}>{uploadNotice}</div>}
          {sectionErrors.contacts && <div className="form-hint" style={{ color: "var(--red)", marginBottom: 8 }}>{sectionErrors.contacts}</div>}
          {sectionErrors.attempts && <div className="form-hint" style={{ color: "var(--red)", marginBottom: 8 }}>Attempts unavailable: {sectionErrors.attempts}</div>}
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
                    <Fragment key={ct.id}>
                      <tr style={{ cursor: "pointer" }} onClick={() => setExpanded(expanded === ct.id ? null : ct.id)}>
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
                      {expanded === ct.id && (
                        <tr>
                          <td colSpan={5}>
                            <AttemptsList attempts={attempts.filter((a) => a.contact_id === ct.id)} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
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
