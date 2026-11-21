import * as React from "react";
import {
  ClipboardCopy,
  Download,
  FileCode2,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { AsyncButton, ButtonRow, Console, Field, FieldGrid, Hint, Page, Section } from "@/components/common";
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
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { apiGet, apiMut, backendUrl } from "@/lib/api";
import { refreshAgents } from "@/lib/actions";
import {
  copyToClipboard,
  getRepoConfig,
  isValidRepo,
  mergePragentYaml,
  validatePathInstructions,
  type PragentYamlInput,
} from "@/lib/cloud-agent";
import {
  useApp,
  type AutofixMode,
  type MergeGateMode,
  type PathInstruction,
  type ReviewProfile,
} from "@/lib/store";
import { errText, githubCommitRepoFile, githubReadRepoFile, jsonObj, tauriInvoke } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";

const NEW = "__new__";

const TRIGGERS = [
  { value: "on_commit", label: "on_commit — every push / PR sync" },
  { value: "on_label", label: "on_label — only on matching label" },
  { value: "manual", label: "manual — API dispatch only" },
];
const MODES = [
  { value: "review_only", label: "review_only — no sandbox" },
  { value: "interactive", label: "interactive — Upstash Box" },
  { value: "ephemeral", label: "ephemeral — Vercel Sandbox" },
];
const PROFILES: { value: ReviewProfile; label: string }[] = [
  { value: "chill", label: "chill — high confidence (≥0.70), max 2 nits" },
  { value: "assertive", label: "assertive — thorough (≥0.50), max 6 nits" },
];
const MERGE_GATES: { value: MergeGateMode; label: string }[] = [
  { value: "off", label: "off — compute internally only" },
  { value: "advisory", label: "advisory — neutral Check Run + sticky checklist" },
  { value: "required", label: "required — fail Check Run below threshold" },
];
const AUTOFIX_MODES: { value: AutofixMode; label: string }[] = [
  { value: "suggest", label: "suggest — committable suggestion blocks" },
  { value: "off", label: "off — comments only" },
  { value: "stacked", label: "stacked — fix PR (sandbox)" },
  { value: "push", label: "push — push fix commit (same-repo PRs)" },
];

interface Form {
  name: string;
  prompt: string;
  trigger: string;
  label: string;
  mode: string;
  profile: ReviewProfile;
  memoryEnabled: boolean;
  mergeGate: MergeGateMode;
  scoreThreshold: number;
  chatEnabled: boolean;
  autofix: AutofixMode;
  pathInstructions: PathInstruction[];
}

const EMPTY: Form = {
  name: "",
  prompt: "",
  trigger: "on_commit",
  label: "",
  mode: "review_only",
  profile: "chill",
  memoryEnabled: true,
  mergeGate: "advisory",
  scoreThreshold: 85,
  chatEnabled: true,
  autofix: "suggest",
  pathInstructions: [],
};

export function AgentsPage({ active }: { active: boolean }) {
  const agents = useApp((s) => s.agents);
  const picked = useApp((s) => s.pickedRepos);
  const setPicked = useApp((s) => s.setPickedRepos);
  const remote = useApp((s) => s.remoteRepos);
  const setRemote = useApp((s) => s.setRemoteRepos);
  const github = useApp((s) => s.secrets.github);

  const [selectedId, setSelectedId] = React.useState(NEW);
  const [form, setForm] = React.useState<Form>(EMPTY);
  const [repoAdd, setRepoAdd] = React.useState("");
  const [msg, setMsg] = React.useState("Agents not loaded.");
  const [confirmDelete, setConfirmDelete] = React.useState(false);

  // .pragent.yaml Preview & Commit dialog
  const [yamlOpen, setYamlOpen] = React.useState(false);
  const [yamlRepo, setYamlRepo] = React.useState("");
  const [existingRepoYaml, setExistingRepoYaml] = React.useState<string | null>(null);
  const [existingRepoYamlFor, setExistingRepoYamlFor] = React.useState<string>("");
  const [existingSource, setExistingSource] = React.useState<string>("");
  const [yamlLoading, setYamlLoading] = React.useState(false);
  const yamlSeqRef = React.useRef(0);

  // Selecting/saving/deleting is serialized: the selected id only changes together with its form.
  const [busy, setBusy] = React.useState(false);
  const patch = (p: Partial<Form>) => setForm((f) => ({ ...f, ...p }));

  const allRepos = React.useMemo(() => Array.from(new Set([...picked, ...remote])).sort(), [picked, remote]);

  const fillForm = (a: any) => {
    const rawInstructions = Array.isArray(a.path_instructions)
      ? a.path_instructions
          .filter((x: any) => x && typeof x === "object")
          .map((x: any) => ({ path: String(x.path || ""), instructions: String(x.instructions || "") }))
      : [];
    setForm({
      name: a.name || "",
      prompt: a.system_prompt || "",
      trigger: a.trigger || "on_commit",
      label: a.trigger_label || "",
      mode: a.runtime_mode || "review_only",
      profile: a.profile === "assertive" ? "assertive" : "chill",
      memoryEnabled: typeof a.memory_enabled === "boolean" ? a.memory_enabled : true,
      mergeGate: a.merge_gate === "off" || a.merge_gate === "required" ? a.merge_gate : "advisory",
      scoreThreshold:
        typeof a.score_threshold === "number" && Number.isFinite(a.score_threshold)
          ? Math.max(0, Math.min(100, Math.round(a.score_threshold)))
          : 85,
      chatEnabled: typeof a.chat_enabled === "boolean" ? a.chat_enabled : true,
      autofix:
        a.autofix === "off" || a.autofix === "stacked" || a.autofix === "push" ? a.autofix : "suggest",
      pathInstructions: rawInstructions,
    });
    const targets: string[] = Array.isArray(a.target_repos) ? a.target_repos : [];
    loadedRepos.current = targets;
    setPicked(targets);
  };

  const refresh = async (preferred?: string) => {
    setMsg("Loading agents…");
    setBusy(true); // serialized with selection and save
    try {
      const list = await refreshAgents();
      if (list === null) {
        setMsg(`Refresh failed (is backend up at ${backendUrl()}?)`);
        return;
      }
      setMsg(`Loaded ${list.length} agent(s).`);
      const keep = preferred !== undefined ? preferred : selectedId;
      if (keep && list.some((a) => a.id === keep)) {
        setSelectedId(keep);
      } else if (selectedId !== NEW || preferred !== undefined) {
        // The open agent no longer exists: clear the form together with the selection.
        selectSeq.current++;
        setSelectedId(NEW);
        fillForm({});
      }
    } finally {
      setBusy(false);
    }
  };

  // `agents` can also change from other flows (repo setup syncs an agent); count shows live.
  const selectSeq = React.useRef(0);
  const loadedRepos = React.useRef<string[]>([]);
  const onSelect = async (id: string) => {
    const seq = ++selectSeq.current; // only the newest selection may fill the form
    if (id === NEW) {
      setSelectedId(NEW);
      fillForm({});
      return;
    }
    setBusy(true);
    try {
      const a = (await apiGet("/agents/" + encodeURIComponent(id))) || {};
      if (seq === selectSeq.current) {
        fillForm(a);
        setSelectedId(id); // commit the id together with its loaded form
      }
    } catch (e) {
      if (seq === selectSeq.current) toastErr("Agent", errText(e, 200));
    } finally {
      if (seq === selectSeq.current) setBusy(false);
    }
  };

  // Repo setup can add targets to the agent that is open here; merge them in so a
  // later save does not silently drop them.
  React.useEffect(() => {
    const cur = agents.find((a) => a.id === selectedId);
    const targets = cur && Array.isArray(cur.target_repos) ? cur.target_repos : null;
    if (!targets) return;
    const added = targets.filter((r) => !loadedRepos.current.includes(r));
    loadedRepos.current = targets;
    if (added.length) setPicked(Array.from(new Set([...useApp.getState().pickedRepos, ...added])));
  }, [agents, selectedId, setPicked]);

  const save = async () => {
    const cleanedRules = form.pathInstructions
      .map((r) => ({ path: r.path.trim(), instructions: r.instructions.trim() }))
      .filter((r) => r.path && r.instructions);
    const body = {
      name: form.name.trim(),
      system_prompt: form.prompt.trim(),
      target_repos: picked,
      trigger: form.trigger,
      trigger_label: form.trigger === "on_label" ? form.label.trim() || null : null,
      runtime_mode: form.mode,
      schema_version: 2,
      profile: form.profile,
      memory_enabled: form.memoryEnabled,
      merge_gate: form.mergeGate,
      score_threshold: form.scoreThreshold,
      chat_enabled: form.chatEnabled,
      autofix: form.autofix,
      path_instructions: cleanedRules,
    };
    if (!body.name) return toastErr("Agent", "Enter an agent name first.");
    if (body.trigger === "on_label" && !body.trigger_label) {
      return toastErr("Agent", "Enter the label name for an on_label agent.");
    }
    setMsg("Saving…");
    setBusy(true);
    try {
      const saved =
        selectedId !== NEW
          ? await apiMut("PUT", "/agents/" + encodeURIComponent(selectedId), body)
          : await apiMut("POST", "/agents", body);
      const savedId = saved && saved.id;
      setMsg("Saved agent " + (savedId || body.name));
      toastOk("Agent saved", savedId || body.name);
      await refresh(savedId || undefined);
    } catch (e) {
      setMsg("Save failed: " + errText(e, 300));
      toastErr("Agent save failed", errText(e, 220));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setConfirmDelete(false);
    if (selectedId === NEW) return toastErr("Agent", "Select an existing agent to delete.");
    setMsg("Deleting…");
    setBusy(true);
    try {
      await apiMut("DELETE", "/agents/" + encodeURIComponent(selectedId));
      setMsg("Deleted.");
      toastOk("Agent deleted", selectedId.slice(0, 12));
      fillForm({});
      await refresh(NEW);
    } catch (e) {
      setMsg("Delete failed: " + errText(e, 300));
      toastErr("Delete failed", errText(e, 200));
    } finally {
      setBusy(false);
    }
  };

  const addRepo = () => {
    const v = repoAdd.trim();
    if (!isValidRepo(v)) return toastErr("Repos", "Use valid owner/repo format.");
    if (!picked.includes(v)) setPicked([...picked, v]);
    setRepoAdd("");
  };

  const loadMyRepos = async () => {
    if (!github) {
      setRemote([]);
      return toastErr("GitHub", "Connect GitHub first (Settings).");
    }
    setMsg("Loading repos…");
    try {
      const raw = await tauriInvoke("github_list_repos", { token: github });
      const arr = Array.isArray(raw) ? raw : jsonObj(raw) || [];
      const names = (Array.isArray(arr) ? arr : [])
        .map((r: any) => r.full_name || r.fullName || "")
        .filter(Boolean) as string[];
      setRemote(names);
      setMsg(`Loaded ${names.length} repo(s). Tick to target.`);
      toastOk("Repos loaded", `${names.length} repositories.`);
    } catch (e) {
      setMsg("Repo load failed: " + errText(e, 300));
      toastErr("Repo load failed", errText(e, 200));
    }
  };

  const toggleRepo = (name: string, on: boolean) =>
    setPicked(on ? Array.from(new Set([...picked, name])) : picked.filter((n) => n !== name));

  const addPathRule = () => {
    patch({ pathInstructions: [...form.pathInstructions, { path: "", instructions: "" }] });
  };
  const updatePathRule = (idx: number, next: Partial<PathInstruction>) => {
    patch({
      pathInstructions: form.pathInstructions.map((item, i) => (i === idx ? { ...item, ...next } : item)),
    });
  };
  const removePathRule = (idx: number) => {
    patch({ pathInstructions: form.pathInstructions.filter((_, i) => i !== idx) });
  };

  const yamlInput = React.useMemo<PragentYamlInput>(
    () => ({
      profile: form.profile,
      merge_gate: form.mergeGate,
      score_threshold: form.scoreThreshold,
      memory_enabled: form.memoryEnabled,
      chat_enabled: form.chatEnabled,
      path_instructions: form.pathInstructions,
    }),
    [form.profile, form.mergeGate, form.scoreThreshold, form.memoryEnabled, form.chatEnabled, form.pathInstructions],
  );

  const matchedExistingYaml =
    existingRepoYaml !== null &&
    existingRepoYamlFor === yamlRepo.trim() &&
    existingSource.startsWith("Read directly")
      ? existingRepoYaml
      : null;

  const pathInstructionsError = React.useMemo(
    () => validatePathInstructions(form.pathInstructions),
    [form.pathInstructions],
  );

  const yamlPreviewState = React.useMemo(() => {
    if (pathInstructionsError) {
      return { yaml: "", error: pathInstructionsError };
    }
    try {
      return { yaml: mergePragentYaml(matchedExistingYaml, yamlInput), error: null as string | null };
    } catch (e) {
      return { yaml: "", error: errText(e, 260) };
    }
  }, [pathInstructionsError, matchedExistingYaml, yamlInput]);
  const yamlPreview = yamlPreviewState.yaml;
  const yamlPreviewError = yamlPreviewState.error;

  const loadExistingRepoYaml = React.useCallback(
    async (targetRepo: string, showToastOnError = false) => {
      const repo = targetRepo.trim();
      const mine = ++yamlSeqRef.current;
      if (!isValidRepo(repo) || !github) {
        setExistingRepoYaml(null);
        setExistingRepoYamlFor("");
        setExistingSource("");
        setYamlLoading(false);
        return;
      }
      setYamlLoading(true);
      try {
        const content = await githubReadRepoFile(github, repo, ".pragent.yaml");
        if (mine !== yamlSeqRef.current) return;
        setExistingRepoYaml(content);
        setExistingRepoYamlFor(repo);
        setExistingSource(
          content !== null
            ? "Read directly from default branch (.pragent.yaml) — unmanaged sections preserved on commit"
            : "No existing .pragent.yaml on default branch — will create a new file",
        );
      } catch (e) {
        if (mine !== yamlSeqRef.current) return;
        setExistingRepoYaml(null);
        setExistingRepoYamlFor("");
        setExistingSource("");
        if (showToastOnError) toastErr("Could not read repo config", errText(e, 220));
      } finally {
        if (mine === yamlSeqRef.current) setYamlLoading(false);
      }
    },
    [github],
  );

  const openYamlDialog = () => {
    const target = yamlRepo || picked[0] || allRepos[0] || "";
    setYamlRepo(target);
    setExistingRepoYaml(null);
    setExistingRepoYamlFor("");
    setExistingSource("");
    setYamlOpen(true);
    if (isValidRepo(target) && github) {
      void loadExistingRepoYaml(target, false);
    }
  };

  React.useEffect(() => {
    if (!yamlOpen) return;
    const clean = yamlRepo.trim();
    if (!isValidRepo(clean) || !github) {
      yamlSeqRef.current++;
      setExistingRepoYaml(null);
      setExistingRepoYamlFor("");
      setExistingSource("");
      setYamlLoading(false);
      return;
    }
    const timer = setTimeout(() => void loadExistingRepoYaml(clean, false), 250);
    return () => clearTimeout(timer);
  }, [yamlOpen, yamlRepo, github, loadExistingRepoYaml]);

  const inspectExistingYaml = async () => {
    const repo = yamlRepo.trim();
    if (!isValidRepo(repo)) return toastErr("Repository", "Enter a valid owner/repo first.");
    const mine = ++yamlSeqRef.current;
    setYamlLoading(true);
    try {
      if (github) {
        const content = await githubReadRepoFile(github, repo, ".pragent.yaml");
        if (mine !== yamlSeqRef.current) return;
        if (content !== null) {
          setExistingRepoYaml(content);
          setExistingRepoYamlFor(repo);
          setExistingSource("Read directly from default branch (.pragent.yaml) — unmanaged sections preserved on commit");
          return;
        }
      }
      const res = await getRepoConfig(repo);
      if (mine !== yamlSeqRef.current) return;
      setExistingRepoYaml(JSON.stringify(res.config, null, 2));
      setExistingRepoYamlFor(repo);
      setExistingSource(`Backend effective config (source: ${res.source})`);
    } catch (e) {
      if (mine === yamlSeqRef.current) toastErr("Could not read repo config", errText(e, 220));
    } finally {
      if (mine === yamlSeqRef.current) setYamlLoading(false);
    }
  };

  const commitYaml = async () => {
    const repo = yamlRepo.trim();
    if (!isValidRepo(repo)) return toastErr("Repository", "Enter a valid owner/repo first.");
    if (!github) return toastErr("GitHub token required", "Connect GitHub first to commit .pragent.yaml.");
    const ruleErr = validatePathInstructions(form.pathInstructions);
    if (ruleErr) return toastErr("Invalid path instructions", ruleErr);
    try {
      // Always merge against the latest remote .pragent.yaml so path_filters,
      // pre_merge_checks, labels, tools, weights, and custom sections are preserved.
      const latestRemote = await githubReadRepoFile(github, repo, ".pragent.yaml");
      const merged = mergePragentYaml(latestRemote, yamlInput);
      const res = await githubCommitRepoFile(
        github,
        repo,
        ".pragent.yaml",
        merged,
        "chore: configure PR Agent (.pragent.yaml)",
        { expectedBaseContent: latestRemote },
      );
      toastOk(
        res.changed ? "Committed .pragent.yaml" : ".pragent.yaml already up to date",
        `${repo} (${res.sha.slice(0, 8)})`,
      );
      setYamlOpen(false);
    } catch (e) {
      toastErr("Commit failed", errText(e, 260));
    }
  };

  return (
    <Page active={active}>
      <Section
        title="Agent configuration"
        description="Agents live in the backend. Configure review profile, merge gate, repo memory, path-scoped instructions, and target repositories."
        action={<Badge variant="outline">{agents.length} agent(s)</Badge>}
        footer={
          <ButtonRow className="w-full">
            <AsyncButton onClick={save} disabled={busy}>
              <Save /> {selectedId === NEW ? "Create agent" : "Save agent"}
            </AsyncButton>
            <Button type="button" variant="outline" onClick={openYamlDialog}>
              <FileCode2 /> .pragent.yaml Preview &amp; Commit
            </Button>
            <AsyncButton variant="outline" onClick={() => refresh()} disabled={busy}>
              <RefreshCw /> Refresh
            </AsyncButton>
            <Button
              type="button"
              variant="destructive"
              disabled={selectedId === NEW || busy}
              onClick={() => setConfirmDelete(true)}
            >
              <Trash2 /> Delete selected
            </Button>
            <span className="text-muted-foreground text-xs">{msg}</span>
          </ButtonRow>
        }
      >
        <FieldGrid>
          <Field label="Existing agents" htmlFor="agent-select">
            <Select value={selectedId} onValueChange={onSelect} disabled={busy}>
              <SelectTrigger id="agent-select" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NEW}>— new agent —</SelectItem>
                {agents.map((a) => (
                  <SelectItem key={a.id} value={a.id}>
                    {(a.name || a.id) + (a.id ? ` (${a.id.slice(0, 8)})` : "")}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label="Agent name" htmlFor="agent-name">
            <Input
              id="agent-name"
              value={form.name}
              placeholder="reviewer-main"
              onChange={(e) => patch({ name: e.target.value })}
              autoComplete="off"
            />
          </Field>
        </FieldGrid>

        <FieldGrid>
          <Field label="Trigger mode" htmlFor="agent-trigger">
            <Select value={form.trigger} onValueChange={(v) => patch({ trigger: v })}>
              <SelectTrigger id="agent-trigger" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TRIGGERS.map((t) => (
                  <SelectItem key={t.value} value={t.value}>
                    {t.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label="Execution mode" htmlFor="agent-mode">
            <Select value={form.mode} onValueChange={(v) => patch({ mode: v })}>
              <SelectTrigger id="agent-mode" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MODES.map((t) => (
                  <SelectItem key={t.value} value={t.value}>
                    {t.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        </FieldGrid>

        {form.trigger === "on_label" ? (
          <Field label="Trigger label (for on_label)" htmlFor="agent-label">
            <Input
              id="agent-label"
              value={form.label}
              placeholder="review-me"
              onChange={(e) => patch({ label: e.target.value })}
              autoComplete="off"
            />
          </Field>
        ) : null}

        <Separator />

        {/* Agent v2 controls */}
        <div className="grid gap-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-semibold">Review Engine, Memory &amp; Merge Readiness (v2)</div>
              <div className="text-muted-foreground text-xs">
                Applied automatically on every webhook and manual review. Can be overridden per repo with{" "}
                <code>.pragent.yaml</code>.
              </div>
            </div>
            <Badge variant="secondary">schema v2</Badge>
          </div>

          <FieldGrid>
            <Field label="Review profile" htmlFor="agent-profile">
              <Select value={form.profile} onValueChange={(v) => patch({ profile: v as ReviewProfile })}>
                <SelectTrigger id="agent-profile" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PROFILES.map((p) => (
                    <SelectItem key={p.value} value={p.value}>
                      {p.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field label="Autofix / Suggestions" htmlFor="agent-autofix">
              <Select value={form.autofix} onValueChange={(v) => patch({ autofix: v as AutofixMode })}>
                <SelectTrigger id="agent-autofix" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {AUTOFIX_MODES.map((m) => (
                    <SelectItem key={m.value} value={m.value}>
                      {m.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </FieldGrid>

          <FieldGrid>
            <Field label="Merge readiness gate" htmlFor="agent-merge-gate">
              <Select value={form.mergeGate} onValueChange={(v) => patch({ mergeGate: v as MergeGateMode })}>
                <SelectTrigger id="agent-merge-gate" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {MERGE_GATES.map((g) => (
                    <SelectItem key={g.value} value={g.value}>
                      {g.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field
              label={
                <span className="flex items-center justify-between">
                  <span>Merge score threshold</span>
                  <Badge variant="outline" className="font-mono tabular-nums">
                    {form.scoreThreshold} / 100
                  </Badge>
                </span>
              }
              hint="Required Check Runs fail when score is below this threshold or when hard blockers remain."
            >
              <div className="pt-2">
                <Slider
                  min={0}
                  max={100}
                  step={5}
                  value={[form.scoreThreshold]}
                  onValueChange={([v]) => patch({ scoreThreshold: v })}
                />
              </div>
            </Field>
          </FieldGrid>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="bg-muted/25 flex items-center justify-between gap-3 rounded-lg border px-3.5 py-2.5">
              <div className="grid gap-0.5">
                <Label htmlFor="agent-memory-enabled" className="cursor-pointer text-xs font-medium">
                  Repo memory enabled
                </Label>
                <span className="text-muted-foreground text-[11px]">
                  Inject scoped learnings and suppress dismissed false positives.
                </span>
              </div>
              <Switch
                id="agent-memory-enabled"
                checked={form.memoryEnabled}
                onCheckedChange={(v) => patch({ memoryEnabled: v })}
              />
            </div>

            <div className="bg-muted/25 flex items-center justify-between gap-3 rounded-lg border px-3.5 py-2.5">
              <div className="grid gap-0.5">
                <Label htmlFor="agent-chat-enabled" className="cursor-pointer text-xs font-medium">
                  @pr-agent PR chat enabled
                </Label>
                <span className="text-muted-foreground text-[11px]">
                  Handle @pr-agent ask, remember, dismiss, review, and readiness replies.
                </span>
              </div>
              <Switch
                id="agent-chat-enabled"
                checked={form.chatEnabled}
                onCheckedChange={(v) => patch({ chatEnabled: v })}
              />
            </div>
          </div>

          {/* Dynamic path_instructions */}
          <Field
            label={
              <span className="flex items-center justify-between">
                <span>Path-scoped instructions ({form.pathInstructions.length})</span>
                <Button type="button" variant="outline" size="xs" onClick={addPathRule}>
                  <Plus /> Add path rule
                </Button>
              </span>
            }
            hint="Optional CodeRabbit-style rules injected only when a PR touches matching file globs."
          >
            {form.pathInstructions.length === 0 ? (
              <div className="bg-muted/20 rounded-md border border-dashed px-3 py-3 text-xs text-muted-foreground">
                No path rules yet. Click <b>Add path rule</b> to add targeted guidance (e.g.{" "}
                <code>backend/auth/**</code> or <code>src/**/*.tsx</code>).
              </div>
            ) : (
              <div className="grid gap-2.5">
                {form.pathInstructions.map((rule, idx) => (
                  <div key={idx} className="bg-muted/20 grid gap-2 rounded-lg border p-3">
                    <div className="flex items-center gap-2">
                      <Input
                        value={rule.path}
                        onChange={(e) => updatePathRule(idx, { path: e.target.value })}
                        placeholder="src/**/*.ts"
                        className="flex-1 font-mono text-xs"
                        aria-label={`Path glob ${idx + 1}`}
                      />
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-xs"
                        onClick={() => removePathRule(idx)}
                        aria-label="Remove path rule"
                      >
                        <X />
                      </Button>
                    </div>
                    <Textarea
                      rows={2}
                      value={rule.instructions}
                      onChange={(e) => updatePathRule(idx, { instructions: e.target.value })}
                      placeholder="Review guidance applied only to files matching this glob…"
                      className="text-xs"
                    />
                  </div>
                ))}
              </div>
            )}
          </Field>
        </div>

        <Separator />

        <Field label="Target repos" hint="Leave empty to match every repository.">
          <div className="rounded-md border">
            <ScrollArea className="max-h-48">
              <div className="grid gap-0.5 p-2">
                {allRepos.length ? (
                  allRepos.map((name) => (
                    <Label
                      key={name}
                      className="hover:bg-muted/60 flex cursor-pointer items-center gap-2.5 rounded px-2 py-1.5 font-mono text-xs font-normal"
                    >
                      <Checkbox checked={picked.includes(name)} onCheckedChange={(v) => toggleRepo(name, v === true)} />
                      {name}
                    </Label>
                  ))
                ) : (
                  <Hint className="px-2 py-3">
                    No repos yet — connect GitHub in Settings to load your repos, or add owner/repo below.
                  </Hint>
                )}
              </div>
            </ScrollArea>
          </div>
          <div className="flex flex-wrap gap-2">
            <Input
              value={repoAdd}
              onChange={(e) => setRepoAdd(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addRepo()}
              placeholder="owner/repo"
              className="min-w-40 flex-1"
              autoComplete="off"
              aria-label="Add repository"
            />
            <Button type="button" variant="outline" onClick={addRepo}>
              <Plus /> Add repo
            </Button>
            <AsyncButton variant="ghost" onClick={loadMyRepos}>
              <Download /> Load my repos
            </AsyncButton>
          </div>
        </Field>

        <Field label="System prompt" htmlFor="agent-prompt">
          <Textarea
            id="agent-prompt"
            rows={4}
            value={form.prompt}
            placeholder="You are a senior reviewer…"
            onChange={(e) => patch({ prompt: e.target.value })}
          />
        </Field>
      </Section>

      {/* Delete confirmation */}
      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete this agent?</DialogTitle>
            <DialogDescription>
              {form.name || selectedId} will be removed from the backend. PRs it targeted fall back to other agents or the
              default flow.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={remove}>
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* .pragent.yaml Preview & Commit Dialog */}
      <Dialog open={yamlOpen} onOpenChange={setYamlOpen}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>.pragent.yaml Preview &amp; Commit</DialogTitle>
            <DialogDescription>
              Generated from your current Agent v2 form. Committing to a repository base branch shares these review rules
              with your team.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <Field label="Target repository" htmlFor="agent-yaml-repo">
              <div className="flex gap-2">
                <Input
                  id="agent-yaml-repo"
                  value={yamlRepo}
                  onChange={(e) => setYamlRepo(e.target.value)}
                  placeholder="owner/repo"
                  list="agent-known-repos"
                  className="flex-1 font-mono text-xs"
                />
                <datalist id="agent-known-repos">
                  {allRepos.map((r) => (
                    <option key={r} value={r} />
                  ))}
                </datalist>
                <AsyncButton variant="outline" size="sm" onClick={inspectExistingYaml}>
                  Check existing
                </AsyncButton>
              </div>
            </Field>
            <div className="grid gap-1.5">
              <div className="text-xs font-medium">
                Preview to commit (.pragent.yaml){yamlLoading ? " — loading existing file…" : ""}
              </div>
              {yamlPreviewError ? (
                <div className="border-destructive/40 bg-destructive/10 text-destructive rounded-md border px-3 py-2 text-xs">
                  {yamlPreviewError}
                </div>
              ) : (
                <Console tall>{yamlPreview}</Console>
              )}
            </div>
            {existingRepoYaml !== null ? (
              <div className="grid gap-1.5">
                <div className="text-muted-foreground text-xs">{existingSource}</div>
                <Console>{existingRepoYaml}</Console>
              </div>
            ) : null}
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={Boolean(yamlPreviewError)}
              onClick={async () => {
                if (yamlPreviewError) return;
                if (await copyToClipboard(yamlPreview)) toastOk("Copied", ".pragent.yaml");
              }}
            >
              <ClipboardCopy /> Copy YAML
            </Button>
            <AsyncButton onClick={commitYaml} disabled={Boolean(yamlPreviewError)}>
              <FileCode2 /> Commit .pragent.yaml
            </AsyncButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Page>
  );
}
