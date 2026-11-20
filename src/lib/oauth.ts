import { create } from "zustand";
import { agentList, refreshAgents } from "./actions";
import { apiGet, apiMut, backendUrl } from "./api";
import { cancelDeviceFlow, tokenAdopted, useGh } from "./github";
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
  vercelTeamsFromSavedToken: boolean;
  vercelTeamsTarget: { backend: string; api: string; draftToken: string | null; localSaved?: boolean } | null;
  lastOpenRouterCloudError: string;
  bump: () => void;
}

export const useOAuth = create<OAuthStoreState>((set) => ({
  activeFlow: null,
  flowMessage: "",
  status: EMPTY_STATUS,
  revision: 0,
  vercelUser: "",
  vercelTeams: [],
  vercelTeamsFromSavedToken: false,
  vercelTeamsTarget: null,
  lastOpenRouterCloudError: "",
  bump: () => set((s) => ({ revision: s.revision + 1 })),
}));

// Team lists belong to the backend connection that loaded them.
let vercelConnection = 0;
export function invalidateVercelTeams() {
  ++vercelConnection;
  useOAuth.setState({ vercelTeams: [], vercelTeamsFromSavedToken: false, vercelTeamsTarget: null });
}
useApp.subscribe((state, previous) => {
  if (state.backendUrl !== previous.backendUrl || state.secrets.api !== previous.secrets.api) {
    invalidateVercelTeams();
  }
});

function vercelTarget(draftToken: string | null = null) {
  return { backend: backendUrl(), api: useApp.getState().secrets.api, draftToken, connection: vercelConnection };
}
function vercelTargetCurrent(target: ReturnType<typeof vercelTarget>) {
  return target.backend === backendUrl() && target.api === useApp.getState().secrets.api && target.connection === vercelConnection;
}

let flowSeq = 0;
let statusSeq = 0;

function startFlow(flow: Exclude<ActiveOAuthFlow, null>, flowMessage: string): number | null {
  const current = useOAuth.getState().activeFlow;
  if (current) {
    toastErr("OAuth in progress", `Finish or cancel the active ${current} connection first.`);
    return null;
  }
  if (flow === "vercel" || flow === "vercel-cli") {
    invalidateVercelTeams();
  }
  const id = ++flowSeq;
  useOAuth.setState({ activeFlow: flow, flowMessage });
  return id;
}

function isFlowCurrent(id: number): boolean {
  return flowSeq === id && useOAuth.getState().activeFlow !== null;
}

function finishFlow(id: number) {
  if (flowSeq === id) {
    useOAuth.setState({ activeFlow: null, flowMessage: "" });
  }
}

function isNonPublicIpv4(host: string): boolean {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (!m) return false;
  const octets = [Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4])];
  if (octets.some((o) => o < 0 || o > 255)) return true;
  const [a, b] = octets;
  return (
    a === 0 ||
    a === 10 ||
    a === 127 ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) ||
    (a === 100 && b >= 64 && b <= 127) ||
    (a === 255 && octets[1] === 255 && octets[2] === 255 && octets[3] === 255)
  );
}

function isLocalOrPrivateHostname(host: string): boolean {
  const h = host.toLowerCase().replace(/^\[|\]$/g, "");
  if (!h || h === "localhost" || h.endsWith(".localhost") || h === "0.0.0.0" || h === "::1" || h === "::") {
    return true;
  }
  if (isNonPublicIpv4(h)) return true;
  if (h.includes(":")) {
    if (h.startsWith("fc") || h.startsWith("fd") || h.startsWith("fe8") || h.startsWith("fe9") || h.startsWith("fea") || h.startsWith("feb")) {
      return true;
    }
    const v4Mapped = h.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/i);
    if (v4Mapped && isNonPublicIpv4(v4Mapped[1])) return true;
    const hexMapped = h.match(/^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/);
    if (hexMapped) {
      const hi = parseInt(hexMapped[1], 16), lo = parseInt(hexMapped[2], 16);
      if (isNonPublicIpv4(`${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`)) return true;
    }
  }
  return false;
}

