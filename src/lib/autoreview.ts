import { create } from "zustand";
import { DEFAULT_MODEL, lsGet, lsSet } from "./store";

/* Auto-review settings: what the PR Agent workflow does in each repo.
 * Non-secret, so they persist in localStorage. Field names match the Rust
 * `WorkflowOptions` (snake_case) so the object is passed through unchanged. */

export interface WorkflowOptions {
  model: string;
  fallback_models: string[];
  max_tokens: number;
  reasoning_effort: string;
  auto_review: boolean;
  auto_describe: boolean;
  auto_improve: boolean;
  review_on_push: boolean;
  skip_drafts: boolean;
  extra_instructions: string;
}

export const DEFAULT_OPTIONS: WorkflowOptions = {
  model: DEFAULT_MODEL,
  fallback_models: ["openrouter/free"],
  max_tokens: 200000,
  reasoning_effort: "low",
  auto_review: true,
  auto_describe: true,
  auto_improve: false,
  review_on_push: true,
  skip_drafts: true,
  extra_instructions: "",
};

const LS_KEY = "pragent.autoreview.options";

function loadOptions(): WorkflowOptions {
  try {
    const raw = JSON.parse(lsGet(LS_KEY, "{}"));
    return normalizeOptions({ ...DEFAULT_OPTIONS, ...(raw && typeof raw === "object" ? raw : {}) });
  } catch {
    return { ...DEFAULT_OPTIONS };
  }
}

/** Same trimming the Rust side applies, so saved and deployed settings compare equal. */
export function normalizeOptions(o: Partial<WorkflowOptions>): WorkflowOptions {
  const merged = { ...DEFAULT_OPTIONS, ...o };
  const fallbacks: string[] = [];
  for (const m of Array.isArray(merged.fallback_models) ? merged.fallback_models : []) {
    const t = String(m).trim();
    if (t && !fallbacks.includes(t)) fallbacks.push(t);
  }
  return {
    model: String(merged.model || "").trim() || DEFAULT_MODEL,
    fallback_models: fallbacks,
    max_tokens: Number(merged.max_tokens) || DEFAULT_OPTIONS.max_tokens,
    reasoning_effort: String(merged.reasoning_effort || "low").trim().toLowerCase() || "low",
    auto_review: !!merged.auto_review,
    auto_describe: !!merged.auto_describe,
    auto_improve: !!merged.auto_improve,
    review_on_push: !!merged.review_on_push,
    skip_drafts: !!merged.skip_drafts,
    extra_instructions: String(merged.extra_instructions || "").replace(/\r\n/g, "\n").trim(),
  };
}

export function sameOptions(a: Partial<WorkflowOptions> | null | undefined, b: WorkflowOptions): boolean {
  if (!a) return false;
  const x = normalizeOptions(a);
  const y = normalizeOptions(b);
  return (Object.keys(y) as (keyof WorkflowOptions)[]).every((k) => JSON.stringify(x[k]) === JSON.stringify(y[k]));
}

/** Client-side check mirroring the Rust validation, for instant feedback. */
export function optionsProblem(o: WorkflowOptions): string {
  if (!(o.auto_review || o.auto_describe || o.auto_improve)) return "Turn on at least one of review, describe or improve.";
  const bad = [o.model, ...o.fallback_models].find((m) => !/^[A-Za-z0-9/._:-]{1,200}$/.test(m.trim()));
  if (bad !== undefined) return `Model id "${bad}" has unsupported characters.`;
  if (o.fallback_models.length > 5) return "Use at most 5 fallback models.";
  if (!(o.max_tokens >= 1000 && o.max_tokens <= 2000000)) return "Max tokens must be between 1,000 and 2,000,000.";
  if (o.extra_instructions.includes("${{")) return "Instructions must not contain '${{' (GitHub would evaluate it).";
  if (o.extra_instructions.length > 2000) return "Instructions are limited to 2,000 characters.";
  return "";
}

interface AutoReviewState {
  options: WorkflowOptions;
  setOptions: (patch: Partial<WorkflowOptions>) => void;
  resetOptions: () => void;
}

export const useAutoReview = create<AutoReviewState>((set, get) => ({
  options: loadOptions(),
  setOptions: (patch) => {
    const options = { ...get().options, ...patch };
    lsSet(LS_KEY, JSON.stringify(options));
    set({ options });
  },
  resetOptions: () => {
    lsSet(LS_KEY, JSON.stringify(DEFAULT_OPTIONS));
    set({ options: { ...DEFAULT_OPTIONS } });
  },
}));

/* ---------- per-repo state, derived from github_repo_status ---------- */

export type RepoState =
  | "unknown" // not checked yet
  | "error" // status lookup failed
  | "on" // managed PR Agent workflow, current template, secret present
  | "outdated" // managed, but an older template or edited by hand
  | "custom" // a pr-agent.yml this app did not write
  | "needs-key" // workflow present but OPENROUTER_API_KEY secret missing
  | "openrabbit" // reviewed by the OpenRabbit workflow
  | "backend" // only the backend webhook reviews this repo
  | "off";

export function repoState(st: any): RepoState {
  if (!st) return "unknown";
  if (st.error) return "error";
  const wf = st.workflow || {};
  const secrets = st.secrets || {};
  if (wf.configured && wf.provider === "openrabbit") return secrets.configured ? "openrabbit" : "needs-key";
  if (wf.configured) {
    if (!secrets.openrouter_api_key) return "needs-key";
    if (!wf.managed) return "custom";
    if (wf.outdated) return "outdated";
    return "on";
  }
  if (st.webhook && st.webhook.configured) return "backend";
  return "off";
}

/** Auto review is running in some form (any reviewer posts on new PRs). */
export function isActive(state: RepoState) {
  return state === "on" || state === "outdated" || state === "custom" || state === "openrabbit" || state === "backend";
}

export const STATE_LABEL: Record<RepoState, string> = {
  unknown: "Not checked",
  error: "Status error",
  on: "Auto review on",
  outdated: "Update available",
  custom: "On · custom workflow",
  "needs-key": "Missing API key",
  openrabbit: "On · OpenRabbit",
  backend: "On · backend webhook",
  off: "Off",
};

export function actionsUrl(repo: string) {
  return `https://github.com/${repo}/actions/workflows/pr-agent.yml`;
}
