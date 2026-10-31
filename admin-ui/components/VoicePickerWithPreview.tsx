"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown, Play, Square } from "lucide-react";
import { ApiError, previewVoice } from "@/lib/api";
import { ElevenLabsVoice } from "@/lib/api";
import { chosenVoice } from "@/lib/voicePicker";

type Row = { voice_id: string; name: string; labels?: Record<string, unknown> };

function PlayCircle({ name, on, onClick }: { name: string; on: boolean; onClick: (e: React.MouseEvent) => void }) {
  return (
    <button
      type="button"
      className={`voice-play${on ? " on" : ""}`}
      onClick={onClick}
      aria-label={on ? `Stop ${name}` : `Play ${name}`}
      title={on ? "Stop" : `Hear ${name}`}
    >
      {on ? <Square size={11} fill="currentColor" /> : <Play size={13} />}
    </button>
  );
}

export function VoicePickerWithPreview({
  voices,
  currentVoiceId,
  currentVoiceLabel,
  placeholder,
  providerId,
  canPreview = () => true,
  preview,
  onPick,
}: {
  voices: ElevenLabsVoice[];
  currentVoiceId?: string | null;
  /** Shown for a saved voice that is not in `voices`. */
  currentVoiceLabel?: string | null;
  placeholder: string;
  /** The voice config previews are spoken through; none = no previews. */
  providerId?: string | null;
  canPreview?: (voiceId: string) => boolean;
  /** Fetches a voice's sample; when given, every listed voice can be played without a voice config. */
  preview?: (voiceId: string) => Promise<Blob>;
  onPick: (voiceId: string, label: string) => void;
}) {
  const [playing, setPlaying] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [isOpen, setIsOpen] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    const close = (e: MouseEvent) => { if (!rootRef.current?.contains(e.target as Node)) setIsOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [isOpen]);

  const currentVoice = chosenVoice(voices, currentVoiceId, currentVoiceLabel);

  const playPreview = async (e: React.MouseEvent, voice: { voice_id: string; name: string }) => {
    e.stopPropagation();
    if (!preview && !providerId) return;
    if (playing === voice.voice_id) {
      audioRef.current?.pause();
      setPlaying(null);
      return;
    }

    setPlaying(voice.voice_id);
    setPreviewError(null);
    try {
      const text = voice.name;
      const blob = preview ? await preview(voice.voice_id) : await previewVoice(providerId!, text, voice.voice_id);
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
      urlRef.current = URL.createObjectURL(blob);
      audioRef.current?.pause();
      const audio = new Audio(urlRef.current);
      audioRef.current = audio;
      audio.onended = () => setPlaying(null);
      audio.onerror = () => {
        setPreviewError("Could not play audio");
        setPlaying(null);
      };
      await audio.play();
    } catch (e) {
      setPreviewError(e instanceof ApiError ? e.detail : "Error playing preview");
      setPlaying(null);
    }
  };

  const meta = (v: Row) => {
    const gender = String(v.labels?.gender ?? "").toLowerCase();
    const accent = String(v.labels?.accent ?? "").trim();
    return {
      symbol: gender === "female" ? "♀" : gender === "male" ? "♂" : null,
      accent: accent ? accent[0].toUpperCase() + accent.slice(1) : null,
    };
  };

  const canPlay = (v: Row) => !!(preview || providerId) && canPreview(v.voice_id);

  const label = (v: Row, strong = false) => {
    const m = meta(v);
    return (
      <>
        {m.symbol && <span className="voice-gender" aria-hidden="true">{m.symbol}</span>}
        <span className={`voice-name${strong ? " strong" : ""}`}>{v.name}</span>
        {m.accent && <span className="voice-accent">{m.accent}</span>}
      </>
    );
  };

  return (
    <div className="voice-picker-container" ref={rootRef}>
      <div className={`voice-picker-field${isOpen ? " open" : ""}`}>
        <button
          type="button"
          className="voice-picker-trigger"
          aria-haspopup="listbox"
          aria-expanded={isOpen}
          onClick={() => setIsOpen(!isOpen)}
        >
          {currentVoice ? label(currentVoice) : <span className="voice-picker-text placeholder">{placeholder}</span>}
          <ChevronDown size={16} className="voice-picker-chevron" />
        </button>
        {currentVoice && canPlay(currentVoice) && (
          <PlayCircle name={currentVoice.name} on={playing === currentVoice.voice_id} onClick={(e) => playPreview(e, currentVoice)} />
        )}
      </div>

      {isOpen && (
        <div className="voice-options-list" role="listbox">
          {voices.map((v) => (
            <div
              key={v.voice_id}
              className={`voice-option${currentVoiceId === v.voice_id ? " selected" : ""}`}
              onClick={() => {
                onPick(v.voice_id, v.name);
                setIsOpen(false);
              }}
              role="option"
              aria-selected={currentVoiceId === v.voice_id}
            >
              {label(v, currentVoiceId === v.voice_id)}
              <span className="voice-option-spacer" />
              {canPlay(v) && <PlayCircle name={v.name} on={playing === v.voice_id} onClick={(e) => playPreview(e, v)} />}
            </div>
          ))}
        </div>
      )}

      {previewError && <div className="cf-preview-error">{previewError}</div>}
    </div>
  );
}
