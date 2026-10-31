// Section-level review of a prompt rewrite: what changed, and the prompt the reviewer's choices produce.

export interface PromptSection { heading: string; body: string }
export type SectionStatus = "added" | "removed" | "edited" | "unchanged";
export interface SectionChange { key: string; heading: string; status: SectionStatus; before: string | null; after: string | null }
export type DiffLine = { kind: "same" | "add" | "del"; text: string };
/** Per section: the proposed text ("new"), the original ("old"), or the reviewer's own edit. */
export type SectionChoice = { kind: "new" } | { kind: "old" } | { kind: "edit"; text: string };

const OPENING = "Opening";

// Headings are bare lines (no markdown): short, after a blank line, no sentence punctuation, not a list item.
function isHeading(lines: string[], i: number): boolean {
  const line = lines[i].trim();
  if (!line || line.length > 40 || /[.:;,!?]$/.test(line) || /^([-*•]|\d+[.)])/.test(line)) return false;
  const prevBlank = i === 0 || lines[i - 1].trim() === "";
  const nextHasText = i + 1 < lines.length && lines[i + 1].trim() !== "";
  return prevBlank && nextHasText;
}

export function splitSections(prompt: string): PromptSection[] {
  const lines = prompt.replace(/\r\n/g, "\n").split("\n");
  const sections: PromptSection[] = [];
  let heading = OPENING;
  let body: string[] = [];
  const flush = () => {
    const text = body.join("\n").trim();
    if (text || heading !== OPENING) sections.push({ heading, body: text });
  };
  lines.forEach((_, i) => {
    if (isHeading(lines, i)) {
      flush();
      heading = lines[i].trim();
      body = [];
    } else {
      body.push(lines[i]);
    }
  });
  flush();
  return sections;
}

const keyOf = (heading: string) => heading.trim().toLowerCase();

export function diffSections(before: string, after: string): SectionChange[] {
  const old = splitSections(before);
  const next = splitSections(after);
  const oldByKey = new Map(old.map((s) => [keyOf(s.heading), s]));
  const nextKeys = new Set(next.map((s) => keyOf(s.heading)));
  const changes: SectionChange[] = next.map((s) => {
    const prev = oldByKey.get(keyOf(s.heading));
    const status: SectionStatus = !prev ? "added" : prev.body === s.body ? "unchanged" : "edited";
    return { key: keyOf(s.heading), heading: s.heading, status, before: prev?.body ?? null, after: s.body };
  });
  // A removed section goes back after the section that preceded it in the original, so a kept one stays in place.
  old.forEach((s, i) => {
    if (nextKeys.has(keyOf(s.heading))) return;
    const removed: SectionChange = { key: keyOf(s.heading), heading: s.heading, status: "removed", before: s.body, after: null };
    const prevKey = i > 0 ? keyOf(old[i - 1].heading) : null;
    const at = prevKey ? changes.findIndex((c) => c.key === prevKey) : -1;
    changes.splice(at + 1, 0, removed);
  });
  return changes;
}

export function composePrompt(changes: SectionChange[], choices: Record<string, SectionChoice>): string {
  const parts: string[] = [];
  for (const c of changes) {
    const choice = choices[c.key] ?? { kind: "new" };
    const body = choice.kind === "edit" ? choice.text : choice.kind === "old" ? c.before : c.after;
    if (body === null) continue;
    parts.push(c.heading === OPENING ? body.trim() : `${c.heading}\n${body.trim()}`);
  }
  return parts.filter(Boolean).join("\n\n");
}

/** Line diff (longest common subsequence) for showing one section's change. */
export function diffLines(before: string, after: string): DiffLine[] {
  const a = before ? before.split("\n") : [];
  const b = after ? after.split("\n") : [];
  const n = a.length, m = b.length;
  const lcs = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ kind: "same", text: a[i] }); i++; j++; }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) out.push({ kind: "del", text: a[i++] });
    else out.push({ kind: "add", text: b[j++] });
  }
  while (i < n) out.push({ kind: "del", text: a[i++] });
  while (j < m) out.push({ kind: "add", text: b[j++] });
  return out;
}
