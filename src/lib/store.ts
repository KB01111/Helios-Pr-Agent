import { create } from "zustand";

export type SecretName = "openrouter" | "commandcode" | "opencodeGo" | "github" | "box" | "api";

const SS_KEYS: Record<SecretName, string> = {
  openrouter: "pragent.openrouter",
  commandcode: "pragent.commandcode",
  opencodeGo: "pragent.opencode_go",
  github: "pragent.github",
  box: "pragent.box",
  api: "pragent.api",
};

/* Secrets live in memory + sessionStorage ONLY — never localStorage. */
function ssGet(k: string): string {
  try {
    return sessionStorage.getItem(k) || "";
  } catch {
    return "";
  }
}
function ssSet(k: string, v: string) {
  try {
    if (v) sessionStorage.setItem(k, v);
    else sessionStorage.removeItem(k);
  } catch {
    /* ignore */
  }
}
export function lsGet(k: string, def = ""): string {
  try {
    return localStorage.getItem(k) ?? def;
  } catch {
    return def;
  }
}
export function lsSet(k: string, v: string) {
  try {
    localStorage.setItem(k, v);
  } catch {
    /* ignore */
  }
}

function loadSecrets(): Record<SecretName, string> {
  // Migrate any legacy localStorage secret -> sessionStorage, then wipe it.
  try {
    const legacy = localStorage.getItem("OPENROUTER_API_KEY") || "";
    if (legacy && !ssGet(SS_KEYS.openrouter)) ssSet(SS_KEYS.openrouter, legacy);
    localStorage.removeItem("OPENROUTER_API_KEY");
  } catch {
    /* ignore */
  }
  return {
    openrouter: ssGet(SS_KEYS.openrouter),
    commandcode: ssGet(SS_KEYS.commandcode),
    opencodeGo: ssGet(SS_KEYS.opencodeGo),
    github: ssGet(SS_KEYS.github),
    box: ssGet(SS_KEYS.box),
    api: ssGet(SS_KEYS.api),
  };
}

export type TabId =
  | "review"
  | "agents"
  | "runs"
  | "autonomy"
  | "quota"
  | "providers"
  | "integrations"
  | "settings";

export const TAB_IDS: TabId[] = [
  "review",
  "agents",
  "runs",
  "autonomy",
  "quota",
  "providers",
  "integrations",
  "settings",
];

export const DEFAULT_MODEL = "openrouter/thinkingmachines/inkling-2-small:free";

export interface ReviewConfig {
  llmProvider: string; // an LLM_PROVIDERS id
  modelChoice: string; // a known option value or "custom"
  customModel: string;
  fallbacks: string;
  maxTokens: string;
  effort: string;
  autoDescribe: boolean;
  autoReview: boolean;
  autoImprove: boolean;
  path: string;
  raw: string;
}

export interface Agent {
  id: string;
  name: string;
  system_prompt?: string;
  trigger?: string;
  trigger_label?: string | null;
  runtime_mode?: string;
  target_repos?: string[];
  [k: string]: unknown;
}

export interface Run {
  run_id?: string;
  id?: string;
  status?: string;
  repo?: string;
  target_repo?: string;
  pr_number?: number | string;
  pr?: number | string;
  provider?: string;
  runtime_mode?: string;
  mode?: string;
  cpu_hours?: number;
  cpu_seconds?: number;
  logs?: string;
  log?: string;
  summary?: string;
  diff?: string;
  [k: string]: unknown;
}

export interface QuotaSlot {
  used: number;
  limit: number;
  remaining: number;
}

export interface BackendState {
  ok: boolean | null;
  label: string;
}

interface AppState {
  theme: "dark" | "light";
  setTheme: (t: "dark" | "light") => void;

  tab: TabId;
  setTab: (t: TabId) => void;

  backendUrl: string;
  setBackendUrl: (u: string) => void;
  backend: BackendState;
  setBackend: (b: BackendState) => void;

  secrets: Record<SecretName, string>;
  setSecret: (name: SecretName, value: string) => void;
  /** Bumped when session secrets are forgotten so pages can reset their local drafts. */
  clearNonce: number;
  wipeSecrets: () => void;

  review: ReviewConfig;
  setReview: (patch: Partial<ReviewConfig>) => void;

  agents: Agent[];
  setAgents: (a: Agent[]) => void;
  pickedRepos: string[];
  setPickedRepos: (r: string[]) => void;
  remoteRepos: string[];
  setRemoteRepos: (r: string[]) => void;

  runs: Run[];
  setRuns: (r: Run[]) => void;
  runsMsg: string;
  setRunsMsg: (m: string) => void;
  streaming: boolean;
  setStreaming: (s: boolean) => void;

  quota: { box: QuotaSlot; vercel: QuotaSlot; updatedAt: number } | null;
  setQuota: (q: AppState["quota"]) => void;

  escCount: number;
  setEscCount: (n: number) => void;

  cloudBase: string;
  setCloudBase: (v: string) => void;
}

const initialTheme = (): "dark" | "light" => (lsGet("pragent.theme") === "light" ? "light" : "dark");
const initialTab = (): TabId => {
  const t = lsGet("pragent.tab") as TabId;
  return TAB_IDS.includes(t) ? t : "review";
};

