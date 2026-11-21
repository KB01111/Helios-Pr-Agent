import { apiGet, apiMut } from "./api";
import type { MergeGateMode, PathInstruction, ReviewProfile } from "./store";

/* ------------------------------------------------------------------ */
/* Types matching backend/datamodel.py and REST contracts              */
/* ------------------------------------------------------------------ */

export type MemoryKind = "learning" | "dismissed_pattern" | "repo_fact" | "incident";
export type MemorySource = "chat" | "dismissal" | "manual" | "merged_outcome" | "bootstrap";
export type MemoryStatus = "active" | "pending" | "archived";

export interface MemoryEntry {
  id: string;
  kind: MemoryKind;
  text: string;
  scope_globs: string[];
  source: MemorySource;
  author?: string | null;
  confidence: number;
  blocking: boolean;
  status: MemoryStatus;
  hits: number;
  created_at: string;
  last_used?: string | null;
}

export interface RepoProfile {
  languages?: string[];
  frameworks?: string[];
  test_commands?: string[];
  build_commands?: string[];
  lint_commands?: string[];
  files_indexed?: string[];
  docs_indexed?: string[];
  bootstrapped_at?: string;
  updated_at?: string;
  [k: string]: unknown;
}

export interface MemoryListResponse {
  repo: string;
  items: MemoryEntry[];
  profile?: RepoProfile | null;
}

export interface MemoryCreatePayload {
  kind: MemoryKind;
  text: string;
  scope_globs: string[];
  blocking: boolean;
  status?: MemoryStatus;
}

export interface MemoryUpdatePayload {
  text?: string;
  scope_globs?: string[];
  blocking?: boolean;
  status?: MemoryStatus;
}

export interface MemoryBootstrapResponse {
  profile?: RepoProfile | null;
  created: MemoryEntry[];
  skipped: number;
}

export interface MemoryImportResponse {
  imported: number;
  skipped: number;
}

export interface MemoryMarkdownResponse {
  repo: string;
  markdown: string;
}

export type ReadinessBand = "ready" | "ready_with_nits" | "needs_work" | "blocked";
export type ReadinessItemKind = "blocker" | "should_fix" | "nice_to_have";

export interface ReadinessItem {
  kind: ReadinessItemKind;
  code: string;
  text: string;
  url?: string | null;
  done: boolean;
}

export interface ReadinessReport {
  repo: string;
  pr_number: number;
  head_sha: string;
  score: number;
  band: ReadinessBand;
  items: ReadinessItem[];
  policy_version?: string;
  computed_at?: string;
}

export type FindingSeverity = "critical" | "major" | "minor" | "nit";
export type FindingCategory = "bug" | "security" | "perf" | "maintainability" | "style" | "test" | "docs";
export type FindingState = "open" | "resolved" | "dismissed" | "outdated";

export interface StoredFinding {
  fingerprint: string;
  file: string;
  start_line: number;
  end_line: number;
  side?: "LEFT" | "RIGHT";
  severity: FindingSeverity;
  category: FindingCategory;
  title: string;
  body?: string;
  suggestion_patch?: string | null;
  confidence?: number;
  source?: "llm" | "tool" | "memory";
  state: FindingState;
  learning_ids?: string[];
  comment_id?: number | null;
  url?: string | null;
}

export interface FindingsCounts {
  critical: number;
  major: number;
  minor: number;
  nit: number;
}

export interface InboxPR {
  repo: string;
  pr_number: number;
  head_sha: string;
  title: string;
  url: string;
  author: string;
  state: string;
  score: number;
  band: ReadinessBand;
  blockers_count: number;
  top_blockers: string[];
  findings_counts: FindingsCounts;
  updated_at: string;
  merge_gate: MergeGateMode | string;
  score_threshold: number;
}

export interface PullReadinessResponse {
  report: ReadinessReport | null;
  findings: StoredFinding[];
  activity_stale?: boolean;
}

export interface RecomputeReadinessResponse {
  report: ReadinessReport;
  published?: Record<string, unknown>;
}

export interface UpdateFindingStateResponse {
  finding: StoredFinding;
  memory_created?: MemoryEntry | null;
}

export interface WalkthroughSummary {
  summary?: string;
  effort?: number;
  file_table?: { file: string; change_summary: string }[];
  sequence_diagram?: string | null;
}

export interface ManualReviewResponse {
  status: string;
  run_id?: string;
  review_posted?: boolean;
  pr_comment_posted?: boolean;
  findings_count?: number;
  readiness?: ReadinessReport | null;
  walkthrough?: WalkthroughSummary | null;
  summary?: string;
  logs?: string[] | string;
}

