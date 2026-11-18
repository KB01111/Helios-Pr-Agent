import { apiGet, apiMut, backendUrl } from "./api";
import { KNOWN_MODELS, selectedModel, useApp, type Agent, type Run } from "./store";
import { errText, tauriInvoke } from "./tauri";
import { toastErr, toastOk } from "./toast";

/* ---------- backend connection ---------- */
let healthSeq = 0;
export async function checkBackend(showToast = false): Promise<boolean> {
  const { setBackend } = useApp.getState();
  const seq = ++healthSeq;
  const base = backendUrl();
  // Only the newest check for the still-selected backend may update the status.
  const current = () => seq === healthSeq && base === backendUrl();
  try {
    const data = await apiGet("/health");
    const mode = (data && (data.store || data.mode)) || "up";
    if (current()) setBackend({ ok: true, label: `online (${mode})` });
    if (showToast) toastOk("Backend reachable", `${base} · store=${mode}`);
    return true;
  } catch (e) {
    if (current()) setBackend({ ok: false, label: "offline" });
    if (showToast) toastErr("Backend unreachable", errText(e, 200));
    return false;
  }
}

/* ---------- .pr_agent.toml review config ---------- */
export async function loadConfig(): Promise<string> {
  const { setReview } = useApp.getState();
  try {
    const cfg = await tauriInvoke("get_config");
    const known = KNOWN_MODELS.map((m) => m.value).filter((v) => v !== "custom");
    const isKnown = known.includes(cfg.model);
    setReview({
      path: cfg.path,
      raw: cfg.raw,
      modelChoice: isKnown ? cfg.model : "custom",
      customModel: isKnown ? "" : cfg.model,
      fallbacks: (cfg.fallback_models || []).join(", "),
      maxTokens: String(cfg.max_model_tokens || cfg.custom_model_max_tokens || "200000"),
      effort: cfg.reasoning_effort || "low",
      autoDescribe: !!cfg.auto_describe,
      autoReview: !!cfg.auto_review,
      autoImprove: !!cfg.auto_improve,
    });
    return "Loaded config.";
  } catch (e) {
    toastErr("Config load failed", errText(e, 160));
    return "Load failed: " + errText(e, 300);
  }
}

export async function saveConfig(): Promise<string> {
  const r = useApp.getState().review;
  const maxTokens = r.maxTokens.trim() || "200000";
  const payload = {
    model: selectedModel(),
    fallback_models: r.fallbacks.split(",").map((s) => s.trim()).filter(Boolean),
    max_model_tokens: maxTokens,
    custom_model_max_tokens: maxTokens,
    reasoning_effort: r.effort,
    auto_describe: r.autoDescribe,
    auto_review: r.autoReview,
    auto_improve: r.autoImprove,
  };
  try {
    const p = await tauriInvoke("save_config", { cfg: payload });
    toastOk("Config saved", String(p));
    void loadConfig();
    return "Saved to " + p;
  } catch (e) {
    toastErr("Save failed", errText(e, 200));
    return "Save failed: " + errText(e, 300);
  }
}

/* ---------- agents ---------- */
export function agentList(data: any): Agent[] {
  const raw = Array.isArray(data) ? data : (data && data.agents) || [];
  return Array.isArray(raw) ? raw : [];
}

export async function refreshAgents(): Promise<Agent[] | null> {
  try {
    const list = agentList(await apiGet("/agents"));
    useApp.getState().setAgents(list);
    return list;
  } catch (e) {
    toastErr("Agents refresh failed", errText(e, 200));
    return null;
  }
}

/* ---------- runs ---------- */
export async function syncRuns() {
  const { setRuns, setRunsMsg } = useApp.getState();
  setRunsMsg("Syncing runs…");
  try {
    const data = await apiGet("/runs?limit=50");
    const list: Run[] = Array.isArray(data) ? data : data.runs || [];
    setRuns(list);
    setRunsMsg(`Synced ${list.length} run(s).`);
  } catch (e) {
    setRunsMsg(`Sync failed (is backend up at ${backendUrl()}?): ${errText(e, 200)}`);
    toastErr("Runs sync failed", errText(e, 200));
  }
}

