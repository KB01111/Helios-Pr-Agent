// PR Agent Control — Tauri v2 backend
// Reads/writes .pr_agent.toml + workflow file next to the app root,
// and optionally shells out to a local `pr-agent` CLI if installed.

use std::path::PathBuf;
use std::process::Command;

fn app_root_candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        out.push(cwd.clone());
        if let Some(p) = cwd.parent() {
            out.push(p.to_path_buf());
        }
    }
    // src-tauri/../  (CARGO_MANIFEST_DIR = src-tauri at compile time)
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    if let Some(parent) = manifest.parent() {
        out.push(parent.to_path_buf());
    }
    out.push(manifest);
    out
}

fn find_file(name: &str) -> Option<PathBuf> {
    // name may contain subdirs, e.g. ".github/workflows/pr-agent.yml"
    for root in app_root_candidates() {
        let direct = root.join(name);
        if direct.is_file() {
            return Some(direct);
        }
        // also try one level up (when running from src-tauri)
        if let Some(parent) = root.parent() {
            let up = parent.join(name);
            if up.is_file() {
                return Some(up);
            }
        }
    }
    None
}

fn config_path_for_write() -> PathBuf {
    if let Some(p) = find_file(".pr_agent.toml") {
        return p;
    }
    // default: app root (parent of src-tauri) or cwd
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest
        .parent()
        .map(|p| p.join(".pr_agent.toml"))
        .unwrap_or_else(|| PathBuf::from(".pr_agent.toml"))
}

fn get_val(src: &str, key: &str) -> Option<String> {
    for line in src.lines() {
        let t = line.trim();
        if t.starts_with(key) {
            if let Some(eq) = t.find('=') {
                let v = t[eq + 1..].trim().trim_matches('"').trim_matches('\'').to_string();
                return Some(v);
            }
        }
    }
    None
}

#[tauri::command]
fn get_config() -> Result<serde_json::Value, String> {
    let path = find_file(".pr_agent.toml").ok_or("No .pr_agent.toml found next to app")?;
    let raw = std::fs::read_to_string(&path).map_err(|e| e.to_string())?;

    // fallback_models = ["a", "b"]
    let mut fallbacks: Vec<String> = Vec::new();
    for line in raw.lines() {
        let t = line.trim();
        if t.starts_with("fallback_models") {
            let mut cur = String::new();
            let mut in_q = false;
            for ch in t.chars() {
                if ch == '"' || ch == '\'' {
                    if in_q {
                        if !cur.is_empty() {
                            fallbacks.push(cur.clone());
                        }
                        cur.clear();
                    }
                    in_q = !in_q;
                } else if in_q {
                    cur.push(ch);
                }
            }
        }
    }

    let parse_bool = |k: &str| -> bool {
        get_val(&raw, k)
            .map(|v| v.to_lowercase() == "true")
            .unwrap_or(false)
    };

    Ok(serde_json::json!({
        "path": path.to_string_lossy(),
        "raw": raw,
        "model": get_val(&raw, "model").unwrap_or_else(|| "openrouter/thinkingmachines/inkling-2-small:free".into()),
        "fallback_models": fallbacks,
        "custom_model_max_tokens": get_val(&raw, "custom_model_max_tokens").unwrap_or_else(|| "200000".into()),
        "reasoning_effort": get_val(&raw, "reasoning_effort").unwrap_or_else(|| "low".into()),
        "auto_describe": parse_bool("auto_describe"),
        "auto_review": parse_bool("auto_review"),
        "auto_improve": parse_bool("auto_improve"),
    }))
}

#[tauri::command]
fn save_config(cfg: serde_json::Value) -> Result<String, String> {
    let model = cfg.get("model").and_then(|v| v.as_str()).unwrap_or("openrouter/thinkingmachines/inkling-2-small:free");
    let fallbacks = cfg.get("fallback_models").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let fb: Vec<String> = fallbacks.iter().filter_map(|v| v.as_str().map(|s| format!("\"{}\"", s))).collect();
    let max_tokens = cfg.get("custom_model_max_tokens").and_then(|v| v.as_str()).unwrap_or("200000");
    let effort = cfg.get("reasoning_effort").and_then(|v| v.as_str()).unwrap_or("low");
    let b = |k: &str, d: bool| cfg.get(k).and_then(|v| v.as_bool()).unwrap_or(d);

    let content = format!(
        r#"[config]
# Managed by PR Agent Control app
model = "{model}"
fallback_models = [{fb}]
custom_model_max_tokens = {max_tokens}
reasoning_effort = "{effort}"

[openrouter]
# key is injected via env OPENROUTER__KEY / secrets.OPENROUTER_API_KEY - don't hardcode

[pr_reviewer]
persistent_comment = false
inline_code_comments = true
ask_and_reflect = true

[github_action_config]
auto_describe = {ad}
auto_review = {ar}
auto_improve = {ai}
"#,
        fb = fb.join(", "),
        ad = b("auto_describe", true),
        ar = b("auto_review", true),
        ai = b("auto_improve", false),
    );

    let path = config_path_for_write();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    std::fs::write(&path, &content).map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().to_string())
}

