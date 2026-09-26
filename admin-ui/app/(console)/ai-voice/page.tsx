"use client";

import { useState } from "react";
import { ProvidersPanel } from "@/components/ProvidersPanel";

type AiVoiceSection = "speech" | "llm" | "embedding";

const TABS: { id: AiVoiceSection; label: string }[] = [
  { id: "speech", label: "Speech Services" },
  { id: "llm", label: "Language Model" },
  { id: "embedding", label: "Embeddings" },
];

export default function AiVoicePage() {
  const [section, setSection] = useState<AiVoiceSection>("speech");

  return (
    <>
      <div style={{ display: "flex", gap: 4, marginBottom: 16 }}>
        {TABS.map((tab) => (
          <button
            key={tab.id}
            className={`btn btn-sm ${section === tab.id ? "btn-primary" : "btn-ghost"}`}
            onClick={() => setSection(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {section === "speech" && <ProvidersPanel allowedRoles={["stt", "tts"]} title="Speech Services" />}
      {section === "llm" && <ProvidersPanel allowedRoles={["llm"]} title="Language Model" />}
      {section === "embedding" && <ProvidersPanel allowedRoles={["embedding"]} title="Embedding Providers" />}
    </>
  );
}
