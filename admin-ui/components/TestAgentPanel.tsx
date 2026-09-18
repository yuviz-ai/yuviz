"use client";

import { useEffect, useRef, useState } from "react";
import { Modal } from "@/components/Modal";
import { getToken } from "@/lib/auth";

const WEBCALL_URL = process.env.NEXT_PUBLIC_WEBCALL_URL || "ws://localhost:8300";
const SAMPLE_RATE = 16000;

function canSendAuthToken(url: string): boolean {
  try {
    const u = new URL(url);
    if (u.protocol === "wss:") return true;
    if (u.protocol === "ws:" && (u.hostname === "localhost" || u.hostname === "127.0.0.1")) {
      return true;
    }
  } catch {
    /* ignore */
  }
  return false;
}

// --- VAD (RMS dB, adaptive noise floor) ---
const ONSET_FRAMES_REQUIRED = 4; // sustained frames to confirm onset
const SILENCE_MS_TO_END = 700; // hangover before end-of-utterance
const NOISE_FLOOR_ADAPT_RATE = 0.02;
const ONSET_MARGIN_DB = 9;
// Stricter while agent speaks — speaker bleed looked like barge-in (farewell loop).
const ONSET_MARGIN_DB_WHILE_AGENT_SPEAKING = 22;
const ONSET_FRAMES_REQUIRED_WHILE_AGENT_SPEAKING = 10;
const CALIBRATION_MS = 600; // measure room before trusting onset
const MAX_UTTERANCE_MS = 15_000; // force end if silence detect fails
const MAX_END_CALL_WAIT_MS = 10_000; // cap end_call teardown wait

type CallState = "idle" | "connecting" | "ready" | "talking" | "thinking" | "speaking" | "ended" | "error";