export const useApp = create<AppState>((set, get) => ({
  theme: initialTheme(),
  setTheme: (theme) => {
    lsSet("pragent.theme", theme);
    set({ theme });
  },

  tab: initialTab(),
  setTab: (tab) => {
    lsSet("pragent.tab", tab);
    set({ tab });
  },

  backendUrl: (lsGet("BACKEND_URL") || "http://localhost:8000").replace(/\/+$/, ""),
  setBackendUrl: (u) => {
    const clean = (u || "").trim().replace(/\/+$/, "");
    if (clean) lsSet("BACKEND_URL", clean);
    else {
      try {
        localStorage.removeItem("BACKEND_URL"); // cleared => back to the default, also after restart
      } catch {
        /* ignore */
      }
    }
    set({ backendUrl: clean || "http://localhost:8000" });
  },
  backend: { ok: null, label: "checking…" },
  setBackend: (backend) => set({ backend }),

  secrets: loadSecrets(),
  setSecret: (name, value) => {
    const v = (value || "").trim();
    ssSet(SS_KEYS[name], v);
    set({ secrets: { ...get().secrets, [name]: v } });
  },
  clearNonce: 0,
  wipeSecrets: () => {
    for (const k of Object.keys(SS_KEYS) as SecretName[]) ssSet(SS_KEYS[k], "");
    set({
      secrets: { openrouter: "", commandcode: "", opencodeGo: "", github: "", box: "", api: "" },
      clearNonce: get().clearNonce + 1,
    });
  },

  review: {
    llmProvider: "openrouter",
    modelChoice: DEFAULT_MODEL,
    customModel: "",
    fallbacks: "openrouter/free",
    maxTokens: "200000",
    effort: "low",
    autoDescribe: true,
    autoReview: true,
    autoImprove: false,
    path: "",
    raw: "",
  },
  setReview: (patch) => set({ review: { ...get().review, ...patch } }),

  agents: [],
  setAgents: (agents) => set({ agents }),
  pickedRepos: [],
  setPickedRepos: (pickedRepos) => set({ pickedRepos }),
  remoteRepos: [],
  setRemoteRepos: (remoteRepos) => set({ remoteRepos }),

  runs: [],
  setRuns: (runs) => set({ runs }),
  runsMsg: "Runs not synced.",
  setRunsMsg: (runsMsg) => set({ runsMsg }),
  streaming: false,
  setStreaming: (streaming) => set({ streaming }),

  quota: null,
  setQuota: (quota) => set({ quota }),

  escCount: 0,
  setEscCount: (escCount) => set({ escCount }),

  cloudBase: lsGet("pragent.cloud_base"),
  setCloudBase: (v) => {
    lsSet("pragent.cloud_base", v);
    set({ cloudBase: v });
  },
}));

export type LlmProviderId = "openrouter" | "commandcode" | "opencode_go";

export interface LlmProvider {
  label: string;
  envKey: string;
  secret: SecretName;
  keyPlaceholder: string;
  models: { value: string; label: string }[];
}

export const LLM_PROVIDERS: Record<LlmProviderId, LlmProvider> = {
  openrouter: {
    label: "OpenRouter",
    envKey: "OPENROUTER_API_KEY",
    secret: "openrouter",
    keyPlaceholder: "sk-or-…",
    models: [
      { value: DEFAULT_MODEL, label: `${DEFAULT_MODEL} (default)` },
      { value: "openrouter/free", label: "openrouter/free (router)" },
      { value: "openrouter/auto", label: "openrouter/auto (router)" },
    ],
  },
  commandcode: {
    label: "Command Code",
    envKey: "COMMANDCODE_API_KEY",
    secret: "commandcode",
    keyPlaceholder: "Command Code API key",
    models: [
      { value: "openai/deepseek/deepseek-v4-flash", label: "openai/deepseek/deepseek-v4-flash (default)" },
      { value: "openai/Qwen/Qwen3.7-Max", label: "openai/Qwen/Qwen3.7-Max" },
    ],
  },
  opencode_go: {
    label: "OpenCode Go",
    envKey: "OPENCODE_GO_API_KEY",
    secret: "opencodeGo",
    keyPlaceholder: "oc_sk_…",
    models: [{ value: "openai/kimi-k3", label: "openai/kimi-k3 (default)" }],
  },
};

export function toProviderId(id: string): LlmProviderId {
  return Object.prototype.hasOwnProperty.call(LLM_PROVIDERS, id) ? (id as LlmProviderId) : "openrouter";
}

export function providerOf(id: string): LlmProvider {
  return LLM_PROVIDERS[toProviderId(id)];
}

export function modelsForProvider(id: string): { value: string; label: string }[] {
  return [...providerOf(id).models, { value: "custom", label: "custom…" }];
}

export function defaultModelFor(id: string): string {
  return providerOf(id).models[0].value;
}

export function selectedProvider(): LlmProviderId {
  return toProviderId(useApp.getState().review.llmProvider);
}

export function selectedModel(): string {
  const r = useApp.getState().review;
  if (r.modelChoice === "custom") return r.customModel.trim() || defaultModelFor(selectedProvider());
  return r.modelChoice;
}
