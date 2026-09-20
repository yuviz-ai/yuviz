"use client";

// One call, on its own page rather than in a modal. A call is a record
// people link to, keep open next to something else, and scroll a long
// transcript inside — none of which a dialog does well.

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  ApiError, Call, TranscriptEntry, getCall, getTranscript,
} from "@/lib/api";
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
    // Fetched independently of the call itself: a transcript that is missing
    // or fails to load should leave the rest of the page usable, which is
    // exactly the case turn_count = 0 does not reliably predict.
    //
    // Reset on sessionId change, not just at mount: Next keeps this component
    // mounted when navigating between two calls, so without this the second
    // call would show the first one's transcript until its fetch resolved.
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
        <Link href="/calls" className="detail-back">← Call Log</Link>
        <div className="error-banner">{error}</div>
      </>
    );
  }
  if (!call) return <div className="empty-state">Loading…</div>;

  const hasPath = call.nodes_visited != null && call.nodes_visited.length > 0;
  const captured = call.extracted_variables ?? {};
  const hasCaptured = Object.keys(captured).length > 0;

  return (
    <>
      <Link href="/calls" className="detail-back">← Call Log</Link>

      <div className="detail-hdr">
        <div>
          <div className="detail-title">
            {call.caller_number && call.called_number
              ? `${call.caller_number} → ${call.called_number}`
              : call.agent_name || "Call"}
          </div>
          <div className="detail-subtitle">
            {formatTime(call.started_at)} · {formatDuration(call.duration_ms)}
          </div>
        </div>
        <div className="detail-hdr-badges">
          {/* Only when there is a real reading — the Sentiment card below
              already explains an unscored call, and a bare "—" floating
              among the status badges reads as a broken badge. */}
          {call.sentiment !== null && <SentimentBadge sentiment={call.sentiment} />}
          <span className={`badge ${call.direction === "inbound" ? "cyan" : "amber"}`}>{call.direction}</span>
          <span className="badge indigo">{call.mode}</span>
          <span className={`badge ${call.status === "live" ? "green" : "gray"}`}>{call.status}</span>
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
                <dd>{call.ended_at ? formatTime(call.ended_at) : "—"}</dd>
                <dt>Duration</dt>
                <dd>{formatDuration(call.duration_ms)}</dd>
                <dt>Turns</dt>
                <dd>{call.turn_count}</dd>
                <dt>Barge-ins</dt>
                <dd>{call.barge_in_count}</dd>
                <dt>Close reason</dt>
                <dd className="mono">{call.close_reason || "—"}</dd>
              </dl>
            </div>

            {/* The path this call took through its workflow, and how it
                ended — the questions a single-prompt agent simply can't
                answer (docs/workflow.md §7.1). Absent entirely for one. */}
            {hasPath && (
              <div className="detail-section">
                <div className="detail-section-title">Path</div>
                <div className="detail-path">
                  {call.nodes_visited!.map((node, i) => (
                    <span key={`${node}-${i}`} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      {i > 0 && <span className="detail-path-arrow">→</span>}
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
                      <dd className="mono">{String(v)}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            )}

            <div className="detail-section">
              <div className="detail-section-title">Session</div>
              <dl className="detail-kv">
                <dt>Session ID</dt>
                <dd className="mono">{call.session_id}</dd>
                <dt>Call ID</dt>
                <dd className="mono">{call.call_id || "—"}</dd>
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
            <div className="empty-state">Loading…</div>
          ) : transcript.length === 0 ? (
            <div className="empty-state">No transcript available for this call.</div>
          ) : (
            <div className="transcript">
              {transcript.map((t) => (
                <div key={t.id} className="transcript-turn">
                  <div className="transcript-turn-no">
                    Turn {t.turn_number}
                    {t.interrupted && <span className="badge amber">interrupted</span>}
                  </div>
                  <div className="transcript-line">
                    <span className="transcript-who">Caller</span>
                    <span className={`transcript-text${t.caller_text ? "" : " empty"}`}>
                      {t.caller_text || "(nothing heard)"}
                    </span>
                  </div>
                  <div className="transcript-line agent">
                    <span className="transcript-who">Agent</span>
                    <span className={`transcript-text${t.ai_response ? "" : " empty"}`}>
                      {t.ai_response || "(no reply)"}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
