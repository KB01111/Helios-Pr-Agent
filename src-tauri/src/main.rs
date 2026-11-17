#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// PR Agent Control — Tauri v2 backend
// Reads/writes .pr_agent.toml + workflow file next to the app root,
// and optionally shells out to a local `pr-agent` CLI if installed.

use std::path::PathBuf;
use std::process::Command;

fn app_root_candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();
    // Installed builds may keep resources next to the executable.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            out.push(dir.to_path_buf());
        }
    }
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

fn toml_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for c in value.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\u{:04X}", c as u32)),
            c => out.push(c),
        }
    }
    out
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
async fn get_config() -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || get_config_blocking())
        .await
        .map_err(|e| e.to_string())?
}

fn get_config_blocking() -> Result<serde_json::Value, String> {
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
async fn save_config(cfg: serde_json::Value) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || save_config_blocking(cfg))
        .await
        .map_err(|e| e.to_string())?
}

fn save_config_blocking(cfg: serde_json::Value) -> Result<String, String> {
    let model = toml_escape(cfg.get("model").and_then(|v| v.as_str()).unwrap_or("openrouter/thinkingmachines/inkling-2-small:free"));
    let fallbacks = cfg.get("fallback_models").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let fb: Vec<String> = fallbacks.iter().filter_map(|v| v.as_str().map(|s| format!("\"{}\"", toml_escape(s)))).collect();
    let max_tokens_raw = cfg.get("custom_model_max_tokens").and_then(|v| v.as_str()).unwrap_or("200000");
    let max_tokens: i64 = max_tokens_raw.trim().parse().map_err(|_| "custom_model_max_tokens must be an integer".to_string())?;
    let effort = toml_escape(cfg.get("reasoning_effort").and_then(|v| v.as_str()).unwrap_or("low"));
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
    if path.is_file() {
        let backup = path.with_extension("toml.bak");
        std::fs::copy(&path, &backup).map_err(|e| format!("failed to back up config: {}", e))?;
    }
    std::fs::write(&path, &content).map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().to_string())
}

#[tauri::command]
async fn get_workflow() -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || get_workflow_blocking())
        .await
        .map_err(|e| e.to_string())?
}

fn get_workflow_blocking() -> Result<serde_json::Value, String> {
    let path = find_file(".github/workflows/pr-agent.yml").ok_or("No workflow file found")?;
    let raw = std::fs::read_to_string(&path).map_err(|e| e.to_string())?;
    Ok(serde_json::json!({ "path": path.to_string_lossy(), "raw": raw }))
}

#[tauri::command]
fn build_cli_command(pr_url: String, command: String) -> String {
    let cmd = command.trim().trim_start_matches('/').to_string();
    format!("pr-agent --pr_url {} {}", pr_url.trim(), cmd)
}

/// Split a command line like `ask "what changed?"` into separate arguments
/// (whitespace-separated, with single/double quote grouping).
fn split_cli_args(input: &str) -> Vec<String> {
    let mut args = Vec::new();
    let mut cur = String::new();
    let mut quote: Option<char> = None;
    let mut started = false;
    for c in input.chars() {
        match (quote, c) {
            (Some(q), c) if c == q => quote = None,
            (Some(_), c) => cur.push(c),
            (None, '"') | (None, '\'') => {
                quote = Some(c);
                started = true;
            }
            (None, c) if c.is_whitespace() => {
                if started || !cur.is_empty() {
                    args.push(std::mem::take(&mut cur));
                    started = false;
                }
            }
            (None, c) => cur.push(c),
        }
    }
    if started || !cur.is_empty() {
        args.push(cur);
    }
    args
}

fn render_process_output(out: &std::process::Output) -> String {
    let mut s = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();
    if !stderr.is_empty() {
        s.push_str("\n[stderr]\n");
        s.push_str(&stderr);
    }
    s
}

fn process_failure(label: &str, out: &std::process::Output) -> String {
    let body = render_process_output(out);
    format!(
        "{} failed (exit {}): {}",
        label,
        out.status,
        if body.trim().is_empty() { "(no output)" } else { body.trim() }
    )
}

