/* PR Agent Control — vanilla JS, CSP-safe (no eval, no inline handlers).
 * Session secrets (OpenRouter / GitHub / Box / API token) live in MEMORY +
 * sessionStorage only — never localStorage. Integration credentials may also be
 * written to backend/.env by the Rust backend when the user explicitly saves
 * them. Backend URL + theme + OAuth client id are non-secret and may persist.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function escHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function parseJsonLoose(t) {
  if (typeof t !== "string") return t;
  try { return JSON.parse(t); } catch (_) { return null; }
}

/* ---------- toasts ---------- */
function toast(title, body, kind) {
  const box = $("toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  const b = document.createElement("b");
  b.textContent = title;
  el.appendChild(b);
  if (body) {
    const s = document.createElement("span");
    s.textContent = body;
    el.appendChild(s);
  }
  box.appendChild(el);
  while (box.children.length > 5) box.removeChild(box.firstChild);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 300);
  }, 4200);
}
const toastOk = (t, b) => toast(t, b, "success");
const toastErr = (t, b) => toast(t, b, "error");

/* ---------- tauri bridge ---------- */
function tauriInvoke(cmd, args) {
  try {
    if (window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke) {
      return window.__TAURI__.core.invoke(cmd, args);
    }
  } catch (_) {}
  return Promise.reject(new Error("tauri-invoke-unavailable"));
}
async function openUrl(url) {
  try {
    if (window.__TAURI__ && window.__TAURI__.opener && window.__TAURI__.opener.openUrl) {
      await window.__TAURI__.opener.openUrl(url);
      return;
    }
  } catch (_) {}
  window.open(url, "_blank", "noopener");
}

/* ---------- secrets: memory + sessionStorage (NEVER localStorage) ---------- */
const Secrets = { openrouter: "", github: "", box: "", api: "" };
let evtSrc = null;
const SS_KEYS = {
  openrouter: "pragent.openrouter",
  github: "pragent.github",
  box: "pragent.box",
  api: "pragent.api",
};
function ssGet(k) { try { return sessionStorage.getItem(k) || ""; } catch (_) { return ""; } }
function ssSet(k, v) { try { if (v) sessionStorage.setItem(k, v); else sessionStorage.removeItem(k); } catch (_) {} }

function loadSecrets() {
  // Migrate any legacy localStorage secrets -> sessionStorage, then wipe localStorage.
  try {
    const legacy = localStorage.getItem("OPENROUTER_API_KEY") || "";
    if (legacy && !ssGet(SS_KEYS.openrouter)) ssSet(SS_KEYS.openrouter, legacy);
    localStorage.removeItem("OPENROUTER_API_KEY");
  } catch (_) {}
  Secrets.openrouter = ssGet(SS_KEYS.openrouter);
  Secrets.github = ssGet(SS_KEYS.github);
  Secrets.box = ssGet(SS_KEYS.box);
  Secrets.api = ssGet(SS_KEYS.api);
}
function setSecret(name, value) {
  Secrets[name] = (value || "").trim();
  ssSet(SS_KEYS[name], Secrets[name]);
}
function clearSecrets() {
  oauthCancel = true; // stop any pending GitHub device-flow polling
  for (const k of Object.keys(Secrets)) { Secrets[k] = ""; ssSet(SS_KEYS[k], ""); }
  for (const id of [
    "or-key", "sett-or", "gh-key", "sett-gh", "box-key", "oauth-pat", "api-token",
    "int-gh-token", "int-gh-secret", "int-vercel-token", "int-box-key",
    "int-redis-conn-url", "int-redis-url", "int-redis-token", "int-or-key",
  ]) {
    const el = $(id);
    if (el) el.value = "";
  }
  if (evtSrc) {
    evtSrc.close();
    evtSrc = null;
    const btn = $("btn-stream");
    if (btn) btn.textContent = "Live: off";
  }
  ghResetUi();
  setGhState(false, "");
  toastOk("Secrets cleared", "Session values and stream tokens wiped.");
}
function syncSecretInputs() {
  const map = {
    "or-key": Secrets.openrouter, "sett-or": Secrets.openrouter,
    "gh-key": Secrets.github, "sett-gh": Secrets.github,
    "box-key": Secrets.box, "api-token": Secrets.api,
  };
  for (const [id, v] of Object.entries(map)) {
    const el = $(id);
    if (el && document.activeElement !== el) el.value = v || "";
  }
}

/* ---------- non-secret prefs (localStorage is fine) ---------- */
function backendUrl() {
  const el = $("backend-url");
  let u = (el && el.value ? el.value : "").trim()
    || (function () { try { return localStorage.getItem("BACKEND_URL") || ""; } catch (_) { return ""; } })()
    || "http://localhost:8000";
  u = u.replace(/\/+$/, "");
  try { localStorage.setItem("BACKEND_URL", u); } catch (_) {}
  if (el && !el.value) el.value = u;
  return u;
}

/* ---------- tabs ---------- */
const TAB_TITLES = {
  review: "Review",
  agents: "Agents & repo sync",
  runs: "Background runs",
  quota: "Quota & autoscale",
  providers: "Providers",
  integrations: "Integrations",
  settings: "Settings",
};
function switchTab(name) {
  if (!TAB_TITLES[name]) return;
  $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  const t = $("page-title");
  if (t) t.textContent = TAB_TITLES[name];
  try { localStorage.setItem("pragent.tab", name); } catch (_) {}
}

/* ---------- backend access: Rust proxy first, fetch fallback ---------- */
function authHeaders() {
  const h = { "Content-Type": "application/json" };
  if (Secrets.api) h["Authorization"] = "Bearer " + Secrets.api;
  return h;
}
function tauriUnavailable(err) {
  return /tauri-invoke-unavailable/.test(String((err && err.message) || err));
}
function fetchOptions(extra) {
  const opts = Object.assign({}, extra || {});
  if (typeof AbortSignal !== "undefined" && AbortSignal.timeout) opts.signal = AbortSignal.timeout(15000);
  return opts;
}
async function apiGet(path) {
  const base = backendUrl();
  let raw;
  try {
    raw = await tauriInvoke("backend_mutate", {
      method: "GET", backendUrl: base, path, bodyJson: null,
      apiToken: Secrets.api || null,
    });
  } catch (e) {
    if (!tauriUnavailable(e)) throw e;
  }
  if (raw !== undefined) {
    const d = parseJsonLoose(raw);
    if (d !== null) return d;
    throw new Error("backend returned a non-JSON response");
  }
  const headers = Secrets.api ? { Authorization: "Bearer " + Secrets.api } : {};
  const r = await fetch(base + path, fetchOptions({ headers }));
  if (!r.ok) throw new Error("HTTP " + r.status + " " + (await r.text()).slice(0, 300));
  return r.json();
}
async function apiMut(method, path, body) {
  const base = backendUrl();
  let raw;
  try {
    raw = await tauriInvoke("backend_mutate", {
      method, backendUrl: base, path,
      bodyJson: body === undefined ? null : JSON.stringify(body || {}),
      apiToken: Secrets.api || null,
    });
  } catch (e) {
    if (!tauriUnavailable(e)) throw e;
  }
  if (raw !== undefined) {
    const d = parseJsonLoose(raw);
    return d !== null ? d : raw;
  }
  const r = await fetch(base + path, fetchOptions({
    method,
    headers: authHeaders(),
    body: body === undefined ? undefined : JSON.stringify(body || {}),
  }));
  if (!r.ok) throw new Error("HTTP " + r.status + " " + (await r.text()).slice(0, 300));
  const t = await r.text();
  return t ? parseJsonLoose(t) : {};
}

/* ---------- connection ---------- */
async function checkBackend(showToast) {
  const pill = $("backend-pill"), conn = $("conn-status"), sc = $("settings-conn");
  const set = (ok, label) => {
    if (pill) { pill.textContent = "backend: " + label; pill.className = "badge " + (ok ? "ok" : "err"); }
    if (conn) conn.innerHTML = "Backend: <b>" + escHtml(label) + "</b>";
    if (sc) { sc.textContent = label; sc.className = "badge " + (ok ? "ok" : "err"); }
  };
  try {
    const base = backendUrl();
    const data = await apiGet("/health");
    const mode = (data && (data.store || data.mode)) || "up";
    set(true, "online (" + mode + ")");
    if (showToast) toastOk("Backend reachable", base + " · store=" + mode);
    return true;
  } catch (e) {
    set(false, "offline");
    if (showToast) toastErr("Backend unreachable", String(e).slice(0, 200));
    return false;
  }
}

