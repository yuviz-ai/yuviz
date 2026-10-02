"use client";

// Which account a page should show: a tenant-scoped user's own, or the header switcher's
// selection for a platform-scoped (superadmin) user.

import { useCallback, useEffect, useState } from "react";
import { Tenant, getCurrentUser, listTenants } from "@/lib/api";
import { ACTIVE_TENANT_STORAGE_KEY, ALL_TENANTS_SENTINEL } from "@/components/AppShell";

/** AppShell dispatches this when the switcher changes, so open pages
 *  re-query instead of showing the previous account until a reload. */
export const ACTIVE_TENANT_EVENT = "yuviz:active-tenant";

export interface ActiveTenant {
  tenant: Tenant | null;
  /** Every account the viewer may see — for switchers and "all accounts" copy. */
  allTenants: Tenant[];
  isPlatformScoped: boolean;
  /** Platform-scoped viewer selected "All tenants": fetch over `allTenants`.
   *  Not equivalent to `tenant === null`, which is also true while loading. */
  isAllTenants: boolean;
  loading: boolean;
  error: string | null;
}

interface Picked {
  tenant: Tenant | null;
  isAllTenants: boolean;
}

export function useActiveTenant(): ActiveTenant {
  const [allTenants, setAllTenants] = useState<Tenant[]>([]);
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [isAllTenants, setIsAllTenants] = useState(false);
  const [isPlatformScoped, setIsPlatformScoped] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const pick = useCallback((tenants: Tenant[], platformScoped: boolean): Picked => {
    if (!platformScoped) return { tenant: tenants[0] ?? null, isAllTenants: false };
    const stored =
      typeof window !== "undefined" ? window.localStorage.getItem(ACTIVE_TENANT_STORAGE_KEY) : null;
    if (stored && stored !== ALL_TENANTS_SENTINEL) {
      const found = tenants.find((t) => t.slug === stored);
      if (found) return { tenant: found, isAllTenants: false };
    }
    // Missing, "All tenants", or stale slug: default to all rather than silently picking tenants[0].
    return { tenant: null, isAllTenants: true };
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getCurrentUser(), listTenants()])
      .then(([me, tenants]) => {
        if (cancelled) return;
        const platformScoped = me.tenant_id === null;
        setIsPlatformScoped(platformScoped);
        setAllTenants(tenants);
        const picked = pick(tenants, platformScoped);
        setTenant(picked.tenant);
        setIsAllTenants(picked.isAllTenants);
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [pick]);

  useEffect(() => {
    const onSwitch = () => {
      const picked = pick(allTenants, isPlatformScoped);
      setTenant(picked.tenant);
      setIsAllTenants(picked.isAllTenants);
    };
    window.addEventListener(ACTIVE_TENANT_EVENT, onSwitch);
    return () => window.removeEventListener(ACTIVE_TENANT_EVENT, onSwitch);
  }, [allTenants, isPlatformScoped, pick]);

  return { tenant, allTenants, isPlatformScoped, isAllTenants, loading, error };
}
