import assert from "node:assert/strict";
import test from "node:test";

import { pauseOutcomes, resumeMissing } from "../../lib/agentStatusImpact.ts";

const agent = { id: "a", slug: "support", name: "Support", status: "active" };
const defaultAgent = { id: "d", slug: "default", name: "Default agent", status: "active" };
const number = { did: "+14155550100", agent_id: "a", status: "active" };

test("pausing names each active number and where its calls go next", () => {
  assert.deepEqual(pauseOutcomes(agent, [agent, defaultAgent], [number]),
    ["+14155550100: calls go to Default agent."]);
  assert.deepEqual(pauseOutcomes(agent, [agent], [number]),
    ["+14155550100: calls won't reach an agent."]);
  assert.deepEqual(pauseOutcomes(agent, [agent, defaultAgent], [{ ...number, status: "inactive" }]), []);
  assert.deepEqual(pauseOutcomes(agent, [agent, defaultAgent], [{ ...number, agent_id: "other" }]), []);
});

test("pausing the default agent includes numbers that reach it implicitly", () => {
  const implicit = { ...number, agent_id: null };
  const inactivePrimary = { ...number, agent_id: "a" };
  assert.deepEqual(pauseOutcomes(defaultAgent, [agent, defaultAgent], [implicit]),
    ["+14155550100: calls won't reach an agent."]);
  assert.deepEqual(pauseOutcomes(defaultAgent, [{ ...agent, status: "inactive" }, defaultAgent], [inactivePrimary]),
    ["+14155550100: calls won't reach an agent."]);
  assert.deepEqual(pauseOutcomes(defaultAgent, [agent, defaultAgent], [inactivePrimary]), []);
});

test("resuming checks inbound and outbound setup by direction", () => {
  const campaign = { agent_id: "a", caller_id: "+1 (415) 555-0100", status: "running" };
  const owned = { ...number, did: "14155550100" };
  assert.equal(resumeMissing(agent, "inbound", [agent], [], []).length, 1);
  assert.deepEqual(resumeMissing(agent, "outbound", [agent], [owned], [campaign]), []);
  assert.equal(resumeMissing(agent, "both", [agent], [], []).length, 2);
  const unwired = { ...number, provider_sync: { ok: false } };
  assert.equal(resumeMissing(agent, "inbound", [agent], [unwired], []).length, 1);
  assert.equal(resumeMissing(agent, "outbound", [agent], [unwired], [campaign]).length, 1);
  assert.deepEqual(resumeMissing(agent, "both", [agent], [{ ...owned, provider_sync: { ok: true } }], [campaign]), []);
  assert.equal(resumeMissing(agent, "outbound", [agent], [owned], [{ ...campaign, status: "completed" }]).length, 1);
  assert.equal(resumeMissing(agent, "outbound", [agent], [owned], [{ ...campaign, caller_id: "+14155550101" }]).length, 1);
});

test("resuming recognizes numbers that reach it through the default route", () => {
  const paused = { ...agent, status: "inactive", slug: "default" };
  assert.deepEqual(resumeMissing(paused, "inbound", [paused], [{ ...number, agent_id: null }], []), []);
});

test("chat agents skip phone readiness and inbound checks survive a campaign outage", () => {
  const chat = { ...agent, template_id: "faq-support" };
  assert.deepEqual(resumeMissing(chat, "inbound", [chat], [], null, true), []);
  assert.deepEqual(resumeMissing(agent, "inbound", [agent], [], null),
    ["No active phone number is assigned to this agent."]);
  assert.deepEqual(resumeMissing(agent, "outbound", [agent], [], null),
    ["Could not check outbound campaign setup."]);
});
