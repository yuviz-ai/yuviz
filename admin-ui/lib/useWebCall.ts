"use client";

// Web-call engine for agent testing: mic capture, energy-based VAD, barge-in,
// and PCM playback against the webcall bridge.

import { useCallback, useEffect, useRef, useState } from "react";

const WEBCALL_URL = process.env.NEXT_PUBLIC_WEBCALL_URL || "ws://localhost:8300";
const SAMPLE_RATE = 16000;

// VAD: RMS dB relative to an adaptive room noise floor, not a fixed threshold.
const ONSET_FRAMES_REQUIRED = 4; // consecutive worklet callbacks of sustained speech to confirm onset
const SILENCE_MS_TO_END = 700; // hangover before declaring end-of-utterance
const NOISE_FLOOR_ADAPT_RATE = 0.02;
const ONSET_MARGIN_DB = 9;
// Stricter while the agent speaks so its own TTS leaking from speakers isn't read as barge-in.
const ONSET_MARGIN_DB_WHILE_AGENT_SPEAKING = 22;
const ONSET_FRAMES_REQUIRED_WHILE_AGENT_SPEAKING = 10;
// Ambient measurement window before any onset/offset decision.
const CALIBRATION_MS = 600;
// Hard cap in case silence detection never releases.
const MAX_UTTERANCE_MS = 15_000;
// Cap on the end_call teardown delay.
const MAX_END_CALL_WAIT_MS = 10_000;


export type CallState = "idle" | "connecting" | "ready" | "talking" | "thinking" | "speaking" | "ended" | "error";

export interface WebCall {
  state: CallState;
  transcript: { role: "user" | "assistant"; text: string; ts: number }[];
  errorMsg: string | null;
  micLevelPct: number;
  muted: boolean;
  setMuted: (m: boolean) => void;
  start: () => Promise<void>;
  hangUp: () => void;
  reset: () => void;
}

export function useWebCall(tenantSlug: string, agentSlug: string): WebCall {
  const [state, setState] = useState<CallState>("idle");
  const [transcript, setTranscript] = useState<{ role: "user" | "assistant"; text: string; ts: number }[]>([]);
  const [transcriptOpen, setTranscriptOpen] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [micLevelPct, setMicLevelPct] = useState(0);
  const [muted, setMutedState] = useState(false);
  const mutedRef = useRef<boolean>(false);

  // Bumped per call so a delayed end_call teardown can't tear down a newer call.
  const sessionGenRef = useRef<number>(0);
  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const workletRef = useRef<AudioWorkletNode | null>(null);
  const talkStartRef = useRef<number>(0);
  const playheadRef = useRef<number>(0);
  // Scheduled agent audio, so barge-in can stop it all at once.
  const activeSourcesRef = useRef<AudioBufferSourceNode[]>([]);
  // Refs, not state: the worklet callback is assigned once and would see stale closures.
  const recordingRef = useRef<boolean>(false);
  const agentSpeakingRef = useRef<boolean>(false);
  const anyAudioSentRef = useRef<boolean>(false);
  const noiseFloorDbRef = useRef<number>(-50);
  const onsetStreakRef = useRef<number>(0);
  const silenceMsAccumRef = useRef<number>(0);
  // Noise floor is calibrated per call; a fixed guess left recording stuck open.
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

  // Reset when the modal closes and tear down on unmount so no mic/WS outlives it.
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
    // createBuffer() throws on 0 frames, and TTS chunks can legitimately be empty.
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
      // Barge-in: stop playback locally and cancel generation server-side.
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

  const handleStart = useCallback(async () => {
    sessionGenRef.current += 1;
    mutedRef.current = false;
    setMutedState(false);
    setState("connecting");
    setErrorMsg(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true } });
      streamRef.current = stream;

      const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
      audioCtxRef.current = ctx;
      playheadRef.current = 0;
      noiseFloorDbRef.current = -50;
      calibrationSamplesRef.current = [];
      calibratingUntilRef.current = performance.now() + CALIBRATION_MS;
      await ctx.audioWorklet.addModule("/pcm-capture-worklet.js");

      const source = ctx.createMediaStreamSource(stream);
      const worklet = new AudioWorkletNode(ctx, "pcm-capture-processor");
      workletRef.current = worklet;
      source.connect(worklet);
      // Not connected to ctx.destination, to avoid echoing the mic back.

      const ws = new WebSocket(
        `${WEBCALL_URL}/webcall?tenant=${encodeURIComponent(tenantSlug)}&agent=${encodeURIComponent(agentSlug)}`,
      );
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        // Talk is enabled on service_ready, not on open.
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
          case "service_ready":
            setState("ready");
            break;
          case "stt_result":
            if (msg.text) setTranscript((prev) => [...prev, { role: "user", text: msg.text, ts: Date.now() }]);
            setState((s) => (s === "talking" ? "thinking" : s));
            break;
          case "tts_started":
            setState((s) => (s === "talking" ? s : "speaking"));
            break;
          case "tts_result":
            if (msg.text) setTranscript((prev) => [...prev, { role: "assistant", text: msg.text, ts: Date.now() }]);
            break;
          case "tts_chunk_final":
            setState((s) => (s === "talking" ? s : "ready"));
            break;
          case "end_call": {
            // end_call arrives before the farewell finishes playing; delay teardown until it does.
            const ctx = audioCtxRef.current;
            const remainingMs = ctx ? Math.max(0, (playheadRef.current - ctx.currentTime) * 1000) : 0;
            const gen = sessionGenRef.current;
            setState("ended");
            window.setTimeout(() => {
              if (sessionGenRef.current === gen) teardown();
            }, Math.min(remainingMs, MAX_END_CALL_WAIT_MS));
            break;
          }
          case "no_response":
            // Agent heard nothing usable and won't reply.
            setErrorMsg(msg.message);
            setState("ready");
            break;
          case "error":
            setErrorMsg(msg.message || "The agent reported an error.");
            if (msg.fatal) setState("error");
            break;
        }
      };

      // Hands-free VAD: onset arms recording (barging in if needed); silence ends the utterance.
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
          // Calibrating: no onset/offset decisions yet.
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

        // Muted: no audio sent and no onset, but level metering continues.
        if (mutedRef.current) {
          onsetStreakRef.current = 0;
          return;
        }

        if (!recordingRef.current) {
          // Adapt only while quiet and the agent is silent, or its audio drags the floor up.
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantSlug, agentSlug]);


  const setMuted = useCallback((m: boolean) => {
    mutedRef.current = m;
    setMutedState(m);
    if (m && recordingRef.current) {
      recordingRef.current = false;
      silenceMsAccumRef.current = 0;
      onsetStreakRef.current = 0;
      setState((s) => (s === "talking" ? "ready" : s));
    }
  }, []);

  const hangUp = useCallback(() => {
    teardown();
    setState("ended");
  }, []);

  const reset = useCallback(() => {
    teardown();
    setState("idle");
    setTranscript([]);
    setErrorMsg(null);
    setMicLevelPct(0);
  }, []);

  useEffect(() => () => teardown(), []);

  return {
    state, transcript, errorMsg, micLevelPct,
    muted, setMuted, start: handleStart, hangUp, reset,
  };
}