/* ---------- model config ---------- */
function selectedModel() {
  const v = $("model").value;
  if (v === "custom") return $("custom-model").value.trim() || "openrouter/thinkingmachines/inkling-2-small:free";
  return v;
}
async function loadConfig() {
  try {
    const cfg = await tauriInvoke("get_config");
    $("cfg-path").textContent = "config: " + cfg.path;
    $("raw").textContent = cfg.raw;
    const known = Array.from($("model").options).map((o) => o.value);
    if (known.includes(cfg.model)) { $("model").value = cfg.model; $("custom-wrap").classList.add("hidden"); }
    else { $("model").value = "custom"; $("custom-wrap").classList.remove("hidden"); $("custom-model").value = cfg.model; }
    $("fallbacks").value = (cfg.fallback_models || []).join(", ");
    $("max-tokens").value = cfg.max_model_tokens || cfg.custom_model_max_tokens || "200000";
    $("effort").value = cfg.reasoning_effort || "low";
    $("auto-describe").checked = !!cfg.auto_describe;
    $("auto-review").checked = !!cfg.auto_review;
    $("auto-improve").checked = !!cfg.auto_improve;
    $("out").textContent = "Loaded config.";
  } catch (e) { $("out").textContent = "Load failed: " + e; toastErr("Config load failed", String(e).slice(0, 160)); }
}
async function saveConfig() {
  const payload = {
    model: selectedModel(),
    fallback_models: $("fallbacks").value.split(",").map((s) => s.trim()).filter(Boolean),
    max_model_tokens: $("max-tokens").value.trim() || "200000",
    custom_model_max_tokens: $("max-tokens").value.trim() || "200000",
    reasoning_effort: $("effort").value,
    auto_describe: $("auto-describe").checked,
    auto_review: $("auto-review").checked,
    auto_improve: $("auto-improve").checked,
  };
  try {
    const p = await tauriInvoke("save_config", { cfg: payload });
    $("out").textContent = "Saved to " + p;
    toastOk("Config saved", String(p));
    loadConfig();
  } catch (e) { $("out").textContent = "Save failed: " + e; toastErr("Save failed", String(e).slice(0, 200)); }
}

/* ---------- review actions ---------- */
function prUrl() { return $("pr-url").value.trim(); }
function reviewCmd() {
  const c = $("cmd").value;
  if (c === "ask") { const q = $("ask-q").value.trim() || "What does this PR change?"; return 'ask "' + q + '"'; }
  return c;
}

/* ---------- GitHub auth (memory/session only) ---------- */
let oauthCancel = false;
function setGhState(on, login) {
  const b = $("gh-state");
  if (b) { b.textContent = on ? ("connected" + (login ? " · " + login : "")) : "not connected"; b.className = "badge " + (on ? "ok" : ""); }
}
async function oauthStart() {
  const clientId = $("oauth-client").value.trim();
  if (!clientId) { toastErr("OAuth", "Enter the OAuth App Client ID first."); return; }
  oauthCancel = false;
  const btn = $("btn-oauth-start");
  if (btn) btn.disabled = true;
  try {
    try { localStorage.setItem("pragent.oauth_client", clientId); } catch (_) {}
    const d = await tauriInvoke("github_device_start", { clientId });
    $("oauth-code").classList.remove("hidden");
    $("oauth-usercode").textContent = d.user_code;
    $("btn-oauth-cancel").classList.remove("hidden");
    $("btn-oauth-open").classList.remove("hidden");
    $("btn-oauth-open").onclick = () => openUrl(d.verification_uri);
    // 1-click: open the verification page immediately via Tauri opener.
    try { await openUrl(d.verification_uri); } catch (_) {}
    $("oauth-out").textContent = "Code shown — approve on GitHub, waiting… (expires in ~" + Math.round((d.expires_in || 900) / 60) + " min)";
    toastOk("GitHub login started", "Approve code " + d.user_code + " on GitHub.");
    let interval = Math.max(5, d.interval || 5);
    while (!oauthCancel) {
      let r;
      try {
        r = await tauriInvoke("github_device_poll", {
          clientId, deviceCode: d.device_code, intervalSecs: interval, maxWaitSecs: 115,
        });
      } catch (e) {
        const msg = String(e);
        if (/pending|slow_down/i.test(msg)) continue;
        throw e;
      }
      if (oauthCancel) break;
      if (r && r.interval) interval = Math.max(interval, Number(r.interval) || interval);
      if (r && r.status === "authorized") {
        setSecret("github", r.access_token);
        syncSecretInputs();
        setGhState(true, r.login);
        ghSetState(true, r.login);
        const ghSection = $("gh-repo-section");
        if (ghSection) ghSection.classList.remove("hidden");
        $("oauth-code").classList.add("hidden");
        $("btn-oauth-cancel").classList.add("hidden");
        $("oauth-out").textContent = "Authenticated as " + (r.login || "(unknown login)") + ". Token in memory/session only.";
        toastOk("GitHub connected", r.login ? "Signed in as " + r.login : "Token stored in session.");
        loadReposIntoPicker();
        return;
      }
    }
    $("oauth-out").textContent = "Login cancelled. Token discarded.";
  } catch (e) {
    $("oauth-out").textContent = "OAuth failed: " + e;
    toastErr("GitHub OAuth failed", String(e).slice(0, 220));
  } finally {
    if (btn) btn.disabled = false;
  }
}
async function oauthPat() {
  const t = $("oauth-pat").value.trim();
  if (!t) { toastErr("PAT", "Paste a token first."); return; }
  try {
    const login = await tauriInvoke("github_login", { token: t });
    setSecret("github", t);
    $("oauth-pat").value = "";
    syncSecretInputs();
    setGhState(true, login);
    ghSetState(true, login);
    const ghSection = $("gh-repo-section");
    if (ghSection) ghSection.classList.remove("hidden");
    $("oauth-out").textContent = "Authenticated as " + (login || "(unknown login)") + ". Token in memory/session only.";
    toastOk("GitHub connected", login ? "Signed in as " + login : "PAT stored in session.");
    loadReposIntoPicker();
  } catch (e) { $("oauth-out").textContent = "PAT rejected: " + e; toastErr("PAT rejected", String(e).slice(0, 200)); }
}

/* ---------- repo picker (per-agent multi-select) ---------- */
const pickedRepos = new Set();
function currentRepos() { return Array.from(pickedRepos); }
function renderRepoPicker(remote) {
  const box = $("agent-repos-list");
  if (!box) return;
  const all = Array.from(new Set([...currentRepos(), ...((remote || []).map((r) => r.full_name).filter(Boolean))])).sort();
  box.innerHTML = "";
  if (!all.length) {
    box.innerHTML = '<p class="hint">No repos yet — connect GitHub or add owner/repo below.</p>';
    return;
  }
  for (const name of all) {
    const label = document.createElement("label");
    label.className = "repo-check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = pickedRepos.has(name);
    cb.addEventListener("change", () => {
      if (cb.checked) pickedRepos.add(name); else pickedRepos.delete(name);
      syncBoxReposHint();
    });
    const sp = document.createElement("span");
    sp.textContent = name;
    label.appendChild(cb);
    label.appendChild(sp);
    box.appendChild(label);
  }
}
function syncBoxReposHint() {
  const el = $("box-repos");
  if (el && !el.value.trim() && pickedRepos.size) el.value = currentRepos().join(", ");
}
async function loadReposIntoPicker() {
  if (!Secrets.github) { toastErr("GitHub", "Connect GitHub first (Settings)."); renderRepoPicker([]); return; }
  $("agents-out").textContent = "Loading repos…";
  try {
    const raw = await tauriInvoke("github_list_repos", { token: Secrets.github });
    const arr = parseJsonLoose(raw) || [];
    const repos = Array.isArray(arr) ? arr.map((r) => ({ full_name: r.full_name || r.fullName || "" })) : [];
    renderRepoPicker(repos);
    $("agents-out").textContent = "Loaded " + repos.length + " repo(s). Tick to target.";
    toastOk("Repos loaded", repos.length + " repositories.");
  } catch (e) { $("agents-out").textContent = "Repo load failed: " + e; toastErr("Repo load failed", String(e).slice(0, 200)); }
}

/* ---------- agents CRUD ---------- */
function renderAgents(data, preferredId) {
  const sel = $("agent-select");
  if (!sel) return;
  const list = Array.isArray(data) ? data : (data.agents || []);
  const keep = preferredId !== undefined ? String(preferredId || "") : (sel.value || "");
  sel.innerHTML = '<option value="">— new agent —</option>';
  for (const a of list) {
    const o = document.createElement("option");
    o.value = a.id || "";
    o.textContent = (a.name || a.id || "agent") + (a.id ? " (" + a.id.slice(0, 8) + ")" : "");
    sel.appendChild(o);
  }
  if (keep && Array.from(sel.options).some((o) => o.value === keep)) sel.value = keep;
  const c = $("agents-count");
  if (c) c.textContent = list.length + " agent(s)";
  return list;
}
async function refreshAgents(preferredId) {
  $("agents-out").textContent = "Loading agents…";
  try {
    const data = await apiGet("/agents");
    const list = renderAgents(data, preferredId) || [];
    $("agents-out").textContent = "Loaded " + list.length + " agent(s).";
  } catch (e) { $("agents-out").textContent = "Refresh failed (is backend up at " + backendUrl() + "?): " + e; toastErr("Agents refresh failed", String(e).slice(0, 200)); }
}
function fillAgentForm(a) {
  $("agent-name").value = a.name || "";
  $("agent-prompt").value = a.system_prompt || "";
  $("agent-trigger").value = a.trigger || "on_commit";
  $("agent-label").value = a.trigger_label || "";
  $("agent-mode").value = a.runtime_mode || "review_only";
  pickedRepos.clear();
  for (const r of (a.target_repos || [])) pickedRepos.add(r);
  renderRepoPicker([]);
  $("agent-label-wrap").style.display = $("agent-trigger").value === "on_label" ? "" : "none";
}
async function saveAgent() {
  const sel = $("agent-select");
  const id = sel && sel.value ? sel.value : "";
  const body = {
    name: $("agent-name").value.trim(),
    system_prompt: $("agent-prompt").value.trim(),
    target_repos: currentRepos(),
    trigger: $("agent-trigger").value,
    trigger_label: $("agent-trigger").value === "on_label" ? ($("agent-label").value.trim() || null) : null,
    runtime_mode: $("agent-mode").value,
  };
  if (!body.name) { toastErr("Agent", "Enter an agent name first."); return; }
  if (body.trigger === "on_label" && !body.trigger_label) {
    toastErr("Agent", "Enter the label name for an on_label agent.");
    $("agent-label").focus();
    return;
  }
  $("agents-out").textContent = "Saving…";
  try {
    const saved = id ? await apiMut("PUT", "/agents/" + encodeURIComponent(id), body)
                      : await apiMut("POST", "/agents", body);
    const savedId = saved && saved.id;
    $("agents-out").textContent = "Saved agent " + (savedId || body.name);
    toastOk("Agent saved", savedId || body.name);
    await refreshAgents(savedId || undefined);
  } catch (e) { $("agents-out").textContent = "Save failed: " + e; toastErr("Agent save failed", String(e).slice(0, 220)); }
}
async function deleteAgent() {
  const sel = $("agent-select");
  const id = sel && sel.value ? sel.value : "";
  if (!id) { toastErr("Agent", "Select an existing agent to delete."); return; }
  $("agents-out").textContent = "Deleting…";
  try {
    await apiMut("DELETE", "/agents/" + encodeURIComponent(id));
    $("agents-out").textContent = "Deleted.";
    toastOk("Agent deleted", id.slice(0, 12));
    fillAgentForm({});
    await refreshAgents("");
  } catch (e) { $("agents-out").textContent = "Delete failed: " + e; toastErr("Delete failed", String(e).slice(0, 200)); }
}