export interface RepoConfigResponse {
  repo: string;
  config: Record<string, unknown>;
  source: "repo" | "default";
}

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

export function isValidRepo(repo: string): boolean {
  const clean = repo.trim();
  if (!/^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/.test(clean)) return false;
  const idx = clean.indexOf("/");
  const owner = clean.slice(0, idx);
  const name = clean.slice(idx + 1);
  return owner !== "." && owner !== ".." && name !== "." && name !== "..";
}

export function splitRepo(repo: string): [string, string] {
  const clean = repo.trim();
  if (!isValidRepo(clean)) {
    throw new Error(`Invalid repository "${repo}" — expected owner/name`);
  }
  const idx = clean.indexOf("/");
  return [encodeURIComponent(clean.slice(0, idx)), encodeURIComponent(clean.slice(idx + 1))];
}

function repoBase(repo: string): string {
  const [owner, name] = splitRepo(repo);
  return `/repos/${owner}/${name}`;
}

/* ------------------------------------------------------------------ */
/* Memory API                                                          */
/* ------------------------------------------------------------------ */

export async function listRepoMemory(
  repo: string,
  opts?: { status?: string; kind?: string },
): Promise<MemoryListResponse> {
  const params = new URLSearchParams();
  if (opts?.status) params.set("status", opts.status);
  if (opts?.kind) params.set("kind", opts.kind);
  const qs = params.toString();
  const data = await apiGet<MemoryListResponse>(`${repoBase(repo)}/memory${qs ? `?${qs}` : ""}`);
  return {
    repo: data?.repo || repo,
    items: Array.isArray(data?.items) ? data.items : [],
    profile: data?.profile ?? null,
  };
}

export async function createRepoMemory(repo: string, payload: MemoryCreatePayload): Promise<MemoryEntry> {
  return apiMut<MemoryEntry>("POST", `${repoBase(repo)}/memory`, payload);
}

export async function updateRepoMemory(
  repo: string,
  memId: string,
  patch: MemoryUpdatePayload,
): Promise<MemoryEntry> {
  return apiMut<MemoryEntry>("PATCH", `${repoBase(repo)}/memory/${encodeURIComponent(memId)}`, patch);
}

export async function deleteRepoMemory(repo: string, memId: string): Promise<{ deleted: boolean }> {
  return apiMut<{ deleted: boolean }>("DELETE", `${repoBase(repo)}/memory/${encodeURIComponent(memId)}`);
}

export async function approveRepoMemory(repo: string, memId: string): Promise<MemoryEntry> {
  return apiMut<MemoryEntry>("POST", `${repoBase(repo)}/memory/${encodeURIComponent(memId)}/approve`, {});
}

export async function bootstrapRepoMemory(repo: string): Promise<MemoryBootstrapResponse> {
  const data = await apiMut<MemoryBootstrapResponse>(
    "POST",
    `${repoBase(repo)}/memory/bootstrap`,
    {},
    undefined,
    undefined,
    60000,
  );
  return {
    profile: data?.profile ?? null,
    created: Array.isArray(data?.created) ? data.created : [],
    skipped: Number(data?.skipped ?? 0),
  };
}

export async function importRepoMemory(
  repo: string,
  items: Partial<MemoryEntry>[],
): Promise<MemoryImportResponse> {
  return apiMut<MemoryImportResponse>("POST", `${repoBase(repo)}/memory/import`, { items });
}

export async function getRepoMemoryMarkdown(repo: string): Promise<MemoryMarkdownResponse> {
  const data = await apiGet<MemoryMarkdownResponse>(`${repoBase(repo)}/memory/markdown`);
  return {
    repo: data?.repo || repo,
    markdown: String(data?.markdown ?? ""),
  };
}

/* ------------------------------------------------------------------ */
/* PR Inbox / Readiness / Findings / Manual Review API                 */
/* ------------------------------------------------------------------ */

export async function listPrInbox(opts?: {
  repo?: string;
  state?: string;
}): Promise<{ items: InboxPR[] }> {
  const params = new URLSearchParams();
  if (opts?.repo) params.set("repo", opts.repo);
  if (opts?.state) params.set("state", opts.state);
  const qs = params.toString();
  const data = await apiGet<{ items?: InboxPR[] }>(`/prs/inbox${qs ? `?${qs}` : ""}`);
  return {
    items: Array.isArray(data?.items) ? data.items : Array.isArray(data) ? (data as InboxPR[]) : [],
  };
}

export async function getPullReadiness(repo: string, prNumber: number): Promise<PullReadinessResponse> {
  const data = await apiGet<PullReadinessResponse>(`${repoBase(repo)}/pulls/${prNumber}/readiness`);
  return {
    report: data?.report ?? null,
    findings: Array.isArray(data?.findings) ? data.findings : [],
    activity_stale: Boolean(data?.activity_stale),
  };
}

