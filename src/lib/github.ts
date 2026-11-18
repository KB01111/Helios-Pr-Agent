import { create } from "zustand";
import { apiGet, apiMut, backendUrl } from "./api";
import { agentList, refreshAgents } from "./actions";
import { lsGet, lsSet, useApp } from "./store";
import { errText, jsonObj, openUrl, tauriInvoke } from "./tauri";
import { toastErr, toastOk } from "./toast";

/* GitHub connection + 1-click repo setup.
 * The token lives in the app store's session secrets (memory + sessionStorage)
 * and is only handed to Rust commands. It is never put in localStorage or the
 * setup log. The only way it reaches disk is the opt-in "Save GitHub token to
 * backend/.env" checkbox (Rust save_backend_env_var).
 */

export interface GhRepo {
  full_name: string;
  private: boolean;
  default_branch: string;
  admin: boolean;
}

export type ReviewMode = "actions" | "backend" | "both";
export type WorkflowProvider = "pr-agent" | "openrabbit";

export interface ConfigureOptions {
  retryOnly: boolean;
  mode: ReviewMode;
  base: string;
  secret: string;
  orKey: string;
  provider: WorkflowProvider;
  saveSecret: boolean;
  saveToken: boolean;
  agentName: string;
  agentCreate: boolean;
  /** Called when a webhook secret had to be generated so the form can show it. */
  onSecretGenerated?: (secret: string) => void;
}

const LOG_PLACEHOLDER = "Configure log appears here.";

interface GhState {
  clientId: string;
  setClientId: (v: string) => void;
  login: string;

  signingIn: boolean;
  userCode: string;
  verificationUri: string;
  signinMsg: string;

  repos: GhRepo[];
  loadingRepos: boolean;
  selected: string[];
  status: Record<string, any>;
  failed: string[];
  configuring: string[];
  batchRunning: boolean;
  log: string;

  toggleSelected: (name: string, on: boolean) => void;
  reset: () => void;
}

export const useGh = create<GhState>((set, get) => ({
  clientId: lsGet("pragent.oauth_client"),
  setClientId: (v) => {
    lsSet("pragent.oauth_client", v.trim());
    set({ clientId: v });
  },
  login: "",

  signingIn: false,
  userCode: "",
  verificationUri: "",
  signinMsg: "",

  repos: [],
  loadingRepos: false,
  selected: [],
  status: {},
  failed: [],
  configuring: [],
  batchRunning: false,
  log: LOG_PLACEHOLDER,

  toggleSelected: (name, on) => {
    const cur = get().selected.filter((n) => n !== name);
    set({ selected: on ? [...cur, name] : cur });
    if (on) void loadStatus(name, false);
  },

  reset: () => {
    flowGen++;
    batchGen++;
    sessionGen++;
    inflight.clear();
    set({
      login: "",
      signingIn: false,
      userCode: "",
      verificationUri: "",
      signinMsg: "",
      repos: [],
      loadingRepos: false,
      selected: [],
      status: {},
      failed: [],
      configuring: [],
      batchRunning: false,
      log: LOG_PLACEHOLDER,
    });
  },
}));

const inflight = new Map<string, Promise<void>>();
let sessionGen = 0; // bumped on reset so responses from an earlier GitHub session are dropped
let flowGen = 0; // bumped to invalidate any in-flight device flow (cancel, reset, PAT win)
let startSeq = 0; // identifies the most recently started device flow
let patWon = false; // a PAT sign-in finished while the device flow was still pending
let batchGen = 0; // identifies the current setup batch; reset() advances it to cancel

const gh = () => useGh.getState();
const token = () => useApp.getState().secrets.github;

export function ghOut(line: string) {
  const cur = gh().log;
  const base = cur && cur !== LOG_PLACEHOLDER ? cur + "\n" : "";
  useGh.setState({ log: base + line });
}

