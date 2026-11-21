import * as React from "react";
import {
  AlertOctagon,
  AlertTriangle,
  BookMarked,
  Brain,
  Check,
  CheckCircle2,
  Circle,
  ClipboardCopy,
  ExternalLink,
  FileCode2,
  GitPullRequest,
  Inbox,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  Sparkles,
  ThumbsDown,
  XCircle,
} from "lucide-react";
import { AsyncButton, ButtonRow, Field, FieldGrid, Hint, Page, Section } from "@/components/common";
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
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  bandBadgeVariant,
  bandLabel,
  copyToClipboard,
  createRepoMemory,
  getPullReadiness,
  listPrInbox,
  recomputePullReadiness,
  relativeTime,
  severityBadgeVariant,
  triggerManualReview,
  updateFindingState,
  type InboxPR,
  type MemoryKind,
  type PullReadinessResponse,
  type ReadinessBand,
  type ReadinessItem,
  type StoredFinding,
} from "@/lib/cloud-agent";
import { useApp } from "@/lib/store";
import { errText, openUrl } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";

type BandFilter = "all" | "blocked" | "needs_work" | "ready";
type FindingFilter = "open" | "all" | "closed";

function bandMatches(band: ReadinessBand | string, filter: BandFilter): boolean {
  if (filter === "all") return true;
  if (filter === "ready") return band === "ready" || band === "ready_with_nits";
  return band === filter;
}

function scoreStrokeClass(band: ReadinessBand | string): string {
  switch (band) {
    case "ready":
      return "stroke-success";
    case "ready_with_nits":
      return "stroke-primary";
    case "needs_work":
      return "stroke-warning";
    case "blocked":
      return "stroke-destructive";
    default:
      return "stroke-muted-foreground";
  }
}

function scoreTextClass(band: ReadinessBand | string): string {
  switch (band) {
    case "ready":
      return "text-success";
    case "ready_with_nits":
      return "text-primary";
    case "needs_work":
      return "text-warning";
    case "blocked":
      return "text-destructive";
    default:
      return "text-muted-foreground";
  }
}

function ScoreRing({ score, band, size = 76 }: { score: number; band: ReadinessBand | string; size?: number }) {
  const clamped = Math.max(0, Math.min(100, Math.round(score)));
  const strokeWidth = 6;
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const dashOffset = circumference - (clamped / 100) * circumference;

  return (
    <div className="relative inline-flex shrink-0 items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          strokeWidth={strokeWidth}
          className="stroke-muted"
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={dashOffset}
          className={cn("transition-all duration-300", scoreStrokeClass(band))}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className={cn("text-lg leading-none font-bold tabular-nums", scoreTextClass(band))}>{clamped}</span>
        <span className="text-muted-foreground text-[9px] font-medium">/100</span>
      </div>
    </div>
  );
}

