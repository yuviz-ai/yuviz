"use client";

// Call-flow canvas: draw the IVR, autosave a draft, publish when valid.
//
// Same shape as components/workflow/WorkflowPanel.tsx (canvas + inspector +
// debounced draft save + publish gated on server validation) but against
// call_flows, and with the inspector fields that an IVR step actually has.
// Validation is always the server's — libs/config_sdk/callflow.py is the one
// definition of a valid flow, so the editor never has a second opinion that
// could drift from what publish will accept.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  addEdge,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useRouter } from "next/navigation";
import { Agent, ApiError, ProviderConfig, listAgents, listProviders, updateAgent } from "@/lib/api";
import {
  CallFlow,
  CallFlowGraph,
  CallFlowNodeData,
  CallFlowNodeType,
  CallFlowProblem,
  DTMF_KEYS,
  MENU_FALLBACK_KEYS,
  deleteCallFlow,
  getCallFlow,
  publishCallFlow,
  saveCallFlowDraft,
  validateCallFlow,
} from "@/lib/callFlowApi";
import { nodeTypes } from "./nodes";
import { edgeTypes } from "./edges";
import { CallFlowVersionPanel } from "./VersionPanel";
import { VoicePreviewButton } from "./VoicePreviewButton";

const ADDABLE: { type: CallFlowNodeType; label: string; hint: string }[] = [
  { type: "play", label: "Play message", hint: "Speaks a line, then continues" },
  { type: "menu", label: "Menu", hint: "Branches on a keypress" },
  { type: "collect", label: "Collect digits", hint: "Gathers a number into a value" },
  { type: "dial", label: "Transfer", hint: "Sends the call to a human" },
  { type: "agent", label: "AI agent", hint: "Hands over to a conversational agent" },
  { type: "hangup", label: "Hang up", hint: "Ends the call" },
];

const DRAFT_DEBOUNCE_MS = 900;

// The drag payload. A private MIME type rather than text/plain so dragging
// selected text over the canvas can't be mistaken for dropping a step.
const DND_TYPE = "application/yuviz-callflow-step";

// Steps the call leaves the flow at — they have no outgoing edge, so nothing
// auto-chains off them.
const TERMINAL_TYPES: CallFlowNodeType[] = ["dial", "agent", "hangup"];

function toReactFlow(graph: CallFlowGraph): { nodes: Node[]; edges: Edge[] } {
  const menuIds = new Set(graph.nodes.filter((n) => n.type === "menu").map((n) => n.id));
  return {
    nodes: graph.nodes.map((n, i) => ({
      id: n.id,
      type: n.type,
      position: n.position ?? { x: i * 280, y: 160 },
      data: (n.data ?? {}) as Record<string, unknown>,
    })),
    edges: graph.edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      type: "keypress",
      data: { ...(e.data ?? {}), __fromMenu: menuIds.has(e.source) },
    })),
  };
}

function toGraph(nodes: Node[], edges: Edge[]): CallFlowGraph {
  return {
    version: 1,
    nodes: nodes.map((n) => ({
      id: n.id,
      type: n.type as CallFlowNodeType,
      data: n.data as CallFlowNodeData,
      position: n.position,
    })),
    edges: edges.map((e) => {
      const key = (e.data as { key?: string } | undefined)?.key;
      return { id: e.id, source: e.source, target: e.target, ...(key ? { data: { key } } : {}) };
    }),
  };
}

