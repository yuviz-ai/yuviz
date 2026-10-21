"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { applyConnectorPreset, completeOAuthCallback } from "@/lib/toolexecApi";
import { OAUTH_CONTEXT_KEY, OAuthContext } from "@/components/ConnectorsPanel";
import { draftSetup, presetDraftKey, readPresetDraft } from "@/lib/connectorDraft";

// Whatever went wrong, the page says the same thing: the provider's error text
// and the server's reason are never shown (the server answers every failure
// identically too).
const FAILURE_MESSAGE = "We could not complete the connection. Go back to Connected Apps and try again.";

// The provider sends the browser here with a single-use code in the URL. The
// URL is stripped before anything else runs so the code is never in history,
// and the meta tag keeps it out of Referer headers.
export default function OAuthCallbackPage() {
  const router = useRouter();
  const [failed, setFailed] = useState(false);
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
    redeemed.catch(() => setFailed(true));
  }, [router]);

  return (
    <>
      <meta name="referrer" content="no-referrer" />
      <div className="card">
        {failed ? (
          <div className="card-body">
            <div className="error-banner">{FAILURE_MESSAGE}</div>
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
