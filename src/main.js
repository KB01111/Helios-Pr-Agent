const $ = (id) => document.getElementById(id);
const out = (m) => { $("out").textContent = m; };
const invoke = async (cmd, args) => window.__TAURI__.core.invoke(cmd, args);

function selectedModel() {
  const v = $("model").value;
  if (v === "custom") return $("custom-model").value.trim() || "openrouter/thinkingmachines/inkling-2-small:free";
  return v;
}

async function load() {
  try {
    const cfg = await invoke("get_config");
    $("cfg-path").textContent = "config: " + cfg.path;
    $("raw").textContent = cfg.raw;
    const known = [...$("model").options].map(o => o.value);
    if (known.includes(cfg.model)) { $("model").value = cfg.model; $("custom-wrap").classList.add("hidden"); }
    else { $("model").value = "custom"; $("custom-wrap").classList.remove("hidden"); $("custom-model").value = cfg.model; }
    $("fallbacks").value = (cfg.fallback_models || []).join(", ");
    $("max-tokens").value = cfg.custom_model_max_tokens || "200000";
    $("effort").value = cfg.reasoning_effort || "low";
    $("auto-describe").checked = !!cfg.auto_describe;
    $("auto-review").checked = !!cfg.auto_review;
    $("auto-improve").checked = !!cfg.auto_improve;
    const savedKey = localStorage.getItem("OPENROUTER_API_KEY") || "";
    if (savedKey) $("or-key").value = savedKey;
    out("Loaded config.");
  } catch (e) { out("Load failed: " + e); }
}

async function save() {
  const payload = {
    model: selectedModel(),
    fallback_models: $("fallbacks").value.split(",").map(s => s.trim()).filter(Boolean),
    custom_model_max_tokens: $("max-tokens").value.trim() || "200000",
    reasoning_effort: $("effort").value,
    auto_describe: $("auto-describe").checked,
    auto_review: $("auto-review").checked,
    auto_improve: $("auto-improve").checked,
  };
  try {
    const p = await invoke("save_config", { cfg: payload });
    localStorage.setItem("OPENROUTER_API_KEY", $("or-key").value.trim());
    out("Saved to " + p);
    load();
  } catch (e) { out("Save failed: " + e); }
}

function prUrl() { return $("pr-url").value.trim(); }
function cmd() {
  const c = $("cmd").value;
  if (c === "ask") { const q = $("ask-q").value.trim() || "What does this PR change?"; return `ask "${q}"`; }
  return c;
}

$("model").addEventListener("change", () => {
  $("custom-wrap").classList.toggle("hidden", $("model").value !== "custom");
});
$("cmd").addEventListener("change", () => {
  $("ask-wrap").classList.toggle("hidden", $("cmd").value !== "ask");
});
$("btn-load").addEventListener("click", load);
$("btn-save").addEventListener("click", save);

$("btn-cli").addEventListener("click", async () => {
  if (!prUrl()) return out("Enter a PR URL first.");
  try {
    const s = await invoke("build_cli_command", { prUrl: prUrl(), command: cmd() });
    await navigator.clipboard.writeText(s);
    out("Copied: " + s);
  } catch (e) { out("Clipboard failed, command: pr-agent --pr_url " + prUrl() + " " + cmd()); }
});

$("btn-run").addEventListener("click", async () => {
  if (!prUrl()) return out("Enter a PR URL first.");
  out("Running local review… (needs `pip install \"pr-agent[github]\"`)");
  try {
    const r = await invoke("run_local_review", { prUrl: prUrl(), command: cmd(), openrouterKey: $("or-key").value.trim() });
    out(r || "(empty output)");
  } catch (e) { out("Run failed: " + e); }
});

$("btn-open-pr").addEventListener("click", async () => {
  if (!prUrl()) return out("Enter a PR URL first.");
  try { await window.__TAURI__.opener.openUrl(prUrl()); }
  catch { window.open(prUrl(), "_blank"); }
});
$("btn-open-actions").addEventListener("click", async () => {
  const m = prUrl().match(/github\.com\/([^/]+\/[^/]+)/);
  const repo = m ? m[1].replace(/\/pull\/.*$/, "") : "";
  const url = repo ? `https://github.com/${repo}/actions` : "https://github.com";
  try { await window.__TAURI__.opener.openUrl(url); }
  catch { window.open(url, "_blank"); }
});

