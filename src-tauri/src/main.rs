#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// PR Agent Control — Tauri v2 backend
// Manages app configuration and repository workflows, and runs local PR-Agent
// through the selected Python interpreter.

mod local_runtime;

mod autoreview;
mod daemon;
mod decider;
mod github_app;
mod oauth;

use autoreview::WorkflowOptions;

use base64::engine::general_purpose::STANDARD;
use base64::Engine as _;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;
use tauri::Manager;

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

fn app_root_dir() -> PathBuf {
    if let Some(path) = find_file(".pr_agent.toml") {
        if let Some(parent) = path.parent() {
            return parent.to_path_buf();
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            return dir.to_path_buf();
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

/// PR-Agent gets its own venv so installing it cannot upgrade/downgrade the
/// FastAPI backend's packages in the ambient Python environment.
fn pr_agent_venv_dir() -> PathBuf {
    if let Ok(path) = std::env::var("PR_AGENT_VENV") {
        let path = path.trim();
        if !path.is_empty() {
            return PathBuf::from(path);
        }
    }
    if let Ok(local) = std::env::var("LOCALAPPDATA") {
        let local = local.trim();
        if !local.is_empty() {
            return PathBuf::from(local)
                .join("PR Agent Control")
                .join("pr-agent-venv");
        }
    }
    app_root_dir().join(".venv")
}

fn venv_python(venv: &Path) -> PathBuf {
    if cfg!(windows) {
        venv.join("Scripts").join("python.exe")
    } else {
        venv.join("bin").join("python")
    }
}

fn venv_bin_dir(venv: &Path) -> PathBuf {
    if cfg!(windows) {
        venv.join("Scripts")
    } else {
        venv.join("bin")
    }
}

/// Prefer the explicit PR_AGENT_PYTHON override, then the app-managed venv,
/// then the ambient `python`.
fn pr_agent_python() -> PathBuf {
    if let Ok(path) = std::env::var("PR_AGENT_PYTHON") {
        let path = path.trim();
        if !path.is_empty() {
            return PathBuf::from(path);
        }
    }
    let python = venv_python(&pr_agent_venv_dir());
    if python.is_file() {
        python
    } else {
        PathBuf::from("python")
    }
}

fn add_venv_to_path(cmd: &mut Command) {
    let bin = venv_bin_dir(&pr_agent_venv_dir());
    if !bin.is_dir() {
        return;
    }
    if let Some(path) = std::env::var_os("PATH") {
        let mut paths = vec![bin];
        paths.extend(std::env::split_paths(&path));
        if let Ok(joined) = std::env::join_paths(paths) {
            cmd.env("PATH", joined);
        }
    }
}

fn ensure_pr_agent_venv() -> Result<PathBuf, String> {
    let venv = pr_agent_venv_dir();
    let python = venv_python(&venv);
    let rebuild = python.is_file();
    if rebuild && local_runtime::ensure_pip(&python).is_ok() {
        return Ok(python);
    }
    if let Some(parent) = venv.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("failed to create {}: {}", parent.display(), e))?;
    }
    let base = std::env::var("PR_AGENT_BASE_PYTHON").unwrap_or_else(|_| "python".to_string());
    let mut create = Command::new(&base);
    create.args(["-m", "venv"]);
    // If ensurepip cannot repair an existing interpreter, recreate only the
    // incomplete managed environment. Healthy environments returned above.
    if rebuild {
        create.arg("--clear");
    }
    let out = create
        .arg(&venv)
        .output()
        .map_err(|e| format!("failed to run {} -m venv: {}", base, e))?;
    if !out.status.success() {
        return Err(format!(
            "failed to create venv at {}: {}",
            venv.display(),
            render_process_output(&out).trim()
        ));
    }
    if !python.is_file() {
        return Err(format!(
            "venv created but Python was not found at {}",
            python.display()
        ));
    }
    local_runtime::ensure_pip(&python)?;
    Ok(python)
}

static CONFIG_PATH: OnceLock<PathBuf> = OnceLock::new();
pub(crate) static APP_CONFIG_DIR: OnceLock<PathBuf> = OnceLock::new();

fn config_path_for_write() -> Result<PathBuf, String> {
    let path = CONFIG_PATH.get().ok_or("app configuration is not initialized")?;
    local_runtime::prepare_config(path)?;
    Ok(path.clone())
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

/// API base for each provider id. Shared by save_config and run_local_review.
fn llm_api_base(provider: &str) -> Result<&'static str, String> {
    match provider {
        "openrouter" => Ok("https://openrouter.ai/api/v1"),
        "commandcode" => Ok("https://api.commandcode.ai/provider/v1"),
        "opencode_go" => Ok("https://opencode.ai/inference/go/openai/v1"),
        _ => Err(unknown_llm_provider(provider)),
    }
}

fn unknown_llm_provider(provider: &str) -> String {
    if provider.is_empty() {
        "llm_provider is required (use: openrouter, commandcode, opencode_go)".into()
    } else {
        format!("unknown llm_provider `{}` (use: openrouter, commandcode, opencode_go)", provider)
    }
}

/// Value of `api_base` inside the `[openai]` section only.
fn openai_api_base(src: &str) -> Option<String> {
    let doc = src.parse::<toml_edit::Document>().ok()?;
    doc.get("openai")?.get("api_base")?.as_str().map(str::to_owned)
}

/// Legacy configs have no `[openai]` section and fall back to OpenRouter.
fn detect_llm_provider(src: &str) -> &'static str {
    match openai_api_base(src) {
        Some(base) if base.contains("api.commandcode.ai") => "commandcode",
        Some(base) if base.contains("opencode.ai/inference/go") => "opencode_go",
        _ => "openrouter",
    }
}

fn default_llm_model(provider: &str) -> &'static str {
    match provider {
        "commandcode" => "openai/deepseek/deepseek-v4-flash",
        "opencode_go" => "openai/kimi-k3",
        _ => "openrouter/thinkingmachines/inkling-2-small:free",
    }
}

fn validate_run_provider(raw: &str, provider: &str) -> Result<(), String> {
    let saved_provider = detect_llm_provider(raw);
    let doc = raw.parse::<toml_edit::Document>().map_err(|e| e.to_string())?;
    let model = doc
        .get("config")
        .and_then(|c| c.get("model"))
        .and_then(|m| m.as_str());
    if saved_provider != provider
        || (provider != "openrouter"
            && model.is_none_or(|m| {
                m.starts_with("openrouter/") || m == "thinkingmachines/inkling-2-small:free"
            }))
    {
        return Err("Save config with the selected provider and a matching model before running a local review.".into());
    }
    Ok(())
}

/// Env vars PR-Agent needs for the selected provider's key.
fn llm_provider_env(provider: &str, key: &str) -> Result<Vec<(&'static str, String)>, String> {
    match provider {
        "openrouter" => Ok(vec![("OPENROUTER__KEY", key.to_string())]),
        "commandcode" | "opencode_go" => Ok(vec![
            ("OPENAI__KEY", key.to_string()),
            ("OPENAI__API_BASE", llm_api_base(provider)?.to_string()),
        ]),
        _ => Err(unknown_llm_provider(provider)),
    }
}

#[tauri::command]
async fn get_config() -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(get_config_blocking)
        .await
        .map_err(|e| e.to_string())?
}

fn get_config_blocking() -> Result<serde_json::Value, String> {
    let path = config_path_for_write()?;
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
        "llm_provider": detect_llm_provider(&raw),
        "model": get_val(&raw, "model").unwrap_or_else(|| default_llm_model(detect_llm_provider(&raw)).into()),
        "fallback_models": fallbacks,
        "max_model_tokens": get_val(&raw, "max_model_tokens")
            .or_else(|| get_val(&raw, "custom_model_max_tokens"))
            .unwrap_or_else(|| "200000".into()),
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
    let content = render_config_toml(&cfg)?;
    let path = config_path_for_write()?;
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

fn render_config_toml(cfg: &serde_json::Value) -> Result<String, String> {
    let provider = cfg
        .get("llm_provider")
        .and_then(|v| v.as_str())
        .map(|p| p.trim().to_lowercase())
        .unwrap_or_else(|| "openrouter".to_string());
    let llm_section = match provider.as_str() {
        "openrouter" => "[openrouter]\n# key is injected via env OPENROUTER__KEY / secrets.OPENROUTER_API_KEY - don't hardcode\n".to_string(),
        _ => format!(
            "[openai]\n# key is injected via env OPENAI__KEY / secrets - don't hardcode\napi_base = \"{}\"\n",
            llm_api_base(&provider)?
        ),
    };
    let model = toml_escape(cfg.get("model").and_then(|v| v.as_str()).unwrap_or_else(|| default_llm_model(&provider)));
    let fallbacks = cfg.get("fallback_models").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let fb: Vec<String> = fallbacks.iter().filter_map(|v| v.as_str().map(|s| format!("\"{}\"", toml_escape(s)))).collect();
    let max_tokens_raw = cfg
        .get("max_model_tokens")
        .or_else(|| cfg.get("custom_model_max_tokens"))
        .and_then(|v| v.as_str())
        .unwrap_or("200000");
    let max_tokens: i64 = max_tokens_raw
        .trim()
        .parse()
        .map_err(|_| "max_model_tokens must be an integer".to_string())?;
    let effort = toml_escape(cfg.get("reasoning_effort").and_then(|v| v.as_str()).unwrap_or("low"));
    let b = |k: &str, d: bool| cfg.get(k).and_then(|v| v.as_bool()).unwrap_or(d);

    Ok(format!(
        r#"[config]
# Managed by PR Agent Control app
model = "{model}"
fallback_models = [{fb}]
max_model_tokens = {max_tokens}
custom_model_max_tokens = {max_tokens}
reasoning_effort = "{effort}"

{llm_section}
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
    ))
}

#[tauri::command]
async fn get_workflow() -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(get_workflow_blocking)
        .await
        .map_err(|e| e.to_string())?
}

