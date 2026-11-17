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
