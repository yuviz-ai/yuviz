"use client";

import { useState } from "react";
import { Check, ChevronDown, Circle, Mars, Venus } from "lucide-react";
import { ApiError, ProviderConfig, createProvider } from "@/lib/api";
import { VOICES_BY_ENGINE, VoiceGender } from "@/lib/engineCatalog";

const ENGINE_LABELS: Record<string, string> = {
  macos: "Basic voice",
  kokoro: "Built-in voice",
};

const GENDER_BADGE: Record<VoiceGender, React.ReactNode> = {
  female: <Venus size={12} />,
  male: <Mars size={12} />,
  neutral: <Circle size={10} />,
};

const GENDER_FILTERS: { value: VoiceGender | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "female", label: "Female" },
  { value: "male", label: "Male" },
  { value: "neutral", label: "Neutral" },
];

// Single-engine voice picker (macOS or Kokoro). Picking a voice find-or-creates a provider_config
// by engine+voice and sets agent.tts_config_id.
export function LocalVoicePicker({
  engine,
  tenantId,
  providers,
  value,
  onChange,
  onProviderCreated,
  onLanguageDetected,
  disabled = false,
}: {
  engine: "macos" | "kokoro";
  tenantId: string;
  providers: ProviderConfig[];
  value: string | null | undefined;
  onChange: (providerId: string) => void;
  onProviderCreated: (provider: ProviderConfig) => void;
  onLanguageDetected: (language: string) => void;
  // Locks the picker once the voice was committed elsewhere and a change here would be a no-op.
  disabled?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [creatingId, setCreatingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [genderFilter, setGenderFilter] = useState<VoiceGender | "all">("all");

  const selected = providers.find((p) => p.id === value);
  const voices = VOICES_BY_ENGINE[engine] ?? [];
  const selectedVoice = voices.find((v) => selected?.engine === engine && selected?.voice === v.id);

  const handlePick = async (voiceId: string, language: string) => {
    setError(null);
    setCreatingId(voiceId);
    try {
      const existing = providers.find((p) => p.role === "tts" && p.engine === engine && p.voice === voiceId);
      if (existing) {
        onChange(existing.id);
      } else {
        const created = await createProvider(tenantId, { name: `${engine} — ${voiceId}`, role: "tts", engine, voice: voiceId });
        onProviderCreated(created);
        onChange(created.id);
      }
      onLanguageDetected(language);
      setExpanded(false);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setCreatingId(null);
    }
  };

  const availableGenders = new Set(voices.map((v) => v.gender));
  const filtered = genderFilter === "all" ? voices : voices.filter((v) => v.gender === genderFilter);

  if (!expanded) {
    return (
      <button
        type="button"
        className="kb-row"
        style={{ width: "100%", textAlign: "left", cursor: disabled ? "not-allowed" : "pointer", background: "none", border: "1px solid var(--border-2)", borderRadius: "var(--rs)", opacity: disabled ? 0.6 : 1 }}
        onClick={() => !disabled && setExpanded(true)}
        disabled={disabled}
      >
        {selectedVoice ? (
          <>
            <Check size={15} style={{ color: "var(--green)", marginRight: 8, flexShrink: 0 }} />
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 500 }}>{selectedVoice.label}</div>
              <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>{ENGINE_LABELS[engine]} · Primary voice</div>
            </div>
          </>
        ) : (
          <div style={{ flex: 1, color: "var(--text-3)" }}>Select a voice…</div>
        )}
        <ChevronDown size={15} style={{ color: "var(--text-3)", flexShrink: 0 }} />
      </button>
    );
  }

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <div style={{ fontSize: ".78rem", fontWeight: 500 }}>
          {selectedVoice ? `Selected: ${selectedVoice.label}` : "Select a voice"}
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setExpanded(false)}>
          Close
        </button>
      </div>
      {availableGenders.size > 1 && (
        <div style={{ display: "flex", gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
          {GENDER_FILTERS.filter((f) => f.value === "all" || availableGenders.has(f.value)).map((f) => (
            <button
              key={f.value}
              type="button"
              className={`btn btn-sm ${genderFilter === f.value ? "btn-primary" : "btn-ghost"}`}
              onClick={() => setGenderFilter(f.value)}
            >
              {f.value !== "all" && GENDER_BADGE[f.value]}
              {f.label}
            </button>
          ))}
        </div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {filtered.map(({ id: voiceId, label, gender, language, sampleUrl }) => {
          const isSelected = selectedVoice?.id === voiceId;
          const isCreating = creatingId === voiceId;
          return (
            <div key={voiceId} className="kb-row">
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 500 }}>
                  <span style={{ marginRight: 4, opacity: 0.6, verticalAlign: "-1px" }}>{GENDER_BADGE[gender]}</span>
                  {label}
                </div>
                <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>{language}</div>
              </div>
              {sampleUrl && <audio controls src={sampleUrl} style={{ height: 30, maxWidth: 200 }} />}
              <button
                type="button"
                className={`btn btn-sm ${isSelected ? "btn-primary" : "btn-ghost"}`}
                onClick={() => handlePick(voiceId, language)}
                disabled={creatingId !== null || disabled}
              >
                {isCreating ? "…" : isSelected ? "Selected" : "Select"}
              </button>
            </div>
          );
        })}
      </div>
      {engine === "kokoro" && (
        <div style={{ fontSize: ".7rem", color: "var(--text-3)", marginTop: 10 }}>
          Voices not yet used on this machine download automatically on first call.
        </div>
      )}
    </div>
  );
}
