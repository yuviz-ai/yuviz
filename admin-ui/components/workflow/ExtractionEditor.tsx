"use client";

// The variable list on an agent node — what this stage should capture from
// what the caller said (docs/workflow.md §5.8). Extraction runs as a
// background LLM pass when the call LEAVES this node, so what's declared
// here is scoped to this stage's slice of the conversation.

import type { Extraction, ExtractionVariable } from "@/lib/workflowApi";

const EMPTY: Extraction = { enabled: false, prompt: "", variables: [] };

export function ExtractionEditor({
  value, onChange,
}: { value: Extraction | undefined; onChange: (e: Extraction) => void }) {
  const extraction = value ?? EMPTY;

  const setVariable = (i: number, patch: Partial<ExtractionVariable>) => {
    const variables = extraction.variables.map((v, j) => (i === j ? { ...v, ...patch } : v));
    onChange({ ...extraction, variables });
  };

  return (
    <div className="wf-extraction">
      <label className="form-label" style={{ justifyContent: "space-between" }}>
        <span>Extract variables</span>
        <span className="toggle-switch">
          <input
            type="checkbox"
            checked={extraction.enabled}
            onChange={(e) => onChange({ ...extraction, enabled: e.target.checked })}
          />
          <span className="toggle-slider" />
        </span>
      </label>

      {extraction.enabled && (
        <>
          <div className="form-group">
            <label className="form-label">
              Instruction <span className="hint">optional</span>
            </label>
            <textarea
              className="form-textarea"
              style={{ minHeight: 52 }}
              value={extraction.prompt}
              placeholder="Only capture what the caller explicitly said."
              onChange={(e) => onChange({ ...extraction, prompt: e.target.value })}
            />
          </div>

          {extraction.variables.map((v, i) => (
            <div key={i} className="wf-variable">
              <div className="form-row">
                <div className="form-group" style={{ flex: 2 }}>
                  <input
                    className="form-input"
                    placeholder="variable_name"
                    value={v.name}
                    onChange={(e) => setVariable(i, { name: e.target.value })}
                  />
                </div>
                <div className="form-group">
                  <select
                    className="form-select"
                    value={v.type}
                    onChange={(e) => setVariable(i, { type: e.target.value as ExtractionVariable["type"] })}
                  >
                    <option value="string">string</option>
                    <option value="number">number</option>
                    <option value="boolean">boolean</option>
                  </select>
                </div>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  style={{ height: 32 }}
                  onClick={() =>
                    onChange({ ...extraction, variables: extraction.variables.filter((_, j) => j !== i) })
                  }
                >
                  ✕
                </button>
              </div>
              <input
                className="form-input"
                placeholder="What is this? e.g. Their policy number."
                value={v.prompt}
                onChange={(e) => setVariable(i, { prompt: e.target.value })}
              />
              <div className="form-hint">
                Reference it later as <code>{`{{ ${v.name || "name"} }}`}</code>
              </div>
            </div>
          ))}

          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() =>
              onChange({
                ...extraction,
                variables: [...extraction.variables, { name: "", type: "string", prompt: "" }],
              })
            }
          >
            + Variable
          </button>
        </>
      )}
    </div>
  );
}
