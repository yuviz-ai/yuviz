// Turns the builder's step picks into a starting graph.
//
// The picker is a list of building blocks, not a drawing surface: it chains
// whatever was picked into one straight line, start -> ... -> end, and the
// canvas is where branches get drawn afterwards. A menu is the one exception
// — it can't be chained linearly, since its whole point is that the next step
// depends on the keypress — so it fans out to the steps that follow it.

import { CallFlowGraph, CallFlowNodeType } from "./callFlowApi";

export interface BlockChoice {
  type: CallFlowNodeType;
  label: string;
  blurb: string;
  /** Directions this block makes sense for; a flow shows only its own. */
  directions: ("inbound" | "outbound" | "both")[];
}

export const BLOCKS: BlockChoice[] = [
  {
    type: "play",
    label: "Play a message",
    blurb: "Speaks a line, then continues. Greetings, notices, disclaimers.",
    directions: ["inbound", "outbound", "both"],
  },
  {
    type: "menu",
    label: "Keypad menu",
    blurb: "Plays options and branches on which key the caller presses.",
    directions: ["inbound", "both"],
  },
  {
    type: "collect",
    label: "Collect digits",
    blurb: "Gathers a number — account, PIN, date of birth — into a value.",
    directions: ["inbound", "outbound", "both"],
  },
  {
    type: "agent",
    label: "Hand to an AI agent",
    blurb: "The conversational agent takes over from here.",
    directions: ["inbound", "outbound", "both"],
  },
  {
    type: "dial",
    label: "Transfer to a human",
    blurb: "Sends the call to a number or SIP address.",
    directions: ["inbound", "outbound", "both"],
  },
  {
    type: "hangup",
    label: "Hang up",
    blurb: "Ends the call.",
    directions: ["inbound", "outbound", "both"],
  },
];

const DEFAULT_PROMPT: Partial<Record<CallFlowNodeType, string>> = {
  play: "Thanks for calling.",
  menu: "Press 1 for sales, or 2 for support.",
  collect: "Please enter your account number, then press hash.",
};

const TERMINAL: CallFlowNodeType[] = ["dial", "agent", "hangup"];

export function scaffoldGraph(picks: CallFlowNodeType[], direction: string): CallFlowGraph {
  const nodes: CallFlowGraph["nodes"] = [
    {
      id: "start",
      type: "start",
      data: { name: direction === "outbound" ? "call answered" : "call starts" },
      position: { x: 0, y: 160 },
    },
  ];
  const edges: CallFlowGraph["edges"] = [];

  // Anything after a menu becomes one of its branches rather than a step in
  // the chain, and a menu needs at least one branch to be valid at all — so
  // if nothing follows it, give it a hangup branch to land on.
  const chain = [...picks];
  if (chain.length === 0) chain.push("hangup");
  if (!TERMINAL.includes(chain[chain.length - 1])) chain.push("hangup");

  const menuAt = chain.findIndex((t) => t === "menu");
  const linear = menuAt === -1 ? chain : chain.slice(0, menuAt + 1);
  const branches = menuAt === -1 ? [] : chain.slice(menuAt + 1);

  let prev = "start";
  let x = 0;
  linear.forEach((type, i) => {
    const id = `${type}-${i}`;
    x += 280;
    nodes.push({
      id,
      type,
      data: {
        name: BLOCKS.find((b) => b.type === type)?.label ?? type,
        prompt: DEFAULT_PROMPT[type] ?? "",
        ...(type === "collect" ? { variable: "account_number", min_digits: 1, max_digits: 10 } : {}),
      },
      position: { x, y: 160 },
    });
    edges.push({ id: `e-${prev}-${id}`, source: prev, target: id });
    prev = id;
  });

  if (branches.length > 0) {
    const menuId = prev;
    branches.forEach((type, i) => {
      const id = `${type}-b${i}`;
      nodes.push({
        id,
        type,
        data: {
          name: BLOCKS.find((b) => b.type === type)?.label ?? type,
          prompt: DEFAULT_PROMPT[type] ?? "",
        },
        position: { x: x + 280, y: i * 150 },
      });
      edges.push({
        id: `e-${menuId}-${id}`,
        source: menuId,
        target: id,
        data: { key: String(i + 1) },
      });
    });
    // A menu with no timeout branch just repeats and gives up, so the
    // scaffold always lands "no answer" somewhere real.
    const bye = "hangup-timeout";
    nodes.push({
      id: bye,
      type: "hangup",
      data: { name: "no answer" },
      position: { x: x + 280, y: branches.length * 150 },
    });
    edges.push({ id: `e-${menuId}-${bye}`, source: menuId, target: bye, data: { key: "timeout" } });
  }

  return { version: 1, nodes, edges };
}