const boxOut = (m) => { $("box-out").textContent = m; };
function boxArgs(statusOnly) {
  const boxKey = $("box-key").value.trim();
  if (!boxKey) { boxOut("Enter UPSTASH_BOX_API_KEY first."); return null; }
  if (!statusOnly) {
    if (!$("gh-key").value.trim()) { boxOut("Enter GITHUB_TOKEN first."); return null; }
    if (!$("or-key").value.trim()) { boxOut("Enter OPENROUTER_API_KEY in section 2 first."); return null; }
    if (!$("box-repos").value.trim()) { boxOut("Enter at least one repo."); return null; }
  }
  return {
    boxKey,
    githubToken: $("gh-key").value.trim(),
    openrouterKey: $("or-key").value.trim(),
    repos: $("box-repos").value.trim(),
    boxName: $("box-name").value.trim() || "pr-agent-runner",
    cron: $("box-cron").value.trim() || "*/15 * * * *",
    model: selectedModel(),
    statusOnly,
  };
}

$("btn-box-up").addEventListener("click", async () => {
  const args = boxArgs(false);
  if (!args) return;
  boxOut("Provisioning box + schedule…");
  try { boxOut(await invoke("box_provision", args)); }
  catch (e) { boxOut("Provision failed: " + e); }
});

$("btn-box-status").addEventListener("click", async () => {
  const args = boxArgs(true);
  if (!args) return;
  boxOut("Checking box…");
  try { boxOut(await invoke("box_provision", args)); }
  catch (e) { boxOut("Status failed: " + e); }
});

// ---- GitHub auth (memory only) ----
let ghToken = "";
let oauthCancel = false;
const oauthOut = (m) => { $("oauth-out").textContent = m; };

$("btn-oauth-start").addEventListener("click", async () => {
  const clientId = $("oauth-client").value.trim();
  if (!clientId) return oauthOut("Enter the OAuth App Client ID first.");
  oauthCancel = false;
  try {
    const d = await invoke("github_device_start", { clientId });
    $("oauth-code").classList.remove("hidden");
    $("oauth-usercode").textContent = d.user_code;
    $("btn-oauth-cancel").classList.remove("hidden");
    $("btn-oauth-open").classList.remove("hidden");
    $("btn-oauth-open").onclick = async () => {
      try { await window.__TAURI__.opener.openUrl(d.verification_uri); }
      catch { window.open(d.verification_uri, "_blank"); }
    };
    oauthOut(`Code shown — approve on GitHub, waiting… (expires in ~${Math.round((d.expires_in || 900) / 60)} min)`);
    const interval = Math.max(5, d.interval || 5);
    while (!oauthCancel) {
      const r = await invoke("github_device_poll", {
        clientId, deviceCode: d.device_code, intervalSecs: interval, maxWaitSecs: 115,
      });
      if (r.status === "authorized") {
        ghToken = r.access_token;
        $("gh-key").value = ghToken;
        $("oauth-code").classList.add("hidden");
        $("btn-oauth-cancel").classList.add("hidden");
        oauthOut(`Authenticated as ${r.login || "(unknown login)"}. Token in memory + filled into Box GITHUB_TOKEN.`);
        return;
      }
      // pending -> loop again until user cancels
    }
    oauthOut("Login cancelled. Token discarded.");
  } catch (e) { oauthOut("OAuth failed: " + e); }
});

$("btn-oauth-cancel").addEventListener("click", () => { oauthCancel = true; });

$("btn-oauth-pat").addEventListener("click", async () => {
  const t = $("oauth-pat").value.trim();
  if (!t) return oauthOut("Paste a PAT first.");
  try {
    const login = await invoke("github_login", { token: t });
    ghToken = t;
    $("gh-key").value = t;
    $("oauth-pat").value = "";
    oauthOut(`Authenticated as ${login || "(unknown login)"}. Token in memory + filled into Box GITHUB_TOKEN.`);
  } catch (e) { oauthOut("PAT rejected: " + e); }
});

// ---- Provider: PR-Agent ----
const paOut = (m) => { $("pa-out").textContent = m; };

