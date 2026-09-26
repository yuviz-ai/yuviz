"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  ApiError, createTenant, deleteTenant, listAgents, listPhoneNumbers, listTenants, Tenant, TenantUpdate,
  updateTenant, updateTenantConcurrency,
} from "@/lib/api";
import { ACTIVE_TENANT_STORAGE_KEY } from "@/components/AppShell";
import { Modal } from "@/components/Modal";
import { useActiveTenant } from "@/lib/useActiveTenant";

export default function TenantsPage() {
  const { tenant: activeTenant, isAllTenants } = useActiveTenant();
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [agentCounts, setAgentCounts] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Tenant | null>(null);

  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [region, setRegion] = useState("us");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [editForm, setEditForm] = useState<TenantUpdate>({});
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  const [deleteTarget, setDeleteTarget] = useState<Tenant | null>(null);
  // null = still checking; never claim "nothing attached" before the real check runs.
  const [deleteCounts, setDeleteCounts] = useState<{
    active_agents: number; active_phone_numbers: number; inactive_agents: number;
  } | null>(null);
  const [deleteChecking, setDeleteChecking] = useState(false);
  const [deleteSubmitting, setDeleteSubmitting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // "" means "leave unchanged" — the concurrency endpoint has no way to clear to NULL.
  const [maxConcurrentCalls, setMaxConcurrentCalls] = useState<number | "">("");

  const refresh = () => {
    setLoading(true);
    listTenants()
      .then(async (ts) => {
        setTenants(ts);
        const counts: Record<string, number> = {};
        await Promise.all(
          ts.map(async (t) => {
            counts[t.id] = (await listAgents(t.slug)).length;
          }),
        );
        setAgentCounts(counts);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(refresh, []);

  const visibleTenants = isAllTenants ? tenants : tenants.filter((t) => t.id === activeTenant?.id);

  const handleCreate = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      await createTenant({ name, slug, region });
      setModalOpen(false);
      setName("");
      setSlug("");
      setRegion("us");
      refresh();
    } catch (e) {
      setFormError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const openDelete = async (t: Tenant) => {
    setDeleteTarget(t);
    setDeleteCounts(null);
    setDeleteError(null);
    setDeleteChecking(true);
    try {
      const [agents, numbers] = await Promise.all([listAgents(t.slug), listPhoneNumbers(t.id)]);
      setDeleteCounts({
        active_agents: agents.filter((a) => a.status === "active").length,
        inactive_agents: agents.filter((a) => a.status !== "active").length,
        active_phone_numbers: numbers.filter((n) => n.status === "active").length,
      });
    } catch (e) {
      setDeleteError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setDeleteChecking(false);
    }
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    setDeleteSubmitting(true);
    setDeleteError(null);
    try {
      await deleteTenant(deleteTarget.id);
      setDeleteTarget(null);
      refresh();
    } catch (e) {
      // 409 carries counts — switch to the blocked variant instead of a plain error.
      if (e instanceof ApiError && e.status === 409 && e.body) {
        // inactive_agents isn't in the 409 body — carry it forward from the pre-check.
        setDeleteCounts((prev) => ({
          active_agents: Number(e.body!.active_agents ?? 0),
          active_phone_numbers: Number(e.body!.active_phone_numbers ?? 0),
          inactive_agents: prev?.inactive_agents ?? 0,
        }));
      } else {
        setDeleteError(e instanceof ApiError ? e.detail : String(e));
      }
    } finally {
      setDeleteSubmitting(false);
    }
  };

  const handleForceDelete = async () => {
    if (!deleteTarget) return;
    setDeleteSubmitting(true);
    setDeleteError(null);
    try {
      await deleteTenant(deleteTarget.id, true);
      setDeleteTarget(null);
      setDeleteCounts(null);
      refresh();
    } catch (e) {
      setDeleteError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setDeleteSubmitting(false);
    }
  };

  const openEdit = (t: Tenant) => {
    setEditTarget(t);
    setEditForm({
      name: t.name,
      region: t.region,
      transfer_timeout_ms: t.transfer_timeout_ms,
      no_speech_timeout_ms: t.no_speech_timeout_ms,
    });
    setMaxConcurrentCalls(t.max_concurrent_calls ?? "");
    setEditError(null);
  };

  const handleEditSave = async () => {
    if (!editTarget) return;
    setEditSubmitting(true);
    setEditError(null);
    // Two independent calls: PATCH /tenants/{id} is superadmin-only, but PATCH
    // /tenants/{id}/concurrency also allows a tenant_admin — must attempt both.
    const concurrencyChanged =
      maxConcurrentCalls !== "" && maxConcurrentCalls !== editTarget.max_concurrent_calls;
    const otherFieldsChanged =
      editForm.name !== editTarget.name ||
      editForm.region !== editTarget.region ||
      editForm.transfer_timeout_ms !== editTarget.transfer_timeout_ms ||
      editForm.no_speech_timeout_ms !== editTarget.no_speech_timeout_ms;

    const errors: string[] = [];
    if (concurrencyChanged) {
      try {
        await updateTenantConcurrency(editTarget.id, maxConcurrentCalls);
      } catch (e) {
        errors.push(e instanceof ApiError ? e.detail : String(e));
      }
    }
    if (otherFieldsChanged) {
      try {
        await updateTenant(editTarget.id, editForm);
      } catch (e) {
        errors.push(e instanceof ApiError ? e.detail : String(e));
      }
    }

    if (errors.length > 0) {
      setEditError(errors.join(" "));
      setEditSubmitting(false);
      return; // keep the modal open — whatever failed is still visible to fix/retry
    }
    setEditSubmitting(false);
    setEditTarget(null);
    refresh();
  };

  return (
    <>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 14 }}>
        <button className="btn btn-primary btn-sm" onClick={() => setModalOpen(true)}>
          + New Tenant
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : visibleTenants.length === 0 ? (
          <div className="empty-state">
            {tenants.length === 0 ? "No tenants yet. Create one to get started." : "No tenant matches the selected account."}
          </div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Name</th>
                <th>Slug</th>
                <th>Region</th>
                <th>Agents</th>
                <th>Config Version</th>
                <th>Created</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {visibleTenants.map((t) => (
                <tr key={t.id}>
                  <td className="bold">{t.name}</td>
                  <td className="mono">{t.slug}</td>
                  <td>{t.region}</td>
                  <td>
                    <span className="ver-badge">{agentCounts[t.id] ?? "…"}</span>
                  </td>
                  <td>
                    <span className="ver-badge">v{t.config_version}</span>
                  </td>
                  <td style={{ fontSize: ".71rem", color: "var(--text-3)" }}>
                    {new Date(t.created_at).toLocaleDateString()}
                  </td>
                  <td style={{ display: "flex", gap: 6 }}>
                    <button className="btn btn-ghost btn-sm" onClick={() => openEdit(t)}>
                      Edit
                    </button>
                    <button className="btn btn-danger btn-sm" onClick={() => openDelete(t)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Modal
        open={modalOpen}
        title="New Tenant"
        onClose={() => setModalOpen(false)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setModalOpen(false)}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={submitting || !name || !slug}>
              {submitting ? "Creating…" : "Create Tenant"}
            </button>
          </>
        }
      >
        {formError && <div className="error-banner">{formError}</div>}
        <div className="form-group">
          <label className="form-label">
            Name <span className="required">*</span>
          </label>
          <input className="form-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Acme Corp" />
        </div>
        <div className="form-group">
          <label className="form-label">
            Slug <span className="required">*</span>
            <span className="hint">lowercase, no spaces — used in routing keys</span>
          </label>
          <input
            className="form-input"
            style={{ fontFamily: "var(--mono)" }}
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="acme"
          />
        </div>
        <div className="form-group">
          <label className="form-label">Region</label>
          <input className="form-input" value={region} onChange={(e) => setRegion(e.target.value)} placeholder="us" />
        </div>
      </Modal>

      <Modal
        open={editTarget !== null}
        title={`Edit Tenant — ${editTarget?.slug ?? ""}`}
        onClose={() => setEditTarget(null)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setEditTarget(null)}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" onClick={handleEditSave} disabled={editSubmitting}>
              {editSubmitting ? "Saving…" : "Save Changes"}
            </button>
          </>
        }
      >
        {editError && <div className="error-banner">{editError}</div>}
        <div className="form-group">
          <label className="form-label">Name</label>
          <input className="form-input" value={editForm.name || ""} onChange={(e) => setEditForm({ ...editForm, name: e.target.value })} />
        </div>
        <div className="form-group">
          <label className="form-label">Region</label>
          <input className="form-input" value={editForm.region || ""} onChange={(e) => setEditForm({ ...editForm, region: e.target.value })} />
        </div>
        <div className="form-group">
          <label className="form-label">
            Transfer Timeout (ms) <span className="hint">how long a call transfer waits for the destination to answer before failing — 10000&ndash;120000, blank = default (45000)</span>
          </label>
          <input
            className="form-input"
            style={{ fontFamily: "var(--mono)", width: 140 }}
            type="number"
            min={10000}
            max={120000}
            step={1000}
            value={editForm.transfer_timeout_ms ?? ""}
            onChange={(e) =>
              setEditForm({
                ...editForm,
                transfer_timeout_ms: e.target.value === "" ? null : Number(e.target.value),
              })
            }
            placeholder="45000"
          />
        </div>
        <div className="form-group">
          <label className="form-label">
            No-Speech Timeout (ms) <span className="hint">how long the platform waits for the caller to say anything before hanging up — 5000&ndash;120000, blank = default (30000)</span>
          </label>
          <input
            className="form-input"
            style={{ fontFamily: "var(--mono)", width: 140 }}
            type="number"
            min={5000}
            max={120000}
            step={1000}
            value={editForm.no_speech_timeout_ms ?? ""}
            onChange={(e) =>
              setEditForm({
                ...editForm,
                no_speech_timeout_ms: e.target.value === "" ? null : Number(e.target.value),
              })
            }
            placeholder="30000"
          />
        </div>
        <div className="form-group">
          <label className="form-label">
            Max Concurrent Calls <span className="hint">the channel cap Live Calls' utilization KPI is measured against — blank means not set yet (never defaulted to a number)</span>
          </label>
          <input
            className="form-input"
            style={{ fontFamily: "var(--mono)", width: 140 }}
            type="number"
            min={1}
            max={10000}
            step={1}
            value={maxConcurrentCalls}
            onChange={(e) => setMaxConcurrentCalls(e.target.value === "" ? "" : Number(e.target.value))}
            placeholder="Not set"
          />
        </div>
        <div className="form-hint">Slug can&apos;t be changed after creation — it&apos;s used in Redis routing keys.</div>
      </Modal>

      {(() => {
        const isBlocked = deleteCounts !== null && (deleteCounts.active_agents > 0 || deleteCounts.active_phone_numbers > 0);
        return (
          <Modal
            open={deleteTarget !== null}
            title={
              deleteChecking
                ? `Checking "${deleteTarget?.name}"…`
                : isBlocked
                  ? `Can't delete "${deleteTarget?.name}"`
                  : `Delete "${deleteTarget?.name}"?`
            }
            onClose={() => setDeleteTarget(null)}
            footer={
              deleteChecking ? (
                <button className="btn btn-ghost btn-sm" onClick={() => setDeleteTarget(null)}>Cancel</button>
              ) : isBlocked ? (
                <>
                  <button className="btn btn-ghost btn-sm" onClick={() => setDeleteTarget(null)}>Cancel</button>
                  {!!deleteCounts?.active_agents && (
                    <Link
                      href="/agents"
                      className="btn btn-ghost btn-sm"
                      onClick={() => {
                        if (deleteTarget) window.localStorage.setItem(ACTIVE_TENANT_STORAGE_KEY, deleteTarget.slug);
                      }}
                    >
                      Deactivate agents
                    </Link>
                  )}
                  {!!deleteCounts?.active_phone_numbers && (
                    <Link
                      href="/telephony"
                      className="btn btn-ghost btn-sm"
                      onClick={() => {
                        if (deleteTarget) window.localStorage.setItem(ACTIVE_TENANT_STORAGE_KEY, deleteTarget.slug);
                      }}
                    >
                      Detach numbers
                    </Link>
                  )}
                  <button className="btn btn-danger btn-sm" onClick={handleForceDelete} disabled={deleteSubmitting}>
                    {deleteSubmitting ? "Deleting…" : "Force delete anyway"}
                  </button>
                </>
              ) : (
                <>
                  <button className="btn btn-ghost btn-sm" onClick={() => setDeleteTarget(null)}>Cancel</button>
                  <button className="btn btn-danger btn-sm" onClick={handleDeleteConfirm} disabled={deleteSubmitting}>
                    {deleteSubmitting ? "Deleting…" : "Delete account"}
                  </button>
                </>
              )
            }
          >
            {deleteError && <div className="error-banner">{deleteError}</div>}
            {deleteChecking ? (
              <p style={{ fontSize: ".78rem", color: "var(--text-3)" }}>
                Checking for active agents and phone numbers still attached to this account…
              </p>
            ) : isBlocked && deleteCounts ? (
              <>
                <div style={{ display: "flex", gap: 28, padding: "10px 0", borderBottom: "1px solid var(--border-2)", marginBottom: 12 }}>
                  <div>
                    <div style={{ fontSize: ".64rem", color: "var(--text-3)", textTransform: "uppercase", letterSpacing: ".05em", marginBottom: 3 }}>
                      Active agents
                    </div>
                    <div style={{ fontSize: "1rem", fontFamily: "var(--mono)", color: "var(--text)" }}>{deleteCounts.active_agents}</div>
                  </div>
                  <div>
                    <div style={{ fontSize: ".64rem", color: "var(--text-3)", textTransform: "uppercase", letterSpacing: ".05em", marginBottom: 3 }}>
                      Active phone numbers
                    </div>
                    <div style={{ fontSize: "1rem", fontFamily: "var(--mono)", color: "var(--text)" }}>{deleteCounts.active_phone_numbers}</div>
                  </div>
                  {!!deleteCounts.inactive_agents && (
                    <div>
                      <div style={{ fontSize: ".64rem", color: "var(--text-3)", textTransform: "uppercase", letterSpacing: ".05em", marginBottom: 3 }}>
                        Inactive agents
                      </div>
                      <div style={{ fontSize: "1rem", fontFamily: "var(--mono)", color: "var(--text-3)" }}>{deleteCounts.inactive_agents}</div>
                    </div>
                  )}
                </div>
                <p style={{
                  fontSize: ".78rem", color: "var(--text-2)", lineHeight: 1.5,
                  borderLeft: "2px solid var(--red-border)", padding: "6px 0 6px 10px", margin: 0,
                }}>
                  Calls to these numbers would stop resolving the moment this account is deleted — deleted
                  tenants are excluded from DID routing immediately, so this is an instant outage for this
                  account&apos;s callers, not a small risk. Deactivate the agents and detach the phone numbers
                  first, or force the delete if you&apos;re certain.
                </p>
              </>
            ) : (
              <p style={{
                fontSize: ".78rem", color: "var(--text-2)", lineHeight: 1.5,
                borderLeft: "2px solid var(--green-border)", padding: "6px 0 6px 10px", margin: 0,
              }}>
                Nothing is attached — no active agents, no active phone numbers. Its providers, past calls, and
                audit history remain in the database for reference but will no longer be reachable from the
                console.
              </p>
            )}
          </Modal>
        );
      })()}
    </>
  );
}
