import assert from "node:assert/strict";
import test from "node:test";
import { availabilityLabel, filterApps, oauthAuthorizationUrl, workAppCards } from "./connectedApps.ts";

const providers = [{ key: "google", label: "Google", auth_kind: "oauth2" }, { key: "zoho", label: "Zoho", auth_kind: "oauth2" }];
const presets = [
  { key: "calendar_booking", title: "Calendar booking", provider: "google" },
  { key: "sheets_lead_capture", title: "Sheets lead capture", provider: "google" },
  { key: "whatsapp_confirmation", title: "WhatsApp confirmation", provider: null },
];

test("catalog keeps deployment providers, nests calendar under Google, and distinguishes setup from upcoming apps", () => {
  const cards = workAppCards(providers, [], presets, []);
  assert.equal(cards.find((c) => c.id === "zoho").available, true);
  assert.equal(cards.find((c) => c.id === "calcom").available, false);
  assert.equal(availabilityLabel(cards.find((c) => c.id === "calcom")), "Needs setup");
  assert.equal(availabilityLabel(cards.find((c) => c.id === "salesforce")), "Needs setup");
  assert.equal(availabilityLabel(cards.find((c) => c.id === "dynamics_crm")), "Coming soon");
  assert.equal(cards.find((c) => c.id === "hubspot").available, false);
  assert.equal(cards.find((c) => c.id === "dynamics_crm").available, false);
  assert.equal(cards.find((c) => c.id === "dynamics_crm").icon, "dynamics365");
  assert.equal(cards.find((c) => c.id === "microsoft").icon, "microsoft");
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


test("connected account details are searchable without exposing disconnected accounts", () => {
  const connection = { id: "g1", provider: "google", status: "connected", account_label: "admin@example.com" };
  const cards = workAppCards(providers, [connection, { ...connection, id: "old", status: "disconnected", account_label: "old@example.com" }], presets, []);
  assert.deepEqual(cards.find((c) => c.id === "google").accounts, [connection]);
  assert.deepEqual(filterApps(cards, "admin@example.com", "Connected", "Work").map((c) => c.id), ["google"]);
  assert.equal(filterApps(cards, "old@example.com", "All", "All").length, 0);
});

test("CRM tools stay inside their account's Manage dialog", () => {
  const cards = workAppCards(providers, [], [...presets, { key: "salesforce_crm", title: "Contact lookup", provider: "salesforce" }], []);
  assert.equal(cards.filter((c) => c.id === "salesforce").length, 1);
  assert.equal(cards.some((c) => c.id === "salesforce_crm"), false);
});

test("Google shows calendar tool state on its account card", () => {
  const card = workAppCards(providers, [], presets, [{ preset_key: "calendar_booking" }]).find((app) => app.id === "google");
  assert.equal(card.tools.find((tool) => tool.key === "calendar_booking").configured, true);
  assert.equal(card.tools.find((tool) => tool.key === "sheets_lead_capture").configured, false);
});

test("changing Google accounts requests its chooser while retaining the server's OAuth parameters", () => {
  const original = "https://accounts.google.com/o/oauth2/v2/auth?state=single-use&code_challenge=pkce&redirect_uri=https%3A%2F%2Fconsole.test%2Fintegrations%2Fcallback&prompt=consent";
  const result = new URL(oauthAuthorizationUrl(original, "google", true));
  assert.equal(result.searchParams.get("prompt"), "consent select_account");
  assert.equal(result.searchParams.get("state"), "single-use");
  assert.equal(result.searchParams.get("code_challenge"), "pkce");
  assert.equal(result.searchParams.get("redirect_uri"), "https://console.test/integrations/callback");
  assert.equal(oauthAuthorizationUrl(original, "google", false), original);
  assert.equal(oauthAuthorizationUrl(original, "salesforce", true), original);
});

test("the account chooser is added to whatever prompt the server already asked for", () => {
  const original = "https://accounts.google.com/o/oauth2/v2/auth?state=s&prompt=login";
  assert.equal(new URL(oauthAuthorizationUrl(original, "google", true)).searchParams.get("prompt"), "login consent select_account");
});
