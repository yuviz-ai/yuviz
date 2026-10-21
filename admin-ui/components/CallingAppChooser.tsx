"use client";

import { useState } from "react";
import type { Tenant } from "@/lib/api";
import { CALLING_APPS } from "@/lib/callingApps";
import type { useConnectionAccess } from "@/lib/useConnectionAccess";
import { AppIcon } from "./AppIcon";
import { CallingAppModal } from "./CallingAppModal";
import { Modal } from "./Modal";

export function CallingAppChooser({ tenants, tenantId, access, onClose, onSaved }: {
  tenants: Tenant[];
  tenantId?: string;
  access: ReturnType<typeof useConnectionAccess>;
  onClose: () => void;
  onSaved: (name: string) => void;
}) {
  const [selectedTenant, setSelectedTenant] = useState(tenantId ?? "");
  const [provider, setProvider] = useState<string | null>(null);
  if (provider && selectedTenant) return <CallingAppModal tenantId={selectedTenant} provider={provider} onClose={() => setProvider(null)} onSaved={onSaved} />;
  return <Modal className="connected-app-dialog" trapFocus open title="Connect a calling app" onClose={onClose}
    footer={<button className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>}>
    {access.error ? <div className="error-banner" role="alert">{access.error} <button className="btn btn-ghost btn-sm" disabled={access.loading} onClick={() => void access.retry()}>Retry permissions</button></div>
      : access.loading ? <p>Checking your permissions…</p>
        : !access.canManage ? <p>An account admin needs to connect a calling app before you can add a phone number.</p>
          : <>
            <p className="form-hint">Connect a calling app first, then buy or add your phone number.</p>
            {!tenantId && <div className="form-group">
              <label className="form-label" htmlFor="calling-app-tenant">Account</label>
              <select id="calling-app-tenant" className="form-select" value={selectedTenant} onChange={(event) => setSelectedTenant(event.target.value)}>
                <option value="">Choose an account</option>
                {tenants.map((tenant) => <option key={tenant.id} value={tenant.id}>{tenant.name}</option>)}
              </select>
              {tenants.length === 0 && <p className="form-hint">Create an account before connecting a calling app.</p>}
            </div>}
            <div className="connected-apps-grid" style={{ marginTop: 16 }}>
              {CALLING_APPS.filter((app) => app.key !== "native" || access.isSuperadmin).map((app) => <button key={app.key} className="btn btn-ghost" style={{ minHeight: 64 }} disabled={!selectedTenant} onClick={() => setProvider(app.key)}>
                <AppIcon icon={app.icon} />{app.label}
              </button>)}
            </div>
          </>}
  </Modal>;
}
