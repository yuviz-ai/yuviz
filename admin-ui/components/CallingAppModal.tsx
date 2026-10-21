"use client";

import { useState } from "react";
import {
  ApiError, Carrier, CarrierProvider, TelephonyConfig,
  createCarrier, createTelephonyConfig, updateCarrier, updateTelephonyConfig,
} from "@/lib/api";
import { secretPayload } from "@/components/SecretRefInput";
import { Modal } from "@/components/Modal";

import { callingAppLabel, CARRIER_PROVIDERS, carrierAccountLabel, cloudonixCredentials, hasAccountId } from "@/lib/callingApps";

export type CallingAccount = { kind: "carrier"; record: Carrier } | { kind: "telephony_config"; record: TelephonyConfig };

// Mount a fresh form for each account so secrets never carry across dialogs.
export function CallingAppModal({ tenantId, provider, account, onClose, onSaved }: {
  tenantId: string; provider: string; account?: CallingAccount;
  onClose: () => void; onSaved: (name: string) => void;
}) {
  const carrier = account?.kind === "carrier" ? account.record : null;
  const config = account?.kind === "telephony_config" ? account.record : null;
  const label = callingAppLabel(provider);
  const isCarrier = CARRIER_PROVIDERS.includes(provider);
  const isNative = provider === "native";
  const [name, setName] = useState(account?.record.name ?? "");
  const [accountId, setAccountId] = useState(carrier?.auth_id ?? String(config?.credentials.auth_id ?? ""));
  const [domain, setDomain] = useState(String(config?.credentials.domain ?? ""));
  const [secret, setSecret] = useState("");
  const [extraLabel, setExtraLabel] = useState(carrier?.carrier_account_ref ?? "");
  const [defaultOutbound, setDefaultOutbound] = useState(config?.is_default_outbound ?? false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Keep stored secret references out of editable fields and rename requests.
  const storedSecret = isCarrier ? carrier?.auth_token_ref : provider === "cloudonix"
    ? (config?.credentials.api_keys as unknown[] | undefined)?.[0] : config?.credentials.auth_token;
  const hasSecret = typeof storedSecret === "string" && storedSecret.length > 0;
  const hasIdentity = provider === "cloudonix" ? !!domain.trim() : hasAccountId(accountId, carrier?.auth_id, !!carrier);
  const valid = !!name.trim() && (isNative || ((!!secret.trim() || hasSecret || !!carrier) && hasIdentity));

  const save = async () => {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      if (isCarrier) {
        const key = secretPayload(secret.trim());
        const body = {
          name: name.trim(),
          ...(secret.trim() ? { auth_token: key.api_key, auth_token_ref: key.api_key_ref } : {}),
        };
        if (carrier) await updateCarrier(carrier.id, { ...body, ...(accountId.trim() ? { auth_id: accountId.trim() } : {}), carrier_account_ref: carrierAccountLabel(extraLabel, true) });
        else await createCarrier(tenantId, { ...body, auth_id: accountId.trim(), provider: provider as CarrierProvider, carrier_account_ref: carrierAccountLabel(extraLabel, false) });
      } else {
        const credentials = isNative ? {} : provider === "cloudonix"
          ? cloudonixCredentials(domain, secret, config?.credentials.api_keys as unknown[] | undefined)
          : { auth_id: accountId.trim(), auth_token: secret.trim() || "[stored]" };
        if (config) await updateTelephonyConfig(config.id, { name: name.trim(), credentials, is_default_outbound: defaultOutbound });
        else await createTelephonyConfig(tenantId, { name: name.trim(), provider, credentials, is_default_outbound: defaultOutbound });
      }
      onSaved(name.trim());
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal className="connected-app-dialog" trapFocus open title={`${account ? "Manage" : "Connect"} ${label}`} onClose={() => { if (!busy) onClose(); }} footer={
      <>
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={onClose}>Cancel</button>
        <button className="btn btn-primary btn-sm" disabled={busy || !valid} onClick={save}>
          {busy ? "Saving…" : account ? "Save changes" : `Connect ${label}`}
        </button>
      </>
    }>
      <p className="form-hint" style={{ marginBottom: 16 }}>
        {isNative ? "No outside account needed. Connect to use this platform’s own phone system."
          : `Paste the details from your ${label} account. Next, go to Phone Numbers to buy or add a number.`}
      </p>
      {error && <div className="error-banner" role="alert">{error}</div>}
      <div className="form-group">
        <label className="form-label" htmlFor="calling-name">Name for this connection <span className="required">*</span></label>
        <input id="calling-name" className="form-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Main line" />
        <div className="form-hint">So you can tell it apart if you connect more than one.</div>
      </div>
      {!isNative && <>
        <div className="form-group">
          <label className="form-label" htmlFor="calling-account">{provider === "cloudonix" ? "Domain" : "Account ID"} {carrier ? <span className="hint">optional</span> : <span className="required">*</span>}</label>
          <input id="calling-account" className="form-input" value={provider === "cloudonix" ? domain : accountId}
            onChange={(e) => provider === "cloudonix" ? setDomain(e.target.value) : setAccountId(e.target.value)} />
        </div>
        <div className="form-group">
          <label className="form-label" htmlFor="calling-secret">Secret key <span className="hint">kept private — never shown again</span></label>
          <input id="calling-secret" className="form-input" type="password" autoComplete="new-password" value={secret}
            placeholder={hasSecret ? "Saved — leave blank to keep" : ""} onChange={(e) => setSecret(e.target.value)} />
          {provider === "cloudonix" && config && <div className="form-hint">A new key replaces the first key. Other existing keys are kept.</div>}
        </div>
        {isCarrier && <div className="form-group">
          <label className="form-label" htmlFor="calling-extra">Extra account label <span className="hint">optional</span></label>
          <input id="calling-extra" className="form-input" value={extraLabel} onChange={(e) => setExtraLabel(e.target.value)} />
        </div>}
        {!isCarrier && <div className="form-group">
          <label className="form-label"><input type="checkbox" checked={defaultOutbound} onChange={(e) => setDefaultOutbound(e.target.checked)} /> Use by default for outgoing calls</label>
          <div className="form-hint">Used when a test call or campaign has no phone connection selected.</div>
        </div>}
      </>}
    </Modal>
  );
}
