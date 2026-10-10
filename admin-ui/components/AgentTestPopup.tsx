"use client";

// Floating test call opened from an agent card (engine: lib/useWebCall). Unmounting ends the call.

import { useEffect, useRef, useState } from "react";
import { Mic, MicOff, Phone, PhoneOff, X, type LucideIcon } from "lucide-react";
import { MODES, type Mode } from "@/components/AgentTestPanel";
import { useWebCall } from "@/lib/useWebCall";

const STATUS_TEXT: Record<string, string> = {
  idle: "Press the call button to talk",
  connecting: "Connecting…",
  ready: "Listening — just start talking",
  talking: "Hearing you…",
  thinking: "Thinking…",
  speaking: "Speaking — talk over it to interrupt",
  ended: "Call ended",
  error: "Something went wrong",
};

export function AgentTestPopup({ tenantSlug, agentSlug, name, icon: Icon, onClose }: {
  tenantSlug: string;
  agentSlug: string;
  name: string;
  icon: LucideIcon;
  onClose: () => void;
}) {
  const call = useWebCall(tenantSlug, agentSlug);
  const [mode, setMode] = useState<Mode>("browser");
  const soon = MODES.find((m) => m.key === mode)?.soon;
  const logRef = useRef<HTMLDivElement | null>(null);
  const live = call.state !== "idle" && call.state !== "ended" && call.state !== "error";

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [call.transcript.length]);

  return (
    <div className="test-pop" role="dialog" aria-label={`Test ${name}`}>
      <div className="test-pop-hdr">
        <div className="test-pop-ico">
          <Icon size={18} />
          {live && <i />}
        </div>
        <div className="test-pop-id">
          <b>{name}</b>
          <span>{live ? "Test call in progress" : "Test call"}</span>
        </div>
        {live ? (
          <button className="btn btn-danger btn-sm" onClick={call.hangUp}>End call</button>
        ) : (
          <button className="test-pop-close" aria-label="Close" onClick={onClose}><X size={16} /></button>
        )}
      </div>

      <div className="test-pop-tabs" role="tablist" aria-label="How to test">
        {MODES.map(({ key, label, icon: TabIcon }) => (
          <button
            key={key}
            role="tab"
            aria-selected={mode === key}
            className={mode === key ? "active" : ""}
            disabled={live && key !== mode}
            onClick={() => setMode(key)}
          >
            <TabIcon size={14} /> {label}
          </button>
        ))}
      </div>

      {soon ? (
        <div className="test-pop-body">
          <div className="test-pop-empty">
            <span className="badge gray">Coming soon</span>
            {soon}
            <span className="test-pop-hint">For now, use Browser to talk to your agent.</span>
          </div>
        </div>
      ) : (
        <>
          <div className="test-pop-body" ref={logRef}>
            {call.transcript.length === 0 ? (
              <div className="test-pop-empty">{STATUS_TEXT[call.state]}</div>
            ) : (
              call.transcript.map((t, i) => (
                <div key={i} className={`ed-bubble${t.role === "user" ? " me" : ""}`}>{t.text}</div>
              ))
            )}
          </div>
          {call.errorMsg && <div className="error-banner test-pop-err">{call.errorMsg}</div>}
          <div className="test-pop-foot">
            {call.transcript.length > 0 && <div className="test-pop-status">{STATUS_TEXT[call.state]}</div>}
            <div className="test-pop-actions">
              {live && (
                <button
                  className={`test-pop-round ghost${call.muted ? " on" : ""}`}
                  aria-label={call.muted ? "Unmute" : "Mute"}
                  aria-pressed={call.muted}
                  onClick={() => call.setMuted(!call.muted)}
                >
                  {call.muted ? <MicOff size={18} /> : <Mic size={18} />}
                </button>
              )}
              <button
                className={`test-pop-round${live ? " end" : ""}`}
                aria-label={live ? "End call" : "Start call"}
                onClick={live ? call.hangUp : call.start}
              >
                {live ? <PhoneOff size={18} /> : <Phone size={18} />}
              </button>
            </div>
            {!live && <div className="test-pop-hint">Use headphones so the agent doesn&apos;t hear itself.</div>}
          </div>
        </>
      )}
    </div>
  );
}
