import assert from "node:assert/strict";
import test from "node:test";
import { composePrompt, diffLines, diffSections, splitSections } from "../../lib/promptDiff.ts";

const BEFORE = `Role
You are Sarah, the receptionist.

Steps
1. Greet the caller.
2. Ask what they need.

When things go wrong
If unsure, offer a callback.`;

const AFTER = `Role
You are Sarah, the friendly receptionist.

When things go wrong
If unsure, offer a callback.

Things to cover
- Their name.
- What they need.`;

test("sections split on bare heading lines, not list items or sentences", () => {
  assert.deepEqual(splitSections(BEFORE).map((s) => s.heading), ["Role", "Steps", "When things go wrong"]);
  assert.equal(splitSections("Just one paragraph of instructions.")[0].heading, "Opening");
});

test("each section is marked added, removed, edited or unchanged, removed ones in their old place", () => {
  const changes = diffSections(BEFORE, AFTER);
  assert.deepEqual(changes.map((c) => [c.heading, c.status]), [
    ["Role", "edited"], ["Steps", "removed"], ["When things go wrong", "unchanged"], ["Things to cover", "added"],
  ]);
});

test("accepting everything gives the rewrite; discarding everything gives the original back", () => {
  const changes = diffSections(BEFORE, AFTER);
  assert.equal(composePrompt(changes, {}), AFTER);
  const revert = Object.fromEntries(changes.map((c) => [c.key, { kind: "old" }]));
  assert.equal(composePrompt(changes, revert), BEFORE);
});

test("a mix of choices: keep a removed section, edit another, discard an added one", () => {
  const changes = diffSections(BEFORE, AFTER);
  const prompt = composePrompt(changes, {
    steps: { kind: "old" }, role: { kind: "edit", text: "You are Sarah." }, "things to cover": { kind: "old" },
  });
  assert.equal(prompt, "Role\nYou are Sarah.\n\nSteps\n1. Greet the caller.\n2. Ask what they need.\n\nWhen things go wrong\nIf unsure, offer a callback.");
});

test("line diff marks removed and added lines around the common ones", () => {
  assert.deepEqual(diffLines("a\nb\nc", "a\nx\nc"), [
    { kind: "same", text: "a" }, { kind: "del", text: "b" }, { kind: "add", text: "x" }, { kind: "same", text: "c" },
  ]);
});
