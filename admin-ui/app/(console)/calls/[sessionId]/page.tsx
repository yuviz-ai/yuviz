"use client";

// Single call detail page.

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, ArrowRight, Check, ChevronRight, Copy } from "lucide-react";
import {
  ApiError, Call, TranscriptEntry, getCall, getTranscript,
} from "@/lib/api";
import { CallTranscript } from "@/components/CallTranscript";
import { SentimentBadge } from "@/components/SentimentBadge";

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function formatDuration(ms: number | null): string {
  if (ms == null) return "—";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

/** "caller_hangup" → "Caller hangup". */
function humanize(code: string): string {
  const s = code.replace(/[_-]+/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function formatValue(v: unknown): string {
  return v !== null && typeof v === "object" ? JSON.stringify(v) : String(v);
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-ghost btn-sm btn-icon"
      title={copied ? "Copied" : `Copy ${label}`}
      aria-label={`Copy ${label}`}
      onClick={() => {
        navigator.clipboard.writeText(value).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
    </button>
  );
}

export default function CallDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const [call, setCall] = useState<Call | null>(null);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [transcriptLoading, setTranscriptLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getCall(sessionId)
      .then(setCall)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, [sessionId]);

  useEffect(() => {
    // Fetched separately so a transcript failure leaves the page usable. Reset on sessionId
    // change: Next keeps this mounted between calls, which would show a stale transcript.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setTranscriptLoading(true);
    getTranscript(sessionId)
      .then(setTranscript)
      .catch(() => setTranscript([]))
      .finally(() => setTranscriptLoading(false));
  }, [sessionId]);

  if (error) {
    return (
      <>
        <Link href="/calls" className="detail-back"><ArrowLeft size={13} /> Calls</Link>
        <div className="error-banner">{error}</div>
      </>
    );
  }
  if (!call) return <div className="empty-state">Loading call…</div>;

  const hasPath = call.nodes_visited != null && call.nodes_visited.length > 0;
  const captured = call.extracted_variables ?? {};
  const hasCaptured = Object.keys(captured).length > 0;
  // turn_count stays 0 for calls reconciled after a restart; the transcript is the source of truth.
  const turns = Math.max(call.turn_count, transcript.length);

  return (
    <>
      <Link href="/calls" className="detail-back"><ArrowLeft size={13} /> Calls</Link>

      <div className="detail-hdr">
        <div>
          <div className="detail-title">
            {call.caller_number && call.called_number ? (
              <span className="mono" style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                {call.caller_number} <ArrowRight size={15} style={{ color: "var(--text-3)" }} /> {call.called_number}
              </span>
            ) : (
              call.agent_name || "Call"
            )}
          </div>
          <div className="detail-subtitle">
            {formatTime(call.started_at)} · {formatDuration(call.duration_ms)}
            {call.agent_name && call.caller_number && call.called_number && <> · {call.agent_name}</>}
          </div>
        </div>
        <div className="detail-hdr-badges">
          {call.sentiment !== null && <SentimentBadge sentiment={call.sentiment} />}
          <span className={`badge ${call.direction === "inbound" ? "cyan" : "amber"}`}>
            {call.direction === "inbound" ? "Inbound" : "Outbound"}
          </span>
          <span className="badge indigo">{call.mode}</span>
          {call.status === "live"
            ? <span className="badge green">Live</span>
            : <span className="badge gray">Completed</span>}
        </div>
      </div>

      <div className="detail-grid">
        <div>
          <div className="card">
            <div className="detail-section">
              <div className="detail-section-title">Sentiment</div>
              <div className="detail-sentiment">
                <SentimentBadge sentiment={call.sentiment} />
                {call.sentiment === null ? (
                  <div className="detail-sentiment-reason">
                    Not scored. Sentiment is read from the finished transcript when a
                    call ends — calls that ended before scoring was enabled, or with no
                    caller speech, stay unscored rather than being shown as neutral.
                  </div>
                ) : call.sentiment_reason ? (
                  <div className="detail-sentiment-reason">{call.sentiment_reason}</div>
                ) : null}
              </div>
            </div>

            <div className="detail-section">
              <div className="detail-section-title">Parties</div>
              <dl className="detail-kv">
                <dt>From</dt>
                <dd className="mono">{call.caller_number || "—"}</dd>
                <dt>To</dt>
                <dd className="mono">{call.called_number || "—"}</dd>
                <dt>Agent</dt>
                <dd>{call.agent_name || "—"}</dd>
              </dl>
            </div>

            <div className="detail-section">
              <div className="detail-section-title">Timeline</div>
              <dl className="detail-kv">
                <dt>Started</dt>
                <dd>{formatTime(call.started_at)}</dd>
                <dt>Ended</dt>
                <dd>{call.ended_at ? formatTime(call.ended_at) : call.status === "live" ? "In progress" : "—"}</dd>
                <dt>Duration</dt>
                <dd>{formatDuration(call.duration_ms)}</dd>
                <dt>Turns</dt>
                <dd>{turns}</dd>
                <dt>Interruptions</dt>
                <dd>{call.barge_in_count}</dd>
                <dt>How it ended</dt>
                <dd title={call.close_reason ?? undefined}>{call.close_reason ? humanize(call.close_reason) : "—"}</dd>
              </dl>
            </div>

            {/* Workflow path and outcome; absent for single-prompt agents. */}
            {hasPath && (
              <div className="detail-section">
                <div className="detail-section-title">Path</div>
                <div className="detail-path">
                  {call.nodes_visited!.map((node, i) => (
                    <span key={`${node}-${i}`} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      {i > 0 && <ChevronRight size={13} className="detail-path-arrow" />}
                      <span className="badge gray">{node}</span>
                    </span>
                  ))}
                  {call.disposition && <span className="badge indigo">{call.disposition}</span>}
                </div>
              </div>
            )}

            {hasCaptured && (
              <div className="detail-section">
                <div className="detail-section-title">Captured</div>
                <dl className="detail-kv">
                  {Object.entries(captured).map(([k, v]) => (
                    <div key={k} style={{ display: "contents" }}>
                      <dt>{k}</dt>
                      <dd className="mono">{formatValue(v)}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            )}

            <div className="detail-section">
              <div className="detail-section-title">Session</div>
              <dl className="detail-kv">
                <dt>Session ID</dt>
                <dd className="detail-copy">
                  <span className="mono">{call.session_id}</span>
                  <CopyButton value={call.session_id} label="session ID" />
                </dd>
                <dt>Call ID</dt>
                <dd className="detail-copy">
                  <span className="mono">{call.call_id || "—"}</span>
                  {call.call_id && <CopyButton value={call.call_id} label="call ID" />}
                </dd>
              </dl>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Transcript</div>
            <div className="card-sub">
              {transcriptLoading
                ? "Loading…"
                : `${transcript.length} turn${transcript.length === 1 ? "" : "s"}`}
            </div>
          </div>
          {transcriptLoading ? (
            <div className="empty-state">Loading transcript…</div>
          ) : transcript.length === 0 ? (
            <div className="empty-state">No transcript was recorded for this call.</div>
          ) : (
            <CallTranscript entries={transcript} />
          )}
        </div>
      </div>
    </>
  );
}
