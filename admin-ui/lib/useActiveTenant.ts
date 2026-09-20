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
import { ACTIVE_TENANT_STORAGE_KEY, ALL_TENANTS_SENTINEL } from "@/components/AppShell";

/** AppShell dispatches this when the switcher changes, so open pages
 *  re-query instead of showing the previous account until a reload. */
export const ACTIVE_TENANT_EVENT = "yuviz:active-tenant";

export interface ActiveTenant {
  tenant: Tenant | null;
  /** Every account the viewer may see — for switchers and "all accounts" copy. */
  allTenants: Tenant[];
  isPlatformScoped: boolean;
  /** True only when a platform-scoped viewer explicitly chose "All tenants"
   *  (or has no stored preference yet, which defaults to it) — a page
   *  should fan its fetch out over `allTenants` instead of `tenant`.
   *  Distinct from `tenant === null`, which is also briefly true before
   *  the first load resolves and permanently true for a tenant-scoped
   *  viewer with somehow zero tenants — neither of those means "show
   *  everything". */
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
    // listTenants() already narrows a tenant-scoped user to their own row;
    // only a platform-scoped viewer (superadmin) ever sees "All tenants".
    const stored =
      typeof window !== "undefined" ? window.localStorage.getItem(ACTIVE_TENANT_STORAGE_KEY) : null;
    if (stored && stored !== ALL_TENANTS_SENTINEL) {
      const found = tenants.find((t) => t.slug === stored);
      if (found) return { tenant: found, isAllTenants: false };
    }
    // No stored preference, an explicit "All tenants", or a stale slug
    // pointing at a deleted tenant — all default to "All tenants" rather
    // than a silently-picked tenants[0], which read as the switcher
    // working for whichever tenant happened to sort first.
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
