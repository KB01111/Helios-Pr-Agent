import * as React from "react";
import { Check, ClipboardCopy, KeyRound, Plug, RefreshCw, RotateCcw, Search, Settings2, Shuffle, X } from "lucide-react";
import { AsyncButton, ButtonRow, Console, Field, FieldGrid, Hint, Page, SecretInput, Section, StatusDot } from "@/components/common";
import { GithubConnect, GithubStateBadge } from "@/components/github-connect";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { apiGet, apiMut, backendUrl } from "@/lib/api";
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
import { LLM_PROVIDERS, useApp, type LlmProviderId } from "@/lib/store";
import { errText, tauriInvoke } from "@/lib/tauri";
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
  onTest,
}: {
  name: string;
  hint: string;
  dot: Dot;
  onTest?: () => Promise<void>;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <span className="flex items-center gap-2 text-sm font-semibold">
        <StatusDot state={dot} />
        {name}
      </span>
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
  boxKey: "",
  redisConn: "",
  redisUrl: "",
  redisToken: "",
  orKey: "",
  ccKey: "",
  ocKey: "",
  llmProvider: "openrouter",
};
type Drafts = typeof EMPTY_DRAFTS;

function ConnectionsCard({
  drafts,
  setDrafts,
}: {
  drafts: Drafts;
  setDrafts: React.Dispatch<React.SetStateAction<Drafts>>;
}) {
  const setSecret = useApp((s) => s.setSecret);
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
  const [out, setOut] = React.useState("Integrations not checked.");
  const patch = (p: Partial<Drafts>) => setDrafts((d) => ({ ...d, ...p }));

  const refresh = React.useCallback(async () => {
    setOut("Checking status…");
    let data: any = null;
    try {
      data = await apiGet("/config/status");
    } catch (e) {
      setOut(`Status failed (is backend up at ${backendUrl()}?): ${errText(e, 300)}`);
      setBadge({ text: "unreachable", tone: "danger" });
      toastErr("Integrations", "Backend unreachable: " + errText(e, 160));
      return;
    }
    const github = (data && data.github) || {};
    const vercel = (data && data.vercel) || {};
    const redis = (data && data.redis) || {};
    const ok = {
      githubFull: !!(github.token_set && github.webhook_secret_set),
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
    const llm = (data && data.llm_provider) || "openrouter";
    if (data && data.llm_provider) setDrafts((d) => ({ ...d, llmProvider: String(data.llm_provider) }));
    setOut(
      [
        `github: ${github.token_set ? "token set" : "token MISSING"} · ${github.webhook_secret_set ? "webhook secret set" : "webhook secret MISSING"}`,
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
        `llm provider (backend): ${llm}`,
        `auth: ${data && data.auth_required ? "BACKEND_API_TOKEN required" : "open (no BACKEND_API_TOKEN)"}`,
      ].join("\n"),
    );
  }, [setDrafts]);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

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

  // Save token inputs to backend/.env via the Tauri backend (never localStorage).
  const save =
    (pairs: [string, keyof Drafts][], label: string, keep: (keyof Drafts)[] = []) =>
    async () => {
      const todo = pairs.map(([key, field]) => [key, drafts[field].trim(), field] as const).filter(([, v]) => v);
      if (!todo.length) return toastErr("Connect", "Enter a value first.");
      setOut(`Saving ${label} to backend/.env…`);
      const nonce = useApp.getState().clearNonce;
      try {
        const paths = new Set<string>();
        for (const [key, val] of todo) paths.add(await tauriInvoke<string>("save_backend_env_var", { key, value: val }));
        // Mirror session-usable secrets into memory + sessionStorage (same convention as
        // Settings) — unless the user cleared session secrets while the save was pending.
        const sessionCleared = useApp.getState().clearNonce !== nonce;
        if (!sessionCleared) {
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
          for (const [, field] of pairs) if (!keep.includes(field)) next[field] = "";
          return next;
        });
        const savedKeys = todo.map(([k]) => k).join(", ");
        setOut(`Saved ${savedKeys} to ${Array.from(paths).join(", ")}.\nRestart/redeploy the backend so it picks up the new values, then Test.`);
        toastOk("Saved", `${savedKeys} → backend/.env`);
        // The backend still reports the old provider until it restarts: keep the saved choice in the select.
        const savedLlm = todo.find(([key]) => key === "LLM_PROVIDER")?.[1];
        void refresh().then(() => {
          if (savedLlm) patch({ llmProvider: savedLlm });
        });
      } catch (e) {
        setOut(`Save failed: ${errText(e, 300)}\n(Outside the desktop app? Set these env vars where the backend runs instead.)`);
        toastErr("Save failed", errText(e, 220));
      }
    };

  return (
    <Section
      title="Connections — 1-click setup"
      description={
        <>
          Tokens are written to <code>backend/.env</code> via the Tauri backend (git-ignored) — never to{" "}
          <code>localStorage</code>. Inputs clear on save. Restart or redeploy the backend afterwards so it picks up the new
          values.
        </>
      }
      action={<Badge variant={badge.tone}>{badge.text}</Badge>}
      footer={
        <AsyncButton variant="outline" size="sm" onClick={refresh}>
          <RefreshCw /> Refresh status
        </AsyncButton>
      }
    >
      {/* GitHub */}
      <div className="grid gap-3">
        <ConnectionHead name="GitHub" hint="webhook secret + token (repo scope)" dot={dots.github} onTest={test("github")} />
        <FieldGrid>
          <Field label="PAT" htmlFor="int-gh-token">
            <SecretInput
              id="int-gh-token"
              value={drafts.ghToken}
              placeholder="ghp_…"
              onChange={(e) => patch({ ghToken: e.target.value })}
            />
          </Field>
          <Field label="Webhook secret" htmlFor="int-gh-secret" hint="Repo Settings → Webhooks → Secret">
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
            size="sm"
            onClick={save(
              [
                ["GITHUB_TOKEN", "ghToken"],
                ["GITHUB_WEBHOOK_SECRET", "ghSecret"],
              ],
              "GitHub",
            )}
          >
            <Plug /> Connect GitHub
          </AsyncButton>
        </ButtonRow>
      </div>
      <Separator />

      {/* Vercel */}
      <div className="grid gap-3">
        <ConnectionHead name="Vercel Sandbox" hint="token (+ optional team id) for ephemeral runs" dot={dots.vercel} onTest={test("vercel")} />
        <FieldGrid>
          <Field label="Vercel token" htmlFor="int-vercel-token">
            <SecretInput
              id="int-vercel-token"
              value={drafts.vercelToken}
              placeholder="vercel_…"
              onChange={(e) => patch({ vercelToken: e.target.value })}
            />
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
        </FieldGrid>
        <ButtonRow>
          <AsyncButton
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
            <Plug /> Connect Vercel
          </AsyncButton>
        </ButtonRow>
      </div>
      <Separator />

      {/* Upstash Box */}
      <div className="grid gap-3">
        <ConnectionHead name="Upstash Box" hint="Box API key for warm-box runs" dot={dots.upstash_box} onTest={test("upstash_box")} />
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
            <Plug /> Connect Box
          </AsyncButton>
        </ButtonRow>
      </div>
      <Separator />

      {/* Redis */}
      <div className="grid gap-3">
        <ConnectionHead
          name="Redis (shared state)"
          hint="agents, runs, quota · FastAPI Cloud injects REDIS_URL"
          dot={dots.redis}
          onTest={test("redis")}
        />
        <Field
          label="REDIS_URL (local/self-hosted)"
          htmlFor="int-redis-conn-url"
          hint="FastAPI Cloud injects REDIS_URL automatically — use this field only for a local/self-hosted backend. Test verifies the deployed backend."
        >
          <SecretInput
            id="int-redis-conn-url"
            value={drafts.redisConn}
            placeholder="rediss://default:…@host:port"
            onChange={(e) => patch({ redisConn: e.target.value })}
          />
        </Field>
        <ButtonRow>
          <AsyncButton size="sm" onClick={save([["REDIS_URL", "redisConn"]], "Redis URL")}>
            <Plug /> Connect Redis URL
          </AsyncButton>
        </ButtonRow>
        <details className="group">
          <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
            Upstash REST (legacy/optional) — REST URL + token instead of REDIS_URL
          </summary>
          <div className="mt-3 grid gap-3">
            <FieldGrid>
              <Field label="REST URL" htmlFor="int-redis-url">
                <Input
                  id="int-redis-url"
                  value={drafts.redisUrl}
                  placeholder="https://….upstash.io"
                  onChange={(e) => patch({ redisUrl: e.target.value })}
                  autoComplete="off"
                />
              </Field>
              <Field label="REST token" htmlFor="int-redis-token">
                <SecretInput
                  id="int-redis-token"
                  value={drafts.redisToken}
                  onChange={(e) => patch({ redisToken: e.target.value })}
                />
              </Field>
            </FieldGrid>
            <ButtonRow>
              <AsyncButton
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
                <Plug /> Connect Redis
              </AsyncButton>
            </ButtonRow>
          </div>
        </details>
      </div>
      <Separator />

      {/* OpenRouter */}
      <div className="grid gap-3">
        <ConnectionHead name="OpenRouter" hint="LLM key for reviews" dot={dots.openrouter} onTest={test("openrouter")} />
        <Field label="API key" htmlFor="int-or-key">
          <SecretInput
            id="int-or-key"
            value={drafts.orKey}
            placeholder="sk-or-…"
            onChange={(e) => patch({ orKey: e.target.value })}
          />
        </Field>
        <ButtonRow>
          <AsyncButton size="sm" onClick={save([["OPENROUTER_API_KEY", "orKey"]], "OpenRouter")}>
            <Plug /> Connect OpenRouter
          </AsyncButton>
        </ButtonRow>
      </div>
      <Separator />

      {/* Command Code */}
      <div className="grid gap-3">
        <ConnectionHead
          name="Command Code"
          hint="LLM key for reviews (OpenAI-compatible)"
          dot={dots.commandcode}
          onTest={test("commandcode")}
        />
        <Field label="API key" htmlFor="int-cc-key">
          <SecretInput
            id="int-cc-key"
            value={drafts.ccKey}
            placeholder={LLM_PROVIDERS.commandcode.keyPlaceholder}
            onChange={(e) => patch({ ccKey: e.target.value })}
          />
        </Field>
        <ButtonRow>
          <AsyncButton size="sm" onClick={save([["COMMANDCODE_API_KEY", "ccKey"]], "Command Code")}>
            <Plug /> Connect Command Code
          </AsyncButton>
        </ButtonRow>
      </div>
      <Separator />

      {/* OpenCode Go */}
      <div className="grid gap-3">
        <ConnectionHead
          name="OpenCode Go"
          hint="LLM key for reviews (OpenAI-compatible)"
          dot={dots.opencode_go}
          onTest={test("opencode_go")}
        />
        <Field label="API key" htmlFor="int-oc-key">
          <SecretInput
            id="int-oc-key"
            value={drafts.ocKey}
            placeholder={LLM_PROVIDERS.opencode_go.keyPlaceholder}
            onChange={(e) => patch({ ocKey: e.target.value })}
          />
        </Field>
        <ButtonRow>
          <AsyncButton size="sm" onClick={save([["OPENCODE_GO_API_KEY", "ocKey"]], "OpenCode Go")}>
            <Plug /> Connect OpenCode Go
          </AsyncButton>
        </ButtonRow>
      </div>
      <Separator />

      {/* Backend LLM provider */}
      <div className="grid gap-3">
        <ConnectionHead name="Backend LLM provider" hint="which LLM the backend uses for reviews" dot="idle" />
        <Field label="LLM provider" htmlFor="int-llm-provider" hint="Saved as LLM_PROVIDER in backend/.env — restart the backend after saving.">
          <Select value={drafts.llmProvider} onValueChange={(v) => patch({ llmProvider: v })}>
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
        <ButtonRow>
          <AsyncButton size="sm" onClick={save([["LLM_PROVIDER", "llmProvider"]], "LLM provider", ["llmProvider"])}>
            <Plug /> Save LLM provider
          </AsyncButton>
        </ButtonRow>
      </div>

      <Console>{out}</Console>
    </Section>
  );
}

