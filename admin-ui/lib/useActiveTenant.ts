"use client";

// Which account a page should be showing.
//
// Pages used to answer this by querying EVERY tenant and merging the
// results — one HTTP request per tenant, per resource. That is fine at five
// accounts and fatal at eight hundred: the browser's connection cap starts
// aborting requests and the page fills with "TypeError: Failed to fetch".
//
// The header already has a tenant switcher; this makes it mean something.
// A tenant-scoped user gets their own account and nothing else. A
// platform-scoped one (superadmin, tenant_id IS NULL) gets whichever account
// the switcher has selected — one account at a time, which is also how
// someone actually reads this data.

import { useCallback, useEffect, useState } from "react";
import { Tenant, getCurrentUser, listTenants } from "@/lib/api";
import { ACTIVE_TENANT_STORAGE_KEY } from "@/components/AppShell";

/** AppShell dispatches this when the switcher changes, so open pages
 *  re-query instead of showing the previous account until a reload. */
export const ACTIVE_TENANT_EVENT = "yuviz:active-tenant";

export interface ActiveTenant {
  tenant: Tenant | null;
  /** Every account the viewer may see — for switchers and "all accounts" copy. */
  allTenants: Tenant[];
  isPlatformScoped: boolean;
  loading: boolean;
  error: string | null;
}

export function useActiveTenant(): ActiveTenant {
  const [allTenants, setAllTenants] = useState<Tenant[]>([]);
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [isPlatformScoped, setIsPlatformScoped] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const pick = useCallback((tenants: Tenant[], platformScoped: boolean): Tenant | null => {
    if (tenants.length === 0) return null;
    if (!platformScoped) return tenants[0]; // listTenants already narrows to their own
    const stored =
      typeof window !== "undefined" ? window.localStorage.getItem(ACTIVE_TENANT_STORAGE_KEY) : null;
    return tenants.find((t) => t.slug === stored) ?? tenants[0];
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getCurrentUser(), listTenants()])
      .then(([me, tenants]) => {
        if (cancelled) return;
        const platformScoped = me.tenant_id === null;
        setIsPlatformScoped(platformScoped);
        setAllTenants(tenants);
        setTenant(pick(tenants, platformScoped));
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [pick]);

  useEffect(() => {
    const onSwitch = () => setTenant((prev) => pick(allTenants, isPlatformScoped) ?? prev);
    window.addEventListener(ACTIVE_TENANT_EVENT, onSwitch);
    return () => window.removeEventListener(ACTIVE_TENANT_EVENT, onSwitch);
  }, [allTenants, isPlatformScoped, pick]);

  return { tenant, allTenants, isPlatformScoped, loading, error };
}
