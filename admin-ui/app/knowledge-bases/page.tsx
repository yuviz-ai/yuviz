"use client";

import { useEffect, useState } from "react";
import { ApiError, getCurrentUser, listTenants, Tenant } from "@/lib/api";
import { KbAgent, KnowledgeBase, listDocuments, listKbAgents, listKnowledgeBases } from "@/lib/knowledgeApi";
import { AddSourceModal } from "@/components/AddSourceModal";

interface KbRow extends KnowledgeBase {
  tenantName: string;
  tenantSlug: string;
}

export default function KnowledgeBasesPage() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [rows, setRows] = useState<KbRow[]>([]);
  const [docCountByKb, setDocCountByKb] = useState<Record<string, number>>({});
  const [agentsByKb, setAgentsByKb] = useState<Record<string, KbAgent[]>>({});
  const [tenantErrors, setTenantErrors] = useState<string[]>([]);
  const [canManage, setCanManage] = useState(false);
  const [loading, setLoading] = useState(true);
  // Fatal only for listTenants() — every fetch downstream of it depends on
  // the tenant list, so its failure alone renders a page-level error
  // (design's "Page data flow", lesson 21 for everything below it).
  const [pageError, setPageError] = useState<string | null>(null);
  const [addSourceOpen, setAddSourceOpen] = useState(false);

  const refresh = async () => {
    setLoading(true);
    setPageError(null);
    setTenantErrors([]);
    try {
      const fetchedTenants = await listTenants();
      setTenants(fetchedTenants);

      const kbResults = await Promise.allSettled(fetchedTenants.map((t) => listKnowledgeBases(t.id)));
      const nextRows: KbRow[] = [];
      const nextTenantErrors: string[] = [];
      kbResults.forEach((result, i) => {
        const t = fetchedTenants[i];
        if (result.status === "fulfilled") {
          nextRows.push(...result.value.map((kb) => ({ ...kb, tenantName: t.name, tenantSlug: t.slug })));
        } else {
          const reason = result.reason;
          nextTenantErrors.push(
            `Couldn't load knowledge bases for ${t.name}: ${reason instanceof ApiError ? reason.detail : String(reason)}`,
          );
        }
      });
      setRows(nextRows);
      setTenantErrors(nextTenantErrors);

      const [docResults, agentResults] = await Promise.all([
        Promise.allSettled(nextRows.map((kb) => listDocuments(kb.id))),
        Promise.allSettled(nextRows.map((kb) => listKbAgents(kb.id))),
      ]);
      const nextDocCounts: Record<string, number> = {};
      docResults.forEach((result, i) => {
        if (result.status === "fulfilled") nextDocCounts[nextRows[i].id] = result.value.length;
      });
      setDocCountByKb(nextDocCounts);
      const nextAgents: Record<string, KbAgent[]> = {};
      agentResults.forEach((result, i) => {
        if (result.status === "fulfilled") nextAgents[nextRows[i].id] = result.value;
      });
      setAgentsByKb(nextAgents);
    } catch (e) {
      setPageError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    getCurrentUser()
      .then((me) => setCanManage(me.role === "superadmin" || me.role === "admin"))
      .catch(() => {
        // Leave canManage false — same "fail closed on write controls" as
        // every other page's canManage check.
      });
  }, []);

  if (pageError) {
    return (
      <div className="error-banner">
        Couldn&apos;t load accounts: {pageError}{" "}
        <button className="btn btn-ghost btn-sm" onClick={refresh}>
          Retry
        </button>
      </div>
    );
  }

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 14, gap: 10 }}>
        <div />
        {canManage && (
          <button className="btn btn-primary btn-sm" onClick={() => setAddSourceOpen(true)}>
            + Add source
          </button>
        )}
      </div>

      {tenantErrors.map((msg) => (
        <div className="error-banner" key={msg}>
          {msg}
        </div>
      ))}

      <div className="card">
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : rows.length === 0 ? (
          <div className="empty-state">No knowledge bases yet.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Name</th>
                <th>Account</th>
                <th>Status</th>
                <th>Documents</th>
                <th>Attached agents</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((kb) => {
                const docCount = docCountByKb[kb.id];
                const agents = agentsByKb[kb.id];
                return (
                  <tr key={kb.id}>
                    <td className="bold">
                      <a href={`/knowledge-bases/${kb.tenantSlug}/${kb.id}`}>{kb.name}</a>
                    </td>
                    <td>{kb.tenantName}</td>
                    <td>
                      <span className={`badge ${kb.status === "active" ? "green" : "gray"}`}>{kb.status}</span>
                    </td>
                    <td>{docCount === undefined ? "—" : docCount}</td>
                    <td>
                      {agents === undefined
                        ? "—"
                        : agents.length === 0
                          ? "Not used by any agent"
                          : `${agents.length} agent${agents.length === 1 ? "" : "s"}`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {addSourceOpen && (
        <AddSourceModal
          tenants={tenants}
          knowledgeBases={rows}
          canManage={canManage}
          onClose={() => setAddSourceOpen(false)}
          onCreated={refresh}
        />
      )}
    </>
  );
}
