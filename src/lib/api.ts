import { useApp } from "./store";
import { parseJsonLoose, tauriInvoke, tauriUnavailable } from "./tauri";

/* Backend access: the Rust proxy first (no CORS, token stays out of the webview
 * network stack), falling back to fetch() when running outside Tauri. */

export const backendUrl = () => useApp.getState().backendUrl;
const apiToken = () => useApp.getState().secrets.api;

function fetchOptions(extra?: RequestInit): RequestInit {
  const opts: RequestInit = { ...(extra || {}) };
  if (typeof AbortSignal !== "undefined" && "timeout" in AbortSignal) {
    opts.signal = AbortSignal.timeout(15000);
  }
  return opts;
}

export async function apiGet<T = any>(path: string, baseOverride?: string): Promise<T> {
  const base = (baseOverride || backendUrl()).replace(/\/+$/, "");
  const token = apiToken();
  let raw: unknown;
  try {
    raw = await tauriInvoke("backend_mutate", {
      method: "GET",
      backendUrl: base,
      path,
      bodyJson: null,
      apiToken: token || null,
    });
  } catch (e) {
    if (!tauriUnavailable(e)) throw e;
  }
  if (raw !== undefined) {
    const d = parseJsonLoose(raw);
    if (d !== null) return d as T;
    throw new Error("backend returned a non-JSON response");
  }
  const headers: Record<string, string> = token ? { Authorization: "Bearer " + token } : {};
  const r = await fetch(base + path, fetchOptions({ headers }));
  if (!r.ok) throw new Error("HTTP " + r.status + " " + (await r.text()).slice(0, 300));
  return r.json() as Promise<T>;
}

export async function apiMut<T = any>(
  method: string,
  path: string,
  body?: unknown,
  baseOverride?: string,
  tokenOverride?: string,
): Promise<T> {
  const base = (baseOverride || backendUrl()).replace(/\/+$/, "");
  const token = tokenOverride !== undefined ? tokenOverride : apiToken();
  let raw: unknown;
  try {
    raw = await tauriInvoke("backend_mutate", {
      method,
      backendUrl: base,
      path,
      bodyJson: body === undefined ? null : JSON.stringify(body || {}),
      apiToken: token || null,
    });
  } catch (e) {
    if (!tauriUnavailable(e)) throw e;
  }
  if (raw !== undefined) {
    const d = parseJsonLoose(raw);
    return (d !== null ? d : raw) as T;
  }
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = "Bearer " + token;
  const r = await fetch(
    base + path,
    fetchOptions({
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body || {}),
    }),
  );
  if (!r.ok) throw new Error("HTTP " + r.status + " " + (await r.text()).slice(0, 300));
  const t = await r.text();
  return (t ? parseJsonLoose(t) : {}) as T;
}
