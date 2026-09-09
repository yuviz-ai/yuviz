"use client";

// Property forms for whatever is selected — one node, or one connection.
//
// The connection's CONDITION field gets more room than anything else on
// purpose: it becomes the description of the function the LLM calls to
// advance the conversation, so it is what actually decides the transition.
// Operators consistently under-write it and then wonder why transitions
// misfire, so the form says what it does rather than labelling it and
// hoping.

import { useRef } from "react";
import type {
  Extraction, WorkflowEdgeData, WorkflowError, WorkflowNodeData, WorkflowNodeType,
} from "@/lib/workflowApi";
import { ExtractionEditor } from "./ExtractionEditor";

// The codes the platform itself reasons about (libs/config_sdk/workflow.py's
// SYSTEM_DISPOSITIONS). An end node may carry any string — the calls filter
// reads distinct values out of the column — so this is a datalist, not a
// closed select.
const DISPOSITIONS = [
  "completed", "qualified", "not_qualified", "transferred", "abandoned", "failed",
];

// "global" is deliberately not here: this dropdown swaps a node's type in
// place, and converting a wired-up step into a handle-less always-applies
// node would strand the connections it already has.
const TYPE_CHOICES: { value: Exclude<WorkflowNodeType, "start" | "global">; label: string }[] = [
  { value: "agent", label: "Stage — a step in the conversation" },
  { value: "transfer", label: "Transfer — hand the call to a human" },
  { value: "end", label: "End — hang up" },
];

export interface Selection {
  kind: "node" | "edge";
  id: string;
  nodeType?: WorkflowNodeType;
  data: WorkflowNodeData | WorkflowEdgeData;
}

interface Props {
  selection: Selection | null;
  agentTools: string[];
  knowledgeBases: { id: string; name: string }[];
  errors: WorkflowError[];
  warnings: WorkflowError[];
  availableVariables: string[];
  onChangeNode: (id: string, data: WorkflowNodeData) => void;
  onChangeNodeType: (id: string, type: WorkflowNodeType) => void;
  onChangeEdge: (id: string, data: WorkflowEdgeData) => void;
  onDelete: (selection: Selection) => void;
}

function Problems({ items, kind }: { items: WorkflowError[]; kind: "error" | "warning" }) {
  if (!items.length) return null;
  return (
    <div className={kind === "error" ? "error-banner" : "wf-warn-banner"}>
      {items.map((e, i) => <div key={i}>{e.message}</div>)}
    </div>
  );
}

/** A textarea that can have {{ variables }} dropped in at the cursor.
 *  Typing the braces by hand is how you end up with {{ custmer_name }}
 *  rendering as empty air in a call recording. */