fn get_workflow_blocking() -> Result<serde_json::Value, String> {
    let path = find_file(".github/workflows/pr-agent.yml").ok_or("No workflow file found")?;
    let raw = std::fs::read_to_string(&path).map_err(|e| e.to_string())?;
    Ok(serde_json::json!({ "path": path.to_string_lossy(), "raw": raw }))
}

#[tauri::command]
fn build_cli_command(pr_url: String, command: String, question: Option<String>) -> Result<String, String> {
    let path = config_path_for_write()?;
    let mut args = vec![
        pr_agent_python().to_string_lossy().into_owned(),
        "-m".into(),
        "pr_agent.cli".into(),
        "--pr_url".into(),
        pr_url.trim().into(),
        "--extra_config_url".into(),
        path.to_string_lossy().into_owned(),
    ];
    args.extend(command_args(&command, question));
    Ok(local_runtime::shell_command(&args, cfg!(windows)))
}

/// Arguments for the PR-Agent command. A free-text `/ask` question is passed as a
/// structured value so quotes and spacing survive unchanged (the splitter below
/// has no escape syntax).
fn command_args(command: &str, question: Option<String>) -> Vec<String> {
    let cmd = command.trim().trim_start_matches('/');
    match question {
        Some(q) if !q.trim().is_empty() && cmd == "ask" => vec!["ask".to_string(), q.trim().to_string()],
        _ => split_cli_args(cmd),
    }
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

fn strip_ansi(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut chars = input.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\x1b' && chars.peek() == Some(&'[') {
            chars.next();
            while let Some(code) = chars.next() {
                if ('@'..='~').contains(&code) {
                    break;
                }
            }
        } else {
            out.push(c);
        }
    }
    out
}

fn render_process_output(out: &std::process::Output) -> String {
    let mut s = strip_ansi(&String::from_utf8_lossy(&out.stdout));
    let stderr = strip_ansi(&String::from_utf8_lossy(&out.stderr));
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

fn is_missing_pr_agent(out: &std::process::Output) -> bool {
    let stderr = String::from_utf8_lossy(&out.stderr);
    stderr.contains("No module named 'pr_agent'")
        || stderr.contains("No module named \"pr_agent\"")
        || stderr.contains("No module named pr_agent")
}

fn missing_pr_agent_message(python: &str, details: &str) -> String {
    format!(
        "PR-Agent CLI is not installed for `{}`. Open Providers → Install CLI (pip), or run: \"{}\" -m pip install \"pr-agent[github]==0.50.0\" — then retry.\n{}",
        python, python, details
    )
}

#[tauri::command]
async fn run_local_review(
    pr_url: String,
    command: String,
    llm_provider: String,
    llm_key: String,
    github_token: String,
    question: Option<String>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        run_local_review_blocking(pr_url, command, llm_provider, llm_key, github_token, question)
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Stable per-PR session id for OpenCode Go. The API only needs a stable string;
/// a small deterministic hash keeps local CLI runs on the same conversation.
fn opencode_session_id(pr_url: &str) -> String {
    let mut h1: u64 = 0xcbf2_9ce4_8422_2325;
    let mut h2: u64 = 0x8422_2325_cbf2_9ce4;
    for byte in pr_url.as_bytes() {
        h1 = (h1 ^ u64::from(*byte)).wrapping_mul(0x100_0000_01b3);
        h2 = (h2.rotate_left(7) ^ u64::from(*byte)).wrapping_mul(0x100_0000_01b3);
    }
    format!("ses_{:016x}{:08x}", h1, (h2 & 0xffff_ffff) as u32)
}

fn run_local_review_blocking(
    pr_url: String,
    command: String,
    llm_provider: String,
    llm_key: String,
    github_token: String,
    question: Option<String>,
) -> Result<String, String> {
    let provider = llm_provider.trim().to_lowercase();
    let llm_env = llm_provider_env(&provider, llm_key.trim())?;
    let cmd_args = command_args(&command, question);
    let url = pr_url.trim();
    let config_path = config_path_for_write()?;
    let raw = std::fs::read_to_string(&config_path).map_err(|e| e.to_string())?;
    validate_run_provider(&raw, &provider)?;
    let python_label = pr_agent_python().display().to_string();

    let build = |program: &str, module: Option<&str>| {
        let mut c = Command::new(program);
        add_venv_to_path(&mut c);
        if let Some(m) = module {
            c.args(["-m", m]);
        }
        c.args(["--pr_url", url]);
        // The target repo's default branch may not contain a .pr_agent.toml.
        // Pass the app's config explicitly so the selected model/limits apply.
        c.arg("--extra_config_url").arg(&config_path);
        c.args(&cmd_args);
        for (name, value) in &llm_env {
            c.env(name, value);
        }
        if provider == "opencode_go" {
            // PR-Agent's LiteLLM handler forwards [litellm] extra_headers on every
            // request; this is the supported way to send OpenCode's session header.
            let headers = serde_json::json!({
                "x-opencode-session": opencode_session_id(url),
            })
            .to_string();
            c.env("LITELLM__EXTRA_HEADERS", headers);
        }
        // Without this, PR-Agent can print a model/provider error and still
        // exit 0, which the app would report as a successful review.
        c.env("CONFIG__PROPAGATE_TOOL_ERRORS", "true");
        // Free models can take longer than the 120 s default on a large PR.
        c.env("CONFIG__AI_TIMEOUT", "300");
        if !github_token.trim().is_empty() {
            c.env("GITHUB__USER_TOKEN", github_token.trim());
        }
        c
    };

    let run_python = || match build(&python_label, Some("pr_agent.cli")).output() {
        Ok(py) if py.status.success() => Ok(render_process_output(&py)),
        Ok(py) => {
            let py_error = process_failure(&format!("{} -m pr_agent.cli", python_label), &py);
            if is_missing_pr_agent(&py) {
                Err(missing_pr_agent_message(&python_label, &py_error))
            } else {
                Err(py_error)
            }
        }
        Err(e) => Err(format!("failed to run {}: {}", python_label, e)),
    };

    // Always invoke the Python module, never the console-script shim: the shim
    // can block under the app's piped stdio. This uses the app venv when
    // present, otherwise the ambient Python.
    run_python()
}

#[tauri::command]
#[allow(clippy::too_many_arguments)]
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

#[allow(clippy::too_many_arguments)]
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

fn version_output(out: &std::process::Output) -> String {
    let mut s = String::from_utf8_lossy(&out.stdout).to_string();
    s.push_str(&String::from_utf8_lossy(&out.stderr));
    s.trim().to_string()
}

fn check_tool_blocking(name: String) -> Result<String, String> {
    let allowed = ["pr-agent", "python", "pip", "node", "gh"];
    if !allowed.contains(&name.as_str()) {
        return Err("tool not allowed".into());
    }

    if name == "pr-agent" {
        // Use the Python module, not the console-script shim: the shim can
        // block under the app's piped stdio, while `-m` returns immediately.
        let python = pr_agent_python();
        let out = Command::new(&python)
            .args(["-m", "pr_agent.cli", "--version"])
            .output()
            .map_err(|e| {
                format!(
                    "pr-agent not found ({} not runnable: {})",
                    python.display(),
                    e
                )
            })?;
        let s = strip_ansi(&version_output(&out));
        if out.status.success() {
            let label = if s.is_empty() {
                "pr-agent installed".to_string()
            } else {
                s
            };
            return Ok(format!("{} (via {})", label, python.display()));
        }
        if is_missing_pr_agent(&out) {
            return Err(missing_pr_agent_message(
                &python.display().to_string(),
                &s,
            ));
        }
        return Err(format!("pr-agent check failed: {}", s));
    }

    let program = if name == "python" {
        pr_agent_python()
    } else {
        PathBuf::from(&name)
    };
    let mut cmd = Command::new(&program);
    add_venv_to_path(&mut cmd);
    let out = cmd
        .arg("--version")
        .output()
        .map_err(|e| format!("{} not found: {}", program.display(), e))?;
    let s = version_output(&out);
    if out.status.success() {
        Ok(if s.is_empty() {
            format!("{} installed", program.display())
        } else {
            s
        })
    } else {
        Err(format!("{} check failed: {}", program.display(), s))
    }
}

#[tauri::command]
async fn install_pr_agent() -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(install_pr_agent_blocking)
        .await
        .map_err(|e| e.to_string())?
}

fn install_pr_agent_blocking() -> Result<String, String> {
    // Dedicated venv: installing PR-Agent must not upgrade/downgrade the
    // FastAPI backend's packages in the ambient Python environment.
    let python = ensure_pr_agent_venv()?;
    let out = Command::new(&python)
        .args([
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--upgrade",
            "pr-agent[github]==0.50.0",
        ])
        .output()
        .map_err(|e| format!("failed to run pip in {}: {}", python.display(), e))?;
    let mut s = String::from_utf8_lossy(&out.stdout).to_string();
    s.push_str(&String::from_utf8_lossy(&out.stderr));
    if out.status.success() {
        let tail: Vec<&str> = s.lines().collect();
        let start = tail.len().saturating_sub(25);
        Ok(format!(
            "Installed into {}\n{}",
            python.display(),
            tail[start..].join("\n")
        ))
    } else {
        Err(format!(
            "pip install failed in {}: {}",
            python.display(),
            s
        ))
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
        "pr-agent" => Ok(autoreview::default_pr_agent_workflow()),
        "openrabbit" => Ok(OPENRABBIT_WORKFLOW.to_string()),
        _ => Err("unknown provider".into()),
    }
}

/// Render the PR Agent workflow for the given auto-review settings (preview only).
#[tauri::command]
fn preview_pr_agent_workflow(options: WorkflowOptions) -> Result<String, String> {
    Ok(autoreview::render_pr_agent_workflow(&options.normalized()?))
}

#[tauri::command]
async fn write_workflow(provider: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || write_workflow_blocking(provider))
        .await
        .map_err(|e| e.to_string())?
}

