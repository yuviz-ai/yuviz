"use client";

import { useEffect, useState } from "react";
import {
  ApiError,
  createInvite,
  getCurrentUser,
  Invite,
  InviteRole,
  listInvites,
  listUsers,
  resendInvite,
  revokeInvite,
  User,
} from "@/lib/api";
import { Modal } from "@/components/Modal";
import { useActiveTenant } from "@/lib/useActiveTenant";

// Resend cooldown mirrors services/config/invites.py's RESEND_COOLDOWN_SECONDS.
const RESEND_COOLDOWN_SECONDS = 60;

const ROLE_BADGE: Record<InviteRole, string> = {
  superadmin: "red",
  admin: "indigo",
  supervisor: "amber",
  agent: "amber",
  viewer: "gray",
};

type DerivedInviteStatus = "pending" | "expired" | "revoked" | "accepted";

const STATUS_BADGE: Record<DerivedInviteStatus, string> = {
  pending: "amber",
  expired: "gray",
  revoked: "red",
  accepted: "green",
};

// `expired` is derived, never stored; the server only stores pending/accepted/revoked.
function deriveStatus(invite: Invite): DerivedInviteStatus {
  if (invite.status === "pending" && new Date(invite.expires_at) < new Date()) return "expired";
  return invite.status;
}

// Read-only reference; roles are fixed in code. Keep in sync with services/config/deps.py
// (CONSOLE_ROLES, LIVE_CALLS_ROLES, TRANSCRIPT_ROLES) and router require_role() gates.
type Reach = "yes" | "no";
const CAPABILITY_MATRIX: { label: string; superadmin: Reach; admin: Reach; supervisor: Reach; viewer: Reach }[] = [
  { label: "View dashboards & analytics", superadmin: "yes", admin: "yes", supervisor: "no", viewer: "yes" },
  { label: "Listen & join live calls", superadmin: "yes", admin: "yes", supervisor: "yes", viewer: "no" },
  { label: "View live-call transcripts", superadmin: "yes", admin: "yes", supervisor: "no", viewer: "no" },
  { label: "Manage agents & IVR flows", superadmin: "yes", admin: "yes", supervisor: "no", viewer: "no" },
  { label: "Manage phone numbers & telephony", superadmin: "yes", admin: "yes", supervisor: "no", viewer: "no" },
  { label: "Invite & manage users", superadmin: "yes", admin: "yes", supervisor: "no", viewer: "no" },
  { label: "Create or delete tenants", superadmin: "yes", admin: "no", supervisor: "no", viewer: "no" },
  { label: "View the platform audit log", superadmin: "yes", admin: "no", supervisor: "no", viewer: "no" },
];

/** One small headline count. Deliberately plainer than the dashboard's
    Kpi tile — these are inventory numbers with no trend behind them. */
function Tile({ label, value, footnote }: { label: string; value: string; footnote?: string }) {
  return (
    <div className="card" style={{ padding: "12px 16px", flex: "1 1 150px", minWidth: 140 }}>
      <div style={{ fontSize: ".7rem", fontWeight: 600, color: "var(--text-2)" }}>{label}</div>
      <div style={{ fontSize: "1.6rem", fontWeight: 600, letterSpacing: "-.025em", color: "var(--text)", fontVariantNumeric: "tabular-nums", lineHeight: 1.25 }}>
        {value}
      </div>
      {footnote && <div style={{ fontSize: ".68rem", color: "var(--text-3)" }}>{footnote}</div>}
    </div>
  );
}

/** The account's members and invites. Its own page for superadmin (/users),
    the Team members tab of Settings for everyone who can manage users. */
