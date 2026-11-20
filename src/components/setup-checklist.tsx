import * as React from "react";
import { RefreshCw } from "lucide-react";
import { AsyncButton, Hint, Section, StatusDot } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { apiGet, apiMut } from "@/lib/api";
import { fetchAppHealth, type AppHealth } from "@/lib/cloud";
import { useApp, type TabId } from "@/lib/store";
import { errText } from "@/lib/tauri";
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
  const [status, setStatus] = React.useState<any>(undefined); // undefined = loading, null = unreachable
  const [health, setHealth] = React.useState<AppHealth | null | undefined>(undefined);
  const [agents, setAgents] = React.useState<number | null>(null);
  const [err, setErr] = React.useState("");

  const refresh = React.useCallback(async () => {
    setErr("");
    try {
      setStatus(await apiGet("/config/status"));
    } catch (e) {
      setStatus(null);
      setHealth(undefined);
      setAgents(null);
      setErr(errText(e, 200));
      return;
    }
    setHealth(await fetchAppHealth());
    try {
      const list = await apiGet("/agents");
      setAgents(Array.isArray(list) ? list.length : Array.isArray(list?.agents) ? list.agents.length : 0);
    } catch {
      setAgents(null);
    }
  }, []);

  // Re-check whenever the tab is shown or the backend URL changes.
  React.useEffect(() => {
    if (active) void refresh();
  }, [active, refresh, backend]);

  const base = cloudBase.trim().replace(/\/+$/, "");
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
    steps.push({
      id: "llm",
      title: "Review model",
      state: llmReady ? "ok" : "warn",
      detail: llmReady
        ? `${llm} · ${status.model}`
        : `${llm} has no API key on the backend. Add it under Connections below — it is saved to the cloud.`,
    });
    const slugOk = !!(health && health.configured && health.ok);
    steps.push({
      id: "app",
      title: "GitHub App (bot identity, Check Runs, all PR events)",
      state: health === undefined ? "idle" : slugOk ? "ok" : "warn",
      detail: !health
        ? "Could not check the App."
        : !health.configured
          ? "Not created yet — use “Create GitHub App” below (one click, then Install)."
          : health.ok
            ? `${health.slug} · installed on ${health.installations?.map((i) => i.account).join(", ")}`
            : health.problems.join(" · "),
    });
    const hookOk = !!(health?.configured && wantedHook && health.webhook_url === wantedHook);
    if (health?.configured) {
      steps.push({
        id: "hook",
        title: "App webhook points at your backend",
        state: hookOk ? "ok" : "warn",
        detail: !base
          ? "Set the Cloud app base URL (webhook URL card below)."
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
        : "No backend agent yet. Enable repos on the Auto review page (Actions, no hosting) or configure them below.",
      action: { label: "Open Auto review", run: go("autoreview") },
    });
  }

  const done = steps.filter((s) => s.state === "ok").length;
  return (
    <Section
      title="Setup status"
      description="Everything the cloud backend needs to review your pull requests, with the fix for whatever is missing."
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
      {status === undefined ? <Hint>Checking the backend…</Hint> : null}
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
