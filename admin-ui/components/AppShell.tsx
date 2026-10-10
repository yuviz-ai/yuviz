"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Moon, Sun } from "lucide-react";
import { getCurrentUser, isConsoleRole, listAllUsageTrend, listTenants, Tenant, User } from "@/lib/api";
import { clearToken, getToken } from "@/lib/auth";
import { clearAllAgentDrafts } from "@/lib/agentDraft";

// Shared with other pages' tenant pickers (e.g. Live Calls) so they stay in sync with the header.
export const ACTIVE_TENANT_STORAGE_KEY = "yuviz.activeTenantId";
/** Stored when a superadmin explicitly picks "All tenants" (distinct from nothing stored). */
export const ALL_TENANTS_SENTINEL = "__all__";

function tenantInitial(name: string): string {
  return (name.trim()[0] || "?").toUpperCase();
}

function userInitials(email: string): string {
  return email.slice(0, 2).toUpperCase();
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
  telephony: (
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
  integrations: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M5.5 2v3M10.5 2v3M4 5h8v3a4 4 0 0 1-8 0zM8 12v2.5" />
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
  clock: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="8" r="6.5" />
      <path d="M8 5v3.5l2 1.5" />
    </svg>
  ),
};

type NavItem = { href: string; label: string; icon: string };

const OVERVIEW_ITEMS: NavItem[] = [{ href: "/dashboard", label: "Dashboard", icon: "dashboard" }];

// Accounts and AI & Voice are superadmin-only here; admins reach AI & Voice under Settings.
const BUILD_ITEMS: NavItem[] = [
  { href: "/tenants", label: "Accounts", icon: "accounts" },
  { href: "/agents", label: "Agents", icon: "agents" },
  { href: "/workflows", label: "Call Flows", icon: "workflows" },
  { href: "/knowledge-bases", label: "Knowledge", icon: "knowledge-bases" },
  { href: "/ai-voice", label: "AI & Voice", icon: "ai-voice" },
  { href: "/telephony", label: "Phone Numbers", icon: "telephony" },
  { href: "/integrations", label: "Connected Apps", icon: "integrations" },
];
const SUPERADMIN_ONLY = new Set(["/tenants", "/ai-voice", "/users", "/live-calls"]);

// Superadmin's cross-account user view; admins manage their team under Settings → Team members.
const USERS_ITEM: NavItem = { href: "/users", label: "Users", icon: "users" };

const CALLING_ITEMS: NavItem[] = [
  { href: "/calls", label: "Calls", icon: "calls" },
  { href: "/campaigns", label: "Campaigns", icon: "campaigns" },
  { href: "/live-calls", label: "Live Calls", icon: "live-calls" },
];

// Billing is superadmin/admin only. UI narrowing, not a security boundary.
const PINNED_ITEMS: NavItem[] = [
  { href: "/billing", label: "Billing", icon: "billing" },
  { href: "/docs", label: "Help", icon: "docs" },
  { href: "/settings", label: "Settings", icon: "settings" },
];

const ALL_ITEMS = [...OVERVIEW_ITEMS, ...BUILD_ITEMS, USERS_ITEM, ...CALLING_ITEMS, ...PINNED_ITEMS];

const PAGE_SUBTITLE: Record<string, string> = {
  "/dashboard": "Overview of your calls and agents",
  "/agents": "The AI that answers and makes your calls",
  "/workflows": "Menus and routing before an agent picks up",
  "/knowledge-bases": "Documents your agents can answer from",
  "/telephony": "Numbers and carriers your calls come through",
  "/integrations": "Calendars, CRMs and helpdesks",
  "/calls": "Every call, with transcript and outcome",
  "/campaigns": "Outbound calling lists",
  "/billing": "Minutes used and cost",
};

// Shown only on the list page itself, and never to viewers (read-only).
const PAGE_CTA: Record<string, { label: string; href: string }> = {
  "/agents": { label: "+ Create Agent", href: "/agents/new" },
  "/workflows": { label: "+ New Flow", href: "/workflows/new" },
  "/knowledge-bases": { label: "+ Add Document", href: "/knowledge-bases?add=1" },
  "/telephony": { label: "+ Add Number", href: "/telephony?add=1" },
  "/integrations": { label: "+ Connect App", href: "/integrations#connect" },
  "/campaigns": { label: "+ New Campaign", href: "/campaigns/new" },
};