fn write_workflow_blocking(provider: String) -> Result<String, String> {
    let (file, content) = match provider.as_str() {
        "pr-agent" => ("pr-agent.yml", autoreview::default_pr_agent_workflow()),
        "openrabbit" => ("openrabbit.yml", OPENRABBIT_WORKFLOW.to_string()),
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
        .json(&serde_json::json!({ "client_id": client_id, "scope": "repo workflow" }))
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
    require_secure_for_token(url, api_token.map(str::trim).is_some_and(|t| !t.is_empty()))?;
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
    let allowed = ["github", "redis", "vercel", "upstash_box", "openrouter", "commandcode", "opencode_go"];
    if !allowed.contains(&p.as_str()) {
        return Err("unknown provider (use: github, redis, vercel, upstash_box, openrouter, commandcode, opencode_go)".into());
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
            | "REDIS_URL"
            | "VERCEL_TOKEN"
            | "VERCEL_API_TOKEN"
            | "VERCEL_TEAM_ID"
            | "OPENROUTER_API_KEY"
            | "COMMANDCODE_API_KEY"
            | "CMD_API_KEY"
            | "OPENCODE_GO_API_KEY"
            | "OPENCODE_API_KEY"
            | "LLM_PROVIDER"
            | "LLM_MODEL"
            | "ALLOWED_ORIGINS"
            | "BACKEND_API_TOKEN"
            | "GITHUB_APP_ID"
            | "GITHUB_APP_SLUG"
            | "GITHUB_APP_PRIVATE_KEY"
            | "GITHUB_APP_WEBHOOK_SECRET"
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
    if v.contains('\n') || v.contains('\r') {
        return Err("value must be a single line".into());
    }
    // Serialize the whole read-modify-write so concurrent saves don't drop keys.
    let _guard = ENV_WRITE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let path = backend_env_for_write();
    if v.is_empty() {
        if !path.is_file() {
            return Ok(path.to_string_lossy().to_string());
        }
        let prefix = format!("{}=", k);
        let lines: Vec<String> = std::fs::read_to_string(&path)
            .map_err(|e| e.to_string())?
            .lines()
            .filter(|line| !(line.starts_with(&prefix) || line.trim_start().starts_with(&prefix)))
            .map(|s| s.to_string())
            .collect();
        let mut content = lines.join("\n");
        if !content.is_empty() {
            content.push('\n');
        }
        std::fs::write(&path, content).map_err(|e| e.to_string())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
        }
        return Ok(path.to_string_lossy().to_string());
    }
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

/// `owner/name`, each part 1-100 chars of [A-Za-z0-9_.-].
fn valid_repo_name(repo: &str) -> bool {
    let mut parts = repo.split('/');
    match (parts.next(), parts.next(), parts.next()) {
        (Some(owner), Some(name), None) => {
            let valid = |part: &str| {
                !part.is_empty()
                    && part.len() <= 100
                    && part
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
            };
            valid(owner) && valid(name)
        }
        _ => false,
    }
}

/// Short single-line excerpt of an error body for UI messages (never headers).
fn gh_snippet(body: &str) -> String {
    let flat = body.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut out: String = flat.chars().take(300).collect();
    if flat.chars().count() > 300 {
        out.push('…');
    }
    out
}

fn gh_error_text(status: u16, body: &str) -> String {
    let snippet = gh_snippet(body);
    if snippet.is_empty() {
        format!("HTTP {}", status)
    } else {
        format!("HTTP {}: {}", status, snippet)
    }
}

/// One authenticated GitHub API call. `query` is encoded by reqwest (branch
/// names may contain '/'). Returns (status, body) without interpreting either.
fn gh_request_json(
    client: &reqwest::blocking::Client,
    token: &str,
    method: reqwest::Method,
    url: &str,
    query: &[(&str, &str)],
    body: Option<serde_json::Value>,
) -> Result<(u16, String), String> {
    let mut req = client
        .request(method, url)
        .query(query)
        .header("Accept", "application/vnd.github+json")
        .header("Authorization", format!("Bearer {}", token))
        .header("X-GitHub-Api-Version", "2022-11-28");
    if let Some(value) = body {
        req = req.json(&value);
    }
    let res = req.send().map_err(|e| format!("GitHub unreachable: {}", e))?;
    let status = res.status().as_u16();
    let text = res.text().map_err(|e| e.to_string())?;
    Ok((status, text))
}

fn hook_url(hook: &serde_json::Value) -> Option<&str> {
    hook.get("config")
        .and_then(|c| c.get("url"))
        .and_then(|u| u.as_str())
}

/// First hook (id, url) whose URL satisfies `matches`.
fn find_hook(body: &str, matches: impl Fn(&str) -> bool) -> Option<(u64, String)> {
    let hooks: serde_json::Value = serde_json::from_str(body).ok()?;
    hooks.as_array()?.iter().find_map(|hook| {
        let url = hook_url(hook).filter(|u| matches(u))?;
        Some((hook.get("id").and_then(|id| id.as_u64())?, url.to_string()))
    })
}

/// GET https://api.github.com/user/repos — list repos for the sync picker.
/// Token is passed in memory only, never stored. Returns compact repo objects.
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
        let url = format!(
            "https://api.github.com/user/repos?per_page=100&sort=updated&page={}",
            page
        );
        let (status, body) = gh_request_json(&client, token, reqwest::Method::GET, &url, &[], None)?;
        if status != 200 {
            return Err(format!("GitHub {}", gh_error_text(status, &body)));
        }
        let batch: Vec<serde_json::Value> = serde_json::from_str(&body)
            .map_err(|e| format!("GitHub response was not a JSON array: {}", e))?;
        let count = batch.len();
        for repo in batch {
            let full_name = repo
                .get("full_name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            if full_name.is_empty() {
                continue;
            }
            let admin = repo
                .get("permissions")
                .and_then(|p| p.get("admin"))
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            all.push(serde_json::json!({
                "full_name": full_name,
                "private": repo.get("private").and_then(|v| v.as_bool()).unwrap_or(false),
                "default_branch": repo
                    .get("default_branch")
                    .and_then(|v| v.as_str())
                    .unwrap_or("main"),
                "admin": admin,
                "permissions": { "admin": admin },
            }));
        }
        if count < 100 {
            break;
        }
    }
    serde_json::to_string(&all).map_err(|e| e.to_string())
}

/// Inspect a repo: webhook, workflow file and Actions secret names (no values).
#[tauri::command]
async fn github_repo_status(token: String, repo: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || github_repo_status_blocking(token, repo))
        .await
        .map_err(|e| e.to_string())?
}

