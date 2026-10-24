"use client";

import { ApiError } from "@/lib/api";

export interface DialingValues {
  calls_per_minute: number;
  max_concurrent_calls: number;
  max_attempts: number;
  retry_gap_minutes: number | "";
}

export const DEFAULT_RETRY_GAP_MINUTES = 60;
export const DEFAULT_CALLS_PER_MINUTE = 1;
const MAX_CALLS_PER_MINUTE = 60;

// The backend paces by the gap between dials, so a per-minute rate is stored as pacing_seconds.
export const pacingFromRate = (rate: number) => Math.ceil(60 / rate);
export const rateFromPacing = (seconds: number) =>
  seconds <= 0 ? MAX_CALLS_PER_MINUTE : Math.max(1, Math.min(MAX_CALLS_PER_MINUTE, Math.round(60 / seconds)));

// Mirrors services/campaigns/schemas.py: attempts, gap and concurrency are all >= 1.
export function validateDialing(v: DialingValues): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!(v.calls_per_minute >= 1 && v.calls_per_minute <= MAX_CALLS_PER_MINUTE)) {
    errors.calls_per_minute = `Must be between 1 and ${MAX_CALLS_PER_MINUTE}.`;
  }
  if (!(v.max_concurrent_calls >= 1)) errors.max_concurrent_calls = "Must be at least 1.";
  if (!(v.max_attempts >= 1)) errors.max_attempts = "Must be at least 1.";
  if (v.retry_gap_minutes !== "" && !(v.retry_gap_minutes >= 1)) errors.retry_gap_minutes = "Must be at least 1 minute.";
  return errors;
}

const FORM_FIELDS = ["max_attempts", "retry_gap_minutes", "max_concurrent_calls"];

// Maps a server error to a form field: list detail by `loc`, string detail by a leading field token.
export function serverFieldError(e: unknown): { field: string | null; message: string } {
  if (!(e instanceof ApiError)) return { field: null, message: String(e) };
  const raw = e.body?.detail;
  if (Array.isArray(raw)) {
    const loc = raw[0]?.loc;
    const last = Array.isArray(loc) ? String(loc[loc.length - 1]) : null;
    return { field: last && FORM_FIELDS.includes(last) ? last : null, message: e.detail };
  }
  const lead = e.detail.split(" ")[0];
  return { field: FORM_FIELDS.includes(lead) ? lead : null, message: e.detail };
}

interface Props {
  values: DialingValues;
  onChange: (values: DialingValues) => void;
  errors?: Record<string, string>;
}

export function CampaignDialingFields({ values, onChange, errors = {} }: Props) {
  const localErrors = { ...validateDialing(values), ...errors };
  const set = (patch: Partial<DialingValues>) => onChange({ ...values, ...patch });
  const fieldError = (field: string) =>
    localErrors[field] && <div className="form-hint" style={{ color: "var(--red)" }}>{localErrors[field]}</div>;

  return (
    <>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <div className="form-group" style={{ flex: "1 1 200px" }}>
          <label className="form-label" htmlFor="campaign-field-calls_per_minute">
            Calls per minute <span className="hint">how fast new calls start</span>
          </label>
          <input
            id="campaign-field-calls_per_minute"
            type="number"
            min={1}
            max={MAX_CALLS_PER_MINUTE}
            className="form-input"
            value={values.calls_per_minute}
            onChange={(e) => set({ calls_per_minute: Number(e.target.value) })}
          />
          {fieldError("calls_per_minute")}
        </div>
        <div className="form-group" style={{ flex: "1 1 200px" }}>
          <label className="form-label" htmlFor="campaign-field-max_concurrent_calls">
            Max simultaneous calls <span className="hint">calls live at once</span>
          </label>
          <input
            id="campaign-field-max_concurrent_calls"
            type="number"
            min={1}
            className="form-input"
            value={values.max_concurrent_calls}
            onChange={(e) => set({ max_concurrent_calls: Number(e.target.value) })}
          />
          {fieldError("max_concurrent_calls")}
        </div>
      </div>
      <div className="form-hint" style={{ marginTop: -6, marginBottom: 12 }}>
        A new call starts only when both limits allow it. Keep the rate low (1–2 per minute) unless your carrier allows more.
      </div>

      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <div className="form-group" style={{ flex: "1 1 200px" }}>
          <label className="form-label" htmlFor="campaign-field-max_attempts">
            Max attempts <span className="hint">per contact</span>
          </label>
          <input
            id="campaign-field-max_attempts"
            type="number"
            min={1}
            className="form-input"
            value={values.max_attempts}
            onChange={(e) => set({ max_attempts: Number(e.target.value) })}
          />
          {fieldError("max_attempts")}
        </div>
        <div className="form-group" style={{ flex: "1 1 200px" }}>
          <label className="form-label" htmlFor="campaign-field-retry_gap_minutes">
            Retry gap (minutes) <span className="hint">blank = {DEFAULT_RETRY_GAP_MINUTES}</span>
          </label>
          <input
            id="campaign-field-retry_gap_minutes"
            type="number"
            min={1}
            className="form-input"
            value={values.retry_gap_minutes}
            onChange={(e) => set({ retry_gap_minutes: e.target.value === "" ? "" : Number(e.target.value) })}
          />
          {fieldError("retry_gap_minutes")}
        </div>
      </div>
    </>
  );
}