/* ---------- runs ---------- */
function runStatusClass(s) {
  s = String(s || "").toLowerCase();
  if (/approv|merged|done|success|completed/.test(s)) return "ok";
  if (/fail|error/.test(s)) return "err";
  if (/wait|pend|queued|running/.test(s)) return "pending";
  return "";
}
let lastRuns = [];
function renderRuns(list) {
  const box = $("runs-list");
  if (!box) return;
  box.innerHTML = "";
  lastRuns = Array.isArray(list) ? list : [];
  if (!lastRuns.length) { box.innerHTML = '<p class="hint">No runs yet.</p>'; return; }
  for (const r of lastRuns) {
    const div = document.createElement("div");
    div.className = "run-item";
    const id = r.run_id || r.id || "";
    const status = r.status || "unknown";
    const repo = String(r.repo || r.target_repo || "?");
    const pr = String(r.pr_number || r.pr || "?");
    const provider = String(r.provider || r.runtime_mode || r.mode || "?");
    const cpu = String(r.cpu_hours ?? r.cpu_seconds ?? "?");
    const head = document.createElement("div");
    head.className = "run-head";
    const strong = document.createElement("strong");
    strong.textContent = repo + " #" + pr;
    const pill = document.createElement("span");
    pill.className = "pill " + runStatusClass(status);
    pill.textContent = status;
    const meta = document.createElement("span");
    meta.className = "run-meta";
    meta.textContent = provider + " · " + cpu + " cpu";
    head.appendChild(strong); head.appendChild(pill); head.appendChild(meta);
    div.appendChild(head);
    const logs = String(r.logs || r.log || r.summary || "").slice(0, 900);
    if (logs) {
      const pre = document.createElement("pre");
      pre.className = "out run-logs";
      pre.textContent = logs;
      div.appendChild(pre);
    }
    const diff = String(r.diff || "");
    if (diff) {
      const det = document.createElement("details");
      det.className = "diff";
      const sum = document.createElement("summary");
      sum.textContent = "diff (" + diff.length + " chars)";
      const dpre = document.createElement("pre");
      dpre.textContent = diff.slice(0, 6000);
      det.appendChild(sum); det.appendChild(dpre);
      div.appendChild(det);
    }
    const row = document.createElement("div");
    row.className = "row wrap";
    if (diff) {
      const v = document.createElement("button");
      v.className = "btn btn-ghost btn-sm";
      v.type = "button";
      v.textContent = "View diff";
      v.addEventListener("click", () => openModal(id));
      row.appendChild(v);
    }
    if (status === "queued" && id) {
      const a = document.createElement("button");
      a.className = "btn btn-outline btn-sm";
      a.type = "button";
      a.textContent = "Approve";
      a.addEventListener("click", async () => {
        a.disabled = true;
        try {
          await apiMut("POST", "/runs/" + encodeURIComponent(id) + "/approve", {});
          toastOk("Run approved", id);
          syncRuns();
        } catch (e) { a.disabled = false; toastErr("Approve failed", String(e).slice(0, 200)); }
      });
      row.appendChild(a);
    }
    if (row.children.length) div.appendChild(row);
    box.appendChild(div);
  }
}
async function syncRuns() {
  $("runs-out").textContent = "Syncing runs…";
  try {
    const data = await apiGet("/runs?limit=50");
    const list = Array.isArray(data) ? data : (data.runs || []);
    renderRuns(list);
    $("runs-out").textContent = "Synced " + list.length + " run(s).";
  } catch (e) { $("runs-out").textContent = "Sync failed (is backend up at " + backendUrl() + "?): " + e; toastErr("Runs sync failed", String(e).slice(0, 200)); }
}
function openModal(id) {
  const r = lastRuns.find((x) => (x.run_id || x.id) === id);
  if (!r) return;
  $("modal-title").textContent = (r.repo || "?") + " #" + (r.pr_number || "?") + " — diff";
  $("modal-body").textContent = String(r.diff || "(no diff)").slice(0, 20000);
  $("modal-back").classList.add("open");
}
function closeModal() { $("modal-back").classList.remove("open"); }

/* ---------- quota ---------- */
function setBar(barId, wrapId, used, limit) {
  const bar = $(barId), wrap = $(wrapId);
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  if (bar) bar.style.width = pct + "%";
  if (wrap) wrap.classList.toggle("over", used >= limit);
}
async function fetchQuota() {
  try {
    const data = await apiGet("/quota");
    const box = (data && (data.upstash_box || data.box)) || {};
    const ver = (data && (data.vercel_sandbox || data.vercel)) || {};
    const bUsed = Number(box.used || 0), bLim = Number(box.limit || 18000);
    const vUsed = Number(ver.used || 0), vLim = Number(ver.limit || 18000);
    setBar("quota-box-bar", "quota-box-barwrap", bUsed, bLim);
    setBar("quota-vercel-bar", "quota-vercel-barwrap", vUsed, vLim);
    if ($("quota-box-num")) $("quota-box-num").textContent = bUsed.toLocaleString() + " / " + bLim.toLocaleString();
    if ($("quota-vercel-num")) $("quota-vercel-num").textContent = vUsed.toLocaleString() + " / " + vLim.toLocaleString();
    if ($("quota-box-sub")) $("quota-box-sub").textContent = "remaining " + (box.remaining != null ? box.remaining : Math.max(0, bLim - bUsed)) + " vCPU-s";
    if ($("quota-vercel-sub")) $("quota-vercel-sub").textContent = "remaining " + (ver.remaining != null ? ver.remaining : Math.max(0, vLim - vUsed)) + " vCPU-s";
    if ($("quota-text")) $("quota-text").textContent = "Updated just now · Box " + bUsed + "/" + bLim + " · Vercel " + vUsed + "/" + vLim;
    $("runs-out").textContent = "Quota updated.";
  } catch (e) { $("runs-out").textContent = "Quota failed: " + e; toastErr("Quota failed", String(e).slice(0, 200)); }
}

/* ---------- live stream ---------- */
let syncTimer = null;
function scheduleSyncRuns() {
  if (syncTimer) clearTimeout(syncTimer);
  syncTimer = setTimeout(() => { syncTimer = null; syncRuns(); }, 300);
}
function toggleStream() {
  const btn = $("btn-stream");
  if (evtSrc) {
    evtSrc.close(); evtSrc = null;
    if (btn) btn.textContent = "Live: off";
    $("runs-out").textContent = "Stream stopped.";
    return;
  }
  startStream(btn);
}
async function startStream(btn) {
  const base = backendUrl();
  // EventSource cannot set headers: exchange the API token for a short-lived,
  // single-use ticket so the long-lived token never appears in a URL.
  let streamUrl = base + "/events/stream";
  try {
    if (Secrets.api) {
      const t = await apiMut("POST", "/events/ticket", {});
      if (!t || !t.ticket) throw new Error("no stream ticket returned");
      streamUrl += "?ticket=" + encodeURIComponent(t.ticket);
    }
    evtSrc = new EventSource(streamUrl);
  } catch (e) { $("runs-out").textContent = "Stream failed: " + e; return; }
  if (btn) btn.textContent = "Live: on";
  $("runs-out").textContent = "Streaming live events…";
  evtSrc.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data && (data.type === "quota" || data.quota)) { fetchQuota(); return; }
      if (data && (data.repo || data.pr_number || data.status)) scheduleSyncRuns();
    } catch (_) { $("runs-out").textContent = "event: " + String(ev.data).slice(0, 200); }
  };
  evtSrc.onerror = () => {
    $("runs-out").textContent = "Stream error — check the backend at " + base +
      " and set ALLOWED_ORIGINS to include tauri://localhost,http://tauri.localhost,http://localhost:1420";
  };
}