#[tauri::command]
fn get_workflow() -> Result<serde_json::Value, String> {
    let path = find_file(".github/workflows/pr-agent.yml").ok_or("No workflow file found")?;
    let raw = std::fs::read_to_string(&path).map_err(|e| e.to_string())?;
    Ok(serde_json::json!({ "path": path.to_string_lossy(), "raw": raw }))
}

#[tauri::command]
fn build_cli_command(pr_url: String, command: String) -> String {
    let cmd = command.trim().trim_start_matches('/').to_string();
    format!("pr-agent --pr_url {} {}", pr_url.trim(), cmd)
}

#[tauri::command]
fn run_local_review(pr_url: String, command: String, openrouter_key: String) -> Result<String, String> {
    let cmd = command.trim().trim_start_matches('/').to_string();
    // Try `pr-agent` on PATH first, then `python -m pr_agent`
    let attempt = Command::new("pr-agent")
        .args(["--pr_url", pr_url.trim(), &cmd])
        .env("OPENROUTER__KEY", &openrouter_key)
        .output();

    match attempt {
        Ok(o) if o.status.success() || !o.stdout.is_empty() || !o.stderr.is_empty() => {
            let mut s = String::from_utf8_lossy(&o.stdout).to_string();
            let e = String::from_utf8_lossy(&o.stderr).to_string();
            if !e.is_empty() {
                s.push_str("\n[stderr]\n");
                s.push_str(&e);
            }
            Ok(s)
        }
        _ => {
            let py = Command::new("python")
                .args(["-m", "pr_agent.cli", "--pr_url", pr_url.trim(), &cmd])
                .env("OPENROUTER__KEY", &openrouter_key)
                .output()
                .map_err(|e| format!("No local `pr-agent` found ({}). Install: pip install \"pr-agent[github]\" — then retry. CLI would be: pr-agent --pr_url {} {}", e, pr_url.trim(), cmd))?;
            let mut s = String::from_utf8_lossy(&py.stdout).to_string();
            let e = String::from_utf8_lossy(&py.stderr).to_string();
            if !e.is_empty() {
                s.push_str("\n[stderr]\n");
                s.push_str(&e);
            }
            Ok(s)
        }
    }
}

#[tauri::command]
fn box_provision(
    box_key: String,
    github_token: String,
    openrouter_key: String,
    repos: String,
    box_name: String,
    cron: String,
    model: String,
    status_only: bool,
) -> Result<String, String> {
    if box_key.trim().is_empty() {
        return Err("UPSTASH_BOX_API_KEY is required".into());
    }
    let script = find_file("upstash-box/provision.mjs")
        .ok_or("upstash-box/provision.mjs not found next to app")?;
    let script_dir = script
        .parent()
        .ok_or("cannot resolve provision script dir")?
        .to_path_buf();
    let mut cmd = Command::new("node");
    cmd.arg(&script)
        .current_dir(&script_dir)
        .env("UPSTASH_BOX_API_KEY", box_key.trim())
        .env("GITHUB_TOKEN", github_token.trim())
        .env("OPENROUTER_API_KEY", openrouter_key.trim())
        .env("REPOS", repos.trim())
        .env("BOX_NAME", box_name.trim())
        .env("PR_CRON", cron.trim())
        .env("MODEL", model.trim());
    if status_only {
        cmd.arg("--status");
    }
    let out = cmd.output().map_err(|e| format!("failed to run node: {}", e))?;
    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();
    if out.status.success() {
        Ok(if stdout.trim().is_empty() {
            "(ok, empty output)".into()
        } else {
            stdout
        })
    } else {
        Err(format!("exit {}: {}", out.status, if stderr.trim().is_empty() { stdout } else { stderr }))
    }
}

