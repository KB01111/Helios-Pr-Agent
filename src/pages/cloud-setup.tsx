import * as React from "react";
import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Bot,
  Brain,
  Check,
  CheckCircle2,
  ChevronDown,
  ClipboardCopy,
  Cloud,
  ExternalLink,
  FileCode2,
  GitBranch,
  GitPullRequest,
  KeyRound,
  Loader2,
  Play,
  Plug,
  Plus,
  RefreshCw,
  Rocket,
  Server,
  ShieldCheck,
  Shuffle,
  Sparkles,
  Terminal,
  Wifi,
  Wrench,
  X,
} from "lucide-react";
import {
  AsyncButton,
  ButtonRow,
  Console,
  Field,
  FieldGrid,
  Hint,
  KV,
  Page,
  SecretInput,
  Section,
  StatusDot,
} from "@/components/common";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
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
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { agentList, checkBackend, refreshAgents } from "@/lib/actions";
import { apiGet, apiMut, backendUrl } from "@/lib/api";
import { cloudModelId, fetchAppHealth, saveCloudSettings, type AppHealth } from "@/lib/cloud";
import {
  bandBadgeVariant,
  bandLabel,
  bootstrapRepoMemory,
  copyToClipboard,
  getPullReadiness,
  isValidRepo,
  renderPragentYaml,
  triggerManualReview,
  type ManualReviewResponse,
  type ReadinessReport,
} from "@/lib/cloud-agent";
import { ghRandomSecret, listPulls, loadRepos, useGh, type PullSummary } from "@/lib/github";
import {
  cancelOAuth,
  connectGitHubOAuth,
  connectOpenRouterOAuth,
  importCommandCodeCli,
  importGitHubCli,
  importOpenCodeCli,
  resolvePublicWebhookBase,
  useOAuth,
} from "@/lib/oauth";
import { saveSecretToVault, setRememberOnPc } from "@/lib/session";
import {
  defaultModelFor,
  LLM_PROVIDERS,
  modelsForProvider,
  toProviderId,
  useApp,
  type LlmProviderId,
  type MergeGateMode,
  type ReviewProfile,
} from "@/lib/store";
import { errText, githubCommitRepoFile, isTauri, openUrl } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";

const STEPS = [
  { id: 1, short: "Backend", title: "Cloud Backend", icon: Server },
  { id: 2, short: "AI Model", title: "AI Model", icon: Sparkles },
  { id: 3, short: "GitHub App", title: "Personal GitHub App", icon: GitBranch },
  { id: 4, short: "Deploy", title: "Configure & Deploy", icon: Bot },
  { id: 5, short: "Verify", title: "Verify Live on a PR", icon: Play },
] as const;

const LLM_STATUS_KEY: Record<LlmProviderId, string> = {
  openrouter: "openrouter",
  commandcode: "commandcode",
  opencode_go: "opencode_go",
};

function CopyBlock({ title, code }: { title: string; code: string }) {
  const [copied, setCopied] = React.useState(false);
  const onCopy = async () => {
    const ok = await copyToClipboard(code);
    if (ok) {
      setCopied(true);
      toastOk("Copied", title);
      setTimeout(() => setCopied(false), 1600);
    } else {
      toastErr("Copy failed", "Select and copy the snippet manually.");
    }
  };
  return (
    <div className="bg-muted/30 grid gap-2 rounded-lg border p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold">{title}</span>
        <Button type="button" variant="ghost" size="xs" onClick={onCopy}>
          {copied ? <Check className="text-success" /> : <ClipboardCopy />}
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <pre className="bg-background/70 text-muted-foreground overflow-x-auto rounded border px-2.5 py-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
        {code}
      </pre>
    </div>
  );
}