/** Validate a single candidate URL as a public HTTPS webhook base. Returns "" if invalid. */
export function validatePublicHttpsUrl(raw: string): string {
  const candidate = raw.trim().replace(/\/+$/, "");
  if (!candidate) return "";
  try {
    const parsed = new URL(candidate);
    if (parsed.protocol !== "https:") return "";
    if (parsed.username || parsed.password || parsed.search || parsed.hash) return "";
    if (isLocalOrPrivateHostname(parsed.hostname)) return "";
    return parsed.href.replace(/\/+$/, "");
  } catch {
    return "";
  }
}

/**
 * Resolve the public HTTPS base URL used for GitHub webhooks.
 * If `cloudBase` is non-empty, it must be a valid public HTTPS URL (invalid overrides are not
 * silently replaced by `backend`, so the UI can warn the user about their override).
 * If `cloudBase` is empty, falls back to `backend` when `backend` is a valid public HTTPS URL.
 */
export function resolvePublicWebhookBase(cloudBase: string, backend: string): string {
  if (cloudBase.trim()) {
    return validatePublicHttpsUrl(cloudBase);
  }
  return validatePublicHttpsUrl(backend);
}

/**
 * Ensure the backend has at least one review agent configured so webhooks can
 * trigger reviews immediately after connecting the GitHub App.
 * Bound to a single captured backend URL + API token and uses a deterministic
 * agent id (`pr-agent-control`) for idempotency.
 */
export async function ensureDefaultBackendAgent(): Promise<{ created: boolean; count: number }> {
  const targetBase = backendUrl();
  const targetToken = useApp.getState().secrets.api;
  const existing = agentList(await apiGet("/agents", targetBase));
  if (backendUrl() !== targetBase || useApp.getState().secrets.api !== targetToken) {
    throw new Error("Backend connection changed while checking agents; please retry.");
  }
  if (existing.length > 0) {
    return { created: false, count: existing.length };
  }
  try {
    await apiMut(
      "POST",
      "/agents",
      {
        id: "pr-agent-control",
        name: "pr-agent-control",
        system_prompt: "",
        target_repos: [],
        trigger: "on_commit",
        trigger_label: null,
        runtime_mode: "review_only",
      },
      targetBase,
      targetToken,
    );
  } catch (e) {
    if (!/409|already exists/i.test(String(e))) {
      throw e;
    }
    if (backendUrl() === targetBase) {
      void refreshAgents();
    }
    return { created: false, count: 1 };
  }
  if (backendUrl() === targetBase) {
    void refreshAgents();
  }
  return { created: true, count: 1 };
}

