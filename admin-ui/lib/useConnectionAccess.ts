"use client";

import { useCallback, useEffect, useState } from "react";
import { getCurrentUser } from "./api";

export function useConnectionAccess() {
  const [role, setRole] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const retry = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const user = await getCurrentUser();
      setRole(user.role);
    } catch {
      setRole(null);
      setError("Could not check your permissions. Please retry.");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void retry();
  }, [retry]);
  return { loading, error, retry, canManage: role === "admin" || role === "superadmin", isSuperadmin: role === "superadmin", checked: role !== null };
}
