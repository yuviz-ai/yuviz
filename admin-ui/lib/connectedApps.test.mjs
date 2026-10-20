import assert from "node:assert/strict";
import test from "node:test";
import { filterApps, workAppCards } from "./connectedApps.ts";

const providers = [{ key: "google", label: "Google", auth_kind: "oauth2" }, { key: "zoho", label: "Zoho", auth_kind: "oauth2" }];
const presets = [
  { key: "calendar_booking", title: "Calendar booking", provider: "google" },
  { key: "sheets_lead_capture", title: "Sheets lead capture", provider: "google" },
  { key: "whatsapp_confirmation", title: "WhatsApp confirmation", provider: null },
];

test("catalog keeps deployment providers, nests calendar under Google, and marks unavailable apps coming soon", () => {
  const cards = workAppCards(providers, [], presets, []);
  assert.equal(cards.find((c) => c.id === "zoho").available, true);
  assert.equal(cards.find((c) => c.id === "calcom").available, false);
  assert.equal(cards.find((c) => c.id === "dynamics_crm").available, false);
  assert.equal(cards.some((c) => c.id === "calendar_booking"), false);
  assert.equal(cards.find((c) => c.id === "whatsapp_confirmation").available, true);
});

test("reconnect and disconnected grants never appear as connected", () => {
  for (const status of ["reconnect_needed", "disconnected"]) {
    const cards = workAppCards(providers, [{ provider: "google", status }], presets, []);
    assert.equal(cards.find((c) => c.id === "google").connected, false);
    assert.equal(cards.find((c) => c.id === "google").reconnect, status === "reconnect_needed");
    assert.equal(filterApps(cards, "", "Connected", "All").length, 0);
  }
});

test("saved tools stay manageable when their grant needs reconnecting", () => {
  const cards = workAppCards([], [], presets, [{ preset_key: "sheets_lead_capture" }, { preset_key: "whatsapp_confirmation" }]);
  const sheets = cards.find((c) => c.id === "sheets_lead_capture");
  assert.equal(sheets.connected, true);
  assert.equal(sheets.available, false);
  assert.equal(sheets.reconnect, true);
  assert.equal(cards.find((c) => c.id === "whatsapp_confirmation").reconnect, false);
});

test("search, category and connection filters intersect without changing the catalog", () => {
  const cards = workAppCards(providers, [{ provider: "google", status: "connected" }], presets, [{ preset_key: "whatsapp_confirmation" }]);
  assert.deepEqual(filterApps(cards, " GOOGLE ", "Connected", "Work").map((c) => c.id), ["google"]);
  assert.deepEqual(filterApps(cards, "confirmation", "Connected", "Messaging").map((c) => c.id), ["whatsapp_confirmation"]);
  assert.equal(filterApps(cards, "google", "Not connected", "Work").some((c) => c.id === "google"), false);
  assert.equal(filterApps(cards, "no such app", "All", "All").length, 0);
  assert.equal(cards.length, workAppCards(providers, [], presets, []).length);
});