/* ---------- integrations (1-click setup) ---------- */
const INT_DOTS = {
  github: "dot-github",
  vercel: "dot-vercel",
  upstash_box: "dot-box",
  redis: "dot-redis",
  openrouter: "dot-openrouter",
};
function setDot(id, state) {
  const el = $(id);
  if (!el) return;
  el.className = "dot " + (state === "ok" ? "ok" : state === "err" ? "err" : "warn");
}
function intOut(m) { const el = $("int-out"); if (el) el.textContent = m; }
function intBadge(m, ok) {
  const el = $("int-state");
  if (!el) return;
  el.textContent = m;
  el.className = "badge " + (ok === true ? "ok" : ok === false ? "err" : "");
}
function intStatusData(src) {
  // Normalise /config/status into {provider: configuredBool}.
  if (!src || typeof src !== "object") return {};
  return {
    github: !!(src.github && (src.github.token_set || src.github.webhook_secret_set)),
    githubFull: !!(src.github && src.github.token_set && src.github.webhook_secret_set),
    vercel: !!(src.vercel && src.vercel.configured),
    upstash_box: !!(src.upstash_box && src.upstash_box.configured),
    redis: !!(src.redis && src.redis.configured),
    openrouter: !!(src.openrouter && src.openrouter.configured),
  };
}
async function intRefresh() {
  intOut("Checking status…");
  let data = null;
  try {
    data = await apiGet("/config/status");
  } catch (e) {
    intOut("Status failed (is backend up at " + backendUrl() + "?): " + e);
    intBadge("unreachable", false);
    toastErr("Integrations", "Backend unreachable: " + String(e).slice(0, 160));
    return;
  }
  const s = intStatusData(data);
  const github = (data && data.github) || {};
  const vercel = (data && data.vercel) || {};
  const redis = (data && data.redis) || {};
  setDot(INT_DOTS.github, s.githubFull ? "ok" : "warn");
  setDot(INT_DOTS.vercel, s.vercel ? "ok" : "warn");
  setDot(INT_DOTS.upstash_box, s.upstash_box ? "ok" : "warn");
  setDot(INT_DOTS.redis, s.redis ? "ok" : "warn");
  setDot(INT_DOTS.openrouter, s.openrouter ? "ok" : "warn");
  const n = [s.githubFull, s.vercel, s.upstash_box, s.redis, s.openrouter].filter(Boolean).length;
  intBadge(n + "/5 connected", n === 5);
  const lines = [
    "github: " + (github.token_set ? "token set" : "token MISSING") + " · " + (github.webhook_secret_set ? "webhook secret set" : "webhook secret MISSING"),
    "vercel: " + (s.vercel ? "configured" + (vercel.team_set ? " (team scoped)" : "") : "not configured"),
    "upstash_box: " + (s.upstash_box ? "configured" : "not configured"),
    "redis: " + (s.redis
      ? "configured (mode=" + (redis.mode || "?") + ", source=" + (redis.source || "?") + ")"
      : "not configured — FastAPI Cloud injects REDIS_URL; for local/self-hosted save a Redis URL (or the Upstash REST pair) then Test"),
    "openrouter: " + (s.openrouter ? "configured" : "not configured"),
    "auth: " + (data && data.auth_required ? "BACKEND_API_TOKEN required" : "open (no BACKEND_API_TOKEN)"),
  ];
  intOut(lines.join("\n"));
}
async function intTest(provider) {
  intOut("Testing " + provider + "…");
  let res = null;
  try {
    res = await apiMut("POST", "/config/test", { provider });
  } catch (e) {
    intOut(provider + ": test failed: " + e);
    toastErr("Test failed", String(e).slice(0, 200));
    return;
  }
  const dotId = INT_DOTS[provider] || INT_DOTS[provider.replace("-", "_")];
  setDot(dotId, res && res.ok ? "ok" : "err");
  intOut(provider + ": " + ((res && res.details) || JSON.stringify(res)));
  if (res && res.ok) toastOk("Test passed", provider + " reachable.");
  else toastErr("Test failed", provider + ": " + ((res && res.details) || "check failed"));
}
// Save token inputs to backend/.env via the Tauri backend (never localStorage).
// pairs: [[ENV_KEY, inputId, sessionMirror|null], ...] — empty inputs are skipped.
async function intSave(pairs, label) {
  const todo = pairs
    .map(([key, id]) => [key, ($(id) ? $(id).value : "").trim()])
    .filter(([, v]) => v);
  if (!todo.length) { toastErr("Connect", "Enter a value first."); return; }
  intOut("Saving " + label + " to backend/.env…");
  try {
    const paths = new Set();
    for (const [key, val] of todo) {
      const p = await tauriInvoke("save_backend_env_var", { key, value: val });
      paths.add(p);
    }
    // Mirror session-usable secrets into memory+sessionStorage (same convention as Settings).
    for (const [key, val] of todo) {
      if (key === "GITHUB_TOKEN") {
        setSecret("github", val);
        // Reuse a token saved through the older Connect button in the 1-click card.
        ghSetState(true, "");
        const section = $("gh-repo-section");
        if (section) section.classList.remove("hidden");
      }
      if (key === "OPENROUTER_API_KEY") setSecret("openrouter", val);
      if (key === "UPSTASH_BOX_API_KEY") setSecret("box", val);
    }
    syncSecretInputs();
    if (todo.some(([key]) => key === "GITHUB_TOKEN")) ghLoadRepos();
    for (const [, id] of pairs) { const el = $(id); if (el) el.value = (id === "int-vercel-team" || id === "int-redis-url" || id === "int-redis-conn-url") ? el.value : ""; }
    // Clear even saved non-secret fields that hold sensitive-adjacent data? Keep URL/team visible.
    const savedKeys = todo.map(([k]) => k).join(", ");
    intOut("Saved " + savedKeys + " to " + Array.from(paths).join(", ") + ".\nRestart/redeploy the backend so it picks up the new values, then Test.");
    toastOk("Saved", savedKeys + " → backend/.env");
    ghKeyHint();
    intRefresh();
  } catch (e) {
    intOut("Save failed: " + e + "\n(Outside the desktop app? Set these env vars where the backend runs instead.)");
    toastErr("Save failed", String(e).slice(0, 220));
  }
}
function intHookUrl() {
  const base = ($("int-cloud-base") ? $("int-cloud-base").value : "").trim().replace(/\/+$/, "");
  const prev = $("int-hook-preview");
  const url = base ? base + "/webhooks/github" : "https://<cloud-app>/webhooks/github";
  if (prev) prev.textContent = url;
  return url;
}

/* ---------- GitHub 1-click repo setup ----------
 * OAuth token stays in Secrets.github (memory + sessionStorage) and is only
 * passed to the Rust commands. It is never put in localStorage, the DOM as
 * markup, or the progress log. The only way it reaches disk is the opt-in
 * "Save GitHub token to backend/.env" checkbox (Rust save_backend_env_var).
 */
let ghOauthCancel = false;
let ghVerificationUri = "";
let ghBatchRunning = false;
let ghBatchCancel = false;
const ghRepos = [];                    // {full_name, private, default_branch, admin}
const ghSelected = new Set();          // full_name
const ghStatus = new Map();            // full_name -> github_repo_status payload
const ghStatusInflight = new Set();
const ghFailed = new Set();            // setup failures awaiting retry
const ghConfiguring = new Set();
const ghRowEls = new Map();            // full_name -> row element (targeted updates)

function jsonObj(v) {
  if (typeof v === "string") {
    const p = parseJsonLoose(v);
    return p !== null ? p : null;
  }
  return v && typeof v === "object" ? v : null;
}
function ghErrSuffix(msg) {
  return msg ? " — " + String(msg).slice(0, 220) : "";
}
function ghOut(line) {
  const el = $("gh-setup-out");
  if (!el) return;
  const base = el.textContent && el.textContent !== "Configure log appears here." ? el.textContent + "\n" : "";
  el.textContent = base + line;
  el.scrollTop = el.scrollHeight;
}
function ghSetState(on, login) {
  const el = $("gh-oauth-state");
  if (!el) return;
  el.textContent = on ? (login ? "Connected as " + login : "Connected (session token)") : "Not connected";
  el.className = "badge" + (on ? " ok" : "");
}
function ghBaseUrl() {
  const v = ($("int-cloud-base") ? $("int-cloud-base").value : "").trim().replace(/\/+$/, "");
  return v || backendUrl();
}
function ghOpenrouterKey() {
  return (($("int-or-key") ? $("int-or-key").value : "").trim()) || Secrets.openrouter || "";
}
function ghHookHint() {
  const el = $("gh-hook-target");
  if (!el) return;
  const base = ghBaseUrl();
  let local = false;
  try {
    const u = new URL(base);
    local = /^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])$/i.test(u.hostname);
  } catch (_) {}
  el.textContent = "Webhook target: " + base + "/webhooks/github" +
    (local ? " — GitHub cannot deliver webhooks to localhost/127.0.0.1; use a public tunnel or a deployed URL." : "");
  el.className = "hint" + (local ? " gh-warn" : "");
}
function ghKeyHint() {
  const el = $("gh-or-hint");
  if (!el) return;
  el.textContent = ghOpenrouterKey() ? "(key ready)" : "(no OpenRouter key yet)";
}
function ghRandomSecret() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
function ghPersistClientId() {
  const el = $("gh-oauth-client");
  if (!el) return;
  const v = el.value.trim();
  try { localStorage.setItem("pragent.oauth_client", v); } catch (_) {}
  const other = $("oauth-client");
  if (other && other.value !== el.value) other.value = el.value;
}
function ghRestoreClientId() {
  const el = $("gh-oauth-client");
  if (!el || el.value) return;
  let v = "";
  try { v = localStorage.getItem("pragent.oauth_client") || ""; } catch (_) {}
  if (!v && $("oauth-client")) v = $("oauth-client").value.trim();
  if (v) el.value = v;
}

