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
  let safeUrl: string;
  try {
    const parsed = new URL(url.trim());
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      console.warn("Refused to open URL with disallowed scheme:", parsed.protocol);
      return;
    }
    safeUrl = parsed.href;
  } catch {
    console.warn("Refused to open invalid URL");
    return;
  }
  try {
    if (isTauri()) {
      await pluginOpenUrl(safeUrl);
      return;
    }
  } catch {
    /* fall through to window.open */
  }
  window.open(safeUrl, "_blank", "noopener,noreferrer");
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

/* ---------- OS-protected local secret vault (Windows DPAPI) ---------- */

export type VaultKey =
  | "backend_api_token"
  | "github_token"
  | "openrouter_api_key"
  | "commandcode_api_key"
  | "opencode_go_api_key";

export const VAULT_KEYS: readonly VaultKey[] = [
  "backend_api_token",
  "github_token",
  "openrouter_api_key",
  "commandcode_api_key",
  "opencode_go_api_key",
] as const;

function isVaultUnsupported(err: unknown): boolean {
  return tauriUnavailable(err) || /command vault_.* not found|unknown command/i.test(String((err as any)?.message ?? err));
}

export async function vaultSaveSecret(key: VaultKey, value: string): Promise<boolean> {
  try {
    await tauriInvoke("vault_save_secret", { key, value });
    return true;
  } catch (e) {
    if (isVaultUnsupported(e)) return false;
    throw e;
  }
}

export async function vaultLoadSecrets(): Promise<Partial<Record<VaultKey, string>>> {
  try {
    const raw = await tauriInvoke<Record<string, unknown>>("vault_load_secrets");
    const obj = jsonObj(raw);
    if (!obj || typeof obj !== "object") return {};
    const out: Partial<Record<VaultKey, string>> = {};
    for (const k of VAULT_KEYS) {
      const v = obj[k];
      if (typeof v === "string" && v.trim()) out[k] = v.trim();
    }
    return out;
  } catch (e) {
    if (isVaultUnsupported(e)) return {};
    throw e;
  }
}

export async function vaultDeleteSecret(key: VaultKey): Promise<boolean> {
  try {
    await tauriInvoke("vault_delete_secret", { key });
    return true;
  } catch (e) {
    if (isVaultUnsupported(e)) return false;
    throw e;
  }
}

export async function vaultClearAll(): Promise<boolean> {
  try {
    await tauriInvoke("vault_clear_all");
    return true;
  } catch (e) {
    if (isVaultUnsupported(e)) return false;
    throw e;
  }
}

/* ---------- GitHub repo config / memory file IPC ---------- */

export interface CommitRepoFileResult {
  changed: boolean;
  sha: string;
  path: string;
}

export async function githubReadRepoFile(token: string, repo: string, path: string): Promise<string | null> {
  try {
    const res = await tauriInvoke<string | null>("github_read_repo_file", { token, repo, path });
    return typeof res === "string" ? res : null;
  } catch (e) {
    if (tauriUnavailable(e)) return null;
    throw e;
  }
}

export async function githubCommitRepoFile(
  token: string,
  repo: string,
  path: string,
  content: string,
  message: string,
): Promise<CommitRepoFileResult> {
  try {
    const raw = await tauriInvoke<CommitRepoFileResult>("github_commit_repo_file", {
      token,
      repo,
      path,
      content,
      message,
    });
    const obj = jsonObj(raw);
    if (!obj || typeof obj !== "object") {
      throw new Error("Unexpected response from github_commit_repo_file");
    }
    return {
      changed: Boolean(obj.changed),
      sha: String(obj.sha || ""),
      path: String(obj.path || path),
    };
  } catch (e) {
    if (tauriUnavailable(e)) {
      throw new Error("Committing files to GitHub requires the PR Agent desktop app (Tauri). Copy the preview content instead.");
    }
    throw e;
  }
}
