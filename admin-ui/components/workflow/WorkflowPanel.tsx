"use client";

// Workflow editor: canvas, inspector, autosave draft, publish. No external
// store — React Flow state + local useState (docs/workflow.md Part 6).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  addEdge,
  Background,
  BackgroundVariant,
  ControlButton,
  Controls,
  Panel as FlowPanel,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { TestAgentPanel } from "@/components/TestAgentPanel";
import { ApiError, listAgentToolPolicies } from "@/lib/api";
import { listAgentKnowledgeBases } from "@/lib/knowledgeApi";
import {
  getWorkflow,
  publishErrors,
  publishWorkflow,
  saveWorkflowDraft,
  starterGraph,
  validateWorkflow,
  type WorkflowEdgeData,
  type WorkflowError,
  type WorkflowGraph,
  type WorkflowNodeData,
  type WorkflowNodeType,
} from "@/lib/workflowApi";
import { autoLayout } from "./autoLayout";
import { EditorContext } from "./editorContext";
import { edgeTypes } from "./edges";
import { Inspector, type Selection } from "./Inspector";
import { nodeTypes } from "./nodes";
import { VersionPanel } from "./VersionPanel";

const AUTOSAVE_MS = 1200;
const VALIDATE_MS = 500;
const UNDO_COALESCE_MS = 600;
const UNDO_LIMIT = 60;

const CALL_CONTEXT_VARIABLES = [
  "caller_number", "called_number", "agent_name", "business_name",
  "current_date", "current_time", "direction",
];

const NEW_NODE_DEFAULTS: Record<Exclude<WorkflowNodeType, "start">, WorkflowNodeData> = {
  agent: { name: "new stage", prompt: "", tools: [], knowledge_base_ids: [] },
  transfer: { name: "to a human", prompt: "Tell the caller you're connecting them now.", transfer_destination: null },
  end: { name: "ended", prompt: "Close the call warmly.", disposition: "completed" },
  global: { name: "always applies", prompt: "" },
};

/** Key-order-independent serialization for JSONB round-trip compares. */
function canonicalize(value: unknown): string {
  return JSON.stringify(value, (_key, v) =>
    v && typeof v === "object" && !Array.isArray(v)
      ? Object.fromEntries(Object.entries(v as object).sort(([a], [b]) => a.localeCompare(b)))
      : v,
  );
}

// __invalid is a canvas-only marker stripped before persisting.
type RFNode = Node<WorkflowNodeData & { __invalid?: boolean }>;
type RFEdge = Edge<WorkflowEdgeData>;

function toReactFlow(graph: WorkflowGraph): { nodes: RFNode[]; edges: RFEdge[] } {
  return {
    nodes: graph.nodes.map((n) => ({
      id: n.id, type: n.type, position: n.position, data: { ...n.data },
      deletable: n.type !== "start",
    })) as RFNode[],
    edges: graph.edges.map((e) => ({
      id: e.id, source: e.source, target: e.target,
      label: e.data.label, data: { ...e.data },
    })) as RFEdge[],
  };
}

function toGraph(nodes: RFNode[], edges: RFEdge[]): WorkflowGraph {
  return {
    version: 1,
    nodes: nodes.map((n) => {
      const { __invalid, ...data } = n.data;
      void __invalid;
      return {
        id: n.id, type: (n.type || "agent") as WorkflowNodeType,
        position: n.position, data: data as WorkflowNodeData,
      };
    }),
    edges: edges.map((e) => {
      // Strip canvas-only markers so they never land in draft/versions.
      const { __invalid, ...data } = (e.data || { label: "", condition: "" }) as WorkflowEdgeData;
      void __invalid;
      return {
        id: e.id, source: e.source, target: e.target,
        data: data as WorkflowEdgeData,
      };
    }),
  };
}

/** Full-page editor chrome (back / title / settings) in the toolbar. */
export type WorkflowHeader = { title: string; backHref: string; settingsHref: string };

type PanelProps = {
  tenantSlug: string;
  agentId: string;
  agentSlug: string;
  greeting?: string;
  systemPrompt?: string;
  header?: WorkflowHeader;
};

