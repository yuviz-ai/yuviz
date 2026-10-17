"use client";

// Call Flows (call_flows table), shareable by several agents via agents.call_flow_id.
// An agent's own conversation graph lives at /workflows/{tenant}/{agent} and isn't listed here.
// Numbers and call totals come from the agents a menu answers for; calls don't record the menu.

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Grid3x3, MoreVertical, Pencil, Plus, Trash2 } from "lucide-react";
import { Agent, AgentCallStats, ApiError, PhoneNumber, getAgentCallStats, listAgents, listPhoneNumbers } from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";
import { useMenuKeys } from "@/lib/useMenuKeys";
import { CallFlowSummary, deleteCallFlow, listCallFlows } from "@/lib/callFlowApi";
import { CallStats, CallStatsGrid, NO_CALLS, sumCallStats } from "@/components/CallStatsGrid";

interface FlowRow extends CallFlowSummary {
  tenantSlug: string;
  tenantName: string;
}

interface Linked {
  agents: Agent[] | null; // null = the lookup failed; show nothing rather than "none"
  numbers: string[] | null;
  calls: CallStats | null;
}

const DIRECTION: Record<CallFlowSummary["direction"], { label: string; badge: string }> = {
  inbound: { label: "Inbound", badge: "cyan" },
  outbound: { label: "Outbound", badge: "indigo" },
  both: { label: "Both directions", badge: "gray" },
};