fn github_repo_status_blocking(token: String, repo: String) -> Result<String, String> {
    let token = token.trim();
    if token.is_empty() {
        return Err("GitHub token is required".into());
    }
    let repo = repo.trim();
    if !valid_repo_name(repo) {
        return Err("repo must look like owner/name".into());
    }
    let client = gh_client()?;
    let repo_api = format!("https://api.github.com/repos/{}", repo);

    let (status, body) =
        gh_request_json(&client, token, reqwest::Method::GET, &repo_api, &[], None)?;
    match status {
        200 => {}
        401 => return Err("GitHub rejected the token (HTTP 401)".into()),
        404 => return Err(format!("repository {} was not found or is not accessible", repo)),
        _ => return Err(format!("repository lookup {}", gh_error_text(status, &body))),
    }
    let repo_json: serde_json::Value = serde_json::from_str(&body)
        .map_err(|e| format!("repository response was not JSON: {}", e))?;
    let default_branch = repo_json
        .get("default_branch")
        .and_then(|v| v.as_str())
        .filter(|b| !b.is_empty())
        .unwrap_or("main")
        .to_string();

    let mut webhook = serde_json::json!({"configured": false, "id": null, "url": ""});
    let hooks_url = format!("{}/hooks?per_page=100", repo_api);
    match gh_request_json(&client, token, reqwest::Method::GET, &hooks_url, &[], None) {
        Ok((200, hooks_body)) => {
            if let Some((id, url)) = find_hook(&hooks_body, |u| {
                u.trim_end_matches('/').ends_with("/webhooks/github")
            }) {
                webhook = serde_json::json!({"configured": true, "id": id, "url": url});
            }
        }
        Ok((403, _)) => {
            // GitHub App user OAuth tokens and fine-grained PATs without
            // administration:read cannot list hooks; keep webhook status non-fatal.
            webhook = serde_json::json!({
                "configured": false,
                "id": null,
                "url": "",
                "unavailable": true,
            });
        }
        Ok((status, hooks_body)) => {
            webhook = serde_json::json!({
                "configured": false,
                "id": null,
                "url": "",
                "error": gh_error_text(status, &hooks_body),
            });
        }
        Err(e) => {
            webhook = serde_json::json!({"configured": false, "id": null, "url": "", "error": e});
        }
    }

    let mut workflow = serde_json::json!({
        "configured": false,
        "file": null,
        "path": null,
        "provider": null,
    });
    let mut workflow_error: Option<String> = None;
    for (provider, path) in [
        ("pr-agent", ".github/workflows/pr-agent.yml"),
        ("openrabbit", ".github/workflows/openrabbit.yml"),
    ] {
        let url = format!("{}/contents/{}", repo_api, path);
        match gh_request_json(
            &client,
            token,
            reqwest::Method::GET,
            &url,
            &[("ref", default_branch.as_str())],
            None,
        ) {
            Ok((200, file_body)) => {
                let file = path.rsplit('/').next().unwrap_or(path);
                workflow = serde_json::json!({
                    "configured": true,
                    "file": file,
                    "path": path,
                    "provider": provider,
                });
                // For the PR Agent workflow, report the settings this app wrote
                // (and whether the file predates the current template).
                if provider == "pr-agent" {
                    let info = serde_json::from_str::<serde_json::Value>(&file_body)
                        .ok()
                        .and_then(|v| gh_file_text(&v).ok())
                        .map(|text| autoreview::describe_workflow(&text));
                    if let (Some(serde_json::Value::Object(info)), Some(map)) =
                        (info, workflow.as_object_mut())
                    {
                        map.extend(info);
                    }
                }
                break;
            }
            Ok((404, _)) => {}
            Ok((status, body)) => {
                workflow_error = Some(gh_error_text(status, &body));
                break;
            }
            Err(e) => {
                workflow_error = Some(e);
                break;
            }
        }
    }
    if let Some(error) = workflow_error {
        if let Some(map) = workflow.as_object_mut() {
            map.insert("error".to_string(), serde_json::Value::String(error));
        }
    }

    let mut secrets = serde_json::json!({
        "configured": false,
        "openrouter_api_key": false,
        "llm_api_key": false,
        "names": [],
    });
    let secrets_url = format!("{}/actions/secrets?per_page=100", repo_api);
    match gh_request_json(&client, token, reqwest::Method::GET, &secrets_url, &[], None) {
        Ok((200, secrets_body)) => match serde_json::from_str::<serde_json::Value>(&secrets_body) {
            Ok(value) => {
                let names: Vec<String> = value
                    .get("secrets")
                    .and_then(|s| s.as_array())
                    .map(|items| {
                        items
                            .iter()
                            .filter_map(|item| item.get("name").and_then(|n| n.as_str()))
                            .map(str::to_string)
                            .collect()
                    })
                    .unwrap_or_default();
                let has_openrouter = names.iter().any(|n| n == "OPENROUTER_API_KEY");
                let has_llm = names.iter().any(|n| n == "LLM_API_KEY");
                // OpenRabbit needs both keys; the PR Agent workflow only needs
                // OPENROUTER_API_KEY. If no workflow is present, require the
                // key that every setup writes.
                let needs_llm = workflow
                    .get("provider")
                    .and_then(|v| v.as_str())
                    .is_some_and(|p| p == "openrabbit");
                let secrets_ok = has_openrouter && (!needs_llm || has_llm);
                secrets = serde_json::json!({
                    "configured": secrets_ok,
                    "openrouter_api_key": has_openrouter,
                    "llm_api_key": has_llm,
                    "names": names,
                });
            }
            Err(_) => {
                secrets = serde_json::json!({
                    "configured": false,
                    "openrouter_api_key": false,
                    "llm_api_key": false,
                    "names": [],
                    "error": "secrets response was not JSON",
                });
            }
        },
        Ok((status, secrets_body)) => {
            secrets = serde_json::json!({
                "configured": false,
                "openrouter_api_key": false,
                "llm_api_key": false,
                "names": [],
                "error": gh_error_text(status, &secrets_body),
            });
        }
        Err(e) => {
            secrets = serde_json::json!({
                "configured": false,
                "openrouter_api_key": false,
                "llm_api_key": false,
                "names": [],
                "error": e,
            });
        }
    }

    let webhook_ok = webhook
        .get("configured")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let workflow_ok = workflow
        .get("configured")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let secrets_ok = secrets
        .get("configured")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let (configured, mode) = derive_status(webhook_ok, workflow_ok, secrets_ok);

    serde_json::to_string(&serde_json::json!({
        "repo": repo,
        "default_branch": default_branch,
        "webhook": webhook,
        "workflow": workflow,
        "secrets": secrets,
        "configured": configured,
        "mode": mode,
    }))
    .map_err(|e| e.to_string())
}

/// What a repo needs depends on how it is reviewed: the GitHub Action needs
/// workflow + secrets (no backend), the backend path needs only the webhook.
/// Returns (configured, mode) where mode is none | actions | backend | both.
fn derive_status(webhook_ok: bool, workflow_ok: bool, secrets_ok: bool) -> (bool, &'static str) {
    let actions_ok = workflow_ok && secrets_ok;
    let mode = match (webhook_ok, workflow_ok) {
        (true, true) => "both",
        (false, true) => "actions",
        (true, false) => "backend",
        (false, false) => "none",
    };
    let configured = match mode {
        "both" | "actions" => actions_ok,
        "backend" => true,
        _ => false,
    };
    (configured, mode)
}

/// Which setup steps a review mode performs.
#[derive(Clone, Copy, Debug, PartialEq)]
struct SetupPlan {
    webhook: bool,
    actions: bool,
}

/// `actions` (default): GitHub Action only - no public backend needed.
/// `backend`: webhook only - the backend posts the review.
/// `both`: both paths; a PR may receive two reviews.
fn setup_plan(mode: &str) -> Result<SetupPlan, String> {
    match mode.trim() {
        "" | "actions" => Ok(SetupPlan { webhook: false, actions: true }),
        "backend" => Ok(SetupPlan { webhook: true, actions: false }),
        "both" => Ok(SetupPlan { webhook: true, actions: true }),
        _ => Err("review_mode must be 'actions', 'backend' or 'both'".into()),
    }
}

static SODIUM_INIT: std::sync::OnceLock<Result<(), String>> = std::sync::OnceLock::new();

fn ensure_sodium() -> Result<(), String> {
    SODIUM_INIT
        .get_or_init(|| sodiumoxide::init().map_err(|_| "failed to initialize libsodium".to_string()))
        .clone()
}

/// Create or update the pull_request webhook pointing at the backend.
fn gh_setup_webhook(
    client: &reqwest::blocking::Client,
    token: &str,
    repo_api: &str,
    target_url: &str,
    secret: &str,
) -> (&'static str, Option<String>) {
    let hooks_url = format!("{}/hooks?per_page=100", repo_api);
    let update_body = serde_json::json!({
        "config": {"url": target_url, "content_type": "json", "secret": secret},
        "events": ["pull_request"],
        "active": true,
    });
    let mut create_body = update_body.clone();
    if let Some(map) = create_body.as_object_mut() {
        map.insert("name".to_string(), serde_json::Value::String("web".to_string()));
    }

    let existing_url_matches = |u: &str| u.trim_end_matches('/') == target_url;
    let existing = match gh_request_json(client, token, reqwest::Method::GET, &hooks_url, &[], None) {
        Ok((200, body)) => find_hook(&body, existing_url_matches).map(|(id, _)| id),
        _ => None,
    };
    if let Some(id) = existing {
        return match gh_request_json(
            client,
            token,
            reqwest::Method::PATCH,
            &format!("{}/hooks/{}", repo_api, id),
            &[],
            Some(update_body),
        ) {
            Ok((200, _)) => ("updated", None),
            Ok((status, body)) => (
                "failed",
                Some(format!("update webhook: {}", gh_error_text(status, &body))),
            ),
            Err(e) => ("failed", Some(format!("update webhook: {}", e))),
        };
    }

    match gh_request_json(
        client,
        token,
        reqwest::Method::POST,
        &hooks_url,
        &[],
        Some(create_body),
    ) {
        Ok((200, _)) | Ok((201, _)) => ("created", None),
        Ok((422, _)) => {
            // GitHub rejects a duplicate hook URL; patch the existing hook instead.
            let existing = match gh_request_json(
                client,
                token,
                reqwest::Method::GET,
                &hooks_url,
                &[],
                None,
            ) {
                Ok((200, body)) => find_hook(&body, existing_url_matches).map(|(id, _)| id),
                _ => None,
            };
            match existing {
                Some(id) => match gh_request_json(
                    client,
                    token,
                    reqwest::Method::PATCH,
                    &format!("{}/hooks/{}", repo_api, id),
                    &[],
                    Some(update_body),
                ) {
                    Ok((200, _)) => ("updated", None),
                    Ok((status, body)) => (
                        "failed",
                        Some(format!("update webhook: {}", gh_error_text(status, &body))),
                    ),
                    Err(e) => ("failed", Some(format!("update webhook: {}", e))),
                },
                None => (
                    "failed",
                    Some("create webhook: HTTP 422 and no matching hook was found".into()),
                ),
            }
        }
        Ok((status, body)) => (
            "failed",
            Some(format!("create webhook: {}", gh_error_text(status, &body))),
        ),
        Err(e) => ("failed", Some(format!("create webhook: {}", e))),
    }
}

