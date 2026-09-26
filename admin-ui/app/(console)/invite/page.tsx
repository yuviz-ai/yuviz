"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { acceptInvite, ApiError, getInvite, InviteAcceptInfo } from "@/lib/api";

// Public accept-invite page. The token lives only in the URL fragment
// (`/invite#<token>`) — fragments never reach a server, so this is the one
// place in the Admin UI that must read location.hash instead of a query
// param, and must never put the token in a URL/query string on its way out
// (see design doc's "Token placement"). AppShell.tsx treats this route as
// standalone/unguarded, the same as /login.
export default function InvitePage() {
  const router = useRouter();
  const [token, setToken] = useState<string | null>(null);
  const [info, setInfo] = useState<InviteAcceptInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState(false);

  useEffect(() => {
    const hash = window.location.hash.replace(/^#/, "");
    if (!hash) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setLoadError("This invite link is invalid.");
      setLoading(false);
      return;
    }
    setToken(hash);
    getInvite(hash)
      .then(setInfo)
      // Every classification (expired/revoked/already used/no-longer-valid/
      // not-found) reaches here as an ApiError with its own `detail` — just
      // surface it verbatim; none of them name an account or tenant.
      .catch((e) => setLoadError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token) return;
    if (password.length < 8) {
      setFormError("Password must be at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setFormError("Passwords do not match.");
      return;
    }
    setSubmitting(true);
    setFormError(null);
    try {
      await acceptInvite(token, password);
      setAccepted(true);
    } catch (e) {
      setFormError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-logo">
          <div className="login-logo-icon">
            <svg width="20" height="18" viewBox="0 0 18 16" fill="none">
              <rect x="0" y="6" width="2.5" height="4" rx="1.25" fill="currentColor" />
              <rect x="3.75" y="3.5" width="2.5" height="9" rx="1.25" fill="currentColor" />
              <rect x="7.5" y="0" width="3" height="16" rx="1.5" fill="currentColor" />
              <rect x="11.75" y="3.5" width="2.5" height="9" rx="1.25" fill="currentColor" />
              <rect x="15.5" y="5.5" width="2.5" height="5" rx="1.25" fill="currentColor" />
            </svg>
          </div>
          <div className="login-logo-text">
            Yuviz<span>.ai</span>
          </div>
        </div>
        <div className="login-box">
          {loading ? (
            <div className="login-sub">Checking your invite…</div>
          ) : accepted ? (
            <>
              <div className="login-title">Account created</div>
              <div className="login-sub">You can now sign in with your new password.</div>
              <button className="login-btn" type="button" onClick={() => router.push("/login")}>
                Go to sign in
              </button>
            </>
          ) : loadError ? (
            <>
              <div className="login-title">This invite isn&apos;t valid</div>
              <div className="login-sub">{loadError}</div>
            </>
          ) : (
            <>
              <div className="login-title">Set your password</div>
              <div className="login-sub">
                {info?.email} is invited as {info?.role} on {info?.tenant_name}.
              </div>
              {formError && <div className="error-banner">{formError}</div>}
              <form onSubmit={handleSubmit}>
                <div className="login-field">
                  <label className="login-label">Password</label>
                  <input
                    className="login-input"
                    type="password"
                    autoComplete="new-password"
                    minLength={8}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                  />
                </div>
                <div className="login-field">
                  <label className="login-label">Confirm password</label>
                  <input
                    className="login-input"
                    type="password"
                    autoComplete="new-password"
                    value={confirm}
                    onChange={(e) => setConfirm(e.target.value)}
                    required
                  />
                </div>
                <button className="login-btn" type="submit" disabled={submitting}>
                  {submitting ? "Creating account…" : "Create account"}
                </button>
              </form>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
