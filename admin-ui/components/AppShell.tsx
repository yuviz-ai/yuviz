"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { getCurrentUser, isConsoleRole, User } from "@/lib/api";
import { clearToken, getToken } from "@/lib/auth";

// Icons match the original "Yuviz.ai — Admin Console" artifact's nav icon
// set exactly where that nav item existed there (Accounts/Agents/Phone
// Numbers/Settings). Profile/Sessions/Security stay panels inside the
// single Settings page (its own internal "Your Account" secondary nav —
// see app/settings/page.tsx). Speech Services/Language Model/Embeddings
// were promoted OUT of Settings into their own top-level "AI & Voice" page
// (2026-07-30) — same standing as Agents/Phone Numbers, not buried in a
// secondary settings nav.
const ICONS: Record<string, React.ReactNode> = {
  dashboard: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="1.5" y="1.5" width="6" height="6" rx="1" />
      <rect x="8.5" y="1.5" width="6" height="3.5" rx="1" />
      <rect x="8.5" y="7" width="6" height="7.5" rx="1" />
      <rect x="1.5" y="9.5" width="6" height="5" rx="1" />
    </svg>
  ),
  accounts: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="1" y="6" width="14" height="9" rx="1" />
      <path d="M5 6V4a3 3 0 016 0v2" />
    </svg>
  ),
  users: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="6" cy="5" r="2.3" />
      <path d="M1.5 14c0-2.76 2.02-4.5 4.5-4.5s4.5 1.74 4.5 4.5" />
      <circle cx="12" cy="4.5" r="1.8" />
      <path d="M10.2 9.7c1.86.3 3.3 1.8 3.3 4.3" />
    </svg>
  ),
  workflows: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="5.5" y="1" width="5" height="3.5" rx="1" />
      <rect x="1" y="11.5" width="5" height="3.5" rx="1" />
      <rect x="10" y="11.5" width="5" height="3.5" rx="1" />
      <path d="M8 4.5v2.5M8 7h-4.5v4.5M8 7h4.5v4.5" />
    </svg>
  ),
  agents: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="5" r="3" />
      <path d="M2 14c0-3.314 2.686-5 6-5s6 1.686 6 5" />
    </svg>
  ),
  "phone-numbers": (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M3 2h3l1.5 4-2 1.5a10 10 0 004.5 4.5L11.5 10l4 1.5v3a2 2 0 01-2 2C7.5 16.5 -0.5 8.5 1 3a2 2 0 012-1z" />
    </svg>
  ),
  "ai-voice": (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M8 1.5a2.5 2.5 0 012.5 2.5v4a2.5 2.5 0 01-5 0V4A2.5 2.5 0 018 1.5z" />
      <path d="M3.5 7.5V8a4.5 4.5 0 009 0v-.5" />
      <path d="M8 12.5v2M5.5 14.5h5" />
    </svg>
  ),
  calls: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M2 2h12v9H9l-3 3v-3H2z" />
    </svg>
  ),
  campaigns: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M1.5 6.5v3L5 10.5V5.5L1.5 6.5z" />
      <path d="M5 5.5l8.5-3v11l-8.5-3" />
      <path d="M4 10.5l1 3.5h2l-.8-3" />
    </svg>
  ),
  settings: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 1.5v2M8 12.5v2M14.5 8h-2M3.5 8h-2M12.4 3.6l-1.4 1.4M5 11l-1.4 1.4M12.4 12.4L11 11M5 5L3.6 3.6" />
    </svg>
  ),
};

const OVERVIEW_ITEMS = [{ href: "/dashboard", label: "Dashboard", icon: "dashboard" }];

const MANAGEMENT_ITEMS = [
  { href: "/tenants", label: "Accounts", icon: "accounts" },
  { href: "/workflows", label: "Agents", icon: "workflows" },
  { href: "/ai-voice", label: "AI & Voice", icon: "ai-voice" },
  { href: "/phone-numbers", label: "Phone Numbers", icon: "phone-numbers" },
];

// Invite-based onboarding is a superadmin/admin surface only (matches
// require_role("superadmin", "admin") on services/config/routers/invites.py's
// admin routes) — everyone else never sees the nav item at all.
const USERS_ITEM = { href: "/users", label: "Users", icon: "users" };

