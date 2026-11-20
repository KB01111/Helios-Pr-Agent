import { create } from "zustand";
import { agentList, refreshAgents } from "./actions";
import { apiGet, apiMut, backendUrl } from "./api";
import { tokenAdopted, useGh } from "./github";
import { useApp } from "./store";
import { errText, tauriInvoke, tauriUnavailable } from "./tauri";
import { toastErr, toastOk } from "./toast";

export interface OAuthStatus {
  github: {
    has_oauth: boolean;
    is_app: boolean;
    client_id: string | null;
    app_slug: string | null;
    html_url: string | null;
    install_url: string | null;
  };
  vercel: {
    has_oauth: boolean;
    client_id: string | null;
  };
  cli: {
    gh_available: boolean;
    gh_login: string | null;
    vercel_cli: boolean;
    commandcode_cli: boolean;
    opencode_cli: boolean;
  };
}

export interface VercelTeam {
  id: string;
  slug: string;
  name: string;
}

export type ActiveOAuthFlow =
  | null
  | "github"
  | "openrouter"
  | "vercel"
  | "github-cli"
  | "vercel-cli"
  | "commandcode-cli"
  | "opencode-cli";

const EMPTY_STATUS: OAuthStatus = {
  github: {
    has_oauth: false,
    is_app: false,
    client_id: null,
    app_slug: null,
    html_url: null,
    install_url: null,
  },
  vercel: {
    has_oauth: false,
    client_id: null,
  },
  cli: {
    gh_available: false,
    gh_login: null,
    vercel_cli: false,
    commandcode_cli: false,
    opencode_cli: false,
  },
};

interface OAuthStoreState {
  activeFlow: ActiveOAuthFlow;
  flowMessage: string;
  status: OAuthStatus;
  revision: number;
  vercelUser: string;
  vercelTeams: VercelTeam[];
  bump: () => void;
}

export const useOAuth = create<OAuthStoreState>((set) => ({
  activeFlow: null,
  flowMessage: "",
  status: EMPTY_STATUS,
  revision: 0,
  vercelUser: "",
  vercelTeams: [],
  bump: () => set((s) => ({ revision: s.revision + 1 })),
}));

let flowSeq = 0;

function startFlow(flow: Exclude<ActiveOAuthFlow, null>, flowMessage: string): number {
  const id = ++flowSeq;
  useOAuth.setState({ activeFlow: flow, flowMessage });
  return id;
}

function finishFlow(id: number) {
  if (flowSeq === id) {
    useOAuth.setState({ activeFlow: null, flowMessage: "" });
  }
}

function isLocalHostname(host: string): boolean {
  const h = host.toLowerCase();
  return h === "localhost" || h === "127.0.0.1" || h === "0.0.0.0" || h === "::1" || h === "[::1]";
}

/**
 * Resolve the public HTTPS base URL used for GitHub webhooks. Uses `cloudBase`
 * first, then falls back to `backend` if it is a non-local HTTPS URL.
 */
export function resolvePublicWebhookBase(cloudBase: string, backend: string): string {
  const explicit = cloudBase.trim().replace(/\/+$/, "");
  if (explicit) return explicit;
  const candidate = backend.trim().replace(/\/+$/, "");
  if (!candidate) return "";
  try {
    const parsed = new URL(candidate);
    if (parsed.protocol === "https:" && !isLocalHostname(parsed.hostname)) {
      return candidate;
    }
  } catch {
    /* ignore malformed URL */
  }
  return "";
}

/**
 * Ensure the backend has at least one review agent configured so webhooks can
 * trigger reviews immediately after connecting the GitHub App.
 */
export async function ensureDefaultBackendAgent(): Promise<{ created: boolean; count: number }> {
  const existing = agentList(await apiGet("/agents"));
  if (existing.length > 0) {
    return { created: false, count: existing.length };
  }
  await apiMut("POST", "/agents", {
    name: "pr-agent-control",
    system_prompt: "",
    target_repos: [],
    trigger: "on_commit",
    trigger_label: null,
    runtime_mode: "review_only",
  });
  void refreshAgents();
  return { created: true, count: 1 };
}

export async function refreshOAuthStatus(): Promise<OAuthStatus | null> {
  try {
    const status = await tauriInvoke<OAuthStatus>("oauth_status");
    useOAuth.setState({ status });
    return status;
  } catch (e) {
    if (!tauriUnavailable(e)) {
      console.warn("oauth_status failed:", e);
    }
    return null;
  }
}

export async function cancelOAuth(): Promise<void> {
  ++flowSeq;
  useOAuth.setState({ activeFlow: null, flowMessage: "" });
  try {
    await tauriInvoke("oauth_cancel");
  } catch (e) {
    if (!tauriUnavailable(e)) {
      toastErr("Cancel failed", errText(e, 160));
    }
  }
}

export async function saveOAuthClient(
  provider: "github" | "vercel",
  clientId: string,
  clientSecret?: string,
  isApp?: boolean,
): Promise<void> {
  await tauriInvoke("oauth_save_client", {
    provider,
    clientId: clientId.trim(),
    clientSecret: clientSecret?.trim() || null,
    isApp: isApp ?? false,
  });
  await refreshOAuthStatus();
}