export async function recomputePullReadiness(repo: string, prNumber: number): Promise<RecomputeReadinessResponse> {
  return apiMut<RecomputeReadinessResponse>(
    "POST",
    `${repoBase(repo)}/pulls/${prNumber}/readiness/recompute`,
    {},
    undefined,
    undefined,
    45000,
  );
}

export async function updateFindingState(
  repo: string,
  prNumber: number,
  fingerprint: string,
  body: { state: "dismissed" | "resolved" | "open"; reason?: string },
): Promise<UpdateFindingStateResponse> {
  return apiMut<UpdateFindingStateResponse>(
    "POST",
    `${repoBase(repo)}/pulls/${prNumber}/findings/${encodeURIComponent(fingerprint)}/state`,
    body,
  );
}

export async function triggerManualReview(
  repo: string,
  prNumber: number,
  opts?: { force_full?: boolean },
): Promise<ManualReviewResponse> {
  return apiMut<ManualReviewResponse>(
    "POST",
    `${repoBase(repo)}/pulls/${prNumber}/review`,
    { force_full: Boolean(opts?.force_full) },
    undefined,
    undefined,
    120000,
  );
}

/* ------------------------------------------------------------------ */
/* Repo Config API & .pragent.yaml renderer                            */
/* ------------------------------------------------------------------ */

export async function getRepoConfig(repo: string): Promise<RepoConfigResponse> {
  return apiGet<RepoConfigResponse>(`${repoBase(repo)}/config`);
}

function yamlString(v: string): string {
  const escaped = v
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"')
    .replace(/\r?\n/g, "\\n");
  return `"${escaped}"`;
}

export interface PragentYamlInput {
  profile: ReviewProfile;
  merge_gate: MergeGateMode;
  score_threshold: number;
  memory_enabled: boolean;
  memory_auto_approve?: boolean;
  chat_enabled: boolean;
  drafts?: boolean;
  path_instructions?: PathInstruction[];
}

export function renderPragentYaml(cfg: PragentYamlInput): string {
  const lines: string[] = [
    "# PR Agent repository configuration (.pragent.yaml)",
    "# Loaded from the PR base branch by the PR Agent cloud backend.",
    `profile: ${cfg.profile}`,
    `merge_gate: ${cfg.merge_gate}`,
    `score_threshold: ${Math.max(0, Math.min(100, Math.round(cfg.score_threshold)))}`,
    "memory:",
    `  enabled: ${cfg.memory_enabled ? "true" : "false"}`,
    `  auto_approve: ${cfg.memory_auto_approve ? "true" : "false"}`,
    "chat:",
    `  enabled: ${cfg.chat_enabled ? "true" : "false"}`,
    "auto_review:",
    `  drafts: ${cfg.drafts ? "true" : "false"}`,
  ];

  const rules = (cfg.path_instructions || []).filter((r) => r.path.trim() && r.instructions.trim());
  if (rules.length > 0) {
    lines.push("path_instructions:");
    for (const r of rules) {
      lines.push(`  - path: ${yamlString(r.path.trim())}`);
      lines.push(`    instructions: ${yamlString(r.instructions.trim())}`);
    }
  } else {
    lines.push("path_instructions: []");
  }
  lines.push("");
  return lines.join("\n");
}

/* ------------------------------------------------------------------ */
/* Display helpers                                                     */
/* ------------------------------------------------------------------ */

export function bandLabel(band: ReadinessBand | string): string {
  switch (band) {
    case "ready":
      return "Ready";
    case "ready_with_nits":
      return "Ready with nits";
    case "needs_work":
      return "Needs work";
    case "blocked":
      return "Blocked";
    default:
      return String(band || "Unknown");
  }
}

export function bandBadgeVariant(band: ReadinessBand | string): "success" | "secondary" | "warning" | "danger" | "outline" {
  switch (band) {
    case "ready":
      return "success";
    case "ready_with_nits":
      return "secondary";
    case "needs_work":
      return "warning";
    case "blocked":
      return "danger";
    default:
      return "outline";
  }
}

export function severityBadgeVariant(sev: FindingSeverity | string): "danger" | "warning" | "secondary" | "outline" {
  switch (sev) {
    case "critical":
      return "danger";
    case "major":
      return "warning";
    case "minor":
      return "secondary";
    default:
      return "outline";
  }
}

export function relativeTime(iso?: string | null): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  const diffSec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (diffSec < 60) return "just now";
  const mins = Math.floor(diffSec / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(ms).toLocaleDateString();
}

export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
