"use client";

import { useEffect, useState } from "react";
import {
  ApiError,
  AvailableNumber,
  Carrier,
  CarrierProvider,
  Tenant,
  createCarrier,
  createPhoneNumber,
  listCarriers,
  listTenants,
  purchaseNumber,
  searchAvailableNumbers,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

const PROVIDER_LABEL: Record<CarrierProvider, string> = {
  twilio: "Twilio",
  plivo: "Plivo",
  vonage: "Vonage",
};

// Tenant-wide trunk/carrier management, moved out of the per-agent SIP tab
// (2026-09) — carriers were never actually agent-scoped (createCarrier only
// ever took a tenantId), so managing them required drilling into one
// specific agent's settings for a concept that applies to the whole
// account. Assigning a DID to an agent stays on /phone-numbers, which
// already does that tenant-wide for both purchased and manually-entered
// numbers — this page only owns carriers and buying new numbers from them.
export default function TelephonyPage() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantId, setTenantId] = useState("");

  const [carriers, setCarriers] = useState<Carrier[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [addingCarrier, setAddingCarrier] = useState(false);
  const [carrierName, setCarrierName] = useState("");
  const [carrierProvider, setCarrierProvider] = useState<CarrierProvider>("twilio");
  const [carrierAuthId, setCarrierAuthId] = useState("");
  const [carrierAuthTokenRef, setCarrierAuthTokenRef] = useState("");
  const [savingCarrier, setSavingCarrier] = useState(false);
  const [carrierError, setCarrierError] = useState<string | null>(null);

  const [buying, setBuying] = useState(false);
  const [buyCarrierId, setBuyCarrierId] = useState("");
  const [buyCountry, setBuyCountry] = useState("US");
  const [buyAreaCode, setBuyAreaCode] = useState("");
  const [searchResults, setSearchResults] = useState<AvailableNumber[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [purchasingNumber, setPurchasingNumber] = useState<string | null>(null);
  const [buyError, setBuyError] = useState<string | null>(null);
  const [lastPurchased, setLastPurchased] = useState<string | null>(null);

  useEffect(() => {
    listTenants().then((ts) => {
      setTenants(ts);
      if (ts.length > 0) setTenantId(ts[0].id);
    });
  }, []);

  const refresh = () => {
    if (!tenantId) return;
    setLoading(true);
    setError(null);
    listCarriers(tenantId)
      .then(setCarriers)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(refresh, [tenantId]);

  const openAddCarrier = () => {
    setCarrierName("");
    setCarrierProvider("twilio");
    setCarrierAuthId("");
    setCarrierAuthTokenRef("");
    setCarrierError(null);
    setAddingCarrier(true);
  };

  const handleSaveCarrier = async () => {
    setSavingCarrier(true);
    setCarrierError(null);
    try {
      await createCarrier(tenantId, {
        name: carrierName,
        provider: carrierProvider,
        auth_id: carrierAuthId || undefined,
        auth_token_ref: carrierAuthTokenRef || undefined,
      });
      setAddingCarrier(false);
      refresh();
    } catch (e) {
      setCarrierError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSavingCarrier(false);
    }
  };

  const openBuy = () => {
    setBuyCarrierId(carriers[0]?.id ?? "");
    setBuyCountry("US");
    setBuyAreaCode("");
    setSearchResults(null);
    setBuyError(null);
    setLastPurchased(null);
    setBuying(true);
  };

  const handleSearch = async () => {
    setSearching(true);
    setBuyError(null);
    setSearchResults(null);
    try {
      const results = await searchAvailableNumbers(tenantId, {
        carrier_id: buyCarrierId,
        country: buyCountry,
        area_code: buyAreaCode || undefined,
        limit: 10,
      });
      setSearchResults(results);
    } catch (e) {
      setBuyError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSearching(false);
    }
  };

  const handlePurchase = async (phoneNumber: string) => {
    if (!confirm(`Purchase ${phoneNumber}? This charges your carrier account for real.`)) return;
    setPurchasingNumber(phoneNumber);
    setBuyError(null);
    try {
      await purchaseNumber(tenantId, { carrier_id: buyCarrierId, phone_number: phoneNumber });
      // Config Service owns DID->agent routing (phone_numbers) — DID Service
      // only owns the carrier purchase itself, per architecture principle #7.
      // Lands unassigned; assign it to an agent from /phone-numbers, same as
      // any manually-entered DID.
      await createPhoneNumber(tenantId, { did: phoneNumber, carrier_id: buyCarrierId, status: "active" });
      setLastPurchased(phoneNumber);
      setSearchResults((prev) => prev?.filter((r) => r.phone_number !== phoneNumber) ?? null);
    } catch (e) {
      setBuyError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setPurchasingNumber(null);
    }
  };

  return (
    <>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 14, gap: 10 }}>
        <select className="form-select" style={{ width: 240 }} value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
          {tenants.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="cols">
        <div className="col-main">
          <div className="card">
            <div className="card-hdr">
              <div className="card-title">Trunk Provider</div>
              <div className="card-sub">carriers this account can buy numbers from</div>
              <button className="btn btn-primary btn-sm" style={{ marginLeft: "auto" }} onClick={openAddCarrier} disabled={!tenantId}>
                + Add Carrier
              </button>
            </div>

            {loading && <div className="empty-state">Loading…</div>}

            {!loading &&
              carriers.map((c) => (
                <div key={c.id} className="kb-row">
                  <div style={{ flex: 1 }}>
                    <div style={{ fontWeight: 500 }}>{c.name}</div>
                    <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>{c.auth_id ?? "no auth_id set"}</div>
                  </div>
                  <span className="badge indigo">{PROVIDER_LABEL[c.provider]}</span>
                </div>
              ))}

            {!loading && carriers.length === 0 && <div className="empty-state">No carriers configured yet.</div>}
          </div>

          <div className="card" style={{ marginTop: 16 }}>
            <div className="card-hdr">
              <div className="card-title">Buy a Number</div>
              <div className="card-sub">search and purchase a new DID from a carrier</div>
              <button
                className="btn btn-primary btn-sm"
                style={{ marginLeft: "auto" }}
                onClick={openBuy}
                disabled={carriers.length === 0}
                title={carriers.length === 0 ? "Add a carrier first" : "Buy a new number"}
              >
                + Buy a Number
              </button>
            </div>
            <div className="empty-state">
              Purchased numbers land unassigned — assign them to an agent from{" "}
              <a href="/phone-numbers">Phone Numbers</a>.
            </div>
          </div>
        </div>
      </div>

      <Modal
        open={addingCarrier}
        title="Add Carrier"
        onClose={() => setAddingCarrier(false)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setAddingCarrier(false)}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" onClick={handleSaveCarrier} disabled={savingCarrier || !carrierName}>
              {savingCarrier ? "Adding…" : "Add Carrier"}
            </button>
          </>
        }
      >
        {carrierError && <div className="error-banner">{carrierError}</div>}
        <div className="form-group">
          <label className="form-label">
            Name <span className="required">*</span>
          </label>
          <input className="form-input" value={carrierName} onChange={(e) => setCarrierName(e.target.value)} />
        </div>
        <div className="form-group">
          <label className="form-label">Provider</label>
          <select
            className="form-input"
            value={carrierProvider}
            onChange={(e) => setCarrierProvider(e.target.value as CarrierProvider)}
          >
            <option value="twilio">Twilio</option>
            <option value="plivo">Plivo</option>
            <option value="vonage">Vonage</option>
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">
            Account SID / Auth ID <span className="hint">e.g. AC... for Twilio, Auth ID for Plivo</span>
          </label>
          <input className="form-input" value={carrierAuthId} onChange={(e) => setCarrierAuthId(e.target.value)} />
        </div>
        <div className="form-group">
          <label className="form-label">
            Auth Token Reference <span className="hint">e.g. env:TWILIO_AUTH_TOKEN — never a raw secret</span>
          </label>
          <input
            className="form-input"
            style={{ fontFamily: "var(--mono)" }}
            value={carrierAuthTokenRef}
            onChange={(e) => setCarrierAuthTokenRef(e.target.value)}
            placeholder="env:TWILIO_AUTH_TOKEN"
          />
        </div>
      </Modal>

      <Modal
        open={buying}
        title="Buy a Number"
        onClose={() => setBuying(false)}
        footer={
          <button className="btn btn-ghost btn-sm" onClick={() => setBuying(false)}>
            Close
          </button>
        }
      >
        {buyError && <div className="error-banner">{buyError}</div>}
        {lastPurchased && (
          <div className="empty-state" style={{ color: "var(--green, green)" }}>
            Purchased {lastPurchased} — unassigned. Assign it to an agent from Phone Numbers.
          </div>
        )}
        <div className="form-group">
          <label className="form-label">Carrier</label>
          <select className="form-input" value={buyCarrierId} onChange={(e) => setBuyCarrierId(e.target.value)}>
            {carriers.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({PROVIDER_LABEL[c.provider]})
              </option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">Country</label>
          <input className="form-input" value={buyCountry} onChange={(e) => setBuyCountry(e.target.value.toUpperCase())} maxLength={2} />
        </div>
        <div className="form-group">
          <label className="form-label">Area Code (optional)</label>
          <input className="form-input" value={buyAreaCode} onChange={(e) => setBuyAreaCode(e.target.value)} placeholder="415" />
        </div>
        <button className="btn btn-primary btn-sm" onClick={handleSearch} disabled={searching || !buyCarrierId}>
          {searching ? "Searching…" : "Search"}
        </button>

        {searchResults !== null && (
          <div style={{ marginTop: 16 }}>
            {searchResults.length === 0 && <div className="empty-state">No numbers found — try a different area code.</div>}
            {searchResults.map((r) => (
              <div key={r.phone_number} className="kb-row">
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 500 }}>{r.phone_number}</div>
                  <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                    {r.region ?? "—"} · {r.capabilities.join(", ")}
                  </div>
                </div>
                <button
                  className="btn btn-primary btn-sm"
                  onClick={() => handlePurchase(r.phone_number)}
                  disabled={purchasingNumber !== null}
                >
                  {purchasingNumber === r.phone_number ? "Purchasing…" : "Purchase"}
                </button>
              </div>
            ))}
          </div>
        )}
      </Modal>
    </>
  );
}
