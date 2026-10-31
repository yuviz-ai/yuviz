"use client";

// In-browser test call beside the agent editor (engine: lib/useWebCall).

import { useEffect, useRef, useState } from "react";
import { Headphones, MessageSquare, Mic, MicOff, Phone, Send, type LucideIcon } from "lucide-react";
import { ApiError, ChatTestSession, sendChatTest, startChatTest } from "@/lib/api";
import { useWebCall } from "@/lib/useWebCall";
import { VoiceInputButton } from "./VoiceInputButton";

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
  { key: "chat", label: "Chat", icon: MessageSquare },
];

export function AgentTestPanel({
  tenantSlug, agentSlug, agentId, agentName, savePending, blockedReason = null, voiceReady = true, chatReady = true,
}: {
  tenantSlug: string;
  agentSlug: string;
  agentId: string;
  agentName: string;
  /** False when a hearing, thinking or voice model the call needs has no working key. */
  voiceReady?: boolean;
  chatReady?: boolean;
  /** A test must run the latest edits, so starting waits for autosave. */
  savePending: boolean;
  /** Why unsaved edits can't be saved yet (held on a live agent, or a required field is empty). */
  blockedReason?: string | null;
}) {
  const call = useWebCall(tenantSlug, agentSlug);
  const [mode, setMode] = useState<Mode>("chat");
  const [chatStatus, setChatStatus] = useState<"idle" | "live" | "ended">("idle");
  const [voiceNotice, setVoiceNotice] = useState(false);

  // No working voice services: say so out loud (a recording, since no voice provider can) and point to chat.
  const startCall = () => {
    if (voiceReady) { setVoiceNotice(false); call.start(); return; }
    setVoiceNotice(true);
    new Audio("/sounds/voice-test-unavailable.m4a").play().catch(() => {});
  };
  const logRef = useRef<HTMLDivElement | null>(null);
  const live = (call.state !== "idle" && call.state !== "ended" && call.state !== "error") || chatStatus === "live";
  const ended = !live && (mode === "chat" ? chatStatus === "ended" : call.state === "ended");
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
        <b className="ed-test-name" title={agentName}>{agentName || "Test your agent"}</b>
        {live && <span className="badge green ed-test-live"><i aria-hidden="true" /> Live</span>}
        {ended && <span className="badge gray ed-test-live ended"><i aria-hidden="true" /> End</span>}
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

      {mode === "chat" ? (
        <ChatTest tenantSlug={tenantSlug} agentId={agentId} savePending={savePending} blockedReason={blockedReason}
          onStatus={setChatStatus} />
      ) : soon ? (
        <div className="ed-test-coming">
          <span className="badge gray">Coming soon</span>
          <div>{soon}</div>
          <div className="ed-test-hint">For now, use Browser to talk to your agent.</div>
        </div>
      ) : (
        <>
          <div className="ed-test-log" ref={logRef}>
            {voiceNotice && !live ? (
              <div className="ed-test-empty ed-test-unavailable">
                <Headphones size={26} />
                Voice services for this agent aren&apos;t set up yet, so a voice test can&apos;t run.
                {chatReady ? " Use chat to test what the agent says." : " Set up its hearing, thinking and voice models in Settings."}
                {chatReady && (
                  <button type="button" className="btn btn-primary btn-sm" onClick={() => { setVoiceNotice(false); setMode("chat"); }}>
                    <MessageSquare size={13} /> Use chat
                  </button>
                )}
              </div>
            ) : call.transcript.length === 0 ? (
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
            <button className="btn btn-primary btn-sm ed-test-btn" onClick={startCall} disabled={savePending || !!blockedReason}>
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

// Typed test: the agent's instructions and thinking model answer each message; no voice, tools or documents.
function ChatTest({ tenantSlug, agentId, savePending, blockedReason, onStatus }: {
  tenantSlug: string;
  agentId: string;
  savePending: boolean;
  blockedReason: string | null;
  onStatus: (status: "idle" | "live" | "ended") => void;
}) {
  const [session, setSession] = useState<ChatTestSession | null>(null);
  const [messages, setMessages] = useState<{ role: "user" | "agent"; text: string }[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ended, setEnded] = useState(false);
  const logRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length, busy]);

  useEffect(() => {
    onStatus(session ? "live" : ended ? "ended" : "idle");
  }, [session, ended, onStatus]);
  useEffect(() => () => onStatus("idle"), [onStatus]);

  const end = () => {
    setSession(null);
    setEnded(true);
    setDraft("");
  };

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const s = await startChatTest(tenantSlug, agentId);
      setEnded(false);
      setSession(s);
      setMessages(s.greeting ? [{ role: "agent", text: s.greeting }] : []);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 429 ? "Too many tests just now. Try again in a minute." : e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  const send = async () => {
    const text = draft.trim();
    if (!session || !text || busy) return;
    setDraft("");
    setMessages((m) => [...m, { role: "user", text }]);
    setBusy(true);
    setError(null);
    try {
      const { reply } = await sendChatTest(tenantSlug, agentId, { credential: session.credential, session_id: session.session_id, message: text });
      setMessages((m) => [...m, { role: "agent", text: reply }]);
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      if (status === 404) {
        setSession(null);
        setEnded(true);
        setError("This chat expired. Start a new one.");
      } else {
        setError(
          status === 429 ? "This chat has reached its message limit. Start a new one."
            : status === 502 ? "The agent's thinking model didn't answer. Check it in Settings, then try again."
            : e instanceof ApiError ? e.detail : String(e),
        );
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="ed-test-log" ref={logRef}>
        {messages.length === 0 ? (
          <div className="ed-test-empty">
            <MessageSquare size={26} />
            Type to your agent to check its answers. Replies use its instructions and thinking model.
          </div>
        ) : (
          messages.map((m, i) => <div key={i} className={`ed-bubble${m.role === "user" ? " me" : ""}`}>{m.text}</div>)
        )}
        {busy && session && <div className="ed-bubble ed-typing" aria-label="Agent is typing"><span /><span /><span /></div>}
        {ended && messages.length > 0 && <div className="ed-test-ended">Conversation ended</div>}
      </div>
      {error && <div className="error-banner">{error}</div>}
      {!session ? (
        <button className="btn btn-primary btn-sm ed-test-btn" onClick={start} disabled={busy || savePending || !!blockedReason}>
          {blockedReason ?? (savePending ? "Saving your changes…" : busy ? "Starting…" : ended ? "Start a new chat" : "Start chat")}
        </button>
      ) : (
        <>
          <form className="ed-chat-input" onSubmit={(e) => { e.preventDefault(); void send(); }}>
            <input
              className="form-input"
              value={draft}
              maxLength={1000}
              placeholder="Type a message…"
              aria-label="Message to the agent"
              onChange={(e) => setDraft(e.target.value)}
              disabled={busy}
              autoFocus
            />
            <VoiceInputButton
              tenantSlug={tenantSlug}
              agentId={agentId}
              disabled={busy}
              onText={(text) => setDraft((d) => (d.trim() ? `${d.trim()} ${text}` : text))}
              onError={setError}
            />
            <button type="submit" className="btn btn-primary btn-sm" disabled={busy || !draft.trim()} aria-label="Send">
              <Send size={14} />
            </button>
          </form>
          <button className="btn btn-danger btn-sm ed-test-btn" onClick={end}>End conversation</button>
        </>
      )}
      <div className="ed-test-hint">Chat checks what the agent says. Voice, tools and documents are tested with a call.</div>
    </>
  );
}
