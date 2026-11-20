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
import { SetupChecklist } from "@/components/setup-checklist";
import { CLOUD_KEYS, cloudModelId, fetchAppHealth, saveCloudSettings, type AppHealth } from "@/lib/cloud";
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
  llmModel: "", // a model option value, "custom", or "" (provider default)
  llmModelCustom: "",
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
  const llmDirty = React.useRef(false); // unsaved provider/model edits
  const patch = (p: Partial<Drafts>) => {
    if ("llmProvider" in p || "llmModel" in p || "llmModelCustom" in p) llmDirty.current = true;
    setDrafts((d) => ({ ...d, ...p }));
  };

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
          github.webhook_secret_set ? "webhook secret set" : github.app_webhook_secret_set ? "no repo webhook secret" : "webhook secret MISSING"
        }${github.app_configured ? ` · App ${github.app_slug || "configured"}${github.app_webhook_secret_set ? "" : " (App webhook secret MISSING)"}` : ""}`,
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
                data.settings.encryption ? "secrets encrypted" : "NO encryption secret — set BACKEND_API_TOKEN to store keys"
              } · ${data.settings.persistent ? "persistent (Redis)" : "NOT persistent (in-memory store: lost on restart)"}`
            : "unavailable (older backend — redeploy it to enable)"
        }${data && data.settings && data.settings.errors.length ? `\n  ⚠ ${data.settings.errors.join("\n  ⚠ ")}` : ""}`,
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

  /**
   * Save settings where the backend can see them:
   *  1. the cloud backend (`PUT /config/settings`): encrypted in its store, live
   *     immediately, no redeploy;
   *  2. the local `backend/.env` via the Tauri backend, for a backend run from this
   *     checkout. Never `localStorage`.
   * `drop` keys are removed from the cloud (e.g. a blank model = provider default).
   * Returns true when every value landed somewhere.
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
    const localOnly = todo.filter(([key]) => !CLOUD_KEYS.has(key)); // e.g. REDIS_URL: must exist before the backend starts
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
      lines.push(cloudOk ? `✓ cloud backend (${backendUrl()}): saved, live now — no redeploy needed` : `✗ cloud backend: ${cloudErr}`);
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

  // Save token inputs: cloud backend first, local backend/.env as well (never localStorage).
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
      // Mirror session-usable secrets into memory + sessionStorage (same convention as
      // Settings) — unless the user cleared session secrets while the save was pending.
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
      // Clear only what was submitted: text typed while the save was pending stays.
      setDrafts((d) => {
        const next = { ...d };
        for (const [, val, field] of todo) if (!keep.includes(field) && d[field].trim() === val) next[field] = "";
        return next;
      });
      void refresh();
    };

  // Backend LLM provider + model: one save, applied to the cloud immediately.
  const saveLlm = async () => {
    const pid = toProviderId(drafts.llmProvider);
    const chosen = drafts.llmModel === "custom" ? drafts.llmModelCustom.trim() : drafts.llmModel;
    // A blank model means "the provider's default": store it explicitly so a deploy-time
    // LLM_MODEL env var cannot silently stay in force.
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

  return (
    <Section
      title="Connections — 1-click setup"
      description={
        <>
          Saved to your <b>cloud backend</b> (encrypted in its store, live immediately — no redeploy) and, for a backend
          run from this checkout, to <code>backend/.env</code> (git-ignored). Never to <code>localStorage</code>. Inputs
          clear on save. The backend needs <code>BACKEND_API_TOKEN</code> set to accept secrets. <code>REDIS_URL</code> must
          stay a deploy-time environment variable.
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
        <ConnectionHead name="Backend LLM provider & model" hint="which LLM the cloud backend uses for reviews" dot="idle" />
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
            <Select value={drafts.llmModel || defaultModelFor(drafts.llmProvider)} onValueChange={(v) => patch({ llmModel: v })}>
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

/* ------------------------------------------------------------------ */
/* GitHub App (bot identity, Check Runs, all PR events)                */
/* ------------------------------------------------------------------ */

type CreatedApp = {
  app_id: number;
  slug: string;
  html_url: string;
  install_url: string;
  webhook_active: boolean;
  env_path: string | null;
  local_error: string | null;
  cloud_saved: boolean;
  cloud_error: string | null;
};

function GithubAppCard() {
  const cloudBase = useApp((s) => s.cloudBase);
  const apiToken = useApp((s) => s.secrets.api);
  const [appName, setAppName] = React.useState("PR Agent Control");
  const [org, setOrg] = React.useState("");
  const [created, setCreated] = React.useState<CreatedApp | null>(null);
  // undefined = first check still running, null = backend cannot answer
  const [health, setHealth] = React.useState<AppHealth | null | undefined>(undefined);
  const base = cloudBase.trim().replace(/\/+$/, "");
  const wantedHook = base ? `${base}/webhooks/github` : "";

  const backend = useApp((s) => s.backendUrl);
  const checkSeq = React.useRef(0);
  const check = React.useCallback(async () => {
    // Newest check wins, so a slow answer from a previous backend cannot restore its state.
    const mine = ++checkSeq.current;
    const h = await fetchAppHealth();
    if (mine === checkSeq.current) setHealth(h);
  }, []);
  React.useEffect(() => {
    setHealth(undefined); // the old backend's App says nothing about the new one
    void check();
  }, [check, backend]);

  const create = async () => {
    // Re-check right before: an App may already exist (or the first check may still have been running).
    const existing = await fetchAppHealth();
    setHealth(existing);
    if (existing?.configured) {
      toastErr("GitHub App", "An App is already set up on this backend; use Check App.");
      return;
    }
    try {
      const res = await tauriInvoke<CreatedApp>("github_app_create", {
        backendUrl: base,
        appName: appName.trim(),
        org: org.trim() || null,
        // Credentials go from the Rust side straight to the cloud backend (never via the webview).
        settingsUrl: backendUrl(),
        apiToken: apiToken || null,
      });
      setCreated(res);
      if (res.cloud_saved) toastOk("GitHub App created", `${res.slug} · credentials saved to the cloud backend`);
      else toastErr("GitHub App created, cloud backend not updated", res.cloud_error || "backend URL / API token missing");
      void check();
    } catch (e) {
      toastErr("GitHub App", errText(e, 300));
    }
  };

  const fixWebhook = async () => {
    try {
      await apiMut("POST", "/github-app/webhook", { base_url: base });
      toastOk("Webhook updated", wantedHook);
    } catch (e) {
      toastErr("Webhook update failed", errText(e, 300));
    }
    await check();
  };

  const installUrl = created?.install_url || (health?.html_url ? `${health.html_url.replace(/\/+$/, "")}/installations/new` : "");
  const hookMismatch = !!(health?.configured && wantedHook && health.webhook_url && health.webhook_url !== wantedHook);

  return (
    <Section
      title="GitHub App (recommended)"
      description={
        <>
          Gives the agent its own bot identity, lets it post the merge-readiness Check Run, and delivers every PR
          event (reviews, resolved threads, CI results) to the backend. GitHub opens in your browser to confirm. The
          private key and webhook secret go straight to your cloud backend (and the local <code>backend/.env</code>), never
          through this window.
        </>
      }
      action={
        <Badge variant={health?.configured ? (health.ok ? "success" : "warning") : "outline"}>
          {health?.configured ? (health.ok ? "healthy" : `${health.problems.length} issue(s)`) : created ? "created" : "not set up"}
        </Badge>
      }
      footer={
        <ButtonRow>
          <AsyncButton onClick={create} disabled={!appName.trim() || !base || health === undefined || !!health?.configured}>
            <Plug /> {health?.configured ? "App already set up" : "Create GitHub App"}
          </AsyncButton>
          {installUrl ? (
            <Button type="button" variant="outline" size="sm" onClick={() => openUrl(installUrl)}>
              Install on repositories
            </Button>
          ) : null}
          <AsyncButton variant="outline" size="sm" onClick={check}>
            <RefreshCw /> Check App
          </AsyncButton>
          {hookMismatch ? (
            <AsyncButton variant="outline" size="sm" onClick={fixWebhook}>
              Fix webhook URL
            </AsyncButton>
          ) : null}
        </ButtonRow>
      }
    >
      <FieldGrid>
        <Field label="App name" htmlFor="int-app-name" hint="Must be unique on GitHub (max 34 characters)">
          <Input id="int-app-name" value={appName} maxLength={34} onChange={(e) => setAppName(e.target.value)} />
        </Field>
        <Field label="Organization (optional)" htmlFor="int-app-org" hint="Leave empty to create it on your account">
          <Input id="int-app-org" value={org} onChange={(e) => setOrg(e.target.value)} placeholder="my-org" />
        </Field>
      </FieldGrid>
      <Hint>
        Webhook:{" "}
        {base ? (
          <code className="break-all">{base}/webhooks/github</code>
        ) : (
          <>none yet. Set the cloud app base URL below first, or the App is created with its webhook off.</>
        )}
      </Hint>
      {!base ? (
        <Hint className="text-warning">
          Set the <b>Cloud app base URL</b> (webhook URL card below) first: the App can only deliver events to a public https
          backend.
        </Hint>
      ) : null}
      {created ? (
        <Hint>
          Created <b>{created.slug}</b> (id {created.app_id}).{" "}
          {created.cloud_saved ? (
            <>
              Credentials are saved on the cloud backend (private key and webhook secret are encrypted there; no redeploy
              needed).
            </>
          ) : (
            <span className="text-destructive">
              The cloud backend did not accept the credentials ({created.cloud_error || "no backend URL"}). Fix that (is{" "}
              <code>BACKEND_API_TOKEN</code> set on it and in Settings?) and recreate the App, or copy the values from{" "}
              <code className="break-all">{created.env_path || "backend/.env"}</code>.
            </span>
          )}{" "}
          {created.env_path ? "A copy is in the local backend/.env. " : ""}
          Next: <b>Install on repositories</b>, and set the same <code>GITHUB_APP_WEBHOOK_SECRET</code> on the Cloudflare
          worker if you use it.
          {created.webhook_active ? null : " Its webhook is off until you set a URL in the App settings on GitHub."}
        </Hint>
      ) : null}
      {health && health.configured ? (
        <div className="grid gap-1.5">
          <p className="text-[13px] font-medium">
            {health.ok ? "✓ " : "⚠ "}
            {health.slug ? `App ${health.slug}` : "App"}
            {health.installations?.length
              ? ` · installed on ${health.installations.map((i) => `${i.account} (${i.repository_selection})`).join(", ")}`
              : ""}
          </p>
          {health.problems.map((p) => (
            <p key={p} className="text-warning text-xs break-words">
              • {p}
            </p>
          ))}
          {hookMismatch ? (
            <p className="text-warning text-xs break-words">
              • the App delivers to <code>{health.webhook_url}</code>, not <code>{wantedHook}</code>. Use “Fix webhook URL”.
            </p>
          ) : null}
        </div>
      ) : health === null ? (
        <Hint>Backend unreachable — App status unknown.</Hint>
      ) : null}
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
      <ConnectionsCard drafts={drafts} setDrafts={setDrafts} />
      <GithubAppCard />
      <RepoSetupCard orKeyDraft={drafts.orKey} />
      <WebhookCard />
    </Page>
  );
}
