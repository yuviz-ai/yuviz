"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AgentWithTenant,
  ApiError,
  createPhoneNumber,
  deletePhoneNumber,
  listAllAgents,
  listAllPhoneNumbers,
  PhoneNumberCreate,
  PhoneNumberStatus,
  PhoneNumberUpdate,
  PhoneNumberWithTenant,
  updatePhoneNumber,
} from "@/lib/api";
import { Modal } from "@/components/Modal";
import { useActiveTenant } from "@/lib/useActiveTenant";

const STATUSES: PhoneNumberStatus[] = ["active", "inactive", "suspended"];
const ALL_TENANTS = "__all__";

export default function PhoneNumbersPage() {
  const { allTenants, isAllTenants, tenant, loading: tenantLoading } = useActiveTenant();
  // Scopes the underlying fetch to the header switcher. The dropdown below
  // is a separate, page-local narrowing on top of whatever that already
  // fetched — unchanged, and still built from allTenants so it keeps
  // offering every account regardless of the header's current selection.
  const targetTenants = useMemo(
    () => (isAllTenants ? allTenants : tenant ? [tenant] : []),
    [tenant, allTenants, isAllTenants],
  );
  const [filterTenantId, setFilterTenantId] = useState<string>(ALL_TENANTS);
  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [numbers, setNumbers] = useState<PhoneNumberWithTenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  const [createTenantId, setCreateTenantId] = useState("");
  const [form, setForm] = useState<PhoneNumberCreate>({ did: "", status: "active" });
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [editTarget, setEditTarget] = useState<PhoneNumberWithTenant | null>(null);
  const [editForm, setEditForm] = useState<PhoneNumberUpdate>({});
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  const openCreateModal = () => {
    setCreateTenantId((id) => id || allTenants[0]?.id || "");
    setModalOpen(true);
  };

  const refresh = () => {
    if (tenantLoading || targetTenants.length === 0) return;
    setLoading(true);
    Promise.all([listAllPhoneNumbers(targetTenants), listAllAgents(targetTenants)])
      .then(([nums, ags]) => {
        setNumbers(nums);
        setAgents(ags);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(refresh, [targetTenants, tenantLoading]);

  const agentName = (id: string | null) => {
    if (!id) return <i style={{ color: "var(--text-3)" }}>none</i>;
    const a = agents.find((a) => a.id === id);
    return a ? a.name : id;
  };

  const handleCreate = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      await createPhoneNumber(createTenantId, form);
      setModalOpen(false);
      setForm({ did: "", status: "active" });
      refresh();
    } catch (e) {
      setFormError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const handleStatusChange = async (n: PhoneNumberWithTenant, status: PhoneNumberStatus) => {
    try {
      await updatePhoneNumber(n.id, { status });
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleDelete = async (n: PhoneNumberWithTenant) => {
    if (!window.confirm(`Delete DID "${n.did}"? Calls to it will fall back to the default agent for its account.`)) return;
    try {
      await deletePhoneNumber(n.id);
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const openEdit = (n: PhoneNumberWithTenant) => {
    setEditTarget(n);
    setEditForm({ agent_id: n.agent_id, fallback_agent_id: n.fallback_agent_id });
    setEditError(null);
  };

  const handleEditSave = async () => {
    if (!editTarget) return;
    setEditSubmitting(true);
    setEditError(null);
    try {
      await updatePhoneNumber(editTarget.id, editForm);
      setEditTarget(null);
      refresh();
    } catch (e) {
      setEditError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setEditSubmitting(false);
    }
  };

  const agentsForTenant = (tenantId: string) => agents.filter((a) => allTenants.find((t) => t.id === tenantId)?.slug === a.tenantSlug);

  const visibleNumbers = filterTenantId === ALL_TENANTS ? numbers : numbers.filter((n) => n.tenant_id === filterTenantId);

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 14, gap: 10 }}>
        <select className="form-select" style={{ width: 240 }} value={filterTenantId} onChange={(e) => setFilterTenantId(e.target.value)}>
          <option value={ALL_TENANTS}>All Accounts</option>
          {allTenants.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
        <button className="btn btn-primary btn-sm" onClick={openCreateModal} disabled={allTenants.length === 0}>
          + Assign DID
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : visibleNumbers.length === 0 ? (
          <div className="empty-state">No phone numbers assigned yet.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>DID</th>
                <th>Account</th>
                <th>Agent</th>
                <th>Fallback Agent</th>
                <th>Status</th>
                <th>Region</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {visibleNumbers.map((n) => (
                <tr key={n.id}>
                  <td className="bold mono" style={{ color: "var(--text)" }}>
                    {n.did}
                  </td>
                  <td>{n.tenantName}</td>
                  <td>{agentName(n.agent_id)}</td>
                  <td>{agentName(n.fallback_agent_id)}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <select
                      className={`form-select status-select ${n.status}`}
                      style={{ width: 110, padding: "3px 8px", fontSize: ".71rem" }}
                      value={n.status}
                      onChange={(e) => handleStatusChange(n, e.target.value as PhoneNumberStatus)}
                    >
                      {STATUSES.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>{n.region || "—"}</td>
                  <td style={{ display: "flex", gap: 6 }} onClick={(e) => e.stopPropagation()}>
                    <button className="btn btn-ghost btn-sm" onClick={() => openEdit(n)}>
                      Edit
                    </button>
                    <button className="btn btn-danger btn-sm" onClick={() => handleDelete(n)}>
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
        title="Assign DID"
        onClose={() => setModalOpen(false)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setModalOpen(false)}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={submitting || !form.did}>
              {submitting ? "Assigning…" : "Assign"}
            </button>
          </>
        }
      >
        {formError && <div className="error-banner">{formError}</div>}
        <div className="form-group">
          <label className="form-label">
            Account <span className="required">*</span>
          </label>
          <select className="form-select" value={createTenantId} onChange={(e) => setCreateTenantId(e.target.value)}>
            {allTenants.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">
            DID <span className="required">*</span>
            <span className="hint">e.g. 5000, or +14085551000</span>
          </label>
          <input
            className="form-input"
            style={{ fontFamily: "var(--mono)" }}
            value={form.did}
            onChange={(e) => setForm({ ...form, did: e.target.value })}
            placeholder="5000"
          />
        </div>
        <div className="form-group">
          <label className="form-label">Agent</label>
          <select className="form-select" value={form.agent_id || ""} onChange={(e) => setForm({ ...form, agent_id: e.target.value || undefined })}>
            <option value="">— none (routes to default) —</option>
            {agentsForTenant(createTenantId).map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">
            Fallback Agent <span className="hint">used if the primary agent is unavailable or inactive</span>
          </label>
          <select
            className="form-select"
            value={form.fallback_agent_id || ""}
            onChange={(e) => setForm({ ...form, fallback_agent_id: e.target.value || undefined })}
          >
            <option value="">— none —</option>
            {agentsForTenant(createTenantId).map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">Status</label>
          <select className="form-select" value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value as PhoneNumberStatus })}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
      </Modal>

      <Modal
        open={editTarget !== null}
        title={`Edit DID — ${editTarget?.did ?? ""}`}
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
        {editTarget && (
          <>
            <div className="form-group">
              <label className="form-label">Agent</label>
              <select
                className="form-select"
                value={editForm.agent_id || ""}
                onChange={(e) => setEditForm({ ...editForm, agent_id: e.target.value || null })}
              >
                <option value="">— none (routes to default) —</option>
                {agentsForTenant(editTarget.tenant_id).map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <label className="form-label">
                Fallback Agent <span className="hint">used if the primary agent is unavailable or inactive</span>
              </label>
              <select
                className="form-select"
                value={editForm.fallback_agent_id || ""}
                onChange={(e) => setEditForm({ ...editForm, fallback_agent_id: e.target.value || null })}
              >
                <option value="">— none —</option>
                {agentsForTenant(editTarget.tenant_id).map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
            </div>
          </>
        )}
      </Modal>
    </>
  );
}