const PR_AGENT_WORKFLOW: &str = r#"name: PR Agent
on:
  pull_request:
    types: [opened, synchronize]
  issue_comment:
jobs:
  pr_agent_job:
    if: ${{ github.event.sender.type != 'Bot' }}
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write
      pull-requests: write
    steps:
      - name: PR Agent action step
        uses: the-pr-agent/pr-agent@main
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          OPENROUTER__KEY: ${{ secrets.OPENROUTER_API_KEY }}
          CONFIG__MODEL: "openrouter/thinkingmachines/inkling-2-small:free"
          CONFIG__FALLBACK_MODELS: '["openrouter/free"]'
          CONFIG__CUSTOM_MODEL_MAX_TOKENS: "200000"
          CONFIG__REASONING_EFFORT: "low"
"#;

const OPENRABBIT_WORKFLOW: &str = r#"name: OpenRabbit Reviewer
on:
  pull_request_target:
    types: [opened, reopened, edited, synchronize]
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - name: OpenRabbit
        uses: aryanbrite/openrabbit@main
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          llm_provider: openrouter
          llm_api_url: https://openrouter.ai/api/v1
          llm_api_key: ${{ secrets.LLM_API_KEY }}
          llm_model: thinkingmachines/inkling-2-small:free
          review_mode: both
          tone_mode: balanced
"#;

#[tauri::command]
fn check_tool(name: String) -> Result<String, String> {
    let allowed = ["pr-agent", "python", "pip", "node", "gh"];
    if !allowed.contains(&name.as_str()) {
        return Err("tool not allowed".into());
    }
    let out = Command::new(&name)
        .arg("--version")
        .output()
        .map_err(|e| format!("{} not found: {}", name, e))?;
    let mut s = String::from_utf8_lossy(&out.stdout).to_string();
    s.push_str(&String::from_utf8_lossy(&out.stderr));
    let s = s.trim().to_string();
    if out.status.success() {
        Ok(if s.is_empty() { format!("{} installed", name) } else { s })
    } else {
        Err(format!("{} check failed: {}", name, s))
    }
}

#[tauri::command]
fn install_pr_agent() -> Result<String, String> {
    // Installs the PR-Agent CLI with the GitHub integration into the ambient Python.
    let out = Command::new("python")
        .args(["-m", "pip", "install", "pr-agent[github]"])
        .output()
        .map_err(|e| format!("failed to run pip: {}", e))?;
    let mut s = String::from_utf8_lossy(&out.stdout).to_string();
    s.push_str(&String::from_utf8_lossy(&out.stderr));
    if out.status.success() {
        let tail: Vec<&str> = s.lines().collect();
        let start = tail.len().saturating_sub(25);
        Ok(tail[start..].join("\n"))
    } else {
        Err(format!("pip install failed: {}", s))
    }
}

#[tauri::command]
fn get_workflow_template(provider: String) -> Result<String, String> {
    match provider.as_str() {
        "pr-agent" => Ok(PR_AGENT_WORKFLOW.to_string()),
        "openrabbit" => Ok(OPENRABBIT_WORKFLOW.to_string()),
        _ => Err("unknown provider".into()),
    }
}

#[tauri::command]
fn write_workflow(provider: String) -> Result<String, String> {
    let (file, content) = match provider.as_str() {
        "pr-agent" => ("pr-agent.yml", PR_AGENT_WORKFLOW),
        "openrabbit" => ("openrabbit.yml", OPENRABBIT_WORKFLOW),
        _ => return Err("unknown provider".into()),
    };
    let rel = format!(".github/workflows/{}", file);
    let path = find_file(&rel).unwrap_or_else(|| {
        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        manifest
            .parent()
            .map(|p| p.join(&rel))
            .unwrap_or_else(|| PathBuf::from(&rel))
    });
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    std::fs::write(&path, content).map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().to_string())
}

fn gh_client() -> Result<reqwest::blocking::Client, String> {
    reqwest::blocking::Client::builder()
        .user_agent("pr-agent-control")
        .build()
        .map_err(|e| e.to_string())
}