export function CloudSetupPage({ active }: { active: boolean }) {
  const setTab = useApp((s) => s.setTab);
  const currentBackendUrl = useApp((s) => s.backendUrl);
  const setBackendUrl = useApp((s) => s.setBackendUrl);
  const backend = useApp((s) => s.backend);
  const cloudBase = useApp((s) => s.cloudBase);
  const setCloudBase = useApp((s) => s.setCloudBase);
  const apiToken = useApp((s) => s.secrets.api);
  const githubToken = useApp((s) => s.secrets.github);
  const allSecrets = useApp((s) => s.secrets);
  const setSecret = useApp((s) => s.setSecret);
  const rememberSecrets = useApp((s) => s.rememberSecrets);
  const vaultKeys = useApp((s) => s.vaultKeys);
  const agents = useApp((s) => s.agents);
  const clearNonce = useApp((s) => s.clearNonce);

  const ghRepos = useGh((s) => s.repos);
  const ghLogin = useGh((s) => s.login);
  const loadingRepos = useGh((s) => s.loadingRepos);

  const activeFlow = useOAuth((s) => s.activeFlow);
  const flowMessage = useOAuth((s) => s.flowMessage);
  const oauthStatus = useOAuth((s) => s.status);
  const oauthRevision = useOAuth((s) => s.revision);

  const [step, setStep] = React.useState<number>(1);

  // Live backend diagnostics
  const [diag, setDiag] = React.useState<any>(undefined); // undefined=loading, null=unreachable
  const [diagErr, setDiagErr] = React.useState("");
  const [health, setHealth] = React.useState<AppHealth | null | undefined>(undefined);
  const diagSeq = React.useRef(0);

  // Step 1 state
  const [urlDraft, setUrlDraft] = React.useState(currentBackendUrl);
  const [deployGuideOpen, setDeployGuideOpen] = React.useState(false);
  const [step1Log, setStep1Log] = React.useState("");
  React.useEffect(() => setUrlDraft(currentBackendUrl), [currentBackendUrl]);

  // Step 2 state
  const [llmProvider, setLlmProvider] = React.useState<LlmProviderId>("openrouter");
  const [llmModel, setLlmModel] = React.useState<string>(defaultModelFor("openrouter"));
  const [llmCustomModel, setLlmCustomModel] = React.useState("");
  const [manualKey, setManualKey] = React.useState("");
  const llmDirtyRef = React.useRef(false);
  const [llmTestResult, setLlmTestResult] = React.useState<{ ok: boolean; text: string } | null>(null);
  React.useEffect(() => setManualKey(""), [clearNonce]);

  // Step 3 state
  const [appName, setAppName] = React.useState("PR Agent Control");
  const [appOrg, setAppOrg] = React.useState("");
  const [webhookOverrideDraft, setWebhookOverrideDraft] = React.useState(cloudBase);
  React.useEffect(() => setWebhookOverrideDraft(cloudBase), [cloudBase]);

  // Step 4 state
  const [profile, setProfile] = React.useState<ReviewProfile>("chill");
  const [mergeGate, setMergeGate] = React.useState<MergeGateMode>("advisory");
  const [scoreThreshold, setScoreThreshold] = React.useState<number>(85);
  const [memoryEnabled, setMemoryEnabled] = React.useState<boolean>(true);
  const [chatEnabled, setChatEnabled] = React.useState<boolean>(true);
  const [targetAllRepos, setTargetAllRepos] = React.useState<boolean>(true);
  const [selectedRepos, setSelectedRepos] = React.useState<string[]>([]);
  const [repoSearch, setRepoSearch] = React.useState("");
  const [manualRepoInput, setManualRepoInput] = React.useState("");
  const [agentName, setAgentName] = React.useState("pr-agent-control");
  const [deployLog, setDeployLog] = React.useState("");
  const [yamlDialogOpen, setYamlDialogOpen] = React.useState(false);
  const [yamlRepoTarget, setYamlRepoTarget] = React.useState("");

  // Step 5 state
  const [verifyRepo, setVerifyRepo] = React.useState("");
  const [pulls, setPulls] = React.useState<PullSummary[]>([]);
  const [selectedPr, setSelectedPr] = React.useState<string>("");
  const [manualPrNum, setManualPrNum] = React.useState<string>("");
  const [forceFull, setForceFull] = React.useState<boolean>(false);
  const [reviewRes, setReviewRes] = React.useState<ManualReviewResponse | null>(null);
  const [verifiedReadiness, setVerifiedReadiness] = React.useState<ReadinessReport | null>(null);
  const [verifyErr, setVerifyErr] = React.useState("");

  const refreshDiagnostics = React.useCallback(async () => {
    const mine = ++diagSeq.current;
    const base = backendUrl();
    const isCurrent = () => mine === diagSeq.current && backendUrl() === base;
    setDiagErr("");
    try {
      const st = await apiGet("/config/status");
      if (!isCurrent()) return;
      setDiag(st);
      if (st?.llm_provider && !llmDirtyRef.current) {
        const pid = toProviderId(String(st.llm_provider));
        const model = String(st.model || "");
        const listed = modelsForProvider(pid).find((m) => m.value !== "custom" && cloudModelId(pid, m.value) === model);
        setLlmProvider(pid);
        setLlmModel(listed ? listed.value : model ? "custom" : defaultModelFor(pid));
        setLlmCustomModel(listed ? "" : model);
      }
      const [h] = await Promise.all([fetchAppHealth(), refreshAgents()]);
      if (!isCurrent()) return;
      setHealth(h);
    } catch (e) {
      if (!isCurrent()) return;
      setDiag(null);
      setHealth(null);
      setDiagErr(errText(e, 240));
    }
  }, []);

  React.useEffect(() => {
    if (active) void refreshDiagnostics();
  }, [active, currentBackendUrl, oauthRevision, refreshDiagnostics]);

  // Seed step 4 controls from an existing backend agent when loaded (reset on backend switch)
  const hydratedAgentRef = React.useRef(false);
  React.useEffect(() => {
    hydratedAgentRef.current = false;
    llmDirtyRef.current = false;
  }, [currentBackendUrl]);
  React.useEffect(() => {
    if (hydratedAgentRef.current || !agents.length) return;
    const primary = agents.find((a) => a.name === "pr-agent-control" || a.id === "pr-agent-control") || agents[0];
    if (!primary) return;
    hydratedAgentRef.current = true;
    setAgentName(primary.name || "pr-agent-control");
    if (primary.profile === "chill" || primary.profile === "assertive") setProfile(primary.profile);
    if (primary.merge_gate === "off" || primary.merge_gate === "advisory" || primary.merge_gate === "required") {
      setMergeGate(primary.merge_gate);
    }
    if (typeof primary.score_threshold === "number") setScoreThreshold(primary.score_threshold);
    if (typeof primary.memory_enabled === "boolean") setMemoryEnabled(primary.memory_enabled);
    if (typeof primary.chat_enabled === "boolean") setChatEnabled(primary.chat_enabled);
    const targets = Array.isArray(primary.target_repos) ? primary.target_repos : [];
    if (targets.length > 0) {
      setTargetAllRepos(false);
      setSelectedRepos(targets);
    } else {
      setTargetAllRepos(true);
    }
  }, [agents]);

  // Available repos list (GitHub repos + agent targets + selected repos)
  const knownRepos = React.useMemo(() => {
    const s = new Set<string>();
    for (const r of ghRepos) s.add(r.full_name);
    for (const a of agents) {
      if (Array.isArray(a.target_repos)) {
        for (const r of a.target_repos) s.add(r);
      }
    }
    for (const r of selectedRepos) s.add(r);
    return Array.from(s).sort();
  }, [ghRepos, agents, selectedRepos]);

  React.useEffect(() => {
    if (!verifyRepo && knownRepos.length > 0) setVerifyRepo(knownRepos[0]);
    if (!yamlRepoTarget && knownRepos.length > 0) setYamlRepoTarget(knownRepos[0]);
  }, [knownRepos, verifyRepo, yamlRepoTarget]);

  // Step completion status calculations
  const backendReachable = diag !== undefined && diag !== null;
  const authProtected = Boolean(diag?.auth_required);
  const encryptionReady = Boolean(diag?.settings?.encryption);
  const redisReady = Boolean(diag?.redis?.configured);
  const step1Done = backendReachable && authProtected && encryptionReady;

  const activeBackendLlm = toProviderId(String(diag?.llm_provider || llmProvider));
  const llmKeyReady = Boolean(diag?.[LLM_STATUS_KEY[activeBackendLlm]]?.configured);
  const step2Done = backendReachable && llmKeyReady;

  const webhookBase = resolvePublicWebhookBase(cloudBase, currentBackendUrl);
  const expectedWebhookUrl = webhookBase ? `${webhookBase}/webhooks/github` : "";
  const webhookMismatch = Boolean(
    health?.configured && expectedWebhookUrl && health.webhook_url && health.webhook_url !== expectedWebhookUrl,
  );
  const appInstallUrl =
    (health?.html_url ? `${health.html_url.replace(/\/+$/, "")}/installations/new` : "") ||
    oauthStatus.github.install_url ||
    "";
  const step3Done = Boolean(health?.configured && health?.ok && !webhookMismatch);
  const step4Done = agents.length > 0;
  const step5Done = Boolean(reviewRes && reviewRes.status && !/error|fail/i.test(reviewRes.status));

  const stepDoneMap: Record<number, boolean> = {
    1: step1Done,
    2: step2Done,
    3: step3Done,
    4: step4Done,
    5: step5Done,
  };
  const completedCount = Object.values(stepDoneMap).filter(Boolean).length;
  const progressPct = Math.round((completedCount / STEPS.length) * 100);

  /* -------------------- Step 1 handlers -------------------- */
  const commitBackendUrl = () => {
    const clean = urlDraft.trim().replace(/\/+$/, "");
    if (clean === currentBackendUrl) return;
    setBackendUrl(clean);
  };

  const generateApiToken = async () => {
    const tok = ghRandomSecret();
    setSecret("api", tok);
    if (rememberSecrets) {
      await saveSecretToVault("api", tok).catch(() => {});
    }
    const copied = await copyToClipboard(tok);
    toastOk(
      "Generated BACKEND_API_TOKEN",
      copied
        ? "Copied to clipboard — set this same value as BACKEND_API_TOKEN on your backend host."
        : "Set this same value as BACKEND_API_TOKEN on your backend host.",
    );
  };

  const testBackendStep = async () => {
    commitBackendUrl();
    setStep1Log("Testing backend connection, auth, Redis, and encryption…");
    const ok = await checkBackend(true);
    await refreshDiagnostics();
    if (!ok) {
      setStep1Log(`Could not reach ${useApp.getState().backendUrl}. Check URL, CORS, and BACKEND_API_TOKEN.`);
      return;
    }
    try {
      const st = await apiGet("/config/status");
      setStep1Log(
        [
          `✓ Reachable: ${useApp.getState().backendUrl}`,
          `• Auth: ${st.auth_required ? "BACKEND_API_TOKEN active" : "OPEN — set BACKEND_API_TOKEN in backend env"}`,
          `• Encryption: ${st.settings?.encryption ? "active (Fernet/HKDF)" : "disabled until BACKEND_API_TOKEN is set"}`,
          `• Store: ${st.redis?.configured ? `${st.redis.mode} (${st.redis.source})` : "in-memory only (connect Redis Cloud / REDIS_URL)"}`,
        ].join("\n"),
      );
    } catch (e) {
      setStep1Log(`Connected to /health, but /config/status returned: ${errText(e, 240)}`);
    }
  };

  /* -------------------- Step 2 handlers -------------------- */
  const saveLlmToCloud = async () => {
    const chosen = llmModel === "custom" ? llmCustomModel.trim() : llmModel;
    const modelId = cloudModelId(llmProvider, chosen || defaultModelFor(llmProvider));
    const providerMeta = LLM_PROVIDERS[llmProvider];
    const values: Record<string, string | null> = {
      LLM_PROVIDER: llmProvider,
      LLM_MODEL: modelId || null,
    };
    const keyTrimmed = manualKey.trim();
    if (keyTrimmed) {
      values[providerMeta.envKey] = keyTrimmed;
    }
    try {
      await saveCloudSettings(values);
      if (keyTrimmed) {
        setSecret(providerMeta.secret, keyTrimmed);
        setManualKey("");
      }
      llmDirtyRef.current = false;
      toastOk("AI model saved to cloud", `${providerMeta.label} · ${modelId || "default"}`);
      await refreshDiagnostics();
    } catch (e) {
      toastErr("Cloud save failed", errText(e, 260));
    }
  };

  const testLlmLive = async () => {
    setLlmTestResult(null);
    try {
      const res = await apiMut<{ ok?: boolean; details?: string }>("POST", "/config/test", { provider: llmProvider });
      const ok = Boolean(res?.ok);
      const text = res?.details || (ok ? "Provider responded cleanly." : "Provider check failed.");
      setLlmTestResult({ ok, text });
      if (ok) toastOk("Model test passed", text);
      else toastErr("Model test failed", text);
    } catch (e) {
      const text = errText(e, 240);
      setLlmTestResult({ ok: false, text });
      toastErr("Model test failed", text);
    }
  };

  /* -------------------- Step 3 handlers -------------------- */
  const fixWebhookUrl = async () => {
    if (!webhookBase) {
      toastErr("Webhook URL", "Enter a public HTTPS backend or Cloudflare Worker URL first.");
      return;
    }
    try {
      await apiMut("POST", "/github-app/webhook", { base_url: webhookBase });
      toastOk("Webhook URL updated", `${webhookBase}/webhooks/github`);
      await refreshDiagnostics();
    } catch (e) {
      toastErr("Webhook update failed", errText(e, 260));
    }
  };

  /* -------------------- Step 4 handlers -------------------- */
  const addManualRepo = () => {
    const clean = manualRepoInput.trim();
    if (!isValidRepo(clean)) {
      toastErr("Repository", "Enter a repository in owner/name format.");
      return;
    }
    if (!selectedRepos.includes(clean)) setSelectedRepos((prev) => [...prev, clean]);
    setTargetAllRepos(false);
    setManualRepoInput("");
  };

  const toggleSelectedRepo = (repo: string, checked: boolean) => {
    setSelectedRepos((prev) => (checked ? Array.from(new Set([...prev, repo])) : prev.filter((r) => r !== repo)));
  };

  const effectiveTargetRepos = targetAllRepos ? [] : selectedRepos;

  const yamlPreview = React.useMemo(
    () =>
      renderPragentYaml({
        profile,
        merge_gate: mergeGate,
        score_threshold: scoreThreshold,
        memory_enabled: memoryEnabled,
        chat_enabled: chatEnabled,
      }),
    [profile, mergeGate, scoreThreshold, memoryEnabled, chatEnabled],
  );

  const deployCloudAgent = async () => {
    const cleanName = agentName.trim() || "pr-agent-control";
    setDeployLog(`Deploying agent "${cleanName}"…`);
    try {
      const existingList = agentList(await apiGet("/agents"));
      const existing = existingList.find((a) => a.name === cleanName || a.id === cleanName);
      const body = {
        ...(existing || {}),
        name: cleanName,
        target_repos: effectiveTargetRepos,
        trigger: existing?.trigger || "on_commit",
        trigger_label: existing?.trigger === "on_label" ? existing.trigger_label || null : null,
        runtime_mode: existing?.runtime_mode || "review_only",
        schema_version: 2,
        profile,
        merge_gate: mergeGate,
        score_threshold: scoreThreshold,
        memory_enabled: memoryEnabled,
        chat_enabled: chatEnabled,
        autofix: existing?.autofix || "suggest",
        path_instructions: Array.isArray(existing?.path_instructions) ? existing.path_instructions : [],
      };
      const saved =
        existing && existing.id
          ? await apiMut("PUT", `/agents/${encodeURIComponent(existing.id)}`, body)
          : await apiMut("POST", "/agents", { id: cleanName === "pr-agent-control" ? "pr-agent-control" : "", ...body });
      await refreshAgents();
      const targetDesc = effectiveTargetRepos.length ? `${effectiveTargetRepos.length} repo(s)` : "all installed repos";
      const line = `✓ Deployed agent "${saved?.name || cleanName}" (${profile}, gate=${mergeGate} @ ${scoreThreshold}, memory=${memoryEnabled ? "on" : "off"}, chat=${chatEnabled ? "on" : "off"}) -> ${targetDesc}`;
      setDeployLog(line);
      toastOk("Cloud Agent deployed", `${cleanName} · ${targetDesc}`);
    } catch (e) {
      const msg = errText(e, 260);
      setDeployLog(`✗ Deploy failed: ${msg}`);
      toastErr("Agent deploy failed", msg);
    }
  };

  const commitYamlToRepo = async () => {
    const repo = yamlRepoTarget.trim();
    if (!isValidRepo(repo)) return toastErr("Repository", "Choose or enter a valid owner/repo.");
    if (!githubToken) return toastErr("GitHub token required", "Connect GitHub first so the app can commit .pragent.yaml.");
    try {
      const res = await githubCommitRepoFile(
        githubToken,
        repo,
        ".pragent.yaml",
        yamlPreview,
        "chore: configure PR Agent (.pragent.yaml)",
      );
      toastOk(
        res.changed ? "Committed .pragent.yaml" : ".pragent.yaml already up to date",
        `${repo} (${res.sha.slice(0, 8)})`,
      );
      setDeployLog((prev) => `${prev ? prev + "\n" : ""}✓ ${repo}: .pragent.yaml ${res.changed ? "committed" : "unchanged"}`);
      setYamlDialogOpen(false);
    } catch (e) {
      toastErr("Commit failed", errText(e, 260));
    }
  };

  const bootstrapSelectedMemory = async () => {
    const reposToBootstrap = effectiveTargetRepos.length
      ? effectiveTargetRepos
      : verifyRepo && isValidRepo(verifyRepo)
        ? [verifyRepo]
        : knownRepos.slice(0, 1);
    if (!reposToBootstrap.length) {
      toastErr("Bootstrap memory", "Select at least one repository first (or load your GitHub repos).");
      return;
    }
    const lines: string[] = [];
    let totalCreated = 0;
    for (const r of reposToBootstrap) {
      try {
        const res = await bootstrapRepoMemory(r);
        totalCreated += res.created.length;
        const langs = res.profile?.languages?.join(", ") || "detected stack";
        lines.push(`✓ ${r}: +${res.created.length} fact(s), ${res.skipped} skipped (${langs})`);
      } catch (e) {
        lines.push(`✗ ${r}: ${errText(e, 200)}`);
      }
    }
    setDeployLog((prev) => [prev, ...lines].filter(Boolean).join("\n"));
    toastOk("Repo memory bootstrapped", `Created ${totalCreated} repo fact(s) across ${reposToBootstrap.length} repo(s).`);
  };

  /* -------------------- Step 5 handlers -------------------- */
  const loadOpenPrsForVerify = async () => {
    const repo = verifyRepo.trim();
    if (!isValidRepo(repo)) return toastErr("Repository", "Enter a valid owner/repo first.");
    setVerifyErr("");
    try {
      const list = await listPulls(repo);
      setPulls(list);
      if (list.length > 0) {
        setSelectedPr(String(list[0].number));
        setManualPrNum(String(list[0].number));
        toastOk("Loaded open PRs", `Found ${list.length} open pull request(s) in ${repo}.`);
      } else {
        setSelectedPr("");
        toastOk("No open PRs", `${repo} has no open pull requests right now.`);
      }
    } catch (e) {
      const msg = errText(e, 220);
      setVerifyErr(msg);
      toastErr("Could not load PRs", msg);
    }
  };

  const runLiveReview = async () => {
    const repo = verifyRepo.trim();
    const prNumber = Number(manualPrNum.trim() || selectedPr);
    if (!isValidRepo(repo)) return toastErr("Verify", "Enter a valid repository (owner/name).");
    if (!Number.isInteger(prNumber) || prNumber <= 0) {
      return toastErr("Verify", "Select an open PR or enter a valid PR number.");
    }
    setVerifyErr("");
    setReviewRes(null);
    setVerifiedReadiness(null);
    try {
      const res = await triggerManualReview(repo, prNumber, { force_full: forceFull });
      setReviewRes(res);
      if (res.readiness) {
        setVerifiedReadiness(res.readiness);
      } else {
        const rd = await getPullReadiness(repo, prNumber).catch(() => null);
        if (rd?.report) setVerifiedReadiness(rd.report);
      }
      toastOk("Cloud PR review completed", `${repo}#${prNumber} · status=${res.status}`);
    } catch (e) {
      const msg = errText(e, 280);
      setVerifyErr(msg);
      toastErr("Review failed", msg);
    }
  };

  const activePrObj = pulls.find((p) => String(p.number) === (manualPrNum.trim() || selectedPr));
  const prUrl =
    activePrObj?.html_url ||
    (isValidRepo(verifyRepo) && Number(manualPrNum || selectedPr) > 0
      ? `https://github.com/${verifyRepo.trim()}/pull/${Number(manualPrNum || selectedPr)}`
      : "");

  const filteredKnownRepos = React.useMemo(() => {
    const q = repoSearch.trim().toLowerCase();
    return knownRepos.filter((r) => !q || r.toLowerCase().includes(q));
  }, [knownRepos, repoSearch]);

  const tokenDisplay = apiToken || "<your-generated-token>";

  return (
    <Page active={active} className="max-w-5xl">
      {/* Progress & Stepper Header */}
      <Section
        title={
          <span className="flex items-center gap-2">
            <Cloud className="text-primary size-4" />
            Cloud PR Agent Setup
          </span>
        }
        description="Configure your personal always-on PR reviewer in 5 guided steps — structured inline reviews, repo memory, @pr-agent chat, and deterministic merge readiness."
        action={
          <div className="flex items-center gap-2">
            <Badge variant={completedCount === STEPS.length ? "success" : "outline"}>
              {completedCount}/{STEPS.length} ready
            </Badge>
            <AsyncButton variant="outline" size="sm" onClick={refreshDiagnostics}>
              <RefreshCw /> Re-check
            </AsyncButton>
          </div>
        }
      >
        <div className="grid gap-3">
          <Progress value={progressPct} aria-label="Cloud PR Agent setup progress" />
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
            {STEPS.map((s) => {
              const Icon = s.icon;
              const isCurrent = step === s.id;
              const isDone = stepDoneMap[s.id];
              return (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => setStep(s.id)}
                  className={cn(
                    "flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-left transition-colors",
                    isCurrent
                      ? "border-primary bg-primary/10 text-foreground"
                      : "hover:bg-muted/50 border-border/80 bg-card/40 text-muted-foreground",
                  )}
                >
                  <span
                    className={cn(
                      "flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold",
                      isDone
                        ? "bg-success/15 text-success"
                        : isCurrent
                          ? "bg-primary text-primary-foreground"
                          : "bg-muted text-muted-foreground",
                    )}
                  >
                    {isDone ? <Check className="size-3.5" /> : s.id}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1 truncate text-xs font-medium">
                      <Icon className="size-3 shrink-0 opacity-75" />
                      <span className="truncate">{s.short}</span>
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      </Section>

      {/* ================================================================ */}
      {/* STEP 1 — Cloud Backend                                           */}
      {/* ================================================================ */}
      {step === 1 ? (
        <Section
          title="Step 1 — Cloud Backend"
          description="Connect the desktop control plane to your FastAPI cloud backend (with Redis persistence and encrypted secret storage)."
          action={
            <Badge variant={backend.ok === true ? "success" : backend.ok === false ? "danger" : "outline"}>
              {backend.label}
            </Badge>
          }
          footer={
            <div className="flex w-full flex-wrap items-center justify-between gap-2">
              <ButtonRow>
                <AsyncButton onClick={testBackendStep}>
                  <Wifi /> Test connection &amp; diagnostics
                </AsyncButton>
              </ButtonRow>
              <Button type="button" onClick={() => setStep(2)}>
                Next: AI Model <ArrowRight />
              </Button>
            </div>
          }
        >
          {/* Live health pills */}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <div className="bg-muted/30 flex items-center gap-2 rounded-md border px-3 py-2 text-xs">
              <StatusDot state={backendReachable ? "ok" : diag === null ? "err" : "idle"} />
              <span className="font-medium">Backend</span>
              <span className="text-muted-foreground ml-auto">{backendReachable ? "online" : diag === null ? "offline" : "…"}</span>
            </div>
            <div className="bg-muted/30 flex items-center gap-2 rounded-md border px-3 py-2 text-xs">
              <StatusDot state={authProtected ? "ok" : backendReachable ? "warn" : "idle"} />
              <span className="font-medium">API Auth</span>
              <span className="text-muted-foreground ml-auto">{authProtected ? "protected" : "open"}</span>
            </div>
            <div className="bg-muted/30 flex items-center gap-2 rounded-md border px-3 py-2 text-xs">
              <StatusDot state={encryptionReady ? "ok" : backendReachable ? "warn" : "idle"} />
              <span className="font-medium">Encryption</span>
              <span className="text-muted-foreground ml-auto">{encryptionReady ? "active" : "off"}</span>
            </div>
            <div className="bg-muted/30 flex items-center gap-2 rounded-md border px-3 py-2 text-xs">
              <StatusDot state={redisReady ? "ok" : backendReachable ? "warn" : "idle"} />
              <span className="font-medium">Redis</span>
              <span className="text-muted-foreground ml-auto">{redisReady ? "persistent" : "memory"}</span>
            </div>
          </div>

          {diagErr ? (
            <Alert variant="destructive">
              <AlertTitle>Backend unreachable</AlertTitle>
              <AlertDescription>{diagErr}</AlertDescription>
            </Alert>
          ) : null}

          <FieldGrid>
            <Field
              label="Backend HTTPS URL"
              htmlFor="setup-backend-url"
              hint="Your deployed FastAPI Cloud URL (or http://localhost:8000 for local dev)."
            >
              <Input
                id="setup-backend-url"
                value={urlDraft}
                placeholder="https://pr-agent-backend.fastapicloud.dev"
                onChange={(e) => setUrlDraft(e.target.value)}
                onBlur={commitBackendUrl}
                onKeyDown={(e) => e.key === "Enter" && commitBackendUrl()}
                autoComplete="off"
              />
            </Field>
            <Field
              label="BACKEND_API_TOKEN"
              htmlFor="setup-api-token"
              hint="Protects your cloud API and derives the encryption key for stored provider secrets."
            >
              <div className="flex gap-2">
                <div className="min-w-0 flex-1">
                  <SecretInput
                    id="setup-api-token"
                    value={apiToken}
                    placeholder="Paste or generate a 64-char hex token"
                    onChange={(e) => setSecret("api", e.target.value)}
                  />
                </div>
                <Button type="button" variant="outline" size="sm" onClick={generateApiToken} title="Generate secure random token">
                  <Shuffle /> Generate
                </Button>
              </div>
            </Field>
          </FieldGrid>

          <div className="bg-muted/25 flex flex-wrap items-center justify-between gap-3 rounded-lg border px-3.5 py-2.5">
            <div className="flex items-center gap-2.5">
              <ShieldCheck className="text-primary size-4 shrink-0" />
              <div className="grid gap-0.5">
                <Label htmlFor="setup-remember-pc" className="cursor-pointer text-xs font-medium">
                  Save securely on this PC (Windows DPAPI)
                </Label>
                <span className="text-muted-foreground text-[11px]">
                  {isTauri()
                    ? vaultKeys.includes("backend_api_token")
                      ? "Encrypted with your Windows user profile — auto-loaded on launch."
                      : "Keep BACKEND_API_TOKEN encrypted on disk so you don't re-paste it on every launch."
                    : "Available in the Tauri desktop app (web preview keeps tokens in session only)."}
                </span>
              </div>
            </div>
            <Switch
              id="setup-remember-pc"
              checked={rememberSecrets}
              onCheckedChange={(v) => void setRememberOnPc(v)}
            />
          </div>

          {step1Log ? <Console>{step1Log}</Console> : null}

          <Collapsible open={deployGuideOpen} onOpenChange={setDeployGuideOpen}>
            <CollapsibleTrigger asChild>
              <Button type="button" variant="outline" size="sm" className="w-full justify-between">
                <span className="flex items-center gap-2">
                  <BookOpen className="size-3.5" />
                  Need to host the backend? Open copy-paste deploy guide (FastAPI Cloud + Redis Cloud + Cloudflare Worker)
                </span>
                <ChevronDown className={cn("size-4 transition-transform", deployGuideOpen && "rotate-180")} />
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent className="mt-3 grid gap-3">
              <CopyBlock
                title="1. Deploy FastAPI Cloud backend (free tier)"
                code={`cd backend
fastapi login
fastapi deploy
# Then set environment variables in FastAPI Cloud dashboard:
BACKEND_API_TOKEN=${tokenDisplay}
ALLOWED_ORIGINS=tauri://localhost,http://tauri.localhost,http://localhost:1420`}
              />
              <CopyBlock
                title="2. Connect Redis Cloud (persistent agents, findings & repo memory)"
                code={`# In FastAPI Cloud dashboard -> Integrations -> Add Redis Cloud (injects REDIS_URL automatically)
# Or set manually from https://redis.io/try-free (30 MB free tier fits >100 repos):
REDIS_URL=redis://default:<password>@<host>:<port>`}
              />
              <CopyBlock
                title="3. Optional: Cloudflare Worker edge proxy (HMAC verification + buffering)"
                code={`cd cloudflare-worker
npx wrangler deploy
npx wrangler secret put BACKEND_URL
# Paste your FastAPI Cloud URL above
npx wrangler secret put GITHUB_APP_WEBHOOK_SECRET
# Paste your GitHub App webhook secret (optional if using FastAPI Cloud directly)`}
              />
            </CollapsibleContent>
          </Collapsible>
        </Section>
      ) : null}

      {/* ================================================================ */}
      {/* STEP 2 — AI Model                                                */}
      {/* ================================================================ */}
      {step === 2 ? (
        <Section
          title="Step 2 — AI Model"
          description="Connect an AI provider and choose your review model. API keys are saved encrypted directly on your cloud backend."
          action={
            <Badge variant={step2Done ? "success" : "warning"}>
              {step2Done ? `${activeBackendLlm} · ${diag?.model || "ready"}` : "API key needed"}
            </Badge>
          }
          footer={
            <div className="flex w-full flex-wrap items-center justify-between gap-2">
              <Button type="button" variant="outline" onClick={() => setStep(1)}>
                <ArrowLeft /> Back
              </Button>
              <ButtonRow>
                <AsyncButton variant="outline" onClick={testLlmLive}>
                  <Play /> Test live model
                </AsyncButton>
                <AsyncButton onClick={saveLlmToCloud}>
                  <KeyRound /> Save encrypted to cloud
                </AsyncButton>
                <Button type="button" onClick={() => setStep(3)}>
                  Next: GitHub App <ArrowRight />
                </Button>
              </ButtonRow>
            </div>
          }
        >
          {/* Provider cards */}
          <div className="grid gap-3 md:grid-cols-3">
            {(Object.keys(LLM_PROVIDERS) as LlmProviderId[]).map((pid) => {
              const p = LLM_PROVIDERS[pid];
              const configured = Boolean(diag?.[LLM_STATUS_KEY[pid]]?.configured);
              const selected = llmProvider === pid;
              return (
                <button
                  key={pid}
                  type="button"
                  onClick={() => {
                    setLlmProvider(pid);
                    setLlmModel(defaultModelFor(pid));
                    setLlmCustomModel("");
                    llmDirtyRef.current = true;
                  }}
                  className={cn(
                    "grid cursor-pointer gap-2 rounded-lg border p-3.5 text-left transition-colors",
                    selected ? "border-primary bg-primary/8" : "hover:bg-muted/40 border-border",
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-semibold">{p.label}</span>
                    <Badge variant={configured ? "success" : "outline"} className="text-[10px]">
                      {configured ? "configured" : pid === "openrouter" ? "free models" : "optional"}
                    </Badge>
                  </div>
                  <p className="text-muted-foreground text-xs leading-relaxed">
                    {pid === "openrouter"
                      ? "1-click PKCE OAuth with free reasoning models ready out of the box."
                      : pid === "commandcode"
                        ? "Fast coding models (DeepSeek / Qwen). Supports CLI key import."
                        : "OpenCode Go gateway (Kimi K3). Supports CLI key import."}
                  </p>
                </button>
              );
            })}
          </div>

          {/* 1-click connect row for selected provider */}
          <div className="bg-muted/25 grid gap-3 rounded-lg border p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-sm font-medium">Connect {LLM_PROVIDERS[llmProvider].label}</div>
                <div className="text-muted-foreground text-xs">
                  {llmProvider === "openrouter"
                    ? "Sign in via OpenRouter OAuth (saves straight to your cloud backend) or paste an API key below."
                    : "Import your existing CLI credentials in 1 click or paste an API key below."}
                </div>
              </div>
              <ButtonRow>
                {llmProvider === "openrouter" ? (
                  <AsyncButton
                    size="sm"
                    onClick={async () => {
                      const ok = await connectOpenRouterOAuth({ persist: true });
                      if (ok) await refreshDiagnostics();
                    }}
                    disabled={activeFlow !== null}
                  >
                    <Plug /> Connect with OpenRouter
                  </AsyncButton>
                ) : null}
                {llmProvider === "commandcode" && oauthStatus.cli.commandcode_cli ? (
                  <AsyncButton
                    size="sm"
                    onClick={async () => {
                      const ok = await importCommandCodeCli();
                      if (ok) await refreshDiagnostics();
                    }}
                    disabled={activeFlow !== null}
                  >
                    <Terminal /> Import Command Code CLI key
                  </AsyncButton>
                ) : null}
                {llmProvider === "opencode_go" && oauthStatus.cli.opencode_cli ? (
                  <AsyncButton
                    size="sm"
                    onClick={async () => {
                      const ok = await importOpenCodeCli();
                      if (ok) await refreshDiagnostics();
                    }}
                    disabled={activeFlow !== null}
                  >
                    <Terminal /> Import OpenCode Go CLI key
                  </AsyncButton>
                ) : null}
                {!diag?.[LLM_STATUS_KEY[llmProvider]]?.configured &&
                allSecrets[LLM_PROVIDERS[llmProvider].secret] ? (
                  <AsyncButton
                    size="sm"
                    variant="outline"
                    onClick={async () => {
                      const meta = LLM_PROVIDERS[llmProvider];
                      const val = allSecrets[meta.secret];
                      if (!val) return;
                      try {
                        await saveCloudSettings({ [meta.envKey]: val });
                        toastOk("Synced to cloud backend", meta.envKey);
                        await refreshDiagnostics();
                      } catch (e) {
                        toastErr("Sync failed", errText(e, 240));
                      }
                    }}
                  >
                    <KeyRound /> Sync session key to cloud
                  </AsyncButton>
                ) : null}
                {activeFlow === "openrouter" ? (
                  <Button type="button" variant="ghost" size="sm" onClick={cancelOAuth}>
                    <X /> Cancel
                  </Button>
                ) : null}
              </ButtonRow>
            </div>

            {activeFlow === "openrouter" ? (
              <div className="bg-background/60 flex items-center gap-2 rounded-md border px-3 py-2 text-xs">
                <Loader2 className="text-primary size-4 shrink-0 animate-spin" />
                <span>{flowMessage}</span>
              </div>
            ) : null}

            <FieldGrid>
              <Field label="Model" htmlFor="setup-llm-model">
                <Select
                  value={llmModel}
                  onValueChange={(v) => {
                    setLlmModel(v);
                    llmDirtyRef.current = true;
                  }}
                >
                  <SelectTrigger id="setup-llm-model" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {modelsForProvider(llmProvider).map((m) => (
                      <SelectItem key={m.value} value={m.value}>
                        {m.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
              <Field
                label={`${LLM_PROVIDERS[llmProvider].envKey} (optional manual override)`}
                htmlFor="setup-llm-manual-key"
                hint={
                  diag?.[LLM_STATUS_KEY[llmProvider]]?.configured
                    ? "Key already stored on cloud backend — leave blank to keep it."
                    : "Encrypted with BACKEND_API_TOKEN before reaching Redis."
                }
              >
                <SecretInput
                  id="setup-llm-manual-key"
                  value={manualKey}
                  placeholder={LLM_PROVIDERS[llmProvider].keyPlaceholder}
                  onChange={(e) => setManualKey(e.target.value)}
                />
              </Field>
            </FieldGrid>

            {llmModel === "custom" ? (
              <Field label="Custom model ID" htmlFor="setup-llm-custom-model">
                <Input
                  id="setup-llm-custom-model"
                  value={llmCustomModel}
                  placeholder={defaultModelFor(llmProvider)}
                  onChange={(e) => {
                    setLlmCustomModel(e.target.value);
                    llmDirtyRef.current = true;
                  }}
                />
              </Field>
            ) : null}

            {llmTestResult ? (
              <Alert variant={llmTestResult.ok ? "default" : "destructive"}>
                <AlertTitle>{llmTestResult.ok ? "Model test passed" : "Model test failed"}</AlertTitle>
                <AlertDescription>{llmTestResult.text}</AlertDescription>
              </Alert>
            ) : null}
          </div>
        </Section>
      ) : null}

      {/* ================================================================ */}
      {/* STEP 3 — Personal GitHub App                                     */}
      {/* ================================================================ */}
      {step === 3 ? (
        <Section
          title="Step 3 — Personal GitHub App"
          description="Create your own GitHub App in 1 click so PR Agent gets a dedicated bot identity, real Check Runs for branch protection, and live PR/comment/check webhooks."
          action={
            <Badge variant={step3Done ? "success" : health?.configured ? "warning" : "outline"}>
              {step3Done
                ? `App ${health?.slug || "healthy"}`
                : health?.configured
                  ? `${health.problems.length || 1} issue(s)`
                  : "not created"}
            </Badge>
          }
          footer={
            <div className="flex w-full flex-wrap items-center justify-between gap-2">
              <Button type="button" variant="outline" onClick={() => setStep(2)}>
                <ArrowLeft /> Back
              </Button>
              <ButtonRow>
                <AsyncButton variant="outline" onClick={refreshDiagnostics}>
                  <RefreshCw /> Check App health
                </AsyncButton>
                <Button type="button" onClick={() => setStep(4)}>
                  Next: Configure &amp; Deploy <ArrowRight />
                </Button>
              </ButtonRow>
            </div>
          }
        >
          {/* Public webhook base */}
          <Field
            label="Public HTTPS webhook base URL (optional Cloudflare Worker override)"
            htmlFor="setup-webhook-base"
            hint={
              expectedWebhookUrl
                ? `GitHub webhooks will deliver to ${expectedWebhookUrl}`
                : "Set a public HTTPS Backend URL (Step 1) or paste a Cloudflare Worker URL here."
            }
          >
            <div className="flex flex-wrap gap-2">
              <Input
                id="setup-webhook-base"
                value={webhookOverrideDraft}
                placeholder={currentBackendUrl.startsWith("https://") ? currentBackendUrl : "https://your-worker.workers.dev"}
                onChange={(e) => setWebhookOverrideDraft(e.target.value)}
                onBlur={() => setCloudBase(webhookOverrideDraft.trim().replace(/\/+$/, ""))}
                className="min-w-56 flex-1"
              />
              {webhookMismatch ? (
                <AsyncButton variant="outline" onClick={fixWebhookUrl}>
                  <Wrench /> Fix webhook URL
                </AsyncButton>
              ) : null}
            </div>
          </Field>

          {/* Primary GitHub App actions */}
          <div className="bg-muted/25 grid gap-3 rounded-lg border p-4">
            <ButtonRow>
              <AsyncButton
                onClick={async () => {
                  const ok = await connectGitHubOAuth({
                    forceNewApp: !health?.configured,
                    appName,
                    org: appOrg,
                  });
                  if (ok) await refreshDiagnostics();
                }}
                disabled={activeFlow !== null}
              >
                <GitBranch /> {health?.configured ? "Reconnect GitHub session" : "Create My PR Agent App"}
              </AsyncButton>
              {appInstallUrl ? (
                <Button type="button" variant="outline" onClick={() => openUrl(appInstallUrl)}>
                  <ExternalLink /> Install on GitHub repos
                </Button>
              ) : null}
              {oauthStatus.cli.gh_available ? (
                <AsyncButton
                  variant="outline"
                  onClick={() => importGitHubCli(false)}
                  disabled={activeFlow !== null}
                >
                  <Terminal /> {oauthStatus.cli.gh_login ? `Use gh CLI (@${oauthStatus.cli.gh_login})` : "Use gh CLI"}
                </AsyncButton>
              ) : null}
              {activeFlow === "github" ? (
                <Button type="button" variant="ghost" size="sm" onClick={cancelOAuth}>
                  <X /> Cancel
                </Button>
              ) : null}
            </ButtonRow>

            {activeFlow === "github" ? (
              <div className="bg-background/60 flex items-center gap-2 rounded-md border px-3 py-2 text-xs">
                <Loader2 className="text-primary size-4 shrink-0 animate-spin" />
                <span>{flowMessage}</span>
              </div>
            ) : null}

            {/* Live GET /github-app/health diagnostics */}
            {health?.configured ? (
              <div className="bg-background/60 grid gap-2 rounded-md border p-3 text-xs">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="flex items-center gap-2 font-semibold">
                    <StatusDot state={health.ok && !webhookMismatch ? "ok" : "warn"} />
                    {health.slug ? `GitHub App: ${health.slug}` : "GitHub App configured"}
                    {ghLogin ? <Badge variant="outline">@{ghLogin}</Badge> : null}
                  </span>
                  {health.html_url ? (
                    <Button type="button" variant="link" size="xs" className="h-auto p-0" onClick={() => openUrl(health.html_url!)}>
                      App settings <ExternalLink className="size-3" />
                    </Button>
                  ) : null}
                </div>
                <div className="text-muted-foreground">
                  Webhook: <code>{health.webhook_url || "(not set)"}</code>
                </div>
                {health.installations?.length ? (
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-muted-foreground">Installations:</span>
                    {health.installations.map((inst) => (
                      <Badge key={inst.id} variant={inst.suspended ? "warning" : "success"}>
                        {inst.account} ({inst.repository_selection})
                      </Badge>
                    ))}
                  </div>
                ) : (
                  <p className="text-warning">
                    Not installed on any account yet — click <b>Install on GitHub repos</b> above.
                  </p>
                )}
                {health.problems.map((p) => (
                  <p key={p} className="text-warning break-words">
                    • {p}
                  </p>
                ))}
                {webhookMismatch ? (
                  <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                    <span className="text-warning">
                      Webhook URL does not match expected <code>{expectedWebhookUrl}</code>.
                    </span>
                    <AsyncButton size="xs" variant="outline" onClick={fixWebhookUrl}>
                      <Wrench /> Fix webhook URL
                    </AsyncButton>
                  </div>
                ) : null}
              </div>
            ) : null}

            <details className="group">
              <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
                {health?.configured
                  ? "Replace GitHub App or customize name / organization"
                  : "Customize App name or GitHub organization (optional)"}
              </summary>
              <div className="mt-3 grid gap-3">
                <FieldGrid>
                  <Field label="GitHub App name" htmlFor="setup-app-name" hint="Max 34 chars, unique across GitHub.">
                    <Input
                      id="setup-app-name"
                      value={appName}
                      maxLength={34}
                      onChange={(e) => setAppName(e.target.value)}
                    />
                  </Field>
                  <Field label="Organization (leave empty for personal)" htmlFor="setup-app-org">
                    <Input
                      id="setup-app-org"
                      value={appOrg}
                      placeholder="my-org"
                      onChange={(e) => setAppOrg(e.target.value)}
                    />
                  </Field>
                </FieldGrid>
                {health?.configured ? (
                  <ButtonRow>
                    <AsyncButton
                      variant="outline"
                      size="sm"
                      disabled={activeFlow !== null || !appName.trim()}
                      onClick={async () => {
                        const ok = await connectGitHubOAuth({ forceNewApp: true, appName, org: appOrg });
                        if (ok) await refreshDiagnostics();
                      }}
                    >
                      <GitBranch /> Create replacement GitHub App
                    </AsyncButton>
                  </ButtonRow>
                ) : null}
              </div>
            </details>
          </div>

          {/* Zero-host Actions Lite alternative */}
          <div className="bg-muted/20 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-dashed px-4 py-3">
            <div className="grid gap-0.5">
              <div className="flex items-center gap-2 text-xs font-semibold">
                <Rocket className="text-primary size-3.5" />
                Prefer zero-host GitHub Actions Lite mode?
              </div>
              <p className="text-muted-foreground text-xs">
                Run reviews purely inside GitHub Actions (with `.pr-agent/memory.md` support) — no backend or GitHub App needed.
              </p>
            </div>
            <Button type="button" variant="outline" size="sm" onClick={() => setTab("autoreview")}>
              Open Auto review (Actions Lite)
            </Button>
          </div>
        </Section>
      ) : null}

      {/* ================================================================ */}
      {/* STEP 4 — Configure & Deploy to Repos                             */}
      {/* ================================================================ */}
      {step === 4 ? (
        <Section
          title="Step 4 — Configure & Deploy to Repos"
          description="Set review strictness, merge gate, repo memory, and @pr-agent PR chat — then deploy your Cloud Agent in 1 click."
          action={<Badge variant={step4Done ? "success" : "outline"}>{agents.length} backend agent(s)</Badge>}
          footer={
            <div className="flex w-full flex-wrap items-center justify-between gap-2">
              <Button type="button" variant="outline" onClick={() => setStep(3)}>
                <ArrowLeft /> Back
              </Button>
              <ButtonRow>
                <AsyncButton onClick={deployCloudAgent}>
                  <Bot /> Deploy Cloud Agent
                </AsyncButton>
                <Button type="button" variant="outline" onClick={() => setYamlDialogOpen(true)}>
                  <FileCode2 /> Commit .pragent.yaml
                </Button>
                <AsyncButton variant="outline" onClick={bootstrapSelectedMemory}>
                  <Brain /> Bootstrap Repo Memory
                </AsyncButton>
                <Button type="button" onClick={() => setStep(5)}>
                  Next: Verify on PR <ArrowRight />
                </Button>
              </ButtonRow>
            </div>
          }
        >
          {/* Review profile */}
          <Field label="Review profile">
            <div className="grid gap-3 sm:grid-cols-2">
              {(
                [
                  {
                    id: "chill" as ReviewProfile,
                    title: "Chill (Recommended)",
                    desc: "High-signal bugs & security issues (confidence ≥ 0.70, max 2 nits). Low noise for daily merges.",
                  },
                  {
                    id: "assertive" as ReviewProfile,
                    title: "Assertive",
                    desc: "Thorough coverage including maintainability, edge cases, and up to 6 nits (confidence ≥ 0.50).",
                  },
                ] as const
              ).map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => setProfile(opt.id)}
                  className={cn(
                    "grid cursor-pointer gap-1 rounded-lg border p-3.5 text-left transition-colors",
                    profile === opt.id ? "border-primary bg-primary/8" : "hover:bg-muted/40 border-border",
                  )}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-semibold">{opt.title}</span>
                    {profile === opt.id ? <CheckCircle2 className="text-primary size-4" /> : null}
                  </div>
                  <span className="text-muted-foreground text-xs leading-relaxed">{opt.desc}</span>
                </button>
              ))}
            </div>
          </Field>

          {/* Merge gate + score threshold */}
          <FieldGrid>
            <Field
              label="Merge readiness gate"
              htmlFor="setup-merge-gate"
              hint="Controls the PR Agent Check Run conclusion on GitHub."
            >
              <Select value={mergeGate} onValueChange={(v) => setMergeGate(v as MergeGateMode)}>
                <SelectTrigger id="setup-merge-gate" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="off">off — compute internally only</SelectItem>
                  <SelectItem value="advisory">advisory — neutral Check Run + sticky checklist</SelectItem>
                  <SelectItem value="required">required — fail Check Run below threshold</SelectItem>
                </SelectContent>
              </Select>
            </Field>

            <Field
              label={
                <span className="flex items-center justify-between">
                  <span>Ready score threshold</span>
                  <Badge variant="outline" className="font-mono tabular-nums">
                    {scoreThreshold} / 100
                  </Badge>
                </span>
              }
              hint="PRs at or above this score (with zero hard blockers) pass as Ready."
            >
              <div className="pt-2">
                <Slider
                  min={50}
                  max={100}
                  step={5}
                  value={[scoreThreshold]}
                  onValueChange={([v]) => setScoreThreshold(v)}
                />
              </div>
            </Field>
          </FieldGrid>

          {/* Toggles */}
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="bg-muted/25 flex items-center justify-between gap-3 rounded-lg border px-3.5 py-3">
              <div className="grid gap-0.5">
                <Label htmlFor="setup-memory-toggle" className="cursor-pointer text-sm font-medium">
                  Repo Memory
                </Label>
                <span className="text-muted-foreground text-xs">
                  Remember codebase conventions, docs, and dismissed false positives.
                </span>
              </div>
              <Switch id="setup-memory-toggle" checked={memoryEnabled} onCheckedChange={setMemoryEnabled} />
            </div>

            <div className="bg-muted/25 flex items-center justify-between gap-3 rounded-lg border px-3.5 py-3">
              <div className="grid gap-0.5">
                <Label htmlFor="setup-chat-toggle" className="cursor-pointer text-sm font-medium">
                  @pr-agent PR Chat
                </Label>
                <span className="text-muted-foreground text-xs">
                  Reply to @pr-agent questions, remember rules, and dismiss findings in PR threads.
                </span>
              </div>
              <Switch id="setup-chat-toggle" checked={chatEnabled} onCheckedChange={setChatEnabled} />
            </div>
          </div>

          <Separator />

          {/* Agent name & target repos */}
          <FieldGrid>
            <Field label="Agent name" htmlFor="setup-agent-name">
              <Input
                id="setup-agent-name"
                value={agentName}
                onChange={(e) => setAgentName(e.target.value)}
                placeholder="pr-agent-control"
              />
            </Field>
            <Field label="Repository scope" hint="Target all repos where your GitHub App is installed, or pick specific ones.">
              <div className="flex h-9 items-center gap-3">
                <Switch
                  id="setup-all-repos"
                  checked={targetAllRepos}
                  onCheckedChange={setTargetAllRepos}
                />
                <Label htmlFor="setup-all-repos" className="cursor-pointer text-xs font-normal">
                  {targetAllRepos ? "All installed repositories (recommended)" : `Specific repositories (${selectedRepos.length} selected)`}
                </Label>
              </div>
            </Field>
          </FieldGrid>

          {!targetAllRepos ? (
            <div className="grid gap-2.5">
              <div className="flex flex-wrap gap-2">
                <Input
                  value={repoSearch}
                  onChange={(e) => setRepoSearch(e.target.value)}
                  placeholder="Filter loaded repositories…"
                  className="min-w-44 flex-1"
                />
                <AsyncButton variant="outline" size="sm" onClick={loadRepos} disabled={loadingRepos || !githubToken}>
                  <RefreshCw className={loadingRepos ? "animate-spin" : ""} /> Load GitHub repos
                </AsyncButton>
              </div>
              <div className="rounded-md border">
                <ScrollArea className="max-h-44">
                  <div className="grid gap-0.5 p-2">
                    {filteredKnownRepos.length ? (
                      filteredKnownRepos.map((repo) => (
                        <Label
                          key={repo}
                          className="hover:bg-muted/60 flex cursor-pointer items-center gap-2.5 rounded px-2 py-1.5 font-mono text-xs font-normal"
                        >
                          <Checkbox
                            checked={selectedRepos.includes(repo)}
                            onCheckedChange={(v) => toggleSelectedRepo(repo, v === true)}
                          />
                          {repo}
                        </Label>
                      ))
                    ) : (
                      <Hint className="px-2 py-3 text-xs">
                        No repos loaded yet. Click Load GitHub repos or add <code>owner/repo</code> below.
                      </Hint>
                    )}
                  </div>
                </ScrollArea>
              </div>
              <div className="flex gap-2">
                <Input
                  value={manualRepoInput}
                  onChange={(e) => setManualRepoInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && addManualRepo()}
                  placeholder="owner/repo"
                  className="flex-1"
                />
                <Button type="button" variant="outline" size="sm" onClick={addManualRepo}>
                  <Plus /> Add
                </Button>
              </div>
            </div>
          ) : null}

          {deployLog ? <Console>{deployLog}</Console> : null}
        </Section>
      ) : null}

      {/* ================================================================ */}
      {/* STEP 5 — Verify Live on a PR                                     */}
      {/* ================================================================ */}
      {step === 5 ? (
        <Section
          title="Step 5 — Verify Live on a PR"
          description="Trigger a live cloud PR review right now to verify inline comments, walkthrough generation, and Merge Readiness scoring end-to-end."
          action={
            step5Done ? <Badge variant="success">Verified</Badge> : <Badge variant="outline">Ready to test</Badge>
          }
          footer={
            <div className="flex w-full flex-wrap items-center justify-between gap-2">
              <Button type="button" variant="outline" onClick={() => setStep(4)}>
                <ArrowLeft /> Back
              </Button>
              <ButtonRow>
                <AsyncButton onClick={runLiveReview}>
                  <Play /> Run Cloud PR Review
                </AsyncButton>
                {prUrl ? (
                  <Button type="button" variant="outline" onClick={() => openUrl(prUrl)}>
                    <ExternalLink /> Open PR on GitHub
                  </Button>
                ) : null}
                <Button type="button" variant="secondary" onClick={() => setTab("inbox")}>
                  <GitPullRequest /> Open PR Inbox
                </Button>
              </ButtonRow>
            </div>
          }
        >
          <FieldGrid>
            <Field label="Repository (owner/name)" htmlFor="setup-verify-repo">
              <div className="flex gap-2">
                <Input
                  id="setup-verify-repo"
                  value={verifyRepo}
                  placeholder="owner/repo"
                  onChange={(e) => {
                    setVerifyRepo(e.target.value);
                    setPulls([]);
                    setSelectedPr("");
                  }}
                  list="setup-known-repos"
                  className="flex-1 font-mono text-xs"
                />
                <AsyncButton variant="outline" size="sm" onClick={loadOpenPrsForVerify} disabled={!githubToken}>
                  Load open PRs
                </AsyncButton>
              </div>
            </Field>

            <Field
              label="Pull request"
              htmlFor="setup-verify-pr"
              hint={pulls.length ? `${pulls.length} open PR(s) loaded` : "Load open PRs or type a PR number directly."}
            >
              {pulls.length > 0 ? (
                <Select
                  value={selectedPr}
                  onValueChange={(v) => {
                    setSelectedPr(v);
                    setManualPrNum(v);
                  }}
                >
                  <SelectTrigger id="setup-verify-pr" className="w-full">
                    <SelectValue placeholder="Choose an open PR" />
                  </SelectTrigger>
                  <SelectContent>
                    {pulls.map((p) => (
                      <SelectItem key={p.number} value={String(p.number)}>
                        #{p.number} — {p.title}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <Input
                  id="setup-verify-pr"
                  type="number"
                  min={1}
                  value={manualPrNum}
                  placeholder="e.g. 42"
                  onChange={(e) => setManualPrNum(e.target.value)}
                />
              )}
            </Field>
          </FieldGrid>

          <Label className="flex cursor-pointer items-center gap-2 text-xs font-normal">
            <Checkbox checked={forceFull} onCheckedChange={(v) => setForceFull(v === true)} />
            Force full PR review (ignore previous incremental commit cursor)
          </Label>

          {verifyErr ? (
            <Alert variant="destructive">
              <AlertTitle>Verification error</AlertTitle>
              <AlertDescription>{verifyErr}</AlertDescription>
            </Alert>
          ) : null}

          {reviewRes ? (
            <div className="bg-muted/25 grid gap-4 rounded-lg border p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="text-success size-4" />
                  <span className="text-sm font-semibold">Live Review Result</span>
                  <Badge variant="success">{reviewRes.status}</Badge>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {reviewRes.review_posted ? <Badge variant="outline">Inline review posted</Badge> : null}
                  {reviewRes.pr_comment_posted ? <Badge variant="outline">Summary comment posted</Badge> : null}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                <KV label="Findings" value={reviewRes.findings_count ?? 0} />
                <KV
                  label="Merge Readiness"
                  value={
                    verifiedReadiness ? (
                      <span className="flex items-center gap-1.5">
                        <span>{verifiedReadiness.score}/100</span>
                        <Badge variant={bandBadgeVariant(verifiedReadiness.band)} className="text-[10px]">
                          {bandLabel(verifiedReadiness.band)}
                        </Badge>
                      </span>
                    ) : (
                      "—"
                    )
                  }
                />
                <KV
                  label="Review Effort"
                  value={reviewRes.walkthrough?.effort ? `${reviewRes.walkthrough.effort} / 5` : reviewRes.run_id?.slice(0, 10) || "—"}
                />
              </div>

              {reviewRes.walkthrough?.summary || reviewRes.summary ? (
                <div className="grid gap-1.5">
                  <div className="text-xs font-semibold">Walkthrough Summary</div>
                  <p className="text-muted-foreground text-xs leading-relaxed whitespace-pre-wrap">
                    {reviewRes.walkthrough?.summary || reviewRes.summary}
                  </p>
                </div>
              ) : null}

              {reviewRes.walkthrough?.file_table?.length ? (
                <div className="grid gap-1.5">
                  <div className="text-xs font-semibold">File Summary</div>
                  <div className="grid gap-1">
                    {reviewRes.walkthrough.file_table.slice(0, 8).map((row) => (
                      <div key={row.file} className="bg-background/60 flex flex-wrap gap-2 rounded border px-2.5 py-1.5 text-xs">
                        <code className="shrink-0">{row.file}</code>
                        <span className="text-muted-foreground min-w-0 flex-1">{row.change_summary}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
        </Section>
      ) : null}

      {/* Shared datalist available across all steps (Step 4 dialog & Step 5 verify) */}
      <datalist id="setup-known-repos">
        {knownRepos.map((r) => (
          <option key={r} value={r} />
        ))}
      </datalist>

      {/* Commit .pragent.yaml Dialog */}
      <Dialog open={yamlDialogOpen} onOpenChange={setYamlDialogOpen}>
        <DialogContent className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Commit .pragent.yaml to repository</DialogTitle>
            <DialogDescription>
              Version-control your review rules in the base branch so every PR inherits this configuration automatically.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <Field label="Target repository" htmlFor="setup-yaml-repo">
              <Input
                id="setup-yaml-repo"
                value={yamlRepoTarget}
                onChange={(e) => setYamlRepoTarget(e.target.value)}
                placeholder="owner/repo"
                list="setup-known-repos"
                className="font-mono text-xs"
              />
            </Field>
            <Console tall>{yamlPreview}</Console>
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={async () => {
                if (await copyToClipboard(yamlPreview)) toastOk("Copied", ".pragent.yaml");
              }}
            >
              <ClipboardCopy /> Copy YAML
            </Button>
            <AsyncButton onClick={commitYamlToRepo}>
              <FileCode2 /> Commit .pragent.yaml
            </AsyncButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Page>
  );
}
