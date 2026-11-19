import * as React from "react";
import {
  CircleCheck,
  ExternalLink,
  Eye,
  FlaskConical,
  GitPullRequest,
  KeyRound,
  PowerOff,
  RefreshCw,
  RotateCcw,
  Rocket,
  Search,
  TriangleAlert,
  X,
} from "lucide-react";
import { AsyncButton, ButtonRow, Console, Field, FieldGrid, Hint, Page, SecretInput, Section } from "@/components/common";
import { GithubConnect, GithubStateBadge } from "@/components/github-connect";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  actionsUrl,
  isActive,
  optionsProblem,
  repoState,
  sameOptions,
  STATE_LABEL,
  useAutoReview,
  type RepoState,
  type WorkflowOptions,
} from "@/lib/autoreview";
import {
  configure,
  disableRepos,
  listPulls,
  loadRepos,
  loadStatus,
  loadStatusMany,
  prCommand,
  useGh,
  webhookBase,
  type PrCommand,
  type PullSummary,
} from "@/lib/github";
import { DEFAULT_MODEL, KNOWN_MODELS, useApp } from "@/lib/store";
import { errText, openUrl, tauriInvoke } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";

/** How many repo statuses are fetched automatically (each costs ~5 GitHub API calls). */
const AUTO_CHECK = 30;

/* ------------------------------------------------------------------ */
/* Deploy helpers                                                      */
/* ------------------------------------------------------------------ */

function enableRepos(repos: string[]) {
  const s = useApp.getState();
  return configure({
    retryOnly: false,
    repos,
    mode: "actions",
    base: webhookBase(s.cloudBase),
    secret: "",
    orKey: s.secrets.openrouter || "",
    provider: "pr-agent",
    saveSecret: false,
    saveToken: false,
    agentName: "",
    agentCreate: false,
  });
}

/** OpenRabbit repos: remove that workflow first so a PR does not get two reviewers. */
async function switchToPrAgent(repo: string) {
  const removed = await disableRepos([repo], { removeWebhook: false, removeSecrets: false });
  if (!removed) {
    // Installing PR Agent now would leave two reviewers on every PR.
    toastErr("Switch to PR Agent", `Could not remove the OpenRabbit workflow from ${repo}; nothing was installed.`);
    return;
  }
  await enableRepos([repo]);
}

/* ------------------------------------------------------------------ */
/* Step 1 — setup checklist                                            */
/* ------------------------------------------------------------------ */

function StepMark({ n, done }: { n: number; done: boolean }) {
  return done ? (
    <CircleCheck className="text-success size-5 shrink-0" aria-label="done" />
  ) : (
    <span className="bg-muted text-muted-foreground flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold">
      {n}
    </span>
  );
}

function SetupCard() {
  const connected = useApp((s) => !!s.secrets.github);
  const orKey = useApp((s) => s.secrets.openrouter);
  const setSecret = useApp((s) => s.setSecret);
  const login = useGh((s) => s.login);
  const [showSignIn, setShowSignIn] = React.useState(false);

  return (
    <Section
      title="Deploy PR Agent to your repositories"
      description="Three steps: connect GitHub, add a free OpenRouter key, then switch auto review on for any repo below. Reviews run in GitHub Actions — no server to host."
      action={<GithubStateBadge />}
    >
      <div className="grid gap-4">
        <div className="flex gap-3">
          <StepMark n={1} done={connected} />
          <div className="grid min-w-0 flex-1 gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium">Connect GitHub</span>
              {connected ? (
                <>
                  <span className="text-muted-foreground text-xs">{login ? `as ${login}` : "session token"}</span>
                  <Button variant="link" size="xs" className="h-auto px-0" onClick={() => setShowSignIn((v) => !v)}>
                    {showSignIn ? "Hide" : "Switch account"}
                  </Button>
                </>
              ) : null}
            </div>
            {!connected || showSignIn ? <GithubConnect /> : null}
          </div>
        </div>
        <Separator />
        <div className="flex gap-3">
          <StepMark n={2} done={!!orKey} />
          <div className="grid min-w-0 flex-1 gap-2">
            <span className="text-sm font-medium">OpenRouter API key</span>
            <div className="flex flex-wrap gap-2">
              <div className="min-w-56 flex-1">
                <SecretInput
                  aria-label="OpenRouter API key"
                  value={orKey}
                  placeholder="sk-or-…"
                  onChange={(e) => setSecret("openrouter", e.target.value)}
                />
              </div>
              <Button variant="outline" onClick={() => openUrl("https://openrouter.ai/settings/keys")}>
                <ExternalLink /> Get a free key
              </Button>
            </div>
            <Hint className="text-xs">
              Stored encrypted as the repo's <code>OPENROUTER_API_KEY</code> Actions secret. Kept in this session only —
              not needed to update repos that already have the secret.
            </Hint>
          </div>
        </div>
        <Separator />
        <div className="flex gap-3">
          <StepMark n={3} done={false} />
          <div className="grid gap-1">
            <span className="text-sm font-medium">Pick repositories below and click Enable</span>
            <Hint className="text-xs">
              The app commits <code>.github/workflows/pr-agent.yml</code> to the default branch and sets the secret. New
              pull requests are reviewed automatically; comment <code>/review</code>, <code>/describe</code>,{" "}
              <code>/improve</code> or <code>/ask …</code> on any PR to run a command.
            </Hint>
          </div>
        </div>
      </div>
    </Section>
  );
}

