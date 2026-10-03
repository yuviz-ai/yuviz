"use client";

// The optional "documents" part of the Easy business step: tick collections
// the account already has, or choose .txt/.md files to upload. All text comes
// from lib/easyCopy.

import { useState } from "react";
import Link from "next/link";
import { ACCEPTED_DOC_ACCEPT, rejectionReasonFor } from "@/components/AddSourceModal";
import { easyCopy } from "@/lib/easyCopy";
import type { KnowledgeBase } from "@/lib/knowledgeApi";

const KNOWLEDGE_HREF = "/knowledge-bases";

export function EasyKnowledgePicker({
  collections,
  ticked,
  onToggle,
  canUpload,
  files,
  onAddFiles,
  onRemoveFile,
}: {
  collections: KnowledgeBase[];
  ticked: string[];
  onToggle: (id: string) => void;
  canUpload: boolean;
  files: File[];
  onAddFiles: (files: File[]) => void;
  onRemoveFile: (index: number) => void;
}) {
  const [skippedType, setSkippedType] = useState(false);

  const handleFiles = (chosen: FileList | null) => {
    const all = Array.from(chosen ?? []);
    const accepted = all.filter((f) => rejectionReasonFor(f) === null);
    setSkippedType(accepted.length < all.length);
    onAddFiles(accepted);
  };

  return (
    <div className="form-group" style={{ marginTop: 14, marginBottom: 0 }}>
      <div className="form-label">{easyCopy.documentsLabel}</div>
      <div className="form-hint">{easyCopy.documentsHint}</div>
      {collections.length > 0 && (
        <fieldset style={{ border: 0, padding: 0, margin: "8px 0 0" }}>
          <legend className="form-hint">{easyCopy.collectionsLabel}</legend>
          {collections.map((kb) => (
            <label key={kb.id} style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input type="checkbox" checked={ticked.includes(kb.id)} onChange={() => onToggle(kb.id)} />
              {kb.name}
            </label>
          ))}
        </fieldset>
      )}
      {canUpload ? (
        <div style={{ marginTop: 8 }}>
          <label className="form-label" htmlFor="easy-files">
            {easyCopy.uploadFilesLabel} <span className="hint">{easyCopy.uploadFilesHint}</span>
          </label>
          <input
            id="easy-files"
            type="file"
            multiple
            accept={ACCEPTED_DOC_ACCEPT}
            value=""
            onChange={(e) => handleFiles(e.target.files)}
          />
          {skippedType && <div role="alert" className="error-banner" style={{ marginTop: 6 }}>{easyCopy.wrongFileType}</div>}
          {files.length > 0 && (
            <ul aria-label={easyCopy.filesChosenLabel} style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {files.map((f, i) => (
                <li key={`${f.name}-${i}`}>
                  {f.name}{" "}
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => onRemoveFile(i)}>
                    {easyCopy.removeFile}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <div className="form-hint" style={{ marginTop: 8 }}>
          {easyCopy.uploadNeedsSetup} <Link href={KNOWLEDGE_HREF}>{easyCopy.uploadNeedsSetupLink}</Link>
        </div>
      )}
    </div>
  );
}
