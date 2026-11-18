/* Autonomy & escalation UI — loaded after main.js (classic script, CSP-safe).
 *
 * Reuses main.js globals: $, $$, toastOk/toastErr, tauriInvoke, apiGet/apiMut,
 * Secrets, backendUrl, openUrl. Everything from the backend (repo names, PR
 * titles) is attacker-influenced, so it is only ever written with textContent.
 */
"use strict";

(function autonomyModule() {
  if (typeof apiGet !== "function" || !$("tab-autonomy")) return;

  const LEVELS = {
    observe: "Shadow mode. Decisions are recorded, but every PR still gets its normal review — nothing changes.",
    assist: "Acts alone only on cheap, confident calls (skip or fast review at/above the auto threshold). Sandbox runs and anything unsure wait in the inbox.",
    auto: "Runs everything at/above the floor, sandboxes included. Only safety flags and gray-zone PRs reach you.",
  };
  const REASONS = {
    SAFETY_FLAGGED: "Touches workflows / secrets / auth, or contains secret-like content.",
    LOW_CONFIDENCE_GRAY_ZONE: "The decider is unsure (or the diff is too big to judge) — needs your call.",
    RUNTIME_TEST_NEEDED: "Wants a runtime test in a warm Upstash Box.",
    UI_PREVIEW_NEEDED: "Wants a UI preview in a Vercel sandbox.",
    STATIC_DIFF_OK: "A diff review should be enough.",
    DOCS_ONLY: "Docs only — nothing to review.",
    LOCKFILE_BUMP: "Dependency lockfile change.",
  };
  const PREFS_KEY = "pragent.daemon";

  const state = { level: "assist", confAuto: 0.75, confFloor: 0.55, timers: [] };

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function pill(text, kind) { return el("span", "pill" + (kind ? " " + kind : ""), text); }
  function button(label, cls, onClick) {
    const b = el("button", "btn btn-sm " + cls, label);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }
  function ago(iso) {
    const t = Date.parse(iso);
    if (!t) return "";
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 90) return s + "s ago";
    if (s < 5400) return Math.round(s / 60) + "m ago";
    return Math.round(s / 3600) + "h ago";
  }
  function secsAgo(epoch) { return epoch ? ago(new Date(epoch * 1000).toISOString()) : "never"; }
  function loadPrefs() {
    try { return Object.assign({ autostart: false, interval: 30, sidecar: "" }, JSON.parse(localStorage.getItem(PREFS_KEY) || "{}")); }
    catch (_) { return { autostart: false, interval: 30, sidecar: "" }; }
  }
  function savePrefs(p) { try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch (_) {} }
  function tabActive() { return $("tab-autonomy").classList.contains("active"); }

  /* ---------- autonomy level ---------- */
  function renderLevel() {
    $$("#autonomy-levels .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.level === state.level));
    $("autonomy-desc").textContent = LEVELS[state.level];
    $("conf-auto").value = state.confAuto;
    $("conf-floor").value = state.confFloor;
    $("conf-auto-val").textContent = state.confAuto.toFixed(2);
    $("conf-floor-val").textContent = state.confFloor.toFixed(2);
    $("conf-auto-row").classList.toggle("hidden", state.level !== "assist");
  }
  async function loadAutonomy() {
    try {
      const a = await apiGet("/autonomy");
      state.level = a.level; state.confAuto = a.conf_auto; state.confFloor = a.conf_floor;
      renderLevel();
      $("autonomy-policy").textContent = "policy " + (a.policy_version || "?");
    } catch (e) { $("autonomy-policy").textContent = "backend unreachable"; }
  }
  async function saveAutonomy() {
    try {
      const a = await apiMut("PUT", "/autonomy", { level: state.level, conf_auto: state.confAuto, conf_floor: state.confFloor });
      state.level = a.level; state.confAuto = a.conf_auto; state.confFloor = a.conf_floor;
      renderLevel();
      toastOk("Autonomy saved", state.level + " · auto ≥ " + state.confAuto.toFixed(2) + " · floor " + state.confFloor.toFixed(2));
    } catch (e) { toastErr("Could not save autonomy", String(e).slice(0, 200)); }
  }

  /* ---------- daemon ---------- */
  function renderDaemon(s, cloud) {
    const running = !!(s && s.running);
    const badge = $("daemon-state");
    badge.textContent = running ? "running" : "stopped";
    badge.className = "badge " + (running ? "ok" : "");
    $("btn-daemon-start").classList.toggle("hidden", running);
    $("btn-daemon-stop").classList.toggle("hidden", !running);
    const grid = $("daemon-stats");
    grid.textContent = "";
    const cells = s ? [
      ["Polls", String(s.polls)], ["Decided", String(s.decided)], ["Settled elsewhere", String(s.lost_races)],
      ["Errors", String(s.failures)], ["Last OK", secsAgo(s.last_ok_at)], ["Model", s.model || "rules"],
    ] : [];
    for (const [k, v] of cells) {
      const c = el("div", "kv");
      c.appendChild(el("span", "kv-k", k));
      c.appendChild(el("b", "kv-v", v));
      grid.appendChild(c);
    }
    const cl = $("daemon-cloud");
    if (cloud) {
      cl.textContent = cloud.desktop_online
        ? "Backend sees this desktop as online — new PRs are handed to it (cloud fallback after " + cloud.defer_grace_s + " s)."
        : "Backend does not see the desktop — the cloud decider handles PRs immediately (fallback active).";
      cl.className = "hint " + (cloud.desktop_online ? "" : "gh-warn");
    } else { cl.textContent = ""; }
    $("daemon-error").textContent = s && s.last_error ? "Last error: " + s.last_error : "";
    const log = $("daemon-log");
    const lines = s && s.recent ? s.recent.slice().reverse().map((l) => new Date(l.at * 1000).toLocaleTimeString() + "  " + l.msg) : [];
    log.textContent = lines.length ? lines.join("\n") : "No daemon activity yet.";
  }
  async function refreshDaemon() {
    let local = null, cloud = null;
    try { local = await tauriInvoke("daemon_status"); } catch (e) {
      if (!tauriUnavailable(e)) console.warn("daemon_status", e);
      $("daemon-error").textContent = "The daemon runs in the desktop app (Tauri) only.";
    }
    try { cloud = await apiGet("/daemon/status"); } catch (_) {}
    if (local) renderDaemon(local, cloud);
  }
  async function startDaemon(silent) {
    const prefs = loadPrefs();
    prefs.interval = Math.min(60, Math.max(5, parseInt($("daemon-interval").value, 10) || 30));
    prefs.sidecar = $("daemon-sidecar").value.trim();
    prefs.autostart = $("daemon-autostart").checked;
    savePrefs(prefs);
    try {
      const s = await tauriInvoke("daemon_start", {
        backendUrl: backendUrl(), apiToken: Secrets.api || null,
        intervalSecs: prefs.interval, sidecarUrl: prefs.sidecar || null,
      });
      renderDaemon(s, null);
      if (!silent) toastOk("Daemon started", "Polling every " + s.interval_secs + " s (" + s.model + ").");
      refreshDaemon();
    } catch (e) {
      if (!silent || !tauriUnavailable(e)) toastErr("Daemon did not start", String(e).slice(0, 220));
    }
  }
  async function stopDaemon() {
    try { renderDaemon(await tauriInvoke("daemon_stop"), null); toastOk("Daemon stopped", "The cloud decider handles PRs now."); refreshDaemon(); }
    catch (e) { toastErr("Could not stop daemon", String(e).slice(0, 200)); }
  }

  /* ---------- escalation inbox ---------- */
  function prUrl(rec) { return "https://github.com/" + rec.repo + "/pull/" + rec.pr_number; }

  function decisionLine(rec) {
    const d = rec.decision;
    const row = el("div", "row wrap esc-pills");
    if (d) {
      row.appendChild(pill(d.decision, d.decision === "ESCALATE_HUMAN" ? "pending" : ""));
      row.appendChild(pill(d.reason_code));
      row.appendChild(pill("provider: " + d.provider));
      row.appendChild(pill((rec.decided_by || "?") + " · " + (d.latency_ms || 0) + " ms"));
    } else {
      row.appendChild(pill(rec.state, "err"));
    }
    return row;
  }
  function confBar(conf) {
    const wrap = el("div", "bar conf");
    const fill = el("div");
    fill.style.width = Math.round(Math.max(0, Math.min(1, conf)) * 100) + "%";
    wrap.appendChild(fill);
    wrap.title = "confidence " + conf.toFixed(2);
    return wrap;
  }
  async function resolveItem(rec, action) {
    try {
      await apiMut("POST", "/decisions/" + encodeURIComponent(rec.id) + "/resolve", { action, resolved_by: "desktop-user" });
      toastOk(action === "skip" ? "Skipped" : "Dispatched", rec.repo + "#" + rec.pr_number);
    } catch (e) { toastErr("Could not resolve", String(e).slice(0, 200)); }
    refreshInbox();
  }
  function escalationCard(rec) {
    const card = el("div", "run-item esc-card");
    const head = el("div", "run-head");
    head.appendChild(el("strong", null, rec.repo + " #" + rec.pr_number));
    head.appendChild(el("span", "run-meta", (rec.head_sha || "").slice(0, 7) + " · " + rec.files + " files · " + rec.churn + " lines · " + ago(rec.created_at)));
    card.appendChild(head);
    if (rec.title) card.appendChild(el("div", "esc-title", rec.title));
    card.appendChild(decisionLine(rec));
    if (rec.decision) {
      card.appendChild(confBar(rec.decision.confidence));
      card.appendChild(el("div", "run-meta", "confidence " + rec.decision.confidence.toFixed(2) + " — " + (REASONS[rec.decision.reason_code] || "")));
    }
    if (rec.error) card.appendChild(el("div", "repo-err", rec.error));
    const row = el("div", "row wrap");
    const failed = rec.state === "failed";
    row.appendChild(button(failed ? "Retry: fast review" : "Fast review", "btn-accent", () => resolveItem(rec, "fast_review")));
    row.appendChild(button(failed ? "Retry: as configured" : "Run as configured", "btn-outline", () => resolveItem(rec, "sandbox")));
    if (!failed) row.appendChild(button("Skip", "btn-ghost", () => resolveItem(rec, "skip")));
    row.appendChild(button("Open PR", "btn-ghost", () => openUrl(prUrl(rec))));
    card.appendChild(row);
    return card;
  }
  function renderInbox(items) {
    const box = $("esc-list");
    box.textContent = "";
    $("esc-count").textContent = items.length + " waiting";
    $("esc-count").className = "badge " + (items.length ? "err" : "ok");
    const nav = $("nav-esc-count");
    if (nav) { nav.textContent = String(items.length); nav.classList.toggle("hidden", items.length === 0); }
    if (!items.length) { box.appendChild(el("p", "hint", "Nothing needs you right now.")); return; }
    items.forEach((rec) => box.appendChild(escalationCard(rec)));
  }
  function renderRecent(items) {
    const box = $("dec-list");
    box.textContent = "";
    if (!items.length) { box.appendChild(el("p", "hint", "No decisions yet.")); return; }
    for (const rec of items.slice(0, 25)) {
      const row = el("div", "dec-row");
      row.appendChild(el("span", "dec-name", rec.repo + " #" + rec.pr_number));
      row.appendChild(el("span", "run-meta", (rec.head_sha || "").slice(0, 7)));
      const kind = { dispatched: "ok", skipped: "ok", escalated: "pending", failed: "err", dismissed: "" }[rec.state] || "";
      row.appendChild(pill(rec.state, kind));
      row.appendChild(el("span", "dec-what", rec.decision ? rec.decision.decision + " · " + rec.decision.reason_code + " · " + rec.decision.confidence.toFixed(2) : "—"));
      row.appendChild(el("span", "run-meta", ago(rec.created_at)));
      box.appendChild(row);
    }
  }
  function renderStats(stats) {
    const box = $("dec-stats");
    box.textContent = "";
    for (const [kind, c] of Object.entries(stats.feedback || {})) {
      if (kind === "ESCALATE_HUMAN") {
        const total = c.chose_fast_review + c.chose_sandbox + c.chose_skip;
        box.appendChild(pill("escalations: " + (total
          ? c.chose_fast_review + " fast review · " + c.chose_sandbox + " as configured · " + c.chose_skip + " skipped"
          : "none resolved yet")));
        continue;
      }
      const total = c.agree + c.override;
      box.appendChild(pill(kind + ": " + (total ? c.agree + " agreed / " + c.override + " overridden" : "no feedback yet")));
    }
  }
  async function refreshInbox() {
    try {
      const all = await apiGet("/decisions?limit=100");
      renderInbox(all.filter((r) => r.state === "escalated" || r.state === "failed"));
      renderRecent(all);
      renderStats(await apiGet("/decisions/stats"));
    } catch (e) { $("esc-count").textContent = "backend unreachable"; $("esc-count").className = "badge err"; }
  }

  /* ---------- wiring ---------- */
  function wire() {
    $$("#autonomy-levels .seg-btn").forEach((b) => b.addEventListener("click", () => { state.level = b.dataset.level; renderLevel(); }));
    $("conf-auto").addEventListener("input", () => {
      state.confAuto = parseFloat($("conf-auto").value);
      if (state.confFloor > state.confAuto) state.confFloor = state.confAuto;
      renderLevel();
    });
    $("conf-floor").addEventListener("input", () => {
      state.confFloor = Math.min(parseFloat($("conf-floor").value), state.confAuto);
      renderLevel();
    });
    $("btn-autonomy-save").addEventListener("click", saveAutonomy);
    $("btn-autonomy-reload").addEventListener("click", loadAutonomy);
    $("btn-daemon-start").addEventListener("click", () => startDaemon(false));
    $("btn-daemon-stop").addEventListener("click", stopDaemon);
    $("btn-daemon-refresh").addEventListener("click", refreshDaemon);
    $("btn-esc-refresh").addEventListener("click", refreshInbox);
    $$(".nav-btn").forEach((b) => b.addEventListener("click", () => { if (b.dataset.tab === "autonomy") refreshAll(); }));
    $("daemon-autostart").addEventListener("change", () => { const p = loadPrefs(); p.autostart = $("daemon-autostart").checked; savePrefs(p); });
  }
  function refreshAll() { loadAutonomy(); refreshDaemon(); refreshInbox(); }

  const prefs = loadPrefs();
  $("daemon-interval").value = prefs.interval;
  $("daemon-sidecar").value = prefs.sidecar;
  $("daemon-autostart").checked = prefs.autostart;
  wire();
  renderLevel();
  refreshAll();
  // Cheap local status while the tab is open; inbox/nav badge refresh every 20 s regardless.
  setInterval(() => { if (tabActive() && !document.hidden) refreshDaemon(); }, 5000);
  setInterval(() => { if (!document.hidden) refreshInbox(); }, 20000);
  if (prefs.autostart) startDaemon(true);
})();
