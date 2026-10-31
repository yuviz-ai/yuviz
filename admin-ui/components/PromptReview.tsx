"use client";

// Review an AI rewrite of the agent's instructions before it replaces them: per section keep, edit or discard.

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { SectionChange, SectionChoice, composePrompt, diffLines, diffSections } from "@/lib/promptDiff";

const STATUS_LABEL = { added: "added", removed: "removed", edited: "edited", unchanged: "unchanged" } as const;

export function PromptReview({ before, after, title = "Review the rewrite", onAccept, onDiscard }: {
  before: string;
  after: string;
  title?: string;
  onAccept: (prompt: string) => void;
  onDiscard: () => void;
}) {
  const [changes] = useState(() => diffSections(before, after));
  const [choices, setChoices] = useState<Record<string, SectionChoice>>({});
  const changed = changes.filter((c) => c.status !== "unchanged");
  const [open, setOpen] = useState<string | null>(changed[0]?.key ?? null);
  // The whole prompt as accepting would leave it (or the original), for reading before deciding.
  const [fullView, setFullView] = useState<"result" | "original" | null>(null);

  const choose = (key: string, choice: SectionChoice) => setChoices((m) => ({ ...m, [key]: choice }));
  const choiceOf = (c: SectionChange): SectionChoice => choices[c.key] ?? { kind: "new" };

  if (changed.length === 0) {
    return (
      <div className="pr-review">
        <div className="pr-review-head"><b>{title}</b><span>The rewrite made no changes.</span></div>
        <div className="pr-review-foot"><button type="button" className="btn btn-ghost btn-sm" onClick={onDiscard}>Close</button></div>
      </div>
    );
  }

  return (
    <div className="pr-review" role="region" aria-label={title}>
      <div className="pr-review-head">
        <b>{title}</b>
        <span>Nothing changes until you accept. Keep, edit or discard each section.</span>
      </div>
      {changed.map((c) => {
        const choice = choiceOf(c);
        const isOpen = open === c.key;
        const reverted = choice.kind === "old";
        const state = choice.kind === "edit" ? "your edit" : reverted ? (c.status === "removed" ? "kept" : c.status === "added" ? "discarded" : "original kept") : STATUS_LABEL[c.status];
        return (
          <div key={c.key} className={`pr-sec ${c.status}${reverted ? " reverted" : ""}`}>
            <div className="pr-sec-head">
              <button type="button" className="pr-sec-toggle" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : c.key)}>
                {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                <span>{c.heading}</span>
                <span className="pr-sec-state">· {state}</span>
              </button>
              <div className="pr-sec-actions">
                {c.status === "removed" ? (
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => choose(c.key, reverted ? { kind: "new" } : { kind: "old" })}>
                    {reverted ? "Remove" : "Keep"}
                  </button>
                ) : (
                  <>
                    {choice.kind !== "edit" && !reverted && (
                      <button type="button" className="btn btn-ghost btn-sm" onClick={() => { choose(c.key, { kind: "edit", text: c.after ?? "" }); setOpen(c.key); }}>Edit</button>
                    )}
                    <button type="button" className="btn btn-ghost btn-sm"
                      onClick={() => choose(c.key, choice.kind === "new" ? { kind: "old" } : { kind: "new" })}>
                      {choice.kind === "new" ? "Discard" : "Use rewrite"}
                    </button>
                  </>
                )}
              </div>
            </div>
            {isOpen && (
              choice.kind === "edit" ? (
                <textarea
                  className="form-textarea pr-sec-edit"
                  value={choice.text}
                  aria-label={`Edit ${c.heading}`}
                  onChange={(e) => choose(c.key, { kind: "edit", text: e.target.value })}
                />
              ) : (
                <div className="pr-diff">
                  {diffLines(c.before ?? "", reverted ? c.before ?? "" : c.after ?? "").map((l, i) => (
                    <div key={i} className={`pr-line ${l.kind}`}>
                      <span aria-hidden="true">{l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}</span>{l.text || " "}
                    </div>
                  ))}
                </div>
              )
            )}
          </div>
        );
      })}
      {fullView && (
        <div className="pr-full">
          <div className="pr-full-tabs" role="tablist" aria-label="Full prompt">
            <button type="button" role="tab" aria-selected={fullView === "result"} className={fullView === "result" ? "on" : ""}
              onClick={() => setFullView("result")}>With your choices</button>
            <button type="button" role="tab" aria-selected={fullView === "original"} className={fullView === "original" ? "on" : ""}
              onClick={() => setFullView("original")}>Original</button>
          </div>
          <textarea className="form-textarea pr-full-text" readOnly aria-label="Full prompt"
            value={fullView === "result" ? composePrompt(changes, choices) : before} />
        </div>
      )}
      <div className="pr-review-foot">
        <button type="button" className="btn btn-ghost btn-sm pr-full-btn" aria-expanded={!!fullView}
          onClick={() => setFullView(fullView ? null : "result")}>
          {fullView ? "Hide full prompt" : "View full prompt"}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onDiscard}>Discard all</button>
        <button type="button" className="btn btn-primary btn-sm" onClick={() => onAccept(composePrompt(changes, choices))}>Accept changes</button>
      </div>
    </div>
  );
}
