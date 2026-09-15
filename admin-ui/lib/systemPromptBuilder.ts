// Deterministic system-prompt assembly — not an LLM call. The wizard asks
// for structured facts (identity, purpose, tone, transfer rule); this just
// arranges them into a strict template with hard-coded anti-hallucination
// and spoken-style guardrails baked in, so those rules can't be dropped or
// diluted by generation variance. Every generated prompt is still editable
// before the agent is created.

export interface SystemPromptInputs {
  name: string;
  purpose: string; // one line: what this agent is for
  persona: string; // free text identity/personality description
  tone: string; // e.g. "friendly and professional"
  language: string | null; // display label, e.g. "English (US)"
  hasKnowledgeBase: boolean;
  transferType: "warm" | "cold" | "none";
  transferCondition: string | null; // form.transfer_prompt
  complianceInstructions?: string;
  fallbackResponse?: string;
}

export function buildSystemPrompt(inputs: SystemPromptInputs): string {
  const lines: string[] = [];

  const identity = inputs.persona.trim() || `You are ${inputs.name.trim()}, a helpful voice assistant.`;
  lines.push(identity);

  if (inputs.purpose.trim()) {
    lines.push(`Your job on this call: ${inputs.purpose.trim()}`);
  }

  if (inputs.tone.trim()) {
    lines.push(`Tone: speak in a ${inputs.tone.trim()} manner at all times.`);
  }

  if (inputs.language) {
    lines.push(`Speak ${inputs.language} unless the caller switches language first.`);
  }

  lines.push(
    "Never invent facts, prices, policies, order details, or availability. If you do not have " +
      "verified information to answer something, say so plainly and offer to check or transfer " +
      "the caller — do not guess or make up an answer.",
  );

  if (inputs.hasKnowledgeBase) {
    lines.push(
      "Ground every factual claim in the knowledge base or tool results provided to you. If the " +
        "knowledge base does not cover what the caller is asking, say you don't have that " +
        "information rather than improvising.",
    );
  }

  if (inputs.fallbackResponse?.trim()) {
    lines.push(`When you genuinely don't know the answer, say: "${inputs.fallbackResponse.trim()}"`);
  }

  if (inputs.complianceInstructions?.trim()) {
    lines.push(`You must always follow these rules: ${inputs.complianceInstructions.trim()}`);
  }

  if (inputs.transferType !== "none") {
    const condition = inputs.transferCondition?.trim() || "the caller explicitly asks to speak to a human agent";
    lines.push(`If ${condition}, offer to transfer the call rather than continuing to guess.`);
  }

  lines.push(
    "Answer in at most 2-3 short spoken sentences. Plain conversational speech only — no " +
      "markdown, no lists, no headings.",
  );

  return lines.join(" ");
}
