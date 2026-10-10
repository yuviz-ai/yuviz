"use client";

import { useState } from "react";
import { Download } from "lucide-react";
import { Modal } from "@/components/Modal";
import { ApiError, CallExportColumn, CallExportRequest, exportCalls } from "@/lib/api";

export type ExportScope = "filtered" | "selected";

const COLUMNS: { key: CallExportColumn; label: string; on: boolean }[] = [
  { key: "started_at", label: "Started", on: true },
  { key: "ended_at", label: "Ended", on: false },
  { key: "account", label: "Account", on: true },
  { key: "direction", label: "Direction", on: true },
  { key: "caller_number", label: "From number", on: true },
  { key: "called_number", label: "To number", on: true },
  { key: "agent", label: "Agent", on: true },
  { key: "duration", label: "Duration (seconds)", on: true },
  { key: "status", label: "Status", on: true },
  { key: "outcome", label: "Outcome", on: true },
  { key: "close_reason", label: "Close reason (system code)", on: false },
  { key: "sentiment", label: "Sentiment", on: true },
  { key: "sentiment_reason", label: "Sentiment reason", on: false },
  { key: "turns", label: "Turns", on: true },
  { key: "disposition", label: "Disposition", on: false },
  { key: "languages", label: "Languages spoken", on: false },
  { key: "session_id", label: "Call ID", on: false },
];

const browserTimezone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;

function saveFile(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function ExportCallsModal({
  onClose, filteredLabel, selectedCount, showAccount, request,
}: {
  onClose: () => void;
  filteredLabel: string;
  selectedCount: number;
  showAccount: boolean;
  request: (scope: ExportScope) => Omit<CallExportRequest, "format" | "columns">;
}) {
  const available = showAccount ? COLUMNS : COLUMNS.filter((c) => c.key !== "account");
  const [scope, setScope] = useState<ExportScope>(selectedCount > 0 ? "selected" : "filtered");
  const [format, setFormat] = useState<"xlsx" | "csv">("xlsx");
  const [columns, setColumns] = useState<Set<CallExportColumn>>(
    () => new Set(available.filter((c) => c.on).map((c) => c.key)),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);

  const toggle = (key: CallExportColumn) =>
    setColumns((prev) => {
      const next = new Set(prev);
      if (!next.delete(key)) next.add(key);
      return next;
    });

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await exportCalls({
        ...request(scope),
        format,
        columns: available.filter((c) => columns.has(c.key)).map((c) => c.key),
        timezone: browserTimezone(),
      });
      saveFile(result.blob, `calls-${new Date().toISOString().slice(0, 10)}.${format}`);
      if (result.truncated) setTruncated(true);
      else onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      title="Export calls"
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>{truncated ? "Done" : "Cancel"}</button>
          <button className="btn btn-primary btn-sm" onClick={download} disabled={busy || columns.size === 0}>
            <Download size={13} /> {busy ? "Preparing…" : "Download"}
          </button>
        </>
      }
    >
      {error && <div className="error-banner">{error}</div>}
      {truncated && (
        <div className="info-banner">
          The file has the newest 50,000 matching calls. Narrow the time range to export older ones.
        </div>
      )}

      <div className="form-group">
        <div className="form-label">What to export</div>
        <div className="export-opts">
          <label className={`export-opt${scope === "filtered" ? " on" : ""}`}>
            <input type="radio" name="export-scope" checked={scope === "filtered"} onChange={() => setScope("filtered")} />
            <span>
              <b>Calls matching your filters</b>
              <small>{filteredLabel}</small>
            </span>
          </label>
          <label className={`export-opt${scope === "selected" ? " on" : ""}${selectedCount === 0 ? " off" : ""}`}>
            <input
              type="radio" name="export-scope" disabled={selectedCount === 0}
              checked={scope === "selected"} onChange={() => setScope("selected")}
            />
            <span>
              <b>Selected calls</b>
              <small>
                {selectedCount === 0
                  ? "Tick calls in the table to export just those"
                  : `${selectedCount} call${selectedCount === 1 ? "" : "s"} ticked`}
              </small>
            </span>
          </label>
        </div>
      </div>

      <div className="form-group">
        <div className="form-label">Format</div>
        <div className="export-opts">
          {([["xlsx", "Excel (.xlsx)", "Formatted sheet with real dates and numbers"], ["csv", "CSV (.csv)", "Plain text, opens anywhere"]] as const).map(
            ([value, label, hint]) => (
              <label key={value} className={`export-opt${format === value ? " on" : ""}`}>
                <input type="radio" name="export-format" checked={format === value} onChange={() => setFormat(value)} />
                <span>
                  <b>{label}</b>
                  <small>{hint}</small>
                </span>
              </label>
            ),
          )}
        </div>
      </div>

      <div className="form-group" style={{ marginBottom: 0 }}>
        <div className="form-label">
          Columns <span className="hint">{columns.size} of {available.length}</span>
          <span style={{ marginLeft: "auto", display: "inline-flex", gap: 4 }}>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setColumns(new Set(available.map((c) => c.key)))}>
              All
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setColumns(new Set())}>
              None
            </button>
          </span>
        </div>
        <div className="export-cols">
          {available.map((c) => (
            <label key={c.key}>
              <input type="checkbox" checked={columns.has(c.key)} onChange={() => toggle(c.key)} />
              {c.label}
            </label>
          ))}
        </div>
        {columns.size === 0 && <div className="form-hint">Pick at least one column.</div>}
        <div className="form-hint">Times are in your timezone ({browserTimezone()}).</div>
      </div>
    </Modal>
  );
}
