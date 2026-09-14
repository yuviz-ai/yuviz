"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, bootstrap, getSetupStatus, isConsoleRole, login } from "@/lib/api";
import { setToken } from "@/lib/auth";

function EyeIcon({ off }: { off: boolean }) {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4">
      <path d="M1 8s2.5-4.5 7-4.5S15 8 15 8s-2.5 4.5-7 4.5S1 8 1 8z" />
      <circle cx="8" cy="8" r="2" />
      {off && <path d="M2.5 13.5l11-11" />}
    </svg>
  );
}

// Sign-in and first-run account creation on one screen. The "create your
// account" link only appears while /auth/setup-status reports setup_required
// — once a superadmin exists /auth/bootstrap 409s, so offering it would be a
// dead end.
export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"signin" | "create" | null>(null);
  const [setupRequired, setSetupRequired] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [reveal, setReveal] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Always lands on sign-in; the create form is one click away when the
  // status says no superadmin exists yet. A failed check just means no link.
  useEffect(() => {
    getSetupStatus()
      .then(({ setup_required }) => setSetupRequired(setup_required))
      .catch(() => {})
      .finally(() => setMode("signin"));
  }, []);

  const creating = mode === "create";

  const switchMode = (next: "signin" | "create") => {
    setMode(next);
    setError(null);
    setConfirm("");
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (creating) {
      // Mirrors schemas.py's BootstrapRequest; the backend still decides.
      if (password.length < 8) {
        setError("Password must be at least 8 characters.");
        return;
      }
      if (password !== confirm) {
        setError("Passwords do not match.");
        return;
      }
    }
    setSubmitting(true);
    setError(null);
    try {
      const result = creating ? await bootstrap(email, password) : await login(email, password);
      setToken(result.access_token);
      // `agent` has zero Config API surface at all (deps.py's CONSOLE_ROLES)
      // — every admin page 403s for it, so it lands on the standalone "not
      // for your role" screen instead. `supervisor` is also outside
      // CONSOLE_ROLES but DOES hold a grant — LIVE_CALLS_ROLES, exactly
      // /live-calls and its POST route (services/config/deps.py) — so it
      // gets its own landing page rather than being lumped in with agent's
      // dead end (lesson 22: the role must land somewhere it can use). A
      // `viewer` is a console role but can't create/edit tenants, so
      // /tenants (built around superadmin/admin actions) isn't a page they
      // can use either — Dashboard is read-only and works for them.
      if (result.user.role === "supervisor") router.push("/live-calls");
      else if (!isConsoleRole(result.user.role)) router.push("/no-access");
      else if (result.user.role === "viewer") router.push("/dashboard");
      else router.push("/tenants");
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  if (mode === null) return null;

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
          <div className="login-title">
            {creating ? "Create your administrator account" : "Sign in to your console"}
          </div>
          <div className="login-sub">
            {creating
              ? "This install has no users yet. The account you create here is the first superadmin — there are no default credentials."
              : "Manage agents, providers, and calls."}
          </div>
          {error && <div className="error-banner">{error}</div>}
          <form onSubmit={handleSubmit}>
            <div className="login-field">
              <label className="login-label">Email</label>
              <input
                className="login-input"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>
            <div className="login-field">
              <label className="login-label">Password</label>
              <div className="login-input-wrap">
                <input
                  className="login-input"
                  type={reveal ? "text" : "password"}
                  autoComplete={creating ? "new-password" : "current-password"}
                  minLength={creating ? 8 : undefined}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
                <button
                  type="button"
                  className="login-reveal"
                  onClick={() => setReveal((r) => !r)}
                  aria-label={reveal ? "Hide password" : "Show password"}
                  aria-pressed={reveal}
                  title={reveal ? "Hide password" : "Show password"}
                >
                  <EyeIcon off={reveal} />
                </button>
              </div>
            </div>
            {creating && (
              <div className="login-field">
                <label className="login-label">Confirm password</label>
                <input
                  className="login-input"
                  type={reveal ? "text" : "password"}
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  required
                />
              </div>
            )}
            <button className="login-btn" type="submit" disabled={submitting}>
              {submitting
                ? creating ? "Creating account…" : "Signing in…"
                : creating ? "Create account" : "Sign In"}
            </button>
          </form>
          {setupRequired && (
            <div className="login-alt">
              {creating ? (
                <>
                  Already have an account?{" "}
                  <button type="button" onClick={() => switchMode("signin")}>Sign in</button>
                </>
              ) : (
                <>
                  Don&apos;t have an account?{" "}
                  <button type="button" onClick={() => switchMode("create")}>Create your account</button>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
