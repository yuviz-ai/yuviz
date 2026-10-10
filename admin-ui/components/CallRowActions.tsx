"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import Link from "next/link";
import { Download, Eye, FileText, LucideIcon, MoreHorizontal, PlayCircle } from "lucide-react";
import { Modal } from "@/components/Modal";
import { CallTranscript } from "@/components/CallTranscript";
import { sentimentLabel } from "@/components/SentimentBadge";
import { ApiError, CallWithTenant, TranscriptEntry, getTranscript } from "@/lib/api";
import { outcomeOf } from "@/lib/callOutcome";

type Panel = "transcript" | "recording" | "error";

// recording_ref is a storage locator; only direct http(s) links can play in the browser.
const playableRecording = (ref: string | null) => (ref && /^https?:\/\//i.test(ref) ? ref : null);

function callLabel(c: CallWithTenant): string {
  const parties = c.caller_number || c.called_number ? `${c.caller_number || "—"} → ${c.called_number || "—"}` : `${c.mode} call`;
  return `${parties} · ${new Date(c.started_at).toLocaleString()}`;
}

function formatDuration(ms: number | null): string {
  if (ms == null) return "—";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

function CallSummary({ call }: { call: CallWithTenant }) {
  return (
    <dl className="detail-kv call-summary">
      <dt>Outcome</dt>
      <dd>{call.status === "live" ? "Still in progress" : outcomeOf(call.close_reason).label}</dd>
      <dt>Duration</dt>
      <dd>{formatDuration(call.duration_ms)}</dd>
      <dt>Agent</dt>
      <dd>{call.agent_name || "—"}</dd>
      <dt>Sentiment</dt>
      <dd>
        {call.sentiment ? sentimentLabel(call.sentiment) : "Not scored"}
        {call.sentiment_reason && <span className="call-summary-note"> — {call.sentiment_reason}</span>}
      </dd>
      {call.disposition && (
        <>
          <dt>Disposition</dt>
          <dd>{call.disposition}</dd>
        </>
      )}
    </dl>
  );
}

export function CallRowActions({ call, onOpen }: { call: CallWithTenant; onOpen: () => void }) {
  const [panel, setPanel] = useState<Panel | null>(null);
  const [menuPos, setMenuPos] = useState<{ top: number; left: number } | null>(null);
  const [transcript, setTranscript] = useState<TranscriptEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const recording = playableRecording(call.recording_ref);
  const noRecording = call.recording_ref
    ? "This recording can't be played in the browser"
    : "No recording for this call";

  useEffect(() => {
    if (!menuPos) return;
    const close = () => setMenuPos(null);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [menuPos]);

  const openTranscript = () => {
    setPanel("transcript");
    setTranscript(null);
    if (!call.has_transcript) {
      setTranscript([]);
      return;
    }
    getTranscript(call.session_id)
      .then(setTranscript)
      .catch((e) => {
        setError(e instanceof ApiError ? e.detail : String(e));
        setPanel("error");
      });
  };

  const downloadRecording = () => {
    const a = document.createElement("a");
    a.href = recording!;
    a.download = `recording-${call.session_id}`;
    a.target = "_blank";
    a.rel = "noopener";
    a.click();
  };

  const items: { icon: LucideIcon; label: string; run: () => void; off?: string }[] = [
    { icon: FileText, label: "Transcript & summary", run: openTranscript },
    { icon: PlayCircle, label: "Play recording", run: () => setPanel("recording"), off: recording ? undefined : noRecording },
    { icon: Eye, label: "View details", run: onOpen },
    { icon: Download, label: "Download recording", run: downloadRecording, off: recording ? undefined : noRecording },
  ];

  return (
    // The row itself opens the call; keep clicks and keys here from reaching it.
    <div className="call-actions" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
      <button
        type="button"
        className={`call-action${menuPos ? " on" : ""}`}
        title="Actions"
        aria-label="Call actions"
        aria-haspopup="menu"
        aria-expanded={menuPos !== null}
        onClick={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          // Fixed: the table's scroll wrapper would clip an absolute menu.
          setMenuPos(menuPos ? null : { top: r.bottom + 4, left: Math.max(8, r.right - 220) });
        }}
      >
        <MoreHorizontal size={16} />
      </button>

      {menuPos && createPortal(
        <>
          <div className="ed2-menu-backdrop" onClick={() => setMenuPos(null)} />
          <div className="ed2-menu-pop call-menu" role="menu" style={{ top: menuPos.top, left: menuPos.left }}>
            {items.map(({ icon: Icon, label, run, off }) => (
              <button
                key={label}
                role="menuitem"
                aria-disabled={!!off}
                title={off}
                onClick={() => {
                  if (off) return;
                  setMenuPos(null);
                  run();
                }}
              >
                <Icon size={15} /> {label}
              </button>
            ))}
          </div>
        </>,
        document.body,
      )}

      {panel && createPortal(
        <Modal
          open
          title={panel === "transcript" ? "Transcript & summary" : panel === "recording" ? "Recording" : "Something went wrong"}
          onClose={() => setPanel(null)}
          footer={panel !== "error" && (
            <Link href={`/calls/${call.session_id}`} className="btn btn-ghost btn-sm">Open full details</Link>
          )}
        >
          {panel !== "error" && <div className="form-hint" style={{ marginTop: 0, marginBottom: 10 }}>{callLabel(call)}</div>}
          {panel === "error" && <div className="error-banner" style={{ margin: 0 }}>{error}</div>}
          {panel === "recording" && recording && (
            <audio
              controls autoPlay src={recording} style={{ width: "100%" }}
              onError={() => {
                setError("The recording couldn't be loaded. It may have expired or been moved.");
                setPanel("error");
              }}
            />
          )}
          {panel === "transcript" && (
            <>
              <CallSummary call={call} />
              {transcript === null
                ? <div className="empty-state">Loading transcript…</div>
                : transcript.length === 0
                  ? <div className="empty-state">{call.status === "live" ? "The transcript appears once the caller speaks." : "No transcript was recorded for this call."}</div>
                  : <CallTranscript entries={transcript} />}
            </>
          )}
        </Modal>,
        document.body,
      )}
    </div>
  );
}
