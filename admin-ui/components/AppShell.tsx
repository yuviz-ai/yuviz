"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { getCurrentUser, isConsoleRole, listTenants, Tenant, User } from "@/lib/api";
import { clearToken, getToken } from "@/lib/auth";

// Shared with other pages' tenant pickers (e.g. Live Calls) so they stay in sync with the header.
export const ACTIVE_TENANT_STORAGE_KEY = "yuviz.activeTenantId";
/** Stored when a superadmin explicitly picks "All tenants" (distinct from nothing stored). */
export const ALL_TENANTS_SENTINEL = "__all__";

function tenantInitial(name: string): string {
  return (name.trim()[0] || "?").toUpperCase();
}

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
  "knowledge-bases": (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M2 3.5A1.5 1.5 0 013.5 2H8v11.5H3.5A1.5 1.5 0 012 12z" />
      <path d="M14 3.5A1.5 1.5 0 0012.5 2H8v11.5h4.5a1.5 1.5 0 001.5-1.5z" />
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
  "live-calls": (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="8" r="2" fill="currentColor" stroke="none" />
      <path d="M4.5 4.5a5 5 0 000 7M11.5 4.5a5 5 0 010 7" />
    </svg>
  ),
  campaigns: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M1.5 6.5v3L5 10.5V5.5L1.5 6.5z" />
      <path d="M5 5.5l8.5-3v11l-8.5-3" />
      <path d="M4 10.5l1 3.5h2l-.8-3" />
    </svg>
  ),
  telephony: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="1.5" y="9.5" width="13" height="5" rx="1" />
      <path d="M4 12h.01M6.5 12h.01M9 12h.01" />
      <path d="M8 8V5M8 5H4.5M8 5h3.5M4.5 5V2.5M11.5 5V2.5" />
    </svg>
  ),
  billing: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="2" y="1.5" width="12" height="13" rx="1.5" />
      <path d="M5 4.5h6M5 7.5h4M5 10.5h3" />
    </svg>
  ),
  settings: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 1.5v2M8 12.5v2M14.5 8h-2M3.5 8h-2M12.4 3.6l-1.4 1.4M5 11l-1.4 1.4M12.4 12.4L11 11M5 5L3.6 3.6" />
    </svg>
  ),
  docs: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M3 2.5h6.5L13 6v7.5H3z" />
      <path d="M9.5 2.5V6H13M5.5 8.5h5M5.5 11h3.5" />
    </svg>
  ),
};

const OVERVIEW_ITEMS = [{ href: "/dashboard", label: "Dashboard", icon: "dashboard" }];

const MANAGEMENT_ITEMS = [
  { href: "/tenants", label: "Accounts", icon: "accounts" },
  { href: "/agents", label: "Agent Studio", icon: "agents" },
  { href: "/workflows", label: "IVR Flows", icon: "workflows" },
  { href: "/knowledge-bases", label: "Knowledge Base", icon: "knowledge-bases" },
  { href: "/ai-voice", label: "AI & Voice", icon: "ai-voice" },
  // Provider configurations and every number on them, add to remove.
  { href: "/telephony", label: "Telephony", icon: "telephony" },
];

// Superadmin's cross-account user view; admins manage their team under
// Settings → Team members instead.
const USERS_ITEM = { href: "/users", label: "Users", icon: "users" };

const CALLING_ITEMS = [
  { href: "/calls", label: "Calls", icon: "calls" },
  { href: "/campaigns", label: "Campaigns", icon: "campaigns" },
  { href: "/live-calls", label: "Live Calls", icon: "live-calls" },
];

const PLATFORM_ITEMS = [
  { href: "/docs", label: "Guide", icon: "docs" },
  { href: "/settings", label: "Settings", icon: "settings" },
];

// superadmin/admin only. UI narrowing, not a security boundary (its endpoints allow any console role).
const BILLING_ITEM = { href: "/billing", label: "Billing & usage", icon: "billing" };

const ALL_ITEMS = [...OVERVIEW_ITEMS, ...MANAGEMENT_ITEMS, USERS_ITEM, ...CALLING_ITEMS, BILLING_ITEM, ...PLATFORM_ITEMS];

