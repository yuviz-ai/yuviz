"use client";

import { useState } from "react";
import { ProvidersPanel } from "@/components/ProvidersPanel";
import { PlatformAiCard } from "@/components/PlatformAiCard";

type AiVoiceSection = "speech" | "llm" | "embedding";

const TABS: { id: AiVoiceSection; label: string }[] = [
  { id: "speech", label: "Speech Services" },
  { id: "llm", label: "Language Model" },
  { id: "embedding", label: "Embeddings" },
];

export function AiVoicePanel() {
  const [section, setSection] = useState<AiVoiceSection>("speech");

  return (
    <>
      <PlatformAiCard />
      <div className="tabs">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            className={`tab${section === tab.id ? " active" : ""}`}
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