export async function refreshOAuthStatus(): Promise<OAuthStatus | null> {
  const mine = ++statusSeq;
  try {
    const status = await tauriInvoke<OAuthStatus>("oauth_status");
    if (mine === statusSeq) {
      useOAuth.setState({ status });
    }
    return useOAuth.getState().status;
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
  ++statusSeq;
  await tauriInvoke("oauth_save_client", {
    provider,
    clientId: clientId.trim(),
    clientSecret: clientSecret?.trim() || null,
    isApp: isApp ?? false,
  });
  await refreshOAuthStatus();
}

export async function clearOAuthClient(provider: "github" | "vercel"): Promise<void> {
  ++statusSeq;
  await tauriInvoke("oauth_clear_client", { provider });
  await refreshOAuthStatus();
}

function summarizePersistTarget(res: { cloud_saved?: boolean; env_path?: string | null }): string {
  if (res.cloud_saved) return "Saved to cloud backend & session";
  if (res.env_path) return "Saved to backend/.env & session";
  return "Saved in session";
}

export async function connectOpenRouterOAuth(opts?: { persist?: boolean }): Promise<boolean> {
  const persist = opts?.persist ?? true;
  const id = startFlow("openrouter", "Browser opened — click Authorize on OpenRouter…");
  if (id === null) return false;
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
      settingsUrl: persist ? backendUrl() || null : null,
      apiToken: persist ? useApp.getState().secrets.api || null : null,
      persist,
    });
    if (!isFlowCurrent(id) || useApp.getState().clearNonce !== nonce) {
      return false;
    }
    if (res.key) {
      useApp.getState().setSecret("openrouter", res.key);
    }
    useOAuth.setState({
      lastOpenRouterCloudError: persist && !res.cloud_saved ? String(res.cloud_error || "") : "",
    });
    useOAuth.getState().bump();
    const target = persist ? summarizePersistTarget(res) : "Saved in session only";
    toastOk("OpenRouter connected", res.user_id ? `${target} (${res.user_id})` : target);
    return true;
  } catch (e) {
    if (isFlowCurrent(id)) {
      const msg = errText(e, 260);
      if (!/cancelled/i.test(msg)) {
        toastErr("OpenRouter OAuth failed", msg);
      }
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
  if (id === null) return false;
  cancelDeviceFlow();
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
      client_save_error?: string | null;
    }>("github_oauth_connect", {
      backendUrl: webhookBase || null,
      settingsUrl: backendUrl() || null,
      apiToken: useApp.getState().secrets.api || null,
      saveToBackend: opts?.saveToBackend ?? false,
      forceNewApp: opts?.forceNewApp ?? false,
      appName: opts?.appName?.trim() || null,
      org: opts?.org?.trim() || null,
    });
    if (!isFlowCurrent(id) || useApp.getState().clearNonce !== nonce) {
      return false;
    }

    const login = String(res.login || "").trim();
    if (res.access_token) {
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
    if (res.client_save_error) parts.push(`warning: client file not saved (${res.client_save_error})`);
    toastOk("GitHub connected", parts.join(" · ") || "Session ready");
    return true;
  } catch (e) {
    if (isFlowCurrent(id)) {
      const msg = errText(e, 260);
      if (!/cancelled/i.test(msg)) {
        toastErr("GitHub OAuth failed", msg);
      }
    }
    return false;
  } finally {
    finishFlow(id);
  }
}

export async function importGitHubCli(saveToBackend = false): Promise<boolean> {
  const id = startFlow("github-cli", "Importing GitHub CLI session…");
  if (id === null) return false;
  cancelDeviceFlow();
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
    if (!isFlowCurrent(id) || useApp.getState().clearNonce !== nonce) {
      return false;
    }
    const login = String(res.login || "").trim();
    if (res.access_token) {
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
    if (isFlowCurrent(id)) {
      toastErr("GitHub CLI import failed", errText(e, 240));
    }
    return false;
  } finally {
    finishFlow(id);
  }
}

export async function fetchVercelTeams(token: string): Promise<VercelTeam[]> {
  invalidateVercelTeams();
  const target = vercelTarget(token.trim());
  const res = await tauriInvoke<{ user?: string; teams?: VercelTeam[] }>("vercel_fetch_teams", { token });
  if (!vercelTargetCurrent(target)) throw new Error("Backend connection changed; reload teams.");
  const teams = Array.isArray(res.teams) ? res.teams : [];
  useOAuth.setState({
    vercelUser: String(res.user || ""),
    vercelTeams: teams,
    vercelTeamsFromSavedToken: false,
    vercelTeamsTarget: target,
  });
  return teams;
}

async function handleVercelResult(
  res: {
    user?: string;
    teams?: VercelTeam[];
    teams_error?: string | null;
    team_id?: string | null;
    cloud_saved?: boolean;
    cloud_error?: string | null;
    local_error?: string | null;
    env_path?: string | null;
  },
  label: string,
  target: ReturnType<typeof vercelTarget>,
) {
  if (!vercelTargetCurrent(target)) return false;
  if (!res.cloud_saved || res.teams_error) {
    useOAuth.setState({ vercelTeams: [], vercelTeamsFromSavedToken: false, vercelTeamsTarget: null });
    toastErr(label, [res.cloud_error, res.local_error, res.teams_error, !res.cloud_saved ? "Token was not saved to the selected backend. Use manual connection or retry." : ""].filter(Boolean).join(" · "));
    return false;
  }
  const teams = Array.isArray(res.teams) ? res.teams : [];
  useOAuth.setState({
    vercelUser: String(res.user || ""),
    vercelTeams: teams,
    vercelTeamsFromSavedToken: true,
    vercelTeamsTarget: { ...target, localSaved: !!res.env_path },
  });
  useOAuth.getState().bump();
  const detail = [
    res.user ? `@${res.user}` : "",
    res.team_id ? `team ${res.team_id}` : teams.length > 1 ? `${teams.length} teams found` : "",
    res.teams_error ? `team lookup warning: ${res.teams_error}` : "",
    summarizePersistTarget(res),
    res.cloud_error || "",
    res.local_error || "",
  ]
    .filter(Boolean)
    .join(" · ");
  toastOk(label, detail);
  return true;
}

