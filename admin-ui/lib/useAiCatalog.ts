"use client";

import { useEffect, useState } from "react";
import { AiCatalog, ApiError, getAiCatalog } from "./api";

// The platform models a workspace may pick; null tenant = don't load.
export function useAiCatalog(tenantId: string | null | undefined) {
  const [state, setState] = useState<{ tenantId: string; catalog: AiCatalog | null; error: string | null } | null>(null);
  useEffect(() => {
    if (!tenantId) return;
    let cancelled = false;
    getAiCatalog(tenantId)
      .then((catalog) => { if (!cancelled) setState({ tenantId, catalog, error: null }); })
      .catch((e) => { if (!cancelled) setState({ tenantId, catalog: null, error: e instanceof ApiError ? e.detail : String(e) }); });
    return () => { cancelled = true; };
  }, [tenantId]);
  const current = state?.tenantId === tenantId ? state : null;
  return { catalog: current?.catalog ?? null, error: current?.error ?? null };
}
