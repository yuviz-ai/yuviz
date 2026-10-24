"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Agent, Campaign, Tenant, createCampaign, listAgents, startCampaign, uploadCampaignContacts } from "@/lib/api";
import { Modal } from "@/components/Modal";
import {
  CampaignDialingFields,
  DEFAULT_CALLS_PER_MINUTE,
  DEFAULT_RETRY_GAP_MINUTES,
  DialingValues,
  pacingFromRate,
  serverFieldError,
  validateDialing,
} from "@/components/campaigns/CampaignDialingFields";
import { TimezoneSelect } from "@/components/TimezoneSelect";
import { nowInZone, timezoneLabel, zonedToUtc } from "@/lib/timezones";

const E164 = /^\+[1-9]\d{6,14}$/;

interface CsvPreview {
  headers: string[];
  rows: string[][];
  totalCount: number;
  invalidCount: number;
}

// Preview only: naive split, no quoted-comma handling. The real parse is server-side.
function parseCsvPreview(text: string): CsvPreview {
  const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0);
  const headers = (lines[0] || "").split(",").map((h) => h.trim());
  const dataLines = lines.slice(1).map((l) => l.split(",").map((c) => c.trim()));
  const phoneCol = headers.findIndex((h) => h.toLowerCase() === "phone_number");
  const invalidCount = phoneCol < 0 ? 0 : dataLines.filter((r) => !E164.test((r[phoneCol] || "").replace(/[\s()-]/g, ""))).length;
  return { headers, rows: dataLines.slice(0, 5), totalCount: dataLines.length, invalidCount };
}

interface Props {
  tenant: Tenant | null;
  isAllTenants: boolean;
  onClose: () => void;
  /** `problem` is set when the campaign exists but its contacts or start did not go through. */
  onCreated: (campaign: Campaign, problem: boolean) => void;
}