/// Step 1 of GitHub OAuth device flow: returns device_code + user_code.
/// Needs only the OAuth App client_id (no secret). Enable Device Flow in the app settings.
#[tauri::command]
fn github_device_start(client_id: String) -> Result<serde_json::Value, String> {
    let client_id = client_id.trim();
    if client_id.is_empty() {
        return Err("OAuth client_id is required".into());
    }
    let res: serde_json::Value = gh_client()?
        .post("https://github.com/login/device/code")
        .header("Accept", "application/json")
        .json(&serde_json::json!({ "client_id": client_id, "scope": "repo" }))
        .send()
        .map_err(|e| e.to_string())?
        .json()
        .map_err(|e| e.to_string())?;
    if res.get("device_code").is_none() {
        return Err(format!("GitHub refused: {}", res));
    }
    Ok(res)
}

/// Step 2: poll until the user approves (or max_wait_secs elapses -> {status:"pending"}).
/// Returns {status:"authorized", access_token, login} on success. Token is returned
/// to the UI only — never written to disk.
#[tauri::command]
fn github_device_poll(
    client_id: String,
    device_code: String,
    interval_secs: u64,
    max_wait_secs: u64,
) -> Result<serde_json::Value, String> {
    let client = gh_client()?;
    let interval = interval_secs.clamp(5, 60);
    let deadline =
        std::time::Instant::now() + std::time::Duration::from_secs(max_wait_secs.clamp(10, 600));
    loop {
        std::thread::sleep(std::time::Duration::from_secs(interval));
        let res: serde_json::Value = client
            .post("https://github.com/login/oauth/access_token")
            .header("Accept", "application/json")
            .json(&serde_json::json!({
                "client_id": client_id.trim(),
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
            }))
            .send()
            .map_err(|e| e.to_string())?
            .json()
            .map_err(|e| e.to_string())?;
        if let Some(token) = res.get("access_token").and_then(|v| v.as_str()) {
            let login = github_login_inner(&client, token).unwrap_or_default();
            return Ok(serde_json::json!({
                "status": "authorized",
                "access_token": token,
                "login": login
            }));
        }
        match res.get("error").and_then(|v| v.as_str()).unwrap_or("") {
            "authorization_pending" | "slow_down" => {}
            other => return Err(format!("OAuth error ({}): {}", other, res)),
        }
        if std::time::Instant::now() >= deadline {
            return Ok(serde_json::json!({ "status": "pending" }));
        }
    }
}

fn github_login_inner(
    client: &reqwest::blocking::Client,
    token: &str,
) -> Result<String, String> {
    let res: serde_json::Value = client
        .get("https://api.github.com/user")
        .header("Accept", "application/vnd.github+json")
        .header("Authorization", format!("Bearer {}", token))
        .send()
        .map_err(|e| e.to_string())?
        .json()
        .map_err(|e| e.to_string())?;
    Ok(res
        .get("login")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string())
}

/// Resolve the login for a token (OAuth or PAT). Used to confirm auth + auto-fill.
#[tauri::command]
fn github_login(token: String) -> Result<String, String> {
    github_login_inner(&gh_client()?, token.trim())
}

// ---- Cloud-brain backend proxies (no tokens logged, no secrets stored) ----
fn backend_base(url: &str) -> Result<String, String> {
    let b = url.trim().trim_end_matches('/').to_string();
    if b.is_empty() {
        return Err("backend_url is required".into());
    }
    if !(b.starts_with("http://") || b.starts_with("https://")) {
        return Err("backend_url must start with http(s)://".into());
    }
    Ok(b)
}

fn backend_get(url: &str) -> Result<String, String> {
    let res = gh_client()?
        .get(url)
        .header("Accept", "application/json")
        .send()
        .map_err(|e| format!("backend unreachable: {}", e))?;
    let status = res.status();
    let body = res.text().map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("backend HTTP {}: {}", status, body.chars().take(300).collect::<String>()));
    }
    Ok(body)
}

/// GET {backend_url}/health — connectivity check.
#[tauri::command]
fn backend_health(backend_url: String) -> Result<String, String> {
    let base = backend_base(&backend_url)?;
    backend_get(&format!("{}/health", base))
}

/// GET {backend_url}/agents — list agents (UI does POST/PUT/DELETE via fetch).
/// `agents_json` is accepted for API symmetry and currently ignored.
#[tauri::command]
fn backend_sync_agents(backend_url: String, agents_json: String) -> Result<String, String> {
    let _ = agents_json;
    let base = backend_base(&backend_url)?;
    backend_get(&format!("{}/agents", base))
}

