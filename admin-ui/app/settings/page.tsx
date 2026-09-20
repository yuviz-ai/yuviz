"use client";

import { useEffect, useState } from "react";
import {
  ApiError,
  AuditLogEntry,
  changePassword,
  getCurrentUser,
  listAuditLog,
  listTenants,
  Tenant,
  User,
  UserRole,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

type SettingsSection = "profile" | "sessions" | "security" | "audit-log";

// Users deliberately is NOT a tab here, nor a link out. It has its own
// top-level page (/users) reachable from the sidebar — the copy that used
// to live inside Settings was a duplicate of that whole surface, modal and
// role gate included, and the second entry point was itself the confusion.
const SECTIONS: { id: SettingsSection; label: string }[] = [
  { id: "profile", label: "Profile" },
  { id: "sessions", label: "Sessions" },
  { id: "security", label: "Security" },
  { id: "audit-log", label: "Audit Log" },
];

const ENTITY_TYPES = [
  "agent",
  "agent_tool_policy",
  "carrier",
  "phone_number",
  "provider_config",
  "telephony_config",
  "tenant",
  "tool_provider_config",
  "user",
];

const ACTION_BADGE: Record<string, string> = { created: "green", updated: "amber", deleted: "red" };

const ROLE_BADGE: Record<UserRole, string> = {
  superadmin: "red",
  admin: "indigo",
  supervisor: "amber",
  agent: "amber",
  viewer: "gray",
};

const ROLE_BLURB: Record<UserRole, string> = {
  superadmin: "Full platform access across every account.",
  admin: "Manages this account: agents, numbers, users and billing.",
  supervisor: "Monitors live calls and may intervene on them.",
  agent: "Handles calls; no console access.",
  viewer: "Read-only access to this account.",
};

/** What each role may actually reach in this console. Mirrors the gates in
    AppShell and services/config/deps.py — it is a description of the real
    permissions, so it must be edited whenever those move. */
const ROLE_ACCESS: Record<UserRole, string[]> = {
  superadmin: ["Every account on the platform", "Agents, IVR flows, knowledge and voice", "Telephony, users and billing", "Full audit trail"],
  admin: ["This account only", "Agents, IVR flows, knowledge and voice", "Telephony, users and billing", "Full audit trail"],
  supervisor: ["Live Calls only", "May listen to and barge into a live call", "No configuration access"],
  agent: ["Handles calls", "No console access at all"],
  viewer: ["This account, read-only", "May not invite users or change configuration"],
};

function ProfilePanel() {
  const [user, setUser] = useState<User | null>(null);
  const [accountName, setAccountName] = useState<string | null>(null);

  useEffect(() => {
    getCurrentUser()
      .then(async (u) => {
        setUser(u);
        if (!u.tenant_id) return;
        // A tenant-scoped user's own account name is worth one lookup; a
        // failure just leaves the id showing rather than blanking the card.
        const ts = await listTenants().catch((): Tenant[] => []);
        setAccountName(ts.find((t) => t.id === u.tenant_id)?.name ?? null);
      })
      .catch(() => {});
  }, []);

  const initials = (user?.email ?? "?").slice(0, 2).toUpperCase();
  const joined = user
    ? new Date(user.created_at).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })
    : "…";

  return (
    <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-body">
          <div className="set-identity">
            <div className="set-avatar">{initials}</div>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ fontSize: "1.02rem", fontWeight: 600, color: "var(--text)", wordBreak: "break-all" }}>
                {user?.email ?? "…"}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 5, flexWrap: "wrap" }}>
                <span className={`badge ${user ? ROLE_BADGE[user.role] : "gray"}`}>{user?.role ?? "…"}</span>
                <span style={{ fontSize: ".74rem", color: "var(--text-3)" }}>
                  {user ? ROLE_BLURB[user.role] : ""}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "stretch" }}>
        <div className="card" style={{ flex: "1 1 340px", minWidth: 280 }}>
          <div className="card-hdr">
            <div className="card-title">Account details</div>
          </div>
          <div className="card-body">
            <div className="set-facts">
              <div>
                <div className="set-fact-label">Account</div>
                <div className="set-fact-value">
                  {user?.tenant_id
                    ? accountName ?? <span className="mono" style={{ fontSize: ".74rem" }}>{user.tenant_id}</span>
                    : user
                      ? "Platform — every account"
                      : "…"}
                </div>
              </div>
              <div>
                <div className="set-fact-label">Member since</div>
                <div className="set-fact-value" suppressHydrationWarning>{joined}</div>
              </div>
            </div>
            <div style={{ marginTop: 14 }}>
              <div className="set-fact-label">User ID</div>
              <div className="set-fact-value mono" style={{ fontSize: ".72rem", wordBreak: "break-all" }}>
                {user?.id ?? "…"}
              </div>
            </div>
            <div className="form-hint" style={{ marginTop: 14 }}>
              Editing your own email or role isn&apos;t supported yet — a superadmin changes it from the Users page.
            </div>
          </div>
        </div>

        <div className="card" style={{ flex: "1 1 300px", minWidth: 260 }}>
          <div className="card-hdr">
            <div className="card-title">What this role can reach</div>
          </div>
          <div className="card-body">
            <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 10 }}>
              {(user ? ROLE_ACCESS[user.role] : []).map((line) => (
                <li key={line} style={{ display: "flex", gap: 9, alignItems: "flex-start", fontSize: ".8rem", color: "var(--text-2)" }}>
                  <svg viewBox="0 0 16 16" fill="none" stroke="var(--cyan)" strokeWidth="1.8" style={{ width: 14, height: 14, flexShrink: 0, marginTop: 2 }}>
                    <path d="M3.5 8.4l3 3 6-6.8" />
                  </svg>
                  <span>{line}</span>
                </li>
              ))}
              {!user && <li style={{ fontSize: ".8rem", color: "var(--text-3)" }}>…</li>}
            </ul>
          </div>
        </div>
      </div>
    </>
  );
}