/* ------------------------------------------------------------------ */
/* GitHub 1-click repo setup                                           */
/* ------------------------------------------------------------------ */

function StepPill({ label, state, title }: { label: string; state: "ok" | "warn" | "" ; title?: string }) {
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
  // A repo reviewed only by the GitHub Action needs no webhook, and a
  // backend-only repo needs no workflow/secrets: show those steps as n/a.
  const mode = st && st.mode;
  let secTitle = secrets.error || generic;
  if (!secTitle && st && !secrets.configured) {
    const missing: string[] = [];
    if (!secrets.openrouter_api_key) missing.push("OPENROUTER_API_KEY");
    // LLM_API_KEY is only required by the OpenRabbit workflow.
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

  const repos = useGh((s) => s.repos);
  const loadingRepos = useGh((s) => s.loadingRepos);
  const selected = useGh((s) => s.selected);
  const failed = useGh((s) => s.failed);
  const batchRunning = useGh((s) => s.batchRunning);
  const log = useGh((s) => s.log);

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
  const base = webhookBase(cloudBase);
  void backend;
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
      <GithubConnect />

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
            Tick repositories to configure. Webhooks, secrets and workflow files require <b>admin</b> access. Statuses load on
            demand — tick a repo or use “Check status” (never auto-checks every repo).
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
            The base URL comes from “Cloud app base URL” in the webhook URL card below; it falls back to the Settings backend
            URL.
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
            Saving to <code>.env</code> requires a backend restart to take effect. The token otherwise stays in
            memory/sessionStorage only. OpenRouter key: uses the key saved in the OpenRouter row above, then the session key{" "}
            <b>{orKey ? "(key ready)" : "(no OpenRouter key yet)"}</b>.
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
          <KeyRound className="size-4" /> Sign in to list your repositories.
          <X className="hidden" />
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
  const base = cloudBase.trim().replace(/\/+$/, "");
  const url = base ? base + "/webhooks/github" : "https://<cloud-app>/webhooks/github";

  const copy = async () => {
    if (!base) return toastErr("Webhook URL", "Enter your cloud app base URL first.");
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
          Repo Settings → Webhooks → Add webhook: paste the Payload URL below, Content type <code>application/json</code>,
          Secret = the webhook secret saved above, events = pull requests only.
        </>
      }
      action={<Badge variant="outline">copy-paste</Badge>}
      footer={
        <Button type="button" variant="outline" size="sm" onClick={copy}>
          <ClipboardCopy /> Copy webhook URL
        </Button>
      }
    >
      <Field label="Cloud app base URL" htmlFor="int-cloud-base" hint="e.g. https://your-app.vercel.app">
        <Input
          id="int-cloud-base"
          value={cloudBase}
          onChange={(e) => setCloudBase(e.target.value)}
          placeholder="https://your-app…"
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
  React.useEffect(() => setDrafts((d) => ({ ...EMPTY_DRAFTS, llmProvider: d.llmProvider })), [clearNonce]);

  return (
    <Page active={active}>
      <ConnectionsCard drafts={drafts} setDrafts={setDrafts} />
      <RepoSetupCard orKeyDraft={drafts.orKey} />
      <WebhookCard />
    </Page>
  );
}
