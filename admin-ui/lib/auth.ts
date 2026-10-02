// Config Service JWT storage. localStorage + bearer header, not a cookie, since the
// API is cross-origin and this avoids SameSite/CSRF handling.

const TOKEN_KEY = "yuviz_access_token";

export interface StoredUser {
  id: string;
  email: string;
  role: "superadmin" | "admin" | "viewer";
  tenant_id: string | null;
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}