export async function clearOAuthClient(provider: "github" | "vercel"): Promise<void> {
  await tauriInvoke("oauth_clear_client", { provider });
  await refreshOAuthStatus();
}

function summarizePersistTarget(res: { cloud_saved?: boolean; env_path?: string | null }): string {
  if (res.cloud_saved) return "Saved to cloud backend & session";
  if (res.env_path) return "Saved to backend/.env & session";
  return "Saved in session";
}

export async function connectOpenRouterOAuth(): Promise<boolean> {
  const id = startFlow("openrouter", "Browser opened — click Authorize on OpenRouter…");
  const nonce = useApp.getState().clearNonce;
  try {
    const res = await tauriInvoke<{
      key: string;
      user_id?: string | null;
      cloud_saved?: boolean;
      cloud_error?: string | null;
      env_path?: string | null;
      local_error?: string | null;
    }>("openrouter_oauth_connect", {
      settingsUrl: backendUrl() || null,
      apiToken: useApp.getState().secrets.api || null,
    });
    if (useApp.getState().clearNonce === nonce && res.key) {
      useApp.getState().setSecret("openrouter", res.key);
    }
    useOAuth.getState().bump();
    const target = summarizePersistTarget(res);
    toastOk("OpenRouter connected", res.user_id ? `${target} (${res.user_id})` : target);
    return true;
  } catch (e) {
    const msg = errText(e, 260);
    if (!/cancelled/i.test(msg)) {
      toastErr("OpenRouter OAuth failed", msg);
    }
    return false;
  } finally {
    finishFlow(id);
  }
}

export async function connectGitHubOAuth(opts?: {
  saveToBackend?: boolean;
  forceNewApp?: boolean;
  appName?: string;
  org?: string;
}): Promise<boolean> {
  const webhookBase = resolvePublicWebhookBase(useApp.getState().cloudBase, backendUrl());
  const willCreateApp = opts?.forceNewApp || !useOAuth.getState().status.github.has_oauth;
  const id = startFlow(
    "github",
    willCreateApp
      ? "Browser opened — approve GitHub App setup, then install it on your repositories…"
      : "Browser opened — click Authorize on GitHub…",
  );
  const nonce = useApp.getState().clearNonce;
  try {
    const res = await tauriInvoke<{
      access_token: string;
      login?: string;
      avatar_url?: string | null;
      mode?: string;
      app_id?: number;
      app_slug?: string | null;
      html_url?: string | null;
      install_url?: string | null;
      webhook_active?: boolean;
      installation_id?: string | null;
      cloud_saved?: boolean;
      cloud_error?: string | null;
      env_path?: string | null;
      local_error?: string | null;
    }>("github_oauth_connect", {
      backendUrl: webhookBase || null,
      settingsUrl: backendUrl() || null,
      apiToken: useApp.getState().secrets.api || null,
      saveToBackend: opts?.saveToBackend ?? false,
      forceNewApp: opts?.forceNewApp ?? false,
      appName: opts?.appName?.trim() || null,
      org: opts?.org?.trim() || null,
    });

    const login = String(res.login || "").trim();
    if (useApp.getState().clearNonce === nonce && res.access_token) {
      useApp.getState().setSecret("github", res.access_token);
    }
    useGh.setState({
      login,
      signingIn: false,
      userCode: "",
      verificationUri: "",
      signinMsg: login
        ? `Connected as @${login}. Token kept in memory/session only.`
        : "GitHub connected. Token kept in memory/session only.",
    });
    tokenAdopted();
    void refreshOAuthStatus();

    if (res.mode === "app_manifest" && res.cloud_saved) {
      ensureDefaultBackendAgent().catch(() => {});
    }
    useOAuth.getState().bump();

    const parts: string[] = [];
    if (login) parts.push(`Signed in as @${login}`);
    if (res.app_slug) {
      parts.push(`App ${res.app_slug}${res.webhook_active ? " (webhook active)" : ""}`);
    }
    if (res.cloud_saved) parts.push("saved to cloud backend");
    else if (res.env_path) parts.push("saved to backend/.env");
    toastOk("GitHub connected", parts.join(" · ") || "Session ready");
    return true;
  } catch (e) {
    const msg = errText(e, 260);
    if (!/cancelled/i.test(msg)) {
      toastErr("GitHub OAuth failed", msg);
    }
    return false;
  } finally {
    finishFlow(id);
  }
}

