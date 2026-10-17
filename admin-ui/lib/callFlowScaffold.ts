// Turns the builder's step picks into a linear starting graph; steps after a menu
// become its keypress branches instead.

import { CallFlowGraph, CallFlowNodeType } from "./callFlowApi";

export type FlowDirection = "inbound" | "outbound" | "both";

export interface BlockChoice {
  type: CallFlowNodeType;
  label: string;
  /** Node name on the canvas. */
  name: string;
  blurb: string;
  /** Directions this block makes sense for; a flow shows only its own. */
  directions: FlowDirection[];
}

export const BLOCKS: BlockChoice[] = [
  {
    type: "play",
    label: "Play a message",
    name: "Play a message",
    blurb: "Greeting, notice or disclaimer.",
    directions: ["inbound", "outbound", "both"],
  },
  {
    type: "menu",
    label: "Keypad menu (press 1, press 2)",
    name: "Keypad menu",
    blurb: "Callers choose with a key.",
    directions: ["inbound", "both"],
  },
  {
    type: "collect",
    label: "Ask for a number",
    name: "Ask for a number",
    blurb: "Account, PIN or date of birth.",
    directions: ["inbound", "outbound", "both"],
  },
  {
    type: "agent",
    label: "Hand to an AI agent",
    name: "Hand to an AI agent",
    blurb: "Your agent takes over.",
    directions: ["inbound", "outbound", "both"],
  },
  {
    type: "dial",
    label: "Transfer to a person",
    name: "Transfer to a person",
    blurb: "A phone number or SIP address.",
    directions: ["inbound", "outbound", "both"],
  },
  {
    type: "hangup",
    label: "Hang up",
    name: "Hang up",
    blurb: "Ends the call.",
    directions: ["inbound", "outbound", "both"],
  },
];

export interface FlowStarter {
  key: string;
  label: string;
  blurb: string;
  picks: CallFlowNodeType[];
}

export const STARTERS: FlowStarter[] = [
  { key: "press", label: "Press 1, press 2", blurb: "1 for your AI agent, 2 for a person.", picks: ["play", "menu", "agent", "dial"] },
  { key: "agent", label: "Straight to an AI agent", blurb: "A welcome, then your agent.", picks: ["play", "agent"] },
  { key: "person", label: "Straight to a person", blurb: "A welcome, then your team.", picks: ["play", "dial"] },
  { key: "number", label: "Ask for a number first", blurb: "Account number, then agent.", picks: ["collect", "agent"] },
  { key: "message", label: "Play a message", blurb: "Says it, then hangs up.", picks: ["play", "hangup"] },
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

  // Ending on a terminal step also guarantees a menu has at least one branch.
  const chain = [...picks];
  if (chain.length === 0) chain.push("hangup");
  if (!TERMINAL.includes(chain[chain.length - 1])) chain.push("hangup");

  const menuAt = chain.findIndex((t) => t === "menu");
  // Without a menu there is only one path. Stop at the first step that leaves
  // the flow, even if the builder has more terminal steps ticked.
  const firstTerminal = chain.findIndex((type) => TERMINAL.includes(type));
  const linear = menuAt === -1 ? chain.slice(0, firstTerminal + 1) : chain.slice(0, menuAt + 1);
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
        name: BLOCKS.find((b) => b.type === type)?.name ?? type,
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
          name: BLOCKS.find((b) => b.type === type)?.name ?? type,
          prompt: DEFAULT_PROMPT[type] ?? "",
          ...(type === "collect" ? { variable: "account_number", min_digits: 1, max_digits: 10 } : {}),
        },
        position: { x: x + 280, y: i * 150 },
      });
      edges.push({
        id: `e-${menuId}-${id}`,
        source: menuId,
        target: id,
        data: { key: String(i + 1) },
      });
      if (!TERMINAL.includes(type)) {
        const endId = `hangup-b${i}`;
        nodes.push({ id: endId, type: "hangup", data: { name: "end call" }, position: { x: x + 560, y: i * 150 } });
        edges.push({ id: `e-${id}-${endId}`, source: id, target: endId });
      }
    });
    // Without a timeout branch a menu just repeats and gives up.
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