/* ---------- quota ---------- */
export async function fetchQuota() {
  const { setQuota, setRunsMsg } = useApp.getState();
  try {
    const data = await apiGet("/quota");
    const box = (data && (data.upstash_box || data.box)) || {};
    const ver = (data && (data.vercel_sandbox || data.vercel)) || {};
    const slot = (s: any) => {
      const used = Number(s.used || 0);
      const limit = Number(s.limit || 18000);
      return { used, limit, remaining: s.remaining != null ? Number(s.remaining) : Math.max(0, limit - used) };
    };
    setQuota({ box: slot(box), vercel: slot(ver), updatedAt: Date.now() });
    setRunsMsg("Quota updated.");
  } catch (e) {
    setRunsMsg("Quota failed: " + errText(e, 200));
    toastErr("Quota failed", errText(e, 200));
  }
}

/* ---------- live event stream (SSE) ---------- */
let evtSrc: EventSource | null = null;
let syncTimer: ReturnType<typeof setTimeout> | null = null;

function scheduleSyncRuns() {
  if (syncTimer) clearTimeout(syncTimer);
  syncTimer = setTimeout(() => {
    syncTimer = null;
    void syncRuns();
  }, 300);
}

let streamGen = 0; // invalidates pending stream starts (ticket requests in flight)
let streamStarting = false;

export function stopStream(message?: string) {
  streamGen++;
  streamStarting = false;
  if (evtSrc) {
    evtSrc.close();
    evtSrc = null;
  }
  useApp.getState().setStreaming(false);
  if (message) useApp.getState().setRunsMsg(message);
}

export async function toggleStream() {
  if (evtSrc) {
    stopStream("Stream stopped.");
    return;
  }
  if (streamStarting) return; // a start is already in flight
  streamStarting = true;
  const gen = ++streamGen;
  const { setRunsMsg, setStreaming, secrets } = useApp.getState();
  const base = backendUrl();
  // EventSource cannot set headers: exchange the API token for a short-lived,
  // single-use ticket so the long-lived token never appears in a URL.
  let streamUrl = base + "/events/stream";
  try {
    if (secrets.api) {
      const t = await apiMut("POST", "/events/ticket", {});
      if (!t || !t.ticket) throw new Error("no stream ticket returned");
      streamUrl += "?ticket=" + encodeURIComponent(t.ticket);
    }
    if (gen !== streamGen) return; // stopped / secrets cleared while the ticket was pending
    evtSrc = new EventSource(streamUrl);
  } catch (e) {
    if (gen === streamGen) {
      streamStarting = false;
      setRunsMsg("Stream failed: " + errText(e, 200));
    }
    return;
  }
  streamStarting = false;
  setStreaming(true);
  setRunsMsg("Streaming live events…");
  evtSrc.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data && (data.type === "quota" || data.quota)) {
        void fetchQuota();
        return;
      }
      if (data && (data.repo || data.pr_number || data.status)) scheduleSyncRuns();
    } catch {
      setRunsMsg("event: " + String(ev.data).slice(0, 200));
    }
  };
  const source = evtSrc;
  source.onerror = () => {
    // A closed source will not retry (e.g. the single-use ticket was rejected on
    // reconnect): stop so the next click mints a fresh ticket.
    if (source.readyState === EventSource.CLOSED && evtSrc === source) {
      stopStream("Live stream closed by the backend — click Live to reconnect.");
      return;
    }
    setRunsMsg(
      "Stream error — check the backend at " +
        base +
        " and set ALLOWED_ORIGINS to include tauri://localhost,http://tauri.localhost,http://localhost:1420",
    );
  };
}
