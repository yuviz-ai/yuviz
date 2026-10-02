// BFS layering from the start node: each node sits one row below the first stage that reaches it.
// Shortest-path depth, not deepest-path, because workflows may contain cycles.

import type { Node } from "@xyflow/react";

const COL_W = 360;
const ROW_H = 230;

export function autoLayout<T extends Node>(nodes: T[], edges: { source: string; target: string }[]): T[] {
  const start = nodes.find((n) => n.type === "start");
  if (!start) return nodes;

  const outgoing = new Map<string, string[]>();
  for (const e of edges) {
    outgoing.set(e.source, [...(outgoing.get(e.source) || []), e.target]);
  }

  const depth = new Map<string, number>([[start.id, 0]]);
  let frontier = [start.id];
  while (frontier.length) {
    const next: string[] = [];
    for (const id of frontier) {
      for (const target of outgoing.get(id) || []) {
        if (depth.has(target)) continue;   // already placed — and back-edges end here
        depth.set(target, (depth.get(id) ?? 0) + 1);
        next.push(target);
      }
    }
    frontier = next;
  }

  // Unreachable nodes get their own row below everything else.
  const maxDepth = Math.max(0, ...depth.values());
  for (const n of nodes) if (!depth.has(n.id)) depth.set(n.id, maxDepth + 1);

  const rows = new Map<number, string[]>();
  for (const n of nodes) {
    const d = depth.get(n.id)!;
    rows.set(d, [...(rows.get(d) || []), n.id]);
  }

  return nodes.map((n) => {
    const d = depth.get(n.id)!;
    const row = rows.get(d)!;
    const i = row.indexOf(n.id);
    return {
      ...n,
      position: { x: (i - (row.length - 1) / 2) * COL_W, y: d * ROW_H },
    };
  });
}