/* ------------------------------------------------------------------ */
/* Step 2 — what the reviewer does                                     */
/* ------------------------------------------------------------------ */

const BEHAVIOUR: { key: keyof WorkflowOptions; title: string; hint: string }[] = [
  { key: "auto_review", title: "Review every pull request", hint: "Posts a review with findings and inline comments." },
  { key: "auto_describe", title: "Write the PR description", hint: "Fills in title, summary and walkthrough when a PR opens." },
  { key: "auto_improve", title: "Suggest code improvements", hint: "Posts concrete code suggestions when a PR opens." },
  {
    key: "review_on_push",
    title: "Re-review on every push",
    hint: "Updates the same review comment for each new commit; older runs are cancelled.",
  },
  { key: "skip_drafts", title: "Skip draft PRs", hint: "Waits until the PR is marked ready for review." },
];

const EFFORTS = ["none", "low", "medium", "high"];

function BehaviourCard() {
  const options = useAutoReview((s) => s.options);
  const setOptions = useAutoReview((s) => s.setOptions);
  const resetOptions = useAutoReview((s) => s.resetOptions);
  const [preview, setPreview] = React.useState<string | null>(null);
  const known = KNOWN_MODELS.some((m) => m.value === options.model && m.value !== "custom");
  const [customModel, setCustomModel] = React.useState(!known);
  const problem = optionsProblem(options);

  const showPreview = async () => {
    try {
      setPreview(await tauriInvoke<string>("preview_pr_agent_workflow", { options }));
    } catch (e) {
      toastErr("Preview", errText(e, 240));
    }
  };

  return (
    <Section
      title="What the reviewer does"
      description="Saved on this PC and applied to every repo you enable or update."
      footer={
        <ButtonRow>
          <AsyncButton variant="outline" size="sm" onClick={showPreview} disabled={!!problem}>
            <Eye /> Preview workflow
          </AsyncButton>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              resetOptions();
              setCustomModel(false);
            }}
          >
            <RotateCcw /> Reset to defaults
          </Button>
        </ButtonRow>
      }
    >
      <div className="grid gap-2 sm:grid-cols-2">
        {BEHAVIOUR.map((b) => (
          <Label
            key={b.key}
            className="bg-muted/20 hover:bg-muted/40 flex cursor-pointer items-start justify-between gap-3 rounded-md border px-3 py-2.5 font-normal"
          >
            <span className="grid gap-0.5">
              <span className="text-sm font-medium">{b.title}</span>
              <span className="text-muted-foreground text-xs leading-snug">{b.hint}</span>
            </span>
            <Switch checked={!!options[b.key]} onCheckedChange={(v) => setOptions({ [b.key]: v })} />
          </Label>
        ))}
      </div>

      <FieldGrid>
        <Field label="Model" htmlFor="ar-model" hint="Free OpenRouter models work well; the fallback is used when it is busy.">
          <Select
            value={customModel ? "custom" : options.model}
            onValueChange={(v) => {
              if (v === "custom") {
                setCustomModel(true);
              } else {
                setCustomModel(false);
                setOptions({ model: v });
              }
            }}
          >
            <SelectTrigger id="ar-model" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {KNOWN_MODELS.map((m) => (
                <SelectItem key={m.value} value={m.value}>
                  {m.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        {customModel ? (
          <Field label="Custom model id" htmlFor="ar-custom-model">
            <Input
              id="ar-custom-model"
              value={options.model}
              placeholder={DEFAULT_MODEL}
              onChange={(e) => setOptions({ model: e.target.value })}
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
        ) : null}
      </FieldGrid>

      <Field
        label="Review instructions (optional)"
        htmlFor="ar-instructions"
        hint={`House rules or focus areas, e.g. "Flag missing tests. Ignore formatting." · ${options.extra_instructions.length}/2000`}
      >
        <Textarea
          id="ar-instructions"
          value={options.extra_instructions}
          maxLength={2000}
          rows={3}
          placeholder="Focus on security and error handling. Don't comment on naming style."
          onChange={(e) => setOptions({ extra_instructions: e.target.value })}
        />
      </Field>

      <details className="group">
        <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
          Advanced: fallback models, reasoning effort, token budget
        </summary>
        <div className="mt-3 grid gap-4 sm:grid-cols-3">
          <Field label="Fallback models" htmlFor="ar-fallbacks" hint="Comma separated.">
            <Input
              id="ar-fallbacks"
              value={options.fallback_models.join(", ")}
              onChange={(e) => setOptions({ fallback_models: e.target.value.split(",").map((s) => s.trim()) })}
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <Field label="Reasoning effort" htmlFor="ar-effort">
            <Select value={options.reasoning_effort} onValueChange={(v) => setOptions({ reasoning_effort: v })}>
              <SelectTrigger id="ar-effort" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {EFFORTS.map((e) => (
                  <SelectItem key={e} value={e}>
                    {e}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label="Max model tokens" htmlFor="ar-max-tokens">
            <Input
              id="ar-max-tokens"
              inputMode="numeric"
              value={String(options.max_tokens)}
              onChange={(e) => setOptions({ max_tokens: Number(e.target.value.replace(/\D/g, "")) || 0 })}
              autoComplete="off"
            />
          </Field>
        </div>
      </details>

      {problem ? (
        <p className="text-destructive flex items-center gap-2 text-sm" role="alert">
          <TriangleAlert className="size-4 shrink-0" /> {problem}
        </p>
      ) : null}

      <Dialog open={preview !== null} onOpenChange={(o) => !o && setPreview(null)}>
        <DialogContent className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>.github/workflows/pr-agent.yml</DialogTitle>
            <DialogDescription>Exactly what Enable / Update commits with the current settings.</DialogDescription>
          </DialogHeader>
          <Console className="max-h-[60vh]">{preview}</Console>
        </DialogContent>
      </Dialog>
    </Section>
  );
}

/* ------------------------------------------------------------------ */
/* Step 3 — repositories                                               */
/* ------------------------------------------------------------------ */

const STATE_TONE: Record<RepoState, "success" | "warning" | "danger" | "outline"> = {
  unknown: "outline",
  error: "danger",
  on: "success",
  outdated: "warning",
  custom: "warning",
  "needs-key": "danger",
  openrabbit: "success",
  backend: "outline",
  off: "outline",
};

type Filter = "all" | "active" | "attention" | "off";

function needsAttention(state: RepoState, differs: boolean) {
  return state === "outdated" || state === "custom" || state === "needs-key" || state === "error" || differs;
}

function useRepoView(name: string) {
  const st = useGh((s) => s.status[name]);
  const options = useAutoReview((s) => s.options);
  const state = repoState(st);
  const wf = (st && st.workflow) || {};
  const differs = state === "on" && wf.managed && !sameOptions(wf.settings, options);
  return { st, state, differs: !!differs };
}

function RepoRow({
  name,
  onTry,
  onDisable,
}: {
  name: string;
  onTry: (repo: string) => void;
  onDisable: (repos: string[]) => void;
}) {
  const repo = useGh((s) => s.repos.find((r) => r.full_name === name));
  const selected = useGh((s) => s.selected.includes(name));
  const working = useGh((s) => s.configuring.includes(name));
  const batchRunning = useGh((s) => s.batchRunning);
  const toggle = useGh((s) => s.toggleSelected);
  const { st, state, differs } = useRepoView(name);
  if (!repo) return null;

  const errs: string[] = [];
  if (st && st.error) errs.push(String(st.error));
  for (const k of ["webhook", "workflow", "secrets"]) if (st && st[k] && st[k].error) errs.push(`${k}: ${st[k].error}`);
  const why = "Admin access is required to add workflows and secrets.";

  let primary: { label: string; run: () => unknown; variant: "default" | "outline" } | null = null;
  if (repo.admin) {
    if (state === "off" || state === "unknown" || state === "backend") primary = { label: "Enable", run: () => enableRepos([name]), variant: "default" };
    else if (state === "outdated" || state === "custom" || differs) primary = { label: "Update", run: () => enableRepos([name]), variant: "default" };
    else if (state === "needs-key") primary = { label: "Add key", run: () => enableRepos([name]), variant: "default" };
    else if (state === "openrabbit") primary = { label: "Switch to PR Agent", run: () => switchToPrAgent(name), variant: "outline" };
    else if (state === "error") primary = { label: "Retry", run: () => loadStatus(name, true), variant: "outline" };
  }
  const active = isActive(state) || state === "needs-key";
  // /review etc. are answered only by the PR Agent workflow with its secret in place.
  const runsPrAgent = state === "on" || state === "outdated" || state === "custom";

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border px-3 py-2 transition-colors",
        selected && "border-primary/50 bg-primary/5",
        working && "animate-pulse",
        !repo.admin && "opacity-60",
      )}
      data-repo={name}
    >
      <Checkbox
        checked={selected}
        disabled={!repo.admin}
        onCheckedChange={(v) => toggle(name, v === true)}
        aria-label={`Select ${name}`}
        title={repo.admin ? undefined : why}
      />
      <div className="grid min-w-0 flex-1 gap-1">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="truncate font-mono text-xs font-medium">{name}</span>
          {repo.private ? <Badge variant="outline">private</Badge> : null}
          {!repo.admin ? (
            <Badge variant="outline" title={why}>
              no admin
            </Badge>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant={STATE_TONE[state]} title={state === "custom" ? "A pr-agent.yml exists that this app did not write. Update replaces it." : undefined}>
            {working ? "Working…" : STATE_LABEL[state]}
          </Badge>
          {differs ? (
            <Badge variant="warning" title="This repo runs different settings than the ones above. Update applies them.">
              settings differ
            </Badge>
          ) : null}
          {state === "backend" ? (
            <span className="text-muted-foreground text-xs">Enable adds the Action; remove the webhook to avoid two reviews.</span>
          ) : null}
        </div>
        {errs.length ? <p className="text-destructive text-xs break-words">{errs.join(" · ").slice(0, 240)}</p> : null}
      </div>
      <div className="flex items-center gap-1">
        {primary ? (
          <AsyncButton size="sm" variant={primary.variant} disabled={batchRunning} onClick={primary.run}>
            {primary.label === "Enable" ? <Rocket /> : null}
            {primary.label}
          </AsyncButton>
        ) : state === "on" ? (
          <span className="text-success flex items-center gap-1 px-2 text-xs font-medium">
            <CircleCheck className="size-3.5" /> Up to date
          </span>
        ) : null}
        {active ? (
          <>
            {runsPrAgent ? (
              <>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon-sm" onClick={() => onTry(name)} aria-label={`Run on a pull request in ${name}`}>
                  <FlaskConical />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Run on a pull request</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon-sm" onClick={() => openUrl(actionsUrl(name))} aria-label={`View runs for ${name}`}>
                  <ExternalLink />
                </Button>
              </TooltipTrigger>
              <TooltipContent>View runs on GitHub</TooltipContent>
            </Tooltip>
              </>
            ) : null}
            {repo.admin ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    disabled={batchRunning}
                    onClick={() => onDisable([name])}
                    aria-label={`Turn off auto review for ${name}`}
                  >
                    <PowerOff />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Turn off</TooltipContent>
              </Tooltip>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  );
}

function DisableDialog({ repos, onClose }: { repos: string[]; onClose: () => void }) {
  const cloudBase = useApp((s) => s.cloudBase);
  const hookUrl = `${webhookBase(cloudBase)}/webhooks/github`;
  const [removeWebhook, setRemoveWebhook] = React.useState(true);
  const [removeSecrets, setRemoveSecrets] = React.useState(false);
  const open = repos.length > 0;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Turn off auto review?</DialogTitle>
          <DialogDescription>
            Deletes <code>.github/workflows/pr-agent.yml</code> (and <code>openrabbit.yml</code>) from the default branch of{" "}
            {repos.length === 1 ? <b>{repos[0]}</b> : `${repos.length} repositories`} with a commit. It stays in git history,
            and you can enable it again at any time.
          </DialogDescription>
        </DialogHeader>
        {repos.length > 1 ? (
          <ul className="text-muted-foreground max-h-32 overflow-y-auto font-mono text-xs">
            {repos.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        ) : null}
        <div className="grid gap-2.5">
          <Label className="font-normal">
            <Checkbox checked={removeWebhook} onCheckedChange={(v) => setRemoveWebhook(v === true)} />
            <span>
              Also remove the webhook for this backend (<code className="break-all">{hookUrl}</code>) if present
            </span>
          </Label>
          <Label className="font-normal">
            <Checkbox checked={removeSecrets} onCheckedChange={(v) => setRemoveSecrets(v === true)} />
            <span>
              Also delete the <code>OPENROUTER_API_KEY</code> / <code>LLM_API_KEY</code> repo secrets
            </span>
          </Label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <AsyncButton
            variant="destructive"
            onClick={async () => {
              const list = repos.slice();
              onClose();
              await disableRepos(list, { removeWebhook, removeSecrets, webhookUrl: hookUrl });
            }}
          >
            <PowerOff /> Turn off
          </AsyncButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function TryDialog({ repo, onClose }: { repo: string | null; onClose: () => void }) {
  const [pulls, setPulls] = React.useState<PullSummary[] | null>(null);
  const [error, setError] = React.useState("");
  const [picked, setPicked] = React.useState<number | null>(null);
  const [posted, setPosted] = React.useState("");

  React.useEffect(() => {
    if (!repo) return;
    let live = true;
    setPulls(null);
    setError("");
    setPicked(null);
    setPosted("");
    listPulls(repo)
      .then((p) => {
        if (!live) return;
        setPulls(p);
        setPicked(p.length ? p[0].number : null);
      })
      .catch((e) => live && setError(errText(e, 240)));
    return () => {
      live = false;
    };
  }, [repo]);

  const run = (cmd: PrCommand) => async () => {
    if (!repo || picked == null) return;
    try {
      const url = await prCommand(repo, picked, cmd);
      setPosted(url);
      toastOk("Command posted", `${cmd} on ${repo}#${picked} — the Action starts in a few seconds.`);
    } catch (e) {
      toastErr("Command failed", errText(e, 220));
    }
  };

  return (
    <Dialog open={!!repo} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Run PR Agent on a pull request</DialogTitle>
          <DialogDescription>
            Posts the command as a comment on the PR in <b>{repo}</b>; the workflow picks it up and replies there.
          </DialogDescription>
        </DialogHeader>
        {error ? (
          <p className="text-destructive text-sm">{error}</p>
        ) : pulls === null ? (
          <Hint>Loading open pull requests…</Hint>
        ) : !pulls.length ? (
          <Hint>No open pull requests. Open one and it is reviewed automatically.</Hint>
        ) : (
          <div className="grid max-h-64 gap-1.5 overflow-y-auto" role="radiogroup" aria-label="Pull request">
            {pulls.map((p) => (
              <button
                type="button"
                role="radio"
                aria-checked={picked === p.number}
                key={p.number}
                onClick={() => setPicked(p.number)}
                className={cn(
                  "hover:bg-muted/50 flex items-center gap-2 rounded-md border px-3 py-2 text-left text-sm",
                  picked === p.number && "border-primary/60 bg-primary/5",
                )}
              >
                <GitPullRequest className="text-muted-foreground size-4 shrink-0" />
                <span className="text-muted-foreground font-mono text-xs">#{p.number}</span>
                <span className="min-w-0 flex-1 truncate">{p.title}</span>
                {p.draft ? <Badge variant="outline">draft</Badge> : null}
              </button>
            ))}
          </div>
        )}
        {posted ? (
          <Button variant="link" size="sm" className="justify-self-start px-0" onClick={() => openUrl(posted)}>
            <ExternalLink /> Open the comment on GitHub
          </Button>
        ) : null}
        <DialogFooter className="flex-wrap">
          {repo ? (
            <Button variant="ghost" onClick={() => openUrl(actionsUrl(repo))}>
              <ExternalLink /> View runs
            </Button>
          ) : null}
          {(["/review", "/describe", "/improve"] as PrCommand[]).map((c) => (
            <AsyncButton key={c} variant={c === "/review" ? "default" : "outline"} disabled={picked == null} onClick={run(c)}>
              {c}
            </AsyncButton>
          ))}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ReposCard({ active }: { active: boolean }) {
  const connected = useApp((s) => !!s.secrets.github);
  const repos = useGh((s) => s.repos);
  const status = useGh((s) => s.status);
  const loadingRepos = useGh((s) => s.loadingRepos);
  const selected = useGh((s) => s.selected);
  const batchRunning = useGh((s) => s.batchRunning);
  const log = useGh((s) => s.log);
  const options = useAutoReview((s) => s.options);
  const clearNonce = useApp((s) => s.clearNonce);

  const [query, setQuery] = React.useState("");
  const [filter, setFilter] = React.useState<Filter>("all");
  const [tryRepo, setTryRepo] = React.useState<string | null>(null);
  const [disableList, setDisableList] = React.useState<string[]>([]);
  React.useEffect(() => setQuery(""), [clearNonce]);

  // Check the most recently updated repos automatically, once per repo list.
  const checkedFor = React.useRef<unknown>(null);
  React.useEffect(() => {
    if (!active || !connected || !repos.length || checkedFor.current === repos) return;
    checkedFor.current = repos;
    const known = useGh.getState().status;
    const todo = repos.filter((r) => r.admin && !known[r.full_name]).slice(0, AUTO_CHECK).map((r) => r.full_name);
    if (todo.length) void loadStatusMany(todo, false);
  }, [active, connected, repos]);

  const views = repos.map((r) => {
    const st = status[r.full_name];
    const state = repoState(st);
    const wf = (st && st.workflow) || {};
    const differs = state === "on" && !!wf.managed && !sameOptions(wf.settings, options);
    return { repo: r, state, differs };
  });
  const counts = {
    active: views.filter((v) => isActive(v.state)).length,
    attention: views.filter((v) => needsAttention(v.state, v.differs)).length,
    unchecked: views.filter((v) => v.state === "unknown" && v.repo.admin).length,
  };
  const q = query.trim().toLowerCase();
  const visible = views.filter((v) => {
    if (q && !v.repo.full_name.toLowerCase().includes(q)) return false;
    if (filter === "active") return isActive(v.state);
    if (filter === "attention") return needsAttention(v.state, v.differs);
    if (filter === "off") return v.state === "off";
    return true;
  });
  const checkMore = () => {
    const todo = visible.filter((v) => v.state === "unknown" && v.repo.admin).slice(0, AUTO_CHECK).map((v) => v.repo.full_name);
    if (!todo.length) return toastOk("Status", "Every listed repository has been checked.");
    return loadStatusMany(todo, false);
  };
  const updatable = views
    .filter((v) => v.repo.admin && (v.state === "outdated" || v.state === "custom" || v.differs))
    .map((v) => v.repo.full_name);
  const selectedActive = selected.filter((n) => {
    const s = repoState(status[n]);
    return isActive(s) || s === "needs-key";
  });
  const optionsOk = !optionsProblem(options);

  return (
    <Section
      title="Repositories"
      description="Repos where you are an admin can be switched on. Status is read live from GitHub."
      action={
        connected ? (
          <div className="flex flex-wrap justify-end gap-1.5">
            <Badge variant="success">{counts.active} on</Badge>
            {counts.attention ? <Badge variant="warning">{counts.attention} need attention</Badge> : null}
          </div>
        ) : null
      }
    >
      {!connected ? (
        <Hint className="flex items-center gap-2">
          <KeyRound className="size-4" /> Connect GitHub above to list your repositories.
        </Hint>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-48 flex-1">
              <Search className="text-muted-foreground absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search repositories…"
                className="pl-8"
                autoComplete="off"
                aria-label="Search repositories"
              />
            </div>
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={filter}
              onValueChange={(v) => v && setFilter(v as Filter)}
              aria-label="Filter repositories"
            >
              <ToggleGroupItem value="all">All</ToggleGroupItem>
              <ToggleGroupItem value="active">On</ToggleGroupItem>
              <ToggleGroupItem value="attention">Attention</ToggleGroupItem>
              <ToggleGroupItem value="off">Off</ToggleGroupItem>
            </ToggleGroup>
          </div>
          <ButtonRow>
            <AsyncButton variant="outline" size="sm" disabled={batchRunning || loadingRepos} onClick={loadRepos}>
              <RefreshCw /> Refresh list
            </AsyncButton>
            <AsyncButton variant="outline" size="sm" disabled={batchRunning || !counts.unchecked} onClick={checkMore}>
              <RefreshCw /> Check status{counts.unchecked ? ` (${Math.min(counts.unchecked, AUTO_CHECK)} more)` : ""}
            </AsyncButton>
            {updatable.length ? (
              <AsyncButton size="sm" disabled={batchRunning || !optionsOk} onClick={() => enableRepos(updatable)}>
                Update all ({updatable.length})
              </AsyncButton>
            ) : null}
          </ButtonRow>

          <div className="grid max-h-[28rem] gap-2 overflow-y-auto pr-1">
            {loadingRepos && !repos.length ? (
              <Hint>Loading repositories…</Hint>
            ) : !repos.length ? (
              <Hint>No repositories loaded — click Refresh list.</Hint>
            ) : !visible.length ? (
              <Hint>No repositories match.</Hint>
            ) : (
              visible.map((v) => (
                <RepoRow key={v.repo.full_name} name={v.repo.full_name} onTry={setTryRepo} onDisable={setDisableList} />
              ))
            )}
          </div>

          {selected.length ? (
            <div className="bg-muted/40 flex flex-wrap items-center gap-2 rounded-md border px-3 py-2">
              <span className="text-sm font-medium">{selected.length} selected</span>
              <AsyncButton size="sm" disabled={batchRunning || !optionsOk} onClick={() => enableRepos(selected.slice())}>
                <Rocket /> Enable / update ({selected.length})
              </AsyncButton>
              {selectedActive.length ? (
                <Button variant="outline" size="sm" disabled={batchRunning} onClick={() => setDisableList(selectedActive)}>
                  <PowerOff /> Turn off ({selectedActive.length})
                </Button>
              ) : null}
              <Button
                variant="ghost"
                size="sm"
                className="ml-auto"
                onClick={() => useGh.setState({ selected: [] })}
              >
                <X /> Clear
              </Button>
            </div>
          ) : null}

          <details className="group">
            <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
              Activity log
            </summary>
            <Console className="mt-2" tall>
              {log}
            </Console>
          </details>
          <Hint className="text-xs">
            Need the webhook/backend path or the OpenRabbit reviewer? Use Integrations → GitHub 1-click repo setup.
          </Hint>
        </>
      )}
      <TryDialog repo={tryRepo} onClose={() => setTryRepo(null)} />
      <DisableDialog repos={disableList} onClose={() => setDisableList([])} />
    </Section>
  );
}

export function AutoReviewPage({ active }: { active: boolean }) {
  return (
    <Page active={active}>
      <SetupCard />
      <BehaviourCard />
      <ReposCard active={active} />
    </Page>
  );
}