/// No key supplied: succeed only if the repo already has the secrets the workflow needs.
/// Each secret is looked up by name, so repos with many secrets are not a problem.
fn gh_keep_secrets(
    client: &reqwest::blocking::Client,
    token: &str,
    repo_api: &str,
    provider: &str,
) -> (&'static str, Option<String>) {
    let needed: &[&str] = if provider == "openrabbit" {
        &["OPENROUTER_API_KEY", "LLM_API_KEY"]
    } else {
        &["OPENROUTER_API_KEY"]
    };
    let mut missing: Vec<&str> = Vec::new();
    for name in needed {
        let url = format!("{}/actions/secrets/{}", repo_api, name);
        match gh_request_json(client, token, reqwest::Method::GET, &url, &[], None) {
            Ok((200, _)) => {}
            Ok((404, _)) => missing.push(name),
            Ok((status, body)) => {
                return ("failed", Some(format!("actions secret {}: {}", name, gh_error_text(status, &body))))
            }
            Err(e) => return ("failed", Some(format!("actions secret {}: {}", name, e))),
        }
    }
    if missing.is_empty() {
        ("kept", None)
    } else {
        (
            "failed",
            Some(format!(
                "no OpenRouter key was given and the repo has no {} secret",
                missing.join(" / ")
            )),
        )
    }
}

/// Upsert Actions secrets with GitHub's sealed-box public key.
fn gh_setup_secrets(
    client: &reqwest::blocking::Client,
    token: &str,
    repo_api: &str,
    provider: &str,
    openrouter_key: &str,
) -> (&'static str, Option<String>) {
    if let Err(e) = ensure_sodium() {
        return ("failed", Some(e));
    }
    let key_url = format!("{}/actions/secrets/public-key", repo_api);
    let (status, body) =
        match gh_request_json(client, token, reqwest::Method::GET, &key_url, &[], None) {
            Ok(response) => response,
            Err(e) => return ("failed", Some(format!("actions public key: {}", e))),
        };
    if status != 200 {
        return (
            "failed",
            Some(format!("actions public key: {}", gh_error_text(status, &body))),
        );
    }
    let public_key_json: serde_json::Value = match serde_json::from_str(&body) {
        Ok(value) => value,
        Err(_) => return ("failed", Some("actions public key: response was not JSON".into())),
    };
    let key_id = match public_key_json.get("key_id").and_then(|v| v.as_str()) {
        Some(key_id) => key_id.to_string(),
        None => return ("failed", Some("actions public key: missing key_id".into())),
    };
    let key_b64 = match public_key_json.get("key").and_then(|v| v.as_str()) {
        Some(key) => key,
        None => return ("failed", Some("actions public key: missing key".into())),
    };
    let raw_key = match STANDARD.decode(key_b64) {
        Ok(bytes) => bytes,
        Err(_) => return ("failed", Some("actions public key: key was not valid base64".into())),
    };
    let public_key = match sodiumoxide::crypto::box_::PublicKey::from_slice(&raw_key) {
        Some(key) => key,
        None => return ("failed", Some("actions public key: expected 32 bytes".into())),
    };

    let secrets: &[(&str, &str)] = match provider {
        "openrabbit" => &[
            ("OPENROUTER_API_KEY", openrouter_key),
            ("LLM_API_KEY", openrouter_key),
        ],
        _ => &[("OPENROUTER_API_KEY", openrouter_key)],
    };
    let mut failures: Vec<String> = Vec::new();
    for &(name, value) in secrets {
        let sealed = sodiumoxide::crypto::sealedbox::seal(value.as_bytes(), &public_key);
        let secret_url = format!("{}/actions/secrets/{}", repo_api, name);
        let payload = serde_json::json!({
            "encrypted_value": STANDARD.encode(sealed),
            "key_id": key_id.as_str(),
        });
        match gh_request_json(client, token, reqwest::Method::PUT, &secret_url, &[], Some(payload)) {
            Ok((status, _)) if (200..300).contains(&status) => {}
            Ok((status, body)) => failures.push(format!("{}: {}", name, gh_error_text(status, &body))),
            Err(e) => failures.push(format!("{}: {}", name, e)),
        }
    }
    if failures.is_empty() {
        ("updated", None)
    } else {
        ("failed", Some(format!("actions secrets: {}", failures.join("; "))))
    }
}

/// Decode the base64 `content` of a GitHub contents API file response.
fn gh_file_text(file_json: &serde_json::Value) -> Result<String, String> {
    let encoded = file_json.get("content").and_then(|v| v.as_str()).unwrap_or("");
    let cleaned: String = encoded.chars().filter(|c| !matches!(c, '\n' | '\r')).collect();
    let bytes = STANDARD
        .decode(cleaned)
        .map_err(|_| "file content was not valid base64".to_string())?;
    String::from_utf8(bytes).map_err(|_| "file content was not UTF-8".to_string())
}

/// Create, update or leave alone the workflow file on the default branch.
fn gh_commit_workflow(
    client: &reqwest::blocking::Client,
    token: &str,
    repo_api: &str,
    branch: &str,
    provider: &str,
    pr_agent_template: &str,
) -> (&'static str, Option<String>) {
    let (path, template, message) = match provider {
        "openrabbit" => (
            ".github/workflows/openrabbit.yml",
            OPENRABBIT_WORKFLOW,
            "chore: add OpenRabbit reviewer workflow (PR Agent Control)",
        ),
        _ => (
            ".github/workflows/pr-agent.yml",
            pr_agent_template,
            "chore: configure PR Agent auto review (PR Agent Control)",
        ),
    };
    let url = format!("{}/contents/{}", repo_api, path);
    let (status, body) =
        match gh_request_json(client, token, reqwest::Method::GET, &url, &[("ref", branch)], None) {
            Ok(response) => response,
            Err(e) => return ("failed", Some(format!("workflow: {}", e))),
        };
    let mut sha: Option<String> = None;
    match status {
        200 => {
            let file_json: serde_json::Value = match serde_json::from_str(&body) {
                Ok(value) => value,
                Err(_) => {
                    return (
                        "failed",
                        Some("workflow: existing file response was not JSON".into()),
                    )
                }
            };
            sha = file_json
                .get("sha")
                .and_then(|v| v.as_str())
                .map(str::to_string);
            // Unreadable content is simply replaced; only identical files are skipped.
            if gh_file_text(&file_json).is_ok_and(|current| current == template) {
                return ("unchanged", None);
            }
        }
        404 => {}
        _ => {
            return (
                "failed",
                Some(format!("workflow: {}", gh_error_text(status, &body))),
            )
        }
    }

    let make_body = |sha: Option<&str>| {
        let mut value = serde_json::json!({
            "message": message,
            "content": STANDARD.encode(template.as_bytes()),
            "branch": branch,
        });
        if let Some(sha) = sha {
            if let Some(map) = value.as_object_mut() {
                map.insert("sha".to_string(), serde_json::Value::String(sha.to_string()));
            }
        }
        value
    };

    match gh_request_json(
        client,
        token,
        reqwest::Method::PUT,
        &url,
        &[],
        Some(make_body(sha.as_deref())),
    ) {
        Ok((201, _)) => ("created", None),
        Ok((200, _)) => ("updated", None),
        Ok((409, _)) => {
            // Race: re-read the fresh sha and retry the same PUT once.
            match gh_request_json(client, token, reqwest::Method::GET, &url, &[("ref", branch)], None)
            {
                Ok((200, fresh_body)) => {
                    let fresh_sha = serde_json::from_str::<serde_json::Value>(&fresh_body)
                        .ok()
                        .and_then(|v| v.get("sha").and_then(|s| s.as_str()).map(str::to_string));
                    match fresh_sha {
                        Some(fresh_sha) => match gh_request_json(
                            client,
                            token,
                            reqwest::Method::PUT,
                            &url,
                            &[],
                            Some(make_body(Some(fresh_sha.as_str()))),
                        ) {
                            Ok((200, _)) | Ok((201, _)) => ("updated", None),
                            Ok((status, body)) => (
                                "failed",
                                Some(format!("workflow: {}", gh_error_text(status, &body))),
                            ),
                            Err(e) => ("failed", Some(format!("workflow: {}", e))),
                        },
                        None => (
                            "failed",
                            Some("workflow: conflict and no file sha was returned".into()),
                        ),
                    }
                }
                Ok((status, body)) => (
                    "failed",
                    Some(format!(
                        "workflow: conflict; re-read failed: {}",
                        gh_error_text(status, &body)
                    )),
                ),
                Err(e) => (
                    "failed",
                    Some(format!("workflow: conflict; re-read failed: {}", e)),
                ),
            }
        }
        Ok((status, body)) => (
            "failed",
            Some(format!("workflow: {}", gh_error_text(status, &body))),
        ),
        Err(e) => ("failed", Some(format!("workflow: {}", e))),
    }
}

/// One-click repo setup: webhook + Actions secrets + workflow file.
/// Each step runs independently and reports its own status. The token,
/// webhook secret and API key are never returned or logged.
#[tauri::command]
async fn github_setup_repo(
    token: String,
    repo: String,
    backend_url: String,
    webhook_secret: String,
    openrouter_key: String,
    workflow_provider: String,
    review_mode: Option<String>,
    workflow_options: Option<WorkflowOptions>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        github_setup_repo_blocking(
            token,
            repo,
            backend_url,
            webhook_secret,
            openrouter_key,
            workflow_provider,
            review_mode.unwrap_or_default(),
            workflow_options.unwrap_or_default(),
        )
    })
    .await
    .map_err(|e| e.to_string())?
}