#[tauri::command]
async fn run_local_review(pr_url: String, command: String, openrouter_key: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || run_local_review_blocking(pr_url, command, openrouter_key))
        .await
        .map_err(|e| e.to_string())?
}

fn run_local_review_blocking(pr_url: String, command: String, openrouter_key: String) -> Result<String, String> {
    let cmd = command.trim().trim_start_matches('/').to_string();
    let cmd_args = split_cli_args(&cmd);
    let url = pr_url.trim();
    let python_fallback = || {
        Command::new("python")
            .args(["-m", "pr_agent.cli", "--pr_url", url])
            .args(&cmd_args)
            .env("OPENROUTER__KEY", &openrouter_key)
            .output()
    };

    // Try `pr-agent` on PATH first, then `python -m pr_agent`.
    match Command::new("pr-agent")
        .args(["--pr_url", url])
        .args(&cmd_args)
        .env("OPENROUTER__KEY", &openrouter_key)
        .output()
    {
        Ok(out) if out.status.success() => Ok(render_process_output(&out)),
        Ok(out) => {
            let first_error = process_failure("pr-agent", &out);
            match python_fallback() {
                Ok(py) if py.status.success() => Ok(render_process_output(&py)),
                Ok(py) => Err(format!("{}; {}", first_error, process_failure("python -m pr_agent", &py))),
                Err(_) => Err(first_error),
            }
        }
        Err(first_err) => match python_fallback() {
            Ok(py) if py.status.success() => Ok(render_process_output(&py)),
            Ok(py) => Err(process_failure("python -m pr_agent", &py)),
            Err(_) => Err(format!(
                "No local `pr-agent` found ({}). Install: pip install \"pr-agent[github]\" — then retry. CLI would be: pr-agent --pr_url {} {}",
                first_err, url, cmd
            )),
        },
    }
}

#[tauri::command]
async fn box_provision(
    box_key: String,
    github_token: String,
    openrouter_key: String,
    repos: String,
    box_name: String,
    cron: String,
    model: String,
    status_only: bool,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || box_provision_blocking(box_key, github_token, openrouter_key, repos, box_name, cron, model, status_only))
        .await
        .map_err(|e| e.to_string())?
}

fn box_provision_blocking(
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
    if: >-
      ${{ github.event.sender.type != 'Bot' &&
          (github.event_name != 'issue_comment' ||
           contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association)) }}
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write
      pull-requests: write
    steps:
      - name: PR Agent action step
        # Pinned so a compromised upstream branch cannot receive repository secrets.
        uses: the-pr-agent/pr-agent@dec3e88c029c0a32957bd95aa694cb34d2b256e0
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
        # Pinned so a compromised upstream branch cannot receive repository secrets.
        uses: aryanbrite/openrabbit@0350c5ff79e6efb3ba92898a46a2e420ae1c526c
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
async fn check_tool(name: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || check_tool_blocking(name))
        .await
        .map_err(|e| e.to_string())?
}

fn check_tool_blocking(name: String) -> Result<String, String> {
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
async fn install_pr_agent() -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || install_pr_agent_blocking())
        .await
        .map_err(|e| e.to_string())?
}

fn install_pr_agent_blocking() -> Result<String, String> {
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
async fn get_workflow_template(provider: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || get_workflow_template_blocking(provider))
        .await
        .map_err(|e| e.to_string())?
}

fn get_workflow_template_blocking(provider: String) -> Result<String, String> {
    match provider.as_str() {
        "pr-agent" => Ok(PR_AGENT_WORKFLOW.to_string()),
        "openrabbit" => Ok(OPENRABBIT_WORKFLOW.to_string()),
        _ => Err("unknown provider".into()),
    }
}

#[tauri::command]
async fn write_workflow(provider: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || write_workflow_blocking(provider))
        .await
        .map_err(|e| e.to_string())?
}

