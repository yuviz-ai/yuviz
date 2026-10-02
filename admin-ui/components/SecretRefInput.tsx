"use client";

import { useState } from "react";

// Keys (encrypted server-side to enc:...) and refs ("env:FOO") must not be conflated, or a key
// gets stored in plaintext. Pasting a key is the default.

const isStored = (v: string) => v.startsWith("enc:");

export function SecretRefInput({
  value,
  onChange,
  placeholder,
  disabled = false,
  canEncrypt = false,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  /** True only if the caller sends the value via secretPayload(); otherwise it'd be stored in plaintext. */
  canEncrypt?: boolean;
}) {
  const [visible, setVisible] = useState(false);
  // A saved key stays hidden until Replace, so editing anything else on the
  // provider doesn't mean retyping the credential.
  const [replacing, setReplacing] = useState(false);
  const [mode, setMode] = useState<"key" | "ref">(
    canEncrypt && (!value || isStored(value)) ? "key" : "ref",
  );

  // Only clear on the first keystroke, so clicking Replace and then saving
  // something else leaves the stored credential alone.
  const replace = (v: string) => onChange(v);

  const trimmed = value.trim();
  let warning: string | null = null;
  if (trimmed && !isStored(value)) {
    if (mode === "key" && /\s/.test(trimmed)) {
      // Likely a pasted header line rather than the bare token.
      warning = 'This looks like it includes extra text, not just the key — e.g. paste "sk-..." alone, not "Authorization: Bearer sk-...".';
    } else if (mode === "ref" && !/^(env|k8s):\S+$/i.test(trimmed)) {
      warning = 'This doesn\'t look like a pointer — use env:VAR_NAME or k8s:namespace/secret.';
    }
  }

  if (isStored(value) && !replacing) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span className="badge green">Key saved</span>
        <span className="form-hint" style={{ marginTop: 0 }}>
          Encrypted. It can&apos;t be shown again.
        </span>
        <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={disabled}
            onClick={() => {
              if (confirm("Replace the saved key? The old one is discarded as soon as you save the new one.")) {
                setReplacing(true);
              }
            }}
          >
            Replace
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={disabled}
            onClick={() => {
              if (confirm("Remove this key? Whatever uses it will stop working until you add a replacement and save.")) {
                onChange("");
              }
            }}
          >
            Remove
          </button>
        </span>
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: "flex", gap: 8 }}>
        <input
          className="form-input"
          style={{ fontFamily: "var(--mono)" }}
          type={visible ? "text" : "password"}
          value={isStored(value) ? "" : value}
          onChange={(e) => replace(e.target.value)}
          placeholder={
            placeholder ?? (mode === "key" ? "Paste the API key" : "env:MY_API_KEY")
          }
          disabled={disabled}
          autoComplete="off"
        />
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => setVisible((v) => !v)}
          disabled={disabled}
        >
          {visible ? "Hide" : "Show"}
        </button>
      </div>
      {warning && (
        <div className="form-hint" style={{ color: "var(--amber)" }}>
          {warning}
        </div>
      )}
      <div className="form-hint">
        {mode === "key" && canEncrypt ? (
          <>
            Paste the key from your provider. It&apos;s encrypted before it&apos;s stored, and
            never shown again.{" "}
            <button type="button" className="wf-linkish" onClick={() => { setMode("ref"); onChange(""); }}>
              Use an environment variable instead
            </button>
          </>
        ) : (
          <>
            Point at a secret provisioned elsewhere — <code>env:VAR_NAME</code>{" "}
            or <code>k8s:namespace/secret</code>. The variable has to
            be set on the conversation service.{" "}
            {canEncrypt && (
              <button type="button" className="wf-linkish" onClick={() => { setMode("key"); onChange(""); }}>
                Paste the key instead
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** A pointer goes to api_key_ref verbatim; anything else is a credential and
 *  goes to api_key for the server to encrypt. */
export function secretPayload(
  value: string,
  original = "",
): { api_key_ref?: string; api_key?: string } {
  const v = value.trim();
  // Empty clears only when there was something to clear (the Remove button);
  // otherwise the field is untouched and must not be sent.
  if (!v) return original ? { api_key_ref: "" } : {};
  // Any scheme-shaped value (even unknown/miscased) is a ref so the server can reject it clearly.
  // No-whitespace rule excludes pasted header lines like "Authorization: Bearer sk-...".
  if (/^[A-Za-z0-9_]+:\S+$/.test(v)) return { api_key_ref: v };
  return { api_key: v };
}