fn github_setup_repo_blocking(
    token: String,
    repo: String,
    backend_url: String,
    webhook_secret: String,
    openrouter_key: String,
    workflow_provider: String,
    review_mode: String,
    workflow_options: WorkflowOptions,
) -> Result<String, String> {
    let plan = setup_plan(&review_mode)?;
    let token = token.trim();
    if token.is_empty() {
        return Err("GitHub token is required".into());
    }
    let repo = repo.trim();
    if !valid_repo_name(repo) {
        return Err("repo must look like owner/name".into());
    }
    let backend = backend_url.trim().trim_end_matches('/').to_string();
    // backend/.env stores trimmed values, so trim here too or webhook
    // signatures and API keys would not match what the backend loads.
    let webhook_secret = webhook_secret.trim();
    let openrouter_key = openrouter_key.trim();
    if plan.webhook {
        if !(backend.starts_with("http://") || backend.starts_with("https://")) {
            return Err("backend_url must start with http(s)://".into());
        }
        if webhook_secret.is_empty() {
            return Err("webhook_secret is required".into());
        }
        if webhook_secret.contains('\r') || webhook_secret.contains('\n') {
            return Err("webhook_secret must be a single line".into());
        }
        if webhook_secret.len() > 512 {
            return Err("webhook_secret is too long (max 512 bytes)".into());
        }
    }
    // An empty key is allowed: the secrets step then keeps the repo's existing
    // secret (and fails for that repo if there is none), so settings can be
    // updated without pasting the key again.
    if plan.actions {
        if openrouter_key.contains('\r') || openrouter_key.contains('\n') {
            return Err("openrouter_key must be a single line".into());
        }
    }
    let provider = workflow_provider.trim();
    let workflow_path = match provider {
        "pr-agent" => ".github/workflows/pr-agent.yml",
        "openrabbit" => ".github/workflows/openrabbit.yml",
        _ => return Err("workflow_provider must be 'pr-agent' or 'openrabbit'".into()),
    };
    // Validate before touching the repo so a bad setting cannot half-configure it.
    // Backend-only and OpenRabbit setups never use these options, so they are not checked.
    let pr_agent_template = if plan.actions && provider == "pr-agent" {
        autoreview::render_pr_agent_workflow(&workflow_options.normalized()?)
    } else {
        String::new()
    };

    let client = gh_client()?;
    let repo_api = format!("https://api.github.com/repos/{}", repo);
    let webhook_url = format!("{}/webhooks/github", backend);

    let (secrets_status, secrets_error) = if plan.actions && openrouter_key.is_empty() {
        gh_keep_secrets(&client, token, &repo_api, provider)
    } else if plan.actions {
        gh_setup_secrets(&client, token, &repo_api, provider, openrouter_key)
    } else {
        ("skipped", None)
    };
    // No key was given and the repo's own secret is gone: stop before changing anything,
    // as setup always did for a blank key.
    let abort = plan.actions && openrouter_key.is_empty() && secrets_status == "failed";
    let (webhook_status, webhook_error) = if plan.webhook && !abort {
        gh_setup_webhook(&client, token, &repo_api, &webhook_url, webhook_secret)
    } else {
        ("skipped", None)
    };

    let (default_branch, workflow_status, workflow_error): (String, &'static str, Option<String>) =
        if !plan.actions || abort {
            (String::new(), "skipped", None)
        } else { match gh_request_json(&client, token, reqwest::Method::GET, &repo_api, &[], None) {
            Ok((200, body)) => {
                let repo_json: serde_json::Value =
                    serde_json::from_str(&body).unwrap_or(serde_json::Value::Null);
                let default_branch = repo_json
                    .get("default_branch")
                    .and_then(|v| v.as_str())
                    .filter(|b| !b.is_empty())
                    .unwrap_or("main")
                    .to_string();
                let (status, error) = gh_commit_workflow(
                    &client,
                    token,
                    &repo_api,
                    &default_branch,
                    provider,
                    &pr_agent_template,
                );
                (default_branch, status, error)
            }
            Ok((status, body)) => (
                String::new(),
                "failed",
                Some(format!("repo lookup: {}", gh_error_text(status, &body))),
            ),
            Err(e) => (String::new(), "failed", Some(format!("repo lookup: {}", e))),
        } };

    let mut errors = serde_json::Map::new();
    if let Some(error) = webhook_error {
        errors.insert("webhook".to_string(), serde_json::Value::String(error));
    }
    if let Some(error) = secrets_error {
        errors.insert("secrets".to_string(), serde_json::Value::String(error));
    }
    if let Some(error) = workflow_error {
        errors.insert("workflow".to_string(), serde_json::Value::String(error));
    }

    let mut result = serde_json::json!({
        "repo": repo,
        "ok": webhook_status != "failed" && secrets_status != "failed" && workflow_status != "failed",
        "webhook": webhook_status,
        "secrets": secrets_status,
        "workflow": workflow_status,
        "default_branch": default_branch,
        "workflow_path": if plan.actions { serde_json::Value::String(workflow_path.to_string()) } else { serde_json::Value::Null },
        "workflow_provider": provider,
        "webhook_url": if plan.webhook { serde_json::Value::String(webhook_url) } else { serde_json::Value::Null },
        "review_mode": review_mode.trim(),
    });
    if !errors.is_empty() {
        if let Some(map) = result.as_object_mut() {
            map.insert("errors".to_string(), serde_json::Value::Object(errors));
        }
    }
    serde_json::to_string(&result).map_err(|e| e.to_string())
}

fn gh_default_branch(
    client: &reqwest::blocking::Client,
    token: &str,
    repo_api: &str,
) -> Result<String, String> {
    let (status, body) = gh_request_json(client, token, reqwest::Method::GET, repo_api, &[], None)?;
    if status != 200 {
        return Err(format!("repo lookup: {}", gh_error_text(status, &body)));
    }
    let repo_json: serde_json::Value =
        serde_json::from_str(&body).map_err(|_| "repo lookup: response was not JSON".to_string())?;
    Ok(repo_json
        .get("default_branch")
        .and_then(|v| v.as_str())
        .filter(|b| !b.is_empty())
        .unwrap_or("main")
        .to_string())
}

/// Delete one file from `branch` if it exists. Returns "removed" or "absent".
fn gh_delete_file(
    client: &reqwest::blocking::Client,
    token: &str,
    repo_api: &str,
    branch: &str,
    path: &str,
    message: &str,
) -> Result<&'static str, String> {
    let url = format!("{}/contents/{}", repo_api, path);
    let (status, body) =
        gh_request_json(client, token, reqwest::Method::GET, &url, &[("ref", branch)], None)?;
    match status {
        404 => return Ok("absent"),
        200 => {}
        _ => return Err(format!("{}: {}", path, gh_error_text(status, &body))),
    }
    let sha = serde_json::from_str::<serde_json::Value>(&body)
        .ok()
        .and_then(|v| v.get("sha").and_then(|s| s.as_str()).map(str::to_string))
        .ok_or_else(|| format!("{}: no file sha returned", path))?;
    let payload = serde_json::json!({ "message": message, "sha": sha, "branch": branch });
    let (status, body) =
        gh_request_json(client, token, reqwest::Method::DELETE, &url, &[], Some(payload))?;
    if status == 200 {
        Ok("removed")
    } else {
        Err(format!("delete {}: {}", path, gh_error_text(status, &body)))
    }
}

/// Turn auto review off for a repo: delete the review workflow files from the
/// default branch and, when asked, the backend webhook(s) and the Actions secrets.
/// Each step reports its own status; a failure in one does not stop the others.
#[tauri::command]
async fn github_disable_repo(
    token: String,
    repo: String,
    remove_webhook: Option<bool>,
    remove_secrets: Option<bool>,
    webhook_url: Option<String>,
    keep_pr_agent: Option<bool>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        github_disable_repo_blocking(
            token,
            repo,
            remove_webhook.unwrap_or(true),
            remove_secrets.unwrap_or(false),
            webhook_url,
            keep_pr_agent.unwrap_or(false),
        )
    })
    .await
    .map_err(|e| e.to_string())?
}

