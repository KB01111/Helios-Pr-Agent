import * as React from "react";
import {
  Archive,
  BookOpen,
  Brain,
  Check,
  ClipboardCopy,
  Download,
  FileCode2,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldAlert,
  Sparkles,
  Trash2,
  Upload,
} from "lucide-react";
import { AsyncButton, ButtonRow, Console, Field, FieldGrid, Hint, Page, Section } from "@/components/common";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  approveRepoMemory,
  bootstrapRepoMemory,
  copyToClipboard,
  createRepoMemory,
  deleteRepoMemory,
  getRepoMemoryMarkdown,
  importRepoMemory,
  isValidRepo,
  listRepoMemory,
  relativeTime,
  updateRepoMemory,
  type MemoryEntry,
  type MemoryKind,
  type MemoryStatus,
  type RepoProfile,
} from "@/lib/cloud-agent";
import { useGh } from "@/lib/github";
import { useApp } from "@/lib/store";
import { errText, githubCommitRepoFile } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";

type MemoryFilterTab = "active" | "pending" | "dismissed" | "facts" | "archived";

function parseGlobs(raw: string): string[] {
  const arr = raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return arr.length ? arr : ["**"];
}

function kindBadgeVariant(kind: MemoryKind): "default" | "secondary" | "warning" | "danger" | "outline" {
  switch (kind) {
    case "learning":
      return "default";
    case "dismissed_pattern":
      return "warning";
    case "repo_fact":
      return "secondary";
    case "incident":
      return "danger";
    default:
      return "outline";
  }
}