export function ghRandomSecret(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/* ---------- sign-in: device flow ---------- */
export function cancelDeviceFlow() {
  flowGen++;
  useGh.setState({ signinMsg: "Cancelling…" });
}

export async function startDeviceFlow() {
  const clientId = gh().clientId.trim();
  if (!clientId) {
    toastErr("GitHub sign-in", "Enter the OAuth App Client ID first (Device Flow must be enabled).");
    return;
  }
  lsSet("pragent.oauth_client", clientId);
  const myGen = ++flowGen;
  const mySeq = ++startSeq;
  const cancelled = () => flowGen !== myGen;
  const ui = (p: Partial<GhState>) => {
    if (mySeq === startSeq) useGh.setState(p);
  };
  patWon = false;
  useGh.setState({ signingIn: true, userCode: "…", signinMsg: "Starting GitHub device flow…" });
  try {
    const d = jsonObj(await tauriInvoke("github_device_start", { clientId }));
    if (!d || !d.device_code || !d.user_code) throw new Error("device flow response missing device_code/user_code");
    if (cancelled()) {
      if (!patWon) ui({ signinMsg: "Login cancelled — no new token was stored.", userCode: "" });
      return;
    }
    const uri = String(d.verification_uri || "https://github.com/login/device");
    useGh.setState({
      verificationUri: uri,
      userCode: String(d.user_code),
      signinMsg: `Approve the code on GitHub — waiting… (expires in ~${Math.round((Number(d.expires_in) || 900) / 60)} min)`,
    });
    toastOk("GitHub login started", `Approve code ${d.user_code} on GitHub.`);
    try {
      await openUrl(uri);
    } catch {
      /* the user can press "Open GitHub" */
    }
    let interval = Math.max(5, Number(d.interval) || 5);
    while (!cancelled()) {
      let r: any = null;
      try {
        r = jsonObj(
          await tauriInvoke("github_device_poll", {
            clientId,
            deviceCode: d.device_code,
            intervalSecs: interval,
            maxWaitSecs: 30,
          }),
        );
      } catch (e) {
        if (cancelled()) break;
        if (/pending|slow_down|authorization_pending/i.test(String(e))) {
          useGh.setState({ signinMsg: "Waiting for approval…" });
          continue;
        }
        throw e;
      }
      if (cancelled()) break;
      if (r && r.interval) interval = Math.max(interval, Number(r.interval) || interval);
      if (r && r.status === "authorized") {
        if (!r.access_token) throw new Error("GitHub did not return an access token");
        useApp.getState().setSecret("github", r.access_token);
        useGh.setState({
          login: r.login || "",
          userCode: "",
          verificationUri: "",
          signinMsg: `Connected as ${r.login || "GitHub user"}. Token kept in memory/session only.`,
        });
        toastOk("GitHub connected", r.login ? "Signed in as " + r.login : "Token stored in session.");
        void loadRepos();
        return;
      }
      if (r && r.status && r.status !== "pending" && r.status !== "authorized") {
        throw new Error("device flow " + r.status);
      }
      useGh.setState({ signinMsg: "Waiting for approval…" });
    }
    if (!patWon) ui({ signinMsg: "Login cancelled — no new token was stored.", userCode: "" });
  } catch (e) {
    useGh.setState({ userCode: "", signinMsg: "Sign-in failed: " + errText(e, 300) });
    toastErr("GitHub sign-in failed", errText(e));
  } finally {
    ui({ signingIn: false });
  }
}

export async function signInWithPat(pat: string): Promise<boolean> {
  const t = pat.trim();
  if (!t) {
    toastErr("PAT", "Paste a token first.");
    return false;
  }
  const sg = sessionGen;
  try {
    const login = await tauriInvoke<string>("github_login", { token: t });
    if (sg !== sessionGen) return false; // secrets were cleared while validating
    useApp.getState().setSecret("github", t);
    if (gh().signingIn) {
      // Stop a pending device flow so its later response cannot replace this token.
      flowGen++;
      patWon = true;
    }
    useGh.setState({
      login: login || "",
      userCode: "",
      verificationUri: "",
      signinMsg: `Authenticated as ${login || "(unknown login)"}. Token kept in memory/session only.`,
    });
    toastOk("GitHub connected", login ? "Signed in as " + login : "PAT stored in session.");
    void loadRepos();
    return true;
  } catch (e) {
    if (sg !== sessionGen) return false;
    useGh.setState({ signinMsg: "PAT rejected: " + errText(e, 300) });
    toastErr("PAT rejected", errText(e, 200));
    return false;
  }
}

/** A token arrived from elsewhere (e.g. saved on the Integrations card). */
export function tokenAdopted() {
  void loadRepos();
}

/* ---------- repo list + status ---------- */
export async function loadRepos() {
  const t = token();
  if (!t) {
    toastErr("GitHub", "Sign in with GitHub first.");
    return;
  }
  const sg = sessionGen;
  const stale = () => sg !== sessionGen || token() !== t;
  useGh.setState({ loadingRepos: true });
  try {
    const raw = await tauriInvoke("github_list_repos", { token: t });
    if (stale()) return; // session cleared or account switched while loading
    const parsed = Array.isArray(raw) ? raw : jsonObj(raw) || [];
    const arr: any[] = Array.isArray(parsed) ? parsed : [];
    const repos: GhRepo[] = [];
    for (const r of arr) {
      const name = String((r && r.full_name) || "").trim();
      if (!name) continue;
      repos.push({
        full_name: name,
        private: !!(r && r.private),
        default_branch: (r && r.default_branch) || "",
        admin: !(r && r.admin === false),
      });
    }
    const names = new Set(repos.map((r) => r.full_name));
    const selectable = new Set(repos.filter((r) => r.admin).map((r) => r.full_name));
    const s = gh();
    const status: Record<string, any> = {};
    for (const [k, v] of Object.entries(s.status)) if (names.has(k)) status[k] = v;
    useGh.setState({
      repos,
      selected: s.selected.filter((n) => names.has(n) && selectable.has(n)),
      status,
      failed: s.failed.filter((n) => names.has(n)),
    });
    ghOut(`Loaded ${repos.length} repositories.`);
    toastOk("Repos loaded", `${repos.length} repositories.`);
  } catch (e) {
    if (stale()) return;
    ghOut("Repo load failed: " + String(e));
    toastErr("Repo load failed", errText(e, 200));
  } finally {
    useGh.setState({ loadingRepos: false });
  }
}

export async function loadStatus(name: string, force: boolean): Promise<void> {
  const t = token();
  if (!t || !name) return;
  if (!force && gh().status[name]) return;
  const pending = inflight.get(name);
  if (pending) {
    await pending; // dedupe ordinary requests...
    if (!force) return;
    // ...but a forced refresh must not be satisfied by a request that started earlier.
    return loadStatus(name, true);
  }
  const sg = sessionGen;
  const stale = () => sg !== sessionGen || token() !== t;
  const run = (async () => {
    try {
      const raw = await tauriInvoke("github_repo_status", { token: t, repo: name });
      if (stale()) return; // session cleared or account switched while loading
      const st = jsonObj(raw);
      const value = st && !Array.isArray(st) ? st : { repo: name, error: "unexpected status response" };
      useGh.setState({ status: { ...gh().status, [name]: value } });
    } catch (e) {
      if (stale()) return;
      useGh.setState({ status: { ...gh().status, [name]: { repo: name, error: errText(e, 300) } } });
    }
  })();
  inflight.set(name, run);
  try {
    await run;
  } finally {
    if (inflight.get(name) === run) inflight.delete(name);
  }
}

async function loadStatusMany(names: string[], force: boolean) {
  const queue = names.slice();
  const workers = Array.from({ length: Math.min(4, queue.length) }, async () => {
    while (queue.length) await loadStatus(queue.shift() as string, force);
  });
  await Promise.all(workers);
}

export async function checkStatus(filter: string) {
  if (!token()) {
    toastErr("GitHub status", "Sign in with GitHub first.");
    return;
  }
  const s = gh();
  const q = filter.trim().toLowerCase();
  const visible = s.repos.filter((r) => !q || r.full_name.toLowerCase().includes(q));
  const targets = s.selected.length
    ? s.selected
    : visible.filter((r) => r.admin).slice(0, 25).map((r) => r.full_name);
  if (!targets.length) {
    toastErr("GitHub status", "No repositories to check.");
    return;
  }
  await loadStatusMany(targets, true);
  const st = gh().status;
  const ok = targets.filter((n) => st[n] && st[n].configured).length;
  toastOk("Status checked", `${ok} of ${targets.length} configured.`);
}

/* ---------- configure batch (sequential; failures never abort the batch) ---------- */
async function saveEnv(key: string, value: string): Promise<boolean> {
  try {
    const p = await tauriInvoke("save_backend_env_var", { key, value });
    ghOut(`[env] ${key} saved to ${p}`);
    return true;
  } catch (e) {
    ghOut(`[env] ${key} save failed: ${errText(e, 220)}`);
    toastErr("Env save failed", `${key}: ${errText(e, 180)}`);
    return false;
  }
}

async function upsertAgent(repos: string[], agentName: string) {
  const name = agentName.trim() || "pr-agent-control";
  if (!repos.length) return;
  ghOut(`[agent] syncing backend agent "${name}"…`);
  try {
    const list = agentList(await apiGet("/agents"));
    const existing = list.find((a) => a && a.name === name);
    const uniq = Array.from(new Set(repos));
    if (existing && existing.id) {
      const current = Array.isArray(existing.target_repos) ? existing.target_repos : [];
      const body = {
        ...existing,
        target_repos: Array.from(new Set([...current, ...uniq])),
        trigger: "on_commit",
        runtime_mode: "review_only",
      };
      const saved = await apiMut("PUT", "/agents/" + encodeURIComponent(existing.id), body);
      ghOut(`[agent] updated ${(saved && saved.id) || existing.id} (target_repos now ${body.target_repos.length})`);
    } else {
      const saved = await apiMut("POST", "/agents", {
        name,
        system_prompt: "",
        target_repos: uniq,
        trigger: "on_commit",
        runtime_mode: "review_only",
      });
      ghOut(`[agent] created ${(saved && saved.id) || name}`);
    }
    toastOk("Agent synced", name);
    void refreshAgents();
  } catch (e) {
    // Agent sync is a convenience: never fail the GitHub setup because of it.
    ghOut("[agent] failed: " + errText(e, 300));
    toastErr("Agent sync failed", errText(e, 200));
  }
}

/** Describe a leftover review path from an earlier setup, or "" if none. */
function staleReviewPath(mode: ReviewMode, st: any): string {
  if (!st || mode === "both" || !st.mode || st.mode === mode || st.mode === "none") return "";
  if (mode === "actions" && st.webhook && st.webhook.configured) {
    return (
      "a webhook from an earlier setup is still active, so the backend may also review PRs. " +
      "Delete the webhook in the repo's Settings > Webhooks and remove the repo from the backend agent's targets."
    );
  }
  if (mode === "backend" && st.workflow && st.workflow.configured) {
    return (
      "the PR Agent workflow from an earlier setup is still committed, so GitHub Actions will also review PRs. " +
      "Delete " + (st.workflow.path || "the workflow file") + " from the repo."
    );
  }
  return "";
}

const errSuffix = (msg: unknown) => (msg ? " — " + String(msg).slice(0, 220) : "");

export async function configure(opts: ConfigureOptions) {
  if (gh().batchRunning) return;
  const t = token();
  if (!t) {
    toastErr("GitHub setup", "Sign in with GitHub first.");
    return;
  }
  const { mode, base, provider } = opts;
  const useWebhook = mode !== "actions"; // webhook + backend agent
  const useActions = mode !== "backend"; // workflow file + Actions secrets
  let parsedBase: URL | null = null;
  try {
    parsedBase = new URL(base);
  } catch {
    /* handled below */
  }
  if (useWebhook && (!parsedBase || !/^https?:$/.test(parsedBase.protocol))) {
    toastErr("GitHub setup", "Set a valid http(s) base URL in the GitHub webhook URL card (or Settings backend URL).");
    return;
  }
  const repos = (opts.retryOnly ? gh().failed : gh().selected).filter(Boolean);
  if (!repos.length) {
    toastErr("GitHub setup", opts.retryOnly ? "No failed repositories to retry." : "Select at least one repository.");
    return;
  }
  const orKey = opts.orKey;
  if (useActions && !orKey) {
    toastErr("GitHub setup", "Add the OpenRouter API key in the OpenRouter row above first.");
    return;
  }
  if (provider !== "pr-agent" && provider !== "openrabbit") {
    toastErr("GitHub setup", "Choose a workflow provider (pr-agent or openrabbit).");
    return;
  }

  const successful: string[] = [];
  const batchFailed: string[] = [];
  // Claim the batch before the first await so a reset during any later step cancels it.
  useGh.setState({ batchRunning: true });
  const myBatch = ++batchGen;
  const cancelled = () => myBatch !== batchGen;
  const out = (line: string) => {
    if (!cancelled()) ghOut(line);
  };
  try {
    let secret = opts.secret.trim();
    if (useWebhook && !secret) {
      // The backend verifies every webhook against ONE global secret. Generating a new
      // one would break deliveries from repositories that were configured earlier, so
      // only generate when the backend that receives the webhooks reports none.
      let backendHasSecret: boolean | null = null; // null = unknown
      try {
        const st = await apiGet("/config/status", base);
        backendHasSecret = !!(st && st.github && st.github.webhook_secret_set);
      } catch {
        backendHasSecret = null;
      }
      if (cancelled()) return;
      if (backendHasSecret !== false) {
        toastErr(
          "GitHub setup",
          backendHasSecret
            ? "The backend already has a webhook secret. Enter that secret in the field — or click Generate to replace it, then re-run setup for every repo."
            : `Could not check whether ${base} already has a webhook secret. Enter the secret in the field (or Generate one), then retry.`,
        );
        return;
      }
      secret = ghRandomSecret();
      opts.onSecretGenerated?.(secret);
      out("[config] generated a 32-byte webhook secret (hex).");
    }
    out(
      `=== Setup started: ${repos.length} repo(s) · mode=${mode}` +
        (useActions ? ` · provider=${provider}` : "") +
        (useWebhook ? ` · webhook base=${base}` : "") +
        " ===",
    );
    if (mode === "both") {
      out("[warn] 'both' posts a review from the GitHub Action AND from the backend agent on every PR.");
    }
    if (useWebhook && opts.saveSecret) {
      const saved = await saveEnv("GITHUB_WEBHOOK_SECRET", secret);
      if (cancelled()) return;
      if (!saved) {
        // GitHub would sign deliveries with a secret the backend does not know.
        out("[setup] aborted: the webhook secret could not be saved to backend/.env, so no repository was changed.");
        return;
      }
      out("[env] restart the backend to pick up .env changes.");
    }
    if (opts.saveToken) {
      await saveEnv("GITHUB_TOKEN", t);
      if (cancelled()) return;
    }

    for (const repo of repos) {
      if (cancelled()) {
        out("[setup] cancelled — session secrets were cleared; remaining repos not touched.");
        break;
      }
      useGh.setState({ configuring: [...gh().configuring, repo] });
      out(`[${repo}] starting…`);
      try {
        const raw = await tauriInvoke("github_setup_repo", {
          token: t,
          repo,
          backendUrl: base,
          webhookSecret: secret,
          openrouterKey: orKey,
          workflowProvider: provider,
          reviewMode: mode,
        });
        if (cancelled()) break; // secrets were cleared mid-request: leave shared state alone
        const r = jsonObj(raw);
        if (!r) throw new Error("unexpected setup response");
        out(`[${repo}] webhook: ${r.webhook || "unknown"}${errSuffix(r.errors && r.errors.webhook)}`);
        out(`[${repo}] secrets: ${r.secrets || "unknown"}${errSuffix(r.errors && r.errors.secrets)}`);
        out(
          `[${repo}] workflow: ${r.workflow || "unknown"}${errSuffix(r.errors && r.errors.workflow)}` +
            (r.workflow_path ? ` (${r.workflow_path})` : ""),
        );
        if (r.webhook_url) out(`[${repo}] webhook target: ${r.webhook_url}`);
        if (r.ok) {
          useGh.setState({ failed: gh().failed.filter((n) => n !== repo) });
          successful.push(repo);
          out(`[${repo}] done.`);
        } else {
          useGh.setState({ failed: Array.from(new Set([...gh().failed, repo])) });
          batchFailed.push(repo);
          out(`[${repo}] failed — see per-step statuses above.`);
        }
      } catch (e) {
        if (cancelled()) break;
        useGh.setState({ failed: Array.from(new Set([...gh().failed, repo])) });
        batchFailed.push(repo);
        out(`[${repo}] error: ${errText(e, 300)}`);
      } finally {
        // Only touch shared state if this batch is still the current one.
        if (!cancelled()) useGh.setState({ configuring: gh().configuring.filter((n) => n !== repo) });
      }
      // Refresh this repo's badges once; a status failure must not abort the batch.
      await loadStatus(repo, true);
      // Switching modes never deletes the other path's webhook/workflow, so a
      // repo can end up with two reviewers. Say so instead of staying silent.
      const stale = staleReviewPath(mode, gh().status[repo]);
      if (stale) {
        out(`[${repo}] warning: ${stale}`);
        toastErr("Possible duplicate reviews", `${repo}: ${stale}`);
      }
    }

    out(`=== Done: ${successful.length} succeeded, ${batchFailed.length} failed ===`);
    if (successful.length) toastOk("GitHub setup", `${successful.length} repo(s) configured.`);
    if (batchFailed.length) toastErr("GitHub setup", `${batchFailed.length} repo(s) failed — use Retry failed.`);
    // The backend agent only matters when the backend reviews PRs; in Actions
    // mode it would just be a second, uncoordinated reviewer.
    if (useWebhook && !cancelled() && opts.agentCreate && successful.length) {
      await upsertAgent(successful, opts.agentName);
    }
  } catch (e) {
    out("[fatal] " + errText(e, 300));
    toastErr("GitHub setup", errText(e));
  } finally {
    // A cancelled batch must not clear the state of a newer one.
    if (!cancelled()) useGh.setState({ batchRunning: false, configuring: [] });
  }
}

/** Where the webhook target would point (used for the "localhost" warning). */
export function webhookBase(cloudBase: string): string {
  return (cloudBase.trim().replace(/\/+$/, "") || backendUrl());
}