function ChecklistGroup({
  title,
  items,
  tone,
}: {
  title: string;
  items: ReadinessItem[];
  tone: "blocker" | "should_fix" | "nice_to_have";
}) {
  if (!items.length) return null;
  const headingClass =
    tone === "blocker" ? "text-destructive" : tone === "should_fix" ? "text-warning" : "text-muted-foreground";
  return (
    <div className="grid gap-1.5">
      <div className={cn("flex items-center gap-1.5 text-xs font-semibold tracking-wide uppercase", headingClass)}>
        {tone === "blocker" ? (
          <AlertOctagon className="size-3.5" />
        ) : tone === "should_fix" ? (
          <AlertTriangle className="size-3.5" />
        ) : (
          <Circle className="size-3" />
        )}
        <span>
          {title} ({items.filter((i) => !i.done).length} open
          {items.some((i) => i.done) ? ` · ${items.filter((i) => i.done).length} done` : ""})
        </span>
      </div>
      <ul className="grid gap-1.5">
        {items.map((item, idx) => (
          <li
            key={`${item.code}-${idx}`}
            className={cn(
              "bg-muted/25 flex items-start gap-2.5 rounded-md border px-3 py-2 text-xs",
              item.done && "opacity-65",
            )}
          >
            {item.done ? (
              <CheckCircle2 className="text-success mt-0.5 size-3.5 shrink-0" />
            ) : tone === "blocker" ? (
              <XCircle className="text-destructive mt-0.5 size-3.5 shrink-0" />
            ) : tone === "should_fix" ? (
              <AlertTriangle className="text-warning mt-0.5 size-3.5 shrink-0" />
            ) : (
              <Circle className="text-muted-foreground mt-0.5 size-3.5 shrink-0" />
            )}
            <div className="min-w-0 flex-1">
              <div className={cn("leading-relaxed break-words", item.done && "line-through")}>{item.text}</div>
              <div className="text-muted-foreground mt-0.5 font-mono text-[10px]">{item.code}</div>
            </div>
            {item.url ? (
              <Button
                type="button"
                variant="ghost"
                size="xs"
                className="shrink-0"
                onClick={() => openUrl(item.url!)}
              >
                <ExternalLink /> Open
              </Button>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function PrInboxPage({ active }: { active: boolean }) {
  const setTab = useApp((s) => s.setTab);
  const backend = useApp((s) => s.backendUrl);
  const vaultLoaded = useApp((s) => s.vaultLoaded);
  const setBlockedPrCount = useApp((s) => s.setBlockedPrCount);
  const agents = useApp((s) => s.agents);

  const [items, setItems] = React.useState<InboxPR[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [err, setErr] = React.useState("");

  const [repoFilter, setRepoFilter] = React.useState("__all__");
  const [bandFilter, setBandFilter] = React.useState<BandFilter>("all");
  const [stateFilter, setStateFilter] = React.useState<"open" | "all">("open");
  const [search, setSearch] = React.useState("");

  const [selectedKey, setSelectedKey] = React.useState<string>("");
  const [detail, setDetail] = React.useState<PullReadinessResponse | null>(null);
  const [detailTargetKey, setDetailTargetKey] = React.useState<string>("");
  const [detailLoading, setDetailLoading] = React.useState(false);
  const [detailErr, setDetailErr] = React.useState("");
  const [findingFilter, setFindingFilter] = React.useState<FindingFilter>("open");

  // Dismiss (teach agent) dialog
  const [dismissTarget, setDismissTarget] = React.useState<StoredFinding | null>(null);
  const [dismissReason, setDismissReason] = React.useState("");

  // Teach agent quick dialog
  const [teachOpen, setTeachOpen] = React.useState(false);
  const [teachText, setTeachText] = React.useState("");
  const [teachKind, setTeachKind] = React.useState<MemoryKind>("learning");
  const [teachGlobs, setTeachGlobs] = React.useState("**");
  const [teachBlocking, setTeachBlocking] = React.useState(false);

  const prKey = (p: Pick<InboxPR, "repo" | "pr_number">) => `${p.repo}#${p.pr_number}`;
  const prTargetKey = (b: string, p: Pick<InboxPR, "repo" | "pr_number">) => `${b}::${p.repo}#${p.pr_number}`;

  const currentBackendRef = React.useRef(backend);
  currentBackendRef.current = backend;
  const currentFiltersRef = React.useRef({ repoFilter, stateFilter });
  currentFiltersRef.current = { repoFilter, stateFilter };

  const listSeq = React.useRef(0);
  const detailSeq = React.useRef(0);

  // Clear inbox list and detail state whenever backend changes.
  React.useEffect(() => {
    listSeq.current++;
    detailSeq.current++;
    setItems([]);
    setSelectedKey("");
    setErr("");
    setDetail(null);
    setDetailTargetKey("");
    setDetailErr("");
    setDetailLoading(false);
    setDismissTarget(null);
    setDismissReason("");
    setTeachOpen(false);
  }, [backend]);

  const loadInbox = React.useCallback(
    async (expectedBackend?: string) => {
      const reqBackend = expectedBackend ?? useApp.getState().backendUrl;
      if (reqBackend !== currentBackendRef.current) return;
      const reqRepoFilter = currentFiltersRef.current.repoFilter;
      const reqStateFilter = currentFiltersRef.current.stateFilter;
      const mine = ++listSeq.current;
      const isCurrentSelection = () =>
        mine === listSeq.current &&
        currentBackendRef.current === reqBackend &&
        currentFiltersRef.current.repoFilter === reqRepoFilter &&
        currentFiltersRef.current.stateFilter === reqStateFilter;

      setLoading(true);
      setErr("");
      try {
        const res = await listPrInbox({
          repo: reqRepoFilter !== "__all__" ? reqRepoFilter : undefined,
          state: reqStateFilter,
        });
        if (!isCurrentSelection()) return;
        const sorted = [...res.items].sort((a, b) => {
          if (a.score !== b.score) return a.score - b.score;
          return (Date.parse(b.updated_at || "") || 0) - (Date.parse(a.updated_at || "") || 0);
        });
        setItems(sorted);
        if (reqRepoFilter === "__all__" && reqStateFilter === "open") {
          setBlockedPrCount(sorted.filter((p) => p.band === "blocked").length);
        }
        setSelectedKey((prev) => {
          if (prev && sorted.some((p) => prKey(p) === prev)) return prev;
          return sorted.length ? prKey(sorted[0]) : "";
        });
      } catch (e) {
        if (!isCurrentSelection()) return;
        setErr(errText(e, 260));
      } finally {
        if (isCurrentSelection()) setLoading(false);
      }
    },
    [setBlockedPrCount],
  );

  React.useEffect(() => {
    if (active && vaultLoaded) void loadInbox(backend);
  }, [active, vaultLoaded, backend, repoFilter, stateFilter, loadInbox]);

  const filteredItems = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    return items.filter((p) => {
      if (!bandMatches(p.band, bandFilter)) return false;
      if (!q) return true;
      return (
        p.title.toLowerCase().includes(q) ||
        p.repo.toLowerCase().includes(q) ||
        p.author.toLowerCase().includes(q) ||
        `#${p.pr_number}`.includes(q)
      );
    });
  }, [items, bandFilter, search]);

  React.useEffect(() => {
    if (!filteredItems.length) {
      if (selectedKey) setSelectedKey("");
      return;
    }
    if (!filteredItems.some((p) => prKey(p) === selectedKey)) {
      setSelectedKey(prKey(filteredItems[0]));
    }
  }, [filteredItems, selectedKey]);

  const selectedPr = React.useMemo(
    () => filteredItems.find((p) => prKey(p) === selectedKey) || null,
    [filteredItems, selectedKey],
  );

  const currentTargetKey = selectedPr ? prTargetKey(backend, selectedPr) : "";
  const currentTargetRef = React.useRef(currentTargetKey);
  currentTargetRef.current = currentTargetKey;

  // Clear detail state and open PR-scoped dialogs whenever the selected target changes.
  React.useEffect(() => {
    detailSeq.current++;
    setDetail(null);
    setDetailTargetKey("");
    setDetailErr("");
    setDetailLoading(false);
    setDismissTarget(null);
    setDismissReason("");
    setTeachOpen(false);
  }, [currentTargetKey]);

  const loadDetail = React.useCallback(async (pr: InboxPR | null, expectedBackend?: string) => {
    const reqBackend = expectedBackend ?? useApp.getState().backendUrl;
    const reqTargetKey = pr ? prTargetKey(reqBackend, pr) : "";
    if (!pr) {
      detailSeq.current++;
      setDetail(null);
      setDetailTargetKey("");
      setDetailErr("");
      setDetailLoading(false);
      return;
    }
    if (reqTargetKey !== currentTargetRef.current) return;
    const mine = ++detailSeq.current;
    setDetailLoading(true);
    setDetailErr("");
    try {
      const res = await getPullReadiness(pr.repo, pr.pr_number);
      if (mine !== detailSeq.current || currentTargetRef.current !== reqTargetKey) return;
      setDetail(res);
      setDetailTargetKey(reqTargetKey);
    } catch (e) {
      if (mine !== detailSeq.current || currentTargetRef.current !== reqTargetKey) return;
      setDetail(null);
      setDetailTargetKey("");
      setDetailErr(errText(e, 240));
    } finally {
      if (mine === detailSeq.current && currentTargetRef.current === reqTargetKey) setDetailLoading(false);
    }
  }, []);

  React.useEffect(() => {
    if (active && vaultLoaded) void loadDetail(selectedPr, backend);
  }, [active, vaultLoaded, selectedPr, backend, loadDetail]);

  const knownRepos = React.useMemo(() => {
    const s = new Set<string>();
    for (const p of items) s.add(p.repo);
    for (const a of agents) {
      if (Array.isArray(a.target_repos)) {
        for (const r of a.target_repos) s.add(r);
      }
    }
    return Array.from(s).sort();
  }, [items, agents]);

  // Summary counts from loaded items (before band filter so counts stay informative)
  const stats = React.useMemo(() => {
    let ready = 0;
    let needsWork = 0;
    let blocked = 0;
    for (const p of items) {
      if (p.band === "blocked") blocked++;
      else if (p.band === "needs_work") needsWork++;
      else ready++;
    }
    return { total: items.length, ready, needsWork, blocked };
  }, [items]);

  const matchedDetail = detail && detailTargetKey === currentTargetKey ? detail : null;

  const handleRereview = async () => {
    if (!selectedPr) return;
    const targetPr = selectedPr;
    const targetBackend = backend;
    const targetKey = prTargetKey(targetBackend, targetPr);
    try {
      const res = await triggerManualReview(targetPr.repo, targetPr.pr_number, { force_full: false });
      toastOk(
        "Re-review completed",
        `${targetPr.repo}#${targetPr.pr_number} · ${res.findings_count ?? 0} finding(s)`,
      );
      const refreshes: Promise<unknown>[] = [];
      if (currentBackendRef.current === targetBackend) refreshes.push(loadInbox(targetBackend));
      if (currentTargetRef.current === targetKey) refreshes.push(loadDetail(targetPr, targetBackend));
      await Promise.all(refreshes);
    } catch (e) {
      toastErr("Re-review failed", errText(e, 260));
    }
  };

  const handleRecompute = async () => {
    if (!selectedPr) return;
    const targetPr = selectedPr;
    const targetBackend = backend;
    const targetKey = prTargetKey(targetBackend, targetPr);
    try {
      const res = await recomputePullReadiness(targetPr.repo, targetPr.pr_number);
      toastOk("Readiness recomputed", `Score ${res.report.score}/100 · ${bandLabel(res.report.band)}`);
      const refreshes: Promise<unknown>[] = [];
      if (currentBackendRef.current === targetBackend) refreshes.push(loadInbox(targetBackend));
      if (currentTargetRef.current === targetKey) refreshes.push(loadDetail(targetPr, targetBackend));
      await Promise.all(refreshes);
    } catch (e) {
      toastErr("Recompute failed", errText(e, 260));
    }
  };

  const handleFindingState = async (
    finding: StoredFinding,
    state: "dismissed" | "resolved" | "open",
    reason?: string,
  ) => {
    if (!selectedPr || !matchedDetail) return;
    const targetPr = selectedPr;
    const targetBackend = backend;
    const targetKey = prTargetKey(targetBackend, targetPr);
    if (detailTargetKey !== targetKey) return;
    try {
      const res = await updateFindingState(targetPr.repo, targetPr.pr_number, finding.fingerprint, {
        state,
        reason: reason?.trim() || undefined,
      });
      if (state === "dismissed") {
        toastOk(
          "Finding dismissed",
          res.memory_created
            ? `Agent learned dismissal pattern #${res.memory_created.id.slice(0, 8)} — future reviews will suppress it.`
            : "Finding marked dismissed.",
        );
      } else if (state === "resolved") {
        toastOk("Finding resolved", finding.title);
      } else {
        toastOk("Finding reopened", finding.title);
      }
      if (currentTargetRef.current === targetKey) {
        setDismissTarget(null);
        setDismissReason("");
      }
      const refreshes: Promise<unknown>[] = [];
      if (currentBackendRef.current === targetBackend) refreshes.push(loadInbox(targetBackend));
      if (currentTargetRef.current === targetKey) refreshes.push(loadDetail(targetPr, targetBackend));
      await Promise.all(refreshes);
    } catch (e) {
      toastErr("Update failed", errText(e, 240));
    }
  };

  const submitTeachAgent = async () => {
    if (!selectedPr) return;
    const targetPr = selectedPr;
    const targetKey = prTargetKey(backend, targetPr);
    const text = teachText.trim();
    if (!text) return toastErr("Teach agent", "Enter a rule or convention first.");
    const globs = teachGlobs
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    try {
      const created = await createRepoMemory(targetPr.repo, {
        kind: teachKind,
        text,
        scope_globs: globs.length ? globs : ["**"],
        blocking: teachBlocking,
        status: "active",
      });
      toastOk("Agent learned new rule", `${targetPr.repo} · #${created.id.slice(0, 8)}`);
      if (currentTargetRef.current === targetKey) {
        setTeachText("");
        setTeachGlobs("**");
        setTeachBlocking(false);
        setTeachOpen(false);
      }
    } catch (e) {
      toastErr("Could not save learning", errText(e, 240));
    }
  };

  const activeReport = matchedDetail?.report ?? null;
  const reportMatchesHead = Boolean(
    activeReport && selectedPr?.head_sha && activeReport.head_sha === selectedPr.head_sha,
  );
  const currentHeadReport = reportMatchesHead ? activeReport : null;
  const activeScore = currentHeadReport?.score ?? selectedPr?.score ?? 0;
  const activeBand = currentHeadReport?.band ?? selectedPr?.band ?? "needs_work";
  const allFindings = matchedDetail?.findings || [];
  const filteredFindings = React.useMemo(() => {
    if (findingFilter === "all") return allFindings;
    if (findingFilter === "open") return allFindings.filter((f) => f.state === "open");
    return allFindings.filter((f) => f.state !== "open");
  }, [allFindings, findingFilter]);

  const checklistBlockers = (currentHeadReport?.items || []).filter((i) => i.kind === "blocker");
  const checklistShouldFix = (currentHeadReport?.items || []).filter((i) => i.kind === "should_fix");
  const checklistNiceToHave = (currentHeadReport?.items || []).filter((i) => i.kind === "nice_to_have");

  return (
    <Page active={active} className="max-w-7xl">
      {/* Summary Stats Strip */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div className="bg-card flex items-center justify-between rounded-xl border px-4 py-3">
          <div>
            <div className="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
              {stateFilter === "open" ? "Total Open PRs" : "Total PRs"}
            </div>
            <div className="mt-0.5 text-2xl font-bold tabular-nums">{stats.total}</div>
          </div>
          <GitPullRequest className="text-muted-foreground size-5" />
        </div>
        <div className="bg-card flex items-center justify-between rounded-xl border px-4 py-3">
          <div>
            <div className="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">Ready</div>
            <div className="text-success mt-0.5 text-2xl font-bold tabular-nums">{stats.ready}</div>
          </div>
          <CheckCircle2 className="text-success size-5" />
        </div>
        <div className="bg-card flex items-center justify-between rounded-xl border px-4 py-3">
          <div>
            <div className="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">Needs Work</div>
            <div className="text-warning mt-0.5 text-2xl font-bold tabular-nums">{stats.needsWork}</div>
          </div>
          <AlertTriangle className="text-warning size-5" />
        </div>
        <div className="bg-card flex items-center justify-between rounded-xl border px-4 py-3">
          <div>
            <div className="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">Blocked</div>
            <div className="text-destructive mt-0.5 text-2xl font-bold tabular-nums">{stats.blocked}</div>
          </div>
          <AlertOctagon className="text-destructive size-5" />
        </div>
      </div>

      {/* Filter Bar */}
      <div className="bg-card flex flex-wrap items-center gap-2.5 rounded-xl border p-3">
        <div className="relative min-w-48 flex-1">
          <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Filter by title, repo, author, or #PR…"
            className="h-8 pl-8 text-xs"
          />
        </div>

        <Select value={repoFilter} onValueChange={setRepoFilter}>
          <SelectTrigger className="h-8 w-48 text-xs">
            <SelectValue placeholder="All repositories" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">All repositories</SelectItem>
            {knownRepos.map((r) => (
              <SelectItem key={r} value={r}>
                {r}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <div className="bg-muted flex items-center rounded-md p-0.5">
          {(
            [
              { id: "all", label: "All" },
              { id: "blocked", label: "Blocked" },
              { id: "needs_work", label: "Needs work" },
              { id: "ready", label: "Ready" },
            ] as const
          ).map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() => setBandFilter(f.id)}
              className={cn(
                "cursor-pointer rounded px-2.5 py-1 text-xs font-medium transition-colors",
                bandFilter === f.id
                  ? "bg-background text-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {f.label}
            </button>
          ))}
        </div>

        <Select value={stateFilter} onValueChange={(v) => setStateFilter(v as "open" | "all")}>
          <SelectTrigger className="h-8 w-28 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="open">Open PRs</SelectItem>
            <SelectItem value="all">All states</SelectItem>
          </SelectContent>
        </Select>

        <AsyncButton variant="outline" size="sm" onClick={() => loadInbox(backend)} disabled={loading}>
          <RefreshCw className={loading ? "animate-spin" : ""} /> Refresh
        </AsyncButton>
      </div>

      {err ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load PR Inbox</AlertTitle>
          <AlertDescription>{err}</AlertDescription>
        </Alert>
      ) : null}

      {/* Master-Detail Split */}
      {loading && items.length === 0 ? (
        <div className="grid gap-4 xl:grid-cols-[380px_1fr]">
          <Skeleton className="h-72 rounded-xl" />
          <Skeleton className="h-72 rounded-xl" />
        </div>
      ) : items.length === 0 ? (
        <Section
          title="No tracked pull requests yet"
          description="PRs appear here automatically as soon as your Cloud PR Agent reviews a PR or receives GitHub App webhook events."
        >
          <div className="flex flex-col items-center gap-3 py-8 text-center">
            <Inbox className="text-muted-foreground/60 size-10" />
            <p className="text-muted-foreground max-w-md text-xs leading-relaxed">
              Finish Cloud Setup to connect your GitHub App and run your first live PR review — every open PR will show its
              Merge Readiness score, blockers checklist, and inline findings right here.
            </p>
            <ButtonRow>
              <Button type="button" onClick={() => setTab("setup")}>
                <Sparkles /> Open Cloud Setup
              </Button>
              <AsyncButton variant="outline" onClick={() => loadInbox(backend)}>
                <RefreshCw /> Check again
              </AsyncButton>
            </ButtonRow>
          </div>
        </Section>
      ) : (
        <div className="grid items-start gap-4 xl:grid-cols-[minmax(320px,400px)_1fr]">
          {/* Left: PR List */}
          <div className="bg-card overflow-hidden rounded-xl border">
            <div className="flex items-center justify-between border-b px-4 py-2.5 text-xs font-medium">
              <span>Pull Requests ({filteredItems.length})</span>
              <span className="text-muted-foreground">Sorted by lowest score</span>
            </div>
            <ScrollArea className="max-h-[calc(100vh-20rem)]">
              <div className="grid divide-y">
                {filteredItems.length === 0 ? (
                  <div className="p-6 text-center">
                    <Hint className="text-xs">No pull requests match the current filters.</Hint>
                  </div>
                ) : (
                  filteredItems.map((pr) => {
                    const key = prKey(pr);
                    const isSelected = key === selectedKey;
                    const fc = pr.findings_counts || { critical: 0, major: 0, minor: 0, nit: 0 };
                    return (
                      <button
                        key={key}
                        type="button"
                        onClick={() => setSelectedKey(key)}
                        className={cn(
                          "hover:bg-muted/40 grid w-full cursor-pointer gap-2 p-3.5 text-left transition-colors",
                          isSelected && "bg-primary/8 border-l-primary border-l-2",
                        )}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0 flex-1">
                            <div className="text-muted-foreground flex flex-wrap items-center gap-1.5 font-mono text-[11px]">
                              <span className="truncate">{pr.repo}</span>
                              <span>#{pr.pr_number}</span>
                              <span>·</span>
                              <span>{relativeTime(pr.updated_at)}</span>
                            </div>
                            <div className="mt-0.5 line-clamp-2 text-sm font-medium leading-snug">{pr.title}</div>
                          </div>
                          <div className="flex shrink-0 flex-col items-end gap-1">
                            <span className={cn("text-base font-bold tabular-nums", scoreTextClass(pr.band))}>
                              {pr.score}
                            </span>
                            <Badge variant={bandBadgeVariant(pr.band)} className="text-[10px]">
                              {bandLabel(pr.band)}
                            </Badge>
                          </div>
                        </div>

                        <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                          <span className="text-muted-foreground">@{pr.author || "unknown"}</span>
                          {pr.state && pr.state !== "open" ? (
                            <Badge variant="outline" className="text-[10px]">
                              {pr.state}
                            </Badge>
                          ) : null}
                          {fc.critical > 0 ? <Badge variant="danger" className="text-[10px]">{fc.critical} crit</Badge> : null}
                          {fc.major > 0 ? <Badge variant="warning" className="text-[10px]">{fc.major} major</Badge> : null}
                          {fc.minor > 0 ? <Badge variant="secondary" className="text-[10px]">{fc.minor} minor</Badge> : null}
                          {fc.nit > 0 ? <Badge variant="outline" className="text-[10px]">{fc.nit} nit</Badge> : null}
                        </div>

                        {pr.top_blockers?.length ? (
                          <div className="text-destructive/90 truncate text-[11px]">
                            • {pr.top_blockers[0]}
                          </div>
                        ) : null}
                      </button>
                    );
                  })
                )}
              </div>
            </ScrollArea>
          </div>

          {/* Right: Detail View */}
          {selectedPr ? (
            <div className="grid gap-4">
              {/* PR Header + Action Bar + Score Ring */}
              <Section
                title={
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-muted-foreground">
                      {selectedPr.repo}#{selectedPr.pr_number}
                    </span>
                    <Badge variant={bandBadgeVariant(activeBand)}>{bandLabel(activeBand)}</Badge>
                    {selectedPr.state && selectedPr.state !== "open" ? (
                      <Badge variant="secondary" className="text-[10px]">
                        {selectedPr.state}
                      </Badge>
                    ) : null}
                    {selectedPr.merge_gate ? (
                      <Badge variant="outline" className="text-[10px]">
                        gate: {selectedPr.merge_gate}
                      </Badge>
                    ) : null}
                    {matchedDetail?.activity_stale ? (
                      <Badge variant="warning" className="text-[10px]">
                        stale — recompute
                      </Badge>
                    ) : null}
                  </div>
                }
                description={
                  <div className="mt-1 grid gap-1">
                    <div className="text-foreground text-base font-semibold">{selectedPr.title}</div>
                    <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
                      <span>by @{selectedPr.author || "unknown"}</span>
                      {selectedPr.head_sha ? (
                        <span>
                          head <code>{selectedPr.head_sha.slice(0, 7)}</code>
                        </span>
                      ) : null}
                      <span>updated {relativeTime(activeReport?.computed_at || selectedPr.updated_at)}</span>
                    </div>
                  </div>
                }
              >
                {/* Top Action Bar */}
                <ButtonRow>
                  <AsyncButton size="sm" onClick={handleRereview}>
                    <Play /> Re-review PR
                  </AsyncButton>
                  <AsyncButton variant="outline" size="sm" onClick={handleRecompute}>
                    <RefreshCw /> Recompute Score
                  </AsyncButton>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setTeachText("");
                      setTeachGlobs("**");
                      setTeachKind("learning");
                      setTeachBlocking(false);
                      setTeachOpen(true);
                    }}
                  >
                    <Brain /> Teach Agent
                  </Button>
                  {selectedPr.url ? (
                    <Button type="button" variant="outline" size="sm" onClick={() => openUrl(selectedPr.url)}>
                      <ExternalLink /> Open on GitHub
                    </Button>
                  ) : null}
                </ButtonRow>

                <Separator />

                {/* Readiness Score Visual */}
                <div className="bg-muted/25 flex flex-wrap items-center gap-5 rounded-lg border p-4">
                  <ScoreRing score={activeScore} band={activeBand} />
                  <div className="grid min-w-52 flex-1 gap-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-sm font-semibold">Merge Readiness Score</span>
                      <span className="text-muted-foreground text-xs tabular-nums">
                        Threshold: {selectedPr.score_threshold ?? 85}/100
                      </span>
                    </div>
                    <Progress value={activeScore} aria-label="Merge readiness score" />
                    <div className="text-muted-foreground flex flex-wrap gap-x-4 gap-y-1 text-xs">
                      <span>
                        Blockers: <b className="text-foreground">{checklistBlockers.filter((i) => !i.done).length}</b>
                      </span>
                      <span>
                        Should fix: <b className="text-foreground">{checklistShouldFix.filter((i) => !i.done).length}</b>
                      </span>
                      <span>
                        Open findings:{" "}
                        <b className="text-foreground">{allFindings.filter((f) => f.state === "open").length}</b>
                      </span>
                    </div>
                  </div>
                </div>

                {detailErr ? (
                  <Alert variant="destructive">
                    <AlertTitle>Failed to load PR readiness details</AlertTitle>
                    <AlertDescription>{detailErr}</AlertDescription>
                  </Alert>
                ) : null}

                {/* Before you merge checklist */}
                <div className="grid gap-3">
                  <div className="text-sm font-semibold">Before you merge</div>
                  {detailLoading && !matchedDetail ? (
                    <Skeleton className="h-24 rounded-md" />
                  ) : !matchedDetail || !currentHeadReport || detailErr ? (
                    <div className="bg-muted/30 text-muted-foreground flex items-center gap-2 rounded-md border border-dashed px-3 py-2.5 text-xs">
                      <AlertTriangle className="size-4 shrink-0" />
                      <span>
                        {detailErr
                          ? "Readiness checklist unavailable — failed to load report from the backend."
                          : "Readiness report not computed yet for this PR head. Click Recompute Score or Re-review PR."}
                      </span>
                    </div>
                  ) : currentHeadReport.items?.length ? (
                    <div className="grid gap-3">
                      <ChecklistGroup title="Blockers" items={checklistBlockers} tone="blocker" />
                      <ChecklistGroup title="Should fix" items={checklistShouldFix} tone="should_fix" />
                      <ChecklistGroup title="Nice to have" items={checklistNiceToHave} tone="nice_to_have" />
                    </div>
                  ) : (
                    <div className="bg-success/10 border-success/30 text-success flex items-center gap-2 rounded-md border px-3 py-2.5 text-xs">
                      <CheckCircle2 className="size-4 shrink-0" />
                      <span>All clear — no blocking or recommended items remaining for this PR head.</span>
                    </div>
                  )}
                </div>
              </Section>

              {/* Inline Findings */}
              <Section
                title="Structured Inline Findings"
                description="Review findings persisted for this PR. Dismissing a finding teaches your repo memory to suppress that false positive in future PRs."
                action={
                  <div className="bg-muted flex items-center rounded-md p-0.5 text-xs">
                    {(
                      [
                        { id: "open", label: `Open (${allFindings.filter((f) => f.state === "open").length})` },
                        { id: "all", label: `All (${allFindings.length})` },
                        {
                          id: "closed",
                          label: `Closed (${allFindings.filter((f) => f.state !== "open").length})`,
                        },
                      ] as const
                    ).map((tab) => (
                      <button
                        key={tab.id}
                        type="button"
                        onClick={() => setFindingFilter(tab.id)}
                        className={cn(
                          "cursor-pointer rounded px-2 py-1 font-medium transition-colors",
                          findingFilter === tab.id
                            ? "bg-background text-foreground shadow-xs"
                            : "text-muted-foreground hover:text-foreground",
                        )}
                      >
                        {tab.label}
                      </button>
                    ))}
                  </div>
                }
              >
                {detailLoading && !matchedDetail ? (
                  <Skeleton className="h-32 rounded-md" />
                ) : !matchedDetail ? (
                  <Hint className="py-4 text-center text-xs">
                    {detailErr ? "Findings unavailable — failed to load PR readiness details." : "No findings loaded for this PR yet."}
                  </Hint>
                ) : filteredFindings.length === 0 ? (
                  <Hint className="py-4 text-center text-xs">
                    {findingFilter === "open"
                      ? "No open inline findings on this PR."
                      : "No findings in this filter."}
                  </Hint>
                ) : (
                  <div className="grid gap-3">
                    {filteredFindings.map((f) => {
                      const loc =
                        f.end_line && f.end_line !== f.start_line
                          ? `${f.file}:${f.start_line}-${f.end_line}`
                          : `${f.file}:${f.start_line}`;
                      return (
                        <div
                          key={f.fingerprint}
                          className={cn(
                            "bg-muted/20 grid gap-2.5 rounded-lg border p-3.5",
                            f.state !== "open" && "opacity-70",
                          )}
                        >
                          <div className="flex flex-wrap items-start justify-between gap-2">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <Badge variant={severityBadgeVariant(f.severity)}>{f.severity}</Badge>
                              <Badge variant="outline">{f.category}</Badge>
                              {f.state !== "open" ? <Badge variant="secondary">{f.state}</Badge> : null}
                              <span className="text-sm font-semibold">{f.title}</span>
                            </div>
                            <div className="flex items-center gap-1.5">
                              {f.url ? (
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="xs"
                                  onClick={() => openUrl(f.url!)}
                                  className="font-mono text-[11px]"
                                >
                                  <FileCode2 /> {loc} <ExternalLink className="size-3" />
                                </Button>
                              ) : (
                                <code className="text-[11px]">{loc}</code>
                              )}
                            </div>
                          </div>

                          {f.body ? (
                            <p className="text-muted-foreground text-xs leading-relaxed whitespace-pre-wrap">{f.body}</p>
                          ) : null}

                          {f.suggestion_patch ? (
                            <div className="bg-background/80 grid gap-1.5 rounded-md border p-2.5">
                              <div className="flex items-center justify-between text-[11px]">
                                <span className="text-success font-medium">Committable suggestion</span>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="xs"
                                  onClick={async () => {
                                    if (await copyToClipboard(f.suggestion_patch || "")) {
                                      toastOk("Copied suggestion patch");
                                    }
                                  }}
                                >
                                  <ClipboardCopy /> Copy
                                </Button>
                              </div>
                              <pre className="overflow-x-auto font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
                                {f.suggestion_patch}
                              </pre>
                            </div>
                          ) : null}

                          <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                            <div className="flex flex-wrap items-center gap-1.5">
                              {f.learning_ids?.length ? (
                                f.learning_ids.map((lid) => (
                                  <Badge key={lid} variant="secondary" className="font-mono text-[10px]">
                                    <BookMarked className="size-3" /> learning #{lid.slice(0, 8)}
                                  </Badge>
                                ))
                              ) : null}
                              {typeof f.confidence === "number" ? (
                                <span className="text-muted-foreground text-[11px]">
                                  confidence {Math.round(f.confidence * 100)}%
                                </span>
                              ) : null}
                            </div>

                            {matchedDetail ? (
                              <ButtonRow>
                                {f.state === "open" ? (
                                  <>
                                    <Button
                                      type="button"
                                      variant="outline"
                                      size="xs"
                                      onClick={() => {
                                        setDismissTarget(f);
                                        setDismissReason("");
                                      }}
                                    >
                                      <ThumbsDown /> Dismiss (teach agent)
                                    </Button>
                                    <AsyncButton
                                      variant="secondary"
                                      size="xs"
                                      onClick={() => handleFindingState(f, "resolved")}
                                    >
                                      <Check /> Resolve
                                    </AsyncButton>
                                  </>
                                ) : (
                                  <AsyncButton
                                    variant="ghost"
                                    size="xs"
                                    onClick={() => handleFindingState(f, "open")}
                                  >
                                    <RotateCcw /> Reopen
                                  </AsyncButton>
                                )}
                              </ButtonRow>
                            ) : null}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </Section>
            </div>
          ) : (
            <Section title="Select a pull request">
              <Hint className="py-8 text-center text-xs">
                Pick a pull request on the left to inspect its Merge Readiness checklist and inline findings.
              </Hint>
            </Section>
          )}
        </div>
      )}

      {/* Dismiss (teach agent) Dialog */}
      <Dialog open={dismissTarget !== null} onOpenChange={(o) => !o && setDismissTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Dismiss finding &amp; teach agent</DialogTitle>
            <DialogDescription>
              {dismissTarget ? `${dismissTarget.title} (${dismissTarget.file}:${dismissTarget.start_line})` : ""}
            </DialogDescription>
          </DialogHeader>
          <Field
            label="Why should PR Agent ignore this pattern? (optional)"
            htmlFor="inbox-dismiss-reason"
            hint="Providing a reason immediately saves a repo dismissal pattern so future reviews suppress it."
          >
            <Textarea
              id="inbox-dismiss-reason"
              rows={3}
              value={dismissReason}
              onChange={(e) => setDismissReason(e.target.value)}
              placeholder="e.g. Intentional retry pattern in this worker module; handled by upstream circuit breaker."
            />
          </Field>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setDismissTarget(null)}>
              Cancel
            </Button>
            <AsyncButton
              onClick={() =>
                dismissTarget ? handleFindingState(dismissTarget, "dismissed", dismissReason) : Promise.resolve()
              }
            >
              <ThumbsDown /> Dismiss finding
            </AsyncButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Teach Agent Dialog */}
      <Dialog open={teachOpen} onOpenChange={setTeachOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Teach PR Agent ({selectedPr?.repo})</DialogTitle>
            <DialogDescription>
              Add a repo-specific learning injected into every future review touching matching files.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <Field label="Rule or convention" htmlFor="inbox-teach-text">
              <Textarea
                id="inbox-teach-text"
                rows={3}
                value={teachText}
                onChange={(e) => setTeachText(e.target.value)}
                placeholder="e.g. Always use Result<T, AppError> instead of unwrap() in src-tauri/src/**"
              />
            </Field>
            <FieldGrid>
              <Field label="Kind" htmlFor="inbox-teach-kind">
                <Select value={teachKind} onValueChange={(v) => setTeachKind(v as MemoryKind)}>
                  <SelectTrigger id="inbox-teach-kind" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="learning">learning (rule)</SelectItem>
                    <SelectItem value="repo_fact">repo_fact (architecture/context)</SelectItem>
                    <SelectItem value="dismissed_pattern">dismissed_pattern (ignore)</SelectItem>
                    <SelectItem value="incident">incident (recurring bug)</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              <Field label="Scope globs (comma-separated)" htmlFor="inbox-teach-globs">
                <Input
                  id="inbox-teach-globs"
                  value={teachGlobs}
                  onChange={(e) => setTeachGlobs(e.target.value)}
                  placeholder="**, src/**/*.ts"
                  className="font-mono text-xs"
                />
              </Field>
            </FieldGrid>
            <div className="flex items-center justify-between rounded-md border px-3 py-2">
              <div className="grid gap-0.5">
                <Label htmlFor="inbox-teach-blocking" className="cursor-pointer text-xs font-medium">
                  Blocking rule (merge gate)
                </Label>
                <span className="text-muted-foreground text-[11px]">
                  Violating a blocking rule marks the PR as Blocked in Merge Readiness.
                </span>
              </div>
              <Switch id="inbox-teach-blocking" checked={teachBlocking} onCheckedChange={setTeachBlocking} />
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setTeachOpen(false)}>
              Cancel
            </Button>
            <AsyncButton onClick={submitTeachAgent}>
              <Brain /> Save learning
            </AsyncButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Page>
  );
}
