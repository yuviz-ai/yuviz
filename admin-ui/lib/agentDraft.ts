// Browser-only autosave for the /agents/new wizard, so leaving the page never loses typed work.

import { tokenUserId } from "@/lib/auth";

const DRAFT_PREFIX = "yuviz:new-agent-draft";

// Keyed per user so a draft never surfaces for another account on the same browser.
function draftKey(): string | null {
  const userId = tokenUserId();
  return userId ? `${DRAFT_PREFIX}:${userId}` : null;
}

export interface AgentDraft {
  savedAt: number;
  step: string;
  name: string;
  tenantSlug: string;
  purpose: string;
  persona: string;
  tone: string;
  languageChoice: string;
  customLanguage: string;
  // Optional: drafts saved before multilingual agents lack them.
  supportedLanguages?: string[];
  ttsByLanguage?: Record<string, string>;
  greetingByLanguage?: Record<string, string>;
  sttId: string | null;
  llmId: string | null;
  ttsId: string | null;
  maxCallDuration: number | "";
  goodbyeGraceMs: number | "";
  escalationThreshold: number | "";
  transferType: "none" | "cold" | "warm" | undefined;
  transferDestination: string;
  transferCondition: string;
  transferAnnouncement: string;
  complianceInstructions: string;
  fallbackResponse: string;
  selectedKbIds: string[];
  selectedApiIds: string[];
  greeting: string;
  systemPrompt: string;
  promptEdited: boolean;
}

export function loadAgentDraft(): AgentDraft | null {
  const key = draftKey();
  if (!key) return null;
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as AgentDraft) : null;
  } catch {
    return null;
  }
}

export function saveAgentDraft(draft: AgentDraft): void {
  const key = draftKey();
  if (!key) return;
  try {
    window.localStorage.setItem(key, JSON.stringify(draft));
  } catch {
    // Blocked/full storage: autosave silently degrades to none.
  }
}

export function clearAgentDraft(): void {
  const key = draftKey();
  if (!key) return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Same non-fatal fallback as saveAgentDraft.
  }
}

// Explicit sign-out only; a 401 expiry keeps the draft for the same user's return.
export function clearAllAgentDrafts(): void {
  try {
    Object.keys(window.localStorage)
      .filter((k) => k.startsWith(DRAFT_PREFIX))
      .forEach((k) => window.localStorage.removeItem(k));
  } catch {
    // Same non-fatal fallback as saveAgentDraft.
  }
}

export function draftSavedLabel(savedAt: number): string {
  const mins = Math.floor((Date.now() - savedAt) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return new Date(savedAt).toLocaleDateString();
}