export function TeamMembers({ embedded = false }: { embedded?: boolean }) {
  const { tenant, allTenants, isPlatformScoped, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Page-level so the "email failed to send" warning survives the modal closing.
  const [notice, setNotice] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<InviteRole>("admin");
  const [tenantId, setTenantId] = useState("");
  const [team, setTeam] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [resendingId, setResendingId] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);

  // Ticks once a second purely to re-render the resend cooldown countdown —
  // the underlying data (last_sent_at) doesn't change on its own.
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNowTick(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const isSuperadmin = currentUser?.role === "superadmin";
  // Mirrors the server's require_role("superadmin", "admin") on invites.
  const canManageUsers = isSuperadmin || currentUser?.role === "admin";

  // Only a superadmin with a specific tenant selected filters; the server scopes everyone else.
  const scopeTenantId = isPlatformScoped && !isAllTenants ? tenant?.id : undefined;

  const refresh = () => {
    setLoading(true);
    setError(null);
    // Viewers can't GET /invites. allSettled so one failing request doesn't blank the rest.
    getCurrentUser()
      .then(async (me) => {
        setCurrentUser(me);
        const canManage = me.role === "superadmin" || me.role === "admin";
        const [usersResult, invitesResult] = await Promise.allSettled([
          listUsers(scopeTenantId),
          canManage ? listInvites(scopeTenantId) : Promise.resolve<Invite[]>([]),
        ]);
        if (usersResult.status === "fulfilled") setUsers(usersResult.value);
        if (invitesResult.status === "fulfilled") setInvites(invitesResult.value);
        const failed = [usersResult, invitesResult].find((r) => r.status === "rejected");
        if (failed?.status === "rejected") {
          const reason = failed.reason;
          setError(reason instanceof ApiError ? reason.detail : String(reason));
        }
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (tenantLoading) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeTenantId, tenantLoading]);

  const tenantName = (id: string | null) => (id ? allTenants.find((t) => t.id === id)?.name ?? id : "— platform —");

  // Mirrors invites.may_invite: superadmin is seeded only, never invited.
  const inviteRoleOptions: InviteRole[] = ["admin", "supervisor", "agent", "viewer"];

  const openInvite = () => {
    setEmail("");
    setRole("admin");
    setTenantId(isSuperadmin ? "" : currentUser?.tenant_id ?? "");
    setTeam("");
    setFormError(null);
    setNotice(null);
    setModalOpen(true);
  };

  const handleInvite = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      const invite = await createInvite({
        email,
        role,
        tenant_id: isSuperadmin ? tenantId || null : currentUser?.tenant_id ?? null,
        team: team || null,
      });
      setModalOpen(false);
      if (invite.email_sent === false) {
        // Non-fatal: the invite exists and can be resent.
        setNotice(`Invite created for ${invite.email}, but the email failed to send. Use Resend once the SMTP issue is fixed.`);
      }
      refresh();
    } catch (e) {
      setFormError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const resendCooldownRemaining = (invite: Invite) => {
    if (!invite.last_sent_at) return 0;
    const elapsed = (nowTick - new Date(invite.last_sent_at).getTime()) / 1000;
    return Math.max(0, Math.ceil(RESEND_COOLDOWN_SECONDS - elapsed));
  };

  const handleResend = async (invite: Invite) => {
    setResendingId(invite.id);
    setError(null);
    setNotice(null);
    try {
      await resendInvite(invite.id);
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setResendingId(null);
    }
  };

  const handleRevoke = async (invite: Invite) => {
    if (!window.confirm(`Revoke the invite to ${invite.email}? They won't be able to use their invite link.`)) return;
    setRevokingId(invite.id);
    setError(null);
    setNotice(null);
    try {
      await revokeInvite(invite.id);
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setRevokingId(null);
    }
  };

  const pendingInvites = invites.filter((i) => deriveStatus(i) === "pending").length;
  const adminCount = users.filter((u) => u.role === "superadmin" || u.role === "admin").length;
  const scopeLabel = isAllTenants
    ? `across ${allTenants.length} account${allTenants.length === 1 ? "" : "s"}`
    : `in ${tenant?.name ?? "this account"}`;

  return (
    <>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
        <div>
          {embedded ? (
            <div style={{ fontSize: "1rem", fontWeight: 600, color: "var(--text)" }}>Team members</div>
          ) : (
            <h1 style={{ fontSize: "1.5rem", fontWeight: 600, letterSpacing: "-.025em", margin: 0, color: "var(--text)" }}>Users</h1>
          )}
          <div className="form-hint" style={{ marginTop: 4 }}>
            {loading ? "Loading members…" : `${users.length} member${users.length === 1 ? "" : "s"} ${scopeLabel}.`}
            {isPlatformScoped && !isAllTenants && " Switch accounts from the header."}
          </div>
        </div>
        {canManageUsers && (
          <button className="btn btn-primary btn-sm" onClick={openInvite}>
            + Invite user
          </button>
        )}
      </div>

      {error && <div className="error-banner">{error}</div>}
      {notice && (
        <div className="error-banner" style={{ background: "var(--amber-dim)", borderColor: "var(--amber-border)", color: "var(--amber)" }}>
          {notice}
        </div>
      )}

      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 14 }}>
        <Tile label="Members" value={loading ? "—" : String(users.length)} />
        <Tile label="Admins" value={loading ? "—" : String(adminCount)} footnote="superadmin or admin" />
        <Tile
          label="Pending invites"
          value={loading || !canManageUsers ? "—" : String(pendingInvites)}
          footnote={canManageUsers ? "not yet accepted" : "visible to admins only"}
        />
      </div>

      {/* Members and Invites each get the full width. They used to share a
          row with the role matrix at 1.6fr/1fr, which squeezed the matrix's
          five columns until every header and label wrapped, while a short
          member list left the other half of the row empty. The matrix is
          reference material, so it sits at the bottom, full width. */}
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-hdr">
          <div className="card-title">Members</div>
          <div className="card-sub">{loading ? "" : `${users.length} total`}</div>
        </div>
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : users.length === 0 ? (
          <div className="empty-state">No users yet.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Email</th>
                <th>Role</th>
                {isSuperadmin && <th>Tenant</th>}
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td className="bold">
                    {u.email}
                    {u.id === currentUser?.id && (
                      <span className="badge indigo" style={{ marginLeft: 6 }}>
                        You
                      </span>
                    )}
                  </td>
                  <td>
                    <span className={`badge ${ROLE_BADGE[u.role]}`}>{u.role}</span>
                  </td>
                  {isSuperadmin && <td>{tenantName(u.tenant_id)}</td>}
                  <td style={{ fontSize: ".71rem", color: "var(--text-3)" }}>
                    {new Date(u.created_at).toLocaleDateString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <div className="card-hdr">
          {/* Lists every invite regardless of status (pending/expired/revoked/
              accepted), not just pending ones — the heading says so. */}
          <div className="card-title">Invites</div>
        </div>
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : invites.length === 0 ? (
          <div className="empty-state">No invites yet.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Email</th>
                <th>Role</th>
                {isSuperadmin && <th>Tenant</th>}
                <th>Status</th>
                <th>Invited</th>
                <th>Expires</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {invites.map((inv) => {
                const status = deriveStatus(inv);
                // Actions only for pending invites (incl. expired: resend extends expires_at).
                const revocable = inv.status === "pending";
                const cooldown = resendCooldownRemaining(inv);
                return (
                  <tr key={inv.id}>
                    <td className="bold">{inv.email}</td>
                    <td>
                      <span className={`badge ${ROLE_BADGE[inv.role]}`}>{inv.role}</span>
                    </td>
                    {isSuperadmin && <td>{tenantName(inv.tenant_id)}</td>}
                    <td>
                      <span className={`badge ${STATUS_BADGE[status]}`}>{status}</span>
                    </td>
                    <td style={{ fontSize: ".71rem", color: "var(--text-3)" }}>
                      {new Date(inv.created_at).toLocaleDateString()}
                    </td>
                    <td style={{ fontSize: ".71rem", color: "var(--text-3)" }}>
                      {new Date(inv.expires_at).toLocaleDateString()}
                    </td>
                    <td style={{ display: "flex", gap: 6 }}>
                      {revocable && (
                        <button
                          className="btn btn-ghost btn-sm"
                          onClick={() => handleResend(inv)}
                          disabled={resendingId === inv.id || cooldown > 0}
                          title={cooldown > 0 ? `Wait ${cooldown}s before resending` : undefined}
                        >
                          {resendingId === inv.id ? "Resending…" : cooldown > 0 ? `Resend (${cooldown}s)` : "Resend"}
                        </button>
                      )}
                      {revocable && (
                        <button
                          className="btn btn-danger btn-sm"
                          onClick={() => handleRevoke(inv)}
                          disabled={revokingId === inv.id}
                        >
                          {revokingId === inv.id ? "Revoking…" : "Revoke"}
                        </button>
                      )}
                      {!revocable && (
                        <span style={{ fontSize: ".71rem", color: "var(--text-3)" }}>—</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-hdr">
          <div className="card-title">What each role can do</div>
          <div className="card-sub">Fixed by role — not editable here</div>
        </div>
        <div className="card-body" style={{ padding: "4px 16px 16px" }}>
          <table className="tbl tbl-matrix">
            <thead>
              <tr>
                <th>Capability</th>
                <th style={{ textAlign: "center" }}>Superadmin</th>
                <th style={{ textAlign: "center" }}>Admin</th>
                <th style={{ textAlign: "center" }}>Supervisor</th>
                <th style={{ textAlign: "center" }}>Viewer</th>
              </tr>
            </thead>
            <tbody>
              {CAPABILITY_MATRIX.map((row) => (
                <tr key={row.label}>
                  <td style={{ fontSize: ".78rem" }}>{row.label}</td>
                  {([row.superadmin, row.admin, row.supervisor, row.viewer] as Reach[]).map((reach, i) => (
                    <td key={i} className="tbl-matrix-cell" style={{ color: reach === "yes" ? "var(--cyan)" : "var(--text-3)" }}>
                      {reach === "yes" ? "✓" : "—"}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <div className="form-hint" style={{ marginTop: 10 }}>
            Reflects how this console actually gates each action today — the same checks
            services/config/deps.py enforces server-side.
          </div>
        </div>
      </div>

      <Modal
        open={modalOpen}
        title="Invite User"
        onClose={() => setModalOpen(false)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setModalOpen(false)}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" onClick={handleInvite} disabled={submitting || !email}>
              {submitting ? "Sending…" : "Send Invite"}
            </button>
          </>
        }
      >
        {formError && <div className="error-banner">{formError}</div>}
        <div className="form-group">
          <label className="form-label" htmlFor="invite-email">
            Email <span className="required">*</span>
          </label>
          <input
            id="invite-email"
            className="form-input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="teammate@company.com"
          />
        </div>
        <div className="form-group">
          <label className="form-label" htmlFor="invite-role">Role</label>
          <select id="invite-role" className="form-input" value={role} onChange={(e) => setRole(e.target.value as InviteRole)}>
            {inviteRoleOptions.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        {isSuperadmin && (
          <div className="form-group">
            <label className="form-label" htmlFor="invite-tenant">
              Tenant <span className="hint">blank = platform-wide (superadmin scope)</span>
            </label>
            <select id="invite-tenant" className="form-input" value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
              <option value="">— Platform (no tenant) —</option>
              {allTenants.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </div>
        )}
        <div className="form-group">
          <label className="form-label" htmlFor="invite-team">
            Team <span className="hint">optional</span>
          </label>
          <input id="invite-team" className="form-input" value={team} onChange={(e) => setTeam(e.target.value)} placeholder="e.g. Support" />
        </div>
        <div className="form-hint">The invite link is valid for 7 days and can be resent or revoked from this page.</div>
      </Modal>
    </>
  );
}
