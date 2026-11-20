import { apiGet, apiMut } from "./api";
import { type LlmProviderId } from "./store";

/* Settings saved to the deployed FastAPI backend (PUT /config/settings), so a
 * provider/model/key chosen in the app takes effect on the cloud without
 * editing its environment or redeploying. Mirrors backend/runtime_settings.py. */

export const CLOUD_KEYS: ReadonlySet<string> = new Set([
  "LLM_PROVIDER",
  "LLM_MODEL",
  "OPENROUTER_API_KEY",
  "COMMANDCODE_API_KEY",
  "OPENCODE_GO_API_KEY",
  "GITHUB_TOKEN",
  "GITHUB_WEBHOOK_SECRET",
  "UPSTASH_BOX_API_KEY",
  "VERCEL_TOKEN",
  "VERCEL_TEAM_ID",
  "GITHUB_APP_ID",
  "GITHUB_APP_SLUG",
  "GITHUB_APP_PRIVATE_KEY",
  "GITHUB_APP_WEBHOOK_SECRET",
]);

export interface CloudSettings {
  persistent: boolean;
  encryption: boolean;
  values: Record<string, string>;
  cloud_keys: string[];
  secrets: Record<string, { set: boolean; cloud: boolean }>;
  errors: string[];
}

/** Save (string) or remove (null) settings on the cloud backend. */
export async function saveCloudSettings(values: Record<string, string | null>): Promise<CloudSettings> {
  return apiMut<CloudSettings>("PUT", "/config/settings", { values });
}

export const loadCloudSettings = () => apiGet<CloudSettings>("/config/settings");

/**
 * The backend wants the model id its provider's API expects. The `.pr_agent.toml`
 * / PR-Agent form carries a LiteLLM route prefix (`openrouter/vendor/model`) that
 * OpenRouter itself does not know. `openrouter/free` and `openrouter/auto` are
 * real OpenRouter router ids, so a prefix is only dropped when a vendor follows.
 * Command Code / OpenCode Go keep their `openai/` prefix; the backend strips it.
 */
export function cloudModelId(provider: LlmProviderId | string, model: string): string {
  const m = model.trim();
  if (provider !== "openrouter") return m;
  const rest = m.replace(/^openrouter\//, "");
  return rest.includes("/") ? rest : m;
}

export type AppHealth = {
  configured: boolean;
  ok: boolean;
  problems: string[];
  slug?: string;
  html_url?: string;
  webhook_url?: string;
  installations?: { id: number; account: string; repository_selection: string; suspended: boolean }[];
};

/** Live GitHub App check from the backend; null when the backend cannot answer. */
export async function fetchAppHealth(): Promise<AppHealth | null> {
  try {
    return await apiGet<AppHealth>("/github-app/health");
  } catch {
    return null;
  }
}

/** Push the chosen provider + model to the cloud. Never throws; returns a one-line result. */
export async function syncProviderToCloud(
  provider: LlmProviderId,
  model: string,
): Promise<{ ok: boolean; message: string }> {
  try {
    const id = cloudModelId(provider, model);
    await saveCloudSettings({ LLM_PROVIDER: provider, LLM_MODEL: id || null });
    return { ok: true, message: `cloud backend now uses ${provider} · ${id || "provider default"}` };
  } catch (e) {
    const m = String((e as any)?.message ?? e).slice(0, 200);
    return { ok: false, message: `cloud sync failed: ${m}` };
  }
}
