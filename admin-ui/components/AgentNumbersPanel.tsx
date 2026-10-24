"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Agent, ApiError, PhoneNumber, listPhoneNumbers, updatePhoneNumber } from "@/lib/api";

const STATUS_CLS: Record<PhoneNumber["status"], string> = { active: "green", inactive: "gray", suspended: "amber" };
const STATUS_LABEL: Record<PhoneNumber["status"], string> = { active: "Active", inactive: "Off", suspended: "Suspended" };
const MANAGE_URL = "/telephony";

// Only full international numbers can be shown to the people an agent calls.
const isInternational = (did: string) => /^\+?[1-9]\d{6,14}$/.test(did);
const asE164 = (did: string) => (did.startsWith("+") ? did : `+${did}`);
const errText = (e: unknown) => (e instanceof ApiError ? e.detail : String(e));

interface Props {
  tenantId: string;
  agentId: string;
  direction: Agent["direction"];
  outboundCallerId: string | null;
  onOutboundCallerId: (value: string | null) => void;
  onNumbers?: (numbers: PhoneNumber[]) => void;
}

function ManageNumbers() {
  return <Link href={MANAGE_URL} className="btn btn-ghost btn-sm" style={{ marginLeft: "auto" }}>Manage numbers</Link>;
}

export function AgentNumbersPanel({ tenantId, agentId, direction, outboundCallerId, onOutboundCallerId, onNumbers }: Props) {
  const [numbers, setNumbers] = useState<PhoneNumber[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      listPhoneNumbers(tenantId)
        .then((list) => {
          setNumbers(list);
          onNumbers?.(list);
        })
        .catch((e) => setError(errText(e))),
    [tenantId, onNumbers],
  );

  // Refetch on focus, so a number added under Manage numbers shows up on return.
  useEffect(() => {
    load();
    window.addEventListener("focus", load);
    return () => window.removeEventListener("focus", load);
  }, [load]);

  const attached = (numbers ?? []).filter((n) => n.agent_id === agentId);
  const current = attached[0] ?? null;
  const free = (numbers ?? []).filter((n) => !n.agent_id && n.status === "active");
  const callerIds = Array.from(new Set([
    ...(numbers ?? []).filter((n) => n.status === "active" && isInternational(n.did)).map((n) => asE164(n.did)),
    ...(outboundCallerId ? [outboundCallerId] : []),
  ]));

  // Attach first, then detach the old number, so the agent never ends up with neither.
  const attach = async (numberId: string) => {
    setSaving(true);
    setError(null);
    try {
      if (numberId) await updatePhoneNumber(numberId, { agent_id: agentId });
      if (current && current.id !== numberId) await updatePhoneNumber(current.id, { agent_id: null });
    } catch (e) {
      setError(errText(e));
    } finally {
      await load();
      setSaving(false);
    }
  };

  const showInbound = direction !== "outbound";
  const showOutbound = direction !== "inbound";
  const syncFailed = current?.provider_sync?.ok === false;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {error && <div className="error-banner">{error}</div>}

      {showInbound && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Inbound phone number</div>
            <div className="card-sub">people who call this number talk to this agent</div>
            <ManageNumbers />
          </div>
          <div style={{ padding: 16 }}>
            {numbers === null ? (
              <div className="form-hint">Loading numbers…</div>
            ) : !current && free.length === 0 ? (
              <div className="form-hint">
                No free numbers. Add one under <Link href={MANAGE_URL}>Manage numbers</Link>, then come back and choose it here.
              </div>
            ) : (
              <div className="form-group" style={{ marginBottom: 0, maxWidth: 420 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <select
                    aria-label="Inbound phone number"
                    className="form-select"
                    value={current?.id ?? ""}
                    disabled={saving}
                    onChange={(e) => attach(e.target.value)}
                  >
                    <option value="">No number</option>
                    {[...attached.slice(0, 1), ...free].map((n) => (
                      <option key={n.id} value={n.id}>
                        {n.did}
                        {n.region ? ` · ${n.region}` : ""}
                      </option>
                    ))}
                  </select>
                  {current && current.status !== "active" && (
                    <span className={`badge ${STATUS_CLS[current.status]}`}>{STATUS_LABEL[current.status]}</span>
                  )}
                </div>
                {syncFailed && (
                  <div className="form-hint" style={{ color: "var(--red)" }}>
                    Not receiving calls: {current?.provider_sync?.message ?? "the calling app couldn't route its calls to us"}. Fix it under{" "}
                    <Link href={MANAGE_URL}>Manage numbers</Link>.
                  </div>
                )}
                {attached.length > 1 && (
                  <div className="form-hint">Also answers {attached.slice(1).map((n) => n.did).join(", ")}.</div>
                )}
                <div className="form-hint">
                  Only numbers not used by another agent are listed. Choosing a different number stops the previous one from reaching this agent.
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {showOutbound && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Outbound caller ID</div>
            <div className="card-sub">the number people see when this agent calls them</div>
            <ManageNumbers />
          </div>
          <div style={{ padding: 16 }}>
            {numbers !== null && callerIds.length === 0 ? (
              <div className="form-hint">
                No numbers that can be shown to callers. Add one under <Link href={MANAGE_URL}>Manage numbers</Link>, then come back and choose it here.
              </div>
            ) : (
              <div className="form-group" style={{ marginBottom: 0, maxWidth: 420 }}>
                <select
                  id="outbound-caller-id"
                  aria-label="Outbound caller ID"
                  className="form-select"
                  value={outboundCallerId ?? ""}
                  disabled={numbers === null}
                  onChange={(e) => onOutboundCallerId(e.target.value || null)}
                >
                  <option value="">{numbers === null ? "Loading numbers…" : "Choose a number"}</option>
                  {callerIds.map((did) => (
                    <option key={did} value={did}>{did}</option>
                  ))}
                </select>
                <div className="form-hint">Campaigns that use this agent call from this number.</div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
