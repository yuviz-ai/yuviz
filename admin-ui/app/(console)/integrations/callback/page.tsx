"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { applyConnectorPreset, completeOAuthCallback } from "@/lib/toolexecApi";
import { OAUTH_CONTEXT_KEY, OAuthContext } from "@/components/ConnectorsPanel";
import { callbackFailureMessage } from "@/lib/oauthCallback";
import { draftSetup, presetDraftKey, readPresetDraft } from "@/lib/connectorDraft";

// The provider sends the browser here with a single-use code in the URL. The
// URL is stripped before anything else runs so the code is never in history,
// and the meta tag keeps it out of Referer headers.
export default function OAuthCallbackPage() {
  const router = useRouter();
  const [failure, setFailure] = useState<string | null>(null);
  // The code is single use; React's dev double-mount must not redeem it twice.
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    const params = new URLSearchParams(window.location.search);
    window.history.replaceState(null, "", window.location.pathname);
    const code = params.get("code");
    const state = params.get("state");
    const raw = window.sessionStorage.getItem(OAUTH_CONTEXT_KEY);
    window.sessionStorage.removeItem(OAUTH_CONTEXT_KEY);
    const context: OAuthContext | null = raw ? JSON.parse(raw) : null;
    const redeemed =
      params.get("error") || !code || !state || !context
        ? Promise.reject(new Error("callback_unusable"))
        : completeOAuthCallback(context.tenantId, { state, code, accounts_server: params.get("accounts-server") }).then(
            async () => {
              const draft = readPresetDraft(window.sessionStorage, context.tenantId);
              if (draft && context.presetKey === draft.presetKey) {
                try {
                  await applyConnectorPreset(context.tenantId, draftSetup(draft));
                  window.sessionStorage.removeItem(presetDraftKey(context.tenantId));
                } catch {
                  // Restore the saved form on return if applying still fails.
                }
              }
              router.replace(context.returnTo);
            },
          );
    redeemed.catch(() => setFailure(callbackFailureMessage(params.get("error"))));
  }, [router]);

  return (
    <>
      <meta name="referrer" content="no-referrer" />
      <div className="card">
        {failure ? (
          <div className="card-body">
            <div className="error-banner">{failure}</div>
            <Link href="/integrations" className="btn btn-ghost btn-sm">
              Back to Connected Apps
            </Link>
          </div>
        ) : (
          <div className="empty-state">Finishing the connection…</div>
        )}
      </div>
    </>
  );
}
