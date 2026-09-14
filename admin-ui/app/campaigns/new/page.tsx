"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AgentWithTenant,
  ApiError,
  CampaignCreate,
  createCampaign,
  listAllAgents,
  listPurchasedNumbers,
  PurchasedNumber,
  Tenant,
  listTenants,
  uploadCampaignContacts,
} from "@/lib/api";

const EMPTY_FORM: CampaignCreate = {
  agent_id: "",
  name: "",
  caller_id: "",
  max_concurrent_calls: 1,
  pacing_seconds: 5,
  max_attempts: 1,
  calling_hours_start: "",
  calling_hours_end: "",
  calling_hours_timezone: "UTC",
};

// Country the calling list is expected to belong to — a client-side guard
// only (no backend field for it): the campaigns worker dials whatever
// number is in the CSV, so this just warns before upload rather than
// silently mis-dialing a batch of the wrong country's numbers.
const COUNTRY_OPTIONS = [
  { code: "IN", label: "India (+91)", prefix: "+91" },
  { code: "US", label: "United States (+1)", prefix: "+1" },
  { code: "GB", label: "United Kingdom (+44)", prefix: "+44" },
  { code: "AE", label: "UAE (+971)", prefix: "+971" },
];

type StepId = 1 | 2 | 3;
const STEPS: { id: StepId; label: string }[] = [
  { id: 1, label: "Upload details" },
  { id: 2, label: "Configure campaign" },
  { id: 3, label: "Review" },
];

function Stepper({ step }: { step: StepId }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, paddingBottom: 16, marginBottom: 20, borderBottom: "1px solid var(--border)" }}>
      {STEPS.map((s, i) => (
        <div key={s.id} style={{ display: "flex", alignItems: "center", gap: 10, flex: i < STEPS.length - 1 ? 1 : undefined }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span
              style={{
                width: 26,
                height: 26,
                borderRadius: "50%",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: ".75rem",
                fontWeight: 700,
                flexShrink: 0,
                background: s.id <= step ? "var(--cyan)" : "var(--surf)",
                border: s.id <= step ? "none" : "1px solid var(--border-2)",
                color: s.id <= step ? "#fff" : "var(--text-3)",
              }}
            >
              {s.id}
            </span>
            <span style={{ fontWeight: s.id === step ? 700 : 500, color: s.id === step ? "var(--text)" : "var(--text-3)", fontSize: ".9rem", whiteSpace: "nowrap" }}>
              {s.label}
            </span>
          </div>
          {i < STEPS.length - 1 && <span style={{ flex: 1, height: 1, background: "var(--border)" }} />}
        </div>
      ))}
    </div>
  );
}

interface CsvPreview {
  headers: string[];
  rows: string[][];
  totalCount: number;
}

// Preview only — a lightweight split, not a real CSV parser (no quoted-comma
// handling). The authoritative parse happens server-side on actual upload
// (services/campaigns's own contacts/upload endpoint); this just gives the
// operator a glance at what they're about to send.
function parseCsvPreview(text: string): CsvPreview {
  const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0);
  const headers = (lines[0] || "").split(",").map((h) => h.trim());
  const dataLines = lines.slice(1);
  const rows = dataLines.slice(0, 8).map((l) => l.split(",").map((c) => c.trim()));
  return { headers, rows, totalCount: dataLines.length };
}