fn write_workflow_blocking(provider: String) -> Result<String, String> {
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
    if path.is_file() {
        let backup = path.with_extension(format!(
            "{}.bak",
            path.extension().and_then(|e| e.to_str()).unwrap_or("yml")
        ));
        std::fs::copy(&path, &backup).map_err(|e| format!("failed to back up workflow: {}", e))?;
    }
    std::fs::write(&path, content).map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().to_string())
}

fn gh_client() -> Result<reqwest::blocking::Client, String> {
    reqwest::blocking::Client::builder()
        .user_agent("pr-agent-control")
        .timeout(std::time::Duration::from_secs(30))
        .connect_timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())
}

/// Step 1 of GitHub OAuth device flow: returns device_code + user_code.
/// Needs only the OAuth App client_id (no secret). Enable Device Flow in the app settings.
#[tauri::command]
async fn github_device_start(client_id: String) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || github_device_start_blocking(client_id))
        .await
        .map_err(|e| e.to_string())?
}

fn github_device_start_blocking(client_id: String) -> Result<serde_json::Value, String> {
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
async fn github_device_poll(
    client_id: String,
    device_code: String,
    interval_secs: u64,
    max_wait_secs: u64,
) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || github_device_poll_blocking(client_id, device_code, interval_secs, max_wait_secs))
        .await
        .map_err(|e| e.to_string())?
}

fn github_device_poll_blocking(
    client_id: String,
    device_code: String,
    interval_secs: u64,
    max_wait_secs: u64,
) -> Result<serde_json::Value, String> {
    let client = gh_client()?;
    let mut interval = interval_secs.clamp(5, 60);
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
            "authorization_pending" => {}
            // RFC 8628: back off by 5s when GitHub asks us to slow down.
            "slow_down" => interval = (interval + 5).min(60),
            other => return Err(format!("OAuth error ({}): {}", other, res)),
        }
        if std::time::Instant::now() >= deadline {
            return Ok(serde_json::json!({ "status": "pending", "interval": interval }));
        }
    }
}

fn github_login_inner(
    client: &reqwest::blocking::Client,
    token: &str,
) -> Result<String, String> {
    let res = client
        .get("https://api.github.com/user")
        .header("Accept", "application/vnd.github+json")
        .header("Authorization", format!("Bearer {}", token))
        .send()
        .map_err(|e| e.to_string())?;
    let status = res.status();
    let body: serde_json::Value = res.json().map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("GitHub rejected the token (HTTP {}): {}", status, body));
    }
    let login = body
        .get("login")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if login.is_empty() {
        return Err("GitHub did not return a login for this token".into());
    }
    Ok(login)
}

/// Resolve the login for a token (OAuth or PAT). Used to confirm auth + auto-fill.
#[tauri::command]
async fn github_login(token: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || github_login_blocking(token))
        .await
        .map_err(|e| e.to_string())?
}

fn github_login_blocking(token: String) -> Result<String, String> {
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

/// Refuse to send a bearer token over plaintext HTTP to a non-loopback host.
fn require_secure_for_token(url: &str, has_token: bool) -> Result<(), String> {
    if !has_token || url.starts_with("https://") {
        return Ok(());
    }
    let rest = url.strip_prefix("http://").unwrap_or(url);
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host = if let Some(stripped) = authority.strip_prefix('[') {
        stripped.split(']').next().unwrap_or("")
    } else {
        authority.rsplit('@').next().unwrap_or("").split(':').next().unwrap_or("")
    };
    let h = host.to_ascii_lowercase();
    if h == "localhost" || h.ends_with(".localhost") || h == "127.0.0.1" || h == "::1" {
        Ok(())
    } else {
        Err("refusing to send the API token over plain http:// to a non-loopback backend; use https://".into())
    }
}

fn backend_get(url: &str, api_token: Option<&str>) -> Result<String, String> {
    require_secure_for_token(url, api_token.map(str::trim).map_or(false, |t| !t.is_empty()))?;
    let mut req = gh_client()?
        .get(url)
        .header("Accept", "application/json");
    if let Some(token) = api_token.map(str::trim).filter(|s| !s.is_empty()) {
        req = req.header("Authorization", format!("Bearer {}", token));
    }
    let res = req
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
async fn backend_health(backend_url: String, api_token: Option<String>) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || backend_health_blocking(backend_url, api_token))
        .await
        .map_err(|e| e.to_string())?
}