function PromptField({
  label, hint, value, placeholder, rows, variables, onChange,
}: {
  label: React.ReactNode;
  hint?: string;
  value: string;
  placeholder?: string;
  rows: number;
  variables: string[];
  onChange: (v: string) => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);

  const insert = (name: string) => {
    const el = ref.current;
    const token = `{{ ${name} }}`;
    if (!el) return onChange(`${value}${token}`);
    const at = el.selectionStart ?? value.length;
    const next = value.slice(0, at) + token + value.slice(el.selectionEnd ?? at);
    onChange(next);
    // Put the caret after what we just inserted, so typing continues where
    // the operator was, not at the end of the box.
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(at + token.length, at + token.length);
    });
  };

  return (
    <div className="form-group">
      <label className="form-label">{label}</label>
      <textarea
        ref={ref}
        className="form-textarea"
        style={{ minHeight: rows * 22 }}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
      {hint && <div className="form-hint">{hint}</div>}
      {variables.length > 0 && (
        <div className="wf-vars">
          <span className="wf-vars-label">Insert:</span>
          {variables.map((v) => (
            <button key={v} type="button" className="wf-var" title={`Insert {{ ${v} }}`} onClick={() => insert(v)}>
              {v}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function Inspector({
  selection, agentTools, knowledgeBases, errors, warnings, availableVariables,
  onChangeNode, onChangeNodeType, onChangeEdge, onDelete,
}: Props) {
  if (!selection) {
    return (
      <div className="wf-inspector">
        <div className="wf-inspector-empty">
          <div className="wf-inspector-empty-title">Nothing selected</div>
          <p>Click a stage or a connection to edit it.</p>
          <ul>
            <li>Drag from the dot under a stage to another stage to connect them.</li>
            <li>Or press <strong>+</strong> on a stage to add the next one, already connected.</li>
            <li>Select something and press <strong>Delete</strong> to remove it.</li>
          </ul>
        </div>
      </div>
    );
  }

  const mineErrors = errors.filter((e) => e.id === selection.id);
  const mineWarnings = warnings.filter((e) => e.id === selection.id);

  if (selection.kind === "edge") {
    const data = selection.data as WorkflowEdgeData;
    const set = (patch: Partial<WorkflowEdgeData>) => onChangeEdge(selection.id, { ...data, ...patch });
    return (
      <div className="wf-inspector">
        <div className="wf-inspector-hdr">
          <span className="wf-inspector-title">{data.label || "connection"}</span>
          <span className="badge gray">connection</span>
        </div>
        <Problems items={mineErrors} kind="error" />
        <Problems items={mineWarnings} kind="warning" />

        <div className="form-group">
          <label className="form-label">
            When should the call move on? <span className="required">*</span>
          </label>
          <textarea
            className="form-textarea wf-condition"
            style={{ minHeight: 120 }}
            value={data.condition || ""}
            placeholder="The caller has confirmed both their date of birth and postcode."
            onChange={(e) => set({ condition: e.target.value })}
          />
          <div className="form-hint">
            <strong>This is the field that decides everything.</strong> The agent re-reads it every
            turn and moves the moment it&apos;s true, so describe exactly what must have happened —
            not what the next stage does. It matters more than either stage&apos;s own instructions.
          </div>
        </div>

        <div className="form-group">
          <label className="form-label">
            Short name <span className="required">*</span>
            <span className="hint">shows on the arrow</span>
          </label>
          <input
            className="form-input"
            value={data.label || ""}
            placeholder="caller wants to book"
            onChange={(e) => set({ label: e.target.value })}
          />
          <div className="form-hint">
            The agent calls this move <code>{toToolName(data.label || "")}</code>. Two connections
            leaving the same stage can&apos;t end up with the same one.
          </div>
        </div>

        <div className="form-group">
          <label className="form-label">
            Say something while moving <span className="hint">optional</span>
          </label>
          <input
            className="form-input"
            value={data.transition_speech || ""}
            placeholder="Of course, let me pull up the calendar."
            onChange={(e) => set({ transition_speech: e.target.value })}
          />
          <div className="form-hint">
            Spoken straight away, so the caller isn&apos;t sitting in silence while the next stage
            works out what to say.
          </div>
        </div>

        <button className="btn btn-danger btn-sm" onClick={() => onDelete(selection)}>
          Delete connection
        </button>
      </div>
    );
  }

  const type = selection.nodeType!;
  const data = selection.data as WorkflowNodeData;
  const set = (patch: Partial<WorkflowNodeData>) => onChangeNode(selection.id, { ...data, ...patch });
  const toggle = (list: string[] | undefined, value: string): string[] => {
    const current = list || [];
    return current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
  };

  return (
    <div className="wf-inspector">
      <div className="wf-inspector-hdr">
        <span className="wf-inspector-title">{data.name || "unnamed"}</span>
        <span className="badge gray">{type === "agent" ? "stage" : type === "global" ? "always applies" : type}</span>
      </div>
      <Problems items={mineErrors} kind="error" />
      <Problems items={mineWarnings} kind="warning" />

      <div className="form-group">
        <label className="form-label">Name <span className="required">*</span></label>
        <input
          className="form-input"
          value={data.name || ""}
          onChange={(e) => set({ name: e.target.value })}
        />
        <div className="form-hint">
          Shows up in call logs and transcripts, so you can see where calls get stuck. Each one
          has to be different.
        </div>
      </div>

      {type !== "start" && type !== "global" && (
        <div className="form-group">
          <label className="form-label">What kind of step is this?</label>
          <select
            className="form-select"
            value={type}
            onChange={(e) => onChangeNodeType(selection.id, e.target.value as WorkflowNodeType)}
          >
            {TYPE_CHOICES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
          </select>
          <div className="form-hint">
            Picked the wrong one? Change it here instead of redrawing. Transfer and End finish the
            call, so any connections leaving this step will need removing.
          </div>
        </div>
      )}

      {type === "start" && (
        <>
          <PromptField
            label={<>First thing the agent says <span className="hint">optional</span></>}
            value={data.greeting || ""}
            placeholder="Hi, thanks for calling {{ business_name }}."
            rows={3}
            variables={availableVariables}
            hint="Spoken before the agent works anything out. Leave it empty and the call opens with whatever the first reply turns out to be."
            onChange={(greeting) => set({ greeting })}
          />
          <div className="form-group">
            <label className="form-label">
              Pause before speaking <span className="hint">optional</span>
            </label>
            <div className="wf-inline">
              <input
                className="form-input"
                type="number"
                min={0}
                step={100}
                value={data.delayed_start_ms ?? 0}
                onChange={(e) => set({ delayed_start_ms: Number(e.target.value) || 0 })}
              />
              <span className="form-hint" style={{ marginTop: 0 }}>milliseconds</span>
            </div>
            <div className="form-hint">
              For outbound calls, where speaking the instant the line opens clips the first word.
            </div>
          </div>
        </>
      )}

      {type === "global" ? (
        <PromptField
          label={<>Instructions for every step <span className="required">*</span></>}
          value={data.prompt || ""}
          placeholder="You are Alex, a warm and efficient receptionist for {{ business_name }}. Keep replies to one or two spoken sentences."
          rows={8}
          variables={availableVariables}
          hint="Who the agent is and how it should sound — added to the top of every step's own instructions. Anything that only applies to one step belongs on that step instead."
          onChange={(prompt) => set({ prompt })}
        />
      ) : (
        <PromptField
          label={<>What should the agent do here? <span className="required">*</span></>}
          value={data.prompt || ""}
          placeholder="Book an appointment. Get a date and time the caller wants, then book it."
          rows={6}
          variables={availableVariables}
          hint="Instructions for this step only. The always-applies step's instructions come first, on every step."
          onChange={(prompt) => set({ prompt })}
        />
      )}

      {(type === "agent" || type === "start") && (
        <>
          <div className="form-group">
            <label className="form-label">
              What can it do here? <span className="hint">nothing is ticked by default</span>
            </label>
            {agentTools.length === 0 ? (
              <div className="form-hint">
                This agent has no tools set up yet — add them on the <strong>Tools</strong> tab and
                they&apos;ll appear here.
              </div>
            ) : (
              agentTools.map((tool) => (
                <label key={tool} className="wf-check">
                  <input
                    type="checkbox"
                    checked={(data.tools || []).includes(tool)}
                    onChange={() => set({ tools: toggle(data.tools, tool) })}
                  />
                  <span>{tool}</span>
                </label>
              ))
            )}
            <div className="form-hint">
              A tool the agent can&apos;t reach here is one it can&apos;t use by mistake — that&apos;s
              the point of splitting the call into steps.
            </div>
          </div>

          <div className="form-group">
            <label className="form-label">Can it look things up here?</label>
            {knowledgeBases.length === 0 ? (
              <div className="form-hint">
                No knowledge bases attached — see the <strong>Knowledge Base</strong> tab.
              </div>
            ) : (
              knowledgeBases.map((kb) => (
                <label key={kb.id} className="wf-check">
                  <input
                    type="checkbox"
                    checked={(data.knowledge_base_ids || []).includes(kb.id)}
                    onChange={() => set({ knowledge_base_ids: toggle(data.knowledge_base_ids, kb.id) })}
                  />
                  <span>{kb.name}</span>
                </label>
              ))
            )}
            <div className="form-hint">Steps with nothing ticked skip the lookup entirely, which is faster.</div>
          </div>

          <ExtractionEditor
            value={data.extraction}
            onChange={(extraction: Extraction) => set({ extraction })}
          />
        </>
      )}

      {type === "transfer" && (
        <div className="form-group">
          <label className="form-label">
            Where should it transfer to? <span className="hint">optional</span>
          </label>
          <input
            className="form-input"
            value={data.transfer_destination || ""}
            placeholder="+15551234567 or sip:support@pbx.example.com"
            onChange={(e) => set({ transfer_destination: e.target.value || null })}
          />
          <div className="form-hint">
            Leave empty to use the number on the <strong>Escalation</strong> tab. That tab also
            decides <em>how</em> the transfer happens — if it&apos;s set to &quot;none&quot;,
            transfers from here will be refused.
          </div>
        </div>
      )}

      {type === "end" && (
        <div className="form-group">
          <label className="form-label">How did the call go?</label>
          <input
            className="form-input"
            list="wf-dispositions"
            value={data.disposition || ""}
            placeholder="qualified"
            onChange={(e) => set({ disposition: e.target.value || null })}
          />
          <datalist id="wf-dispositions">
            {DISPOSITIONS.map((d) => <option key={d} value={d} />)}
          </datalist>
          <div className="form-hint">
            Recorded on every call that ends here, so you can filter the calls list by outcome. Any
            word works; these are the usual ones.
          </div>
        </div>
      )}

      {type !== "start" && (
        <button className="btn btn-danger btn-sm" onClick={() => onDelete(selection)}>
          Delete this step
        </button>
      )}
    </div>
  );
}

/** Mirrors Edge.tool_name in libs/config_sdk/workflow.py — shown so an
 *  operator can see the collision the server would reject before they hit
 *  publish. */
function toToolName(label: string): string {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "…";
}