const monthKey = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [theme, setTheme] = useState<"dark" | "light">("light");
  const [search, setSearch] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [activeTenantId, setActiveTenantId] = useState<string | null>(null);
  const [tenantMenuOpen, setTenantMenuOpen] = useState(false);
  const [monthMinutes, setMonthMinutes] = useState<number | null>(null);
  const accountMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!accountMenuOpen) return;
    const handler = (e: MouseEvent) => {
      if (accountMenuRef.current && !accountMenuRef.current.contains(e.target as Node)) {
        setAccountMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [accountMenuOpen]);

  // Auth guard. /login and /invite skip it; authChecked stays false during a
  // redirect so the page underneath never fetches.
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
        if (u.role !== "superadmin" && (pathname.startsWith("/tenants") || pathname.startsWith("/live-calls"))) {
          router.push("/no-access");
          return;
        }
        // Direct-URL guard matching the hidden nav item.
        if (u.role !== "superadmin" && u.role !== "admin" && (pathname.startsWith("/billing") || pathname.startsWith("/integrations"))) {
          router.push("/no-access");
          return;
        }
        setUser(u);
        setAuthChecked(true);
      })
      .catch(() => {
        setAuthChecked(true);
      });
  }, [pathname, router]);

  const resolveActiveTenantId = useCallback((ts: Tenant[]): string | null => {
    const stored = typeof window !== "undefined" ? window.localStorage.getItem(ACTIVE_TENANT_STORAGE_KEY) : null;
    const found = stored && stored !== ALL_TENANTS_SENTINEL ? ts.find((t) => t.slug === stored) ?? null : null;
    return found?.id ?? null;
  }, []);

  useEffect(() => {
    if (user?.role !== "superadmin") return;
    listTenants()
      .then((ts) => {
        setTenants(ts);
        setActiveTenantId(resolveActiveTenantId(ts));
      })
      .catch(() => {});
  }, [user, resolveActiveTenantId]);

  useEffect(() => {
    if (user?.role !== "superadmin") return;
    const onSwitch = () => setActiveTenantId(resolveActiveTenantId(tenants));
    window.addEventListener("yuviz:active-tenant", onSwitch);
    return () => window.removeEventListener("yuviz:active-tenant", onSwitch);
  }, [user, tenants, resolveActiveTenantId]);

  const activeTenant = tenants.find((t) => t.id === activeTenantId) ?? null;

  // Minutes pill: the switcher's selection for a superadmin, the user's own account otherwise.
  useEffect(() => {
    if (user?.role !== "superadmin" && user?.role !== "admin") return;
    if (user.role === "superadmin" && tenants.length === 0) return;
    const scope = user.role === "superadmin"
      ? Promise.resolve(activeTenant ? [activeTenant] : tenants)
      : listTenants();
    const now = new Date();
    scope
      .then((ts) => listAllUsageTrend(ts, now.getDate() + 1))
      .then((points) => {
        const key = monthKey(now);
        setMonthMinutes(points.filter((p) => p.date.startsWith(key)).reduce((sum, p) => sum + p.minutes, 0));
      })
      .catch(() => setMonthMinutes(null));
  }, [user, tenants, activeTenant]);

  const selectTenant = (t: Tenant) => {
    setActiveTenantId(t.id);
    setTenantMenuOpen(false);
    try { window.localStorage.setItem(ACTIVE_TENANT_STORAGE_KEY, t.slug); } catch { /* non-fatal */ }
    window.dispatchEvent(new CustomEvent("yuviz:active-tenant", { detail: t.slug }));
  };

  const selectAllTenants = () => {
    setActiveTenantId(null);
    setTenantMenuOpen(false);
    try { window.localStorage.setItem(ACTIVE_TENANT_STORAGE_KEY, ALL_TENANTS_SENTINEL); } catch { /* non-fatal */ }
    window.dispatchEvent(new CustomEvent("yuviz:active-tenant", { detail: ALL_TENANTS_SENTINEL }));
  };

  const handleLogout = () => {
    clearAllAgentDrafts();
    clearToken();
    router.push("/login");
  };

  if (pathname === "/login" || pathname === "/invite" || pathname === "/no-access") return <>{children}</>;
  if (!authChecked) return null;

  const isSuperadmin = user?.role === "superadmin";
  const canManageUsers = isSuperadmin || user?.role === "admin";
  const matches = (label: string) => label.toLowerCase().includes(search.trim().toLowerCase());

  // Server-side checks enforce these too (tenants.py, LIVE_CALLS_ROLES); this only hides the links.
  const visible = (item: NavItem) =>
    matches(item.label) &&
    (isSuperadmin || !SUPERADMIN_ONLY.has(item.href)) &&
    (item.href !== "/billing" || canManageUsers) &&
    (item.href !== "/integrations" || canManageUsers);
  const visibleOverview = OVERVIEW_ITEMS.filter(visible);
  const visibleBuild = [...BUILD_ITEMS, USERS_ITEM].filter(visible);
  const visibleCalling = CALLING_ITEMS.filter(visible);
  const visiblePinned = PINNED_ITEMS.filter(visible);
  const noResults = [visibleOverview, visibleBuild, visibleCalling, visiblePinned].every((g) => g.length === 0);

  // Longest-prefix match, not first-match: /agents/acme/bot resolves to Agents.
  const activeItem = [...ALL_ITEMS]
    .sort((a, b) => b.href.length - a.href.length)
    .find((item) => pathname.startsWith(item.href));

  const inAgentConfig = /^\/agents\/[^/]+\/[^/]+/.test(pathname);
  const pageTitle = activeItem?.label ?? "Yuviz";
  const pageSubtitle = inAgentConfig ? "Configuration" : PAGE_SUBTITLE[pathname];
  const cta = user?.role !== "viewer" ? PAGE_CTA[pathname] : undefined;

  const navLink = (item: NavItem) => (
    <Link key={item.href} href={item.href} className={`nav-item${item.href === activeItem?.href ? " active" : ""}`}>
      {ICONS[item.icon]}
      <span className="nav-label">{item.label}</span>
    </Link>
  );

  return (
    <div className={`app-shell${navOpen ? " nav-open" : ""}`}>
      <button className="nav-scrim" aria-label="Close menu" onClick={() => setNavOpen(false)} />

      <aside className={`sidebar${collapsed ? " collapsed" : ""}`}>
        <div className="logo">
          <div className="logo-icon" aria-hidden="true">
            <span className="logo-bar" />
            <span className="logo-bar" />
            <span className="logo-bar" />
          </div>
          <div className="logo-text">Yuviz<span>.ai</span></div>
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
            type="search"
            name="sidebar-page-search"
            autoComplete="off"
            aria-label="Search pages"
            placeholder="Search pages"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        <nav className="nav">
          {visibleOverview.map(navLink)}
          {visibleBuild.length > 0 && (
            <>
              <div className="nav-section">Build</div>
              {visibleBuild.map(navLink)}
            </>
          )}
          {visibleCalling.length > 0 && (
            <>
              <div className="nav-section">Calls</div>
              {visibleCalling.map(navLink)}
            </>
          )}
          {noResults && <div className="nav-empty">No pages match &quot;{search}&quot;</div>}
          <div className="nav-spacer" />
          {visiblePinned.length > 0 && <div className="nav-pinned">{visiblePinned.map(navLink)}</div>}
        </nav>
      </aside>

      <div className="main">
        <header className="topbar">
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

          {isSuperadmin && tenants.length > 0 && (
            <>
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
                  <ChevronDown size={13} className="tenant-switch-caret" />
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
                          All tenants<br />
                          <span className="tenant-switch-row-meta">every account, unfiltered</span>
                        </span>
                        {activeTenantId === null && <Check size={14} className="tenant-switch-check" />}
                      </button>
                      {tenants.map((t) => (
                        <button
                          key={t.id}
                          className={`tenant-switch-row${t.id === activeTenantId ? " active" : ""}`}
                          onClick={() => selectTenant(t)}
                        >
                          <span className="tenant-switch-mark">{tenantInitial(t.name)}</span>
                          <span className="tenant-switch-row-name">
                            {t.name}<br />
                            <span className="tenant-switch-row-meta">{t.slug}</span>
                          </span>
                          {t.id === activeTenantId && <Check size={14} className="tenant-switch-check" />}
                        </button>
                      ))}
                    </div>
                  </>
                )}
              </div>
              <div className="topbar-divider" />
            </>
          )}

          <div className="topbar-title">
            <span className="topbar-page-name">{pageTitle}</span>
            {pageSubtitle && <span className="topbar-page-sub">{pageSubtitle}</span>}
          </div>

          <div className="topbar-actions">
            {canManageUsers && monthMinutes !== null && (
              <Link href="/billing" className="topbar-usage-pill" title="Open Billing">
                {ICONS.clock}
                <span>{Math.round(monthMinutes).toLocaleString("en-IN")} min this month</span>
              </Link>
            )}
            {cta && (
              <Link href={cta.href} className="topbar-cta">
                {cta.label}
              </Link>
            )}
            <div className="account-menu-wrap" ref={accountMenuRef}>
              <button
                className="account-trigger"
                onClick={() => setAccountMenuOpen((o) => !o)}
                aria-expanded={accountMenuOpen}
                aria-label="Account menu"
              >
                <span className="account-avatar">{userInitials(user?.email ?? "?")}</span>
                <ChevronDown size={12} className="account-caret" />
              </button>

              {accountMenuOpen && (
                <div className="account-dropdown">
                  <div className="account-dropdown-user">
                    <span className="account-dropdown-email">{user?.email}</span>
                    <span className="account-dropdown-role">{user?.role}</span>
                  </div>
                  <button
                    className="account-dropdown-item"
                    onClick={() => { setTheme((t) => (t === "dark" ? "light" : "dark")); }}
                  >
                    {theme === "dark" ? <Sun size={13} /> : <Moon size={13} />}
                    <span>{theme === "dark" ? "Light mode" : "Dark mode"}</span>
                    <span className="account-dropdown-switch" data-on={theme === "dark"} />
                  </button>
                  <button
                    className="account-dropdown-item danger"
                    onClick={handleLogout}
                  >
                    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" width="13" height="13">
                      <path d="M6 2H3a1 1 0 00-1 1v10a1 1 0 001 1h3M11 11l3-3-3-3M14 8H6" />
                    </svg>
                    <span>Log out</span>
                  </button>
                </div>
              )}
            </div>
          </div>
        </header>

        <div className="content">{children}</div>
      </div>
    </div>
  );
}