fn github_disable_repo_blocking(
    token: String,
    repo: String,
    remove_webhook: bool,
    remove_secrets: bool,
    webhook_url: Option<String>,
    keep_pr_agent: bool,
) -> Result<String, String> {
    let token = token.trim();
    if token.is_empty() {
        return Err("GitHub token is required".into());
    }
    let repo = repo.trim();
    if !valid_repo_name(repo) {
        return Err("repo must look like owner/name".into());
    }
    let client = gh_client()?;
    let repo_api = format!("https://api.github.com/repos/{}", repo);
    let mut errors = serde_json::Map::new();

    let workflow_status = match gh_default_branch(&client, token, &repo_api) {
        Ok(branch) => {
            let mut removed = 0;
            let mut failed = Vec::new();
            // Switching reviewers removes only the OpenRabbit file and keeps PR Agent's.
            let (paths, message): (&[&str], &str) = if keep_pr_agent {
                (
                    &[".github/workflows/openrabbit.yml"],
                    "chore: remove OpenRabbit workflow, replaced by PR Agent (PR Agent Control)",
                )
            } else {
                (
                    &[".github/workflows/pr-agent.yml", ".github/workflows/openrabbit.yml"],
                    "chore: disable PR Agent auto review (PR Agent Control)",
                )
            };
            for path in paths.iter().copied() {
                match gh_delete_file(&client, token, &repo_api, &branch, path, message) {
                    Ok("removed") => removed += 1,
                    Ok(_) => {}
                    Err(e) => failed.push(e),
                }
            }
            if !failed.is_empty() {
                errors.insert("workflow".into(), failed.join("; ").into());
                "failed"
            } else if removed > 0 {
                "removed"
            } else {
                "absent"
            }
        }
        Err(e) => {
            errors.insert("workflow".into(), e.into());
            "failed"
        }
    };

    // Only the hook registered for this backend is removed: other services may
    // use the same path on a different host. Without a URL nothing is touched.
    let hook_target = webhook_url
        .as_deref()
        .map(|u| u.trim().trim_end_matches('/').to_string())
        .filter(|u| !u.is_empty());
    let webhook_status = if !remove_webhook || hook_target.is_none() {
        "skipped"
    } else {
        let target = hook_target.unwrap_or_default();
        let hooks_url = format!("{}/hooks?per_page=100", repo_api);
        match gh_request_json(&client, token, reqwest::Method::GET, &hooks_url, &[], None) {
            Ok((200, body)) => {
                let ids: Vec<u64> = serde_json::from_str::<serde_json::Value>(&body)
                    .ok()
                    .and_then(|v| v.as_array().cloned())
                    .unwrap_or_default()
                    .iter()
                    .filter(|h| {
                        hook_url(h).is_some_and(|u| u.trim_end_matches('/') == target)
                    })
                    .filter_map(|h| h.get("id").and_then(|id| id.as_u64()))
                    .collect();
                let mut failed = Vec::new();
                for id in &ids {
                    let url = format!("{}/hooks/{}", repo_api, id);
                    match gh_request_json(&client, token, reqwest::Method::DELETE, &url, &[], None) {
                        Ok((204, _)) | Ok((404, _)) => {}
                        Ok((status, body)) => failed.push(format!("hook {}: {}", id, gh_error_text(status, &body))),
                        Err(e) => failed.push(format!("hook {}: {}", id, e)),
                    }
                }
                if !failed.is_empty() {
                    errors.insert("webhook".into(), failed.join("; ").into());
                    "failed"
                } else if ids.is_empty() {
                    "absent"
                } else {
                    "removed"
                }
            }
            Ok((status, body)) => {
                errors.insert("webhook".into(), gh_error_text(status, &body).into());
                "failed"
            }
            Err(e) => {
                errors.insert("webhook".into(), e.into());
                "failed"
            }
        }
    };

    let secrets_status = if !remove_secrets {
        "skipped"
    } else {
        let mut failed = Vec::new();
        for name in ["OPENROUTER_API_KEY", "LLM_API_KEY"] {
            let url = format!("{}/actions/secrets/{}", repo_api, name);
            match gh_request_json(&client, token, reqwest::Method::DELETE, &url, &[], None) {
                Ok((204, _)) | Ok((404, _)) => {}
                Ok((status, body)) => failed.push(format!("{}: {}", name, gh_error_text(status, &body))),
                Err(e) => failed.push(format!("{}: {}", name, e)),
            }
        }
        if failed.is_empty() {
            "removed"
        } else {
            errors.insert("secrets".into(), failed.join("; ").into());
            "failed"
        }
    };

    let mut result = serde_json::json!({
        "repo": repo,
        "ok": errors.is_empty(),
        "workflow": workflow_status,
        "webhook": webhook_status,
        "secrets": secrets_status,
    });
    if !errors.is_empty() {
        if let Some(map) = result.as_object_mut() {
            map.insert("errors".into(), serde_json::Value::Object(errors));
        }
    }
    serde_json::to_string(&result).map_err(|e| e.to_string())
}

/// Open pull requests (most recently updated first) for the "test on a PR" picker.
#[tauri::command]
async fn github_list_pulls(token: String, repo: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || github_list_pulls_blocking(token, repo))
        .await
        .map_err(|e| e.to_string())?
}

fn github_list_pulls_blocking(token: String, repo: String) -> Result<String, String> {
    let token = token.trim();
    if token.is_empty() {
        return Err("GitHub token is required".into());
    }
    let repo = repo.trim();
    if !valid_repo_name(repo) {
        return Err("repo must look like owner/name".into());
    }
    let client = gh_client()?;
    let url = format!("https://api.github.com/repos/{}/pulls", repo);
    let (status, body) = gh_request_json(
        &client,
        token,
        reqwest::Method::GET,
        &url,
        &[("state", "open"), ("sort", "updated"), ("direction", "desc"), ("per_page", "20")],
        None,
    )?;
    if status != 200 {
        return Err(format!("list pull requests: {}", gh_error_text(status, &body)));
    }
    let items: Vec<serde_json::Value> = serde_json::from_str(&body)
        .map_err(|_| "pull request list was not a JSON array".to_string())?;
    let pulls: Vec<serde_json::Value> = items
        .iter()
        .filter_map(|p| {
            Some(serde_json::json!({
                "number": p.get("number")?.as_u64()?,
                "title": p.get("title").and_then(|v| v.as_str()).unwrap_or(""),
                "draft": p.get("draft").and_then(|v| v.as_bool()).unwrap_or(false),
                "user": p.get("user").and_then(|u| u.get("login")).and_then(|v| v.as_str()).unwrap_or(""),
                "html_url": p.get("html_url").and_then(|v| v.as_str()).unwrap_or(""),
                "updated_at": p.get("updated_at").and_then(|v| v.as_str()).unwrap_or(""),
            }))
        })
        .collect();
    serde_json::to_string(&pulls).map_err(|e| e.to_string())
}

/// Post a PR-Agent command (/review, /describe, /improve) as a comment on a PR.
/// The repo's workflow picks it up through the issue_comment trigger.
#[tauri::command]
async fn github_pr_command(
    token: String,
    repo: String,
    number: u64,
    command: String,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || github_pr_command_blocking(token, repo, number, command))
        .await
        .map_err(|e| e.to_string())?
}

