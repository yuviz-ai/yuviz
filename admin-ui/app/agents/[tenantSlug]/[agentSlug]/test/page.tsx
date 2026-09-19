"use client";

// Test an agent on a real call, as a page rather than a modal: a transcript
// that stays readable while you talk, and call controls beside it. The modal
// it replaces had the transcript behind a collapsed toggle, which is the
// wrong shape for the thing you are actually watching.
//
// The call itself is lib/useWebCall — identical engine, different chrome.

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { Agent, ApiError, getAgent } from "@/lib/api";
import { useWebCall } from "@/lib/useWebCall";

const STATUS_TEXT: Record<string, string> = {
  idle: "Ready when you are.",
  connecting: "Connecting…",
  ready: "Listening — just start talking.",
  talking: "Hearing you…",
  thinking: "Thinking…",
  speaking: "Speaking — talk over it to interrupt.",
  ended: "Call ended.",
  error: "Something went wrong.",
};

export default function AgentTestPage() {
  const { tenantSlug, agentSlug } = useParams<{ tenantSlug: string; agentSlug: string }>();
  const router = useRouter();
  const [agent, setAgent] = useState<Agent | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const call = useWebCall(tenantSlug, agentSlug);
  const logRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    getAgent(tenantSlug, agentSlug)
      .then(setAgent)
      .catch((e) => setLoadError(e instanceof ApiError ? e.detail : String(e)));
  }, [tenantSlug, agentSlug]);

  // Keep the newest line in view without yanking the page around.
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [call.transcript.length]);

  const live = call.state !== "idle" && call.state !== "ended" && call.state !== "error";
  const listening = ["ready", "talking", "thinking", "speaking"].includes(call.state);

  if (loadError) return <div className="error-banner">{loadError}</div>;

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 14 }}>
        <button
          className="btn btn-ghost btn-sm"
          onClick={() => router.push(`/agents/${tenantSlug}/${agentSlug}`)}
        >
          ← {agent?.name ?? "Agent"}
        </button>
        <div style={{ marginLeft: "auto" }}>
          <span className={`badge ${live ? "green" : "gray"}`}>{live ? "On a call" : "Idle"}</span>
        </div>
      </div>

      <div className="test-layout">
        <div className="card test-transcript">
          <div className="card-hdr">
            <span className="card-title">Live transcript</span>
            <span className="card-sub">{STATUS_TEXT[call.state] ?? ""}</span>
          </div>
          <div className="test-log" ref={logRef}>
            {call.transcript.length === 0 ? (
              <div className="empty-state" style={{ padding: 24 }}>
                Start the session and the conversation appears here as it happens.
              </div>
            ) : (
              call.transcript.map((t, i) => (
                <div key={i} className="test-turn">
                  <span className="test-turn-who">{t.role === "user" ? "You" : "Agent"}</span>
                  <span>{t.text}</span>
                </div>
              ))
            )}
          </div>
        </div>

        <aside className="card test-controls">
          <div className="card-hdr">
            <span className="card-title">Voice call</span>
          </div>
          <div className="card-body" style={{ textAlign: "center" }}>
            <div
              className={`test-agent-orb${live ? " active" : ""}${call.state === "speaking" ? " speaking" : ""}`}
              style={{ width: 78, height: 78, borderRadius: "50%", margin: "4px auto 14px" }}
            />
            <div className="test-agent-name">{agent?.name ?? "…"}</div>
            <div className="test-status">{STATUS_TEXT[call.state] ?? ""}</div>

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

            {call.errorMsg && <div className="error-banner" style={{ textAlign: "left" }}>{call.errorMsg}</div>}

            <div className="test-actions">
              {!live ? (
                <button className="btn btn-primary btn-sm" onClick={call.start}>
                  {call.state === "idle" ? "Start session" : "Start a new session"}
                </button>
              ) : (
                <>
                  <button
                    className={`btn btn-sm ${call.muted ? "btn-primary" : "btn-ghost"}`}
                    onClick={() => call.setMuted(!call.muted)}
                    aria-pressed={call.muted}
                  >
                    {call.muted ? "🔇 Unmute" : "🎙 Mute"}
                  </button>
                  <button className="btn btn-danger btn-sm" onClick={call.hangUp}>
                    End session
                  </button>
                </>
              )}
            </div>

            {call.muted && live && (
              <div className="test-muted-note">
                Mic muted — the agent can&apos;t hear you, and won&apos;t be interrupted.
              </div>
            )}

            <div className="test-hint">
              Hands-free: no push-to-talk. Just speak, including over the agent to interrupt it.
              Headphones recommended — without them it can mistake its own voice for you.
            </div>
          </div>
        </aside>
      </div>
    </>
  );
}