$("btn-pa-check").addEventListener("click", async () => {
  paOut("Checking…");
  const parts = [];
  for (const t of ["pr-agent", "python", "gh"]) {
    try { parts.push(t + ": " + await invoke("check_tool", { name: t })); }
    catch (e) { parts.push(t + ": MISSING (" + e + ")"); }
  }
  paOut(parts.join("\n"));
});

$("btn-pa-install").addEventListener("click", async () => {
  paOut("Installing pr-agent[github] via pip — takes a few minutes…");
  try { paOut(await invoke("install_pr_agent")); }
  catch (e) { paOut("Install failed: " + e); }
});

$("btn-pa-write").addEventListener("click", async () => {
  try { paOut("Wrote workflow to " + await invoke("write_workflow", { provider: "pr-agent" }) + "\nCommit + push, add secret OPENROUTER_API_KEY, and it reviews every PR (always online)."); }
  catch (e) { paOut("Write failed: " + e); }
});

$("btn-pa-copy").addEventListener("click", async () => {
  try {
    const y = await invoke("get_workflow_template", { provider: "pr-agent" });
    await navigator.clipboard.writeText(y);
    paOut("Workflow YAML copied — add as .github/workflows/pr-agent.yml in any repo + secret OPENROUTER_API_KEY.");
  } catch (e) { paOut("Copy failed: " + e); }
});

// ---- Provider: OpenRabbit ----
const orOut = (m) => { $("or-out").textContent = m; };

$("btn-or-write").addEventListener("click", async () => {
  try { orOut("Wrote workflow to " + await invoke("write_workflow", { provider: "openrabbit" }) + "\nCommit + push, add secret LLM_API_KEY (= OpenRouter key), and every PR is reviewed automatically (always online)."); }
  catch (e) { orOut("Write failed: " + e); }
});

$("btn-or-copy").addEventListener("click", async () => {
  try {
    const y = await invoke("get_workflow_template", { provider: "openrabbit" });
    await navigator.clipboard.writeText(y);
    orOut("Workflow YAML copied — add as .github/workflows/openrabbit.yml in any repo + secret LLM_API_KEY.");
  } catch (e) { orOut("Copy failed: " + e); }
});

load();

// ---- 8 · Multi-PR Agents (cloud brain) + 9 · Background Runs ----
// Backend URL only in localStorage; never log tokens.
function backendUrl() {
  const el = $("backend-url");
  let u = (el && el.value ? el.value : "").trim()
    || localStorage.getItem("BACKEND_URL")
    || "http://localhost:8000";
  u = u.replace(/\/+$/, "");
  try { localStorage.setItem("BACKEND_URL", u); } catch (_) {}
  if (el && !el.value) el.value = u;
  return u;
}
function safeInvoke(cmd, args) {
  try {
    if (window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke) {
      return window.__TAURI__.core.invoke(cmd, args);
    }
  } catch (_) {}
  return Promise.reject(new Error("tauri-invoke-unavailable"));
}
const agentsOut = (m) => { const el = $("agents-out"); if (el) el.textContent = m; };
const runsOut = (m) => { const el = $("runs-out"); if (el) el.textContent = m; };
function escHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function parseJsonLoose(t) {
  if (typeof t !== "string") return t;
  try { return JSON.parse(t); } catch (_) { return null; }
}

async function refreshAgents() {
  const base = backendUrl();
  agentsOut("Loading agents…");
  // Prefer Rust proxy (avoids CORS in dev), fall back to direct fetch.
  try {
    const raw = await safeInvoke("backend_sync_agents", { backendUrl: base, agentsJson: "" });
    const data = parseJsonLoose(raw);
    if (data) { renderAgents(data); agentsOut(`Loaded ${Array.isArray(data) ? data.length : 0} agent(s) via proxy.`); return; }
  } catch (_) {}
  try {
    const r = await fetch(base + "/agents");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    renderAgents(data);
    agentsOut(`Loaded ${Array.isArray(data) ? data.length : 0} agent(s).`);
  } catch (e) { agentsOut("Refresh failed (is backend up at " + base + "?): " + e); }
}
function renderAgents(data) {
  const sel = $("agent-select");
  if (!sel) return;
  const list = Array.isArray(data) ? data : (data.agents || []);
  sel.innerHTML = '<option value="">— new agent —</option>';
  for (const a of list) {
    const o = document.createElement("option");
    o.value = a.id || "";
    o.textContent = (a.name || a.id || "agent") + (a.id ? " (" + a.id.slice(0, 8) + ")" : "");
    sel.appendChild(o);
  }
}

