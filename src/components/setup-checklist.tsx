import * as React from "react";
import { RefreshCw } from "lucide-react";
import { AsyncButton, Hint, Section, StatusDot } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { apiGet, apiMut } from "@/lib/api";
import { fetchAppHealth, saveCloudSettings, type AppHealth } from "@/lib/cloud";
import {
  connectGitHubOAuth,
  connectOpenRouterOAuth,
  ensureDefaultBackendAgent,
  importCommandCodeCli,
  importOpenCodeCli,
  resolvePublicWebhookBase,
  useOAuth,
} from "@/lib/oauth";
import { useApp, type TabId } from "@/lib/store";
import { errText, openUrl } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";

type StepState = "ok" | "warn" | "err" | "idle";
interface Step {
  id: string;
  title: string;
  state: StepState;
  detail: string;
  action?: { label: string; run: () => Promise<unknown> | unknown };
}

const LLM_FLAG: Record<string, string> = {
  openrouter: "openrouter",
  commandcode: "commandcode",
  opencode_go: "opencode_go",
};

/**
 * "Is everything wired?" in one place: backend, cloud settings, LLM, GitHub App,
 * webhook and repos, each with the one click that fixes it (or where to go).
 */
export function SetupChecklist({ active }: { active: boolean }) {
  const setTab = useApp((s) => s.setTab);
  const cloudBase = useApp((s) => s.cloudBase);
  const backend = useApp((s) => s.backendUrl);
  const sessionOrKey = useApp((s) => s.secrets.openrouter);
  const revision = useOAuth((s) => s.revision);
  const oauthStatus = useOAuth((s) => s.status);
  const lastOrCloudErr = useOAuth((s) => s.lastOpenRouterCloudError);
  const [status, setStatus] = React.useState<any>(undefined); // undefined = loading, null = unreachable
  const [health, setHealth] = React.useState<AppHealth | null | undefined>(undefined);
  const [agents, setAgents] = React.useState<number | null>(null);
  const [err, setErr] = React.useState("");

  const seq = React.useRef(0);
  const refresh = React.useCallback(async () => {
    // Only the newest check may write state: a slow answer from a previous
    // backend must not overwrite the current one.
    const mine = ++seq.current;
    const current = () => mine === seq.current;
    setErr("");
    let st: any;
    try {
      st = await apiGet("/config/status");
    } catch (e) {
      if (!current()) return;
      setStatus(null);
      setHealth(undefined);
      setAgents(null);
      setErr(errText(e, 200));
      return;
    }
    if (!current()) return;
    setStatus(st);
    const h = await fetchAppHealth();
    if (!current()) return;
    setHealth(h);
    let count: number | null = null;
    try {
      const list = await apiGet("/agents");
      count = Array.isArray(list) ? list.length : Array.isArray(list?.agents) ? list.agents.length : 0;
    } catch {
      /* leave unknown */
    }
    if (current()) setAgents(count);
  }, []);

  // Re-check whenever the tab is shown, the backend URL changes, or an OAuth connection finishes.
  React.useEffect(() => {
    if (active) void refresh();
  }, [active, refresh, backend, revision]);

  const base = resolvePublicWebhookBase(cloudBase, backend);
  const wantedHook = base ? `${base}/webhooks/github` : "";
  const go = (tab: TabId) => () => setTab(tab);

  const steps: Step[] = [];
  if (status === undefined) {
    // loading: render no steps yet
  } else if (status === null) {
    steps.push({
      id: "backend",
      title: "Cloud backend reachable",
      state: "err",
      detail: `${backend} did not answer${err ? `: ${err}` : ""}. Set its URL and API token in Settings.`,
      action: { label: "Open Settings", run: go("settings") },
    });
  } else {
    const llm = String(status.llm_provider || "openrouter");
    const llmReady = !!status[LLM_FLAG[llm] || "openrouter"]?.configured;
    const cloud = status.settings;
    steps.push({ id: "backend", title: "Cloud backend reachable", state: "ok", detail: backend });
    steps.push({
      id: "secure",
      title: "Backend protected and able to store keys",
      state: status.auth_required && cloud?.encryption ? "ok" : "warn",
      detail: !cloud
        ? "This backend predates cloud settings — redeploy it to save keys from the app."
        : !status.auth_required
          ? "Set BACKEND_API_TOKEN on the backend (and the same token in Settings): without it the API is open and keys cannot be saved."
          : "Secrets saved from the app are encrypted before they reach Redis.",
    });
    steps.push({
      id: "store",
      title: "Persistent store (Redis)",
      state: status.redis?.configured ? "ok" : "warn",
      detail: status.redis?.configured
        ? `${status.redis.mode} via ${status.redis.source}`
        : "In-memory only: agents, runs and cloud settings are lost on restart. Connect Redis Cloud on FastAPI Cloud (sets REDIS_URL).",
    });
    let llmAction: Step["action"];
    let llmDetail = llmReady
      ? `${llm} · ${status.model}`
      : `${llm} has no API key on the backend. Connect with 1 click or add it under Integrations below.`;
    if (!llmReady) {
      if (llm === "openrouter" && sessionOrKey) {
        llmDetail = `OpenRouter key is in this session, but could not be saved to the backend${
          lastOrCloudErr ? ` (${lastOrCloudErr})` : ""
        }. Ensure BACKEND_API_TOKEN is set on the backend and in Settings, then click Save session key to backend.`;
        llmAction = {
          label: "Save session key to backend",
          run: async () => {
            try {
              await saveCloudSettings({ OPENROUTER_API_KEY: sessionOrKey });
              useOAuth.setState({ lastOpenRouterCloudError: "" });
              toastOk("Saved to cloud backend", "OPENROUTER_API_KEY");
              await refresh();
            } catch (e) {
              toastErr("Backend save failed", errText(e, 240));
            }
          },
        };
      } else if (llm === "openrouter") {
        llmAction = {
          label: "Connect OpenRouter",
          run: () => connectOpenRouterOAuth(),
        };
      } else if (llm === "commandcode" && oauthStatus.cli.commandcode_cli) {
        llmAction = {
          label: "Import CLI key",
          run: () => importCommandCodeCli(),
        };
      } else if (llm === "opencode_go" && oauthStatus.cli.opencode_cli) {
        llmAction = {
          label: "Import CLI key",
          run: () => importOpenCodeCli(),
        };
      }
    }
    steps.push({
      id: "llm",
      title: "Review model",
      state: llmReady ? "ok" : "warn",
      detail: llmDetail,
      action: llmAction,
    });
    const slugOk = !!(health && health.configured && health.ok);
    const installUrl =
      (health?.html_url ? `${health.html_url.replace(/\/+$/, "")}/installations/new` : "") ||
      oauthStatus.github.install_url ||
      "";
    let appAction: Step["action"];
    if (health && !health.configured) {
      appAction = {
        label: "Connect GitHub App",
        run: () => connectGitHubOAuth({ forceNewApp: true }),
      };
    } else if (health && health.configured && !health.ok && installUrl) {
      appAction = {
        label: "Install on repos",
        run: () => openUrl(installUrl),
      };
    }
    steps.push({
      id: "app",
      title: "GitHub App (bot identity, Check Runs, all PR events)",
      state: health === undefined ? "idle" : slugOk ? "ok" : "warn",
      detail: !health
        ? "Could not check the App."
        : !health.configured
          ? "Not created yet — click Connect GitHub App (opens browser once, then installs automatically)."
          : health.ok
            ? `${health.slug} · installed on ${health.installations?.map((i) => i.account).join(", ")}`
            : health.problems.join(" · "),
      action: appAction,
    });
    const hookOk = !!(health?.configured && wantedHook && health.webhook_url === wantedHook);
    if (health?.configured) {
      steps.push({
        id: "hook",
        title: "App webhook points at your backend",
        state: hookOk ? "ok" : "warn",
        detail: !base
          ? "Set a public HTTPS Cloud app base URL in the webhook URL card below."
          : hookOk
            ? wantedHook
            : `App delivers to ${health.webhook_url || "nowhere"}; expected ${wantedHook}.`,
        action:
          !hookOk && base
            ? {
                label: "Fix webhook URL",
                run: async () => {
                  try {
                    await apiMut("POST", "/github-app/webhook", { base_url: base });
                    toastOk("Webhook updated", wantedHook);
                  } catch (e) {
                    toastErr("Webhook update failed", errText(e, 240));
                  }
                  await refresh();
                },
              }
            : undefined,
      });
    }
    steps.push({
      id: "repos",
      title: "Repositories being reviewed",
      state: agents ? "ok" : "warn",
      detail: agents
        ? `${agents} backend agent(s). Auto review manages GitHub Actions repos.`
        : "No backend agent yet. Create a default review agent or enable GitHub Actions on the Auto review page.",
      action: !agents
        ? {
            label: "Create default agent",
            run: async () => {
              try {
                const r = await ensureDefaultBackendAgent();
                if (r.created) toastOk("Default agent created", "pr-agent-control (review_only)");
                await refresh();
              } catch (e) {
                toastErr("Agent creation failed", errText(e, 200));
              }
            },
          }
        : { label: "Open Auto review", run: go("autoreview") },
    });
  }

  const done = steps.filter((s) => s.state === "ok").length;
  const pct = steps.length ? Math.round((done / steps.length) * 100) : 0;
  return (
    <Section
      title="Setup status"
      description="Everything the cloud backend needs to review your pull requests, with 1-click fixes for whatever is missing."
      action={
        <Badge variant={steps.length && done === steps.length ? "success" : "outline"}>
          {status === undefined ? "checking…" : `${done}/${steps.length} ready`}
        </Badge>
      }
      footer={
        <AsyncButton variant="outline" size="sm" onClick={refresh}>
          <RefreshCw /> Re-check
        </AsyncButton>
      }
    >
      {status === undefined ? (
        <Hint>Checking the backend…</Hint>
      ) : (
        <Progress value={pct} aria-label="Setup readiness" />
      )}
      <ul className="grid gap-2.5">
        {steps.map((s) => (
          <li key={s.id} className="flex flex-wrap items-start gap-x-3 gap-y-1">
            <span className="mt-1.5">
              <StatusDot state={s.state} />
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium">{s.title}</div>
              <div className="text-muted-foreground text-xs leading-relaxed break-words">{s.detail}</div>
            </div>
            {s.action ? (
              <AsyncButton variant="outline" size="sm" onClick={s.action.run}>
                {s.action.label}
              </AsyncButton>
            ) : null}
          </li>
        ))}
      </ul>
    </Section>
  );
}
