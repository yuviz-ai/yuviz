"use client";

import { useEffect, useState } from "react";
import {
  ApiError,
  createInvite,
  getCurrentUser,
  Invite,
  InviteRole,
  listInvites,
  listTenants,
  listUsers,
  resendInvite,
  revokeInvite,
  Tenant,
  User,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

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

// `expired` is derived, never stored (services/config/user_invites.status
// only ever holds pending/accepted/revoked) — matches the design's "the
// table is the state machine" and lets a resend on an expired invite still
// go through server-side (it only checks status = 'pending').
function deriveStatus(invite: Invite): DerivedInviteStatus {
  if (invite.status === "pending" && new Date(invite.expires_at) < new Date()) return "expired";
  return invite.status;
}

// A read-only reference, not an editable ACL — this system's roles are
// fixed in code (services/config/deps.py's require_role() call sites,
// CONSOLE_ROLES, LIVE_CALLS_ROLES, TRANSCRIPT_ROLES), not a per-tenant
// configurable permission set, so there is nothing here for a click to
// grant or revoke. Every ✓/— below traces to a real gate, not a guess:
//   - dashboards: CONSOLE_ROLES (deps.py) — supervisor isn't a member,
//     and AppShell's own nav restricts a supervisor to the Live Calls
//     item alone, so it never reaches the dashboard route at all.
//   - live calls / transcripts: LIVE_CALLS_ROLES / TRANSCRIPT_ROLES
//     (deps.py) — the two sets this feature's own security rounds fixed.
//   - agents/IVR, phone numbers/telephony, invites: require_role(
//     "superadmin","admin") on agents.py / phone_numbers.py /
//     telephony_configs.py / carriers.py / invites.py.
//   - tenants (create/delete) and the audit log: require_role(
//     "superadmin") alone (tenants.py, audit_log.py) — the one row where
//     "admin" is genuinely narrower than tenant management as a whole
//     (an admin CAN update their own tenant's settings, just not create
//     or delete a tenant, or read another tenant's).
// `agent` isn't a column: it has zero console reach (CONSOLE_ROLES
// excludes it), landing on /no-access on any console URL.
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

export default function UsersPage() {
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Page-level, not modal-level: AC12's "email failed to send" warning has
  // to survive the modal closing — the modal is gone by the time the
  // operator would otherwise see it (same "banner near the top of the page"
  // pattern the error banner below already uses, just amber instead of red).
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
  // Mirrors require_role("superadmin", "admin") on the invite-create route
  // (services/config/routers/invites.py) — the server enforces this either
  // way, this just keeps the button from being offered to a role it always
  // 403s for.
  const canManageUsers = isSuperadmin || currentUser?.role === "admin";

  const refresh = () => {
    setLoading(true);
    setError(null);
    // No `?tenant_id=` passed — the server already scopes both lists to the
    // caller's own tenant (or every tenant for a super_admin, AC11). The UI
    // must not re-derive that scope itself.
    //
    // GET /invites is superadmin/admin-only, so it 403s for a viewer — never
    // request it for a role that can't have invites. And each fetch below is
    // independently caught (allSettled, not Promise.all) so one forbidden or
    // failing sub-request can't blank out data the others already returned;
    // a real failure still surfaces via `error` below, it just doesn't wipe
    // the page.
    getCurrentUser()
      .then(async (me) => {
        setCurrentUser(me);
        const canManage = me.role === "superadmin" || me.role === "admin";
        const [usersResult, invitesResult, tenantsResult] = await Promise.allSettled([
          listUsers(),
          canManage ? listInvites() : Promise.resolve<Invite[]>([]),
          me.role === "superadmin" ? listTenants() : Promise.resolve<Tenant[]>([]),
        ]);
        if (usersResult.status === "fulfilled") setUsers(usersResult.value);
        if (invitesResult.status === "fulfilled") setInvites(invitesResult.value);
        if (tenantsResult.status === "fulfilled") setTenants(tenantsResult.value);
        const failed = [usersResult, invitesResult, tenantsResult].find((r) => r.status === "rejected");
        if (failed?.status === "rejected") {
          const reason = failed.reason;
          setError(reason instanceof ApiError ? reason.detail : String(reason));
        }
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(refresh, []);

  const tenantName = (id: string | null) => (id ? tenants.find((t) => t.id === id)?.name ?? id : "— platform —");

  // Mirrors invites.may_invite: an admin actor may invite anyone except a
  // superadmin, and only within their own tenant. The server enforces this
  // regardless — these just keep the form from offering a request it knows
  // will be refused.
  const inviteRoleOptions: InviteRole[] = isSuperadmin
    ? ["superadmin", "admin", "supervisor", "agent", "viewer"]
    : ["admin", "supervisor", "agent", "viewer"];

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
        // Non-fatal (AC12): the row is already pending and resendable —
        // the modal is about to close, so this has to live at the page
        // level to be seen at all.
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

  return (
    <>
      {canManageUsers && (
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 14 }}>
          <button className="btn btn-primary btn-sm" onClick={openInvite}>
            + Invite User
          </button>
        </div>
      )}

      {error && <div className="error-banner">{error}</div>}
      {notice && (
        <div className="error-banner" style={{ background: "var(--amber-dim)", borderColor: "var(--amber-border)", color: "var(--amber)" }}>
          {notice}
        </div>
      )}

      <div className="users-layout" style={{ marginBottom: 14 }}>
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Members</div>
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
            <div className="card-title">What each role can do</div>
          </div>
          <div className="card-body" style={{ padding: "10px 16px 16px" }}>
            <table className="tbl tbl-matrix">
              <thead>
                <tr>
                  <th></th>
                  <th style={{ textAlign: "center" }}>Super&shy;admin</th>
                  <th style={{ textAlign: "center" }}>Admin</th>
                  <th style={{ textAlign: "center" }}>Super&shy;visor</th>
                  <th style={{ textAlign: "center" }}>Viewer</th>
                </tr>
              </thead>
              <tbody>
                {CAPABILITY_MATRIX.map((row) => (
                  <tr key={row.label}>
                    <td style={{ fontSize: ".76rem" }}>{row.label}</td>
                    <td className="tbl-matrix-cell">{row.superadmin === "yes" ? "✓" : "—"}</td>
                    <td className="tbl-matrix-cell">{row.admin === "yes" ? "✓" : "—"}</td>
                    <td className="tbl-matrix-cell">{row.supervisor === "yes" ? "✓" : "—"}</td>
                    <td className="tbl-matrix-cell">{row.viewer === "yes" ? "✓" : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="form-hint" style={{ marginTop: 10 }}>
              Fixed by role, not editable here — reflects how this console actually
              gates each action today.
            </div>
          </div>
        </div>
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
                // Product decision: an accepted (or already revoked) invite
                // offers neither action — not disabled-with-a-tooltip,
                // simply absent. The server would 409 "invite is not
                // pending" on either, but that path must be unreachable
                // through this UI. Resend now extends expires_at (PR #19
                // finding 2), so it's offered on an expired invite too —
                // same predicate as revoke.
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
              {tenants.map((t) => (
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
