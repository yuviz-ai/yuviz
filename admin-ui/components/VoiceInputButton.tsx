"use client";

// Mic button for typing by voice: records a short clip, the agent's own hearing model turns it into text.

import { useEffect, useRef, useState } from "react";
import { Loader2, Mic, Square } from "lucide-react";
import { ApiError, transcribeVoiceInput } from "@/lib/api";

const MAX_MS = 55_000;
const TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"];

export function VoiceInputButton({ tenantSlug, agentId, onText, onError, disabled = false }: {
  tenantSlug: string;
  agentId: string;
  onText: (text: string) => void;
  onError: (message: string | null) => void;
  disabled?: boolean;
}) {
  const [state, setState] = useState<"idle" | "recording" | "transcribing">("idle");
  const [secs, setSecs] = useState(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const timers = useRef<number[]>([]);

  const clearTimers = () => { timers.current.forEach((t) => window.clearTimeout(t)); timers.current = []; };
  useEffect(() => () => { clearTimers(); recorder.current?.stream.getTracks().forEach((t) => t.stop()); }, []);

  const start = async () => {
    onError(null);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      onError("Allow microphone access in the browser to type by voice.");
      return;
    }
    const type = TYPES.find((t) => MediaRecorder.isTypeSupported(t));
    const rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    const chunks: Blob[] = [];
    rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    rec.onstop = async () => {
      clearTimers();
      stream.getTracks().forEach((t) => t.stop());
      const clip = new Blob(chunks, { type: (rec.mimeType || "audio/webm").split(";")[0] });
      if (!clip.size) { setState("idle"); return; }
      setState("transcribing");
      try {
        const text = await transcribeVoiceInput(tenantSlug, agentId, clip);
        if (text) onText(text);
        else onError("Nothing was heard. Try again a little closer to the mic.");
      } catch (e) {
        // No hearing model the platform can reach: say so out loud, then in text.
        if (e instanceof ApiError && e.status === 400 && /none is set up|needs a cloud hearing/i.test(e.detail)) {
          new Audio("/sounds/voice-input-unavailable.m4a").play().catch(() => {});
          onError("Voice input isn't set up yet. Please type your message instead.");
          return;
        }
        onError(e instanceof ApiError && e.status === 429 ? "Too many voice inputs just now. Try again in a minute."
          : e instanceof ApiError ? `Couldn't turn that into text: ${e.detail}` : String(e));
      } finally {
        setState("idle");
      }
    };
    recorder.current = rec;
    rec.start();
    setSecs(0);
    setState("recording");
    const began = Date.now();
    const tick = () => { setSecs(Math.floor((Date.now() - began) / 1000)); timers.current.push(window.setTimeout(tick, 500)); };
    tick();
    timers.current.push(window.setTimeout(() => rec.state === "recording" && rec.stop(), MAX_MS));
  };

  const stop = () => { if (recorder.current?.state === "recording") recorder.current.stop(); };

  return (
    <button
      type="button"
      className={`btn btn-sm voice-input-btn${state === "recording" ? " recording" : ""}`}
      onClick={state === "recording" ? stop : start}
      disabled={disabled || state === "transcribing"}
      aria-label={state === "recording" ? "Stop and use what you said" : "Speak instead of typing"}
      title={state === "recording" ? "Stop and use what you said" : "Speak instead of typing (uses this agent's hearing model)"}
    >
      {state === "transcribing" ? <Loader2 size={14} className="spin" />
        : state === "recording" ? <><Square size={11} fill="currentColor" /> {secs}s</>
        : <Mic size={14} />}
    </button>
  );
}