export async function importGitHubCli(saveToBackend = false): Promise<boolean> {
  const id = startFlow("github-cli", "Importing GitHub CLI session…");
  const nonce = useApp.getState().clearNonce;
  try {
    const res = await tauriInvoke<{
      access_token: string;
      login?: string;
      cloud_saved?: boolean;
      env_path?: string | null;
    }>("github_import_cli", {
      settingsUrl: backendUrl() || null,
      apiToken: useApp.getState().secrets.api || null,
      saveToBackend,
    });
    const login = String(res.login || "").trim();
    if (useApp.getState().clearNonce === nonce && res.access_token) {
      useApp.getState().setSecret("github", res.access_token);
    }
    useGh.setState({
      login,
      signingIn: false,
      userCode: "",
      verificationUri: "",
      signinMsg: login
        ? `Imported from GitHub CLI as @${login}. Token kept in memory/session only.`
        : "Imported GitHub CLI token.",
    });
    tokenAdopted();
    useOAuth.getState().bump();
    toastOk("GitHub CLI imported", login ? `Signed in as @${login}` : "Session token ready");
    return true;
  } catch (e) {
    toastErr("GitHub CLI import failed", errText(e, 240));
    return false;
  } finally {
    finishFlow(id);
  }
}

export async function fetchVercelTeams(token: string): Promise<VercelTeam[]> {
  const res = await tauriInvoke<{ user?: string; teams?: VercelTeam[] }>("vercel_fetch_teams", { token });
  const teams = Array.isArray(res.teams) ? res.teams : [];
  useOAuth.setState({
    vercelUser: String(res.user || ""),
    vercelTeams: teams,
  });
  return teams;
}

async function handleVercelResult(
  res: {
    user?: string;
    teams?: VercelTeam[];
    team_id?: string | null;
    cloud_saved?: boolean;
    env_path?: string | null;
  },
  label: string,
) {
  const teams = Array.isArray(res.teams) ? res.teams : [];
  useOAuth.setState({
    vercelUser: String(res.user || ""),
    vercelTeams: teams,
  });
  useOAuth.getState().bump();
  const detail = [
    res.user ? `@${res.user}` : "",
    res.team_id ? `team ${res.team_id}` : teams.length > 1 ? `${teams.length} teams found` : "",
    summarizePersistTarget(res),
  ]
    .filter(Boolean)
    .join(" · ");
  toastOk(label, detail);
}

export async function importVercelCli(): Promise<boolean> {
  const id = startFlow("vercel-cli", "Importing Vercel CLI login…");
  try {
    const res = await tauriInvoke<{
      user?: string;
      teams?: VercelTeam[];
      team_id?: string | null;
      cloud_saved?: boolean;
      env_path?: string | null;
    }>("vercel_import_cli", {
      settingsUrl: backendUrl() || null,
      apiToken: useApp.getState().secrets.api || null,
    });
    await handleVercelResult(res, "Vercel CLI imported");
    return true;
  } catch (e) {
    toastErr("Vercel CLI import failed", errText(e, 240));
    return false;
  } finally {
    finishFlow(id);
  }
}

export async function connectVercelOAuth(): Promise<boolean> {
  const id = startFlow("vercel", "Browser opened — authorize Vercel integration…");
  try {
    const res = await tauriInvoke<{
      user?: string;
      teams?: VercelTeam[];
      team_id?: string | null;
      cloud_saved?: boolean;
      env_path?: string | null;
    }>("vercel_oauth_connect", {
      settingsUrl: backendUrl() || null,
      apiToken: useApp.getState().secrets.api || null,
    });
    await handleVercelResult(res, "Vercel connected");
    return true;
  } catch (e) {
    const msg = errText(e, 260);
    if (!/cancelled/i.test(msg)) {
      toastErr("Vercel OAuth failed", msg);
    }
    return false;
  } finally {
    finishFlow(id);
  }
}

export async function importCommandCodeCli(): Promise<boolean> {
  const id = startFlow("commandcode-cli", "Importing Command Code CLI key…");
  const nonce = useApp.getState().clearNonce;
  try {
    const res = await tauriInvoke<{
      key: string;
      cloud_saved?: boolean;
      env_path?: string | null;
    }>("commandcode_import_cli", {
      settingsUrl: backendUrl() || null,
      apiToken: useApp.getState().secrets.api || null,
    });
    if (useApp.getState().clearNonce === nonce && res.key) {
      useApp.getState().setSecret("commandcode", res.key);
    }
    useOAuth.getState().bump();
    toastOk("Command Code connected", summarizePersistTarget(res));
    return true;
  } catch (e) {
    toastErr("Command Code import failed", errText(e, 240));
    return false;
  } finally {
    finishFlow(id);
  }
}

export async function importOpenCodeCli(): Promise<boolean> {
  const id = startFlow("opencode-cli", "Importing OpenCode Go CLI key…");
  const nonce = useApp.getState().clearNonce;
  try {
    const res = await tauriInvoke<{
      key: string;
      cloud_saved?: boolean;
      env_path?: string | null;
    }>("opencode_import_cli", {
      settingsUrl: backendUrl() || null,
      apiToken: useApp.getState().secrets.api || null,
    });
    if (useApp.getState().clearNonce === nonce && res.key) {
      useApp.getState().setSecret("opencodeGo", res.key);
    }
    useOAuth.getState().bump();
    toastOk("OpenCode Go connected", summarizePersistTarget(res));
    return true;
  } catch (e) {
    toastErr("OpenCode Go import failed", errText(e, 240));
    return false;
  } finally {
    finishFlow(id);
  }
}