/// GET {backend_url}/runs?limit=50 — recent background runs.
#[tauri::command]
fn backend_get_runs(backend_url: String) -> Result<String, String> {
    let base = backend_base(&backend_url)?;
    backend_get(&format!("{}/runs?limit=50", base))
}

/// GET {backend_url}/quota — Upstash Box / Vercel Sandbox usage.
#[tauri::command]
fn backend_get_quota(backend_url: String) -> Result<String, String> {
    let base = backend_base(&backend_url)?;
    backend_get(&format!("{}/quota", base))
}

/// GET {backend_url}/config/status — which providers are configured (booleans only, no secrets).
#[tauri::command]
fn backend_config_status(backend_url: String) -> Result<String, String> {
    let base = backend_base(&backend_url)?;
    backend_get(&format!("{}/config/status", base))
}

/// POST {backend_url}/config/test {"provider": ...} — live connectivity check (no secrets echoed).
#[tauri::command]
fn backend_config_test(
    backend_url: String,
    provider: String,
    api_token: String,
) -> Result<String, String> {
    let base = backend_base(&backend_url)?;
    let p = provider.trim().to_lowercase();
    let allowed = ["github", "redis", "vercel", "upstash_box", "openrouter"];
    if !allowed.contains(&p.as_str()) {
        return Err("unknown provider (use: github, redis, vercel, upstash_box, openrouter)".into());
    }
    let mut req = gh_client()?
        .post(format!("{}/config/test", base))
        .header("Accept", "application/json")
        .json(&serde_json::json!({ "provider": p }));
    if !api_token.trim().is_empty() {
        req = req.header("Authorization", format!("Bearer {}", api_token.trim()));
    }
    let res = req.send().map_err(|e| format!("backend unreachable: {}", e))?;
    let status = res.status();
    let body = res.text().map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("backend HTTP {}: {}", status, body.chars().take(300).collect::<String>()));
    }
    Ok(body)
}

// ---- Integrations: secrets go to backend/.env via Rust (never localStorage) ----

/// Env vars the Integrations card is allowed to write. Anything else is rejected.
fn allowed_env_key(key: &str) -> bool {
    matches!(
        key,
        "GITHUB_TOKEN"
            | "GITHUB_WEBHOOK_SECRET"
            | "UPSTASH_BOX_API_KEY"
            | "UPSTASH_REDIS_REST_URL"
            | "UPSTASH_REDIS_REST_TOKEN"
            | "VERCEL_TOKEN"
            | "VERCEL_API_TOKEN"
            | "VERCEL_TEAM_ID"
            | "OPENROUTER_API_KEY"
            | "LLM_MODEL"
            | "ALLOWED_ORIGINS"
            | "BACKEND_API_TOKEN"
    )
}

fn find_backend_env() -> Option<PathBuf> {
    for root in app_root_candidates() {
        let direct = root.join("backend").join(".env");
        if direct.is_file() {
            return Some(direct);
        }
        if let Some(parent) = root.parent() {
            let up = parent.join("backend").join(".env");
            if up.is_file() {
                return Some(up);
            }
        }
    }
    None
}

fn backend_env_for_write() -> PathBuf {
    if let Some(p) = find_backend_env() {
        return p;
    }
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest
        .parent()
        .map(|p| p.join("backend").join(".env"))
        .unwrap_or_else(|| PathBuf::from("backend/.env"))
}

/// Write one integration secret into backend/.env (creates the file if needed).
/// The value is never logged and never touches localStorage — it stays in
/// Rust + the dotenv file, which is git-ignored.
#[tauri::command]
fn save_backend_env_var(key: String, value: String) -> Result<String, String> {
    let k = key.trim().to_uppercase();
    if !allowed_env_key(&k) {
        return Err("refusing to write unknown env key".into());
    }
    let v = value.trim();
    if v.is_empty() {
        return Err(format!("{} is empty — nothing written", k));
    }
    if v.contains('\n') || v.contains('\r') {
        return Err("value must be a single line".into());
    }
    let path = backend_env_for_write();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let mut lines: Vec<String> = if path.is_file() {
        std::fs::read_to_string(&path)
            .map_err(|e| e.to_string())?
            .lines()
            .map(|s| s.to_string())
            .collect()
    } else {
        vec!["# Local only — NEVER commit (gitignored). Managed by PR Agent Control Integrations.".to_string()]
    };
    let prefix = format!("{}=", k);
    let replacement = format!("{}={}", k, v);
    let mut found = false;
    for line in lines.iter_mut() {
        if line.starts_with(&prefix) || line.trim_start().starts_with(&prefix) {
            *line = replacement.clone();
            found = true;
            break;
        }
    }
    if !found {
        lines.push(replacement);
    }
    let mut content = lines.join("\n");
    content.push('\n');
    std::fs::write(&path, content).map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().to_string())
}

