import assert from "node:assert/strict";
import test from "node:test";
import { draftSetup, makePresetDraft, presetDraftKey, readPresetDraft } from "./connectorDraft.ts";
import { carrierAccountLabel } from "./callingApps.ts";

test("blank carrier labels are omitted on create and explicitly cleared on edit", () => {
  assert.equal(JSON.stringify({ carrier_account_ref: carrierAccountLabel("  ", false) }), "{}");
  assert.equal(JSON.stringify({ carrier_account_ref: carrierAccountLabel("  ", true) }), '{"carrier_account_ref":null}');
  assert.equal(carrierAccountLabel(" Main account ", true), "Main account");
});

test("OAuth drafts preserve calendar settings while excluding unrelated fields and secrets", () => {
  const values = { calendar_id: "support@example.com", timezone: "Asia/Kolkata", day_start: "10:30", day_end: "17:30", slot_minutes: "45", api_key: "private" };
  const draft = makePresetDraft("calendar_booking", values);
  const data = new Map([[presetDraftKey("tenant-a"), JSON.stringify(draft)]]);
  const storage = { getItem: (key) => data.get(key) ?? null, removeItem: (key) => data.delete(key) };
  assert.equal(readPresetDraft(storage, "tenant-b"), null);
  const restored = readPresetDraft(storage, "tenant-a");
  assert.deepEqual(draftSetup(restored), { preset_key: "calendar_booking", calendar_id: values.calendar_id, timezone: values.timezone, day_start: values.day_start, day_end: values.day_end, slot_minutes: 45 });
  assert.equal(restored.values.api_key, undefined);
  assert.equal(makePresetDraft("whatsapp_confirmation", values), null);
  assert.deepEqual(draftSetup(makePresetDraft("sheets_lead_capture", { title: "My leads" })), { preset_key: "sheets_lead_capture", title: "My leads" });
});

test("expired and malformed OAuth drafts are discarded", () => {
  for (const raw of ["{", JSON.stringify({ ...makePresetDraft("sheets_lead_capture", { title: "Leads" }), createdAt: Date.now() - 31 * 60_000 }), JSON.stringify({ presetKey: "calendar_booking", values: {}, createdAt: Date.now() })]) {
    const data = new Map([[presetDraftKey("tenant-a"), raw]]);
    const storage = { getItem: (key) => data.get(key) ?? null, removeItem: (key) => data.delete(key) };
    assert.equal(readPresetDraft(storage, "tenant-a"), null);
    assert.equal(data.size, 0);
  }
});
