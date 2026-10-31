"use client";

import { useEffect, useState } from "react";
import { Check } from "lucide-react";
import { ApiError, PlatformAiSettings, getCurrentUser, getPlatformAiSettings, setPlatformAiSettings } from "@/lib/api";

// Superadmin only: whether workspaces may use local models, and which platform keys are set in .env.
export function PlatformAiCard() {
  const [settings, setSettings] = useState<PlatformAiSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getCurrentUser()
      .then((me) => (me.role === "superadmin" ? getPlatformAiSettings().then(setSettings) : undefined))
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  if (!settings) return error ? <div className="error-banner">{error}</div> : null;

  const toggle = async (on: boolean) => {
    setSaving(true);
    setError(null);
    try {
      const saved = await setPlatformAiSettings(on);
      setSettings({ ...settings, local_models_enabled: saved.local_models_enabled });
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="card" style={{ marginBottom: 14 }}>
      <div className="card-hdr">
        <div>
          <div className="card-title">Platform AI</div>
          <div className="card-sub" style={{ marginLeft: 0 }}>
            Models every workspace can pick, run on the platform&apos;s keys. Only superadmins see this.
          </div>
        </div>
      </div>
      <div className="card-body">
        {error && <div className="error-banner">{error}</div>}
        <div className="ai-toggle-row" style={{ marginBottom: 14 }}>
          <label className="toggle-switch">
            <input
              type="checkbox"
              aria-label="Allow local models"
              checked={settings.local_models_enabled}
              disabled={saving}
              onChange={(e) => void toggle(e.target.checked)}
            />
            <span className="toggle-slider" />
          </label>
          <div>
            <div className="form-label" style={{ margin: 0 }}>Allow local models</div>
            <div className="form-hint" style={{ marginTop: 2 }}>Offers Ollama, Whisper and Kokoro, which run on this server, to every workspace.</div>
          </div>
        </div>
        <div className="form-label">Platform keys</div>
        <div className="ai-chips">
          {settings.keys.map((k) => (
            <span key={k.key_env} className={`ai-chip${k.configured ? " on" : ""}`} title={k.key_env}>
              {k.configured && <Check size={12} />} {k.providers.join(", ")}
              {!k.configured && <span className="ai-chip-tag">Not set</span>}
            </span>
          ))}
        </div>
        <div className="form-hint">Set a key in the root .env (for example OPENAI_API_KEY) and restart the services to offer that provider.</div>
      </div>
    </section>
  );
}