export default function NewCampaignPage() {
  const router = useRouter();
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [purchasedNumbers, setPurchasedNumbers] = useState<PurchasedNumber[]>([]);
  const [tenantId, setTenantId] = useState("");
  const [form, setForm] = useState<CampaignCreate>(EMPTY_FORM);
  const [callerIdCustom, setCallerIdCustom] = useState(false);
  const [countryCode, setCountryCode] = useState(COUNTRY_OPTIONS[0].prefix);
  const [contactsFile, setContactsFile] = useState<File | null>(null);
  const [csvPreview, setCsvPreview] = useState<CsvPreview | null>(null);
  const [csvMismatchCount, setCsvMismatchCount] = useState(0);
  const [dragOver, setDragOver] = useState(false);
  const [step, setStep] = useState<StepId>(1);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const agentsForTenant = (id: string) =>
    agents.filter((a) => tenants.find((t) => t.id === id)?.slug === a.tenantSlug);

  useEffect(() => {
    listTenants().then((ts) => {
      setTenants(ts);
      if (ts.length > 0) setTenantId(ts[0].id);
      listAllAgents(ts).then(setAgents);
    });
  }, []);

  useEffect(() => {
    const first = agentsForTenant(tenantId)[0];
    setForm((f) => ({ ...f, agent_id: first?.id || "" }));
    if (tenantId) listPurchasedNumbers(tenantId).then(setPurchasedNumbers).catch(() => setPurchasedNumbers([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, agents]);

  // Workflow (agent_id) is marked optional in this wizard for now, even
  // though campaigns.agent_id is NOT NULL server-side (database/schema.sql)
  // — submitting without one surfaces the backend's own real rejection at
  // Create time (the error banner) rather than a client-side block here.
  const audienceValid = !!form.name.trim();

  const handleFile = (file: File) => {
    setContactsFile(file);
    file.text().then((text) => {
      const preview = parseCsvPreview(text);
      setCsvPreview(preview);
      const phoneCol = preview.headers.findIndex((h) => h.toLowerCase() === "phone_number");
      if (phoneCol >= 0) {
        const mismatches = preview.rows.filter((r) => r[phoneCol] && !r[phoneCol].startsWith(countryCode)).length;
        setCsvMismatchCount(mismatches);
      } else {
        setCsvMismatchCount(0);
      }
    });
  };

  const handleCreate = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const created = await createCampaign(tenantId, form);
      if (contactsFile) {
        // Contacts can only be uploaded against a campaign that already
        // exists (services/campaigns/campaigns.py), so the file staged in
        // step 1 is held in memory and only sent now — a failure here
        // still lands the operator on a real campaign, not a lost draft.
        await uploadCampaignContacts(created.id, contactsFile).catch(() => {
          // Non-fatal: the campaign page's own Upload CSV control covers
          // retrying this — the campaign itself must not be blocked on it.
        });
      }
      router.push(`/campaigns/${created.id}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
      setSubmitting(false);
    }
  };

  return (
    <>
      <div style={{ marginBottom: 18 }}>
        <Link href="/campaigns" style={{ color: "var(--text-3)", fontSize: ".8rem" }}>
          ← Campaigns
        </Link>
        <h1 style={{ fontSize: "1.6rem", fontWeight: 700, color: "var(--text)", margin: "6px 0 0" }}>Create Campaign</h1>
      </div>

      <Stepper step={step} />

      {error && <div className="error-banner" style={{ marginBottom: 14 }}>{error}</div>}

      {step === 1 && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 380px", gap: 16, alignItems: "start" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div className="card">
              <div className="card-hdr" style={{ flexDirection: "column", alignItems: "flex-start", gap: 2 }}>
                <div className="card-title">Campaign &amp; dial setup</div>
                <div style={{ fontSize: ".76rem", color: "var(--text-3)" }}>
                  Name the campaign and choose call centre, caller ID, country, dial mode, and workflow.
                </div>
              </div>
              <div style={{ padding: 16 }}>
                <div className="form-group">
                  <label className="form-label">
                    Account <span className="required">*</span>
                  </label>
                  <select className="form-select" value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
                    {tenants.map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.name}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="form-group">
                  <label className="form-label">
                    Campaign name <span className="required">*</span>
                  </label>
                  <input
                    className="form-input"
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder="August collections"
                  />
                </div>

                <div className="form-group">
                  <label className="form-label">Caller ID</label>
                  {!callerIdCustom ? (
                    <select
                      className="form-select"
                      value={form.caller_id || ""}
                      onChange={(e) => {
                        if (e.target.value === "__custom__") {
                          setCallerIdCustom(true);
                          setForm({ ...form, caller_id: "" });
                        } else {
                          setForm({ ...form, caller_id: e.target.value });
                        }
                      }}
                    >
                      <option value="">Select a caller ID…</option>
                      {purchasedNumbers.map((n) => (
                        <option key={n.id} value={n.phone_number}>
                          {n.phone_number}
                        </option>
                      ))}
                      <option value="__custom__">Custom…</option>
                    </select>
                  ) : (
                    <div style={{ display: "flex", gap: 8 }}>
                      <input
                        className="form-input"
                        style={{ fontFamily: "var(--mono)" }}
                        value={form.caller_id || ""}
                        onChange={(e) => setForm({ ...form, caller_id: e.target.value })}
                        placeholder="5000"
                        autoFocus
                      />
                      <button className="btn btn-ghost btn-sm" onClick={() => setCallerIdCustom(false)}>
                        Use a registered number
                      </button>
                    </div>
                  )}
                  {!callerIdCustom && purchasedNumbers.length === 0 && (
                    <div className="form-hint">No active caller IDs — pick &quot;Custom…&quot; to enter one.</div>
                  )}
                </div>

                <div className="form-group">
                  <label className="form-label">Country code</label>
                  <select className="form-select" value={countryCode} onChange={(e) => setCountryCode(e.target.value)}>
                    {COUNTRY_OPTIONS.map((c) => (
                      <option key={c.code} value={c.prefix}>
                        {c.label}
                      </option>
                    ))}
                  </select>
                  <div className="form-hint">
                    This campaign dials one country. Every number in your calling list must be a{" "}
                    {COUNTRY_OPTIONS.find((c) => c.prefix === countryCode)?.label || countryCode} number.
                  </div>
                </div>

                <div className="form-group">
                  <label className="form-label">Dialing mode</label>
                  <select className="form-select" value="progressive" disabled>
                    <option value="progressive">Progressive — dial as capacity frees up</option>
                  </select>
                  <div className="form-hint">The only dialing mode this system runs today — predictive and preview dialing aren&apos;t built yet.</div>
                </div>

                <div className="form-group" style={{ marginBottom: 0 }}>
                  <label className="form-label">
                    Workflow <span className="hint">optional for now — the agent (flow) that handles these calls</span>
                  </label>
                  <select
                    className="form-select"
                    value={form.agent_id}
                    onChange={(e) => setForm({ ...form, agent_id: e.target.value })}
                  >
                    <option value="">Select a workflow…</option>
                    {agentsForTenant(tenantId).map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            </div>

            <div className="card">
              <div className="card-hdr" style={{ flexDirection: "column", alignItems: "flex-start", gap: 2 }}>
                <div className="card-title">Upload calling list</div>
                <div style={{ fontSize: ".76rem", color: "var(--text-3)" }}>
                  Upload your calling list for this campaign. Numbers must belong to{" "}
                  {COUNTRY_OPTIONS.find((c) => c.prefix === countryCode)?.label || countryCode} — change the country in dial setup if this list is for another country.
                </div>
              </div>
              <div style={{ padding: 16 }}>
                <label className="form-label">Contacts CSV</label>
                <div
                  onDragOver={(e) => {
                    e.preventDefault();
                    setDragOver(true);
                  }}
                  onDragLeave={() => setDragOver(false)}
                  onDrop={(e) => {
                    e.preventDefault();
                    setDragOver(false);
                    const file = e.dataTransfer.files?.[0];
                    if (file) handleFile(file);
                  }}
                  style={{
                    border: `1px dashed ${dragOver ? "var(--cyan)" : "var(--border-2)"}`,
                    borderRadius: 8,
                    background: dragOver ? "var(--surf-2)" : "var(--surf)",
                    padding: 32,
                    textAlign: "center",
                  }}
                >
                  {contactsFile ? (
                    <>
                      <div style={{ fontSize: ".85rem", color: "var(--text)", fontWeight: 600, marginBottom: 6 }}>{contactsFile.name}</div>
                      <div style={{ fontSize: ".76rem", color: "var(--text-3)", marginBottom: 10 }}>
                        {csvPreview ? `${csvPreview.totalCount} contact${csvPreview.totalCount === 1 ? "" : "s"} detected` : "Parsing…"}
                      </div>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => {
                          setContactsFile(null);
                          setCsvPreview(null);
                        }}
                      >
                        Remove file
                      </button>
                    </>
                  ) : (
                    <>
                      <div style={{ color: "var(--text-3)", marginBottom: 8 }}>
                        <svg width="20" height="20" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" style={{ margin: "0 auto" }}>
                          <path d="M8 11V2M8 2L4.5 5.5M8 2l3.5 3.5" />
                          <path d="M2.5 11v1.5A1.5 1.5 0 004 14h8a1.5 1.5 0 001.5-1.5V11" />
                        </svg>
                      </div>
                      <div style={{ fontSize: ".82rem", color: "var(--text-2)", marginBottom: 10 }}>
                        Drag a <code>.csv</code> here, or
                      </div>
                      <button className="btn btn-ghost btn-sm" onClick={() => fileInputRef.current?.click()}>
                        Choose a file
                      </button>
                      <input
                        ref={fileInputRef}
                        type="file"
                        accept=".csv"
                        style={{ display: "none" }}
                        onChange={(e) => {
                          const file = e.target.files?.[0];
                          if (file) handleFile(file);
                        }}
                      />
                    </>
                  )}
                </div>
                <div className="form-hint" style={{ marginTop: 8 }}>
                  A <code>phone_number</code> column (and optional <code>name</code>). You can also add this later from the campaign page.
                </div>
              </div>
            </div>
          </div>

          <div className="card" style={{ position: "sticky", top: 16 }}>
            <div style={{ padding: 16 }}>
              {!csvPreview ? (
                <div style={{ color: "var(--text-3)", fontSize: ".85rem", textAlign: "center", padding: "24px 0" }}>
                  Upload a CSV to preview your contacts.
                </div>
              ) : (
                <>
                  <div style={{ fontSize: ".76rem", color: "var(--text-3)", marginBottom: 10 }}>
                    {csvPreview.totalCount} contact{csvPreview.totalCount === 1 ? "" : "s"} · showing first {csvPreview.rows.length}
                  </div>
                  {csvMismatchCount > 0 && (
                    <div className="form-hint" style={{ color: "var(--red)", marginBottom: 10 }}>
                      {csvMismatchCount} number{csvMismatchCount === 1 ? "" : "s"} in the preview don&apos;t start with {countryCode} — double check the country code above.
                    </div>
                  )}
                  <div style={{ overflowX: "auto" }}>
                    <table className="tbl" style={{ fontSize: ".76rem" }}>
                      <thead>
                        <tr>
                          {csvPreview.headers.map((h) => (
                            <th key={h}>{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {csvPreview.rows.map((r, i) => (
                          <tr key={i}>
                            {r.map((c, j) => (
                              <td key={j} className={j === 0 ? "mono" : undefined}>{c}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Pacing &amp; retry policy</div>
          </div>
          <div style={{ padding: 16 }}>
            <div style={{ display: "flex", gap: 12 }}>
              <div className="form-group" style={{ flex: 1 }}>
                <label className="form-label">
                  Pacing (seconds) <span className="hint">minimum gap between dial attempts</span>
                </label>
                <input
                  type="number"
                  min={0}
                  className="form-input"
                  value={form.pacing_seconds}
                  onChange={(e) => setForm({ ...form, pacing_seconds: Number(e.target.value) })}
                />
              </div>
              <div className="form-group" style={{ flex: 1 }}>
                <label className="form-label">
                  Max concurrent calls <span className="hint">calls in flight at once</span>
                </label>
                <input
                  type="number"
                  min={1}
                  className="form-input"
                  value={form.max_concurrent_calls}
                  onChange={(e) => setForm({ ...form, max_concurrent_calls: Number(e.target.value) })}
                />
              </div>
              <div className="form-group" style={{ flex: 1 }}>
                <label className="form-label">
                  Max attempts <span className="hint">retry ladder per contact</span>
                </label>
                <input
                  type="number"
                  min={1}
                  className="form-input"
                  value={form.max_attempts}
                  onChange={(e) => setForm({ ...form, max_attempts: Number(e.target.value) })}
                />
              </div>
            </div>

            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">
                Calling hours <span className="hint">leave both blank to allow any time — outside this window, contacts are skipped, not failed</span>
              </label>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input
                  type="time"
                  className="form-input"
                  style={{ width: 120 }}
                  value={form.calling_hours_start || ""}
                  onChange={(e) => setForm({ ...form, calling_hours_start: e.target.value })}
                />
                <span style={{ color: "var(--text-3)" }}>to</span>
                <input
                  type="time"
                  className="form-input"
                  style={{ width: 120 }}
                  value={form.calling_hours_end || ""}
                  onChange={(e) => setForm({ ...form, calling_hours_end: e.target.value })}
                />
                <input
                  className="form-input"
                  style={{ flex: 1, fontFamily: "var(--mono)" }}
                  placeholder="Timezone, e.g. America/New_York"
                  value={form.calling_hours_timezone || ""}
                  onChange={(e) => setForm({ ...form, calling_hours_timezone: e.target.value })}
                />
              </div>
            </div>
          </div>
        </div>
      )}

      {step === 3 && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Review</div>
          </div>
          <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-2)" }}>Account</span>
              <strong style={{ color: "var(--text)" }}>{tenants.find((t) => t.id === tenantId)?.name || "—"}</strong>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-2)" }}>Campaign name</span>
              <strong style={{ color: "var(--text)" }}>{form.name || "—"}</strong>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-2)" }}>Workflow</span>
              <strong style={{ color: "var(--text)" }}>{agentsForTenant(tenantId).find((a) => a.id === form.agent_id)?.name || "—"}</strong>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-2)" }}>Caller ID</span>
              <strong className="mono" style={{ color: "var(--text)" }}>{form.caller_id || "not set"}</strong>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-2)" }}>Country</span>
              <strong style={{ color: "var(--text)" }}>{COUNTRY_OPTIONS.find((c) => c.prefix === countryCode)?.label}</strong>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-2)" }}>Dialing mode</span>
              <strong style={{ color: "var(--text)" }}>Progressive</strong>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-2)" }}>Contacts file</span>
              <strong style={{ color: "var(--text)" }}>
                {contactsFile ? `${contactsFile.name}${csvPreview ? ` (${csvPreview.totalCount})` : ""}` : "none — add later"}
              </strong>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-2)" }}>Pacing</span>
              <strong style={{ color: "var(--text)" }}>{form.pacing_seconds}s gap, {form.max_concurrent_calls} concurrent</strong>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-2)" }}>Max attempts</span>
              <strong style={{ color: "var(--text)" }}>{form.max_attempts}</strong>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-2)" }}>Calling hours</span>
              <strong style={{ color: "var(--text)" }}>
                {form.calling_hours_start && form.calling_hours_end
                  ? `${form.calling_hours_start}–${form.calling_hours_end} (${form.calling_hours_timezone})`
                  : "any time"}
              </strong>
            </div>
          </div>
        </div>
      )}

      <div style={{ display: "flex", gap: 8, justifyContent: "space-between", marginTop: 20 }}>
        <div>
          {step > 1 && (
            <button className="btn btn-ghost btn-sm" onClick={() => setStep((step - 1) as StepId)}>
              Back
            </button>
          )}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <Link href="/campaigns" className="btn btn-ghost btn-sm">
            Cancel
          </Link>
          {step < 3 ? (
            <button
              className="btn btn-primary btn-sm"
              onClick={() => setStep((step + 1) as StepId)}
              disabled={step === 1 && !audienceValid}
            >
              Next
            </button>
          ) : (
            <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={submitting}>
              {submitting ? "Creating…" : "Create campaign"}
            </button>
          )}
        </div>
      </div>
    </>
  );
}
