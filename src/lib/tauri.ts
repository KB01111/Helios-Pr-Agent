import { invoke } from "@tauri-apps/api/core";
import { openUrl as pluginOpenUrl } from "@tauri-apps/plugin-opener";

/** True when running inside the Tauri webview (false in a plain browser / vite preview). */
export const isTauri = () => typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

export async function tauriInvoke<T = any>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  if (!isTauri()) throw new Error("tauri-invoke-unavailable");
  return invoke<T>(cmd, args);
}

export function tauriUnavailable(err: unknown): boolean {
  return /tauri-invoke-unavailable/.test(String((err as any)?.message ?? err));
}

export async function openUrl(url: string): Promise<void> {
  try {
    if (isTauri()) {
      await pluginOpenUrl(url);
      return;
    }
  } catch {
    /* fall through to window.open */
  }
  window.open(url, "_blank", "noopener");
}

export function errText(e: unknown, max = 220): string {
  const m = typeof e === "string" ? e : ((e as any)?.message ?? String(e));
  return String(m).slice(0, max);
}

export function parseJsonLoose(t: unknown): any {
  if (typeof t !== "string") return t;
  try {
    return JSON.parse(t);
  } catch {
    return null;
  }
}

export function jsonObj(v: unknown): any | null {
  if (typeof v === "string") {
    const p = parseJsonLoose(v);
    return p !== null ? p : null;
  }
  return v && typeof v === "object" ? v : null;
}
