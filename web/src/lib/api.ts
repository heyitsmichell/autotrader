/**
 * The one HTTP client for the dashboard (QA M21 / blocker B2).
 *
 * Every non-GET request carries `X-Trader: 1` and the per-launch `X-Trader-Token`. The token
 * comes from `<meta name="trader-token">`, which the backend injects into the served HTML.
 * In `next dev` (no injected meta) it falls back to `GET /api/auth/token`, which the backend
 * only answers when `X-Trader: 1` is present. A 403 token error triggers one refetch of the
 * token and one retry, so a backend restart (new token) does not need a page reload.
 */

export type Api = <T = unknown>(path: string, method?: string, body?: unknown) => Promise<T>;

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

let cachedToken: string | null = null;
let pendingToken: Promise<string | null> | null = null;

function metaToken(): string | null {
  if (typeof document === "undefined") return null;
  const el = document.querySelector('meta[name="trader-token"]');
  const value = el?.getAttribute("content")?.trim();
  return value ? value : null;
}

async function fetchToken(): Promise<string | null> {
  try {
    const res = await fetch("/api/auth/token", { headers: { "X-Trader": "1" }, cache: "no-store" });
    if (!res.ok) return null;
    const data = (await res.json().catch(() => ({}))) as { token?: string };
    return data.token || null;
  } catch {
    return null;
  }
}

/** The API token: the injected meta tag, else (dev mode) the token endpoint. Cached. */
export async function getToken(force = false): Promise<string | null> {
  if (force) {
    cachedToken = null;
    pendingToken = null;
  }
  if (cachedToken) return cachedToken;
  const fromMeta = force ? null : metaToken();
  if (fromMeta) {
    cachedToken = fromMeta;
    return cachedToken;
  }
  if (!pendingToken) {
    pendingToken = fetchToken().then(t => {
      cachedToken = t;
      pendingToken = null;
      return t;
    });
  }
  return pendingToken;
}

async function send(path: string, method: string, body: unknown, token: string | null): Promise<Response> {
  const headers: Record<string, string> = {};
  if (method !== "GET") {
    headers["X-Trader"] = "1";
    headers["Content-Type"] = "application/json";
    if (token) headers["X-Trader-Token"] = token;
  }
  return fetch(path, {
    method,
    headers,
    cache: "no-store",
    body: method === "GET" ? undefined : JSON.stringify(body ?? {}),
  });
}

function errorText(data: unknown, res: Response): string {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map(d => (d as { msg?: string })?.msg || JSON.stringify(d)).join("; ");
  }
  return res.statusText || `Request failed (${res.status})`;
}

export const api: Api = async <T,>(path: string, method = "GET", body?: unknown): Promise<T> => {
  const upper = method.toUpperCase();
  let token = upper === "GET" ? null : await getToken();
  let res = await send(path, upper, body, token);
  if (res.status === 403 && upper !== "GET") {
    // The backend restarted (new per-launch token) or the cached token is stale: retry once.
    const fresh = await getToken(true);
    if (fresh && fresh !== token) {
      token = fresh;
      res = await send(path, upper, body, token);
    }
  }
  let data: unknown;
  let parsed = true;
  try { data = await res.json(); } catch { data = {}; parsed = false; }
  if (!res.ok) throw new ApiError(errorText(data, res), res.status);
  if (!parsed) throw new ApiError("Unexpected non-JSON response from server", res.status);
  return data as T;
};
