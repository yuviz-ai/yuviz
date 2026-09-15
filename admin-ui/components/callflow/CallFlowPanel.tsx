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
  type Connection,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useRouter } from "next/navigation";
import { Agent, ApiError, listAgents, updateAgent } from "@/lib/api";
import {
  CallFlow,
  CallFlowGraph,
  CallFlowNodeData,
  CallFlowNodeType,
  CallFlowProblem,
  DTMF_KEYS,
  MENU_FALLBACK_KEYS,
  publishCallFlow,
  saveCallFlowDraft,
  validateCallFlow,
} from "@/lib/callFlowApi";
import { nodeTypes } from "./nodes";
import { edgeTypes } from "./edges";

const ADDABLE: { type: CallFlowNodeType; label: string }[] = [
  { type: "play", label: "Play message" },
  { type: "menu", label: "Menu" },
  { type: "collect", label: "Collect digits" },
  { type: "dial", label: "Transfer" },
  { type: "agent", label: "AI agent" },
  { type: "hangup", label: "Hang up" },
];

const DRAFT_DEBOUNCE_MS = 900;

function toReactFlow(graph: CallFlowGraph): { nodes: Node[]; edges: Edge[] } {
  const menuIds = new Set(graph.nodes.filter((n) => n.type === "menu").map((n) => n.id));
  return {
    nodes: graph.nodes.map((n, i) => ({
      id: n.id,
      type: n.type,
      position: n.position ?? { x: 0, y: i * 150 },
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
  const [version, setVersion] = useState(flow.config_version);

  const saveTimer = useRef<number | null>(null);
  const firstRender = useRef(true);
  const [attaching, setAttaching] = useState<string | null>(null);

  useEffect(() => {
    listAgents(tenantSlug).then(setAgents).catch(() => {});
  }, [tenantSlug]);

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

  const addNode = (type: CallFlowNodeType) => {
    const id = `${type}-${Date.now().toString(36)}`;
    const maxY = nodes.reduce((m, n) => Math.max(m, n.position.y), 0);
    setNodes((ns) => [
      ...ns,
      {
        id,
        type,
        position: { x: 260, y: maxY + 40 },
        data: { name: ADDABLE.find((a) => a.type === type)?.label ?? type, prompt: "" },
      },
    ]);
    setSelectedId(id);
  };

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

  const selected = nodes.find((n) => n.id === selectedId) ?? null;
  const selectedEdge = edges.find((e) => e.id === selectedEdgeId) ?? null;
  const selectedEdgeFromMenu =
    selectedEdge && nodes.find((n) => n.id === selectedEdge.source)?.type === "menu";
  const d = (selected?.data ?? {}) as CallFlowNodeData;
  const errors = problems.filter(() => !valid);
  const warnings = valid ? problems : [];

  return (
    <div className="wf-root">
      <div className="wf-toolbar">
        <button className="wf-back-btn" onClick={() => router.push("/workflows")}>← Call Flows</button>
        <div className="wf-page-title">{flow.name}</div>
        <span className="wf-toolbar-sep" />
        <span className="mono" style={{ fontSize: ".7rem", opacity: 0.7 }}>v{version}</span>
        <div className="wf-toolbar-right">
          <span className="wf-save-state">
            {saveState === "saving" ? "Saving…" : saveState === "saved" ? "Draft saved" : saveState === "error" ? "Save failed" : ""}
          </span>
          <button className="btn btn-primary btn-sm" onClick={handlePublish} disabled={publishing || !valid}>
            {publishing ? "Publishing…" : "Publish"}
          </button>
        </div>
      </div>

      {banner && <div className="wf-warn-banner">{banner}</div>}

      <div className="wf-layout">
        <div className="wf-canvas">
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
            fitView
          >
            <Background />
            <Controls />
          </ReactFlow>

          <div className="wf-canvas-panel">
            {ADDABLE.map((a) => (
              <button key={a.type} className="wf-canvas-btn" onClick={() => addNode(a.type)}>
                + {a.label}
              </button>
            ))}
          </div>
        </div>

        <aside className="wf-side">
          <div className="wf-inspector">
            <div className="wf-inspector-hdr">
              <span className="wf-inspector-title">
                {selected ? "Step" : selectedEdge ? "Branch" : "Nothing selected"}
              </span>
              {(selected || selectedEdge) && (
                <button className="btn btn-ghost btn-sm" style={{ marginLeft: "auto" }} onClick={deleteSelected}>
                  Delete
                </button>
              )}
            </div>

            {!selected && !selectedEdge && (
              <div className="wf-inspector-empty">
                <div className="wf-inspector-empty-title">Pick a step</div>
                Click a step or a branch to edit it, or add one from the buttons on the canvas.
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

                {["play", "menu", "collect", "dial"].includes(selected.type as string) && (
                  <div className="form-group">
                    <label className="form-label">
                      Says{" "}
                      <span className="hint">
                        {selected.type === "dial" ? "optional — spoken before transferring" : "spoken to the caller"}
                      </span>
                    </label>
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
                  </div>
                )}
              </div>
            )}
          </div>

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
        </aside>
      </div>
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