async function saveAgent() {
  const base = backendUrl();
  const sel = $("agent-select");
  const id = sel && sel.value ? sel.value : "";
  const body = {
    name: $("agent-name").value.trim(),
    system_prompt: $("agent-prompt").value.trim(),
    target_repos: $("agent-repos").value.split(",").map((s) => s.trim()).filter(Boolean),
    trigger: $("agent-trigger").value,
    trigger_label: $("agent-label").value.trim() || null,
    runtime_mode: $("agent-mode").value,
  };
  if (!body.name) { agentsOut("Enter an agent name first."); return; }
  if (body.trigger !== "on_label") body.trigger_label = null;
  agentsOut("Saving…");
  try {
    const r = await fetch(base + (id ? "/agents/" + encodeURIComponent(id) : "/agents"), {
      method: id ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error("HTTP " + r.status + " " + (await r.text()).slice(0, 300));
    const saved = await r.json();
    agentsOut("Saved agent " + (saved.id || body.name));
    refreshAgents();
  } catch (e) { agentsOut("Save failed: " + e); }
}

async function deleteAgent() {
  const base = backendUrl();
  const sel = $("agent-select");
  const id = sel && sel.value ? sel.value : "";
  if (!id) { agentsOut("Select an existing agent to delete."); return; }
  agentsOut("Deleting…");
  try {
    const r = await fetch(base + "/agents/" + encodeURIComponent(id), { method: "DELETE" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    agentsOut("Deleted.");
    refreshAgents();
  } catch (e) { agentsOut("Delete failed: " + e); }
}

function runStatusClass(s) {
  s = String(s || "").toLowerCase();
  if (s.includes("approv") || s.includes("merged") || s.includes("done") || s.includes("success")) return "ok";
  if (s.includes("fail") || s.includes("error")) return "err";
  if (s.includes("wait") || s.includes("pend")) return "pending";
  return "";
}
function renderRuns(list) {
  const box = $("runs-list");
  if (!box) return;
  box.innerHTML = "";
  if (!list || !list.length) {
    box.innerHTML = '<p class="hint">No runs yet.</p>';
    return;
  }
  for (const r of list) {
    const div = document.createElement("div");
    div.className = "run-item";
    const id = r.id || r.run_id || "";
    const status = r.status || "unknown";
    const repo = escHtml(r.repo || r.target_repo || "?");
    const pr = escHtml(String(r.pr_number || r.pr || "?"));
    const provider = escHtml(r.provider || r.runtime_mode || r.mode || "?");
    const cpu = escHtml(String(r.cpu_hours ?? r.cpu ?? "?"));
    const logs = escHtml(String(r.logs || r.log || r.summary || "").slice(0, 800));
    const diff = escHtml(String(r.diff || ""));
    div.innerHTML =
      '<div class="agent-row"><strong>' + repo + '#' + pr + '</strong>'
      + '<span class="pill ' + runStatusClass(status) + '">' + escHtml(status) + '</span>'
      + '<span class="hint">' + provider + ' · ' + cpu + ' cpu-h</span></div>'
      + (logs ? '<pre class="run-logs">' + logs + '</pre>' : '')
      + (diff ? '<details><summary>diff</summary><pre>' + diff.slice(0, 6000) + '</pre></details>' : '');
    if (id && /wait|pend/i.test(String(status))) {
      const b = document.createElement("button");
      b.className = "ghost";
      b.textContent = "Approve";
      b.onclick = () => approveRun(id);
      div.appendChild(b);
    }
    box.appendChild(div);
  }
}

async function syncRuns() {
  const base = backendUrl();
  runsOut("Syncing runs…");
  try {
    const raw = await safeInvoke("backend_get_runs", { backendUrl: base });
    const data = parseJsonLoose(raw);
    if (data) {
      const list = Array.isArray(data) ? data : (data.runs || []);
      renderRuns(list);
      runsOut(`Synced ${list.length} run(s) via proxy.`);
      return;
    }
  } catch (_) {}
  try {
    const r = await fetch(base + "/runs?limit=50");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    const list = Array.isArray(data) ? data : (data.runs || []);
    renderRuns(list);
    runsOut(`Synced ${list.length} run(s).`);
  } catch (e) { runsOut("Sync failed (is backend up at " + base + "?): " + e); }
}

async function approveRun(id) {
  const base = backendUrl();
  runsOut("Approving " + String(id).slice(0, 8) + "…");
  try {
    const r = await fetch(base + "/runs/" + encodeURIComponent(id) + "/approve", { method: "POST" });
    if (!r.ok) throw new Error("HTTP " + r.status + " " + (await r.text()).slice(0, 300));
    runsOut("Approved.");
    syncRuns();
  } catch (e) { runsOut("Approve failed: " + e); }
}

async function fetchQuota() {
  const base = backendUrl();
  try {
    let data = null;
    try {
      const raw = await safeInvoke("backend_get_quota", { backendUrl: base });
      data = parseJsonLoose(raw);
    } catch (_) {}
    if (!data) {
      const r = await fetch(base + "/quota");
      if (!r.ok) throw new Error("HTTP " + r.status);
      data = await r.json();
    }
    const box = (data && (data.upstash_box || data.box)) || {};
    const ver = (data && (data.vercel_sandbox || data.vercel)) || {};
    const bUsed = Number(box.used || 0), bLim = Number(box.limit || 18000);
    const vUsed = Number(ver.used || 0), vLim = Number(ver.limit || 18000);
    const bb = $("quota-box-bar"), vb = $("quota-vercel-bar"), qt = $("quota-text");
    if (bb) bb.style.width = Math.min(100, bLim ? (bUsed / bLim) * 100 : 0) + "%";
    if (vb) vb.style.width = Math.min(100, vLim ? (vUsed / vLim) * 100 : 0) + "%";
    if (qt) qt.textContent = `Box ${bUsed}/${bLim} (remaining ${box.remaining != null ? box.remaining : Math.max(0, bLim - bUsed)}h) · Vercel ${vUsed}/${vLim} (remaining ${ver.remaining != null ? ver.remaining : Math.max(0, vLim - vUsed)}h)`;
    runsOut("Quota updated.");
  } catch (e) { runsOut("Quota failed: " + e); }
}

let evtSrc = null;
function toggleStream() {
  const btn = $("btn-stream");
  if (evtSrc) {
    evtSrc.close(); evtSrc = null;
    if (btn) btn.textContent = "Live: off";
    runsOut("Stream stopped.");
    return;
  }
  const base = backendUrl();
  try {
    evtSrc = new EventSource(base + "/events/stream");
  } catch (e) { runsOut("Stream failed: " + e); return; }
  if (btn) btn.textContent = "Live: on";
  runsOut("Streaming live events…");
  evtSrc.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data && (data.type === "quota" || data.quota)) { fetchQuota(); return; }
      const box = $("runs-list");
      if (box && data && (data.repo || data.pr_number || data.status)) {
        renderRuns([data].concat(
          [...box.querySelectorAll(".run-item")].length ? [] : []
        ));
        syncRuns();
      }
    } catch (_) {
      runsOut("event: " + String(ev.data).slice(0, 200));
    }
  };
  evtSrc.onerror = () => { runsOut("Stream error — backend reachable at " + base + "?"); };
}

// Wire new cards (guard for partial DOM) + auto-sync on load.
(function wireCloudBrain() {
  const on = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };
  on("btn-agents-save", saveAgent);
  on("btn-agents-refresh", refreshAgents);
  on("btn-agents-delete", deleteAgent);
  on("btn-runs-sync", syncRuns);
  on("btn-quota", fetchQuota);
  on("btn-stream", toggleStream);
  const trig = $("agent-trigger");
  if (trig) trig.addEventListener("change", () => {
    const w = $("agent-label-wrap");
    if (w) w.style.display = trig.value === "on_label" ? "" : "none";
    if (trig.value !== "on_label" && w) w.style.display = "none";
    else if (trig.value === "on_label" && w) w.style.display = "";
  });
  const bu = $("backend-url");
  if (bu) {
    try {
      const saved = localStorage.getItem("BACKEND_URL");
      if (saved) bu.value = saved;
    } catch (_) {}
    bu.addEventListener("change", backendUrl);
  }
  // Auto-sync: best effort, never throw.
  try { refreshAgents(); } catch (_) {}
  try { syncRuns(); } catch (_) {}
  try { fetchQuota(); } catch (_) {}
})();