function Canvas({ flow, tenantSlug }: { flow: CallFlow; tenantSlug: string }) {
  const router = useRouter();
  const initial = useMemo(
    () => toReactFlow(flow.graph_draft ?? flow.graph ?? { version: 1, nodes: [], edges: [] }),
    [flow],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(initial.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initial.edges);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [problems, setProblems] = useState<CallFlowProblem[]>([]);
  const [valid, setValid] = useState(true);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [publishing, setPublishing] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [ttsProviders, setTtsProviders] = useState<ProviderConfig[]>([]);
  const [version, setVersion] = useState(flow.config_version);

  const saveTimer = useRef<number | null>(null);
  const firstRender = useRef(true);
  const [attaching, setAttaching] = useState<string | null>(null);
  const [draggingType, setDraggingType] = useState<CallFlowNodeType | null>(null);
  const [showVersions, setShowVersions] = useState(false);
  const [versionsKey, setVersionsKey] = useState(0);
  const [deleting, setDeleting] = useState(false);
  const [published, setPublished] = useState(flow.graph !== null);
  const { screenToFlowPosition } = useReactFlow();

  useEffect(() => {
    listAgents(tenantSlug).then(setAgents).catch(() => {});
  }, [tenantSlug]);

  useEffect(() => {
    listProviders(flow.tenant_id, { role: "tts" }).then(setTtsProviders).catch(() => {});
  }, [flow.tenant_id]);

  // Attaching is owned by the flow, not by the agent: this is the page where
  // you decide which agents answer behind this IVR, so the write happens
  // here (agents.call_flow_id) rather than in a picker buried in each
  // agent's own settings.
  const toggleAgent = async (agent: Agent, attach: boolean) => {
    setAttaching(agent.id);
    setBanner(null);
    try {
      const updated = await updateAgent(tenantSlug, agent.id, {
        call_flow_id: attach ? flow.id : null,
      });
      setAgents((as) => as.map((a) => (a.id === updated.id ? updated : a)));
    } catch (e) {
      setBanner(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setAttaching(null);
    }
  };

  const graph = useMemo(() => toGraph(nodes, edges), [nodes, edges]);

  // Draft autosave + server validation, debounced together: both are about
  // "the canvas settled", and running them from one timer keeps the problems
  // panel in step with what was last saved.
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(async () => {
      setSaveState("saving");
      try {
        await saveCallFlowDraft(flow.id, graph, version);
        setSaveState("saved");
      } catch (e) {
        setSaveState("error");
        setBanner(e instanceof ApiError ? e.detail : String(e));
        return;
      }
      const res = await validateCallFlow(flow.id, graph).catch(() => null);
      if (res) {
        setValid(res.valid);
        setProblems(res.problems);
      }
    }, DRAFT_DEBOUNCE_MS);
    return () => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    };
  }, [graph, flow.id, version]);

  const onConnect = useCallback(
    (c: Connection) => {
      const fromMenu = nodes.find((n) => n.id === c.source)?.type === "menu";
      setEdges((eds) =>
        addEdge(
          { ...c, type: "keypress", data: { __fromMenu: fromMenu }, id: `e-${c.source}-${c.target}-${Date.now()}` },
          eds,
        ),
      );
    },
    [nodes, setEdges],
  );

  // One factory for both ways of adding a step, so a dropped node and a
  // clicked one are never subtly different objects.
  const makeNode = (type: CallFlowNodeType, position: { x: number; y: number }): Node => ({
    id: `${type}-${Date.now().toString(36)}`,
    type,
    position,
    data: { name: ADDABLE.find((a) => a.type === type)?.label ?? type, prompt: "" },
  });

  /** Wire `from` -> `to` unless `from` is terminal or already leads somewhere
   *  it shouldn't be silently added to. Returns the edge, or null. */
  const autoEdge = (fromId: string | null, toId: string): Edge | null => {
    if (!fromId) return null;
    const from = nodes.find((n) => n.id === fromId);
    if (!from || TERMINAL_TYPES.includes(from.type as CallFlowNodeType)) return null;
    // A non-menu step may only lead to one place, so don't add a second.
    const already = edges.filter((e) => e.source === fromId).length;
    if (from.type !== "menu" && already > 0) return null;
    return {
      id: `e-${fromId}-${toId}-${Date.now()}`,
      source: fromId,
      target: toId,
      type: "keypress",
      data: { __fromMenu: from.type === "menu" },
    };
  };

  const placeNode = (type: CallFlowNodeType, position: { x: number; y: number }) => {
    const node = makeNode(type, position);
    // Chain onto whatever was selected, so clicking Play then Menu then Hang
    // up builds a connected flow without drawing a single edge by hand.
    const edge = autoEdge(selectedId, node.id);
    setNodes((ns) => [...ns, node]);
    if (edge) setEdges((es) => [...es, edge]);
    setSelectedId(node.id);
  };

  const addNode = (type: CallFlowNodeType) => {
    const from = nodes.find((n) => n.id === selectedId);
    const maxX = nodes.reduce((m, n) => Math.max(m, n.position.x), 0);
    // Place it just right of the selected step when chaining, so the new card
    // lands where the edge points rather than at the far end of the canvas.
    const position = from ? { x: from.position.x + 280, y: from.position.y } : { x: maxX + 280, y: 160 };
    placeNode(type, position);
  };

  const onDragOver = useCallback((e: React.DragEvent) => {
    if (!e.dataTransfer.types.includes(DND_TYPE)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  }, []);

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      const type = e.dataTransfer.getData(DND_TYPE) as CallFlowNodeType;
      if (!type) return;
      e.preventDefault();
      // Drop where the cursor actually is, in graph coordinates — not screen
      // ones, or the step lands somewhere else at any zoom but 100%.
      const position = screenToFlowPosition({ x: e.clientX, y: e.clientY });
      placeNode(type, position);
      setDraggingType(null);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [screenToFlowPosition, setNodes, setEdges, selectedId, nodes, edges],
  );

  const patchNode = (id: string, patch: Partial<CallFlowNodeData>) =>
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)));

  const patchEdgeKey = (id: string, key: string) =>
    setEdges((es) => es.map((e) => (e.id === id ? { ...e, data: { ...e.data, key } } : e)));

  const deleteSelected = () => {
    if (selectedId) {
      setNodes((ns) => ns.filter((n) => n.id !== selectedId));
      setEdges((es) => es.filter((e) => e.source !== selectedId && e.target !== selectedId));
      setSelectedId(null);
    } else if (selectedEdgeId) {
      setEdges((es) => es.filter((e) => e.id !== selectedEdgeId));
      setSelectedEdgeId(null);
    }
  };

  const handlePublish = async () => {
    setPublishing(true);
    setBanner(null);
    try {
      const updated = await publishCallFlow(flow.id, graph);
      setVersion(updated.config_version);
      setPublished(true);
      setVersionsKey((k) => k + 1);
      setBanner(`Published v${updated.config_version}.`);
      setValid(true);
    } catch (e) {
      const errors = (e as { body?: { errors?: CallFlowProblem[] } })?.body?.errors;
      if (errors) {
        setProblems(errors);
        setValid(false);
        setBanner("This flow can't go live yet — see the problems below.");
      } else {
        setBanner(e instanceof ApiError ? e.detail : String(e));
      }
    } finally {
      setPublishing(false);
    }
  };

  const handleDelete = async () => {
    const msg =
      `Delete "${flow.name}"? Any agents attached to it go back to answering directly, ` +
      `and its published versions go with it.`;
    if (!window.confirm(msg)) return;
    setDeleting(true);
    try {
      await deleteCallFlow(flow.id);
      router.push("/workflows");
    } catch (e) {
      setBanner(e instanceof ApiError ? e.detail : String(e));
      setDeleting(false);
    }
  };

  // A restore republishes an old graph as a new version, so the canvas has
  // to reload from the server rather than keep showing the graph that was
  // on screen when the restore was clicked.
  const handleRolledBack = async () => {
    const fresh = await getCallFlow(flow.id).catch(() => null);
    if (!fresh) return;
    const rf = toReactFlow(fresh.graph_draft ?? fresh.graph ?? { version: 1, nodes: [], edges: [] });
    firstRender.current = true; // the reload is not an edit — don't autosave it back
    setNodes(rf.nodes);
    setEdges(rf.edges);
    setVersion(fresh.config_version);
    setVersionsKey((k) => k + 1);
    setBanner(`Restored — now live as v${fresh.config_version}.`);
  };

  const startNode = nodes.find((n) => n.type === "start") ?? null;
  const flowVoiceId = (startNode?.data as CallFlowNodeData | undefined)?.tts_config_id ?? null;
  // An agent step speaks in its OWN agent's voice, not the flow's — that is
  // the agent talking, and previewing it in the flow's voice would be a lie.
  const voiceForNode = (node: Node | null): string | null => {
    if (!node) return flowVoiceId;
    if (node.type !== "agent") return flowVoiceId;
    const agentId = (node.data as CallFlowNodeData).agent_id;
    return agents.find((a) => a.id === agentId)?.tts_config_id ?? flowVoiceId;
  };

  const selected = nodes.find((n) => n.id === selectedId) ?? null;
  const selectedEdge = edges.find((e) => e.id === selectedEdgeId) ?? null;
  const selectedEdgeFromMenu =
    selectedEdge && nodes.find((n) => n.id === selectedEdge.source)?.type === "menu";
  const d = (selected?.data ?? {}) as CallFlowNodeData;
  // Problems belonging to the selected step, shown in its own panel — the
  // flow-wide list names no step, so two identical messages from two
  // different steps are indistinguishable there.
  const selectedProblems = problems.filter(
    (pr) => (selected && pr.id === selected.id) || (selectedEdge && pr.id === selectedEdge.id),
  );
  const errors = problems.filter(() => !valid);
  const warnings = valid ? problems : [];

  return (
    <div className="wf-root">
      <div className="cf-header">
        <button className="btn btn-ghost btn-sm" onClick={() => router.push("/workflows")}>
          ← IVR Flows
        </button>
        <div className="cf-header-title">
          <h1>{flow.name}</h1>
          <span className="mono cf-header-ver">v{version}</span>
          <span className={`badge ${published ? "green" : "gray"}`}>
            {published ? "Published" : "Draft"}
          </span>
          <span className="cf-header-dir">{flow.direction}</span>
        </div>
        <div className="cf-header-actions">
          <span className="wf-save-state">
            {saveState === "saving" ? "Saving…" : saveState === "saved" ? "Draft saved" : saveState === "error" ? "Save failed" : ""}
          </span>
          <button className="btn btn-ghost btn-sm" onClick={() => setShowVersions((v) => !v)}>
            {showVersions ? "Hide history" : "Version history"}
          </button>
          <button className="btn btn-danger btn-sm" onClick={handleDelete} disabled={deleting}>
            {deleting ? "Deleting…" : "Delete"}
          </button>
          <button className="btn btn-primary btn-sm" onClick={handlePublish} disabled={publishing || !valid}>
            {publishing ? "Publishing…" : "Publish"}
          </button>
        </div>
      </div>

      {/* Drag a step onto the canvas to place it where you want; clicking
          appends it below the lowest step (and is the keyboard path). */}
      <div className={`cf-addbar${draggingType ? " dragging" : ""}`}>
        <span className="cf-addbar-label">Add step</span>
        {ADDABLE.map((a) => (
          <button
            key={a.type}
            type="button"
            className={`cf-chip cf-pill-${a.type}${draggingType === a.type ? " active" : ""}`}
            draggable
            onDragStart={(e) => {
              e.dataTransfer.setData(DND_TYPE, a.type);
              e.dataTransfer.effectAllowed = "copy";
              setDraggingType(a.type);
            }}
            onDragEnd={() => setDraggingType(null)}
            onClick={() => addNode(a.type)}
            title={`${a.hint} — drag onto the canvas, or click to append`}
          >
            <span className="cf-chip-grip" aria-hidden="true">⠿</span>
            {a.label}
          </button>
        ))}
      </div>

      {banner && <div className="wf-warn-banner">{banner}</div>}

      <div className="wf-layout">
        <div className="wf-canvas cf-canvas" onDragOver={onDragOver} onDrop={onDrop}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={(_, n) => { setSelectedId(n.id); setSelectedEdgeId(null); }}
            onEdgeClick={(_, e) => { setSelectedEdgeId(e.id); setSelectedId(null); }}
            onPaneClick={() => { setSelectedId(null); setSelectedEdgeId(null); }}
            // Without a maxZoom, fitView on a three-step flow zooms until the
            // cards fill the viewport and the canvas reads as broken.
            fitView
            fitViewOptions={{ maxZoom: 1, padding: 0.25 }}
            minZoom={0.3}
            maxZoom={1.6}
            // Default is 20px, which means aiming at an 8px dot. At 70 a
            // connection snaps to the nearest handle from most of the way
            // across a neighbouring card.
            connectionRadius={70}
            // Click the source handle, then click the target — an alternative
            // to dragging for anyone who finds the drag fiddly.
            connectOnClick
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={18} size={1} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>

        <aside className="wf-side">
          <div className="wf-inspector">
            <div className="wf-inspector-hdr">
              <span className="wf-inspector-title">
                {selected ? "Step settings" : selectedEdge ? "Branch" : "Nothing selected"}
              </span>
              {(selected || selectedEdge) && (
                <button className="btn btn-ghost btn-sm" style={{ marginLeft: "auto" }} onClick={deleteSelected}>
                  Delete
                </button>
              )}
            </div>

            {selected && (
              <div className="cf-inspector-sub">
                <span className="cf-inspector-kind">{selected.type}</span>
                <span className="mono">{selected.id}</span>
              </div>
            )}

            {!selected && !selectedEdge && (
              <div className="wf-inspector-empty">
                <div className="wf-inspector-empty-title">Pick a step</div>
                Click a step or a branch to edit it, or drag one in from the Add step bar above.
              </div>
            )}

            {selectedEdge && (
              <div style={{ padding: 12 }}>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <label className="form-label">
                    Keypress{" "}
                    <span className="hint">
                      {selectedEdgeFromMenu
                        ? "which key takes this branch"
                        : "only a menu branches on a key — this one just continues"}
                    </span>
                  </label>
                  <select
                    className="form-select"
                    disabled={!selectedEdgeFromMenu}
                    value={(selectedEdge.data as { key?: string } | undefined)?.key ?? ""}
                    onChange={(e) => patchEdgeKey(selectedEdge.id, e.target.value)}
                  >
                    <option value="">— pick a key —</option>
                    {DTMF_KEYS.map((k) => <option key={k} value={k}>Press {k}</option>)}
                    {MENU_FALLBACK_KEYS.map((k) => (
                      <option key={k} value={k}>{k === "timeout" ? "No answer (timeout)" : "Wrong key pressed"}</option>
                    ))}
                  </select>
                </div>
              </div>
            )}

            {selected && (
              <div style={{ padding: 12 }}>
                <div className="form-group">
                  <label className="form-label">Name <span className="hint">shown on the card</span></label>
                  <input
                    className="form-input"
                    value={d.name ?? ""}
                    onChange={(e) => patchNode(selected.id, { name: e.target.value })}
                  />
                </div>

                {selected.type === "start" && (
                  <div className="form-group">
                    <div className="cf-field-row">
                      <label className="form-label">
                        Voice <span className="hint">used by every spoken step in this flow</span>
                      </label>
                    </div>
                    <select
                      className="form-select"
                      value={d.tts_config_id ?? ""}
                      onChange={(e) => patchNode(selected.id, { tts_config_id: e.target.value || undefined })}
                    >
                      <option value="">— the account's default voice —</option>
                      {ttsProviders.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}{p.voice ? ` · ${p.voice}` : ""} ({p.engine})
                        </option>
                      ))}
                    </select>
                    <div style={{ marginTop: 8 }}>
                      <VoicePreviewButton
                        providerId={d.tts_config_id}
                        text="Hi there, this is how this voice sounds on a call."
                        label="Play sample"
                      />
                    </div>
                    {ttsProviders.length === 0 && (
                      <div className="form-hint" style={{ marginTop: 6 }}>
                        No voices configured for this account yet — add one under AI &amp; Voice.
                      </div>
                    )}
                  </div>
                )}

                {["play", "menu", "collect", "dial"].includes(selected.type as string) && (
                  <div className="form-group">
                    <div className="cf-field-row">
                      <label className="form-label">
                        Says{" "}
                        <span className="hint">
                          {selected.type === "dial" ? "optional — spoken before transferring" : "spoken to the caller"}
                        </span>
                      </label>
                      <VoicePreviewButton providerId={flowVoiceId} text={d.prompt ?? ""} compact />
                    </div>
                    <textarea
                      className="form-textarea"
                      style={{ minHeight: 72 }}
                      value={d.prompt ?? ""}
                      onChange={(e) => patchNode(selected.id, { prompt: e.target.value })}
                      placeholder={selected.type === "menu" ? "Press 1 for sales, 2 for support." : ""}
                    />
                  </div>
                )}

                {(selected.type === "menu" || selected.type === "collect") && (
                  <div className="form-row">
                    <div className="form-group">
                      <label className="form-label">Wait <span className="hint">ms</span></label>
                      <input
                        className="form-input" type="number" style={{ fontFamily: "var(--mono)" }}
                        value={d.timeout_ms ?? 5000}
                        onChange={(e) => patchNode(selected.id, { timeout_ms: Number(e.target.value) })}
                      />
                    </div>
                    <div className="form-group">
                      <label className="form-label">Retries</label>
                      <input
                        className="form-input" type="number" style={{ fontFamily: "var(--mono)" }}
                        value={d.max_retries ?? 2}
                        onChange={(e) => patchNode(selected.id, { max_retries: Number(e.target.value) })}
                      />
                    </div>
                  </div>
                )}

                {selected.type === "collect" && (
                  <>
                    <div className="form-group">
                      <label className="form-label">
                        Store as <span className="hint">later steps use this name</span>
                      </label>
                      <input
                        className="form-input" style={{ fontFamily: "var(--mono)" }}
                        value={d.variable ?? ""}
                        placeholder="account_number"
                        onChange={(e) => patchNode(selected.id, { variable: e.target.value })}
                      />
                    </div>
                    <div className="form-row">
                      <div className="form-group">
                        <label className="form-label">Min digits</label>
                        <input
                          className="form-input" type="number" min={1} style={{ fontFamily: "var(--mono)" }}
                          value={d.min_digits ?? 1}
                          onChange={(e) => patchNode(selected.id, { min_digits: Number(e.target.value) })}
                        />
                      </div>
                      <div className="form-group">
                        <label className="form-label">Max digits</label>
                        <input
                          className="form-input" type="number" min={1} style={{ fontFamily: "var(--mono)" }}
                          value={d.max_digits ?? 10}
                          onChange={(e) => patchNode(selected.id, { max_digits: Number(e.target.value) })}
                        />
                      </div>
                    </div>
                    <div className="form-group">
                      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: ".8rem" }}>
                        <input
                          type="checkbox"
                          checked={!!d.sensitive}
                          onChange={(e) => patchNode(selected.id, { sensitive: e.target.checked })}
                        />
                        this value is sensitive — don&apos;t store it
                      </label>
                    </div>
                  </>
                )}

                {selected.type === "dial" && (
                  <div className="form-group">
                    <label className="form-label">
                      Destination <span className="hint">phone number or SIP address</span>
                    </label>
                    <input
                      className="form-input" style={{ fontFamily: "var(--mono)", fontSize: ".75rem" }}
                      value={d.destination ?? ""}
                      placeholder="+18005550100 or sip:desk@example.com"
                      onChange={(e) => patchNode(selected.id, { destination: e.target.value })}
                    />
                  </div>
                )}

                {selected.type === "agent" && (
                  <div className="form-group">
                    <label className="form-label">
                      Agent <span className="hint">who takes the call from here</span>
                    </label>
                    <select
                      className="form-select"
                      value={d.agent_id ?? ""}
                      onChange={(e) => patchNode(selected.id, { agent_id: e.target.value })}
                    >
                      <option value="">— pick an agent —</option>
                      {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                    </select>
                    {(() => {
                      const picked = agents.find((a) => a.id === d.agent_id);
                      if (!picked) return null;
                      const greeting = picked.greeting?.trim();
                      return (
                        <div style={{ marginTop: 8 }}>
                          <div className="form-hint" style={{ marginBottom: 6 }}>
                            {greeting
                              ? `Opens with: “${greeting}”`
                              : "This agent has no greeting set, so there is nothing to preview."}
                          </div>
                          {greeting && (
                            <VoicePreviewButton
                              providerId={voiceForNode(selected)}
                              text={greeting}
                              label="Play the agent's greeting"
                            />
                          )}
                          {!picked.tts_config_id && greeting && (
                            <div className="form-hint" style={{ marginTop: 6 }}>
                              This agent has no voice of its own — previewing in the flow&apos;s voice.
                            </div>
                          )}
                        </div>
                      );
                    })()}
                  </div>
                )}

                {selectedProblems.length > 0 && (
                  <div className="cf-inspector-problems">
                    {selectedProblems.map((pr, i) => (
                      <div key={i}>{pr.message}</div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          {!selected && !selectedEdge && (
          <div className="wf-problems" style={{ marginBottom: 12 }}>
            <div className="wf-problems-hdr wf-problems-hdr-warn">
              Agents on this flow
            </div>
            <div style={{ padding: "8px 12px" }}>
              {agents.length === 0 ? (
                <div className="form-hint">No agents in this account yet.</div>
              ) : (
                agents.map((a) => {
                  const attached = a.call_flow_id === flow.id;
                  const onOther = !attached && !!a.call_flow_id;
                  return (
                    <label
                      key={a.id}
                      title={onOther ? "Currently attached to a different flow" : undefined}
                      style={{ display: "flex", alignItems: "center", gap: 8, padding: "3px 0", fontSize: ".76rem" }}
                    >
                      <input
                        type="checkbox"
                        checked={attached}
                        disabled={attaching === a.id}
                        onChange={(e) => toggleAgent(a, e.target.checked)}
                      />
                      <span style={{ opacity: onOther ? 0.55 : 1 }}>{a.name}</span>
                      {onOther && <span className="wf-badge" style={{ marginLeft: "auto" }}>other flow</span>}
                    </label>
                  );
                })
              )}
              <div className="form-hint" style={{ marginTop: 8 }}>
                A call this agent answers runs this flow first. Ticking an agent already on another
                flow moves it to this one.
              </div>
            </div>
          </div>
          )}

          {!selected && !selectedEdge && (
          <div className="wf-problems">
            <div className={`wf-problems-hdr${!valid ? "" : " wf-problems-hdr-warn"}`}>
              {!valid ? `${errors.length} problem(s)` : warnings.length ? `${warnings.length} suggestion(s)` : "Ready to publish"}
            </div>
            {!valid && errors.map((p, i) => (
              <div key={i} className="wf-problem wf-problem-error">
                <span className="wf-problem-dot" /> {p.message}
              </div>
            ))}
            {valid && warnings.map((p, i) => (
              <div key={i} className="wf-problem wf-problem-warning">
                <span className="wf-problem-dot" /> {p.message}
              </div>
            ))}
            {valid && warnings.length === 0 && (
              <div className="wf-problems-ok">No problems — this flow is ready to publish.</div>
            )}
          </div>
          )}
        </aside>
      </div>

      {showVersions && (
        <CallFlowVersionPanel
          callFlowId={flow.id}
          refreshKey={versionsKey}
          onRolledBack={handleRolledBack}
        />
      )}
    </div>
  );
}

export function CallFlowPanel(props: { flow: CallFlow; tenantSlug: string }) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}