fn github_pr_command_blocking(
    token: String,
    repo: String,
    number: u64,
    command: String,
) -> Result<String, String> {
    let token = token.trim();
    if token.is_empty() {
        return Err("GitHub token is required".into());
    }
    let repo = repo.trim();
    if !valid_repo_name(repo) {
        return Err("repo must look like owner/name".into());
    }
    if number == 0 {
        return Err("pull request number is required".into());
    }
    let command = command.trim();
    if !["/review", "/describe", "/improve"].contains(&command) {
        return Err("command must be /review, /describe or /improve".into());
    }
    let client = gh_client()?;
    let url = format!("https://api.github.com/repos/{}/issues/{}/comments", repo, number);
    let (status, body) = gh_request_json(
        &client,
        token,
        reqwest::Method::POST,
        &url,
        &[],
        Some(serde_json::json!({ "body": command })),
    )?;
    if status != 201 {
        return Err(format!("post comment: {}", gh_error_text(status, &body)));
    }
    Ok(serde_json::from_str::<serde_json::Value>(&body)
        .ok()
        .and_then(|v| v.get("html_url").and_then(|u| u.as_str()).map(str::to_string))
        .unwrap_or_default())
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
    require_secure_for_token(&url, api_token.as_deref().map(str::trim).is_some_and(|t| !t.is_empty()))?;
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
        .manage(daemon::DaemonState::default())
        .manage(oauth::OAuthState::default())
        .setup(|app| {
            let config_dir = app.path().app_config_dir()?;
            let _ = APP_CONFIG_DIR.set(config_dir.clone());
            // Installed apps use a writable per-user copy of the embedded default.
            let path = if cfg!(debug_assertions) {
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.pr_agent.toml")
            } else {
                config_dir.join(".pr_agent.toml")
            };
            local_runtime::prepare_config(&path).map_err(std::io::Error::other)?;
            CONFIG_PATH
                .set(path)
                .map_err(|_| std::io::Error::other("configuration already initialized"))?;
            // Non-fatal: without a tray the window simply closes normally.
            if let Err(e) = daemon::setup_tray(app) {
                eprintln!("tray unavailable, daemon will stop when the window closes: {}", e);
            }
            Ok(())
        })
        .on_window_event(|window, event| daemon::on_window_event(window, event))
        .invoke_handler(tauri::generate_handler![
            daemon::daemon_start,
            daemon::daemon_stop,
            daemon::daemon_status,
            get_config,
            save_config,
            get_workflow,
            build_cli_command,
            run_local_review,
            box_provision,
            check_tool,
            install_pr_agent,
            get_workflow_template,
            preview_pr_agent_workflow,
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
            github_list_repos,
            github_repo_status,
            github_setup_repo,
            github_disable_repo,
            github_list_pulls,
            github_pr_command,
            github_app::github_app_create,
            oauth::oauth_cancel,
            oauth::oauth_status,
            oauth::oauth_save_client,
            oauth::oauth_clear_client,
            oauth::openrouter_oauth_connect,
            oauth::github_oauth_connect,
            oauth::github_import_cli,
            oauth::vercel_fetch_teams,
            oauth::vercel_import_cli,
            oauth::vercel_oauth_connect,
            oauth::commandcode_import_cli,
            oauth::opencode_import_cli
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn main() {
    run();
}

#[cfg(all(test, unix))]
mod runtime_tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn rebuilds_unusable_managed_interpreter_and_keeps_healthy_venv() {
        let dir = std::env::temp_dir().join(format!("pr-agent-rebuild-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("bin")).unwrap();
        let python = venv_python(&dir);
        std::fs::write(&python, "#!/bin/sh\nexit 1\n").unwrap();
        std::fs::set_permissions(&python, std::fs::Permissions::from_mode(0o755)).unwrap();
        let previous_venv = std::env::var_os("PR_AGENT_VENV");
        let previous_base = std::env::var_os("PR_AGENT_BASE_PYTHON");
        std::env::set_var("PR_AGENT_VENV", &dir);
        std::env::set_var("PR_AGENT_BASE_PYTHON", "python3");
        let repaired = ensure_pr_agent_venv();
        let marker = dir.join("keep.txt");
        std::fs::write(&marker, "healthy state").unwrap();
        let healthy = ensure_pr_agent_venv();
        for (key, old) in [("PR_AGENT_VENV", previous_venv), ("PR_AGENT_BASE_PYTHON", previous_base)] {
            match old {
                Some(value) => std::env::set_var(key, value),
                None => std::env::remove_var(key),
            }
        }
        assert_eq!(repaired.unwrap(), python);
        assert_eq!(healthy.unwrap(), python);
        assert_eq!(std::fs::read_to_string(marker).unwrap(), "healthy state");
        std::fs::remove_dir_all(dir).unwrap();
    }
}

#[cfg(test)]
mod setup_mode_tests {
    use super::*;

    #[test]
    fn plan_maps_modes_to_steps() {
        assert_eq!(setup_plan("").unwrap(), SetupPlan { webhook: false, actions: true });
        assert_eq!(setup_plan("actions").unwrap(), SetupPlan { webhook: false, actions: true });
        assert_eq!(setup_plan("backend").unwrap(), SetupPlan { webhook: true, actions: false });
        assert_eq!(setup_plan(" both ").unwrap(), SetupPlan { webhook: true, actions: true });
        assert!(setup_plan("everything").is_err());
    }

    #[test]
    fn status_depends_on_the_chosen_mode() {
        // Action-only repos are complete without a webhook.
        assert_eq!(derive_status(false, true, true), (true, "actions"));
        // A workflow without its secret would silently skip reviews.
        assert_eq!(derive_status(false, true, false), (false, "actions"));
        // Backend-only repos need no workflow or Actions secrets.
        assert_eq!(derive_status(true, false, false), (true, "backend"));
        assert_eq!(derive_status(true, true, true), (true, "both"));
        assert_eq!(derive_status(false, false, false), (false, "none"));
    }

    #[test]
    fn workflow_template_uses_current_token_setting() {
        let yaml = autoreview::default_pr_agent_workflow();
        assert!(yaml.contains("CONFIG__MAX_MODEL_TOKENS"));
        assert!(yaml.contains("CONFIG__CUSTOM_MODEL_MAX_TOKENS"));
    }

    #[test]
    fn decodes_github_content_with_line_breaks() {
        let encoded = format!("{}\n{}", &STANDARD.encode("hello world")[..8], &STANDARD.encode("hello world")[8..]);
        let file = serde_json::json!({ "content": encoded, "sha": "abc" });
        assert_eq!(gh_file_text(&file).unwrap(), "hello world");
    }
}

#[cfg(test)]
mod llm_provider_tests {
    use super::*;

    #[test]
    fn env_allowlist_accepts_new_provider_keys() {
        for key in ["COMMANDCODE_API_KEY", "CMD_API_KEY", "OPENCODE_GO_API_KEY", "OPENCODE_API_KEY", "LLM_PROVIDER"] {
            assert!(allowed_env_key(key), "{} should be allowed", key);
        }
        assert!(!allowed_env_key("OPENAI__KEY"));
    }

    #[test]
    fn api_base_per_provider() {
        assert_eq!(llm_api_base("openrouter").unwrap(), "https://openrouter.ai/api/v1");
        assert_eq!(llm_api_base("commandcode").unwrap(), "https://api.commandcode.ai/provider/v1");
        assert_eq!(llm_api_base("opencode_go").unwrap(), "https://opencode.ai/inference/go/openai/v1");
        assert!(llm_api_base("").is_err());
        assert!(llm_api_base("anthropic").is_err());
    }

    #[test]
    fn provider_env_per_provider() {
        let or = llm_provider_env("openrouter", "k").unwrap();
        assert_eq!(or, vec![("OPENROUTER__KEY", "k".to_string())]);

        let cc = llm_provider_env("commandcode", "k").unwrap();
        assert_eq!(
            cc,
            vec![
                ("OPENAI__KEY", "k".to_string()),
                ("OPENAI__API_BASE", "https://api.commandcode.ai/provider/v1".to_string()),
            ]
        );

        let oc = llm_provider_env("opencode_go", "k").unwrap();
        assert_eq!(oc[1].1, "https://opencode.ai/inference/go/openai/v1");

        assert!(llm_provider_env("", "k").is_err());
        assert!(llm_provider_env("bogus", "k").is_err());
    }

    #[test]
    fn detects_provider_from_openai_section_only() {
        let commandcode = "[config]\nmodel = \"openai/deepseek/deepseek-v4-flash\"\n\n[openai]\napi_base = \"https://api.commandcode.ai/provider/v1\"\n";
        assert_eq!(detect_llm_provider(commandcode), "commandcode");

        let opencode = "[openai]\napi_base = \"https://opencode.ai/inference/go/openai/v1\"\n";
        assert_eq!(detect_llm_provider(opencode), "opencode_go");

        // An api_base in another section must not be mistaken for the provider.
        let other_section = "[openrouter]\napi_base = \"https://api.commandcode.ai/provider/v1\"\n";
        assert_eq!(detect_llm_provider(other_section), "openrouter");

        let commented = "[openai] # provider endpoint\napi_base = 'https://api.commandcode.ai/provider/v1' # endpoint\n";
        assert_eq!(detect_llm_provider(commented), "commandcode");

        let legacy = "[config]\nmodel = \"openrouter/free\"\n[openrouter]\n";
        assert_eq!(detect_llm_provider(legacy), "openrouter");
    }

    #[test]
    fn render_openrouter_keeps_openrouter_section() {
        let toml = render_config_toml(&serde_json::json!({
            "model": "openrouter/free",
            "llm_provider": "openrouter",
        }))
        .unwrap();
        assert!(toml.contains("[openrouter]\n"));
        assert!(!toml.contains("[openai]"));
        assert_eq!(detect_llm_provider(&toml), "openrouter");
    }

    #[test]
    fn render_openai_compatible_provider_writes_openai_section() {
        let toml = render_config_toml(&serde_json::json!({
            "model": "openai/Qwen/Qwen3.7-Max",
            "llm_provider": "commandcode",
        }))
        .unwrap();
        assert!(toml.contains("[openai]\n"));
        assert!(toml.contains("api_base = \"https://api.commandcode.ai/provider/v1\""));
        assert!(!toml.contains("[openrouter]"));
        assert!(toml.contains("model = \"openai/Qwen/Qwen3.7-Max\""));
        assert_eq!(detect_llm_provider(&toml), "commandcode");
    }

    #[test]
    fn render_defaults_to_openrouter_and_rejects_unknown_provider() {
        let toml = render_config_toml(&serde_json::json!({ "model": "openrouter/free" })).unwrap();
        assert!(toml.contains("[openrouter]"));
        assert!(render_config_toml(&serde_json::json!({ "llm_provider": "nope" })).is_err());
    }

    #[test]
    fn provider_defaults_and_local_run_consistency() {
        for provider in ["openrouter", "commandcode", "opencode_go"] {
            let raw = render_config_toml(&serde_json::json!({"llm_provider": provider})).unwrap();
            let doc = raw.parse::<toml_edit::Document>().unwrap();
            assert_eq!(doc["config"]["model"].as_str(), Some(default_llm_model(provider)));
            assert!(validate_run_provider(&raw, provider).is_ok());
            for other in ["openrouter", "commandcode", "opencode_go"] {
                if other != provider {
                    assert!(validate_run_provider(&raw, other).is_err());
                }
            }
        }
        let stale = render_config_toml(&serde_json::json!({
            "llm_provider": "commandcode", "model": "openrouter/free"
        })).unwrap();
        assert!(validate_run_provider(&stale, "commandcode").is_err());
        let missing = "[openai] # provider\napi_base = 'https://opencode.ai/inference/go/openai/v1'\n";
        assert!(validate_run_provider(missing, "opencode_go").is_err());
        let explicit = render_config_toml(&serde_json::json!({
            "llm_provider": "opencode_go", "model": "openai/custom"
        })).unwrap();
        assert_eq!(get_val(&explicit, "model").as_deref(), Some("openai/custom"));
        assert!(validate_run_provider(&explicit, "opencode_go").is_ok());
    }

    #[test]
    fn opencode_session_id_is_stable_per_pr() {
        let a = opencode_session_id("https://github.com/owner/repo/pull/7");
        let b = opencode_session_id("https://github.com/owner/repo/pull/7");
        let c = opencode_session_id("https://github.com/owner/repo/pull/8");
        assert_eq!(a, b);
        assert_ne!(a, c);
        assert!(a.starts_with("ses_"));
        assert_eq!(a.len(), 4 + 24);
    }

    #[test]
    fn backend_test_rejects_unknown_provider_before_network() {
        let err = backend_config_test_blocking(
            "https://example.invalid".into(),
            "nope".into(),
            String::new(),
        )
        .unwrap_err();
        assert!(err.contains("commandcode, opencode_go"));
    }
}
