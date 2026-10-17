import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "callFlowScaffold.ts"), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const dir = mkdtempSync(join(here, ".call-flow-scaffold-"));
writeFileSync(join(dir, "callFlowScaffold.mjs"), outputText);
const { scaffoldGraph } = await import(pathToFileURL(join(dir, "callFlowScaffold.mjs")).href);
rmSync(dir, { recursive: true });

test("without a menu the first terminal ends the graph", () => {
  const graph = scaffoldGraph(["play", "agent", "dial"], "outbound");
  assert.deepEqual(graph.nodes.map((node) => node.type), ["start", "play", "agent"]);
  assert.equal(graph.edges.some((edge) => edge.source === "agent-1"), false);
});

test("a menu keeps distinct terminal branches", () => {
  const graph = scaffoldGraph(["play", "menu", "agent", "dial"], "inbound");
  const menuEdges = graph.edges.filter((edge) => edge.source === "menu-1");
  assert.deepEqual(menuEdges.map((edge) => edge.data?.key), ["1", "2", "timeout"]);
});

test("a collect menu branch ends instead of stalling", () => {
  const graph = scaffoldGraph(["menu", "collect"], "inbound");
  const collect = graph.nodes.find((node) => node.type === "collect");
  assert.equal(collect.data.variable, "account_number");
  assert.equal(graph.edges.some((edge) => edge.source === collect.id &&
    graph.nodes.find((node) => node.id === edge.target)?.type === "hangup"), true);
});