function SessionsPanel() {
  return (
    <div className="card">
      <div className="card-hdr">
        <div className="card-title">Signed-in devices</div>
        <div className="card-sub">Sign out anything you don&apos;t recognise</div>
      </div>
      <div className="card-body">
        {/* This used to reach for .health-row/.status-dot/.health-name,
            none of which exist in globals.css — the row rendered as three
            unstyled lines of text. */}
        <div className="device-row">
          <span className="device-dot" />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="device-name">
              This browser
              <span className="badge green" style={{ marginLeft: 7 }}>This device</span>
            </div>
            <div className="device-meta">Active now</div>
          </div>
        </div>
        <div className="form-hint" style={{ marginTop: 8 }}>
          Sessions aren&apos;t tracked server-side yet — tokens are held in this browser only, so signing out here is
          the only session this console can end.
        </div>
      </div>
    </div>
  );
}

function AuditDiff({ oldValue, newValue }: { oldValue: string | null; newValue: string | null }) {
  const parse = (v: string | null) => {
    if (!v) return null;
    try {
      return JSON.parse(v) as Record<string, unknown>;
    } catch {
      return null;
    }
  };
  const before = parse(oldValue);
  const after = parse(newValue);

  const keys = new Set([...(before ? Object.keys(before) : []), ...(after ? Object.keys(after) : [])]);
  const changed = [...keys].filter(
    (k) => before?.[k] === "[redacted]" || after?.[k] === "[redacted]" || JSON.stringify(before?.[k]) !== JSON.stringify(after?.[k]),
  );

  if (changed.length === 0) {
    return <div className="form-hint">No field-level diff available.</div>;
  }

  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>Field</th>
          <th>Before</th>
          <th>After</th>
        </tr>
      </thead>
      <tbody>
        {changed.map((k) => {
          const isRedacted = before?.[k] === "[redacted]" || after?.[k] === "[redacted]";
          return (
            <tr key={k}>
              <td className="mono">{k}</td>
              <td className="mono" style={{ color: "var(--text-3)" }}>
                {before ? JSON.stringify(before[k]) : "—"}
              </td>
              <td className="mono">
                {after ? JSON.stringify(after[k]) : "—"}
                {isRedacted && (
                  <div style={{ fontSize: ".68rem", color: "var(--text-3)", fontFamily: "var(--font)" }}>
                    value changed — redacted, can&apos;t show what
                  </div>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function AuditLogPanel() {
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [entries, setEntries] = useState<AuditLogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<AuditLogEntry | null>(null);

  const [entityType, setEntityType] = useState("");
  const [action, setAction] = useState("");
  const [userEmail, setUserEmail] = useState("");
  const [debouncedEmail, setDebouncedEmail] = useState("");
  const [offset, setOffset] = useState(0);
  const limit = 25;

  const isSuperadmin = currentUser?.role === "superadmin";

  useEffect(() => {
    getCurrentUser()
      .then((me) => setCurrentUser(me))
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  useEffect(() => {
    const t = setTimeout(() => setDebouncedEmail(userEmail), 300);
    return () => clearTimeout(t);
  }, [userEmail]);

  useEffect(() => {
    if (!currentUser || currentUser.role !== "superadmin") return;
    let ignore = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setError(null);
    listAuditLog({
      entity_type: entityType || undefined,
      action: (action || undefined) as AuditLogEntry["action"] | undefined,
      user_email: debouncedEmail || undefined,
      limit,
      offset,
    })
      .then((result) => {
        if (ignore) return;
        setEntries(result.items);
        setTotal(result.total);
      })
      .catch((e) => {
        if (!ignore) setError(e instanceof ApiError ? e.detail : String(e));
      })
      .finally(() => {
        if (!ignore) setLoading(false);
      });
    return () => {
      ignore = true;
    };
  }, [currentUser, entityType, action, debouncedEmail, offset]);

  if (currentUser && !isSuperadmin) {
    return (
      <div className="card">
        <div className="card-body">
          <div className="empty-state">Viewing the audit log requires a superadmin.</div>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-body" style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <div className="form-group" style={{ margin: 0 }}>
            <label className="form-label">Entity Type</label>
            <select
              className="form-input"
              value={entityType}
              onChange={(e) => {
                setOffset(0);
                setEntityType(e.target.value);
              }}
            >
              <option value="">All</option>
              {ENTITY_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div className="form-group" style={{ margin: 0 }}>
            <label className="form-label">Action</label>
            <select
              className="form-input"
              value={action}
              onChange={(e) => {
                setOffset(0);
                setAction(e.target.value);
              }}
            >
              <option value="">All</option>
              <option value="created">created</option>
              <option value="updated">updated</option>
              <option value="deleted">deleted</option>
            </select>
          </div>
          <div className="form-group" style={{ margin: 0, flex: 1, minWidth: 200 }}>
            <label className="form-label">User Email</label>
            <input
              className="form-input"
              value={userEmail}
              onChange={(e) => {
                setOffset(0);
                setUserEmail(e.target.value);
              }}
              placeholder="search by email…"
            />
          </div>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : entries.length === 0 ? (
          <div className="empty-state">No matching audit entries.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>When</th>
                <th>Entity</th>
                <th>Action</th>
                <th>By</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id}>
                  <td style={{ fontSize: ".71rem", color: "var(--text-3)" }}>
                    {new Date(e.changed_at).toLocaleString()}
                  </td>
                  <td>
                    <span className="mono">{e.entity_type}</span>
                    <span style={{ color: "var(--text-3)", fontSize: ".7rem", marginLeft: 6 }}>
                      {e.entity_id.slice(0, 8)}…
                    </span>
                  </td>
                  <td>
                    <span className={`badge ${ACTION_BADGE[e.action] ?? "gray"}`}>{e.action}</span>
                  </td>
                  <td>{e.user_email ?? <span style={{ color: "var(--text-3)" }}>system</span>}</td>
                  <td>
                    <button className="btn btn-ghost btn-sm" onClick={() => setDetail(e)}>
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {total > limit && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 12 }}>
          <span style={{ fontSize: ".75rem", color: "var(--text-3)" }}>
            {offset + 1}–{Math.min(offset + limit, total)} of {total}
          </span>
          <div style={{ display: "flex", gap: 6 }}>
            <button
              className="btn btn-ghost btn-sm"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - limit))}
            >
              Previous
            </button>
            <button
              className="btn btn-ghost btn-sm"
              disabled={offset + limit >= total}
              onClick={() => setOffset(offset + limit)}
            >
              Next
            </button>
          </div>
        </div>
      )}

      <Modal
        open={detail !== null}
        title={detail ? `${detail.entity_type} ${detail.action} — ${detail.entity_id.slice(0, 8)}…` : ""}
        onClose={() => setDetail(null)}
        footer={
          <button className="btn btn-ghost btn-sm" onClick={() => setDetail(null)}>
            Close
          </button>
        }
      >
        {detail && (
          <>
            <div className="form-hint" style={{ marginBottom: 10 }}>
              {detail.user_email ?? "system"} · {new Date(detail.changed_at).toLocaleString()}
              {detail.ip_address ? ` · ${detail.ip_address}` : ""}
            </div>
            <AuditDiff oldValue={detail.old_value} newValue={detail.new_value} />
          </>
        )}
      </Modal>
    </>
  );
}

function SecurityPanel() {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSuccess(false);
    if (newPassword.length < 8) {
      setError("New password must be at least 8 characters.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("New password and confirmation don't match.");
      return;
    }
    setSubmitting(true);
    try {
      await changePassword(currentPassword, newPassword);
      setSuccess(true);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-hdr">
          <div className="card-title">Two-Factor Authentication</div>
        </div>
        <div className="card-body">
          <label style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span className="toggle-switch">
              <input type="checkbox" disabled />
              <span className="toggle-slider" />
            </span>
            <span style={{ fontSize: ".78rem", color: "var(--text-2)" }}>
              Not available yet — this build authenticates with a password only.
            </span>
          </label>
        </div>
      </div>
      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Change Password</div>
          <div className="card-sub">Use at least 8 characters</div>
        </div>
        <div className="card-body">
          {error && <div className="error-banner">{error}</div>}
          {success && (
            <div className="form-hint" style={{ color: "var(--green)", marginBottom: 12 }}>
              Password changed.
            </div>
          )}
          <form onSubmit={handleSubmit}>
            <div className="form-group">
              <label className="form-label">Current Password</label>
              <input
                className="form-input"
                type="password"
                placeholder="Your current password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                required
              />
            </div>
            <div className="form-row">
              <div className="form-group">
                <label className="form-label">New Password</label>
                <input
                  className="form-input"
                  type="password"
                  placeholder="At least 8 characters"
                  autoComplete="new-password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  required
                />
              </div>
              <div className="form-group">
                <label className="form-label">Confirm New Password</label>
                <input
                  className="form-input"
                  type="password"
                  placeholder="Repeat new password"
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  required
                />
              </div>
            </div>
            <button className="btn btn-primary btn-sm" type="submit" disabled={submitting}>
              {submitting ? "Changing…" : "Change Password"}
            </button>
          </form>
        </div>
      </div>
    </>
  );
}

export default function SettingsPage() {
  const [section, setSection] = useState<SettingsSection>("profile");

  return (
    <div style={{ maxWidth: 980 }}>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ fontSize: "1.5rem", fontWeight: 600, letterSpacing: "-.025em", margin: 0, color: "var(--text)" }}>
          Settings
        </h1>
        <div className="form-hint" style={{ marginTop: 4 }}>
          Your profile and sign-in, plus the audit trail for this account.
        </div>
      </div>

      {/* One horizontal tab strip, the same .tabs/.tab pair the rest of the
          console uses. The left rail this replaced was a second navigation
          idiom for four panels, and it pushed every panel into a narrow
          column on an otherwise empty page. */}
      <div className="tabs">
        {SECTIONS.map((item) => (
          <button
            key={item.id}
            type="button"
            className={`tab${section === item.id ? " active" : ""}`}
            onClick={() => setSection(item.id)}
            aria-current={section === item.id}
          >
            {item.label}
          </button>
        ))}
      </div>

      {section === "profile" && <ProfilePanel />}
      {section === "sessions" && <SessionsPanel />}
      {section === "security" && <SecurityPanel />}
      {section === "audit-log" && <AuditLogPanel />}
    </div>
  );
}
