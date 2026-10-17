/* eslint-disable react-hooks/purity */
"use client";

// Call-flow canvas: draw the IVR, autosave a draft, publish when valid.
// Validation is always server-side (libs/config_sdk/callflow.py) so it can't drift from publish.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Settings } from "lucide-react";
import { Modal } from "@/components/Modal";
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
  CallFlowDirection,
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
  updateCallFlow,
  validateCallFlow,
} from "@/lib/callFlowApi";
import { nodeTypes } from "./nodes";
import { edgeTypes } from "./edges";
import { CallFlowVersionPanel } from "./VersionPanel";
import { VoicePreviewButton } from "./VoicePreviewButton";

const ADDABLE: { type: CallFlowNodeType; label: string; hint: string }[] = [
  { type: "play", label: "Play a message", hint: "Speaks a line, then continues" },
  { type: "menu", label: "Keypad menu", hint: "Callers choose with a key — press 1, press 2" },
  { type: "collect", label: "Ask for a number", hint: "Account, PIN or date of birth" },
  { type: "dial", label: "Transfer to a person", hint: "A phone number or SIP address" },
  { type: "agent", label: "Hand to an AI agent", hint: "Your agent takes over" },
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

type FlowMeta = Pick<CallFlow, "name" | "description" | "direction">;

const pickMeta = ({ name, description, direction }: CallFlow): FlowMeta => ({ name, description, direction });

const DIRECTIONS: { value: CallFlowDirection; label: string; blurb: string }[] = [
  { value: "inbound", label: "People call us", blurb: "Inbound" },
  { value: "outbound", label: "We call people", blurb: "Outbound" },
  { value: "both", label: "Both", blurb: "Either direction" },
];

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
  const [agentsLoaded, setAgentsLoaded] = useState(false);
  const [agentsError, setAgentsError] = useState<string | null>(null);
  const [ttsProviders, setTtsProviders] = useState<ProviderConfig[]>([]);
  const [version, setVersion] = useState(flow.config_version);

  const saveTimer = useRef<number | null>(null);
  const firstRender = useRef(true);
  const [draggingType, setDraggingType] = useState<CallFlowNodeType | null>(null);
  const [showVersions, setShowVersions] = useState(false);
  const [versionsKey, setVersionsKey] = useState(0);
  const [deleting, setDeleting] = useState(false);
  const [published, setPublished] = useState(flow.graph !== null);
  const [savedMeta, setSavedMeta] = useState(() => pickMeta(flow));
  const [meta, setMeta] = useState(savedMeta);
  const [savingMeta, setSavingMeta] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [agentPicks, setAgentPicks] = useState<Set<string>>(new Set());
  const { screenToFlowPosition } = useReactFlow();

  useEffect(() => {
    let cancelled = false;
    listAgents(tenantSlug)
      .then((found) => {
        if (cancelled) return;
        setAgents(found);
        setAgentsLoaded(true);
        setAgentsError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        setAgentsError(e instanceof ApiError ? e.detail : String(e));
      });
    return () => { cancelled = true; };
  }, [tenantSlug]);

  useEffect(() => {
    listProviders(flow.tenant_id, { role: "tts" }).then(setTtsProviders).catch(() => {});
  }, [flow.tenant_id]);

  const attached = agents.filter((a) => a.call_flow_id === flow.id);

  const openSettings = () => {
    if (!agentsLoaded) return;
    setMeta(savedMeta);
    setAgentPicks(new Set(attached.map((a) => a.id)));
    setSettingsError(null);
    setSettingsOpen(true);
  };

  // Writes call_flows for the details and agents.call_flow_id for each agent whose tick changed.
  const saveSettings = async () => {
    if (!agentsLoaded) return;
    setSavingMeta(true);
    setSettingsError(null);
    try {
      if ((Object.keys(meta) as (keyof FlowMeta)[]).some((k) => meta[k] !== savedMeta[k])) {
        setSavedMeta(pickMeta(await updateCallFlow(flow.id, { ...meta, name: meta.name.trim() })));
      }
      for (const a of agents) {
        const want = agentPicks.has(a.id);
        if (want === (a.call_flow_id === flow.id)) continue;
        const updated = await updateAgent(tenantSlug, a.id, { call_flow_id: want ? flow.id : null });
        setAgents((as) => as.map((x) => (x.id === updated.id ? updated : x)));
      }
      setSettingsOpen(false);
      setBanner("Settings saved.");
    } catch (e) {
      setSettingsError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSavingMeta(false);
    }
  };

  const graph = useMemo(() => toGraph(nodes, edges), [nodes, edges]);

  // One debounce for autosave and validation so the problems panel matches the saved draft.
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
        setBanner("This menu can't go live yet — see the problems below.");
      } else {
        setBanner(e instanceof ApiError ? e.detail : String(e));
      }
    } finally {
      setPublishing(false);
    }
  };

  const handleDelete = async () => {
    const msg =
      `Delete "${savedMeta.name}"? Any agents attached to it go back to answering directly, ` +
      `and its published versions go with it.`;
    if (!window.confirm(msg)) return;
    setDeleting(true);
    try {
      await deleteCallFlow(flow.id);
      router.push("/ivr-call");
    } catch (e) {
      setBanner(e instanceof ApiError ? e.detail : String(e));
      setDeleting(false);
    }
  };

  // A restore publishes a new version, so reload the canvas from the server.
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
  // The flow-wide list doesn't name steps, so show the selected step's problems separately.
  const selectedProblems = problems.filter(
    (pr) => (selected && pr.id === selected.id) || (selectedEdge && pr.id === selectedEdge.id),
  );
  const errors = problems.filter(() => !valid);
  const warnings = valid ? problems : [];

  return (
    <div className="wf-root">
      <div className="cf-header">
        <button className="btn btn-ghost btn-sm" onClick={() => router.push("/ivr-call")}>
          <ArrowLeft size={13} /> All IVR-Calls
        </button>
        <div className="cf-header-title">
          <h1>{savedMeta.name}</h1>
          <span className={`badge ${published ? "green" : "gray"}`}>
            {published ? `Published · v${version}` : "Draft — not published yet"}
          </span>
          <span className="badge gray">{DIRECTIONS.find((d) => d.value === savedMeta.direction)?.label}</span>
        </div>
        <div className="cf-header-actions">
          <span className="wf-save-state">
            {saveState === "saving" ? "Saving…" : saveState === "saved" ? "Draft saved" : saveState === "error" ? "Save failed" : ""}
          </span>
          <button className="btn btn-ghost btn-sm" onClick={openSettings} disabled={!agentsLoaded}>
            <Settings size={13} /> Settings
          </button>
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
            // Default 20px is too small to hit an 8px handle reliably.
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
                {selected ? "Step settings" : selectedEdge ? "Branch" : "Edit a step"}
              </span>
              {(selected || selectedEdge) && (
                <button className="btn btn-danger btn-sm" style={{ marginLeft: "auto" }} onClick={deleteSelected}>
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
                <ul style={{ margin: 0 }}>
                  <li><b>Click a step</b> on the canvas to change what it says or does.</li>
                  <li><b>Click a line</b> between steps to pick which key leads there.</li>
                  <li><b>Add a step</b> by clicking or dragging one from the bar above.</li>
                </ul>
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
                        Voice <span className="hint">speaks this menu&apos;s prompts; agent steps use their own voice</span>
                      </label>
                    </div>
                    <select
                      className="form-select"
                      value={d.tts_config_id ?? ""}
                      onChange={(e) => patchNode(selected.id, { tts_config_id: e.target.value || undefined })}
                    >
                      <option value="">— the account&apos;s default voice —</option>
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
                              This agent has no voice of its own — previewing in the menu&apos;s voice.
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
          <div className="cf-who">
            <div className="cf-who-title">Who hears this menu</div>
            {!agentsLoaded ? (
              <p>{agentsError ? `Could not load agents: ${agentsError}` : "Loading agents…"}</p>
            ) : savedMeta.direction !== "inbound" ? (
              <p>Calls only play menus set to People call us, so no caller hears this yet.</p>
            ) : attached.length > 0 && published ? (
              <p>Callers to <b>{attached.map((a) => a.name).join(", ")}</b> hear this menu first.</p>
            ) : attached.length > 0 ? (
              <p>This menu is attached to <b>{attached.map((a) => a.name).join(", ")}</b>, but callers cannot hear it until it is published.</p>
            ) : (
              <p>No agent uses this menu yet, so no caller hears it.</p>
            )}
            {!published && <p>Publish it once the steps look right — until then callers don&apos;t hear it.</p>}
            <button className="btn btn-ghost btn-sm" onClick={openSettings} disabled={!agentsLoaded}>
              {attached.length > 0 ? "Change agents" : "Choose agents"}
            </button>
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
              <div className="wf-problems-ok">No problems — this menu is ready to publish.</div>
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

      <Modal
        open={settingsOpen}
        title="IVR-Call settings"
        onClose={() => !savingMeta && setSettingsOpen(false)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setSettingsOpen(false)} disabled={savingMeta}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" onClick={saveSettings} disabled={!meta.name.trim() || savingMeta || !agentsLoaded}>
              {savingMeta ? "Saving…" : "Save"}
            </button>
          </>
        }
      >
        <div className="form-group">
          <label className="form-label">Name</label>
          <input
            className="form-input"
            value={meta.name}
            maxLength={80}
            onChange={(e) => setMeta({ ...meta, name: e.target.value })}
          />
        </div>
        <div className="form-group">
          <label className="form-label">Description <span className="qc-opt">(optional)</span></label>
          <textarea
            className="form-textarea"
            rows={2}
            value={meta.description}
            placeholder="e.g. Main line — sends callers to sales or support"
            onChange={(e) => setMeta({ ...meta, description: e.target.value })}
          />
        </div>
        <div className="form-group">
          <label className="form-label">Which calls is it for?</label>
          <div className="qc-toggle three" role="radiogroup" aria-label="Which calls is it for?">
            {DIRECTIONS.map((d) => (
              <button
                key={d.value}
                type="button"
                role="radio"
                aria-checked={meta.direction === d.value}
                className={meta.direction === d.value ? "on" : ""}
                onClick={() => setMeta({ ...meta, direction: d.value })}
              >
                <b>{d.label}</b>
                <span>{d.blurb}</span>
              </button>
            ))}
          </div>
          {meta.direction !== "inbound" && (
            <div className="form-hint">Calls only play menus set to &ldquo;People call us&rdquo; for now.</div>
          )}
        </div>
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label">Agents that use this menu</label>
          <div className="form-hint" style={{ marginBottom: 6 }}>
            Callers to a ticked agent hear this menu before the agent answers.
          </div>
          {agents.length === 0 ? (
            <div className="form-hint">No agents in this account yet.</div>
          ) : (
            <div className="qc-steps">
              {agents.map((a) => {
                const onOther = !!a.call_flow_id && a.call_flow_id !== flow.id;
                return (
                  <label key={a.id}>
                    <input
                      type="checkbox"
                      checked={agentPicks.has(a.id)}
                      onChange={(e) => {
                        const next = new Set(agentPicks);
                        if (e.target.checked) next.add(a.id); else next.delete(a.id);
                        setAgentPicks(next);
                      }}
                    />
                    <span>
                      <b>{a.name}</b>
                      {onOther && <span className="form-hint">Uses another menu — ticking moves it here.</span>}
                    </span>
                  </label>
                );
              })}
            </div>
          )}
        </div>
        {settingsError && <div className="error-banner" style={{ marginTop: 12 }}>{settingsError}</div>}
      </Modal>
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
