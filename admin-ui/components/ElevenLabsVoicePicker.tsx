"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, ElevenLabsVoice, ProviderConfig, createProvider, listElevenLabsVoices, updateProvider } from "@/lib/api";
import { ELEVENLABS_LANGUAGES } from "@/lib/engineCatalog";
import { SecretRefInput } from "./SecretRefInput";

// Module-level so it survives unmounts; cleared on reload or Refresh.
const voicesCache = new Map<string, ElevenLabsVoice[]>();

// Prefer validated verified_languages; labels.language is unvalidated free text.
function voicePrimaryLanguage(v: ElevenLabsVoice): string | null {
  return v.verified_languages[0]?.language ?? v.labels.language ?? null;
}

// ElevenLabs voices belong to an account, so a provider_config with an api_key_ref must exist
// first; without one this renders a one-time "connect" step.
export function ElevenLabsVoicePicker({
  tenantId,
  provider,
  onProviderCreated,
  onVoicePicked,
  onLanguageDetected,
  disabled = false,
  isCurrentAssignment = true,
}: {
  tenantId: string;
  provider: ProviderConfig | null;
  onProviderCreated: (provider: ProviderConfig) => void;
  onVoicePicked: (provider: ProviderConfig) => void;
  onLanguageDetected: (language: string) => void;
  // Locks the whole picker (can't connect, can't expand/reselect) — see
  // LocalVoicePicker's disabled prop for why this exists.
  disabled?: boolean;
  // False when `provider` is a tenant fallback, not the agent's assigned one; its `voice` then
  // isn't this agent's voice and must not be shown as selected. Defaults to true.
  isCurrentAssignment?: boolean;
}) {
  const [apiKeyRef, setApiKeyRef] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

  const [voices, setVoices] = useState<ElevenLabsVoice[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [savingLanguage, setSavingLanguage] = useState(false);
  const [languageError, setLanguageError] = useState<string | null>(null);
  // null = no explicit choice; defaults to English when available.
  const [languageFilter, setLanguageFilter] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  // Latest provider id for handleRefresh's stale-response check (a closure would be stale).
  const currentProviderId = useRef(provider?.id);
  useEffect(() => {
    currentProviderId.current = provider?.id;
  }, [provider?.id]);

  useEffect(() => {
    if (!provider) return;
    const cached = voicesCache.get(provider.id);
    if (cached) {
      // Cache hit: `loading` starts true, so clear it here too.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setVoices(cached);
      setLoading(false);
      return;
    }
    let ignore = false;
    // Reset on provider change too, so the previous provider's state doesn't flash.
    setLoading(true);
    setError(null);
    listElevenLabsVoices(provider.id)
      .then((v) => {
        if (ignore) return;
        voicesCache.set(provider.id, v);
        setVoices(v);
      })
      .catch((e) => {
        if (!ignore) setError(e instanceof ApiError ? e.detail : String(e));
      })
      .finally(() => {
        if (!ignore) setLoading(false);
      });
    return () => {
      ignore = true;
    };
  }, [provider]);

  const handleRefresh = () => {
    if (!provider) return;
    // Drop the response if the provider changed while in flight.
    const providerId = provider.id;
    voicesCache.delete(providerId);
    setLoading(true);
    setError(null);
    listElevenLabsVoices(providerId)
      .then((v) => {
        voicesCache.set(providerId, v);
        if (providerId === currentProviderId.current) setVoices(v);
      })
      .catch((e) => {
        if (providerId === currentProviderId.current) setError(e instanceof ApiError ? e.detail : String(e));
      })
      .finally(() => {
        if (providerId === currentProviderId.current) setLoading(false);
      });
  };

  const handleConnect = async () => {
    setConnecting(true);
    setConnectError(null);
    try {
      const created = await createProvider(tenantId, {
        name: "ElevenLabs", role: "tts", engine: "elevenlabs", api_key_ref: apiKeyRef,
      });
      onProviderCreated(created);
    } catch (e) {
      setConnectError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setConnecting(false);
    }
  };

  if (!provider) {
    return (
      <div>
        {connectError && <div className="error-banner">{connectError}</div>}
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label">
            ElevenLabs API Key Reference <span className="hint">e.g. env:ELEVENLABS_API_KEY — never a raw key, see Secret Manager</span>
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            <div style={{ flex: 1 }}>
              <SecretRefInput
                value={apiKeyRef}
                onChange={setApiKeyRef}
                placeholder="env:ELEVENLABS_API_KEY"
                disabled={disabled}
              />
            </div>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={handleConnect}
              disabled={connecting || !apiKeyRef.trim() || disabled}
            >
              {connecting ? "Connecting…" : "Connect"}
            </button>
          </div>
        </div>
      </div>
    );
  }

  const handleSynthesisLanguageChange = async (language: string) => {
    setSavingLanguage(true);
    setLanguageError(null);
    try {
      const updated = await updateProvider(provider.id, { language: language || null });
      onVoicePicked(updated);
    } catch (e) {
      setLanguageError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSavingLanguage(false);
    }
  };

  const handlePick = async (voiceId: string, language: string | null) => {
    setSaving(voiceId);
    setError(null);
    try {
      const updated = await updateProvider(provider.id, { voice: voiceId });
      onVoicePicked(updated);
      if (language) onLanguageDetected(language);
      setExpanded(false);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSaving(null);
    }
  };

  if (loading) return <div className="empty-state">Loading voices…</div>;
  if (error) return <div className="error-banner">{error}</div>;
  if (!voices || voices.length === 0) return <div className="empty-state">No voices on this ElevenLabs account.</div>;

  // Filter options come from the account's own voices; ElevenLabs has no fixed list.
  const languages = Array.from(new Set(voices.map(voicePrimaryLanguage).filter((l): l is string => !!l))).sort();
  // Ignore a filter no longer in the list, else it could match nothing with no way to reset it.
  const activeLanguageFilter =
    languageFilter && languages.includes(languageFilter) ? languageFilter : languages.includes("en") ? "en" : "all";
  const filteredVoices =
    activeLanguageFilter === "all" ? voices : voices.filter((v) => voicePrimaryLanguage(v) === activeLanguageFilter);
  const selectedVoice = voices.find((v) => v.voice_id === provider.voice);

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
            <span style={{ color: isCurrentAssignment ? "var(--green)" : "var(--text-3)", marginRight: 8 }}>
              {isCurrentAssignment ? "✓" : "•"}
            </span>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 500 }}>{selectedVoice.name}</div>
              <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                {isCurrentAssignment ? "Primary voice" : "Already set on this account — not yet assigned to this agent"}
              </div>
            </div>
          </>
        ) : (
          <div style={{ flex: 1, color: "var(--text-3)" }}>Select a voice…</div>
        )}
        <span style={{ color: "var(--text-3)" }}>▾</span>
      </button>
    );
  }

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <div style={{ fontSize: ".78rem", fontWeight: 500 }}>
          {selectedVoice
            ? isCurrentAssignment
              ? `Selected: ${selectedVoice.name}`
              : `${selectedVoice.name} is set on this account — pick a voice below to assign it to this agent`
            : "Select a voice"}
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button type="button" className="btn btn-ghost btn-sm" onClick={handleRefresh} disabled={loading}>
            {loading ? "Refreshing…" : "Refresh"}
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setExpanded(false)}>
            Close
          </button>
        </div>
      </div>
      {languageError && <div className="error-banner">{languageError}</div>}
      <div className="form-group" style={{ marginBottom: 12 }}>
        <label className="form-label">
          Synthesis Language <span className="hint">what language the voice actually speaks on calls — not just which voices are shown below</span>
        </label>
        <select
          className="form-select"
          value={provider.language ?? ""}
          disabled={savingLanguage || disabled}
          onChange={(e) => handleSynthesisLanguageChange(e.target.value)}
        >
          <option value="">Auto-detect from text (default)</option>
          {ELEVENLABS_LANGUAGES.map((l) => (
            <option key={l.value} value={l.value}>
              {l.label}
            </option>
          ))}
        </select>
      </div>
      {languages.length > 1 && (
        <div style={{ marginBottom: 12 }}>
          <div className="form-label" style={{ marginBottom: 6 }}>
            Filter voices below by native language/accent
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <button
            type="button"
            className={`btn btn-sm ${activeLanguageFilter === "all" ? "btn-primary" : "btn-ghost"}`}
            onClick={() => setLanguageFilter("all")}
          >
            All Languages
          </button>
          {languages.map((lang) => (
            <button
              key={lang}
              type="button"
              className={`btn btn-sm ${activeLanguageFilter === lang ? "btn-primary" : "btn-ghost"}`}
              onClick={() => setLanguageFilter(lang)}
            >
              {lang === "en" ? "English (Default)" : lang}
            </button>
          ))}
          </div>
        </div>
      )}
      {filteredVoices.length === 0 && <div className="empty-state">No voices match this language.</div>}
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {filteredVoices.map((v) => {
          const isSelected = provider.voice === v.voice_id;
          const isSaving = saving === v.voice_id;
          return (
            <div key={v.voice_id} className="kb-row">
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 500 }}>{v.name}</div>
                <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                  {v.category}
                  {v.labels.gender ? ` · ${v.labels.gender}` : ""}
                  {v.labels.accent ? ` · ${v.labels.accent}` : ""}
                  {voicePrimaryLanguage(v) ? ` · ${voicePrimaryLanguage(v)}` : ""}
                </div>
              </div>
              {v.preview_url && <audio controls src={v.preview_url} style={{ height: 30, maxWidth: 200 }} />}
              <button
                type="button"
                className={`btn btn-sm ${isSelected ? "btn-primary" : "btn-ghost"}`}
                onClick={() => handlePick(v.voice_id, voicePrimaryLanguage(v))}
                disabled={saving !== null || disabled}
              >
                {isSaving ? "…" : isSelected ? "Selected" : "Select"}
              </button>
            </div>
          );
        })}
      </div>
      <div style={{ fontSize: ".7rem", color: "var(--text-3)", marginTop: 10 }}>
        Voices come from this ElevenLabs account directly, and are cached after the first load — add or remove voices at elevenlabs.io, then click Refresh above to see the change here.
      </div>
    </div>
  );
}