export function CreateCampaignModal({ tenant, isAllTenants, onClose, onCreated }: Props) {
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [agentId, setAgentId] = useState("");
  const [dialing, setDialing] = useState<DialingValues>({
    calls_per_minute: DEFAULT_CALLS_PER_MINUTE,
    max_concurrent_calls: 1,
    max_attempts: 1,
    retry_gap_minutes: "",
  });
  const [hoursStart, setHoursStart] = useState("");
  const [hoursEnd, setHoursEnd] = useState("");
  const [leadTimezone, setLeadTimezone] = useState("UTC");
  const [nowMs, setNowMs] = useState(0);
  const [schedule, setSchedule] = useState<"now" | "later">("now");
  const [startAt, setStartAt] = useState("");
  const [contactsFile, setContactsFile] = useState<File | null>(null);
  const [csvPreview, setCsvPreview] = useState<CsvPreview | null>(null);
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // Read on open: the server render has neither the viewer's timezone nor the clock.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLeadTimezone(Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
    setNowMs(Date.now());
  }, []);

  useEffect(() => {
    if (!tenant) return;
    listAgents(tenant.slug)
      .then((all) => {
        const outbound = all.filter((a) => a.direction !== "inbound");
        setAgents(outbound);
        setAgentId((current) => current || outbound.find((a) => a.status === "active")?.id || "");
      })
      .catch((e) => {
        setAgents([]);
        setError(String(e));
      });
  }, [tenant]);

  const agent = agents?.find((a) => a.id === agentId) ?? null;
  const startAtUtc = schedule === "later" ? zonedToUtc(startAt, leadTimezone) : null;
  const startAtValid = schedule === "now" || (startAtUtc !== null && startAtUtc.getTime() > nowMs);
  const hoursValid = (hoursStart === "") === (hoursEnd === "");
  const valid =
    !!tenant && name.trim() !== "" && !!agent && !!agent.outbound_caller_id && !!contactsFile &&
    Object.keys(validateDialing(dialing)).length === 0 && startAtValid && hoursValid;

  const handleFile = (file: File) => {
    setContactsFile(file);
    setCsvPreview(null);
    file.text().then((text) => setCsvPreview(parseCsvPreview(text)));
  };

  const handleCreate = async () => {
    if (!tenant || !contactsFile) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await createCampaign(tenant.id, {
        agent_id: agentId,
        name: name.trim(),
        description: description.trim() || null,
        scheduled_start_at: startAtUtc ? startAtUtc.toISOString() : null,
        pacing_seconds: pacingFromRate(dialing.calls_per_minute),
        max_concurrent_calls: dialing.max_concurrent_calls,
        max_attempts: dialing.max_attempts,
        retry_gap_minutes: dialing.retry_gap_minutes === "" ? DEFAULT_RETRY_GAP_MINUTES : dialing.retry_gap_minutes,
        calling_hours_start: hoursStart || null,
        calling_hours_end: hoursEnd || null,
        calling_hours_timezone: leadTimezone,
      });
      let problem = false;
      try {
        await uploadCampaignContacts(created.id, contactsFile);
        if (schedule === "now") await startCampaign(created.id);
      } catch {
        problem = true;
      }
      onCreated(created, problem);
    } catch (e) {
      const { field, message } = serverFieldError(e);
      if (field) setServerErrors({ [field]: message });
      else setError(message);
      setSubmitting(false);
    }
  };

  const footer = (
    <>
      <button className="btn btn-ghost btn-sm" onClick={onClose} disabled={submitting}>Cancel</button>
      <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={submitting || !valid}>
        {submitting ? "Creating…" : "Create campaign"}
      </button>
    </>
  );

  if (isAllTenants || !tenant) {
    return (
      <Modal open title="Create campaign" onClose={onClose} trapFocus>
        <div className="form-hint" style={{ fontSize: ".85rem" }}>
          {isAllTenants
            ? "Choose an account in the switcher at the top of the page, then create the campaign for it."
            : "Loading…"}
        </div>
      </Modal>
    );
  }

  return (
    <Modal open title="Create campaign" onClose={() => !submitting && onClose()} className="campaign-dialog" footer={footer} trapFocus>
      {error && <div className="error-banner" style={{ margin: "14px 20px 0" }}>{error}</div>}

      <section className="campaign-dialog-section">
        <h3>Campaign details</h3>
        <div className="campaign-dialog-grid">
          <div className="form-group">
            <label className="form-label" htmlFor="campaign-name">
              Campaign name <span className="required">*</span>
            </label>
            <input
              id="campaign-name"
              className="form-input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="August appointment reminders"
            />
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="campaign-leads">
              Lead batch (CSV) <span className="required">*</span>
            </label>
            <div style={{ display: "flex", gap: 8, alignItems: "center", minHeight: 38 }}>
              <button id="campaign-leads" type="button" className="btn btn-ghost btn-sm" onClick={() => fileInputRef.current?.click()}>
                {contactsFile ? "Change file" : "Choose a file"}
              </button>
              <span style={{ fontSize: ".8rem", color: "var(--text-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {contactsFile
                  ? `${contactsFile.name}${csvPreview ? ` · ${csvPreview.totalCount} lead${csvPreview.totalCount === 1 ? "" : "s"}` : ""}`
                  : "No file chosen"}
              </span>
              <input
                ref={fileInputRef}
                type="file"
                accept=".csv"
                style={{ display: "none" }}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) handleFile(file);
                  e.target.value = "";
                }}
              />
            </div>
          </div>
        </div>
        <div className="form-hint" style={{ marginTop: -6 }}>
          A <code>phone_number</code> column with full international numbers (e.g. <span className="mono">+919876543210</span>), and an optional <code>name</code>.
        </div>
        {csvPreview && csvPreview.invalidCount > 0 && (
          <div className="form-hint" style={{ color: "var(--red)" }}>
            {csvPreview.invalidCount} row{csvPreview.invalidCount === 1 ? "" : "s"} don&apos;t have a full international number and will be rejected.
            Add the country code with a leading +.
          </div>
        )}
        {csvPreview && csvPreview.rows.length > 0 && (
          <div style={{ overflowX: "auto", marginTop: 10 }}>
            <table className="tbl" style={{ fontSize: ".76rem" }}>
              <thead>
                <tr>
                  {csvPreview.headers.map((h) => <th key={h}>{h}</th>)}
                </tr>
              </thead>
              <tbody>
                {csvPreview.rows.map((r, i) => (
                  <tr key={i}>
                    {r.map((c, j) => (
                      <td key={j} className={csvPreview.headers[j]?.toLowerCase() === "phone_number" ? "mono" : undefined}>{c}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            {csvPreview.totalCount > csvPreview.rows.length && (
              <div className="form-hint">Showing the first {csvPreview.rows.length} of {csvPreview.totalCount}.</div>
            )}
          </div>
        )}
        <div className="form-group" style={{ marginTop: 12, marginBottom: 0 }}>
          <label className="form-label" htmlFor="campaign-description">
            Description <span className="hint">optional</span>
          </label>
          <textarea
            id="campaign-description"
            className="form-input"
            rows={3}
            maxLength={1000}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Describe the campaign's purpose and goals"
          />
        </div>
      </section>

      <section className="campaign-dialog-section">
        <h3>AI assistant</h3>
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label" htmlFor="campaign-agent-select">
            Select assistant <span className="required">*</span>
          </label>
          <select
            id="campaign-agent-select"
            className="form-select"
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            disabled={agents === null}
          >
            <option value="">{agents === null ? "Loading agents…" : "Choose an outbound agent"}</option>
            {(agents ?? []).map((a) => (
              <option key={a.id} value={a.id} disabled={a.status !== "active"}>
                {a.name}
                {a.status !== "active" ? " (paused)" : ""}
              </option>
            ))}
          </select>
          {agents?.length === 0 && (
            <div className="form-hint">No outbound agents yet. Set an agent&apos;s calls to &quot;My agent calls people&quot; on its page.</div>
          )}
          {agent?.outbound_caller_id && (
            <div className="form-hint">
              Calls show <span className="mono">{agent.outbound_caller_id}</span>, the agent&apos;s outbound caller ID.
            </div>
          )}
          {agent && !agent.outbound_caller_id && (
            <div className="form-hint" style={{ color: "var(--red)" }}>
              This agent has no outbound caller ID.{" "}
              <Link href={`/agents/${tenant.slug}/${agent.slug}`}>Set one in the agent&apos;s settings</Link>.
            </div>
          )}
        </div>
      </section>

      <section className="campaign-dialog-section">
        <h3>Campaign settings</h3>
        <CampaignDialingFields
          values={dialing}
          onChange={(v) => {
            setDialing(v);
            setServerErrors({});
          }}
          errors={serverErrors}
        />
        <div className="form-group" style={{ maxWidth: 360 }}>
          <label className="form-label" htmlFor="campaign-lead-timezone">
            Lead&apos;s timezone <span className="required">*</span>
          </label>
          <TimezoneSelect id="campaign-lead-timezone" value={leadTimezone} onChange={setLeadTimezone} />
          <div className="form-hint">Calling hours and the scheduled start time are in this timezone.</div>
        </div>
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label">
            Calling hours <span className="hint">optional; leave both blank to call at any time</span>
          </label>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <input type="time" className="form-input" style={{ width: 130 }} value={hoursStart} onChange={(e) => setHoursStart(e.target.value)} aria-label="Calling hours start" />
            <span style={{ color: "var(--text-3)" }}>to</span>
            <input type="time" className="form-input" style={{ width: 130 }} value={hoursEnd} onChange={(e) => setHoursEnd(e.target.value)} aria-label="Calling hours end" />
          </div>
          {!hoursValid && <div className="form-hint" style={{ color: "var(--red)" }}>Set both a start and an end time, or neither.</div>}
          <div className="form-hint">Outside these hours, leads wait; they aren&apos;t marked as failed.</div>
        </div>
      </section>

      <section className="campaign-dialog-section">
        <h3>Schedule</h3>
        <div className="export-opts">
          <label className={`export-opt${schedule === "now" ? " on" : ""}`}>
            <input type="radio" name="campaign-schedule" checked={schedule === "now"} onChange={() => setSchedule("now")} />
            <span>
              <b>Start immediately</b>
              <small>Calling begins as soon as the campaign is created</small>
            </span>
          </label>
          <label className={`export-opt${schedule === "later" ? " on" : ""}`}>
            <input type="radio" name="campaign-schedule" checked={schedule === "later"} onChange={() => setSchedule("later")} />
            <span>
              <b>Schedule for later</b>
              <small>Calling begins at the time you choose</small>
            </span>
          </label>
        </div>
        {schedule === "later" ? (
          <div className="form-group" style={{ marginTop: 12, marginBottom: 0, maxWidth: 360 }}>
            <label className="form-label" htmlFor="campaign-start-at">
              Scheduled start time <span className="required">*</span>
            </label>
            <input
              id="campaign-start-at"
              type="datetime-local"
              className="form-input"
              value={startAt}
              min={nowMs ? nowInZone(leadTimezone, nowMs) : undefined}
              onChange={(e) => setStartAt(e.target.value)}
            />
            <div className="form-hint" style={startAt && !startAtValid ? { color: "var(--red)" } : undefined}>
              {startAt && !startAtValid ? "Choose a time in the future." : `In the lead's timezone, ${timezoneLabel(leadTimezone)}.`}
            </div>
          </div>
        ) : null}
      </section>
    </Modal>
  );
}
