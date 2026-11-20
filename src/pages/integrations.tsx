import * as React from "react";
import {
  Check,
  ClipboardCopy,
  ExternalLink,
  GitBranch,
  KeyRound,
  Loader2,
  Plug,
  RefreshCw,
  RotateCcw,
  Search,
  Settings2,
  Shuffle,
  Terminal,
  Trash2,
  X,
} from "lucide-react";
import {
  AsyncButton,
  ButtonRow,
  Console,
  Field,
  FieldGrid,
  Hint,
  Page,
  SecretInput,
  Section,
  StatusDot,
} from "@/components/common";
import { GithubConnect, GithubStateBadge } from "@/components/github-connect";
import { SetupChecklist } from "@/components/setup-checklist";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { apiGet, apiMut, backendUrl } from "@/lib/api";
import { CLOUD_KEYS, cloudModelId, fetchAppHealth, saveCloudSettings, type AppHealth } from "@/lib/cloud";
import {
  checkStatus,
  configure,
  ghRandomSecret,
  loadRepos,
  tokenAdopted,
  useGh,
  webhookBase,
  type ReviewMode,
  type WorkflowProvider,
} from "@/lib/github";
import {
  cancelOAuth,
  clearOAuthClient,
  connectGitHubOAuth,
  connectOpenRouterOAuth,
  connectVercelOAuth,
  fetchVercelTeams,
  importCommandCodeCli,
  importGitHubCli,
  importOpenCodeCli,
  importVercelCli,
  resolvePublicWebhookBase,
  saveOAuthClient,
  useOAuth,
} from "@/lib/oauth";
import { defaultModelFor, LLM_PROVIDERS, modelsForProvider, toProviderId, useApp, type LlmProviderId } from "@/lib/store";
import { errText, openUrl, tauriInvoke } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";

type Dot = "ok" | "warn" | "err" | "idle";
type ProviderKey = "github" | "vercel" | "upstash_box" | "redis" | "openrouter" | "commandcode" | "opencode_go";

/* ------------------------------------------------------------------ */
/* Connections                                                         */
/* ------------------------------------------------------------------ */

function ConnectionHead({
  name,
  hint,
  dot,
  badge,
  onTest,
}: {
  name: string;
  hint: string;
  dot: Dot;
  badge?: React.ReactNode;
  onTest?: () => Promise<void>;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
      <span className="flex items-center gap-2 text-sm font-semibold">
        <StatusDot state={dot} />
        {name}
      </span>
      {badge}
      <span className="text-muted-foreground min-w-0 flex-1 text-xs">{hint}</span>
      {onTest ? (
        <AsyncButton variant="ghost" size="sm" onClick={onTest}>
          Test
        </AsyncButton>
      ) : null}
    </div>
  );
}

const EMPTY_DRAFTS = {
  ghToken: "",
  ghSecret: "",
  vercelToken: "",
  vercelTeam: "",
  vercelClientId: "",
  vercelClientSecret: "",
  boxKey: "",
  redisConn: "",
  redisUrl: "",
  redisToken: "",
  orKey: "",
  ccKey: "",
  ocKey: "",
  llmProvider: "openrouter",
  llmModel: "", // a model option value, "custom", or "" (provider default)
  llmModelCustom: "",
};
type Drafts = typeof EMPTY_DRAFTS;