/// GET https://api.github.com/user/repos — list repos for the sync picker.
/// Token is passed in memory only, never stored. Returns raw JSON array text.
#[tauri::command]
fn github_list_repos(token: String) -> Result<String, String> {
    let token = token.trim();
    if token.is_empty() {
        return Err("GitHub token is required".into());
    }
    let res = gh_client()?
        .get("https://api.github.com/user/repos?per_page=100&sort=updated")
        .header("Accept", "application/vnd.github+json")
        .header("Authorization", format!("Bearer {}", token))
        .header("X-GitHub-Api-Version", "2022-11-28")
        .send()
        .map_err(|e| format!("GitHub unreachable: {}", e))?;
    let status = res.status();
    let body = res.text().map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("GitHub HTTP {}: {}", status, body.chars().take(300).collect::<String>()));
    }
    Ok(body)
}

/// Generic backend proxy: forwards GET/POST/PUT/PATCH/DELETE to {backend_url}{path}
/// with an optional Bearer API token. Keeps secrets out of the webview fetch path
/// (avoids CORS/preflight issues in Tauri) and never logs tokens.
#[tauri::command]
fn backend_mutate(
    method: String,
    backend_url: String,
    path: String,
    body_json: Option<String>,
    api_token: Option<String>,
) -> Result<String, String> {
    let base = backend_base(&backend_url)?;
    let p = path.trim();
    if !p.starts_with('/') {
        return Err("path must start with '/'".into());
    }
    if p.contains("..") || p.contains('\\') {
        return Err("invalid path".into());
    }
    let m = method.trim().to_uppercase();
    if !["GET", "POST", "PUT", "PATCH", "DELETE"].contains(&m.as_str()) {
        return Err("method not allowed".into());
    }
    let url = format!("{}{}", base, p);
    let client = gh_client()?;
    let mut req = match m.as_str() {
        "POST" => client.post(&url),
        "PUT" => client.put(&url),
        "PATCH" => client.patch(&url),
        "DELETE" => client.delete(&url),
        _ => client.get(&url),
    };
    req = req.header("Accept", "application/json");
    if let Some(t) = api_token.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        req = req.header("Authorization", format!("Bearer {}", t));
    }
    if let Some(b) = body_json.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        let v: serde_json::Value =
            serde_json::from_str(b).map_err(|e| format!("body_json must be valid JSON: {}", e))?;
        req = req.header("Content-Type", "application/json").json(&v);
    }
    let res = req.send().map_err(|e| format!("backend unreachable: {}", e))?;
    let status = res.status();
    let body = res.text().map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("backend HTTP {}: {}", status, body.chars().take(500).collect::<String>()));
    }
    Ok(body)
}

/// POST {backend_url}/runs/{run_id}/approve — used by the Runs approve buttons.
#[tauri::command]
fn backend_approve_run(
    backend_url: String,
    run_id: String,
    api_token: Option<String>,
) -> Result<String, String> {
    let id = run_id.trim();
    if id.is_empty() {
        return Err("run_id is required".into());
    }
    if id.contains('/') || id.contains("..") || id.contains('\\') {
        return Err("invalid run_id".into());
    }
    backend_mutate(
        "POST".into(),
        backend_url,
        format!("/runs/{}/approve", id),
        Some("{}".into()),
        api_token,
    )
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            get_config,
            save_config,
            get_workflow,
            build_cli_command,
            run_local_review,
            box_provision,
            check_tool,
            install_pr_agent,
            get_workflow_template,
            write_workflow,
            github_device_start,
            github_device_poll,
            github_login,
            backend_health,
            backend_sync_agents,
            backend_get_runs,
            backend_get_quota,
            backend_config_status,
            backend_config_test,
            save_backend_env_var,
            backend_mutate,
            backend_approve_run,
            github_list_repos
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn main() {
    run();
}