const CALLING_ITEMS = [
  { href: "/calls", label: "Calls", icon: "calls" },
  { href: "/campaigns", label: "Campaigns", icon: "campaigns" },
];

const PLATFORM_ITEMS = [{ href: "/settings", label: "Settings", icon: "settings" }];

const ALL_ITEMS = [...OVERVIEW_ITEMS, ...MANAGEMENT_ITEMS, USERS_ITEM, ...CALLING_ITEMS, ...PLATFORM_ITEMS];

// Agent settings live under /workflows/.../settings — second crumb for that sub-route.
const SETTINGS_CRUMBS = ["Agents", "Settings"];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [search, setSearch] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [authChecked, setAuthChecked] = useState(false);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  // Auth guard: /login and /invite render standalone (no sidebar, nothing
  // to guard — see the early return below). /invite hosts invite acceptance
  // for someone who, by definition, has no account yet — it must not bounce
  // to /login the way every other route does. /no-access also renders
  // standalone (below) but still needs a token to know who's asking, so it
  // does NOT skip the guard here the way login/invite do.
  // Every other route requires a token; a missing one redirects immediately,
  // a present-but-invalid/expired one is caught by getCurrentUser() itself
  // (api.ts's request() already redirects to /login on any 401, so this only
  // needs to handle "no token at all").
  //
  // A token alone isn't enough: login/page.tsx sends non-console roles
  // (supervisor/agent — see isConsoleRole) to /no-access, but that's only
  // enforced at login time. Without re-checking here, a bookmark or a
  // refresh on any admin URL renders the full sidebar for a role with zero
  // Config API surface, so the page's own fetches 403 into an error banner
  // instead (lesson 22). authChecked stays false while a redirect is in
  // flight so the page underneath never gets to render its own fetches.
  useEffect(() => {
    if (pathname === "/login" || pathname === "/invite") return;
    if (!getToken()) {
      router.push("/login");
      return;
    }
    getCurrentUser()
      .then((u) => {
        if (!isConsoleRole(u.role) && pathname !== "/no-access") {
          router.push("/no-access");
          return;
        }
        setUser(u);
        setAuthChecked(true);
      })
      .catch(() => {
        // api.ts's request() already redirects to /login on 401; nothing
        // extra to do here.
        setAuthChecked(true);
      });
  }, [pathname, router]);

  const handleLogout = () => {
    clearToken();
    router.push("/login");
  };

  if (pathname === "/login" || pathname === "/invite" || pathname === "/no-access") return <>{children}</>;
  if (!authChecked) return null;

  const canManageUsers = user?.role === "superadmin" || user?.role === "admin";
  const matches = (label: string) => label.toLowerCase().includes(search.trim().toLowerCase());
  const visibleOverview = OVERVIEW_ITEMS.filter((item) => matches(item.label));
  const visibleManagement = MANAGEMENT_ITEMS.filter((item) => matches(item.label));
  const visibleUsers = canManageUsers && matches(USERS_ITEM.label);
  const visibleCalling = CALLING_ITEMS.filter((item) => matches(item.label));
  const visiblePlatform = PLATFORM_ITEMS.filter((item) => matches(item.label));

  // Longest-prefix match, not first-match: /workflows/acme/reception must
  // resolve to "Agents", not a shorter unrelated prefix.
  const activeItem = [...ALL_ITEMS]
    .sort((a, b) => b.href.length - a.href.length)
    .find((item) => pathname.startsWith(item.href));
  const inAgentSettings = /^\/workflows\/[^/]+\/[^/]+\/settings/.test(pathname);
  const crumbs = inAgentSettings ? SETTINGS_CRUMBS : [activeItem?.label ?? "Yuviz.ai"];

  return (
    <div className="app-shell">
      <aside className={`sidebar${collapsed ? " collapsed" : ""}`}>
        <div className="logo">
          <div className="logo-icon">
            <svg width="18" height="16" viewBox="0 0 18 16" fill="none">
              <rect x="0" y="6" width="2.5" height="4" rx="1.25" fill="currentColor" />
              <rect x="3.75" y="3.5" width="2.5" height="9" rx="1.25" fill="currentColor" />
              <rect x="7.5" y="0" width="3" height="16" rx="1.5" fill="currentColor" />
              <rect x="11.75" y="3.5" width="2.5" height="9" rx="1.25" fill="currentColor" />
              <rect x="15.5" y="5.5" width="2.5" height="5" rx="1.25" fill="currentColor" />
            </svg>
          </div>
          <div className="logo-text">
            Yuviz<span>.ai</span>
          </div>
          <button
            className="sidebar-collapse-btn"
            onClick={() => setCollapsed((c) => !c)}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M10 3l-5 5 5 5" />
            </svg>
          </button>
        </div>

        <div className="sidebar-search">
          <input
            type="text"
            placeholder="Search pages"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        <nav className="nav">
          {visibleOverview.length > 0 && (
            <>
              <div className="nav-section">Overview</div>
              {visibleOverview.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`nav-item${item.href === activeItem?.href ? " active" : ""}`}
                >
                  {ICONS[item.icon]}
                  <span className="nav-label">{item.label}</span>
                </Link>
              ))}
            </>
          )}
          {(visibleManagement.length > 0 || visibleUsers) && (
            <>
              <div className="nav-section">Management</div>
              {visibleManagement.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`nav-item${item.href === activeItem?.href ? " active" : ""}`}
                >
                  {ICONS[item.icon]}
                  <span className="nav-label">{item.label}</span>
                </Link>
              ))}
              {visibleUsers && (
                <Link
                  href={USERS_ITEM.href}
                  className={`nav-item${USERS_ITEM.href === activeItem?.href ? " active" : ""}`}
                >
                  {ICONS[USERS_ITEM.icon]}
                  <span className="nav-label">{USERS_ITEM.label}</span>
                </Link>
              )}
            </>
          )}
          {visibleCalling.length > 0 && (
            <>
              <div className="nav-section">Calling</div>
              {visibleCalling.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`nav-item${item.href === activeItem?.href ? " active" : ""}`}
                >
                  {ICONS[item.icon]}
                  <span className="nav-label">{item.label}</span>
                </Link>
              ))}
            </>
          )}
          {visiblePlatform.length > 0 && (
            <>
              <div className="nav-section">Platform</div>
              {visiblePlatform.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`nav-item${item.href === activeItem?.href ? " active" : ""}`}
                >
                  {ICONS[item.icon]}
                  <span className="nav-label">{item.label}</span>
                </Link>
              ))}
            </>
          )}
          {visibleOverview.length === 0 && visibleManagement.length === 0 && !visibleUsers && visibleCalling.length === 0 && visiblePlatform.length === 0 && (
            <div style={{ padding: "12px 10px", fontSize: ".76rem", color: "var(--text-3)" }}>No pages match &quot;{search}&quot;</div>
          )}
        </nav>

        <div className="sidebar-user">
          <div className="user-avatar">
            {(user?.email || "?").slice(0, 2).toUpperCase()}
          </div>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div className="user-name">{user?.email ?? "…"}</div>
            <div className="user-role">{user?.role ?? ""}</div>
          </div>
          <button
            className="sidebar-collapse-btn"
            onClick={handleLogout}
            title="Sign out"
            style={{ flexShrink: 0 }}
          >
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" width="12" height="12">
              <path d="M6 2H3a1 1 0 00-1 1v10a1 1 0 001 1h3M11 11l3-3-3-3M14 8H6" />
            </svg>
          </button>
        </div>
      </aside>

      <div className="main">
        <div className="topbar">
          <span className="topbar-title">
            <span style={{ color: "var(--text-3)" }}>Yuviz</span>
            {crumbs.map((crumb) => (
              <span key={crumb}>
                <span style={{ color: "var(--text-3)", margin: "0 6px" }}>›</span>
                {crumb}
              </span>
            ))}
          </span>
          <div className="topbar-actions">
            <button
              className="theme-toggle"
              onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
              title="Toggle theme"
            >
              {theme === "dark" ? "🌙" : "☀️"}
            </button>
          </div>
        </div>
        <div className="content">{children}</div>
      </div>
    </div>
  );
}