/* ----- device flow (separate cancel flag from the Settings flow) ----- */
function ghOauthCancelFlow() {
  ghOauthCancel = true;
  const out = $("gh-oauth-out");
  if (out) out.textContent = "Cancelling…";
  const btn = $("btn-gh-oauth-cancel");
  if (btn) btn.disabled = true;
}
async function ghOauthStart() {
  const el = $("gh-oauth-client");
  const other = $("oauth-client");
  const clientId = ((el ? el.value : "").trim() || (other ? other.value : "").trim());
  if (!clientId) {
    toastErr("GitHub sign-in", "Enter the OAuth App Client ID first (Device Flow must be enabled).");
    if (el) el.focus();
    return;
  }
  if (el) el.value = clientId;
  if (other) other.value = clientId;
  try { localStorage.setItem("pragent.oauth_client", clientId); } catch (_) {}
  ghOauthCancel = false;
  ghVerificationUri = "https://github.com/login/device";
  const startBtn = $("btn-gh-oauth-start");
  const cancelBtn = $("btn-gh-oauth-cancel");
  const codeBlock = $("gh-oauth-code");
  const userCode = $("gh-oauth-usercode");
  const out = $("gh-oauth-out");
  if (startBtn) startBtn.disabled = true;
  if (cancelBtn) { cancelBtn.classList.remove("hidden"); cancelBtn.disabled = false; }
  if (codeBlock) codeBlock.classList.remove("hidden");
  if (userCode) userCode.textContent = "…";
  if (out) out.textContent = "Starting GitHub device flow…";
  try {
    const d = jsonObj(await tauriInvoke("github_device_start", { clientId }));
    if (!d || !d.device_code || !d.user_code) throw new Error("device flow response missing device_code/user_code");
    if (ghOauthCancel) {
      if (out) out.textContent = "Login cancelled — no new token was stored.";
      return;
    }
    ghVerificationUri = String(d.verification_uri || "https://github.com/login/device");
    if (userCode) userCode.textContent = String(d.user_code);
    if (out) out.textContent = "Approve the code on GitHub — waiting… (expires in ~" +
      Math.round((Number(d.expires_in) || 900) / 60) + " min)";
    toastOk("GitHub login started", "Approve code " + d.user_code + " on GitHub.");
    try { await openUrl(ghVerificationUri); } catch (_) {}
    let interval = Math.max(5, Number(d.interval) || 5);
    while (!ghOauthCancel) {
      let r = null;
      try {
        r = jsonObj(await tauriInvoke("github_device_poll", {
          clientId, deviceCode: d.device_code, intervalSecs: interval, maxWaitSecs: 30,
        }));
      } catch (e) {
        if (ghOauthCancel) break;
        if (/pending|slow_down|authorization_pending/i.test(String(e))) {
          if (out) out.textContent = "Waiting for approval…";
          continue;
        }
        throw e;
      }
      if (ghOauthCancel) break;
      if (r && r.interval) interval = Math.max(interval, Number(r.interval) || interval);
      if (r && r.status === "authorized") {
        if (!r.access_token) throw new Error("GitHub did not return an access token");
        setSecret("github", r.access_token); // memory + sessionStorage only
        syncSecretInputs();
        ghSetState(true, r.login);
        setGhState(true, r.login);
        if (codeBlock) codeBlock.classList.add("hidden");
        if (cancelBtn) cancelBtn.classList.add("hidden");
        if (out) out.textContent = "Connected as " + (r.login || "GitHub user") + ".";
        if (userCode) userCode.textContent = "…";
        const section = $("gh-repo-section");
        if (section) section.classList.remove("hidden");
        toastOk("GitHub connected", r.login ? "Signed in as " + r.login : "Token stored in session.");
        ghLoadRepos();
        return;
      }
      if (r && r.status && r.status !== "pending" && r.status !== "authorized") {
        throw new Error("device flow " + r.status);
      }
      if (out) out.textContent = "Waiting for approval…";
    }
    if (out) out.textContent = "Login cancelled — no new token was stored.";
    if (userCode) userCode.textContent = "—";
  } catch (e) {
    if (codeBlock) codeBlock.classList.remove("hidden");
    if (userCode) userCode.textContent = "—";
    if (out) out.textContent = "Sign-in failed: " + String(e).slice(0, 300);
    toastErr("GitHub sign-in failed", String(e).slice(0, 220));
  } finally {
    if (startBtn) startBtn.disabled = false;
    if (cancelBtn) { cancelBtn.classList.add("hidden"); cancelBtn.disabled = false; }
  }
}

