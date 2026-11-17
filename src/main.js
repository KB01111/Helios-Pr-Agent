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
  for (const k of Object.keys(Secrets)) { Secrets[k] = ""; ssSet(SS_KEYS[k], ""); }
  for (const id of [
    "or-key", "sett-or", "gh-key", "sett-gh", "box-key", "oauth-pat", "api-token",
    "int-gh-token", "int-gh-secret", "int-vercel-token", "int-box-key",
    "int-redis-url", "int-redis-token", "int-or-key",
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
    $("max-tokens").value = cfg.custom_model_max_tokens || "200000";
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
    const interval = Math.max(5, d.interval || 5);
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
      if (r && r.status === "authorized") {
        setSecret("github", r.access_token);
        syncSecretInputs();
        setGhState(true, r.login);
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
  const base = backendUrl();
  // EventSource cannot set headers; the backend only accepts ?token= here.
  const streamUrl = base + "/events/stream" + (Secrets.api ? "?token=" + encodeURIComponent(Secrets.api) : "");
  try { evtSrc = new EventSource(streamUrl); }
  catch (e) { $("runs-out").textContent = "Stream failed: " + e; return; }
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
    "redis: " + (s.redis ? "configured (mode=" + (redis.mode || "?") + ")" : "not configured (mode=" + (redis.mode || "?") + ")"),
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
      if (key === "GITHUB_TOKEN") setSecret("github", val);
      if (key === "OPENROUTER_API_KEY") setSecret("openrouter", val);
      if (key === "UPSTASH_BOX_API_KEY") setSecret("box", val);
    }
    syncSecretInputs();
    for (const [, id] of pairs) { const el = $(id); if (el) el.value = (id === "int-vercel-team" || id === "int-redis-url") ? el.value : ""; }
    // Clear even saved non-secret fields that hold sensitive-adjacent data? Keep URL/team visible.
    const savedKeys = todo.map(([k]) => k).join(", ");
    intOut("Saved " + savedKeys + " to " + Array.from(paths).join(", ") + ".\nRestart/redeploy the backend so it picks up the new values, then Test.");
    toastOk("Saved", savedKeys + " → backend/.env");
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

  $("backend-url").addEventListener("change", () => { backendUrl(); checkBackend(false); });
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
    $("out").textContent = "Running local review…";
    try {
      const r = await tauriInvoke("run_local_review", { prUrl: prUrl(), command: reviewCmd(), openrouterKey: key });
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

  // agents
  on("btn-agents-save", saveAgent);
  on("btn-agents-refresh", refreshAgents);
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
  on("btn-int-redis-save", () => intSave([["UPSTASH_REDIS_REST_URL", "int-redis-url"], ["UPSTASH_REDIS_REST_TOKEN", "int-redis-token"]], "Redis"));
  on("btn-int-or-save", () => intSave([["OPENROUTER_API_KEY", "int-or-key"]], "OpenRouter"));
  if ($("int-cloud-base")) $("int-cloud-base").addEventListener("input", intHookUrl);
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
    paOut("Installing pr-agent[github] via pip — takes a few minutes…");
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
    if (oc && $("oauth-client")) $("oauth-client").value = oc;
  } catch (_) {}
  loadSecrets();
  syncSecretInputs();
  if (Secrets.github) setGhState(true, "");
  wire();
  try {
    const last = localStorage.getItem("pragent.tab");
    if (last && TAB_TITLES[last]) switchTab(last);
  } catch (_) {}
  backendUrl();
  renderRepoPicker([]);
  checkBackend(false);
  loadConfig();
  refreshAgents();
  syncRuns();
  fetchQuota();
  try { intRefresh(); } catch (_) {}
})();
