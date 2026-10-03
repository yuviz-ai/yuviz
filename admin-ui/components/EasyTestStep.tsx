"use client";

// The Easy "Test it" step. A new receptionist is inactive, so both variants
// run on a one-shot credential minted by Config for this test only: voice
// sends it as the first WebSocket frame (see lib/useWebCall), chat sends it
// with every turn. All text comes from lib/easyCopy; server detail text and
// the call hook's own error string are never shown.

import { useEffect, useState } from "react";
import { Agent, TestChannel, createTestSession, sendTestChat } from "@/lib/api";
import { CallState, useWebCall } from "@/lib/useWebCall";
import { easyCopy, easyErrorText } from "@/lib/easyCopy";

interface Props {
  tenantSlug: string;
  agent: Agent;
  channel: TestChannel;
  /** The test session to fix from, or null until it has at least one spoken turn. */
  onSession: (sessionId: string | null) => void;
}

type Turn = { role: "user" | "assistant"; text: string };

function Transcript({ turns }: { turns: Turn[] }) {
  if (turns.length === 0) return <div className="form-hint">{easyCopy.transcriptEmpty}</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {turns.map((t, i) => (
        <div key={i}>
          <div className="form-hint">{t.role === "user" ? easyCopy.youSaid : easyCopy.itSaid}</div>
          <div>{t.text}</div>
        </div>
      ))}
    </div>
  );
}

const STATUS_TEXT: Record<CallState, string> = {
  idle: easyCopy.statusIdle,
  connecting: easyCopy.statusConnecting,
  ready: easyCopy.statusReady,
  talking: easyCopy.statusTalking,
  thinking: easyCopy.statusThinking,
  speaking: easyCopy.statusSpeaking,
  ended: easyCopy.statusEnded,
  error: easyCopy.statusError,
};

function VoiceTest({ tenantSlug, agent, onSession }: Omit<Props, "channel">) {
  const [credential, setCredential] = useState<string | undefined>(undefined);
  const [startRequested, setStartRequested] = useState(false);
  const [minting, setMinting] = useState(false);
  const [mintError, setMintError] = useState<string | null>(null);
  const call = useWebCall(tenantSlug, agent.slug, credential);
  const { start, sessionId, transcript } = call;

  // The hook takes a credential on its next render, so start() must wait for
  // the render that carries the freshly minted one; calling it in the same
  // tick as the mint would find the previous, spent credential.
  useEffect(() => {
    if (!startRequested || credential === undefined) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStartRequested(false);
    setMinting(false);
    onSession(null);
    void start();
  }, [startRequested, credential, start, onSession]);

  // Fix is offered only once this session has a spoken turn. Not reported null
  // on mount: coming back to this step keeps the session already under test.
  useEffect(() => {
    if (sessionId !== null && transcript.length > 0) onSession(sessionId);
  }, [sessionId, transcript.length, onSession]);

  const handleStart = async () => {
    setMintError(null);
    setMinting(true);
    try {
      const minted = await createTestSession(tenantSlug, agent.id, "voice");
      setCredential(minted.credential);
      setStartRequested(true);
    } catch (e) {
      setMintError(easyErrorText(e));
      setMinting(false);
    }
  };

  const idle = call.state === "idle" || call.state === "ended" || call.state === "error";

  return (
    <div className="card">
      <div className="card-body">
        <div className="form-hint" style={{ marginBottom: 10 }}>{easyCopy.testHint}</div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
          {idle ? (
            <button className="btn btn-primary btn-sm" onClick={handleStart} disabled={minting}>
              {easyCopy.startTalking}
            </button>
          ) : (
            <button className="btn btn-ghost btn-sm" onClick={call.hangUp}>
              {easyCopy.stop}
            </button>
          )}
          <span className="form-hint" role="status">{STATUS_TEXT[call.state]}</span>
        </div>
        {mintError && <div role="alert" className="error-banner">{mintError}</div>}
        <Transcript turns={transcript} />
      </div>
    </div>
  );
}

function ChatTest({ tenantSlug, agent, onSession }: Omit<Props, "channel">) {
  const [session, setSession] = useState<{ credential: string; sessionId: string } | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleStart = async () => {
    setBusy(true);
    setError(null);
    try {
      const minted = await createTestSession(tenantSlug, agent.id, "chat");
      setSession({ credential: minted.credential, sessionId: minted.session_id });
      setTurns(minted.greeting ? [{ role: "assistant", text: minted.greeting }] : []);
      onSession(null);
    } catch (e) {
      setError(easyErrorText(e));
    } finally {
      setBusy(false);
    }
  };

  const handleSend = async () => {
    const message = draft.trim();
    if (!session || !message) return;
    setBusy(true);
    setError(null);
    setTurns((prev) => [...prev, { role: "user", text: message }]);
    setDraft("");
    try {
      const { reply } = await sendTestChat(tenantSlug, agent.id, {
        credential: session.credential,
        session_id: session.sessionId,
        message,
      });
      setTurns((prev) => [...prev, { role: "assistant", text: reply }]);
      onSession(session.sessionId);
    } catch (e) {
      setError(easyErrorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <div className="card-body">
        <div className="form-hint" style={{ marginBottom: 10 }}>{easyCopy.testHint}</div>
        {session === null ? (
          <button className="btn btn-primary btn-sm" onClick={handleStart} disabled={busy}>
            {busy ? easyCopy.working : easyCopy.startChatTest}
          </button>
        ) : (
          <>
            <Transcript turns={turns} />
            <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
              <input
                className="form-input"
                value={draft}
                placeholder={easyCopy.chatPlaceholder}
                maxLength={1000}
                disabled={busy}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSend()}
              />
              <button className="btn btn-primary btn-sm" onClick={handleSend} disabled={busy || draft.trim() === ""}>
                {easyCopy.send}
              </button>
            </div>
            <button
              className="btn btn-ghost btn-sm"
              style={{ marginTop: 8 }}
              onClick={handleStart}
              disabled={busy}
            >
              {easyCopy.startOver}
            </button>
          </>
        )}
        {error && <div role="alert" className="error-banner" style={{ marginTop: 10 }}>{error}</div>}
      </div>
    </div>
  );
}

export function EasyTestStep({ channel, ...rest }: Props) {
  return channel === "chat" ? <ChatTest {...rest} /> : <VoiceTest {...rest} />;
}
