// The bearer token lives in sessionStorage: it is gone when the tab closes, and expires after 15 minutes anyway.
const KEY = "priorauth.token";

export function getToken(): string | null {
  try {
    const token = sessionStorage.getItem(KEY);
    return token && !isExpired(token) ? token : null;
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    sessionStorage.setItem(KEY, token);
  } catch {
    // storage blocked: the session lasts until the next navigation
  }
}

export function clearToken(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // nothing stored
  }
}

/** True if the JWT's exp claim is in the past (the signature is the API's business, not the browser's). */
export function isExpired(token: string, nowSeconds: number = Date.now() / 1000): boolean {
  const payload = token.split(".")[1];
  if (!payload) return true;
  try {
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    const exp = (JSON.parse(json) as { exp?: unknown }).exp;
    return typeof exp !== "number" || exp <= nowSeconds;
  } catch {
    return true;
  }
}