function FlowCardMenu({ onOpen, onDelete, deleting }: { onOpen: () => void; onDelete: () => void; deleting: boolean }) {
  const [open, setOpen] = useState(false);
  const { triggerRef, menuRef, onMenuKey } = useMenuKeys(open, () => setOpen(false));
  return (
    <div className="ed2-menu">
      <button
        ref={triggerRef}
        className="agent-card-icon-btn"
        aria-label="More actions"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <MoreVertical size={15} />
      </button>
      {open && (
        <>
          <div className="ed2-menu-backdrop" onClick={() => setOpen(false)} />
          <div ref={menuRef} className="ed2-menu-pop" role="menu" onKeyDown={onMenuKey}>
            <button role="menuitem" onClick={onOpen}>
              <Pencil size={13} /> Edit menu
            </button>
            <button role="menuitem" disabled={deleting} onClick={() => { setOpen(false); onDelete(); }}>
              <Trash2 size={13} /> {deleting ? "Deleting…" : "Delete"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export default function CallFlowsPage() {
  const router = useRouter();
  const { tenant, allTenants, isPlatformScoped, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const [flows, setFlows] = useState<FlowRow[]>([]);
  const [linked, setLinked] = useState<Record<string, Linked>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);

  // Each tenant's fetch fails independently so one bad account never blanks the rest.
  useEffect(() => {
    if (tenantLoading) return;
    let cancelled = false;
    const targets = isAllTenants ? allTenants : tenant ? [tenant] : [];
    if (targets.length === 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setFlows([]);
      setLinked({});
      setLoading(false);
      return;
    }

    setLoading(true);
    setLinked({});
    (async () => {
      const results = await Promise.allSettled(
        targets.map((t) => listCallFlows(t.slug).then((rows) => ({ t, rows }))),
      );
      const nextFlows: FlowRow[] = [];
      const errs: string[] = [];
      results.forEach((r, i) => {
        if (r.status === "fulfilled") {
          nextFlows.push(
            ...r.value.rows.map((f) => ({ ...f, tenantSlug: r.value.t.slug, tenantName: r.value.t.name })),
          );
        } else {
          errs.push(`${targets[i].name}: ${r.reason instanceof ApiError ? r.reason.detail : String(r.reason)}`);
        }
      });
      if (cancelled) return;
      setFlows(nextFlows);
      setError(errs.length > 0 ? errs.join("; ") : null);
      setLoading(false);

      const perTenant = new Map(
        await Promise.all(
          targets.map(async (t) => {
            const [agents, numbers, stats] = await Promise.all([
              listAgents(t.slug).catch(() => null),
              listPhoneNumbers(t.id).catch((): PhoneNumber[] | null => null),
              getAgentCallStats(t.slug).catch((): AgentCallStats[] | null => null),
            ]);
            return [t.id, { agents, numbers, stats }] as const;
          }),
        ),
      );
      if (cancelled) return;
      setLinked(
        Object.fromEntries(
          nextFlows.map((f) => {
            const data = perTenant.get(f.tenant_id);
            const agents = data?.agents ? data.agents.filter((a) => a.call_flow_id === f.id) : null;
            const ids = new Set(agents?.map((a) => a.id));
            const numbers = agents && data?.numbers
              ? data.numbers.filter((n) => n.agent_id && ids.has(n.agent_id)).map((n) => n.did)
              : null;
            const calls = agents && data?.stats
              ? sumCallStats(agents.map((a) => data.stats!.find((s) => s.agent_id === a.id) ?? NO_CALLS))
              : null;
            return [f.id, { agents, numbers, calls }];
          }),
        ),
      );
    })();
    return () => { cancelled = true; };
  }, [tenant, allTenants, isAllTenants, tenantLoading]);

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = q
      ? flows.filter((f) => `${f.name} ${f.tenantName}`.toLowerCase().includes(q))
      : flows;
    return [...matched].sort(
      (a, b) => Number(b.is_published) - Number(a.is_published) || a.name.localeCompare(b.name),
    );
  }, [flows, search]);

  const remove = async (f: FlowRow) => {
    const msg =
      `Delete "${f.name}"? Any agents attached to it go back to answering directly, ` +
      `and its published versions go with it.`;
    if (!window.confirm(msg)) return;
    setDeleting(f.id);
    try {
      await deleteCallFlow(f.id);
      setFlows((fs) => fs.filter((x) => x.id !== f.id));
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setDeleting(null);
    }
  };

  const count = `${flows.length} IVR-Call${flows.length === 1 ? "" : "s"}`;
  const accountLine = isAllTenants
    ? `${count} across ${allTenants.length} account${allTenants.length === 1 ? "" : "s"}.`
    : tenant
      ? `${count} in ${tenant.name}.${isPlatformScoped ? " Switch accounts from the header." : ""}`
      : "";

  return (
    <>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", gap: 12, marginBottom: 18 }}>
        <div>
          <h1 style={{ fontSize: "1.5rem", fontWeight: 600, margin: 0 }}>IVR-Call</h1>
          <div className="form-hint" style={{ marginTop: 4 }}>
            {accountLine} A menu answers before your AI agent: it plays messages, listens for keypresses and
            sends the caller on.
          </div>
        </div>
        <input
          className="form-input"
          style={{ width: 200, marginLeft: "auto" }}
          placeholder="Search menus…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {error && <div className="error-banner">{error}</div>}

      {loading ? (
        <div className="empty-state">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="agent-empty">
          <div className="agent-empty-ico"><Grid3x3 size={22} /></div>
          <h2>{search.trim() ? "No menus match your search" : "Create your first IVR-Call"}</h2>
          <p>
            {search.trim()
              ? "Try a different name, or clear the search."
              : "An IVR-Call answers the call, plays options like “press 1 for sales” and sends the caller on."}
          </p>
          {!search.trim() && (
            <button className="btn btn-primary btn-sm" onClick={() => router.push("/ivr-call/new")}>
              <Plus size={13} /> New IVR-Call
            </button>
          )}
        </div>
      ) : (
        <div className="agent-card-grid">
          {rows.map((f) => {
            const l = linked[f.id];
            const dir = DIRECTION[f.direction];
            const url = `/ivr-call/${f.id}`;
            // The call runtime only plays published inbound menus; it ignores status.
            const live = f.is_published && f.direction === "inbound" && (l?.agents?.length ?? 0) > 0;
            const status = live
              ? "Live"
              : !f.is_published
                ? "Draft"
                : f.direction === "inbound" && l?.agents == null
                  ? "Agent links unknown"
                  : "Not playing on calls";
            return (
              <div key={f.id} className={`agent-card${live ? "" : " paused"}`}>
                <div className="agent-card-top">
                  <div className="agent-card-avatar"><Grid3x3 size={22} /></div>
                  <FlowCardMenu onOpen={() => router.push(url)} onDelete={() => remove(f)} deleting={deleting === f.id} />
                </div>

                <Link href={url} className="agent-card-name agent-card-link">{f.name}</Link>
                {isAllTenants && <div className="agent-card-acct">{f.tenantName}</div>}

                <div className="agent-card-meta">
                  <span className={`badge ${dir.badge}`}>{dir.label}</span>
                  {l?.numbers != null && (l.numbers.length === 0
                    ? <span className="badge gray">No number</span>
                    : l.numbers.map((n) => <span key={n} className="badge green">{n}</span>))}
                </div>

                <div className="agent-card-facts">
                  {l?.agents != null && (l.agents.length === 0
                    ? <span className="muted">No agent attached</span>
                    : <span>Answers for {l.agents.map((a) => a.name).join(", ")}</span>)}
                  <span>{f.is_published ? `Version ${f.config_version}` : "Not published yet"}</span>
                  {f.is_published && f.has_draft && <span>Unpublished changes</span>}
                </div>

                {l?.calls && <CallStatsGrid stats={l.calls} />}

                <div className="agent-card-foot">
                  <span className={`agent-card-status${live ? " on" : ""}`}>
                    <i />{status}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