fn backend_health_blocking(backend_url: String, api_token: Option<String>) -> Result<String, String> {
    let base = backend_base(&backend_url)?;
    backend_get(&format!("{}/health", base), api_token.as_deref())
}

/// GET {backend_url}/agents — list agents (UI does POST/PUT/DELETE via fetch).
/// `agents_json` is accepted for API symmetry and currently ignored.
#[tauri::command]
async fn backend_sync_agents(
    backend_url: String,
    agents_json: String,
    api_token: Option<String>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || backend_sync_agents_blocking(backend_url, agents_json, api_token))
        .await
        .map_err(|e| e.to_string())?
}

fn backend_sync_agents_blocking(
    backend_url: String,
    agents_json: String,
    api_token: Option<String>,
) -> Result<String, String> {
    let _ = agents_json;
    let base = backend_base(&backend_url)?;
    backend_get(&format!("{}/agents", base), api_token.as_deref())
}

/// GET {backend_url}/runs?limit=50 — recent background runs.
#[tauri::command]
async fn backend_get_runs(backend_url: String, api_token: Option<String>) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || backend_get_runs_blocking(backend_url, api_token))
        .await
        .map_err(|e| e.to_string())?
}

fn backend_get_runs_blocking(backend_url: String, api_token: Option<String>) -> Result<String, String> {
    let base = backend_base(&backend_url)?;
    backend_get(&format!("{}/runs?limit=50", base), api_token.as_deref())
}

/// GET {backend_url}/quota — Upstash Box / Vercel Sandbox usage.
#[tauri::command]
async fn backend_get_quota(backend_url: String, api_token: Option<String>) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || backend_get_quota_blocking(backend_url, api_token))
        .await
        .map_err(|e| e.to_string())?
}

fn backend_get_quota_blocking(backend_url: String, api_token: Option<String>) -> Result<String, String> {
    let base = backend_base(&backend_url)?;
    backend_get(&format!("{}/quota", base), api_token.as_deref())
}

/// GET {backend_url}/config/status — which providers are configured (booleans only, no secrets).
#[tauri::command]
async fn backend_config_status(
    backend_url: String,
    api_token: Option<String>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || backend_config_status_blocking(backend_url, api_token))
        .await
        .map_err(|e| e.to_string())?
}

fn backend_config_status_blocking(
    backend_url: String,
    api_token: Option<String>,
) -> Result<String, String> {
    let base = backend_base(&backend_url)?;
    backend_get(&format!("{}/config/status", base), api_token.as_deref())
}

/// POST {backend_url}/config/test {"provider": ...} — live connectivity check (no secrets echoed).
#[tauri::command]
async fn backend_config_test(
    backend_url: String,
    provider: String,
    api_token: String,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || backend_config_test_blocking(backend_url, provider, api_token))
        .await
        .map_err(|e| e.to_string())?
}

