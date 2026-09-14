"use client";

import { useEffect, useState } from "react";
import {
  ApiError, createTenant, deleteTenant, listAgents, listTenants, Tenant, TenantUpdate,
  updateTenant, updateTenantConcurrency,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

export default function TenantsPage() {
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
  // Separate from editForm/updateTenant() on purpose: this goes through its
  // own PATCH /tenants/{id}/concurrency (services/config/routers/tenants.py),
  // not the superadmin-only PATCH /tenants/{id} — that's what lets an
  // admin (not just superadmin) edit their own tenant's cap. "" means
  // "leave it unchanged," never "clear it to NULL" (that endpoint doesn't
  // offer clearing — see schemas.py's TenantConcurrencyUpdate).
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

  const handleDelete = async (t: Tenant) => {
    if (!window.confirm(`Delete tenant "${t.name}"? Its agents, providers, and phone numbers remain in the database but will no longer resolve.`)) return;
    try {
      await deleteTenant(t.id);
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
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
    // Two independent calls, not one folded into the other: PATCH
    // /tenants/{id} (name/region/timeouts) is superadmin-only
    // (require_role("superadmin") — routers/tenants.py), but PATCH
    // /tenants/{id}/concurrency is the route T17 built specifically so a
    // tenant_admin can set their OWN tenant's cap. Calling updateTenant()
    // first and letting its 403 abort the handler made that route
    // unreachable from this page for exactly the role it was built for —
    // concurrency must be attempted regardless of whether the other PATCH
    // succeeds, fails, or isn't applicable to this actor's role.
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
        // Its own audited/cache-invalidated write (T16/T17) — the next
        // Live Calls poll for this tenant reflects the new utilization_pct
        // because update_tenant()'s cache.invalidate() fires either way.
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
        ) : tenants.length === 0 ? (
          <div className="empty-state">No tenants yet. Create one to get started.</div>
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
              {tenants.map((t) => (
                <tr key={t.id}>
                  <td className="bold">{t.name}</td>
                  <td className="mono">{t.slug}</td>
                  <td>{t.region}</td>
                  <td>
                    <span className="badge indigo">{agentCounts[t.id] ?? "…"}</span>
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
                    <button className="btn btn-danger btn-sm" onClick={() => handleDelete(t)}>
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
    </>
  );
}
