"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getCurrentUser, User } from "@/lib/api";
import { clearToken } from "@/lib/auth";

// Standalone landing page for the "agent" role, which has no Config API access (see CONSOLE_ROLES).
export default function NoAccessPage() {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);

  useEffect(() => {
    // Uses /auth/me, not console-gated (get_authenticated_user) — works
    // for every authenticated role, including the ones this page is for.
    getCurrentUser().then(setUser).catch(() => {});
  }, []);

  const handleSignOut = () => {
    clearToken();
    router.push("/login");
  };

  const message = user
    ? `You're signed in as ${user.email} (role: ${user.role}). This admin console is for superadmin, admin, and viewer accounts only, so there's nothing here for you yet. Sign out below, or ask a superadmin or admin on your team if that seems wrong.`
    : "This admin console is for superadmin, admin, and viewer accounts only. Sign out below, or ask a superadmin or admin on your team if that seems wrong.";

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
          <div className="login-title">This console isn&apos;t for your role</div>
          <div className="login-sub">{message}</div>
          <button className="login-btn" type="button" onClick={handleSignOut}>
            Sign out
          </button>
        </div>
      </div>
    </div>
  );
}