export async function importVercelCli(): Promise<boolean> {
  const id = startFlow("vercel-cli", "Importing Vercel CLI login…");
  if (id === null) return false;
  const target = vercelTarget();
  try {
    const res = await tauriInvoke<{
      user?: string;
      teams?: VercelTeam[];
      teams_error?: string | null;
      team_id?: string | null;
      cloud_saved?: boolean;
      cloud_error?: string | null;
      local_error?: string | null;
      env_path?: string | null;
    }>("vercel_import_cli", {
      settingsUrl: target.backend || null,
      apiToken: target.api || null,
    });
    if (!isFlowCurrent(id)) return false;
    return await handleVercelResult(res, "Vercel CLI imported", target);
  } catch (e) {
    if (isFlowCurrent(id)) {
      toastErr("Vercel CLI import failed", errText(e, 240));
    }
    return false;
  } finally {
    finishFlow(id);
  }
}

export async function connectVercelOAuth(): Promise<boolean> {
  const id = startFlow("vercel", "Browser opened — authorize Vercel integration…");
  if (id === null) return false;
  const target = vercelTarget();
  try {
    const res = await tauriInvoke<{
      user?: string;
      teams?: VercelTeam[];
      teams_error?: string | null;
      team_id?: string | null;
      cloud_saved?: boolean;
      cloud_error?: string | null;
      local_error?: string | null;
      env_path?: string | null;
    }>("vercel_oauth_connect", {
      settingsUrl: target.backend || null,
      apiToken: target.api || null,
    });
    if (!isFlowCurrent(id)) return false;
    return await handleVercelResult(res, "Vercel connected", target);
  } catch (e) {
    if (isFlowCurrent(id)) {
      const msg = errText(e, 260);
      if (!/cancelled/i.test(msg)) {
        toastErr("Vercel OAuth failed", msg);
      }
    }
    return false;
  } finally {
    finishFlow(id);
  }
}

export async function importCommandCodeCli(): Promise<boolean> {
  const id = startFlow("commandcode-cli", "Importing Command Code CLI key…");
  if (id === null) return false;
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
    if (!isFlowCurrent(id) || useApp.getState().clearNonce !== nonce) {
      return false;
    }
    if (res.key) {
      useApp.getState().setSecret("commandcode", res.key);
    }
    useOAuth.getState().bump();
    toastOk("Command Code connected", summarizePersistTarget(res));
    return true;
  } catch (e) {
    if (isFlowCurrent(id)) {
      toastErr("Command Code import failed", errText(e, 240));
    }
    return false;
  } finally {
    finishFlow(id);
  }
}

export async function importOpenCodeCli(): Promise<boolean> {
  const id = startFlow("opencode-cli", "Importing OpenCode Go CLI key…");
  if (id === null) return false;
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
    if (!isFlowCurrent(id) || useApp.getState().clearNonce !== nonce) {
      return false;
    }
    if (res.key) {
      useApp.getState().setSecret("opencodeGo", res.key);
    }
    useOAuth.getState().bump();
    toastOk("OpenCode Go connected", summarizePersistTarget(res));
    return true;
  } catch (e) {
    if (isFlowCurrent(id)) {
      toastErr("OpenCode Go import failed", errText(e, 240));
    }
    return false;
  } finally {
    finishFlow(id);
  }
}