function Panel({
  tenantSlug, agentId, agentSlug, greeting = "", systemPrompt = "", header,
}: PanelProps) {
  const [nodes, setNodes, onNodesChange] = useNodesState<RFNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<RFEdge>([]);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errors, setErrors] = useState<WorkflowError[]>([]);
  const [warnings, setWarnings] = useState<WorkflowError[]>([]);
  const [publishing, setPublishing] = useState(false);
  const [published, setPublished] = useState<string | null>(null);   // canonical live graph
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved">("idle");
  const [justPublished, setJustPublished] = useState(false);
  const [versionKey, setVersionKey] = useState(0);
  const [showHistory, setShowHistory] = useState(false);
  const [agentTools, setAgentTools] = useState<string[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<{ id: string; name: string }[]>([]);
  const [addOpen, setAddOpen] = useState(false);
  // Server rejects a second global; UI mirrors that.
  const hasGlobal = nodes.some((n) => n.type === "global");
  const [moreOpen, setMoreOpen] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  // Browser voice test hits the published agent (draft live-highlight later).
  const [testing, setTesting] = useState(false);
  const loaded = useRef(false);
  // Client-only seed: skip autosave until the canvas diverges from this snapshot.
  const seedPristine = useRef<string | null>(null);
  const saveGen = useRef(0);
  const saveAbort = useRef<AbortController | null>(null);
  const configVersion = useRef<number>(0);
  const reactFlow = useReactFlow();

  const applyWorkflowState = useCallback((state: Awaited<ReturnType<typeof getWorkflow>>) => {
    const fromServer = state.workflow_draft ?? state.workflow;
    const graph = fromServer ?? starterGraph(greeting, systemPrompt);
    const rf = toReactFlow(graph);
    seedPristine.current = fromServer ? null : JSON.stringify(toGraph(rf.nodes, rf.edges));
    configVersion.current = state.config_version;
    setNodes(rf.nodes);
    setEdges(rf.edges);
    setPublished(state.workflow ? canonicalize(state.workflow) : null);
  }, [greeting, systemPrompt, setNodes, setEdges]);

  useEffect(() => {
    loaded.current = false;
    seedPristine.current = null;
    saveAbort.current?.abort();
    saveGen.current += 1;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    Promise.all([
      getWorkflow(tenantSlug, agentId),
      listAgentToolPolicies(agentId).catch(() => []),
      listAgentKnowledgeBases(agentId).catch(() => []),
    ])
      .then(([state, policies, kbs]) => {
        applyWorkflowState(state);
        setAgentTools(policies.filter((p) => p.enabled).map((p) => p.tool_name));
        setKnowledgeBases(kbs.map((kb) => ({ id: kb.kb_id, name: kb.kb_name })));
        setShowHelp(!state.workflow && !state.workflow_draft);
        loaded.current = true;
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantSlug, agentId]);

  const availableVariables = useMemo(() => {
    const declared = nodes.flatMap((n) =>
      n.data.extraction?.enabled ? n.data.extraction.variables.map((v) => v.name) : [],
    );
    return [...new Set([...CALL_CONTEXT_VARIABLES, ...declared.filter(Boolean)])];
  }, [nodes]);

  const graph = useMemo(() => toGraph(nodes, edges), [nodes, edges]);
  const serialized = JSON.stringify(graph);
  // Compared against the live graph with keys sorted, because that copy has
  // round-tripped through a Postgres JSONB column, which reorders object
  // keys — a raw string compare never matches and the badge would read
  // "draft" forever, including on a freshly published graph nobody touched.
  const canonical = canonicalize(graph);

  // ── Undo/redo ─────────────────────────────────────────────────────────
  // Autosave means there is no Cancel, so there has to be a way back.
  const past = useRef<string[]>([]);
  const future = useRef<string[]>([]);
  const lastGraph = useRef<string>("");
  const lastPushAt = useRef(0);
  const timeTravelling = useRef(false);
  // Mirrored into state, not read off the refs at render time: a ref
  // changing doesn't re-render, so the buttons would sit at their initial
  // enabled/disabled state forever.
  const [depth, setDepth] = useState({ undo: 0, redo: 0 });
  const syncDepth = useCallback(
    () => setDepth({ undo: past.current.length, redo: future.current.length }),
    [],
  );

  useEffect(() => {
    if (!loaded.current) return;
    if (timeTravelling.current) {
      timeTravelling.current = false;
      lastGraph.current = serialized;
      return;
    }
    if (lastGraph.current && lastGraph.current !== serialized) {
      const now = Date.now();
      // Collapse a drag (dozens of intermediate positions) into one step.
      if (now - lastPushAt.current > UNDO_COALESCE_MS) {
        past.current.push(lastGraph.current);
        if (past.current.length > UNDO_LIMIT) past.current.shift();
        lastPushAt.current = now;
      }
      future.current = [];
      syncDepth();
    }
    lastGraph.current = serialized;
  }, [serialized, syncDepth]);

  const restore = useCallback((snapshot: string) => {
    const rf = toReactFlow(JSON.parse(snapshot) as WorkflowGraph);
    timeTravelling.current = true;
    setNodes(rf.nodes);
    setEdges(rf.edges);
    setSelection(null);
    syncDepth();
  }, [setNodes, setEdges, syncDepth]);

  const undo = useCallback(() => {
    const previous = past.current.pop();
    if (previous === undefined) return;
    future.current.push(lastGraph.current);
    restore(previous);
  }, [restore]);

  const redo = useCallback(() => {
    const next = future.current.pop();
    if (next === undefined) return;
    past.current.push(lastGraph.current);
    restore(next);
  }, [restore]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Never steal the browser's own undo while someone is typing a prompt.
      const el = e.target as HTMLElement | null;
      if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) redo(); else undo();
      }
      if (e.key === "Escape") setAddOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo, redo]);

  // Autosave: abort + config_version fence so a late PUT cannot clobber after publish.
  useEffect(() => {
    if (!loaded.current || publishing) return;
    if (seedPristine.current !== null) {
      if (serialized === seedPristine.current) return;
      seedPristine.current = null;
    }
    const gen = ++saveGen.current;
    saveAbort.current?.abort();
    const ac = new AbortController();
    saveAbort.current = ac;
    setSaveState("saving");
    const timer = setTimeout(() => {
      saveWorkflowDraft(tenantSlug, agentId, JSON.parse(serialized), {
        signal: ac.signal,
        baseConfigVersion: configVersion.current,
      })
        .then((res) => {
          if (gen !== saveGen.current) return;
          if (typeof res.config_version === "number") configVersion.current = res.config_version;
          setSaveState("saved");
        })
        .catch((e) => {
          if (ac.signal.aborted) return;
          // Publish won the race — drop this save; next edit retries with fresh version.
          if (e instanceof ApiError && e.status === 409) {
            setSaveState("idle");
            getWorkflow(tenantSlug, agentId).then((state) => {
              configVersion.current = state.config_version;
            }).catch(() => {});
            return;
          }
          setSaveState("idle");
          setError(e instanceof ApiError ? e.detail : String(e));
        });
    }, AUTOSAVE_MS);
    return () => clearTimeout(timer);
  }, [serialized, tenantSlug, agentId, publishing]);

  // ── Live validation ───────────────────────────────────────────────────
  // The same check that gates a publish, run as you draw — so "this
  // connection has no condition" surfaces while you're looking at it,
  // instead of as a wall of red after you press Publish.
  useEffect(() => {
    if (!loaded.current) return;
    let current = true;
    const timer = setTimeout(() => {
      validateWorkflow(tenantSlug, agentId, JSON.parse(serialized))
        .then((result) => {
          if (!current) return;   // a newer edit already superseded this
          setErrors(result.errors ?? []);
          setWarnings(result.warnings ?? []);
        })
        .catch(() => {/* the publish attempt reports for real; don't nag */});
    }, VALIDATE_MS);
    return () => { current = false; clearTimeout(timer); };
  }, [serialized, tenantSlug, agentId]);

  // ── Canvas painting ───────────────────────────────────────────────────
  const badNodeIds = useMemo(
    () => new Set(errors.filter((e) => e.kind === "node").map((e) => e.id)),
    [errors],
  );
  const badEdgeIds = useMemo(
    () => new Set(errors.filter((e) => e.kind === "edge").map((e) => e.id)),
    [errors],
  );

  const paintedNodes = useMemo(() => nodes.map((n) => {
    const invalid = badNodeIds.has(n.id);
    if (Boolean(n.data.__invalid) === invalid) return n;
    return { ...n, data: { ...n.data, __invalid: invalid } };
  }), [nodes, badNodeIds]);

  // ConditionEdge draws the label itself, and reads __invalid off data.
  // toGraph strips the marker on the way out (see there) — it can't just be
  // assumed not to reach it, because onEdgeClick selects off this derived
  // copy and editing writes the selection back into the real edge.
  const paintedEdges = useMemo(() => edges.map((e) => {
    const invalid = badEdgeIds.has(e.id);
    return {
      ...e,
      type: "condition",
      label: undefined,
      data: { ...(e.data as WorkflowEdgeData), __invalid: invalid },
      className: invalid ? "wf-edge-invalid" : !e.data?.condition?.trim() ? "wf-edge-unfinished" : undefined,
      animated: invalid,
    };
  }), [edges, badEdgeIds]);

  // ── Editing ───────────────────────────────────────────────────────────
  const onConnect = useCallback((connection: Connection) => {
    setEdges((eds) =>
      addEdge(
        {
          ...connection,
          id: `e-${connection.source}-${connection.target}-${Date.now()}`,
          label: "",
          data: { label: "", condition: "" },
        } as RFEdge,
        eds,
      ),
    );
  }, [setEdges]);

  const selectNode = useCallback((node: RFNode) => {
    setSelection({
      kind: "node", id: node.id,
      nodeType: node.type as WorkflowNodeType,
      data: node.data as WorkflowNodeData,
    });
  }, []);

  const updateNode = (id: string, data: WorkflowNodeData) => {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...data } } : n)));
    setSelection((s) => (s && s.id === id ? { ...s, data } : s));
  };

  const changeNodeType = (id: string, type: WorkflowNodeType) => {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, type } : n)));
    setSelection((s) => (s && s.id === id ? { ...s, nodeType: type } : s));
  };

  const updateEdge = (id: string, data: WorkflowEdgeData) => {
    setEdges((es) => es.map((e) => (e.id === id ? { ...e, label: data.label, data } : e)));
    setSelection((s) => (s && s.id === id ? { ...s, data } : s));
  };

  /** Re-layout top-to-bottom and re-centre. Autosave picks the new
   *  positions up like any other edit, so there is nothing to save here. */
  const tidyUp = () => {
    setNodes((ns) => autoLayout(ns, edges));
    window.setTimeout(() => reactFlow.fitView({ duration: 400, padding: 0.2 }), 0);
  };

  const remove = (sel: Selection) => {
    if (sel.kind === "node") {
      setNodes((ns) => ns.filter((n) => n.id !== sel.id));
      setEdges((es) => es.filter((e) => e.source !== sel.id && e.target !== sel.id));
    } else {
      setEdges((es) => es.filter((e) => e.id !== sel.id));
    }
    setSelection(null);
  };

  const addNode = (type: Exclude<WorkflowNodeType, "start">, from?: string) => {
    const id = `${type}-${Date.now()}`;
    const origin = from ? nodes.find((n) => n.id === from) : undefined;
    // Branching is the normal case — a step routes to booking OR to Q&A —
    // so the second child of a step has to land beside the first, not on
    // top of it. Offset by however many branches already leave `from`.
    const siblings = from ? edges.filter((e) => e.source === from).length : 0;
    const position = origin
      ? { x: origin.position.x + siblings * 360, y: origin.position.y + 230 }
      : { x: 320, y: 60 + nodes.length * 40 };
    const node = { id, type, position, data: { ...NEW_NODE_DEFAULTS[type] }, deletable: true } as RFNode;

    setNodes((ns) => [...ns, node]);
    if (from) {
      setEdges((es) => [...es, {
        id: `e-${from}-${id}`, source: from, target: id,
        label: "", data: { label: "", condition: "" },
      } as RFEdge]);
    }
    setAddOpen(false);
    // Land the operator straight in the form for what they just made,
    // rather than making them find and click it.
    selectNode(node);
  };

  const addConnectedStage = useCallback((fromNodeId: string) => {
    addNode("agent", fromNodeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes]);

  /** Jump to whatever a problem is talking about. A message naming a node
   *  you then have to go hunting for is barely better than no message. */
  const revealProblem = (problem: WorkflowError) => {
    if (!problem.id) return;
    const node = nodes.find((n) => n.id === problem.id);
    if (node) {
      selectNode(node);
      reactFlow.fitView({ nodes: [{ id: node.id }], duration: 400, maxZoom: 1.3 });
      return;
    }
    const edge = edges.find((e) => e.id === problem.id);
    if (edge) {
      setSelection({
        kind: "edge", id: edge.id,
        data: (edge.data || { label: "", condition: "" }) as WorkflowEdgeData,
      });
      reactFlow.fitView({ nodes: [{ id: edge.source }, { id: edge.target }], duration: 400, maxZoom: 1.3 });
    }
  };

  // ── Publish ───────────────────────────────────────────────────────────
  const publish = async () => {
    // Invalidate in-flight draft PUTs before writing live+draft on the server.
    saveAbort.current?.abort();
    saveGen.current += 1;
    setPublishing(true);
    setError(null);
    try {
      const result = await publishWorkflow(tenantSlug, agentId, JSON.parse(serialized));
      setWarnings(result.warnings);
      setErrors([]);
      configVersion.current = result.config_version;
      // Refetch so chrome-only / no-op publish cannot leave a false "clean" badge.
      const state = await getWorkflow(tenantSlug, agentId);
      applyWorkflowState(state);
      seedPristine.current = null;
      setVersionKey((k) => k + 1);
      setJustPublished(true);
      setTimeout(() => setJustPublished(false), 4000);
    } catch (e) {
      const { message, errors: found } = publishErrors(e);
      setError(message);
      setErrors(found);
    } finally {
      setPublishing(false);
    }
  };


  if (loading) return <div className="empty-state">Loading workflow…</div>;

  const diverged = published !== canonical;
  const blocking = errors.length > 0;
  const status = published === null ? "not live" : diverged ? "unpublished changes" : "live";

  return (
    <EditorContext.Provider value={{ addConnectedStage }}>
      <div className="wf-root">
        {showHelp && (
          <div className="wf-help">
            <div>
              <strong>A workflow splits the call into stages.</strong> Each stage has its own
              instructions and its own tools, and the agent moves between them when a
              connection&apos;s condition is met. Callers only reach a stage once the agent has
              earned its way there — so it can&apos;t book before it has verified.
            </div>
            <button className="btn btn-ghost btn-sm" onClick={() => setShowHelp(false)}>Got it</button>
          </div>
        )}

        <div className="wf-toolbar">
          {header && (
            <>
              <Link href={header.backHref} className="wf-back-btn" title="Back to Workflows">←</Link>
              <span className="wf-page-title">{header.title}</span>
              <span className="wf-toolbar-sep" />
            </>
          )}
          <button
            className="btn btn-ghost btn-sm"
            title="Undo (Ctrl+Z)"
            disabled={depth.undo === 0}
            onClick={undo}
          >
            ↶
          </button>
          <button
            className="btn btn-ghost btn-sm"
            title="Redo (Ctrl+Shift+Z)"
            disabled={depth.redo === 0}
            onClick={redo}
          >
            ↷
          </button>
          <button
            className={`btn btn-sm ${testing ? "btn-primary" : "btn-ghost"}`}
            title={
              diverged
                ? "Tests the live (published) agent — unpublished canvas changes are not included yet"
                : "Try the live agent in the browser"
            }
            onClick={() => setTesting(!testing)}
          >
            {diverged ? "Test live agent" : "Test Agent"}
          </button>

          <span className={`badge ${published === null ? "gray" : diverged ? "amber" : "green"}`}>
            {status}
          </span>
          <span className="wf-save-state">
            {saveState === "saving" ? "Saving…" : saveState === "saved" ? "Draft saved" : ""}
          </span>

          <div className="wf-toolbar-right">
            <button
              className="btn btn-primary btn-sm"
              disabled={publishing || blocking || !diverged || justPublished}
              title={
                blocking ? "Fix the problems listed below first"
                  : justPublished ? "Just published"
                  : !diverged ? "Nothing has changed since the last publish"
                  : "Make this the flow live calls run"
              }
              onClick={publish}
            >
              {publishing ? "Publishing…"
                : justPublished ? "Published ✓"
                : blocking ? `${errors.length} to fix`
                : "Publish"}
            </button>

            {/* Everything you reach for once a session, not once a minute. */}
            <div className="wf-menu-wrap">
              <button
                className="btn btn-ghost btn-sm"
                aria-expanded={moreOpen}
                aria-haspopup="menu"
                title="More"
                onClick={() => setMoreOpen((o) => !o)}
              >
                ⋮
              </button>
              {moreOpen && (
                <>
                  <div className="wf-menu-scrim" onClick={() => setMoreOpen(false)} />
                  <div className="wf-menu wf-menu-right" role="menu">
                    {header && (
                      <Link role="menuitem" href={header.settingsHref} onClick={() => setMoreOpen(false)}>
                        <span>Settings</span>
                        <small>Voice, model, tools and number</small>
                      </Link>
                    )}
                    <button
                      role="menuitem"
                      onClick={() => { setMoreOpen(false); setShowHistory((v) => !v); }}
                    >
                      <span>{showHistory ? "Hide" : "Show"} publish history</span>
                      <small>Earlier versions, and roll back</small>
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>

        {error && <div className="error-banner">{error}</div>}

        <div className="wf-layout">
          <div className="wf-canvas">
            <ReactFlow
              nodes={paintedNodes}
              edges={paintedEdges}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onNodeClick={(_, node) => selectNode(node as RFNode)}
              onEdgeClick={(_, edge) =>
                setSelection({
                  kind: "edge", id: edge.id,
                  data: (edge.data || { label: "", condition: "" }) as WorkflowEdgeData,
                })
              }
              onPaneClick={() => setSelection(null)}
              onNodesDelete={() => setSelection(null)}
              onEdgesDelete={() => setSelection(null)}
              deleteKeyCode={["Delete", "Backspace"]}
              fitView
              minZoom={0.2}
              proOptions={{ hideAttribution: false }}
            >
              <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--border-2)" />
              <Controls showInteractive={false}>
                {/* Drag a few nodes around and the graph stops reading as a
                    flow. This re-lays it out top-to-bottom and re-centres —
                    the one canvas action that is pure clean-up, so it lives
                    with the other view controls rather than in a menu. */}
                <ControlButton onClick={tidyUp} title="Tidy up — arrange the stages and centre them">
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6">
                    <rect x="5.5" y="1.5" width="5" height="3.5" rx="1" />
                    <rect x="1" y="11" width="5" height="3.5" rx="1" />
                    <rect x="10" y="11" width="5" height="3.5" rx="1" />
                    <path d="M8 5v2.5M3.5 11V7.5h9V11" />
                  </svg>
                </ControlButton>
              </Controls>

              {/* Adding a node belongs on the canvas you're adding it to,
                  not in a page toolbar — same placement as Dograh's. */}
              <FlowPanel position="top-right" className="wf-canvas-panel">
                <div className="wf-menu-wrap">
                  <button
                    className="wf-canvas-btn"
                    aria-expanded={addOpen}
                    aria-haspopup="menu"
                    title="Add a node"
                    onClick={() => setAddOpen((o) => !o)}
                  >
                    +
                  </button>
                  {addOpen && (
                    <>
                      {/* Click-anywhere-else closes it, the way every other menu does. */}
                      <div className="wf-menu-scrim" onClick={() => setAddOpen(false)} />
                      <div className="wf-menu wf-menu-right" role="menu">
                        <button role="menuitem" onClick={() => addNode("agent")}>
                          <span>Stage</span><small>Another step in the conversation</small>
                        </button>
                        <button role="menuitem" onClick={() => addNode("transfer")}>
                          <span>Transfer to a human</span><small>Hands the call over</small>
                        </button>
                        <button role="menuitem" onClick={() => addNode("end")}>
                          <span>End the call</span><small>Hangs up, with an outcome</small>
                        </button>
                        <button
                          role="menuitem"
                          disabled={hasGlobal}
                          title={hasGlobal ? "This flow already has one" : undefined}
                          onClick={() => addNode("global")}
                        >
                          <span>Always applies</span>
                          <small>{hasGlobal ? "Already added" : "Instructions for every step"}</small>
                        </button>
                      </div>
                    </>
                  )}
                </div>
              </FlowPanel>
            </ReactFlow>
          </div>
          {/* The column only exists when it has something to say: a
              selected node to edit, or a test session. With neither, the
              canvas gets the whole page — which is what you want open in
              front of you while you are drawing a flow. */}
          {(testing || selection) && (
          <div className="wf-side">
            {testing ? (
              <TestAgentPanel
                open
                onClose={() => setTesting(false)}
                tenantSlug={tenantSlug}
                agentSlug={agentSlug}
              />
            ) : (
              <Inspector
                selection={selection}
                agentTools={agentTools}
                knowledgeBases={knowledgeBases}
                errors={errors}
                warnings={warnings}
                availableVariables={availableVariables}
                onChangeNode={updateNode}
                onChangeNodeType={changeNodeType}
                onChangeEdge={updateEdge}
                onDelete={remove}
              />
            )}
          </div>
          )}
        </div>

        <ProblemList errors={errors} warnings={warnings} onReveal={revealProblem} />

        <div className="wf-history">
          {showHistory && (
            <VersionPanel
              tenantSlug={tenantSlug}
              agentId={agentId}
              refreshKey={versionKey}
              onRolledBack={() => {
                saveAbort.current?.abort();
                saveGen.current += 1;
                setVersionKey((k) => k + 1);
                getWorkflow(tenantSlug, agentId).then((state) => {
                  applyWorkflowState(state);
                  seedPristine.current = null;
                });
              }}
            />
          )}
        </div>

      </div>
    </EditorContext.Provider>
  );
}

function ProblemList({
  errors, warnings, onReveal,
}: { errors: WorkflowError[]; warnings: WorkflowError[]; onReveal: (p: WorkflowError) => void }) {
  if (!errors.length && !warnings.length) {
    return <div className="wf-problems wf-problems-ok">No problems — this flow is ready to publish.</div>;
  }
  const row = (p: WorkflowError, kind: "error" | "warning", i: number) => (
    <button
      key={`${kind}-${i}`}
      className={`wf-problem wf-problem-${kind}`}
      onClick={() => onReveal(p)}
      disabled={!p.id}
      title={p.id ? "Show me" : undefined}
    >
      <span className="wf-problem-dot" aria-hidden />
      <span>{p.message}</span>
    </button>
  );
  return (
    <div className="wf-problems">
      {errors.length > 0 && (
        <div className="wf-problems-hdr">
          {errors.length} thing{errors.length === 1 ? "" : "s"} to fix before this can go live
        </div>
      )}
      {errors.map((e, i) => row(e, "error", i))}
      {warnings.length > 0 && (
        <div className="wf-problems-hdr wf-problems-hdr-warn">
          {warnings.length} thing{warnings.length === 1 ? "" : "s"} worth a look — these won&apos;t block publishing
        </div>
      )}
      {warnings.map((w, i) => row(w, "warning", i))}
    </div>
  );
}

export function WorkflowPanel(props: {
  tenantSlug: string;
  agentId: string;
  agentSlug: string;
  greeting?: string;
  systemPrompt?: string;
  header?: WorkflowHeader;
}) {
  return (
    <ReactFlowProvider>
      <Panel {...props} />
    </ReactFlowProvider>
  );
}
