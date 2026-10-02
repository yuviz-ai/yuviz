"use client";

// Preview the typed line in a voice. Synthesized server-side so credentials never reach the browser;
// unsupported engines return a 400 whose message is shown as-is.

import { useEffect, useRef, useState } from "react";
import { ApiError, previewVoice } from "@/lib/api";

export function VoicePreviewButton({
  providerId,
  text,
  label = "Play",
  compact = false,
}: {
  providerId: string | null | undefined;
  text: string;
  label?: string;
  compact?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);

  // A blob URL is a document-lifetime handle; without revoking it every
  // preview leaks one for as long as the editor stays open.
  useEffect(
    () => () => {
      audioRef.current?.pause();
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    },
    [],
  );

  const play = async () => {
    if (!providerId || !text.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const blob = await previewVoice(providerId, text.trim());
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
      urlRef.current = URL.createObjectURL(blob);
      audioRef.current?.pause();
      const audio = new Audio(urlRef.current);
      audioRef.current = audio;
      audio.onended = () => setBusy(false);
      audio.onerror = () => {
        setError("Could not play the audio.");
        setBusy(false);
      };
      await audio.play();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
      setBusy(false);
    }
  };

  const disabled = !providerId || !text.trim() || busy;
  const why = !providerId
    ? "Pick a voice on the Start step first"
    : !text.trim()
      ? "Write something for it to say"
      : undefined;

  return (
    <>
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        onClick={play}
        disabled={disabled}
        title={why ?? "Hear this line in the flow's voice"}
      >
        {busy ? "▶ Playing…" : `▶ ${label}`}
      </button>
      {error && !compact && <div className="cf-preview-error">{error}</div>}
      {error && compact && <span className="cf-preview-error">{error}</span>}
    </>
  );
}