fn backend_config_test_blocking(
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
    require_secure_for_token(&base, !api_token.trim().is_empty())?;
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

/// Render one value for a dotenv file. Unquoted values only when they are
/// plainly safe; otherwise single-quote (no interpolation is used when the
/// backend loads the file), falling back to double quotes for values that
/// contain a single quote.
fn dotenv_render_value(value: &str) -> Result<String, String> {
    let safe = !value.is_empty()
        && value.chars().all(|c| {
            c.is_ascii_alphanumeric() || "._~:/?&=+@%*,;!()-[]{}".contains(c)
        });
    if safe {
        return Ok(value.to_string());
    }
    // python-dotenv decodes backslash escapes even inside single quotes, so
    // single-quote only values with neither backslashes nor single quotes.
    if !value.contains('\'') && !value.contains('\\') {
        return Ok(format!("'{}'", value));
    }
    // Double quotes are still interpolated (${VAR}); refuse what we cannot
    // store faithfully rather than silently corrupt a credential.
    if value.contains("${") {
        return Err("value contains '${' together with quotes/backslashes and cannot be stored safely".into());
    }
    let escaped = value.replace('\\', "\\\\").replace('"', "\\\"");
    Ok(format!("\"{}\"", escaped))
}

static ENV_WRITE_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// Write one integration secret into backend/.env (creates the file if needed).
/// The value is never logged and never touches localStorage — it stays in
/// Rust + the dotenv file, which is git-ignored.
#[tauri::command]
async fn save_backend_env_var(key: String, value: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || save_backend_env_var_blocking(key, value))
        .await
        .map_err(|e| e.to_string())?
}

fn save_backend_env_var_blocking(key: String, value: String) -> Result<String, String> {
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
    // Serialize the whole read-modify-write so concurrent saves don't drop keys.
    let _guard = ENV_WRITE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
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
    let replacement = format!("{}={}", k, dotenv_render_value(v)?);
    // Replace the first occurrence and drop later duplicates so the value that
    // python-dotenv resolves is unambiguous.
    let mut replaced = false;
    lines.retain_mut(|line| {
        if !(line.starts_with(&prefix) || line.trim_start().starts_with(&prefix)) {
            return true;
        }
        if replaced {
            return false;
        }
        *line = replacement.clone();
        replaced = true;
        true
    });
    if !replaced {
        lines.push(replacement);
    }
    let mut content = lines.join("\n");
    content.push('\n');
    std::fs::write(&path, content).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(path.to_string_lossy().to_string())
}

/// GET https://api.github.com/user/repos — list repos for the sync picker.
/// Token is passed in memory only, never stored. Returns raw JSON array text.
#[tauri::command]
async fn github_list_repos(token: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || github_list_repos_blocking(token))
        .await
        .map_err(|e| e.to_string())?
}

fn github_list_repos_blocking(token: String) -> Result<String, String> {
    let token = token.trim();
    if token.is_empty() {
        return Err("GitHub token is required".into());
    }
    let client = gh_client()?;
    let mut all: Vec<serde_json::Value> = Vec::new();
    // Five pages (500 repos) is plenty for the picker and bounds latency.
    for page in 1..=5 {
        let res = client
            .get(format!(
                "https://api.github.com/user/repos?per_page=100&sort=updated&page={}",
                page
            ))
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
        let batch: Vec<serde_json::Value> = serde_json::from_str(&body)
            .map_err(|e| format!("GitHub response was not a JSON array: {}", e))?;
        let count = batch.len();
        all.extend(batch);
        if count < 100 {
            break;
        }
    }
    serde_json::to_string(&all).map_err(|e| e.to_string())
}

/// Generic backend proxy: forwards GET/POST/PUT/PATCH/DELETE to {backend_url}{path}
/// with an optional Bearer API token. Keeps secrets out of the webview fetch path
/// (avoids CORS/preflight issues in Tauri) and never logs tokens.
#[tauri::command]
async fn backend_mutate(
    method: String,
    backend_url: String,
    path: String,
    body_json: Option<String>,
    api_token: Option<String>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || backend_mutate_blocking(method, backend_url, path, body_json, api_token))
        .await
        .map_err(|e| e.to_string())?
}

fn backend_mutate_blocking(
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
    require_secure_for_token(&url, api_token.as_deref().map(str::trim).map_or(false, |t| !t.is_empty()))?;
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
async fn backend_approve_run(
    backend_url: String,
    run_id: String,
    api_token: Option<String>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || backend_approve_run_blocking(backend_url, run_id, api_token))
        .await
        .map_err(|e| e.to_string())?
}

fn backend_approve_run_blocking(
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
    backend_mutate_blocking(
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
