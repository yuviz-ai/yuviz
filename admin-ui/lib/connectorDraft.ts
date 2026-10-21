import type { PresetSetup } from "./toolexecApi";

const DRAFT_FIELDS: Record<string, string[]> = {
  calendar_booking: ["calendar_id", "timezone", "day_start", "day_end", "slot_minutes"],
  sheets_lead_capture: ["title"],
};
export const presetDraftKey = (tenantId: string) => `yuviz_oauth_preset:${tenantId}`;
export type PresetDraft = { presetKey: string; values: Record<string, string>; createdAt: number };

export function makePresetDraft(presetKey: string, values: Record<string, string>): PresetDraft | null {
  const fields = DRAFT_FIELDS[presetKey];
  if (!Array.isArray(fields)) return null;
  return { presetKey, createdAt: Date.now(), values: Object.fromEntries(fields.map((key) => [key, values[key] ?? ""])) };
}

export function readPresetDraft(storage: Pick<Storage, "getItem" | "removeItem">, tenantId: string): PresetDraft | null {
  const key = presetDraftKey(tenantId);
  try {
    const raw = storage.getItem(key);
    if (!raw) return null;
    const draft = JSON.parse(raw);
    if (typeof draft.createdAt !== "number" || draft.createdAt > Date.now() || Date.now() - draft.createdAt > 30 * 60_000
      || !Array.isArray(DRAFT_FIELDS[draft.presetKey]) || !draft.values || typeof draft.values !== "object"
      || !DRAFT_FIELDS[draft.presetKey].every((field) => typeof draft.values[field] === "string")) throw new Error("invalid draft");
    return { ...makePresetDraft(draft.presetKey, draft.values)!, createdAt: draft.createdAt };
  } catch {
    storage.removeItem(key);
    return null;
  }
}

export function draftSetup(draft: PresetDraft): PresetSetup {
  const setup: PresetSetup = { preset_key: draft.presetKey };
  for (const field of DRAFT_FIELDS[draft.presetKey] ?? []) {
    const value = draft.values[field]?.trim();
    if (value) setup[field] = field === "slot_minutes" ? Number(value) : value;
  }
  return setup;
}