function ConnectionsCard({
  active,
  drafts,
  setDrafts,
}: {
  active: boolean;
  drafts: Drafts;
  setDrafts: React.Dispatch<React.SetStateAction<Drafts>>;
}) {
  const setSecret = useApp((s) => s.setSecret);
  const backend = useApp((s) => s.backendUrl);
  const cloudBase = useApp((s) => s.cloudBase);
  const sessionGh = useApp((s) => !!s.secrets.github);
  const sessionOr = useApp((s) => !!s.secrets.openrouter);
  const ghLogin = useGh((s) => s.login);

  const activeFlow = useOAuth((s) => s.activeFlow);
  const flowMessage = useOAuth((s) => s.flowMessage);
  const oauthStatus = useOAuth((s) => s.status);
  const revision = useOAuth((s) => s.revision);
  const vercelUser = useOAuth((s) => s.vercelUser);
  const vercelTeams = useOAuth((s) => s.vercelTeams);

  const [dots, setDots] = React.useState<Record<ProviderKey, Dot>>({
    github: "warn",
    vercel: "warn",
    upstash_box: "warn",
    redis: "warn",
    openrouter: "warn",
    commandcode: "warn",
    opencode_go: "warn",
  });
  const [badge, setBadge] = React.useState<{ text: string; tone: "outline" | "success" | "danger" }>({
    text: "not checked",
    tone: "outline",
  });
  const [redisSummary, setRedisSummary] = React.useState("Not checked yet.");
  const [out, setOut] = React.useState("Integrations not checked.");
  const [health, setHealth] = React.useState<AppHealth | null | undefined>(undefined);
  const [appName, setAppName] = React.useState("PR Agent Control");
  const [org, setOrg] = React.useState("");

  const llmDirty = React.useRef(false); // unsaved provider/model edits
  const seq = React.useRef(0);
  const patch = (p: Partial<Drafts>) => {
    if ("llmProvider" in p || "llmModel" in p || "llmModelCustom" in p) llmDirty.current = true;
    setDrafts((d) => ({ ...d, ...p }));
  };

  const checkApp = React.useCallback(async () => {
    const h = await fetchAppHealth();
    setHealth(h);
  }, []);

  const refresh = React.useCallback(async () => {
    const mine = ++seq.current;
    setOut("Checking status…");
    let data: any = null;
    try {
      data = await apiGet("/config/status");
    } catch (e) {
      if (mine !== seq.current) return;
      setHealth(null);
      setRedisSummary("Backend unreachable.");
      setOut(`Status failed (is backend up at ${backendUrl()}?): ${errText(e, 300)}`);
      setBadge({ text: "unreachable", tone: "danger" });
      toastErr("Integrations", "Backend unreachable: " + errText(e, 160));
      return;
    }
    if (mine !== seq.current) return;

    const h = await fetchAppHealth();
    if (mine !== seq.current) return;
    setHealth(h);

    const github = (data && data.github) || {};
    const vercel = (data && data.vercel) || {};
    const redis = (data && data.redis) || {};
    const ok = {
      // A GitHub App can stand in for the PAT (credentials) and its own webhook secret.
      githubFull: !!(
        (github.token_set || github.app_configured) &&
        (github.webhook_secret_set || github.app_webhook_secret_set)
      ),
      vercel: !!vercel.configured,
      box: !!(data && data.upstash_box && data.upstash_box.configured),
      redis: !!redis.configured,
      openrouter: !!(data && data.openrouter && data.openrouter.configured),
      commandcode: !!(data && data.commandcode && data.commandcode.configured),
      opencodeGo: !!(data && data.opencode_go && data.opencode_go.configured),
    };
    setDots({
      github: ok.githubFull ? "ok" : "warn",
      vercel: ok.vercel ? "ok" : "warn",
      upstash_box: ok.box ? "ok" : "warn",
      redis: ok.redis ? "ok" : "warn",
      openrouter: ok.openrouter ? "ok" : "warn",
      commandcode: ok.commandcode ? "ok" : "warn",
      opencode_go: ok.opencodeGo ? "ok" : "warn",
    });
    const all = [ok.githubFull, ok.vercel, ok.box, ok.redis, ok.openrouter, ok.commandcode, ok.opencodeGo];
    const n = all.filter(Boolean).length;
    setBadge({ text: `${n}/7 connected`, tone: n === 7 ? "success" : "outline" });
    setRedisSummary(
      ok.redis
        ? `${redis.mode || "redis"} via ${redis.source || "env"}`
        : "In-memory only — FastAPI Cloud injects REDIS_URL automatically.",
    );

    const llm = (data && data.llm_provider) || "openrouter";
    if (data && data.llm_provider && !llmDirty.current) {
      // Show what the backend really runs: its provider and (if listed) its model.
      // Skipped while the user has unsaved edits, which a late response must not erase.
      const pid = toProviderId(String(data.llm_provider));
      const model = String(data.model || "");
      const listed = modelsForProvider(pid).find((m) => m.value !== "custom" && cloudModelId(pid, m.value) === model);
      setDrafts((d) => ({
        ...d,
        llmProvider: pid,
        llmModel: listed ? listed.value : model ? "custom" : "",
        llmModelCustom: listed ? "" : model,
      }));
    }
    setOut(
      [
        `github: ${github.token_set ? "token set" : github.app_configured ? "no PAT" : "token MISSING"} · ${
          github.webhook_secret_set
            ? "webhook secret set"
            : github.app_webhook_secret_set
              ? "no repo webhook secret"
              : "webhook secret MISSING"
        }${
          github.app_configured
            ? ` · App ${github.app_slug || "configured"}${github.app_webhook_secret_set ? "" : " (App webhook secret MISSING)"}`
            : ""
        }`,
        `vercel: ${ok.vercel ? "configured" + (vercel.team_set ? " (team scoped)" : "") : "not configured"}`,
        `upstash_box: ${ok.box ? "configured" : "not configured"}`,
        `redis: ${
          ok.redis
            ? `configured (mode=${redis.mode || "?"}, source=${redis.source || "?"})`
            : "not configured — FastAPI Cloud injects REDIS_URL; for local/self-hosted save a Redis URL (or the Upstash REST pair) then Test"
        }`,
        `openrouter: ${ok.openrouter ? "configured" : "not configured"}`,
        `commandcode: ${ok.commandcode ? "configured" : "not configured"}`,
        `opencode_go: ${ok.opencodeGo ? "configured" : "not configured"}`,
        `llm provider (backend): ${llm} · model ${(data && data.model) || "default"}`,
        `cloud settings: ${
          data && data.settings
            ? `${data.settings.cloud_keys.length} saved from the app · ${
                data.settings.encryption
                  ? "secrets encrypted"
                  : "NO encryption secret — set BACKEND_API_TOKEN to store keys"
              } · ${data.settings.persistent ? "persistent (Redis)" : "NOT persistent (in-memory store: lost on restart)"}`
            : "unavailable (older backend — redeploy it to enable)"
        }${data && data.settings && data.settings.errors.length ? `\n  ⚠ ${data.settings.errors.join("\n  ⚠ ")}` : ""}`,
        `auth: ${data && data.auth_required ? "BACKEND_API_TOKEN required" : "open (no BACKEND_API_TOKEN)"}`,
      ].join("\n"),
    );
  }, [setDrafts]);

  React.useEffect(() => {
    if (active) void refresh();
  }, [active, refresh, backend, revision]);

  const test = (provider: ProviderKey) => async () => {
    setOut(`Testing ${provider}…`);
    let res: any = null;
    try {
      res = await apiMut("POST", "/config/test", { provider });
    } catch (e) {
      setOut(`${provider}: test failed: ${errText(e, 300)}`);
      toastErr("Test failed", errText(e, 200));
      return;
    }
    setDots((d) => ({ ...d, [provider]: res && res.ok ? "ok" : "err" }));
    setOut(`${provider}: ${(res && res.details) || JSON.stringify(res)}`);
    if (res && res.ok) toastOk("Test passed", `${provider} reachable.`);
    else toastErr("Test failed", `${provider}: ${(res && res.details) || "check failed"}`);
  };

  /**
   * Save settings where the backend can see them:
   *  1. the cloud backend (`PUT /config/settings`): encrypted in its store, live
   *     immediately, no redeploy;
   *  2. the local `backend/.env` via the Tauri backend, for a backend run from this
   *     checkout. Never `localStorage`.
   */
  const persist = async (todo: readonly (readonly [string, string])[], drop: string[], label: string): Promise<boolean> => {
    setOut(`Saving ${label}…`);
    const cloudValues: Record<string, string | null> = {};
    for (const [key, val] of todo) if (CLOUD_KEYS.has(key)) cloudValues[key] = val;
    for (const key of drop) cloudValues[key] = null;

    let cloudOk = false;
    let cloudErr = "";
    if (Object.keys(cloudValues).length) {
      try {
        await saveCloudSettings(cloudValues);
        cloudOk = true;
      } catch (e) {
        cloudErr = errText(e, 300);
      }
    }
    const localOnly = todo.filter(([key]) => !CLOUD_KEYS.has(key));
    const localPaths = new Set<string>();
    let localErr = "";
    for (const [key, val] of todo) {
      try {
        localPaths.add(await tauriInvoke<string>("save_backend_env_var", { key, value: val }));
      } catch (e) {
        localErr = errText(e, 200);
        break;
      }
    }
    const cloudWanted = Object.keys(cloudValues).length > 0;
    const everySaved = (!cloudWanted || cloudOk || (!!localPaths.size && !localErr)) && (!localOnly.length || !localErr);
    const lines: string[] = [];
    if (cloudWanted) {
      lines.push(
        cloudOk ? `✓ cloud backend (${backendUrl()}): saved, live now — no redeploy needed` : `✗ cloud backend: ${cloudErr}`,
      );
    }
    if (todo.length) {
      lines.push(
        localPaths.size && !localErr
          ? `✓ local file ${Array.from(localPaths).join(", ")}${cloudOk ? "" : " — restart a locally run backend to apply"}`
          : `– local backend/.env not written${localErr ? `: ${localErr}` : ""}`,
      );
    }
    if (!cloudOk && cloudWanted && /BACKEND_API_TOKEN/.test(cloudErr)) {
      lines.push("Set BACKEND_API_TOKEN on the deployed backend (and in Settings) so secrets can be stored there.");
    }
    setOut(lines.join("\n"));
    if (everySaved) toastOk("Saved", `${label}${cloudOk ? " → cloud backend" : " → backend/.env"}`);
    else toastErr("Save failed", cloudErr || localErr || "nothing was saved");
    return everySaved;
  };

  const save =
    (pairs: [string, keyof Drafts][], label: string, keep: (keyof Drafts)[] = []) =>
    async () => {
      const todo = pairs.map(([key, field]) => [key, drafts[field].trim(), field] as const).filter(([, v]) => v);
      if (!todo.length) return toastErr("Connect", "Enter a value first.");
      const nonce = useApp.getState().clearNonce;
      const saved = await persist(
        todo.map(([key, val]) => [key, val] as const),
        [],
        label,
      );
      if (!saved) return;
      if (useApp.getState().clearNonce === nonce) {
        for (const [key, val] of todo) {
          if (key === "GITHUB_TOKEN") setSecret("github", val);
          if (key === "OPENROUTER_API_KEY") setSecret("openrouter", val);
          if (key === "COMMANDCODE_API_KEY") setSecret("commandcode", val);
          if (key === "OPENCODE_GO_API_KEY") setSecret("opencodeGo", val);
          if (key === "UPSTASH_BOX_API_KEY") setSecret("box", val);
        }
        if (todo.some(([key]) => key === "GITHUB_TOKEN")) tokenAdopted();
      }
      setDrafts((d) => {
        const next = { ...d };
        for (const [, val, field] of todo) if (!keep.includes(field) && d[field].trim() === val) next[field] = "";
        return next;
      });
      void refresh();
    };

  const saveLlm = async () => {
    const pid = toProviderId(drafts.llmProvider);
    const chosen = drafts.llmModel === "custom" ? drafts.llmModelCustom.trim() : drafts.llmModel;
    const model = cloudModelId(pid, chosen || defaultModelFor(pid));
    const saved = await persist(
      [
        ["LLM_PROVIDER", pid],
        ["LLM_MODEL", model],
      ],
      [],
      "LLM provider & model",
    );
    if (saved) {
      llmDirty.current = false;
      void refresh();
    }
  };

  const base = resolvePublicWebhookBase(cloudBase, backend);
  const wantedHook = base ? `${base}/webhooks/github` : "";
  const installUrl =
    (health?.html_url ? `${health.html_url.replace(/\/+$/, "")}/installations/new` : "") ||
    oauthStatus.github.install_url ||
    "";
  const hookMismatch = !!(health?.configured && wantedHook && health.webhook_url && health.webhook_url !== wantedHook);

  const fixWebhook = async () => {
    try {
      await apiMut("POST", "/github-app/webhook", { base_url: base });
      toastOk("Webhook updated", wantedHook);
    } catch (e) {
      toastErr("Webhook update failed", errText(e, 300));
    }
    await checkApp();
  };

  const ghBadgeText = health?.configured
    ? health.ok
      ? "healthy"
      : `${health.problems.length} issue(s)`
    : ghLogin
      ? `@${ghLogin}`
      : sessionGh
        ? "session token"
        : "not connected";
  const ghBadgeVariant: "success" | "warning" | "outline" =
    health?.configured && health.ok
      ? "success"
      : health?.configured && !health.ok
        ? "warning"
        : sessionGh
          ? "success"
          : "outline";

  const orBadgeText = dots.openrouter === "ok" ? "connected" : sessionOr ? "session only" : "not connected";
  const orBadgeVariant: "success" | "warning" | "outline" =
    dots.openrouter === "ok" ? "success" : sessionOr ? "warning" : "outline";

  const vercelSaveTeam = async (teamId: string) => {
    patch({ vercelTeam: teamId });
    if (!teamId) return;
    await persist([["VERCEL_TEAM_ID", teamId]], [], "Vercel team");
    void refresh();
  };

  const saveVercelOAuthApp = async () => {
    const cid = drafts.vercelClientId.trim();
    if (!cid) return toastErr("Vercel OAuth", "Enter a Vercel OAuth Client ID first.");
    try {
      await saveOAuthClient("vercel", cid, drafts.vercelClientSecret.trim() || undefined);
      patch({ vercelClientSecret: "" });
      toastOk("Vercel OAuth saved", "You can now use 1-click Connect with Vercel.");
    } catch (e) {
      toastErr("Vercel OAuth", errText(e, 200));
    }
  };

  return (
    <Section
      title="Connections — 1-click OAuth & cloud settings"
      description={
        <>
          Connect services in one click — secrets stay inside Rust, your encrypted <b>cloud backend</b> store, and local{" "}
          <code>backend/.env</code> (never <code>localStorage</code>).
        </>
      }
      action={<Badge variant={badge.tone}>{badge.text}</Badge>}
      footer={
        <AsyncButton variant="outline" size="sm" onClick={refresh}>
          <RefreshCw /> Refresh status
        </AsyncButton>
      }
    >
      {/* 1. Core Integrations (2-col) */}
      <div className="grid gap-4 lg:grid-cols-2">
        {/* GitHub OAuth + App */}
        <div className="bg-card/50 grid content-start gap-3 rounded-lg border p-4">
          <ConnectionHead
            name="GitHub (OAuth + App)"
            hint="Bot identity, Check Runs & repo access"
            dot={health?.ok ? "ok" : dots.github}
            badge={<Badge variant={ghBadgeVariant}>{ghBadgeText}</Badge>}
            onTest={test("github")}
          />

          <ButtonRow>
            <AsyncButton
              size="sm"
              onClick={() => connectGitHubOAuth()}
              disabled={activeFlow === "github" || activeFlow === "github-cli"}
            >
              <GitBranch /> {sessionGh ? "Reconnect GitHub" : "Connect with GitHub"}
            </AsyncButton>
            {oauthStatus.cli.gh_available ? (
              <AsyncButton
                variant="outline"
                size="sm"
                onClick={() => importGitHubCli(true)}
                disabled={activeFlow === "github" || activeFlow === "github-cli"}
              >
                <Terminal />{" "}
                {oauthStatus.cli.gh_login ? `Use GitHub CLI (@${oauthStatus.cli.gh_login})` : "Use GitHub CLI"}
              </AsyncButton>
            ) : null}
          </ButtonRow>

          {activeFlow === "github" ? (
            <div
              className="bg-muted/30 flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-xs"
              aria-live="polite"
            >
              <Loader2 className="text-primary size-4 shrink-0 animate-spin" />
              <span className="min-w-0 flex-1">{flowMessage}</span>
              <Button type="button" variant="ghost" size="sm" onClick={cancelOAuth}>
                <X /> Cancel
              </Button>
            </div>
          ) : null}

          {health?.configured ? (
            <div className="bg-muted/20 grid gap-2 rounded-md border p-3 text-xs">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">
                  {health.ok ? "✓ " : "⚠ "}
                  {health.slug ? `App ${health.slug}` : "GitHub App configured"}
                </span>
                <div className="flex flex-wrap gap-1.5">
                  {installUrl ? (
                    <Button type="button" variant="outline" size="xs" onClick={() => openUrl(installUrl)}>
                      <ExternalLink /> Install / manage repos
                    </Button>
                  ) : null}
                  <AsyncButton variant="ghost" size="xs" onClick={checkApp}>
                    <RefreshCw /> Check App
                  </AsyncButton>
                  {hookMismatch ? (
                    <AsyncButton variant="outline" size="xs" onClick={fixWebhook}>
                      Fix webhook URL
                    </AsyncButton>
                  ) : null}
                </div>
              </div>
              {health.installations?.length ? (
                <p className="text-muted-foreground">
                  Installed on{" "}
                  {health.installations.map((i) => `${i.account} (${i.repository_selection})`).join(", ")}
                </p>
              ) : null}
              {health.problems.map((p) => (
                <p key={p} className="text-warning break-words">
                  • {p}
                </p>
              ))}
              {hookMismatch ? (
                <p className="text-warning break-words">
                  • Webhook points to <code>{health.webhook_url}</code>, expected <code>{wantedHook}</code>.
                </p>
              ) : null}
            </div>
          ) : (
            <Hint className="text-xs">
              First click opens GitHub to create &amp; install your personal GitHub App; future sign-ins use 1-click OAuth.
            </Hint>
          )}

          <details className="group">
            <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
              Advanced: custom App name / org, PAT, or webhook secret
            </summary>
            <div className="mt-3 grid gap-3">
              <FieldGrid>
                <Field label="App name" htmlFor="int-app-name" hint="Max 34 chars, unique on GitHub.">
                  <Input
                    id="int-app-name"
                    value={appName}
                    maxLength={34}
                    onChange={(e) => setAppName(e.target.value)}
                    autoComplete="off"
                  />
                </Field>
                <Field label="Organization (optional)" htmlFor="int-app-org">
                  <Input
                    id="int-app-org"
                    value={org}
                    onChange={(e) => setOrg(e.target.value)}
                    placeholder="my-org"
                    autoComplete="off"
                  />
                </Field>
              </FieldGrid>
              <ButtonRow>
                <AsyncButton
                  variant="outline"
                  size="sm"
                  onClick={() => connectGitHubOAuth({ forceNewApp: true, appName, org })}
                  disabled={activeFlow === "github" || !appName.trim()}
                >
                  <Plug /> Create / replace GitHub App
                </AsyncButton>
                {oauthStatus.github.has_oauth ? (
                  <AsyncButton
                    variant="ghost"
                    size="sm"
                    onClick={async () => {
                      await clearOAuthClient("github");
                      toastOk("Cleared saved GitHub OAuth client", "Next Connect will set up a fresh GitHub App.");
                    }}
                  >
                    <Trash2 /> Reset saved OAuth client
                  </AsyncButton>
                ) : null}
              </ButtonRow>
              <Separator />
              <FieldGrid>
                <Field label="Manual PAT" htmlFor="int-gh-token">
                  <SecretInput
                    id="int-gh-token"
                    value={drafts.ghToken}
                    placeholder="ghp_…"
                    onChange={(e) => patch({ ghToken: e.target.value })}
                  />
                </Field>
                <Field label="Manual webhook secret" htmlFor="int-gh-secret">
                  <SecretInput
                    id="int-gh-secret"
                    value={drafts.ghSecret}
                    placeholder="whsec_…"
                    onChange={(e) => patch({ ghSecret: e.target.value })}
                  />
                </Field>
              </FieldGrid>
              <ButtonRow>
                <AsyncButton
                  variant="outline"
                  size="sm"
                  onClick={save(
                    [
                      ["GITHUB_TOKEN", "ghToken"],
                      ["GITHUB_WEBHOOK_SECRET", "ghSecret"],
                    ],
                    "GitHub",
                  )}
                >
                  <KeyRound /> Save manual credentials
                </AsyncButton>
              </ButtonRow>
            </div>
          </details>
        </div>

        {/* OpenRouter OAuth */}
        <div className="bg-card/50 grid content-start gap-3 rounded-lg border p-4">
          <ConnectionHead
            name="OpenRouter (AI Reviews)"
            hint="Default LLM gateway with free & reasoning models"
            dot={dots.openrouter}
            badge={<Badge variant={orBadgeVariant}>{orBadgeText}</Badge>}
            onTest={test("openrouter")}
          />

          <ButtonRow>
            <AsyncButton size="sm" onClick={connectOpenRouterOAuth} disabled={activeFlow === "openrouter"}>
              <Plug /> {dots.openrouter === "ok" || sessionOr ? "Reconnect OpenRouter" : "Connect with OpenRouter"}
            </AsyncButton>
            {activeFlow === "openrouter" ? (
              <Button type="button" variant="ghost" size="sm" onClick={cancelOAuth}>
                <X /> Cancel
              </Button>
            ) : null}
          </ButtonRow>

          {activeFlow === "openrouter" ? (
            <div
              className="bg-muted/30 flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-xs"
              aria-live="polite"
            >
              <Loader2 className="text-primary size-4 shrink-0 animate-spin" />
              <span className="min-w-0 flex-1">{flowMessage}</span>
            </div>
          ) : (
            <Hint className="text-xs">
              One click opens OpenRouter in your browser via PKCE OAuth — no Client ID needed. Your key is saved to
              session, cloud backend, and <code>backend/.env</code> automatically.
            </Hint>
          )}

          <details className="group">
            <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
              Or paste an API key manually
            </summary>
            <div className="mt-3 grid gap-3">
              <Field label="API key" htmlFor="int-or-key">
                <SecretInput
                  id="int-or-key"
                  value={drafts.orKey}
                  placeholder="sk-or-…"
                  onChange={(e) => patch({ orKey: e.target.value })}
                />
              </Field>
              <ButtonRow>
                <AsyncButton
                  variant="outline"
                  size="sm"
                  onClick={save([["OPENROUTER_API_KEY", "orKey"]], "OpenRouter")}
                >
                  <KeyRound /> Save key
                </AsyncButton>
              </ButtonRow>
            </div>
          </details>
        </div>
      </div>

      {/* 2. Review Model & Alternate LLMs */}
      <div className="bg-card/50 grid gap-3 rounded-lg border p-4">
        <ConnectionHead name="Backend LLM provider & model" hint="Which LLM the cloud backend uses for reviews" dot="idle" />
        <FieldGrid>
          <Field
            label="LLM provider"
            htmlFor="int-llm-provider"
            hint="Saved to the cloud backend — applies to the next review, no redeploy."
          >
            <Select
              value={drafts.llmProvider}
              onValueChange={(v) => {
                const pid = toProviderId(v);
                patch({ llmProvider: pid, llmModel: defaultModelFor(pid), llmModelCustom: "" });
              }}
            >
              <SelectTrigger id="int-llm-provider" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(LLM_PROVIDERS) as LlmProviderId[]).map((id) => (
                  <SelectItem key={id} value={id}>
                    {LLM_PROVIDERS[id].label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label="Model" htmlFor="int-llm-model">
            <Select
              value={drafts.llmModel || defaultModelFor(drafts.llmProvider)}
              onValueChange={(v) => patch({ llmModel: v })}
            >
              <SelectTrigger id="int-llm-model" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {modelsForProvider(drafts.llmProvider).map((m) => (
                  <SelectItem key={m.value} value={m.value}>
                    {m.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        </FieldGrid>
        {drafts.llmModel === "custom" ? (
          <Field label="Custom model id" htmlFor="int-llm-model-custom" hint="Blank = the provider's default model.">
            <Input
              id="int-llm-model-custom"
              value={drafts.llmModelCustom}
              placeholder={defaultModelFor(drafts.llmProvider)}
              onChange={(e) => patch({ llmModelCustom: e.target.value })}
              autoComplete="off"
            />
          </Field>
        ) : null}
        <ButtonRow>
          <AsyncButton size="sm" onClick={saveLlm}>
            <Plug /> Save provider &amp; model
          </AsyncButton>
        </ButtonRow>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {/* Command Code */}
        <div className="bg-card/50 grid content-start gap-3 rounded-lg border p-4">
          <ConnectionHead
            name="Command Code"
            hint="OpenAI-compatible review provider"
            dot={dots.commandcode}
            onTest={test("commandcode")}
          />
          <ButtonRow>
            <AsyncButton
              variant={oauthStatus.cli.commandcode_cli ? "default" : "outline"}
              size="sm"
              onClick={importCommandCodeCli}
              disabled={activeFlow === "commandcode-cli"}
            >
              <Terminal /> Import from CLI
            </AsyncButton>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => openUrl("https://commandcode.ai")}
            >
              <ExternalLink /> Get key
            </Button>
          </ButtonRow>
          <details className="group">
            <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
              Paste API key manually
            </summary>
            <div className="mt-3 grid gap-3">
              <Field label="API key" htmlFor="int-cc-key">
                <SecretInput
                  id="int-cc-key"
                  value={drafts.ccKey}
                  placeholder={LLM_PROVIDERS.commandcode.keyPlaceholder}
                  onChange={(e) => patch({ ccKey: e.target.value })}
                />
              </Field>
              <ButtonRow>
                <AsyncButton
                  variant="outline"
                  size="sm"
                  onClick={save([["COMMANDCODE_API_KEY", "ccKey"]], "Command Code")}
                >
                  <KeyRound /> Save key
                </AsyncButton>
              </ButtonRow>
            </div>
          </details>
        </div>

        {/* OpenCode Go */}
        <div className="bg-card/50 grid content-start gap-3 rounded-lg border p-4">
          <ConnectionHead
            name="OpenCode Go"
            hint="OpenAI-compatible review provider"
            dot={dots.opencode_go}
            onTest={test("opencode_go")}
          />
          <ButtonRow>
            <AsyncButton
              variant={oauthStatus.cli.opencode_cli ? "default" : "outline"}
              size="sm"
              onClick={importOpenCodeCli}
              disabled={activeFlow === "opencode-cli"}
            >
              <Terminal /> Import from CLI
            </AsyncButton>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => openUrl("https://opencode.ai/go")}
            >
              <ExternalLink /> Get key
            </Button>
          </ButtonRow>
          <details className="group">
            <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
              Paste API key manually
            </summary>
            <div className="mt-3 grid gap-3">
              <Field label="API key" htmlFor="int-oc-key">
                <SecretInput
                  id="int-oc-key"
                  value={drafts.ocKey}
                  placeholder={LLM_PROVIDERS.opencode_go.keyPlaceholder}
                  onChange={(e) => patch({ ocKey: e.target.value })}
                />
              </Field>
              <ButtonRow>
                <AsyncButton
                  variant="outline"
                  size="sm"
                  onClick={save([["OPENCODE_GO_API_KEY", "ocKey"]], "OpenCode Go")}
                >
                  <KeyRound /> Save key
                </AsyncButton>
              </ButtonRow>
            </div>
          </details>
        </div>
      </div>

      {/* 3. Sandboxes & Storage (3-col) */}
      <div className="grid gap-4 lg:grid-cols-3">
        {/* Vercel Sandbox */}
        <div className="bg-card/50 grid content-start gap-3 rounded-lg border p-4">
          <ConnectionHead name="Vercel Sandbox" hint="Ephemeral PR sandboxes" dot={dots.vercel} onTest={test("vercel")} />
          <ButtonRow>
            {oauthStatus.cli.vercel_cli ? (
              <AsyncButton
                size="sm"
                onClick={importVercelCli}
                disabled={activeFlow === "vercel-cli" || activeFlow === "vercel"}
              >
                <Terminal /> Import from Vercel CLI
              </AsyncButton>
            ) : null}
            {oauthStatus.vercel.has_oauth ? (
              <AsyncButton
                variant={oauthStatus.cli.vercel_cli ? "outline" : "default"}
                size="sm"
                onClick={connectVercelOAuth}
                disabled={activeFlow === "vercel" || activeFlow === "vercel-cli"}
              >
                <Plug /> Connect with Vercel
              </AsyncButton>
            ) : null}
          </ButtonRow>
          {activeFlow === "vercel" ? (
            <div className="bg-muted/30 flex items-center gap-2 rounded-md border px-3 py-2 text-xs">
              <Loader2 className="text-primary size-4 shrink-0 animate-spin" />
              <span className="min-w-0 flex-1">{flowMessage}</span>
              <Button type="button" variant="ghost" size="xs" onClick={cancelOAuth}>
                Cancel
              </Button>
            </div>
          ) : null}
          {!oauthStatus.cli.vercel_cli && !oauthStatus.vercel.has_oauth ? (
            <Hint className="text-xs">
              Run <code>vercel login</code> locally for 1-click CLI import, or paste a token below.
            </Hint>
          ) : null}
          {vercelTeams.length > 0 ? (
            <Field
              label={vercelUser ? `Vercel team (@${vercelUser})` : "Vercel team"}
              htmlFor="int-vercel-team-select"
            >
              <Select value={drafts.vercelTeam} onValueChange={vercelSaveTeam}>
                <SelectTrigger id="int-vercel-team-select" className="w-full">
                  <SelectValue placeholder="Choose team…" />
                </SelectTrigger>
                <SelectContent>
                  {vercelTeams.map((t) => (
                    <SelectItem key={t.id} value={t.id}>
                      {t.name} ({t.slug || t.id})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          ) : null}
          <details className="group">
            <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
              Manual token or OAuth app
            </summary>
            <div className="mt-3 grid gap-3">
              <Field label="Vercel token" htmlFor="int-vercel-token">
                <div className="flex gap-2">
                  <div className="flex-1">
                    <SecretInput
                      id="int-vercel-token"
                      value={drafts.vercelToken}
                      placeholder="vercel_…"
                      onChange={(e) => patch({ vercelToken: e.target.value })}
                    />
                  </div>
                  <AsyncButton
                    variant="outline"
                    size="sm"
                    onClick={async () => {
                      try {
                        const teams = await fetchVercelTeams(drafts.vercelToken);
                        toastOk("Vercel teams loaded", `${teams.length} team(s) found.`);
                      } catch (e) {
                        toastErr("Vercel teams failed", errText(e, 200));
                      }
                    }}
                  >
                    Load teams
                  </AsyncButton>
                </div>
              </Field>
              <Field label="Team ID (optional)" htmlFor="int-vercel-team">
                <Input
                  id="int-vercel-team"
                  value={drafts.vercelTeam}
                  placeholder="team_…"
                  onChange={(e) => patch({ vercelTeam: e.target.value })}
                  autoComplete="off"
                />
              </Field>
              <ButtonRow>
                <AsyncButton
                  variant="outline"
                  size="sm"
                  onClick={save(
                    [
                      ["VERCEL_TOKEN", "vercelToken"],
                      ["VERCEL_TEAM_ID", "vercelTeam"],
                    ],
                    "Vercel",
                    ["vercelTeam"],
                  )}
                >
                  <KeyRound /> Save token &amp; team
                </AsyncButton>
              </ButtonRow>
              <Separator />
              <Field
                label="Vercel OAuth Client ID"
                htmlFor="int-vercel-cid"
                hint="Callback URL: http://127.0.0.1:17495/vercel/callback"
              >
                <Input
                  id="int-vercel-cid"
                  value={drafts.vercelClientId}
                  placeholder={oauthStatus.vercel.client_id || "oac_…"}
                  onChange={(e) => patch({ vercelClientId: e.target.value })}
                  autoComplete="off"
                />
              </Field>
              <Field label="Client Secret (optional)" htmlFor="int-vercel-csec">
                <SecretInput
                  id="int-vercel-csec"
                  value={drafts.vercelClientSecret}
                  placeholder="optional for PKCE"
                  onChange={(e) => patch({ vercelClientSecret: e.target.value })}
                />
              </Field>
              <ButtonRow>
                <AsyncButton variant="outline" size="sm" onClick={saveVercelOAuthApp}>
                  Save OAuth client
                </AsyncButton>
                {oauthStatus.vercel.has_oauth ? (
                  <AsyncButton
                    variant="ghost"
                    size="sm"
                    onClick={async () => {
                      await clearOAuthClient("vercel");
                      toastOk("Cleared Vercel OAuth client");
                    }}
                  >
                    Clear
                  </AsyncButton>
                ) : null}
              </ButtonRow>
            </div>
          </details>
        </div>

        {/* Upstash Box */}
        <div className="bg-card/50 grid content-start gap-3 rounded-lg border p-4">
          <ConnectionHead name="Upstash Box" hint="Warm-box sandbox runner" dot={dots.upstash_box} onTest={test("upstash_box")} />
          <Field label="Box API key" htmlFor="int-box-key">
            <SecretInput
              id="int-box-key"
              value={drafts.boxKey}
              placeholder="box_…"
              onChange={(e) => patch({ boxKey: e.target.value })}
            />
          </Field>
          <ButtonRow>
            <AsyncButton size="sm" onClick={save([["UPSTASH_BOX_API_KEY", "boxKey"]], "Upstash Box")}>
              <Plug /> Save Box key
            </AsyncButton>
            <Button type="button" variant="ghost" size="sm" onClick={() => openUrl("https://console.upstash.com")}>
              <ExternalLink /> Open Upstash Console
            </Button>
          </ButtonRow>
        </div>

        {/* Redis */}
        <div className="bg-card/50 grid content-start gap-3 rounded-lg border p-4">
          <ConnectionHead
            name="Redis (shared state)"
            hint="Agents, runs & quota"
            dot={dots.redis}
            onTest={test("redis")}
          />
          <Hint className="text-xs">{redisSummary}</Hint>
          <details className="group">
            <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
              Self-hosted REDIS_URL or Upstash REST
            </summary>
            <div className="mt-3 grid gap-3">
              <Field
                label="REDIS_URL (local/self-hosted)"
                htmlFor="int-redis-conn-url"
                hint="FastAPI Cloud injects REDIS_URL automatically — only needed for local/self-hosted backends."
              >
                <SecretInput
                  id="int-redis-conn-url"
                  value={drafts.redisConn}
                  placeholder="rediss://default:…@host:port"
                  onChange={(e) => patch({ redisConn: e.target.value })}
                />
              </Field>
              <ButtonRow>
                <AsyncButton variant="outline" size="sm" onClick={save([["REDIS_URL", "redisConn"]], "Redis URL")}>
                  <Plug /> Save Redis URL
                </AsyncButton>
              </ButtonRow>
              <Separator />
              <Field label="Upstash REST URL" htmlFor="int-redis-url">
                <Input
                  id="int-redis-url"
                  value={drafts.redisUrl}
                  placeholder="https://….upstash.io"
                  onChange={(e) => patch({ redisUrl: e.target.value })}
                  autoComplete="off"
                />
              </Field>
              <Field label="Upstash REST token" htmlFor="int-redis-token">
                <SecretInput
                  id="int-redis-token"
                  value={drafts.redisToken}
                  onChange={(e) => patch({ redisToken: e.target.value })}
                />
              </Field>
              <ButtonRow>
                <AsyncButton
                  variant="outline"
                  size="sm"
                  onClick={save(
                    [
                      ["UPSTASH_REDIS_REST_URL", "redisUrl"],
                      ["UPSTASH_REDIS_REST_TOKEN", "redisToken"],
                    ],
                    "Redis",
                    ["redisUrl"],
                  )}
                >
                  <Plug /> Save REST credentials
                </AsyncButton>
              </ButtonRow>
            </div>
          </details>
        </div>
      </div>

      {/* 4. Collapsible Diagnostics */}
      <details className="group">
        <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
          Connection diagnostics
        </summary>
        <Console className="mt-2">{out}</Console>
      </details>
    </Section>
  );
}

/* ------------------------------------------------------------------ */
/* GitHub 1-click repo setup                                           */
/* ------------------------------------------------------------------ */

function StepPill({ label, state, title }: { label: string; state: "ok" | "warn" | ""; title?: string }) {
  const mark = state === "ok" ? "✓" : state === "warn" ? "✕" : "—";
  return (
    <Badge
      variant={state === "ok" ? "success" : state === "warn" ? "warning" : "outline"}
      title={title ? title.slice(0, 300) : undefined}
      className="font-mono text-[11px]"
    >
      {label} {mark}
    </Badge>
  );
}

function RepoRow({ name }: { name: string }) {
  const repo = useGh((s) => s.repos.find((r) => r.full_name === name));
  const selected = useGh((s) => s.selected.includes(name));
  const configuring = useGh((s) => s.configuring.includes(name));
  const failed = useGh((s) => s.failed.includes(name));
  const st = useGh((s) => s.status[name]);
  const toggle = useGh((s) => s.toggleSelected);
  if (!repo) return null;

  const webhook = (st && st.webhook) || {};
  const workflow = (st && st.workflow) || {};
  const secrets = (st && st.secrets) || {};
  const generic = (st && st.error) || "";
  const mode = st && st.mode;
  let secTitle = secrets.error || generic;
  if (!secTitle && st && !secrets.configured) {
    const missing: string[] = [];
    if (!secrets.openrouter_api_key) missing.push("OPENROUTER_API_KEY");
    if (workflow.provider === "openrabbit" && !secrets.llm_api_key) missing.push("LLM_API_KEY");
    if (missing.length) secTitle = "missing repo secret(s): " + missing.join(", ");
  }
  const errs: string[] = [];
  if (st && st.error) errs.push(String(st.error));
  for (const k of ["webhook", "workflow", "secrets"]) if (st && st[k] && st[k].error) errs.push(`${k}: ${st[k].error}`);
  const errMsg = errs.join(" · ").slice(0, 220);
  const why = "Admin access is required to configure webhooks, repo secrets and workflow files.";

  return (
    <div
      className={cn(
        "grid gap-1.5 rounded-md border px-3 py-2 transition-colors",
        selected && "border-primary/50 bg-primary/5",
        configuring && "animate-pulse",
        failed && "border-destructive/50",
        !repo.admin && "opacity-60",
      )}
      data-repo={name}
    >
      <Label className="flex flex-wrap items-center gap-2 font-normal" title={repo.admin ? undefined : why}>
        <Checkbox
          checked={selected}
          disabled={!repo.admin}
          onCheckedChange={(v) => toggle(name, v === true)}
          title={repo.admin ? undefined : why}
        />
        <span className="font-mono text-xs font-medium">{name}</span>
        {repo.private ? <Badge variant="outline">private</Badge> : null}
        {repo.default_branch ? <span className="text-muted-foreground text-xs">default: {repo.default_branch}</span> : null}
        {!repo.admin ? <Badge variant="outline">no admin</Badge> : null}
      </Label>
      <div className="flex flex-wrap items-center gap-1.5 pl-6">
        <StepPill
          label="hook"
          state={st ? (webhook.configured ? "ok" : mode === "actions" ? "" : "warn") : ""}
          title={webhook.error || generic || (mode === "actions" ? "not needed: reviews run in GitHub Actions" : "")}
        />
        <StepPill
          label="workflow"
          state={st ? (workflow.configured ? "ok" : mode === "backend" ? "" : "warn") : ""}
          title={
            workflow.error ||
            generic ||
            (workflow.file ? "file: " + workflow.file : mode === "backend" ? "not needed: the backend posts reviews" : "")
          }
        />
        <StepPill
          label="secrets"
          state={st ? (secrets.configured ? "ok" : mode === "backend" ? "" : "warn") : ""}
          title={secTitle}
        />
        {st && st.configured ? (
          <Badge
            variant="success"
            title={mode === "both" ? "Both paths are active: a PR may receive two reviews." : undefined}
          >
            configured ✓{mode && mode !== "none" ? ` (${mode})` : ""}
          </Badge>
        ) : null}
      </div>
      {errMsg ? <p className="text-destructive pl-6 text-xs break-words">{errMsg}</p> : null}
    </div>
  );
}

const REVIEW_MODES: { value: ReviewMode; label: string }[] = [
  { value: "actions", label: "GitHub Actions only (recommended: no backend needed)" },
  { value: "backend", label: "Backend only (webhook; needs a public HTTPS backend)" },
  { value: "both", label: "Both (a PR may get two reviews)" },
];

function RepoSetupCard({ orKeyDraft }: { orKeyDraft: string }) {
  const connected = useApp((s) => !!s.secrets.github);
  const sessionOrKey = useApp((s) => s.secrets.openrouter);
  const backend = useApp((s) => s.backendUrl);
  const cloudBase = useApp((s) => s.cloudBase);
  const clearNonce = useApp((s) => s.clearNonce);

  const login = useGh((s) => s.login);
  const repos = useGh((s) => s.repos);
  const loadingRepos = useGh((s) => s.loadingRepos);
  const selected = useGh((s) => s.selected);
  const failed = useGh((s) => s.failed);
  const batchRunning = useGh((s) => s.batchRunning);
  const log = useGh((s) => s.log);

  const [switchOpen, setSwitchOpen] = React.useState(false);
  const [filter, setFilter] = React.useState("");
  const [mode, setMode] = React.useState<ReviewMode>("actions");
  const [secret, setSecretText] = React.useState("");
  const [provider, setProvider] = React.useState<WorkflowProvider>("pr-agent");
  const [saveSecret, setSaveSecret] = React.useState(true);
  const [saveToken, setSaveToken] = React.useState(false);
  const [agentName, setAgentName] = React.useState("pr-agent-control");
  const [agentCreate, setAgentCreate] = React.useState(true);
  const logRef = React.useRef<HTMLPreElement>(null);

  React.useEffect(() => {
    setSecretText("");
    setFilter("");
  }, [clearNonce]);
  React.useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log]);

  const orKey = orKeyDraft.trim() || sessionOrKey || "";
  const base = resolvePublicWebhookBase(cloudBase, backend) || webhookBase(cloudBase);
  let isLocal = false;
  try {
    isLocal = /^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])$/i.test(new URL(base).hostname);
  } catch {
    /* not a URL */
  }

  const q = filter.trim().toLowerCase();
  const visible = repos.filter((r) => !q || r.full_name.toLowerCase().includes(q));

  const run = (retryOnly: boolean) =>
    configure({
      retryOnly,
      mode,
      base,
      secret,
      orKey,
      provider,
      saveSecret,
      saveToken,
      agentName,
      agentCreate,
      onSecretGenerated: setSecretText,
    });

  return (
    <Section
      title="GitHub — 1-click repo setup"
      description={
        <>
          Advanced setup with webhook/backend modes and the OpenRabbit reviewer. For the simple path — PR Agent in GitHub
          Actions with your review settings — use the <b>Auto review</b> page.
        </>
      }
      action={<GithubStateBadge />}
    >
      {connected ? (
        <div className="grid gap-3">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="text-muted-foreground">
              {login ? (
                <>
                  Connected as <b>@{login}</b>
                </>
              ) : (
                "Connected (session token)"
              )}
            </span>
            <Button
              type="button"
              variant="link"
              size="xs"
              className="h-auto px-0"
              onClick={() => setSwitchOpen((v) => !v)}
            >
              {switchOpen ? "Hide sign-in" : "Switch account"}
            </Button>
          </div>
          {switchOpen ? <GithubConnect /> : null}
        </div>
      ) : (
        <GithubConnect />
      )}

      {connected ? (
        <div className="grid gap-4">
          <Separator />
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold">Repositories</h3>
            <Badge variant="outline">
              {visible.length} of {repos.length} repos
            </Badge>
            <span className="text-muted-foreground ml-auto text-xs">{selected.length} selected</span>
          </div>
          <Hint>
            Tick repositories to configure. Webhooks, secrets and workflow files require <b>admin</b> access. Statuses
            load on demand — tick a repo or use “Check status”.
          </Hint>
          <div className="flex flex-wrap gap-2">
            <div className="relative min-w-48 flex-1">
              <Search className="text-muted-foreground absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
              <Input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter repositories…"
                className="pl-8"
                autoComplete="off"
                aria-label="Filter repositories"
              />
            </div>
            <AsyncButton variant="outline" size="sm" disabled={batchRunning || loadingRepos} onClick={loadRepos}>
              <RefreshCw /> Refresh repos
            </AsyncButton>
            <AsyncButton variant="outline" size="sm" disabled={batchRunning} onClick={() => checkStatus(filter)}>
              <Check /> Check status
            </AsyncButton>
          </div>
          <div className="grid max-h-80 gap-2 overflow-y-auto pr-1">
            {loadingRepos && !repos.length ? (
              <Hint>Loading repositories…</Hint>
            ) : !repos.length ? (
              <Hint>No repositories loaded — click Refresh repos.</Hint>
            ) : !visible.length ? (
              <Hint>No repositories match the filter.</Hint>
            ) : (
              visible.map((r) => <RepoRow key={r.full_name} name={r.full_name} />)
            )}
          </div>

          <Separator />
          <Field label="Review mode" htmlFor="gh-review-mode">
            <Select value={mode} onValueChange={(v) => setMode(v as ReviewMode)}>
              <SelectTrigger id="gh-review-mode" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {REVIEW_MODES.map((m) => (
                  <SelectItem key={m.value} value={m.value}>
                    {m.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <FieldGrid>
            <Field
              label="Webhook secret"
              htmlFor="gh-webhook-secret"
              hint="Auto-generated when empty (backend/both modes)."
            >
              <div className="flex gap-2">
                <div className="flex-1">
                  <SecretInput
                    id="gh-webhook-secret"
                    value={secret}
                    placeholder="auto-generated 32-byte hex"
                    onChange={(e) => setSecretText(e.target.value)}
                  />
                </div>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    setSecretText(ghRandomSecret());
                    toastOk("Webhook secret generated", "Configure will save it to backend/.env only if that option is checked.");
                  }}
                >
                  <Shuffle /> Generate
                </Button>
              </div>
            </Field>
            <Field label="Workflow provider" htmlFor="gh-workflow-provider">
              <Select value={provider} onValueChange={(v) => setProvider(v as WorkflowProvider)}>
                <SelectTrigger id="gh-workflow-provider" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="pr-agent">PR Agent (Inkling)</SelectItem>
                  <SelectItem value="openrabbit">OpenRabbit Reviewer</SelectItem>
                </SelectContent>
              </Select>
            </Field>
          </FieldGrid>
          <Hint className={mode !== "actions" && isLocal ? "text-warning" : ""}>
            {mode === "actions"
              ? "GitHub Actions mode: no webhook and no public backend needed. Reviews run inside GitHub."
              : `Webhook target: ${base}/webhooks/github` +
                (isLocal
                  ? " — GitHub cannot deliver webhooks to localhost/127.0.0.1; use a public tunnel or a deployed URL."
                  : "")}
          </Hint>
          <Hint>
            The base URL comes from “Cloud app base URL” in the webhook URL card below; it falls back to an HTTPS
            Settings backend URL.
          </Hint>
          <div className="grid gap-2.5">
            <Label className="font-normal">
              <Checkbox checked={saveSecret} onCheckedChange={(v) => setSaveSecret(v === true)} />
              Save webhook secret to <code>backend/.env</code>
            </Label>
            <Label className="font-normal">
              <Checkbox checked={saveToken} onCheckedChange={(v) => setSaveToken(v === true)} />
              Save GitHub token to <code>backend/.env</code>
            </Label>
          </div>
          <Hint>
            Saving to <code>.env</code> requires a backend restart to take effect. OpenRouter key: uses the key from
            OpenRouter above or the current session key <b>{orKey ? "(key ready)" : "(no OpenRouter key yet)"}</b>.
          </Hint>
          <FieldGrid>
            <Field label="Backend agent name" htmlFor="gh-agent-name">
              <Input
                id="gh-agent-name"
                value={agentName}
                onChange={(e) => setAgentName(e.target.value)}
                autoComplete="off"
              />
            </Field>
            <Field label="Agent sync">
              <Label className="h-9 font-normal">
                <Checkbox checked={agentCreate} onCheckedChange={(v) => setAgentCreate(v === true)} />
                Create/update backend agent for these repos
              </Label>
            </Field>
          </FieldGrid>
          <ButtonRow>
            <Button type="button" disabled={batchRunning || selected.length === 0} onClick={() => run(false)}>
              <Settings2 /> Configure selected ({selected.length})
            </Button>
            {failed.length > 0 && !batchRunning ? (
              <Button type="button" variant="outline" onClick={() => run(true)}>
                <RotateCcw /> Retry failed ({failed.length})
              </Button>
            ) : null}
          </ButtonRow>
          <Console ref={logRef} tall>
            {log}
          </Console>
        </div>
      ) : (
        <Hint className="flex items-center gap-2">
          <KeyRound className="size-4" /> Sign in above to list your repositories.
        </Hint>
      )}
    </Section>
  );
}

/* ------------------------------------------------------------------ */
/* Webhook URL                                                         */
/* ------------------------------------------------------------------ */

function WebhookCard() {
  const cloudBase = useApp((s) => s.cloudBase);
  const setCloudBase = useApp((s) => s.setCloudBase);
  const backend = useApp((s) => s.backendUrl);
  const base = resolvePublicWebhookBase(cloudBase, backend);
  const url = base ? base + "/webhooks/github" : "https://<cloud-app>/webhooks/github";

  const copy = async () => {
    if (!base) return toastErr("Webhook URL", "Enter a public HTTPS base URL first.");
    try {
      await navigator.clipboard.writeText(url);
      toastOk("Copied", url);
    } catch {
      toastErr("Copy failed", url);
    }
  };

  return (
    <Section
      title="GitHub webhook URL"
      description={
        <>
          Repo Settings → Webhooks → Add webhook: paste the Payload URL below, Content type{" "}
          <code>application/json</code>, Secret = the webhook secret saved above, events = pull requests &amp; comments.
        </>
      }
      action={<Badge variant="outline">copy-paste</Badge>}
      footer={
        <Button type="button" variant="outline" size="sm" onClick={copy}>
          <ClipboardCopy /> Copy webhook URL
        </Button>
      }
    >
      <Field
        label="Cloud app base URL"
        htmlFor="int-cloud-base"
        hint="Optional override — defaults to your HTTPS Settings Backend URL when set."
      >
        <Input
          id="int-cloud-base"
          value={cloudBase}
          onChange={(e) => setCloudBase(e.target.value)}
          placeholder={base || "https://your-app.vercel.app"}
          autoComplete="off"
        />
      </Field>
      <Hint>
        Payload URL: <code className="break-all">{url}</code>
      </Hint>
    </Section>
  );
}

export function IntegrationsPage({ active }: { active: boolean }) {
  const [drafts, setDrafts] = React.useState<Drafts>(EMPTY_DRAFTS);
  const clearNonce = useApp((s) => s.clearNonce);
  React.useEffect(
    () =>
      setDrafts((d) => ({
        ...EMPTY_DRAFTS,
        llmProvider: d.llmProvider,
        llmModel: d.llmModel,
        llmModelCustom: d.llmModelCustom,
      })),
    [clearNonce],
  );

  return (
    <Page active={active}>
      <SetupChecklist active={active} />
      <ConnectionsCard active={active} drafts={drafts} setDrafts={setDrafts} />
      <RepoSetupCard orKeyDraft={drafts.orKey} />
      <WebhookCard />
    </Page>
  );
}