export function TestAgentPanel({
  open,
  onClose,
  tenantSlug,
  agentSlug,
  useDraft = false,
  inline = false,
  onNodeChanged,
}: {
  open: boolean;
  onClose: () => void;
  tenantSlug: string;
  agentSlug: string;
  useDraft?: boolean; // unpublished draft (workflow editor)
  inline?: boolean; // side column — modal would cover the lit canvas
  onNodeChanged?: (node: { id: string; name: string; type: string }) => void;
}) {
  const [state, setState] = useState<CallState>("idle");
  const [transcript, setTranscript] = useState<{ text: string; ts: number }[]>([]);
  const [transcriptOpen, setTranscriptOpen] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [micLevelPct, setMicLevelPct] = useState(0);
  const [draftFellBack, setDraftFellBack] = useState(false);

  // Bump on start; stale end_call timers must not tear down a new session.
  const sessionGenRef = useRef<number>(0);
  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const workletRef = useRef<AudioWorkletNode | null>(null);
  const talkStartRef = useRef<number>(0);
  const playheadRef = useRef<number>(0);
  // Playing agent chunks — barge-in stops them all.
  const activeSourcesRef = useRef<AudioBufferSourceNode[]>([]);
  // Refs for VAD loop (worklet callback is assigned once — no stale state).
  const recordingRef = useRef<boolean>(false);
  const agentSpeakingRef = useRef<boolean>(false);
  const anyAudioSentRef = useRef<boolean>(false);
  const noiseFloorDbRef = useRef<number>(-50);
  const onsetStreakRef = useRef<number>(0);
  const silenceMsAccumRef = useRef<number>(0);
  // Calibrate noise floor (fixed -50dB guess stuck recording for ~57s).
  const calibratingUntilRef = useRef<number>(0);
  const calibrationSamplesRef = useRef<number[]>([]);
  const recordingStartedAtRef = useRef<number>(0);

  const stopAgentPlayback = () => {
    for (const src of activeSourcesRef.current) {
      try {
        src.onended = null;
        src.stop();
      } catch {
        // already finished — nothing to do
      }
    }
    activeSourcesRef.current = [];
    if (audioCtxRef.current) playheadRef.current = audioCtxRef.current.currentTime;
    agentSpeakingRef.current = false;
  };

  const teardown = () => {
    recordingRef.current = false;
    agentSpeakingRef.current = false;
    stopAgentPlayback();
    wsRef.current?.close();
    wsRef.current = null;
    workletRef.current?.disconnect();
    workletRef.current = null;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    audioCtxRef.current?.close().catch(() => {});
    audioCtxRef.current = null;
  };

  // Teardown on close/unmount — stray mic/WS is a privacy bug.
  useEffect(() => {
    if (!open) {
      teardown();
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setState("idle");
      setTranscript([]);
      setErrorMsg(null);
      setMicLevelPct(0);
    }
    return () => teardown();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const playPcmChunk = (buf: ArrayBuffer) => {
    const ctx = audioCtxRef.current;
    if (!ctx) return;
    const int16 = new Int16Array(buf);
    // Empty TTS chunk is valid (pre-is_final); createBuffer(0) throws.
    if (int16.length === 0) return;
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / (int16[i] < 0 ? 0x8000 : 0x7fff);

    const audioBuffer = ctx.createBuffer(1, float32.length, SAMPLE_RATE);
    audioBuffer.copyToChannel(float32, 0);
    const source = ctx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(ctx.destination);

    const startAt = Math.max(ctx.currentTime, playheadRef.current);
    source.start(startAt);
    playheadRef.current = startAt + audioBuffer.duration;
    agentSpeakingRef.current = true;
    activeSourcesRef.current.push(source);
    source.onended = () => {
      activeSourcesRef.current = activeSourcesRef.current.filter((s) => s !== source);
      if (activeSourcesRef.current.length === 0 && playheadRef.current <= ctx.currentTime + 0.05) {
        agentSpeakingRef.current = false;
      }
    };
  };

  const beginUtterance = () => {
    recordingRef.current = true;
    onsetStreakRef.current = 0;
    silenceMsAccumRef.current = 0;
    anyAudioSentRef.current = false;
    talkStartRef.current = performance.now();
    recordingStartedAtRef.current = talkStartRef.current;
    setErrorMsg(null);
    if (agentSpeakingRef.current) {
      // Barge-in while agent audio playing.
      stopAgentPlayback();
      wsRef.current?.send(JSON.stringify({ type: "cancel" }));
      wsRef.current?.send(JSON.stringify({ type: "playback_finished", interrupted: true }));
    }
    setState("talking");
  };

  const endUtterance = () => {
    recordingRef.current = false;
    if (anyAudioSentRef.current) {
      const durationMs = performance.now() - talkStartRef.current;
      wsRef.current?.send(
        JSON.stringify({ type: "speech_ended", duration_ms: Math.round(durationMs), energy_db: -20 }),
      );
      setState("thinking");
    } else {
      setState("ready");
    }
  };

  const handleStart = async () => {
    // Close any prior WS/mic first. Otherwise "Test again" during the
    // end_call farewell delay bumps sessionGen and skips the deferred
    // teardown — orphaned sockets keep holding webcall session slots.
    teardown();
    sessionGenRef.current += 1;
    setState("connecting");
    setErrorMsg(null);
    setDraftFellBack(false);

    const token = getToken();
    if (!token) {
      setState("error");
      setErrorMsg("Sign in again to run a test call.");
      return;
    }
    if (!canSendAuthToken(WEBCALL_URL)) {
      setState("error");
      setErrorMsg(
        "Test call needs a secure webcall URL (wss://), or ws://localhost. "
        + "Refusing to send your session token over cleartext.",
      );
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true } });
      streamRef.current = stream;

      const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
      audioCtxRef.current = ctx;
      playheadRef.current = 0;
      noiseFloorDbRef.current = -50; // placeholder — replaced by real measurement below
      calibrationSamplesRef.current = [];
      calibratingUntilRef.current = performance.now() + CALIBRATION_MS;
      await ctx.audioWorklet.addModule("/pcm-capture-worklet.js");

      const source = ctx.createMediaStreamSource(stream);
      const worklet = new AudioWorkletNode(ctx, "pcm-capture-processor");
      workletRef.current = worklet;
      source.connect(worklet);
      // Deliberately not connected to ctx.destination — we don't want the
      // caller's own mic echoed back to them.

      const ws = new WebSocket(
        `${WEBCALL_URL}/webcall?tenant=${encodeURIComponent(tenantSlug)}&agent=${encodeURIComponent(agentSlug)}` +
          (useDraft ? "&draft=1" : ""),
      );
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        ws.send(JSON.stringify({ type: "auth", token }));
      };
      ws.onerror = () => {
        setState("error");
        setErrorMsg("Connection to the agent failed. Is the webcall bridge running?");
      };
      ws.onclose = () => {
        setState((s) => (s === "error" ? s : "ended"));
      };
      ws.onmessage = (ev) => {
        if (ev.data instanceof ArrayBuffer) {
          playPcmChunk(ev.data);
          setState((s) => (s === "talking" ? s : "speaking"));
          return;
        }
        const msg = JSON.parse(ev.data);
        switch (msg.type) {
          case "auth_ok":
            // Wait for service_ready before allowing talk — matches the
            // documented wire protocol ordering in conversation.proto.
            break;
          case "service_ready":
            setState("ready");
            break;
          case "note":
            if (msg.kind === "draft_fallback") setDraftFellBack(true);
            if (msg.text) {
              setTranscript((t) => [...t, { text: msg.text, ts: Date.now() }]);
            }
            break;
          case "stt_result":
            if (msg.text) setTranscript((prev) => [...prev, { text: msg.text, ts: Date.now() }]);
            setState((s) => (s === "talking" ? "thinking" : s));
            break;
          case "tts_started":
            setState((s) => (s === "talking" ? s : "speaking"));
            break;
          case "tts_chunk_final":
            setState((s) => (s === "talking" ? s : "ready"));
            break;
          case "transfer":
            // Browser can't hand off — surface transfer in the transcript.
            setTranscript((t) => [...t, {
              text: `[the agent handed the call to ${msg.destination || "a human"}]`,
              ts: Date.now(),
            }]);
            break;
          case "end_call": {
            // Wait for scheduled farewell audio before teardown (else cuts mid-sentence).
            const ctx = audioCtxRef.current;
            const remainingMs = ctx ? Math.max(0, (playheadRef.current - ctx.currentTime) * 1000) : 0;
            const gen = sessionGenRef.current;
            setState("ended");
            window.setTimeout(() => {
              if (sessionGenRef.current === gen) teardown();
            }, Math.min(remainingMs, MAX_END_CALL_WAIT_MS));
            break;
          }
          case "workflow_node":
            onNodeChanged?.({ id: msg.node_id, name: msg.node_name, type: msg.node_type });
            break;
          case "no_response":
            // No reply coming — unblock the UI.
            setErrorMsg(msg.message);
            setState("ready");
            break;
          case "error":
            setErrorMsg(msg.message || "The agent reported an error.");
            if (msg.fatal) {
              setState("error");
              teardown();
            }
            break;
        }
      };

      // Hands-free VAD: onset arms recording; silence sends speech_ended.
      worklet.port.onmessage = (ev: MessageEvent<ArrayBuffer>) => {
        const pcm16 = new Int16Array(ev.data);
        if (pcm16.length === 0) return;

        let sumSquares = 0;
        for (let i = 0; i < pcm16.length; i++) {
          const s = pcm16[i] / 0x8000;
          sumSquares += s * s;
        }
        const rms = Math.sqrt(sumSquares / pcm16.length);
        const db = 20 * Math.log10(Math.max(rms, 1e-8));
        setMicLevelPct(Math.max(0, Math.min(100, (db + 60) * 1.6)));

        const now = performance.now();
        if (now < calibratingUntilRef.current) {
          // Calibrating — no onset decisions yet.
          calibrationSamplesRef.current.push(db);
          return;
        }
        if (calibrationSamplesRef.current.length > 0) {
          const avg =
            calibrationSamplesRef.current.reduce((a, b) => a + b, 0) / calibrationSamplesRef.current.length;
          noiseFloorDbRef.current = avg;
          calibrationSamplesRef.current = [];
        }

        const frameMs = (pcm16.length / SAMPLE_RATE) * 1000;
        const speaking = agentSpeakingRef.current;
        const onsetMargin = speaking ? ONSET_MARGIN_DB_WHILE_AGENT_SPEAKING : ONSET_MARGIN_DB;
        const framesNeeded = speaking ? ONSET_FRAMES_REQUIRED_WHILE_AGENT_SPEAKING : ONSET_FRAMES_REQUIRED;
        const isSpeechFrame = db > noiseFloorDbRef.current + onsetMargin;

        if (!recordingRef.current) {
          // Slowly adapt the noise floor only while quiet AND the agent
          // isn't talking (its own audio would otherwise drag the
          // baseline up while it plays).
          if (!isSpeechFrame && !speaking) {
            noiseFloorDbRef.current =
              noiseFloorDbRef.current * (1 - NOISE_FLOOR_ADAPT_RATE) + db * NOISE_FLOOR_ADAPT_RATE;
          }
          if (isSpeechFrame) {
            onsetStreakRef.current++;
            if (onsetStreakRef.current >= framesNeeded) {
              onsetStreakRef.current = 0;
              beginUtterance();
            }
          } else {
            onsetStreakRef.current = 0;
          }
        }

        if (recordingRef.current) {
          if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(ev.data);
            anyAudioSentRef.current = true;
          }
          if (now - recordingStartedAtRef.current >= MAX_UTTERANCE_MS) {
            // Safety backstop — see MAX_UTTERANCE_MS's comment. Whatever
            // the VAD thinks, don't let a single utterance run forever.
            silenceMsAccumRef.current = 0;
            endUtterance();
          } else if (isSpeechFrame) {
            silenceMsAccumRef.current = 0;
          } else {
            silenceMsAccumRef.current += frameMs;
            if (silenceMsAccumRef.current >= SILENCE_MS_TO_END) {
              silenceMsAccumRef.current = 0;
              endUtterance();
            }
          }
        }
      };
    } catch (e) {
      setState("error");
      setErrorMsg(e instanceof Error ? e.message : "Could not access the microphone.");
    }
  };

  const statusText: Record<CallState, string> = {
    idle: "Run a live test call with this agent's real voice, prompt, and tools — using your laptop's microphone, no phone call involved.",
    connecting: "Connecting…",
    ready: draftFellBack
      ? "Listening — draft invalid, running the live flow. Just start talking."
      : useDraft
        ? "Listening — running your draft. Just start talking whenever you're ready."
        : "Listening — just start talking whenever you're ready.",
    talking: "Hearing you…",
    thinking: "Thinking…",
    speaking: "Speaking… (start talking to interrupt)",
    ended: "Call ended.",
    error: errorMsg || "Something went wrong.",
  };

  const active = state === "talking" || state === "thinking" || state === "speaking";
  const listening = state === "ready" || state === "talking" || state === "thinking" || state === "speaking";

  const body = (
      <div style={{ textAlign: "center", padding: "12px 4px" }}>
        <div
          className={`test-agent-orb${active ? " active" : ""}${state === "speaking" ? " speaking" : ""}`}
          style={{ width: 72, height: 72, borderRadius: "50%", margin: "0 auto 16px" }}
        />

        <div style={{ fontSize: ".85rem", color: "var(--text-2)", marginBottom: 6, minHeight: "2.6em" }}>
          {statusText[state]}
        </div>

        {listening && (
          <div
            style={{
              height: 6,
              background: "var(--border, #333)",
              borderRadius: 3,
              overflow: "hidden",
              margin: "0 auto 14px",
              maxWidth: 220,
            }}
          >
            <div
              style={{
                height: "100%",
                width: `${micLevelPct}%`,
                background: state === "talking" ? "#d33" : "#4a4",
                transition: "width 60ms linear",
              }}
            />
          </div>
        )}

        {errorMsg && state !== "error" && (
          <div className="error-banner" style={{ marginBottom: 14, textAlign: "left" }}>
            {errorMsg}
          </div>
        )}

        {transcript.length > 0 && (
          <div style={{ marginBottom: 14, textAlign: "left" }}>
            <button
              className="btn btn-ghost btn-sm"
              style={{ width: "100%", justifyContent: "space-between", display: "flex" }}
              onClick={() => setTranscriptOpen((o) => !o)}
            >
              <span>Transcript ({transcript.length})</span>
              <span>{transcriptOpen ? "▲" : "▼"}</span>
            </button>
            {transcriptOpen && (
              <div style={{ maxHeight: 160, overflowY: "auto", padding: "8px 4px", fontSize: ".78rem", color: "var(--text-3)" }}>
                <div className="hint" style={{ marginBottom: 8 }}>
                  Only your own recognized speech is shown — the agent&apos;s spoken replies aren&apos;t sent back as
                  text, only as audio.
                </div>
                {transcript.map((t, i) => (
                  <div key={i} style={{ marginBottom: 6, fontStyle: "italic" }}>
                    “{t.text}”
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="hint" style={{ marginBottom: 16 }}>
          Hands-free — no buttons. Just start talking, including to interrupt the agent mid-response.
          Headphones recommended (without them the agent may occasionally mishear its own voice as you
          interrupting). No memory across separate test sessions.
        </div>

        {state === "idle" && (
          <button className="btn btn-primary btn-sm" onClick={handleStart}>
            Start Test
          </button>
        )}

        {(state === "ended" || state === "error") && (
          <button className="btn btn-ghost btn-sm" onClick={handleStart}>
            Start New Test
          </button>
        )}
      </div>
  );

  if (inline) {
    // Inline: hide when closed (modal handles that itself).
    if (!open) return null;
    return (
      <div className="wf-testpanel">
        <div className="wf-inspector-hdr">
          <span className="wf-inspector-title">Test call</span>
          <button className="btn btn-ghost btn-sm" style={{ marginLeft: "auto" }} onClick={onClose}>
            Close
          </button>
        </div>
        {body}
      </div>
    );
  }

  return (
    <Modal open={open} title="Test Agent" onClose={onClose} footer={null}>
      {body}
    </Modal>
  );
}