export function MemoryPage({ active }: { active: boolean }) {
  const agents = useApp((s) => s.agents);
  const backend = useApp((s) => s.backendUrl);
  const githubToken = useApp((s) => s.secrets.github);
  const ghRepos = useGh((s) => s.repos);

  const knownRepos = React.useMemo(() => {
    const s = new Set<string>();
    for (const a of agents) {
      if (Array.isArray(a.target_repos)) {
        for (const r of a.target_repos) s.add(r);
      }
    }
    for (const r of ghRepos) s.add(r.full_name);
    return Array.from(s).sort();
  }, [agents, ghRepos]);

  const [repo, setRepo] = React.useState("");
  const [repoDraft, setRepoDraft] = React.useState("");
  const [items, setItems] = React.useState<MemoryEntry[]>([]);
  const [profile, setProfile] = React.useState<RepoProfile | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [err, setErr] = React.useState("");

  const [filterTab, setFilterTab] = React.useState<MemoryFilterTab>("active");
  const [search, setSearch] = React.useState("");

  // Add Learning dialog
  const [addOpen, setAddOpen] = React.useState(false);
  const [addText, setAddText] = React.useState("");
  const [addKind, setAddKind] = React.useState<MemoryKind>("learning");
  const [addGlobs, setAddGlobs] = React.useState("**");
  const [addBlocking, setAddBlocking] = React.useState(false);

  // Edit Learning dialog
  const [editTarget, setEditTarget] = React.useState<MemoryEntry | null>(null);
  const [editText, setEditText] = React.useState("");
  const [editGlobs, setEditGlobs] = React.useState("**");
  const [editBlocking, setEditBlocking] = React.useState(false);
  const [editStatus, setEditStatus] = React.useState<MemoryStatus>("active");

  // Delete confirmation dialog
  const [deleteTarget, setDeleteTarget] = React.useState<MemoryEntry | null>(null);

  // Import JSON dialog
  const [importOpen, setImportOpen] = React.useState(false);
  const [importText, setImportText] = React.useState("");

  // Sync .pr-agent/memory.md dialog
  const [syncOpen, setSyncOpen] = React.useState(false);
  const [syncMarkdown, setSyncMarkdown] = React.useState("");
  const [syncLoading, setSyncLoading] = React.useState(false);

  // Auto-select first known repo if none selected yet
  React.useEffect(() => {
    if (!repo && knownRepos.length > 0) {
      setRepo(knownRepos[0]);
      setRepoDraft(knownRepos[0]);
    }
  }, [repo, knownRepos]);

  const seq = React.useRef(0);
  const loadMemory = React.useCallback(async (targetRepo: string) => {
    const clean = targetRepo.trim();
    if (!clean || !isValidRepo(clean)) {
      setItems([]);
      setProfile(null);
      return;
    }
    const mine = ++seq.current;
    setLoading(true);
    setErr("");
    try {
      const res = await listRepoMemory(clean);
      if (mine !== seq.current) return;
      setItems(res.items);
      setProfile(res.profile ?? null);
    } catch (e) {
      if (mine !== seq.current) return;
      setErr(errText(e, 260));
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    if (active && repo) void loadMemory(repo);
  }, [active, repo, backend, loadMemory]);

  const selectRepo = (next: string) => {
    const clean = next.trim();
    if (!isValidRepo(clean)) {
      toastErr("Repository", "Enter a valid owner/repo name.");
      return;
    }
    setRepo(clean);
    setRepoDraft(clean);
  };

  // Tab counts
  const counts = React.useMemo(() => {
    let activeCount = 0;
    let pendingCount = 0;
    let dismissedCount = 0;
    let factsCount = 0;
    let archivedCount = 0;
    for (const m of items) {
      if (m.status === "archived") {
        archivedCount++;
        continue;
      }
      if (m.status === "active") activeCount++;
      if (m.status === "pending") pendingCount++;
      if (m.kind === "dismissed_pattern") dismissedCount++;
      if (m.kind === "repo_fact") factsCount++;
    }
    return { activeCount, pendingCount, dismissedCount, factsCount, archivedCount };
  }, [items]);

  const visibleItems = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    return items.filter((m) => {
      if (filterTab === "archived") {
        if (m.status !== "archived") return false;
      } else {
        if (m.status === "archived") return false;
        if (filterTab === "active" && m.status !== "active") return false;
        if (filterTab === "pending" && m.status !== "pending") return false;
        if (filterTab === "dismissed" && m.kind !== "dismissed_pattern") return false;
        if (filterTab === "facts" && m.kind !== "repo_fact") return false;
      }
      if (!q) return true;
      return (
        m.text.toLowerCase().includes(q) ||
        m.id.toLowerCase().includes(q) ||
        m.source.toLowerCase().includes(q) ||
        (m.author || "").toLowerCase().includes(q) ||
        m.scope_globs.some((g) => g.toLowerCase().includes(q))
      );
    });
  }, [items, filterTab, search]);

  /* -------------------- Actions -------------------- */
  const handleAdd = async () => {
    if (!isValidRepo(repo)) return toastErr("Memory", "Select a repository first.");
    const text = addText.trim();
    if (!text) return toastErr("Add learning", "Enter learning text first.");
    try {
      const created = await createRepoMemory(repo, {
        kind: addKind,
        text,
        scope_globs: parseGlobs(addGlobs),
        blocking: addBlocking,
        status: "active",
      });
      toastOk("Learning added", `#${created.id.slice(0, 8)} · ${repo}`);
      setAddText("");
      setAddGlobs("**");
      setAddBlocking(false);
      setAddOpen(false);
      await loadMemory(repo);
    } catch (e) {
      toastErr("Create failed", errText(e, 240));
    }
  };

  const openEdit = (m: MemoryEntry) => {
    setEditTarget(m);
    setEditText(m.text);
    setEditGlobs((m.scope_globs || ["**"]).join(", "));
    setEditBlocking(Boolean(m.blocking));
    setEditStatus(m.status);
  };

  const handleEditSave = async () => {
    if (!editTarget || !isValidRepo(repo)) return;
    const text = editText.trim();
    if (!text) return toastErr("Edit learning", "Text cannot be empty.");
    try {
      await updateRepoMemory(repo, editTarget.id, {
        text,
        scope_globs: parseGlobs(editGlobs),
        blocking: editBlocking,
        status: editStatus,
      });
      toastOk("Learning updated", `#${editTarget.id.slice(0, 8)}`);
      setEditTarget(null);
      await loadMemory(repo);
    } catch (e) {
      toastErr("Update failed", errText(e, 240));
    }
  };

  const handleApprove = async (m: MemoryEntry) => {
    try {
      await approveRepoMemory(repo, m.id);
      toastOk("Approved learning", `#${m.id.slice(0, 8)} is now active.`);
      await loadMemory(repo);
    } catch (e) {
      toastErr("Approve failed", errText(e, 220));
    }
  };

  const handleArchive = async (m: MemoryEntry) => {
    try {
      await updateRepoMemory(repo, m.id, { status: "archived" });
      toastOk("Archived learning", `#${m.id.slice(0, 8)}`);
      await loadMemory(repo);
    } catch (e) {
      toastErr("Archive failed", errText(e, 220));
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await deleteRepoMemory(repo, deleteTarget.id);
      toastOk("Deleted learning", `#${deleteTarget.id.slice(0, 8)}`);
      setDeleteTarget(null);
      await loadMemory(repo);
    } catch (e) {
      toastErr("Delete failed", errText(e, 220));
    }
  };

  const handleBootstrap = async () => {
    if (!isValidRepo(repo)) return toastErr("Bootstrap", "Select a valid repository first.");
    try {
      const res = await bootstrapRepoMemory(repo);
      toastOk(
        "Repo memory bootstrapped",
        `${repo}: added ${res.created.length} repo fact(s) (${res.skipped} already known).`,
      );
      await loadMemory(repo);
    } catch (e) {
      toastErr("Bootstrap failed", errText(e, 260));
    }
  };

  const handleExportJson = async () => {
    if (!isValidRepo(repo)) return toastErr("Export", "Select a repository first.");
    const payload = JSON.stringify({ repo, exported_at: new Date().toISOString(), items }, null, 2);
    try {
      const blob = new Blob([payload], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `pr-agent-memory-${repo.replace("/", "-")}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toastOk("Exported JSON", `${items.length} memory entry(ies) downloaded.`);
    } catch {
      const copied = await copyToClipboard(payload);
      if (copied) toastOk("Copied JSON", `${items.length} memory entry(ies) copied to clipboard.`);
      else toastErr("Export failed", "Could not download or copy JSON.");
    }
  };

  const handleImportFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => setImportText(String(reader.result || ""));
    reader.readAsText(file);
  };

  const handleImportSubmit = async () => {
    if (!isValidRepo(repo)) return toastErr("Import", "Select a repository first.");
    let parsed: unknown;
    try {
      parsed = JSON.parse(importText);
    } catch (e) {
      return toastErr("Invalid JSON", errText(e, 180));
    }
    const list = Array.isArray(parsed)
      ? parsed
      : parsed && typeof parsed === "object" && Array.isArray((parsed as any).items)
        ? (parsed as any).items
        : null;
    if (!list) return toastErr("Invalid JSON", "Expected a JSON array or an object with an `items` array.");
    try {
      const res = await importRepoMemory(repo, list);
      toastOk("Memory imported", `Imported ${res.imported}, skipped ${res.skipped}.`);
      setImportText("");
      setImportOpen(false);
      await loadMemory(repo);
    } catch (e) {
      toastErr("Import failed", errText(e, 240));
    }
  };

  const openSyncMarkdown = async () => {
    if (!isValidRepo(repo)) return toastErr("Sync memory", "Select a repository first.");
    setSyncOpen(true);
    setSyncLoading(true);
    try {
      const res = await getRepoMemoryMarkdown(repo);
      setSyncMarkdown(res.markdown || "# PR Agent Memory\n\nNo active learnings yet.\n");
    } catch (e) {
      setSyncMarkdown("");
      toastErr("Could not render memory.md", errText(e, 240));
    } finally {
      setSyncLoading(false);
    }
  };

  const commitMemoryMarkdown = async () => {
    if (!isValidRepo(repo)) return toastErr("Sync memory", "Select a repository first.");
    if (!githubToken) {
      return toastErr("GitHub token required", "Sign in with GitHub first to commit .pr-agent/memory.md.");
    }
    try {
      const res = await githubCommitRepoFile(
        githubToken,
        repo,
        ".pr-agent/memory.md",
        syncMarkdown,
        "chore(pr-agent): sync .pr-agent/memory.md",
      );
      toastOk(
        res.changed ? "Synced .pr-agent/memory.md" : ".pr-agent/memory.md already up to date",
        `${repo} (${res.sha.slice(0, 8)})`,
      );
      setSyncOpen(false);
    } catch (e) {
      toastErr("Commit failed", errText(e, 260));
    }
  };

  return (
    <Page active={active} className="max-w-6xl">
      {/* Repo Picker & Top Actions */}
      <Section
        title={
          <span className="flex items-center gap-2">
            <Brain className="text-primary size-4" />
            Repository Memory
          </span>
        }
        description="Conventions, architecture facts, and dismissed false-positive patterns scoped by path globs and injected into every future PR review."
        action={
          repo ? (
            <Badge variant="outline" className="font-mono">
              {items.length} entries
            </Badge>
          ) : null
        }
      >
        <div className="flex flex-wrap items-end gap-2.5">
          {knownRepos.length > 0 ? (
            <Field label="Known repositories" htmlFor="mem-repo-select" className="min-w-52 flex-1">
              <Select value={knownRepos.includes(repo) ? repo : "__custom__"} onValueChange={(v) => v !== "__custom__" && selectRepo(v)}>
                <SelectTrigger id="mem-repo-select" className="w-full font-mono text-xs">
                  <SelectValue placeholder="Choose repository" />
                </SelectTrigger>
                <SelectContent>
                  {knownRepos.map((r) => (
                    <SelectItem key={r} value={r}>
                      {r}
                    </SelectItem>
                  ))}
                  {repo && !knownRepos.includes(repo) ? (
                    <SelectItem value="__custom__">{repo} (custom)</SelectItem>
                  ) : null}
                </SelectContent>
              </Select>
            </Field>
          ) : null}

          <Field label="Repository (owner/repo)" htmlFor="mem-repo-input" className="min-w-56 flex-1">
            <div className="flex gap-2">
              <Input
                id="mem-repo-input"
                value={repoDraft}
                onChange={(e) => setRepoDraft(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && selectRepo(repoDraft)}
                placeholder="owner/repo"
                className="font-mono text-xs"
              />
              <Button type="button" variant="outline" onClick={() => selectRepo(repoDraft)}>
                Load
              </Button>
            </div>
          </Field>
        </div>

        {/* Top Actions */}
        <ButtonRow>
          <Button
            type="button"
            size="sm"
            disabled={!isValidRepo(repo)}
            onClick={() => {
              setAddText("");
              setAddKind("learning");
              setAddGlobs("**");
              setAddBlocking(false);
              setAddOpen(true);
            }}
          >
            <Plus /> Add Learning
          </Button>
          <AsyncButton variant="outline" size="sm" disabled={!isValidRepo(repo)} onClick={handleBootstrap}>
            <Sparkles /> Bootstrap from Repo Docs
          </AsyncButton>
          <Button type="button" variant="outline" size="sm" disabled={!isValidRepo(repo)} onClick={openSyncMarkdown}>
            <FileCode2 /> Sync .pr-agent/memory.md
          </Button>
          <Button type="button" variant="outline" size="sm" disabled={!isValidRepo(repo)} onClick={handleExportJson}>
            <Download /> Export JSON
          </Button>
          <Button type="button" variant="outline" size="sm" disabled={!isValidRepo(repo)} onClick={() => setImportOpen(true)}>
            <Upload /> Import JSON
          </Button>
          <AsyncButton
            variant="ghost"
            size="sm"
            disabled={!isValidRepo(repo) || loading}
            onClick={() => loadMemory(repo)}
          >
            <RefreshCw className={loading ? "animate-spin" : ""} /> Refresh
          </AsyncButton>
        </ButtonRow>

        {/* Detected Repo Profile */}
        {profile &&
        (profile.languages?.length ||
          profile.frameworks?.length ||
          profile.test_commands?.length ||
          profile.build_commands?.length) ? (
          <div className="bg-muted/25 flex flex-wrap items-center gap-2 rounded-lg border px-3.5 py-2.5 text-xs">
            <BookOpen className="text-primary size-3.5 shrink-0" />
            <span className="font-medium">Repo profile:</span>
            {profile.languages?.map((l) => (
              <Badge key={`lang-${l}`} variant="secondary" className="text-[10px]">
                {l}
              </Badge>
            ))}
            {profile.frameworks?.map((fw) => (
              <Badge key={`fw-${fw}`} variant="outline" className="text-[10px]">
                {fw}
              </Badge>
            ))}
            {profile.test_commands?.length ? (
              <span className="text-muted-foreground">
                test: <code>{profile.test_commands[0]}</code>
              </span>
            ) : null}
            {profile.build_commands?.length ? (
              <span className="text-muted-foreground">
                build: <code>{profile.build_commands[0]}</code>
              </span>
            ) : null}
          </div>
        ) : null}
      </Section>

      {err ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load repo memory</AlertTitle>
          <AlertDescription>{err}</AlertDescription>
        </Alert>
      ) : null}

      {/* Filter Tabs + Search + Entries List */}
      <Section
        title={repo ? `Memories for ${repo}` : "Select a repository"}
        action={
          <div className="relative w-56">
            <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search text, globs, source…"
              className="h-8 pl-8 text-xs"
            />
          </div>
        }
      >
        <Tabs value={filterTab} onValueChange={(v) => setFilterTab(v as MemoryFilterTab)}>
          <TabsList className="flex-wrap">
            <TabsTrigger value="active">
              Active
              <Badge variant="secondary" className="ml-1 text-[10px]">
                {counts.activeCount}
              </Badge>
            </TabsTrigger>
            <TabsTrigger value="pending">
              Pending approval
              <Badge variant={counts.pendingCount > 0 ? "warning" : "outline"} className="ml-1 text-[10px]">
                {counts.pendingCount}
              </Badge>
            </TabsTrigger>
            <TabsTrigger value="dismissed">
              Dismissed patterns
              <Badge variant="outline" className="ml-1 text-[10px]">
                {counts.dismissedCount}
              </Badge>
            </TabsTrigger>
            <TabsTrigger value="facts">
              Repo facts
              <Badge variant="outline" className="ml-1 text-[10px]">
                {counts.factsCount}
              </Badge>
            </TabsTrigger>
            <TabsTrigger value="archived">
              Archived
              <Badge variant="outline" className="ml-1 text-[10px]">
                {counts.archivedCount}
              </Badge>
            </TabsTrigger>
          </TabsList>
        </Tabs>

        {!isValidRepo(repo) ? (
          <Hint className="py-6 text-center text-xs">
            Choose a repository above (or type <code>owner/repo</code> and click Load) to view and edit its memory.
          </Hint>
        ) : loading && items.length === 0 ? (
          <div className="grid gap-2.5">
            <Skeleton className="h-24 rounded-lg" />
            <Skeleton className="h-24 rounded-lg" />
          </div>
        ) : visibleItems.length === 0 ? (
          <div className="bg-muted/20 flex flex-col items-center gap-2 rounded-lg border border-dashed py-8 text-center">
            <Brain className="text-muted-foreground/60 size-8" />
            <div className="text-sm font-medium">No entries in this view</div>
            <p className="text-muted-foreground max-w-md text-xs">
              {filterTab === "pending"
                ? "Candidate learnings extracted from merged PRs will appear here for 1-click approval."
                : "Click Bootstrap from Repo Docs to index CONTRIBUTING.md / AGENTS.md / package manifests, or add your own learning."}
            </p>
          </div>
        ) : (
          <div className="grid gap-2.5">
            {visibleItems.map((m) => (
              <div
                key={m.id}
                className={cn(
                  "bg-muted/20 grid gap-2.5 rounded-lg border p-3.5",
                  m.status === "archived" && "opacity-60",
                )}
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Badge variant={kindBadgeVariant(m.kind)}>{m.kind}</Badge>
                    {m.status === "pending" ? <Badge variant="warning">pending approval</Badge> : null}
                    {m.status === "archived" ? <Badge variant="outline">archived</Badge> : null}
                    {m.blocking ? (
                      <Badge variant="danger">
                        <ShieldAlert className="size-3" /> blocking
                      </Badge>
                    ) : null}
                    <code className="text-[11px]">#{m.id.slice(0, 8)}</code>
                    <span className="text-muted-foreground text-[11px]">
                      via <b>{m.source}</b>
                      {m.author ? ` · @${m.author}` : ""}
                    </span>
                  </div>

                  <ButtonRow>
                    {m.status === "pending" ? (
                      <>
                        <AsyncButton size="xs" onClick={() => handleApprove(m)}>
                          <Check /> Approve
                        </AsyncButton>
                        <AsyncButton variant="outline" size="xs" onClick={() => handleArchive(m)}>
                          <Archive /> Archive
                        </AsyncButton>
                      </>
                    ) : m.status === "archived" ? (
                      <AsyncButton size="xs" variant="outline" onClick={() => handleApprove(m)}>
                        <RotateCcw /> Restore
                      </AsyncButton>
                    ) : null}
                    <Button type="button" variant="ghost" size="xs" onClick={() => openEdit(m)}>
                      <Pencil /> Edit
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="xs"
                      className="text-destructive hover:text-destructive"
                      onClick={() => setDeleteTarget(m)}
                    >
                      <Trash2 /> Delete
                    </Button>
                  </ButtonRow>
                </div>

                <p className="text-sm leading-relaxed whitespace-pre-wrap">{m.text}</p>

                <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]">
                  <div className="flex flex-wrap items-center gap-1.5">
                    {(m.scope_globs?.length ? m.scope_globs : ["**"]).map((glob) => (
                      <Badge key={glob} variant="outline" className="font-mono text-[10px]">
                        {glob}
                      </Badge>
                    ))}
                  </div>
                  <div className="text-muted-foreground flex flex-wrap items-center gap-3 tabular-nums">
                    <span>
                      <b className="text-foreground">{m.hits ?? 0}</b> hit(s)
                    </span>
                    {m.last_used ? <span>used {relativeTime(m.last_used)}</span> : null}
                    <span>added {relativeTime(m.created_at)}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* Add Learning Dialog */}
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Learning ({repo})</DialogTitle>
            <DialogDescription>
              Store a repo rule, architecture fact, or pattern to inject into future reviews.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <Field label="Memory text" htmlFor="mem-add-text">
              <Textarea
                id="mem-add-text"
                rows={4}
                value={addText}
                onChange={(e) => setAddText(e.target.value)}
                placeholder="e.g. Never log request headers or bearer tokens in backend/main.py"
              />
            </Field>
            <FieldGrid>
              <Field label="Kind" htmlFor="mem-add-kind">
                <Select value={addKind} onValueChange={(v) => setAddKind(v as MemoryKind)}>
                  <SelectTrigger id="mem-add-kind" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="learning">learning (rule)</SelectItem>
                    <SelectItem value="repo_fact">repo_fact (architecture / commands)</SelectItem>
                    <SelectItem value="dismissed_pattern">dismissed_pattern (suppress noise)</SelectItem>
                    <SelectItem value="incident">incident (past bug signature)</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              <Field label="Scope globs (comma-separated)" htmlFor="mem-add-globs">
                <Input
                  id="mem-add-globs"
                  value={addGlobs}
                  onChange={(e) => setAddGlobs(e.target.value)}
                  placeholder="**, backend/**/*.py"
                  className="font-mono text-xs"
                />
              </Field>
            </FieldGrid>
            <div className="flex items-center justify-between rounded-md border px-3 py-2">
              <div className="grid gap-0.5">
                <Label htmlFor="mem-add-blocking" className="cursor-pointer text-xs font-medium">
                  Blocking rule
                </Label>
                <span className="text-muted-foreground text-[11px]">
                  Violations act as hard blockers in Merge Readiness (cap score at 49).
                </span>
              </div>
              <Switch id="mem-add-blocking" checked={addBlocking} onCheckedChange={setAddBlocking} />
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setAddOpen(false)}>
              Cancel
            </Button>
            <AsyncButton onClick={handleAdd}>
              <Plus /> Save learning
            </AsyncButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit Learning Dialog */}
      <Dialog open={editTarget !== null} onOpenChange={(o) => !o && setEditTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Learning #{editTarget?.id.slice(0, 8)}</DialogTitle>
            <DialogDescription>Update wording, file scope globs, blocking severity, or status.</DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <Field label="Memory text" htmlFor="mem-edit-text">
              <Textarea
                id="mem-edit-text"
                rows={4}
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
              />
            </Field>
            <FieldGrid>
              <Field label="Scope globs" htmlFor="mem-edit-globs">
                <Input
                  id="mem-edit-globs"
                  value={editGlobs}
                  onChange={(e) => setEditGlobs(e.target.value)}
                  className="font-mono text-xs"
                />
              </Field>
              <Field label="Status" htmlFor="mem-edit-status">
                <Select value={editStatus} onValueChange={(v) => setEditStatus(v as MemoryStatus)}>
                  <SelectTrigger id="mem-edit-status" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="active">active</SelectItem>
                    <SelectItem value="pending">pending</SelectItem>
                    <SelectItem value="archived">archived</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
            </FieldGrid>
            <div className="flex items-center justify-between rounded-md border px-3 py-2">
              <Label htmlFor="mem-edit-blocking" className="cursor-pointer text-xs font-medium">
                Blocking rule (merge gate)
              </Label>
              <Switch id="mem-edit-blocking" checked={editBlocking} onCheckedChange={setEditBlocking} />
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setEditTarget(null)}>
              Cancel
            </Button>
            <AsyncButton onClick={handleEditSave}>Save changes</AsyncButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteTarget !== null} onOpenChange={(o) => !o && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete this memory entry?</DialogTitle>
            <DialogDescription>
              Entry <code>#{deleteTarget?.id.slice(0, 8)}</code> will be permanently removed from {repo}.
            </DialogDescription>
          </DialogHeader>
          {deleteTarget ? (
            <div className="bg-muted/40 rounded-md border p-3 text-xs leading-relaxed">{deleteTarget.text}</div>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setDeleteTarget(null)}>
              Cancel
            </Button>
            <AsyncButton variant="destructive" onClick={handleDelete}>
              <Trash2 /> Delete
            </AsyncButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Import JSON Dialog */}
      <Dialog open={importOpen} onOpenChange={setImportOpen}>
        <DialogContent className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Import Memory JSON ({repo})</DialogTitle>
            <DialogDescription>
              Upload an exported JSON file or paste a JSON array of memory entries. Duplicates are skipped automatically.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <Input type="file" accept=".json,application/json" onChange={handleImportFile} />
            <Textarea
              rows={8}
              value={importText}
              onChange={(e) => setImportText(e.target.value)}
              placeholder='[{"kind": "learning", "text": "Use strict typing", "scope_globs": ["src/**/*.ts"], "blocking": false}]'
              className="font-mono text-xs"
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setImportOpen(false)}>
              Cancel
            </Button>
            <AsyncButton onClick={handleImportSubmit} disabled={!importText.trim()}>
              <Upload /> Import entries
            </AsyncButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Sync .pr-agent/memory.md Dialog */}
      <Dialog open={syncOpen} onOpenChange={setSyncOpen}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Sync .pr-agent/memory.md to {repo}</DialogTitle>
            <DialogDescription>
              Commits your active repo memory as Markdown so GitHub Actions Lite mode and human contributors share the
              exact same conventions.
            </DialogDescription>
          </DialogHeader>
          {syncLoading ? <Skeleton className="h-48 rounded-md" /> : <Console tall>{syncMarkdown}</Console>}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={!syncMarkdown}
              onClick={async () => {
                if (await copyToClipboard(syncMarkdown)) toastOk("Copied", ".pr-agent/memory.md");
              }}
            >
              <ClipboardCopy /> Copy Markdown
            </Button>
            <AsyncButton onClick={commitMemoryMarkdown} disabled={syncLoading || !syncMarkdown}>
              <FileCode2 /> Commit .pr-agent/memory.md
            </AsyncButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Page>
  );
}
