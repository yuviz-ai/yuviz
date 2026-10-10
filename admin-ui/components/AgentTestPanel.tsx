"use client";

// In-browser test call beside the agent editor (engine: lib/useWebCall).

import { useEffect, useRef, useState } from "react";
import { Headphones, MessageSquare, Mic, MicOff, Phone, type LucideIcon } from "lucide-react";
import { useWebCall } from "@/lib/useWebCall";

const STATUS_TEXT: Record<string, string> = {
  connecting: "Connecting…",
  ready: "Listening — just start talking.",
  talking: "Hearing you…",
  thinking: "Thinking…",
  speaking: "Speaking — talk over it to interrupt.",
  ended: "Call ended.",
  error: "Something went wrong.",
};

export type Mode = "browser" | "phone" | "chat";

export const MODES: { key: Mode; label: string; icon: LucideIcon; soon?: string }[] = [
  { key: "browser", label: "Browser", icon: Headphones },
  { key: "phone", label: "Phone", icon: Phone, soon: "Get a real call from your agent on your own phone." },
  { key: "chat", label: "Chat", icon: MessageSquare, soon: "Type messages to your agent and read its replies." },
];

export function AgentTestPanel({ tenantSlug, agentSlug, savePending, blockedReason = null }: {
  tenantSlug: string;
  agentSlug: string;
  /** A test must run the latest edits, so starting waits for autosave. */
  savePending: boolean;
  /** Why unsaved edits can't be saved yet (held on a live agent, or a required field is empty). */
  blockedReason?: string | null;
}) {
  const call = useWebCall(tenantSlug, agentSlug);
  const [mode, setMode] = useState<Mode>("browser");
  const logRef = useRef<HTMLDivElement | null>(null);
  const live = call.state !== "idle" && call.state !== "ended" && call.state !== "error";
  const listening = ["ready", "talking", "thinking", "speaking"].includes(call.state);
  const soon = MODES.find((m) => m.key === mode)?.soon;
  const startedAt = useRef<number | null>(null);
  const [lastTest, setLastTest] = useState<{ at: number; secs: number } | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [call.transcript.length]);

  useEffect(() => {
    if (listening && startedAt.current === null) startedAt.current = Date.now();
    if (!live && startedAt.current !== null) {
      const at = Date.now();
      setLastTest({ at, secs: Math.round((at - startedAt.current) / 1000) });
      setNow(at);
      startedAt.current = null;
    }
  }, [listening, live]);

  useEffect(() => {
    if (!lastTest) return;
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, [lastTest]);

  const ago = (ms: number) => {
    const mins = Math.floor(ms / 60_000);
    return mins < 1 ? "just now" : mins < 60 ? `${mins} min ago` : `${Math.floor(mins / 60)} h ago`;
  };
  const length = (s: number) => `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;

  return (
    <div className="card ed-test">
      <div className="ed-test-hdr">
        <b>Test your agent</b>
        {live && <span className="badge green">On a call</span>}
      </div>

      <div className="ed-test-modes" role="tablist" aria-label="How to test">
        {MODES.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            role="tab"
            aria-selected={mode === key}
            className={`ed-test-mode${mode === key ? " active" : ""}`}
            disabled={live && key !== mode}
            onClick={() => setMode(key)}
          >
            <span className="ed-test-mode-name"><Icon size={13} /> {label}</span>          </button>
        ))}
      </div>

      {soon ? (
        <div className="ed-test-coming">
          <span className="badge gray">Coming soon</span>
          <div>{soon}</div>
          <div className="ed-test-hint">For now, use Browser to talk to your agent.</div>
        </div>
      ) : (
        <>
          <div className="ed-test-log" ref={logRef}>
            {call.transcript.length === 0 ? (
              <div className="ed-test-empty">
                <Headphones size={26} />
                Talk to your agent from this browser. Changes are saved before every test.
              </div>
            ) : (
              call.transcript.map((t, i) => (
                <div key={i} className={`ed-bubble${t.role === "user" ? " me" : ""}`}>{t.text}</div>
              ))
            )}
          </div>

          {STATUS_TEXT[call.state] && <div className="ed-test-status">{STATUS_TEXT[call.state]}</div>}
          {listening && (
            <div className="test-meter" aria-hidden="true">
              <div
                className="test-meter-fill"
                style={{
                  width: `${call.muted ? 0 : call.micLevelPct}%`,
                  background: call.state === "talking" ? "var(--red)" : "var(--green)",
                }}
              />
            </div>
          )}
          {call.errorMsg && <div className="error-banner">{call.errorMsg}</div>}

          {!live ? (
            <button className="btn btn-primary btn-sm ed-test-btn" onClick={call.start} disabled={savePending || !!blockedReason}>
              {blockedReason ?? (savePending ? "Saving your changes…" : "Start test call")}
            </button>
          ) : (
            <div className="ed-test-actions">
              <button
                className={`btn btn-sm ${call.muted ? "btn-primary" : "btn-ghost"}`}
                onClick={() => call.setMuted(!call.muted)}
                aria-pressed={call.muted}
              >
                {call.muted ? <><MicOff size={13} /> Unmute</> : <><Mic size={13} /> Mute</>}
              </button>
              <button className="btn btn-danger btn-sm" onClick={call.hangUp}>End call</button>
            </div>
          )}
          <div className="ed-test-hint">
            {lastTest && !live
              ? `Last test: ${ago(now - lastTest.at)} · ${length(lastTest.secs)}`
              : "Use headphones, so the agent doesn't hear itself."}
          </div>
        </>
      )}
    </div>
  );
}
