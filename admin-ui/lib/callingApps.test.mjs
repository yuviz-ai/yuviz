import assert from "node:assert/strict";
import test from "node:test";
import { CALLING_APPS, callingAppLabel, cloudonixCredentials } from "./callingApps.ts";

test("Cloudonix edits preserve every existing key without echoing credentials", () => {
  const existing = ["[stored]", "env:ROTATING_KEY", "enc:private"];
  assert.deepEqual(cloudonixCredentials(" example.cloudonix.io ", "", existing), {
    domain: "example.cloudonix.io", api_keys: ["[stored]", "[stored]", "[stored]"],
  });
  assert.deepEqual(existing, ["[stored]", "env:ROTATING_KEY", "enc:private"]);
});

test("Cloudonix key replacement retains the remaining keys during rotation", () => {
  assert.deepEqual(cloudonixCredentials("example.cloudonix.io", " new-key ", ["[stored]", "[stored]"]).api_keys, ["new-key", "[stored]"]);
  assert.deepEqual(cloudonixCredentials("example.cloudonix.io", "first-key").api_keys, ["first-key"]);
});

test("calling app names fall back safely and their icons come from the catalog", () => {
  assert.equal(callingAppLabel("native"), "Local phone");
  assert.equal(callingAppLabel("future-provider"), "future-provider");
  assert.equal(CALLING_APPS.find((app) => app.key === "native").icon, "local-phone");
  assert.ok(CALLING_APPS.every((app) => !!app.icon));
});