// One agent's config page is /agents/{tenant}/{agent} — second crumb for it.
const SETTINGS_CRUMBS = ["Agent Studio", "Configuration"];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [theme, setTheme] = useState<"dark" | "light">("light");
  const [search, setSearch] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  // Off-canvas nav on narrow screens; independent of the desktop `collapsed` rail.
  const [navOpen, setNavOpen] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [activeTenantId, setActiveTenantId] = useState<string | null>(null);
  const [tenantMenuOpen, setTenantMenuOpen] = useState(false);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setNavOpen(false);
  }, [pathname]);

  // Auth guard. /login and /invite skip it (invitees have no account); /no-access still needs a token.
  // Non-console roles go to /no-access, except supervisor which may reach /live-calls only.
  // authChecked stays false during a redirect so the page underneath never fetches.
  useEffect(() => {
    if (pathname === "/login" || pathname === "/invite") return;
    if (!getToken()) {
      router.push("/login");
      return;
    }
    getCurrentUser()
      .then((u) => {
        const supervisorOnItsOwnPage = u.role === "supervisor" && pathname.startsWith("/live-calls");
        if (!isConsoleRole(u.role) && !supervisorOnItsOwnPage && pathname !== "/no-access") {
          router.push("/no-access");
          return;
        }
        // Direct-URL guard matching the hidden nav item.
        if (u.role !== "superadmin" && pathname.startsWith("/tenants")) {
          router.push("/no-access");
          return;
        }
        // Direct-URL guard matching the hidden nav item.
        if (u.role !== "superadmin" && u.role !== "admin" && pathname.startsWith("/billing")) {
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

  // Tenant switcher is only for platform-scoped superadmins; other roles are bound to one tenant.
  // Resolves the stored selection against a tenant list (used on load and on change events).
  const resolveActiveTenantId = useCallback((ts: Tenant[]): string | null => {
    const stored = typeof window !== "undefined" ? window.localStorage.getItem(ACTIVE_TENANT_STORAGE_KEY) : null;
    // Missing or stale selection defaults to "All tenants", never ts[0].
    const found = stored && stored !== ALL_TENANTS_SENTINEL ? ts.find((t) => t.slug === stored) ?? null : null;
    return found?.id ?? null;
  }, []);

  useEffect(() => {
    if (user?.role !== "superadmin") return;
    listTenants()
      .then((ts) => {
        setTenants(ts);
        // Stores the tenant slug, not id: tenant-scoped routes take slugs.
        setActiveTenantId(resolveActiveTenantId(ts));
      })
      .catch(() => {
        // Non-fatal: the switcher simply doesn't render.
      });
  }, [user, resolveActiveTenantId]);

  // Other pages' pickers (e.g. Live Calls) change the selection without selectTenant(), so listen too.
  useEffect(() => {
    if (user?.role !== "superadmin") return;
    const onSwitch = () => setActiveTenantId(resolveActiveTenantId(tenants));
    window.addEventListener("yuviz:active-tenant", onSwitch);
    return () => window.removeEventListener("yuviz:active-tenant", onSwitch);
  }, [user, tenants, resolveActiveTenantId]);

  const activeTenant = tenants.find((t) => t.id === activeTenantId) ?? null;

  const selectTenant = (t: Tenant) => {
    setActiveTenantId(t.id);
    setTenantMenuOpen(false);
    try {
      window.localStorage.setItem(ACTIVE_TENANT_STORAGE_KEY, t.slug);
    } catch {
      // Private-mode/blocked storage: the selection just won't survive a
      // reload, which is a strictly worse-but-safe fallback, not a crash.
    }
    // Tell open pages to re-query. Without this the switcher only took
    // effect on the next full page load, which reads as it not working.
    window.dispatchEvent(new CustomEvent("yuviz:active-tenant", { detail: t.slug }));
  };

  const selectAllTenants = () => {
    setActiveTenantId(null);
    setTenantMenuOpen(false);
    try {
      window.localStorage.setItem(ACTIVE_TENANT_STORAGE_KEY, ALL_TENANTS_SENTINEL);
    } catch {
      // Same non-fatal fallback as selectTenant above.
    }
    window.dispatchEvent(new CustomEvent("yuviz:active-tenant", { detail: ALL_TENANTS_SENTINEL }));
  };

  const handleLogout = () => {
    clearToken();
    router.push("/login");
  };

  if (pathname === "/login" || pathname === "/invite" || pathname === "/no-access") return <>{children}</>;
  if (!authChecked) return null;

  // supervisor's only grant is LIVE_CALLS_ROLES, so it sees just Live Calls.
  const isSupervisor = user?.role === "supervisor";
  const canManageUsers = user?.role === "superadmin" || user?.role === "admin";
  const matches = (label: string) => label.toLowerCase().includes(search.trim().toLowerCase());
  const visibleOverview = isSupervisor ? [] : OVERVIEW_ITEMS.filter((item) => matches(item.label));
  // Accounts is superadmin-only (tenants.py enforces it server-side).
  const visibleManagement = isSupervisor
    ? []
    : MANAGEMENT_ITEMS.filter((item) => matches(item.label) && (item.href !== "/tenants" || user?.role === "superadmin"));
  const visibleUsers = user?.role === "superadmin" && matches(USERS_ITEM.label);
  const visibleCalling = isSupervisor
    ? CALLING_ITEMS.filter((item) => item.href === "/live-calls")
    : CALLING_ITEMS.filter((item) => matches(item.label));
  const visibleBilling = !isSupervisor && canManageUsers && matches(BILLING_ITEM.label);
  const visiblePlatform = isSupervisor ? [] : PLATFORM_ITEMS.filter((item) => matches(item.label));

  // Longest-prefix match, not first-match: /workflows/acme/reception must
  // resolve to "Agents", not a shorter unrelated prefix.
  const activeItem = [...ALL_ITEMS]
    .sort((a, b) => b.href.length - a.href.length)
    .find((item) => pathname.startsWith(item.href));
  const inAgentConfig = /^\/agents\/[^/]+\/[^/]+/.test(pathname);
  const crumbs = inAgentConfig ? SETTINGS_CRUMBS : [activeItem?.label ?? "Yuviz.ai"];

  return (
    <div className={`app-shell${navOpen ? " nav-open" : ""}`}>
      <button
        className="nav-scrim"
        aria-label="Close menu"
        onClick={() => setNavOpen(false)}
      />
      <aside className={`sidebar${collapsed ? " collapsed" : ""}`}>
        <div className="logo">
          <div className="logo-icon" aria-hidden="true">
            <span className="logo-bar" />
            <span className="logo-bar" />
            <span className="logo-bar" />
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
          {(visiblePlatform.length > 0 || visibleBilling) && (
            <>
              <div className="nav-section">Platform</div>
              {visibleBilling && (
                <Link
                  href={BILLING_ITEM.href}
                  className={`nav-item${BILLING_ITEM.href === activeItem?.href ? " active" : ""}`}
                >
                  {ICONS[BILLING_ITEM.icon]}
                  <span className="nav-label">{BILLING_ITEM.label}</span>
                </Link>
              )}
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
          {visibleOverview.length === 0 && visibleManagement.length === 0 && !visibleUsers && visibleCalling.length === 0 && !visibleBilling && visiblePlatform.length === 0 && (
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
          <button
            className="nav-toggle"
            aria-label="Menu"
            aria-expanded={navOpen}
            onClick={() => setNavOpen((o) => !o)}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6">
              <path d="M2 4h12M2 8h12M2 12h12" />
            </svg>
          </button>
          {user?.role === "superadmin" ? (
            tenants.length > 0 && (
              <div className="tenant-switch">
                <button
                  className="tenant-switch-btn"
                  onClick={() => setTenantMenuOpen((o) => !o)}
                  aria-expanded={tenantMenuOpen}
                  title="Switch tenant"
                >
                  <span className="tenant-switch-mark">
                    {activeTenant ? tenantInitial(activeTenant.name) : "—"}
                  </span>
                  <span className="tenant-switch-label">
                    <span className="tenant-switch-name">{activeTenant?.name ?? "All tenants"}</span>
                    <span className="tenant-switch-id">{activeTenant?.slug ?? "platform"}</span>
                  </span>
                  <span className="tenant-switch-caret">▾</span>
                </button>
                {tenantMenuOpen && (
                  <>
                    <div className="wf-menu-scrim" onClick={() => setTenantMenuOpen(false)} />
                    <div className="tenant-switch-menu">
                      <div className="tenant-switch-menu-label">Switch tenant</div>
                      <button
                        className={`tenant-switch-row${activeTenantId === null ? " active" : ""}`}
                        onClick={selectAllTenants}
                      >
                        <span className="tenant-switch-mark">∀</span>
                        <span className="tenant-switch-row-name">
                          All tenants
                          <br />
                          <span className="tenant-switch-row-meta">every account, unfiltered</span>
                        </span>
                        {activeTenantId === null && <span className="tenant-switch-check">✓</span>}
                      </button>
                      {tenants.map((t) => (
                        <button
                          key={t.id}
                          className={`tenant-switch-row${t.id === activeTenantId ? " active" : ""}`}
                          onClick={() => selectTenant(t)}
                        >
                          <span className="tenant-switch-mark">{tenantInitial(t.name)}</span>
                          <span className="tenant-switch-row-name">
                            {t.name}
                            <br />
                            <span className="tenant-switch-row-meta">{t.slug}</span>
                          </span>
                          {t.id === activeTenantId && <span className="tenant-switch-check">✓</span>}
                        </button>
                      ))}
                    </div>
                  </>
                )}
              </div>
            )
          ) : null}
          {user?.role === "superadmin" && tenants.length > 0 && <div className="topbar-divider" />}
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
