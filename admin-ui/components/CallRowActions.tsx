"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import Link from "next/link";
import { Eye, FileText, LucideIcon, MoreHorizontal } from "lucide-react";
import { Modal } from "@/components/Modal";
import { CallTranscript } from "@/components/CallTranscript";
import { sentimentLabel } from "@/components/SentimentBadge";
import { ApiError, CallWithTenant, TranscriptEntry, getTranscript } from "@/lib/api";
import { outcomeOf } from "@/lib/callOutcome";
import { useMenuKeys } from "@/lib/useMenuKeys";

type Panel = "transcript" | "error";

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
  const { triggerRef, menuRef, onMenuKey } = useMenuKeys(menuPos !== null, () => setMenuPos(null));

  useEffect(() => {
    if (!menuPos) return;
    const close = () => setMenuPos(null);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [menuPos]);

  useEffect(() => {
    if (panel !== "transcript" || !call.has_transcript) return;
    let active = true;
    getTranscript(call.session_id)
      .then((t) => active && setTranscript(t))
      .catch((e) => {
        if (!active) return;
        setError(e instanceof ApiError ? e.detail : String(e));
        setPanel("error");
      });
    return () => {
      active = false;
    };
  }, [panel, call.session_id, call.has_transcript]);

  const openTranscript = () => {
    setTranscript(call.has_transcript ? null : []);
    setPanel("transcript");
  };

  // A campaign call that never connected has no transcript or conversation page.
  const isCampaignCall = call.session_id.startsWith("campaign:");
  const items: { icon: LucideIcon; label: string; run: () => void }[] = [
    ...(isCampaignCall ? [] : [{ icon: FileText, label: "Transcript & summary", run: openTranscript }]),
    { icon: Eye, label: "View details", run: onOpen },
  ];

  return (
    // The row itself opens the call; keep clicks and keys here from reaching it.
    <div className="call-actions" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
      <button
        ref={triggerRef}
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
          <div
            ref={menuRef}
            className="ed2-menu-pop call-menu"
            role="menu"
            aria-label="Call actions"
            style={{ top: menuPos.top, left: menuPos.left }}
            onKeyDown={onMenuKey}
          >
            {items.map((item) => (
              <button
                key={item.label}
                role="menuitem"
                onClick={() => {
                  setMenuPos(null);
                  item.run();
                }}
              >
                <item.icon size={15} /> {item.label}
              </button>
            ))}
          </div>
        </>,
        document.body,
      )}

      {panel && createPortal(
        <Modal
          open
          title={panel === "transcript" ? "Transcript & summary" : "Something went wrong"}
          onClose={() => setPanel(null)}
          footer={panel !== "error" && !isCampaignCall && (
            <Link href={`/calls/${call.session_id}`} className="btn btn-ghost btn-sm">Open full details</Link>
          )}
        >
          {panel !== "error" && <div className="form-hint" style={{ marginTop: 0, marginBottom: 10 }}>{callLabel(call)}</div>}
          {panel === "error" && <div className="error-banner" style={{ margin: 0 }}>{error}</div>}
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