/* ----- repo list, selection, status cache ----- */
function ghFilteredRepos() {
  const q = ($("gh-repo-search") ? $("gh-repo-search").value : "").trim().toLowerCase();
  if (!q) return ghRepos.slice();
  return ghRepos.filter((r) => r.full_name.toLowerCase().includes(q));
}
function ghEmptyNote(box, text) {
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent = text;
  box.appendChild(p);
}
function ghStepPill(label, state, title) {
  const el = document.createElement("span");
  el.className = "step-pill " + (state || "");
  const mark = state === "ok" ? "✓" : state === "warn" || state === "err" ? "✕" : "—";
  el.textContent = label + " " + mark;
  if (title) el.title = String(title).slice(0, 300);
  return el;
}
function ghRowError(name) {
  const st = ghStatus.get(name);
  if (!st) return "";
  const errs = [];
  if (st.error) errs.push(String(st.error));
  for (const k of ["webhook", "workflow", "secrets"]) {
    if (st[k] && st[k].error) errs.push(k + ": " + String(st[k].error));
  }
  return errs.join(" · ").slice(0, 220);
}
function ghFillRowStatus(box, name) {
  if (!box) return;
  box.textContent = "";
  const st = ghStatus.get(name);
  const webhook = (st && st.webhook) || {};
  const workflow = (st && st.workflow) || {};
  const secrets = (st && st.secrets) || {};
  const generic = (st && st.error) || "";
  box.appendChild(ghStepPill("hook", st ? (webhook.configured ? "ok" : "warn") : "", webhook.error || generic));
  box.appendChild(ghStepPill("workflow", st ? (workflow.configured ? "ok" : "warn") : "",
    workflow.error || generic || (workflow.file ? "file: " + workflow.file : "")));
  let secTitle = secrets.error || generic;
  if (!secTitle && st && !secrets.configured) {
    const missing = [];
    if (!secrets.openrouter_api_key) missing.push("OPENROUTER_API_KEY");
    // LLM_API_KEY is only required by the OpenRabbit workflow.
    if (workflow.provider === "openrabbit" && !secrets.llm_api_key) missing.push("LLM_API_KEY");
    if (missing.length) secTitle = "missing repo secret(s): " + missing.join(", ");
  }
  box.appendChild(ghStepPill("secrets", st ? (secrets.configured ? "ok" : "warn") : "", secTitle));
  const overall = document.createElement("span");
  overall.className = "pill ok";
  overall.textContent = "configured ✓";
  if (!(st && st.configured)) overall.classList.add("hidden");
  box.appendChild(overall);
}
function ghUpdateRow(name) {
  const row = ghRowEls.get(name);
  if (!row) return;
  row.classList.toggle("selected", ghSelected.has(name));
  row.classList.toggle("configuring", ghConfiguring.has(name));
  row.classList.toggle("failed", ghFailed.has(name));
  ghFillRowStatus(row.querySelector(".repo-status"), name);
  const err = row.querySelector(".repo-err");
  if (err) {
    const msg = ghRowError(name);
    err.textContent = msg;
    err.classList.toggle("hidden", !msg);
  }
}
function ghBuildRow(r) {
  const name = r.full_name;
  const row = document.createElement("div");
  row.className = "repo-row";
  row.dataset.repo = name;
  row.classList.toggle("selected", ghSelected.has(name));
  row.classList.toggle("locked", r.admin === false);
  row.classList.toggle("configuring", ghConfiguring.has(name));
  row.classList.toggle("failed", ghFailed.has(name));

  const label = document.createElement("label");
  label.className = "repo-check";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = ghSelected.has(name);
  cb.disabled = r.admin === false;
  if (cb.disabled) {
    const why = "Admin access is required to configure webhooks, repo secrets and workflow files.";
    cb.title = why;
    label.title = why;
  }
  cb.addEventListener("change", () => {
    if (cb.checked) ghSelected.add(name); else ghSelected.delete(name);
    ghUpdateRow(name);
    ghUpdateConfigureBtn();
    if (cb.checked) ghLoadStatus(name, false);
  });
  const nameEl = document.createElement("span");
  nameEl.className = "repo-name";
  nameEl.textContent = name;
  label.appendChild(cb);
  label.appendChild(nameEl);
  if (r.private) {
    const priv = document.createElement("span");
    priv.className = "badge";
    priv.textContent = "private";
    label.appendChild(priv);
  }
  if (r.default_branch) {
    const br = document.createElement("span");
    br.className = "hint";
    br.textContent = "default: " + r.default_branch;
    label.appendChild(br);
  }
  row.appendChild(label);

  const status = document.createElement("div");
  status.className = "repo-status";
  ghFillRowStatus(status, name);
  row.appendChild(status);

  const err = document.createElement("span");
  err.className = "repo-err hint hidden";
  row.appendChild(err);
  return row;
}
function ghRenderRepos() {
  const box = $("gh-repo-list");
  const count = $("gh-repo-count");
  if (!box) return;
  const visible = ghFilteredRepos();
  if (count) count.textContent = visible.length + " of " + ghRepos.length + " repos";
  box.textContent = "";
  ghRowEls.clear();
  if (!ghRepos.length) { ghEmptyNote(box, "No repositories loaded — click Refresh repos."); return; }
  if (!visible.length) { ghEmptyNote(box, "No repositories match the filter."); return; }
  for (const r of visible) {
    const row = ghBuildRow(r);
    ghRowEls.set(r.full_name, row);
    box.appendChild(row);
  }
}
function ghUpdateConfigureBtn() {
  const btn = $("btn-gh-configure");
  if (btn) {
    btn.textContent = "Configure selected (" + ghSelected.size + ")";
    btn.disabled = ghBatchRunning || ghSelected.size === 0;
  }
  const retry = $("btn-gh-retry-failed");
  if (retry) {
    retry.textContent = "Retry failed (" + ghFailed.size + ")";
    retry.classList.toggle("hidden", ghBatchRunning || ghFailed.size === 0);
  }
}
async function ghLoadRepos() {
  if (!Secrets.github) { toastErr("GitHub", "Sign in with GitHub first."); return; }
  const section = $("gh-repo-section");
  if (section) section.classList.remove("hidden");
  const btn = $("btn-gh-repos-refresh");
  const box = $("gh-repo-list");
  if (btn) btn.disabled = true;
  if (box) { box.textContent = ""; ghEmptyNote(box, "Loading repositories…"); }
  try {
    const raw = await tauriInvoke("github_list_repos", { token: Secrets.github });
    if (!Secrets.github) return; // session was cleared while loading
    const parsed = Array.isArray(raw) ? raw : (jsonObj(raw) || []);
    const arr = Array.isArray(parsed) ? parsed : [];
    ghRepos.length = 0;
    for (const r of arr) {
      const name = String((r && r.full_name) || "").trim();
      if (!name) continue;
      ghRepos.push({
        full_name: name,
        private: !!(r && r.private),
        default_branch: (r && r.default_branch) || "",
        admin: !(r && r.admin === false),
      });
    }
    const names = new Set(ghRepos.map((r) => r.full_name));
    const selectable = new Set(ghRepos.filter((r) => r.admin !== false).map((r) => r.full_name));
    for (const n of Array.from(ghSelected)) if (!names.has(n) || !selectable.has(n)) ghSelected.delete(n);
    for (const n of Array.from(ghStatus.keys())) if (!names.has(n)) ghStatus.delete(n);
    for (const n of Array.from(ghFailed)) if (!names.has(n)) ghFailed.delete(n);
    ghRenderRepos();
    ghUpdateConfigureBtn();
    ghOut("Loaded " + ghRepos.length + " repositories.");
    toastOk("Repos loaded", ghRepos.length + " repositories.");
  } catch (e) {
    if (box) { box.textContent = ""; ghEmptyNote(box, "Repo load failed: " + String(e).slice(0, 300)); }
    ghOut("Repo load failed: " + String(e));
    toastErr("Repo load failed", String(e).slice(0, 200));
  } finally {
    if (btn) btn.disabled = false;
  }
}
async function ghLoadStatus(name, force) {
  if (!Secrets.github || !name) return;
  if (force) ghStatus.delete(name);
  else if (ghStatus.has(name)) return;
  if (ghStatusInflight.has(name)) return;
  ghStatusInflight.add(name);
  try {
    const raw = await tauriInvoke("github_repo_status", { token: Secrets.github, repo: name });
    if (!Secrets.github) return; // session was cleared while loading
    const st = jsonObj(raw);
    if (st && !Array.isArray(st)) ghStatus.set(name, st);
    else ghStatus.set(name, { repo: name, error: "unexpected status response" });
  } catch (e) {
    ghStatus.set(name, { repo: name, error: String(e).slice(0, 300) });
  } finally {
    ghStatusInflight.delete(name);
    ghUpdateRow(name);
  }
}
async function ghLoadStatusMany(names, force) {
  const queue = names.slice();
  const workers = Array.from({ length: Math.min(4, queue.length) }, async () => {
    while (queue.length) await ghLoadStatus(queue.shift(), !!force);
  });
  await Promise.all(workers);
}
async function ghCheckStatus() {
  if (!Secrets.github) { toastErr("GitHub status", "Sign in with GitHub first."); return; }
  const btn = $("btn-gh-status-refresh");
  const selected = Array.from(ghSelected);
  const targets = selected.length
    ? selected
    : ghFilteredRepos().filter((r) => r.admin !== false).slice(0, 25).map((r) => r.full_name);
  if (!targets.length) { toastErr("GitHub status", "No repositories to check."); return; }
  if (btn) btn.disabled = true;
  try {
    await ghLoadStatusMany(targets, true);
    const ok = targets.filter((n) => ghStatus.get(n) && ghStatus.get(n).configured).length;
    toastOk("Status checked", ok + " of " + targets.length + " configured.");
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ----- configure batch (sequential, failures never abort the batch) ----- */
async function ghSaveEnv(key, value) {
  try {
    const p = await tauriInvoke("save_backend_env_var", { key, value });
    ghOut("[env] " + key + " saved to " + p);
  } catch (e) {
    // GitHub-side setup does not depend on local .env; surface and continue.
    ghOut("[env] " + key + " save failed: " + String(e).slice(0, 220));
    toastErr("Env save failed", key + ": " + String(e).slice(0, 180));
  }
}
async function ghUpsertAgent(repos) {
  const name = ($("gh-agent-name") ? $("gh-agent-name").value.trim() : "") || "pr-agent-control";
  if (!repos.length) return;
  ghOut("[agent] syncing backend agent \"" + name + "\"…");
  try {
    const data = await apiGet("/agents");
    const rawList = Array.isArray(data) ? data : ((data && data.agents) || []);
    const list = Array.isArray(rawList) ? rawList : [];
    const existing = list.find((a) => a && a.name === name);
    const uniq = Array.from(new Set(repos));
    if (existing && existing.id) {
      const currentTargets = Array.isArray(existing.target_repos) ? existing.target_repos : [];
      const body = Object.assign({}, existing, {
        target_repos: Array.from(new Set([...currentTargets, ...uniq])),
        trigger: "on_commit",
        runtime_mode: "review_only",
      });
      const saved = await apiMut("PUT", "/agents/" + encodeURIComponent(existing.id), body);
      ghOut("[agent] updated " + ((saved && saved.id) || existing.id) + " (target_repos now " + body.target_repos.length + ")");
    } else {
      const saved = await apiMut("POST", "/agents", {
        name, system_prompt: "", target_repos: uniq, trigger: "on_commit", runtime_mode: "review_only",
      });
      ghOut("[agent] created " + ((saved && saved.id) || name));
    }
    toastOk("Agent synced", name);
    try { refreshAgents(); } catch (_) {}
  } catch (e) {
    // Agent sync is a convenience: never fail the GitHub setup because of it.
    ghOut("[agent] failed: " + String(e).slice(0, 300));
    toastErr("Agent sync failed", String(e).slice(0, 200));
  }
}
async function ghConfigure(retryOnly) {
  if (ghBatchRunning) return;
  const token = Secrets.github;
  if (!token) { toastErr("GitHub setup", "Sign in with GitHub first."); return; }
  const base = ghBaseUrl();
  let parsedBase = null;
  try { parsedBase = new URL(base); } catch (_) {}
  if (!parsedBase || !/^https?:$/.test(parsedBase.protocol)) {
    toastErr("GitHub setup", "Set a valid http(s) base URL in the GitHub webhook URL card (or Settings backend URL).");
    return;
  }
  const repos = (retryOnly ? Array.from(ghFailed) : Array.from(ghSelected)).filter(Boolean);
  if (!repos.length) {
    toastErr("GitHub setup", retryOnly ? "No failed repositories to retry." : "Select at least one repository.");
    return;
  }
  let secret = ($("gh-webhook-secret") ? $("gh-webhook-secret").value : "").trim();
  if (!secret) {
    secret = ghRandomSecret();
    if ($("gh-webhook-secret")) $("gh-webhook-secret").value = secret;
    ghOut("[config] generated a 32-byte webhook secret (hex).");
  }
  const orKey = ghOpenrouterKey();
  if (!orKey) {
    toastErr("GitHub setup", "Add the OpenRouter API key in the OpenRouter row above first.");
    return;
  }
  const provider = ($("gh-workflow-provider") ? $("gh-workflow-provider").value : "pr-agent") || "pr-agent";
  if (provider !== "pr-agent" && provider !== "openrabbit") {
    toastErr("GitHub setup", "Choose a workflow provider (pr-agent or openrabbit).");
    return;
  }
  const successful = [];
  const batchFailed = [];
  const saveSecret = !!($("gh-secret-save") && $("gh-secret-save").checked);
  const saveToken = !!($("gh-save-token") && $("gh-save-token").checked);

  ghBatchRunning = true;
  ghBatchCancel = false;
  ghUpdateConfigureBtn();
  if ($("btn-gh-repos-refresh")) $("btn-gh-repos-refresh").disabled = true;
  if ($("btn-gh-status-refresh")) $("btn-gh-status-refresh").disabled = true;
  try {
    ghOut("=== Setup started: " + repos.length + " repo(s) · provider=" + provider + " · webhook base=" + base + " ===");
    if (saveSecret) {
      await ghSaveEnv("GITHUB_WEBHOOK_SECRET", secret);
      ghOut("[env] restart the backend to pick up .env changes.");
    }
    if (saveToken) await ghSaveEnv("GITHUB_TOKEN", token);

    for (const repo of repos) {
      if (ghBatchCancel) {
        ghOut("[setup] cancelled — session secrets were cleared; remaining repos not touched.");
        break;
      }
      ghConfiguring.add(repo);
      ghUpdateRow(repo);
      ghOut("[" + repo + "] starting…");
      let ok = false;
      try {
        const raw = await tauriInvoke("github_setup_repo", {
          token, repo, backendUrl: base, webhookSecret: secret,
          openrouterKey: orKey, workflowProvider: provider,
        });
        const r = jsonObj(raw);
        if (!r) throw new Error("unexpected setup response");
        ghOut("[" + repo + "] webhook: " + (r.webhook || "unknown") + ghErrSuffix(r.errors && r.errors.webhook));
        ghOut("[" + repo + "] secrets: " + (r.secrets || "unknown") + ghErrSuffix(r.errors && r.errors.secrets));
        ghOut("[" + repo + "] workflow: " + (r.workflow || "unknown") +
          ghErrSuffix(r.errors && r.errors.workflow) +
          (r.workflow_path ? " (" + r.workflow_path + ")" : ""));
        if (r.webhook_url) ghOut("[" + repo + "] webhook target: " + r.webhook_url);
        ok = !!r.ok;
        if (ok) {
          ghFailed.delete(repo);
          successful.push(repo);
          ghOut("[" + repo + "] done.");
        } else {
          ghFailed.add(repo);
          batchFailed.push(repo);
          ghOut("[" + repo + "] failed — see per-step statuses above.");
        }
      } catch (e) {
        ghFailed.add(repo);
        batchFailed.push(repo);
        ghOut("[" + repo + "] error: " + String(e).slice(0, 300));
      } finally {
        ghConfiguring.delete(repo);
      }
      // Refresh this repo's badges once; a status failure must not abort the batch.
      await ghLoadStatus(repo, true);
      ghUpdateRow(repo);
    }

    ghOut("=== Done: " + successful.length + " succeeded, " + batchFailed.length + " failed ===");
    if (successful.length) toastOk("GitHub setup", successful.length + " repo(s) configured.");
    if (batchFailed.length) toastErr("GitHub setup", batchFailed.length + " repo(s) failed — use Retry failed.");
    if (!ghBatchCancel && $("gh-agent-create") && $("gh-agent-create").checked && successful.length) {
      await ghUpsertAgent(successful);
    }
  } catch (e) {
    ghOut("[fatal] " + String(e).slice(0, 300));
    toastErr("GitHub setup", String(e).slice(0, 220));
  } finally {
    ghBatchRunning = false;
    ghConfiguring.clear();
    for (const n of repos) ghUpdateRow(n);
    ghUpdateConfigureBtn();
    if ($("btn-gh-repos-refresh")) $("btn-gh-repos-refresh").disabled = false;
    if ($("btn-gh-status-refresh")) $("btn-gh-status-refresh").disabled = false;
  }
}
function ghResetUi() {
  ghOauthCancel = true;
  ghVerificationUri = "";
  ghBatchCancel = true;
  if ($("gh-webhook-secret")) $("gh-webhook-secret").value = "";
  if ($("gh-oauth-code")) $("gh-oauth-code").classList.add("hidden");
  if ($("gh-oauth-usercode")) $("gh-oauth-usercode").textContent = "…";
  if ($("gh-oauth-out")) $("gh-oauth-out").textContent = "";
  const cancel = $("btn-gh-oauth-cancel");
  if (cancel) { cancel.classList.add("hidden"); cancel.disabled = false; }
  if ($("gh-setup-out")) $("gh-setup-out").textContent = "Configure log appears here.";
  if ($("gh-repo-search")) $("gh-repo-search").value = "";
  if ($("gh-repo-list")) $("gh-repo-list").textContent = "";
  if ($("gh-repo-count")) $("gh-repo-count").textContent = "0 of 0 repos";
  if ($("gh-repo-section")) $("gh-repo-section").classList.add("hidden");
  ghRepos.length = 0;
  ghSelected.clear();
  ghStatus.clear();
  ghStatusInflight.clear();
  ghFailed.clear();
  ghConfiguring.clear();
  ghRowEls.clear();
  if ($("gh-repo-list")) ghEmptyNote($("gh-repo-list"), "No repositories loaded — click Refresh repos.");
  ghSetState(false, "");
  ghKeyHint();
  ghUpdateConfigureBtn();
}

/* ---------- providers / box ---------- */
function boxArgs(statusOnly) {
  const boxKey = $("box-key").value.trim() || Secrets.box;
  if (!boxKey) { $("box-out").textContent = "Enter UPSTASH_BOX_API_KEY first."; toastErr("Box", "Enter UPSTASH_BOX_API_KEY first."); return null; }
  if (!statusOnly) {
    if (!($("gh-key").value.trim() || Secrets.github)) { $("box-out").textContent = "Enter GITHUB_TOKEN first (Settings → Connect)."; toastErr("Box", "Connect GitHub first."); return null; }
    if (!(Secrets.openrouter || $("or-key").value.trim())) { $("box-out").textContent = "Enter OPENROUTER_API_KEY in Review first."; toastErr("Box", "Enter OPENROUTER_API_KEY first."); return null; }
    if (!$("box-repos").value.trim() && !pickedRepos.size) { $("box-out").textContent = "Enter at least one repo."; toastErr("Box", "Enter at least one repo."); return null; }
  }
  return {
    boxKey,
    githubToken: $("gh-key").value.trim() || Secrets.github,
    openrouterKey: $("or-key").value.trim() || Secrets.openrouter,
    repos: $("box-repos").value.trim() || currentRepos().join(", "),
    boxName: $("box-name").value.trim() || "pr-agent-runner",
    cron: $("box-cron").value.trim() || "*/15 * * * *",
    model: (function () { try { return selectedModel(); } catch (_) { return "openrouter/thinkingmachines/inkling-2-small:free"; } })(),
    statusOnly,
  };
}

/* ---------- wiring ---------- */
function on(id, fn) { const el = $(id); if (el) el.addEventListener("click", fn); }

function wire() {
  $$(".nav-btn").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

  on("theme-toggle", () => {
    const html = document.documentElement;
    const next = html.getAttribute("data-theme") === "light" ? "dark" : "light";
    html.setAttribute("data-theme", next);
    try { localStorage.setItem("pragent.theme", next); } catch (_) {}
  });

  // secret inputs -> memory + sessionStorage (two-way synced pairs)
  const bindSecret = (ids, name) => {
    for (const id of ids) {
      const el = $(id);
      if (!el) continue;
      el.addEventListener("input", () => {
        setSecret(name, el.value);
        for (const other of ids) {
          if (other !== id) { const o = $(other); if (o && o.value !== el.value) o.value = el.value; }
        }
      });
    }
  };
  bindSecret(["or-key", "sett-or"], "openrouter");
  bindSecret(["gh-key", "sett-gh"], "github");
  bindSecret(["box-key"], "box");
  bindSecret(["api-token"], "api");
  on("btn-clear-secrets", clearSecrets);
  on("btn-forget", clearSecrets);

  $("backend-url").addEventListener("change", () => { backendUrl(); ghHookHint(); checkBackend(false); });
  on("btn-backend-test", async () => {
    $("backend-out").textContent = "Testing…";
    const ok = await checkBackend(true);
    $("backend-out").textContent = ok ? "OK — " + backendUrl() : "Unreachable — is the backend running at " + backendUrl() + "?";
  });
  on("btn-reload-all", async () => { await checkBackend(false); loadConfig(); refreshAgents(); syncRuns(); fetchQuota(); });

  on("btn-load", loadConfig);
  on("btn-save", saveConfig);
  $("model").addEventListener("change", () => $("custom-wrap").classList.toggle("hidden", $("model").value !== "custom"));
  $("cmd").addEventListener("change", () => $("ask-wrap").classList.toggle("hidden", $("cmd").value !== "ask"));

  on("btn-cli", async () => {
    if (!prUrl()) { toastErr("Review", "Enter a PR URL first."); return; }
    try {
      const s = await tauriInvoke("build_cli_command", { prUrl: prUrl(), command: reviewCmd() });
      await navigator.clipboard.writeText(s);
      $("out").textContent = "Copied: " + s;
      toastOk("Copied", s.slice(0, 120));
    } catch (e) { $("out").textContent = "Clipboard failed: " + e; toastErr("Copy failed", String(e).slice(0, 160)); }
  });
  on("btn-run", async () => {
    if (!prUrl()) { toastErr("Review", "Enter a PR URL first."); return; }
    const key = $("or-key").value.trim() || Secrets.openrouter;
    if (!key) { toastErr("Review", "Enter OPENROUTER_API_KEY first (session only)."); return; }
    const ghToken = ($("gh-key") ? $("gh-key").value.trim() : "") || Secrets.github;
    if (!ghToken) {
      $("out").textContent = "GitHub token required. Connect GitHub in Settings → GitHub, then retry.";
      toastErr("Review", "Connect GitHub first (Settings → GitHub).");
      return;
    }
    $("out").textContent = "Running local review…";
    try {
      const r = await tauriInvoke("run_local_review", {
        prUrl: prUrl(),
        command: reviewCmd(),
        openrouterKey: key,
        githubToken: ghToken,
      });
      $("out").textContent = r || "(empty output)";
      toastOk("Review finished", String(r || "").slice(0, 140));
    } catch (e) { $("out").textContent = "Run failed: " + e; toastErr("Run failed", String(e).slice(0, 220)); }
  });
  on("btn-open-pr", () => { if (prUrl()) openUrl(prUrl()); });
  on("btn-open-actions", () => {
    const m = prUrl().match(/github\.com\/([^/]+\/[^/]+)/);
    openUrl(m ? "https://github.com/" + m[1].replace(/\/pull\/.*$/, "") + "/actions" : "https://github.com");
  });

  on("btn-oauth-start", oauthStart);
  on("btn-oauth-cancel", () => { oauthCancel = true; });
  on("btn-oauth-pat", oauthPat);

  // GitHub — 1-click repo setup (separate flow from Settings OAuth above)
  on("btn-gh-oauth-start", ghOauthStart);
  on("btn-gh-oauth-cancel", ghOauthCancelFlow);
  on("gh-oauth-open", () => { if (ghVerificationUri) openUrl(ghVerificationUri); });
  if ($("gh-oauth-client")) $("gh-oauth-client").addEventListener("input", ghPersistClientId);
  if ($("oauth-client")) $("oauth-client").addEventListener("input", () => {
    const src = $("oauth-client");
    try { localStorage.setItem("pragent.oauth_client", src.value.trim()); } catch (_) {}
    const other = $("gh-oauth-client");
    if (other && other.value !== src.value) other.value = src.value;
  });
  if ($("gh-repo-search")) $("gh-repo-search").addEventListener("input", ghRenderRepos);
  on("btn-gh-repos-refresh", ghLoadRepos);
  on("btn-gh-status-refresh", ghCheckStatus);
  on("btn-gh-secret-gen", () => {
    const el = $("gh-webhook-secret");
    if (el) el.value = ghRandomSecret();
    toastOk("Webhook secret generated", "Configure will save it to backend/.env only if that option is checked.");
  });
  on("btn-gh-configure", () => ghConfigure(false));
  on("btn-gh-retry-failed", () => ghConfigure(true));

  // agents
  on("btn-agents-save", saveAgent);
  on("btn-agents-refresh", () => refreshAgents());
  on("btn-agents-delete", deleteAgent);
  $("agent-trigger").addEventListener("change", () => {
    $("agent-label-wrap").style.display = $("agent-trigger").value === "on_label" ? "" : "none";
  });
  $("agent-select").addEventListener("change", async () => {
    const id = $("agent-select").value;
    if (!id) { fillAgentForm({}); return; }
    try {
      const a = await apiGet("/agents/" + encodeURIComponent(id));
      fillAgentForm(a || {});
    } catch (e) { toastErr("Agent", String(e).slice(0, 200)); }
  });
  on("btn-agent-repo-add", () => {
    const v = $("agent-repo-add").value.trim();
    if (!/^[^/\s]+\/[^/\s]+$/.test(v)) { toastErr("Repos", "Use owner/repo format."); return; }
    pickedRepos.add(v);
    $("agent-repo-add").value = "";
    renderRepoPicker([]);
  });
  on("btn-agent-repos-load", loadReposIntoPicker);

  // runs / quota
  on("btn-runs-sync", syncRuns);
  on("btn-quota", fetchQuota);
  on("btn-stream", toggleStream);
  on("btn-view-diff", () => { if (lastRuns.length) openModal(lastRuns[0].run_id || lastRuns[0].id); });
  on("modal-close", closeModal);
  $("modal-back").addEventListener("click", (e) => { if (e.target === $("modal-back")) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

  // integrations (1-click setup)
  on("btn-int-refresh", intRefresh);
  on("btn-int-gh-test", () => intTest("github"));
  on("btn-int-vercel-test", () => intTest("vercel"));
  on("btn-int-box-test", () => intTest("upstash_box"));
  on("btn-int-redis-test", () => intTest("redis"));
  on("btn-int-or-test", () => intTest("openrouter"));
  on("btn-int-gh-save", () => intSave([["GITHUB_TOKEN", "int-gh-token"], ["GITHUB_WEBHOOK_SECRET", "int-gh-secret"]], "GitHub"));
  on("btn-int-vercel-save", () => intSave([["VERCEL_TOKEN", "int-vercel-token"], ["VERCEL_TEAM_ID", "int-vercel-team"]], "Vercel"));
  on("btn-int-box-save", () => intSave([["UPSTASH_BOX_API_KEY", "int-box-key"]], "Upstash Box"));
  on("btn-int-redis-conn-save", () => intSave([["REDIS_URL", "int-redis-conn-url"]], "Redis URL"));
  on("btn-int-redis-save", () => intSave([["UPSTASH_REDIS_REST_URL", "int-redis-url"], ["UPSTASH_REDIS_REST_TOKEN", "int-redis-token"]], "Redis"));
  on("btn-int-or-save", () => intSave([["OPENROUTER_API_KEY", "int-or-key"]], "OpenRouter"));
  if ($("int-cloud-base")) $("int-cloud-base").addEventListener("input", intHookUrl);
  if ($("int-cloud-base")) $("int-cloud-base").addEventListener("input", ghHookHint);
  if ($("int-or-key")) $("int-or-key").addEventListener("input", ghKeyHint);
  on("btn-int-hook-copy", async () => {
    const url = intHookUrl();
    if (url.includes("<cloud-app>")) { toastErr("Webhook URL", "Enter your cloud app base URL first."); return; }
    try {
      await navigator.clipboard.writeText(url);
      toastOk("Copied", url);
    } catch (e) { toastErr("Copy failed", url); }
  });

  // box
  on("btn-box-up", async () => {
    const args = boxArgs(false);
    if (!args) return;
    $("box-out").textContent = "Provisioning box + schedule…";
    try { $("box-out").textContent = await tauriInvoke("box_provision", args); toastOk("Box provisioned", "Schedule started."); }
    catch (e) { $("box-out").textContent = "Provision failed: " + e; toastErr("Provision failed", String(e).slice(0, 220)); }
  });
  on("btn-box-status", async () => {
    const args = boxArgs(true);
    if (!args) return;
    $("box-out").textContent = "Checking box…";
    try { $("box-out").textContent = await tauriInvoke("box_provision", args); }
    catch (e) { $("box-out").textContent = "Status failed: " + e; toastErr("Box status failed", String(e).slice(0, 200)); }
  });

  // providers
  const paOut = (m) => { $("pa-out").textContent = m; };
  on("btn-pa-check", async () => {
    paOut("Checking…");
    const parts = [];
    for (const t of ["pr-agent", "python", "gh"]) {
      try { parts.push(t + ": " + await tauriInvoke("check_tool", { name: t })); }
      catch (e) { parts.push(t + ": MISSING (" + e + ")"); }
    }
    paOut(parts.join("\n"));
  });
  on("btn-pa-install", async () => {
    paOut("Creating a dedicated venv and installing pr-agent[github] — takes a few minutes…");
    try { paOut(await tauriInvoke("install_pr_agent")); toastOk("Install finished", "pr-agent CLI installed."); }
    catch (e) { paOut("Install failed: " + e); toastErr("Install failed", String(e).slice(0, 200)); }
  });
  on("btn-pa-write", async () => {
    try { paOut("Wrote workflow to " + await tauriInvoke("write_workflow", { provider: "pr-agent" }) + "\nCommit + push, add secret OPENROUTER_API_KEY."); toastOk("Workflow written", "pr-agent.yml"); }
    catch (e) { paOut("Write failed: " + e); toastErr("Write failed", String(e).slice(0, 160)); }
  });
  on("btn-pa-copy", async () => {
    try {
      const y = await tauriInvoke("get_workflow_template", { provider: "pr-agent" });
      await navigator.clipboard.writeText(y);
      paOut("Workflow YAML copied.");
      toastOk("Copied", "pr-agent workflow YAML.");
    } catch (e) { paOut("Copy failed: " + e); }
  });
  on("btn-or-write", async () => {
    try { $("or-out").textContent = "Wrote workflow to " + await tauriInvoke("write_workflow", { provider: "openrabbit" }) + "\nCommit + push, add secret LLM_API_KEY."; toastOk("Workflow written", "openrabbit.yml"); }
    catch (e) { $("or-out").textContent = "Write failed: " + e; toastErr("Write failed", String(e).slice(0, 160)); }
  });
  on("btn-or-copy", async () => {
    try {
      const y = await tauriInvoke("get_workflow_template", { provider: "openrabbit" });
      await navigator.clipboard.writeText(y);
      $("or-out").textContent = "Workflow YAML copied.";
      toastOk("Copied", "openrabbit workflow YAML.");
    } catch (e) { $("or-out").textContent = "Copy failed: " + e; }
  });
}

/* ---------- boot ---------- */
(function boot() {
  try {
    const th = localStorage.getItem("pragent.theme");
    if (th === "light" || th === "dark") document.documentElement.setAttribute("data-theme", th);
  } catch (_) {}
  try {
    const oc = localStorage.getItem("pragent.oauth_client");
    if (oc) {
      if ($("oauth-client")) $("oauth-client").value = oc;
      if ($("gh-oauth-client")) $("gh-oauth-client").value = oc;
    }
  } catch (_) {}
  loadSecrets();
  syncSecretInputs();
  if (Secrets.github) {
    setGhState(true, "");
    ghSetState(true, "");
    const ghSection = $("gh-repo-section");
    if (ghSection) ghSection.classList.remove("hidden");
  }
  ghRestoreClientId();
  wire();
  try {
    const last = localStorage.getItem("pragent.tab");
    if (last && TAB_TITLES[last]) switchTab(last);
  } catch (_) {}
  backendUrl();
  ghHookHint();
  ghKeyHint();
  ghUpdateConfigureBtn();
  renderRepoPicker([]);
  checkBackend(false);
  loadConfig();
  refreshAgents();
  syncRuns();
  fetchQuota();
  try { intRefresh(); } catch (_) {}
})();
