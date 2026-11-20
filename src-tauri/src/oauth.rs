//! 1-click browser OAuth, PKCE loopback listeners, and local CLI credential imports.
//!
//! Secrets (OAuth client secrets, GitHub App keys) stay strictly in the Rust process,
//! `oauth_clients.json` (in Tauri's app config dir), `backend/.env`, and the deployed
//! cloud backend (`PUT /config/settings`). They are never written to `localStorage`.

use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use serde::{Deserialize, Serialize};

use crate::github_app;

const OAUTH_TIMEOUT: Duration = Duration::from_secs(5 * 60);
const MANIFEST_OAUTH_TIMEOUT: Duration = Duration::from_secs(10 * 60);
const VERCEL_OAUTH_PORT: u16 = 17495;

static OAUTH_CLIENTS_LOCK: Mutex<()> = Mutex::new(());

#[derive(Default)]
pub struct OAuthState {
    cancel: Mutex<Option<Arc<AtomicBool>>>,
}

impl OAuthState {
    fn begin_flow(&self) -> Arc<AtomicBool> {
        let mut guard = self.cancel.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(prev) = guard.take() {
            prev.store(true, Ordering::Relaxed);
        }
        let flag = Arc::new(AtomicBool::new(false));
        *guard = Some(flag.clone());
        flag
    }

    pub fn cancel_active(&self) {
        let guard = self.cancel.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(flag) = guard.as_ref() {
            flag.store(true, Ordering::Relaxed);
        }
    }
}

#[derive(Default, Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct StoredOAuthClients {
    pub github_client_id: Option<String>,
    pub github_client_secret: Option<String>,
    pub github_is_app: bool,
    pub github_app_slug: Option<String>,
    pub github_html_url: Option<String>,
    pub github_install_url: Option<String>,
    pub vercel_client_id: Option<String>,
    pub vercel_client_secret: Option<String>,
}

fn oauth_clients_path() -> Result<PathBuf, String> {
    let dir = crate::APP_CONFIG_DIR
        .get()
        .ok_or("app configuration directory is not initialized")?;
    Ok(dir.join("oauth_clients.json"))
}

fn load_clients_from_path(path: &Path) -> StoredOAuthClients {
    let Ok(raw) = std::fs::read_to_string(path) else {
        return StoredOAuthClients::default();
    };
    serde_json::from_str(&raw).unwrap_or_default()
}

fn save_clients_to_path(path: &Path, clients: &StoredOAuthClients) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let json = serde_json::to_string_pretty(clients).map_err(|e| e.to_string())?;
    std::fs::write(path, json).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

fn load_clients() -> StoredOAuthClients {
    let _guard = OAUTH_CLIENTS_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    match oauth_clients_path() {
        Ok(path) => load_clients_from_path(&path),
        Err(_) => StoredOAuthClients::default(),
    }
}

fn mutate_clients(f: impl FnOnce(&mut StoredOAuthClients)) -> Result<StoredOAuthClients, String> {
    let _guard = OAUTH_CLIENTS_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let path = oauth_clients_path()?;
    let mut clients = load_clients_from_path(&path);
    f(&mut clients);
    save_clients_to_path(&path, &clients)?;
    Ok(clients)
}

pub fn save_github_oauth_client(
    client_id: &str,
    client_secret: &str,
    is_app: bool,
    slug: Option<&str>,
    html_url: Option<&str>,
    install_url: Option<&str>,
) -> Result<(), String> {
    let cid = client_id.trim().to_string();
    let csec = client_secret.trim().to_string();
    if cid.is_empty() || csec.is_empty() {
        return Err("GitHub OAuth client_id and client_secret are required".into());
    }
    mutate_clients(|c| {
        c.github_client_id = Some(cid);
        c.github_client_secret = Some(csec);
        c.github_is_app = is_app;
        c.github_app_slug = slug.map(str::trim).filter(|s| !s.is_empty()).map(str::to_string);
        c.github_html_url = html_url.map(str::trim).filter(|s| !s.is_empty()).map(str::to_string);
        c.github_install_url = install_url.map(str::trim).filter(|s| !s.is_empty()).map(str::to_string);
    })?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Crypto, URL & HTTP helpers
// ---------------------------------------------------------------------------

pub fn constant_time_eq(a: &str, b: &str) -> bool {
    let (ab, bb) = (a.as_bytes(), b.as_bytes());
    if ab.len() != bb.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in ab.iter().zip(bb.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

pub fn url_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for byte in s.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{:02X}", byte));
        }
    }
    out
}

pub fn pkce_s256_challenge(verifier: &str) -> String {
    let digest = sodiumoxide::crypto::hash::sha256::hash(verifier.as_bytes());
    URL_SAFE_NO_PAD.encode(digest.as_ref())
}

pub fn pkce_pair() -> Result<(String, String), String> {
    sodiumoxide::init().map_err(|_| "failed to initialize libsodium".to_string())?;
    let verifier = URL_SAFE_NO_PAD.encode(sodiumoxide::randombytes::randombytes(32));
    let challenge = pkce_s256_challenge(&verifier);
    Ok((verifier, challenge))
}

pub fn build_openrouter_auth_url(callback_url: &str, code_challenge: &str) -> String {
    format!(
        "https://openrouter.ai/auth?callback_url={}&code_challenge={}&code_challenge_method=S256",
        url_encode(callback_url),
        url_encode(code_challenge)
    )
}

pub fn build_github_authorize_url(
    client_id: &str,
    redirect_uri: &str,
    state: &str,
    is_app: bool,
) -> String {
    let mut url = format!(
        "https://github.com/login/oauth/authorize?client_id={}&redirect_uri={}&state={}",
        url_encode(client_id.trim()),
        url_encode(redirect_uri),
        url_encode(state),
    );
    if !is_app {
        url.push_str("&scope=repo%20workflow");
    }
    url
}

fn param<'a>(params: &'a [(String, String)], key: &str) -> Option<&'a str> {
    params
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.as_str())
}

#[derive(Default)]
struct PersistResult {
    cloud_saved: bool,
    cloud_error: Option<String>,
    env_path: Option<String>,
    local_error: Option<String>,
}

fn persist_settings(
    settings_url: Option<&str>,
    api_token: Option<&str>,
    pairs: &[(&str, &str)],
) -> PersistResult {
    let mut out = PersistResult::default();
    if pairs.is_empty() {
        return out;
    }

    let mut local_err = None;
    let mut env_path = None;
    for &(k, v) in pairs {
        match crate::save_backend_env_var_blocking(k.to_string(), v.to_string()) {
            Ok(p) => env_path = Some(p),
            Err(e) => {
                local_err = Some(e);
                break;
            }
        }
    }
    out.env_path = env_path;
    out.local_error = local_err;

    if let Some(url) = settings_url.map(str::trim).filter(|u| !u.is_empty()) {
        let mut values = serde_json::Map::new();
        for &(k, v) in pairs {
            values.insert(k.to_string(), serde_json::Value::String(v.to_string()));
        }
        let body = serde_json::json!({ "values": values });
        match github_app::push_to_cloud(
            url,
            api_token.map(str::trim).filter(|t| !t.is_empty()),
            &body,
        ) {
            Ok(()) => out.cloud_saved = true,
            Err(e) => out.cloud_error = Some(e),
        }
    }
    out
}

fn wait_for_oauth_code(
    listener: &TcpListener,
    expected_path: &str,
    expected_state: Option<&str>,
    cancelled: &Arc<AtomicBool>,
    title_ok: &str,
    msg_ok: &str,
) -> Result<String, String> {
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let deadline = Instant::now() + OAUTH_TIMEOUT;
    while Instant::now() < deadline {
        if cancelled.load(Ordering::Relaxed) {
            return Err("Authorization cancelled.".into());
        }
        let mut stream = match listener.accept() {
            Ok((s, _)) => s,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(150));
                continue;
            }
            Err(e) => return Err(e.to_string()),
        };
        let _ = stream.set_nonblocking(false);
        let line = github_app::read_request_line(&mut stream);
        let Some((path, params)) = github_app::parse_request_line(&line) else {
            github_app::respond(&mut stream, "405 Method Not Allowed", "");
            continue;
        };
        if path != expected_path {
            github_app::respond(&mut stream, "404 Not Found", "");
            continue;
        }
        if let Some(err) = param(&params, "error").filter(|e| !e.is_empty()) {
            let desc = param(&params, "error_description").unwrap_or(err);
            github_app::respond(
                &mut stream,
                "400 Bad Request",
                &github_app::result_page(false, "Authorization declined", desc),
            );
            return Err(format!("Authorization declined: {}", desc));
        }
        if let Some(exp) = expected_state {
            let got = param(&params, "state").unwrap_or("");
            if !constant_time_eq(got, exp) {
                github_app::respond(
                    &mut stream,
                    "400 Bad Request",
                    &github_app::result_page(
                        false,
                        "State mismatch",
                        "Please restart sign-in from PR Agent Control.",
                    ),
                );
                continue;
            }
        }
        match param(&params, "code").filter(|c| !c.is_empty() && c.len() <= 512) {
            Some(code) => {
                github_app::respond(
                    &mut stream,
                    "200 OK",
                    &github_app::result_page(true, title_ok, msg_ok),
                );
                return Ok(code.to_string());
            }
            None => {
                github_app::respond(
                    &mut stream,
                    "400 Bad Request",
                    &github_app::result_page(
                        false,
                        "Missing authorization code",
                        "The provider did not return an authorization code.",
                    ),
                );
                return Err("The provider did not return an authorization code.".into());
            }
        }
    }
    Err("Timed out waiting for browser authorization (5 minutes).".into())
}

// ---------------------------------------------------------------------------
// Local CLI detection & readers (no network in status probe)
// ---------------------------------------------------------------------------

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .filter(|p| !p.as_os_str().is_empty())
}

fn read_gh_cli_login() -> Option<String> {
    let mut candidates = Vec::new();
    if let Some(appdata) = std::env::var_os("APPDATA") {
        candidates.push(PathBuf::from(appdata).join("GitHub CLI").join("hosts.yml"));
    }
    if let Some(xdg) = std::env::var_os("XDG_CONFIG_HOME") {
        candidates.push(PathBuf::from(xdg).join("gh").join("hosts.yml"));
    }
    if let Some(home) = home_dir() {
        candidates.push(home.join(".config").join("gh").join("hosts.yml"));
    }
    for path in candidates {
        let Ok(raw) = std::fs::read_to_string(&path) else {
            continue;
        };
        let mut in_gh = false;
        for line in raw.lines() {
            let trimmed = line.trim();
            if !line.starts_with(' ') && !line.starts_with('\t') {
                in_gh = trimmed.starts_with("github.com:");
                continue;
            }
            if in_gh {
                if let Some(rest) = trimmed.strip_prefix("user:") {
                    let login = rest.trim().trim_matches('"').trim_matches('\'').to_string();
                    if !login.is_empty() {
                        return Some(login);
                    }
                }
            }
        }
    }
    None
}

fn read_gh_cli_token() -> Option<String> {
    let out = Command::new("gh").args(["auth", "token"]).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let tok = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if tok.is_empty() { None } else { Some(tok) }
}

fn vercel_auth_candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();
    for env_name in ["APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME"] {
        if let Some(dir) = std::env::var_os(env_name) {
            let base = PathBuf::from(dir).join("com.vercel.cli");
            out.push(base.join("Data").join("auth.json"));
            out.push(base.join("auth.json"));
        }
    }
    if let Some(home) = home_dir() {
        out.push(home.join("Library/Application Support/com.vercel.cli/auth.json"));
        out.push(home.join(".local/share/com.vercel.cli/auth.json"));
    }
    out
}

fn read_vercel_cli_token() -> Option<String> {
    for path in vercel_auth_candidates() {
        let Ok(raw) = std::fs::read_to_string(&path) else {
            continue;
        };
        let Ok(val) = serde_json::from_str::<serde_json::Value>(&raw) else {
            continue;
        };
        if let Some(tok) = val.get("token").and_then(|v| v.as_str()).map(str::trim).filter(|s| !s.is_empty()) {
            return Some(tok.to_string());
        }
    }
    None
}

fn read_commandcode_cli_key() -> Option<String> {
    let path = home_dir()?.join(".commandcode").join("auth.json");
    let raw = std::fs::read_to_string(path).ok()?;
    let val: serde_json::Value = serde_json::from_str(&raw).ok()?;
    for field in ["apiKey", "api_key", "token", "access_token"] {
        if let Some(s) = val.get(field).and_then(|v| v.as_str()).map(str::trim).filter(|s| !s.is_empty()) {
            return Some(s.to_string());
        }
    }
    None
}

fn opencode_auth_candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();
    for env_name in ["XDG_DATA_HOME", "LOCALAPPDATA", "APPDATA"] {
        if let Some(dir) = std::env::var_os(env_name) {
            out.push(PathBuf::from(dir).join("opencode").join("auth.json"));
        }
    }
    if let Some(home) = home_dir() {
        out.push(home.join(".local/share/opencode/auth.json"));
        out.push(home.join(".opencode/auth.json"));
    }
    out
}

fn extract_opencode_key_from_json(val: &serde_json::Value) -> Option<String> {
    for section in ["opencode-go", "opencode_go", "opencode"] {
        let Some(obj) = val.get(section) else {
            continue;
        };
        for field in ["key", "apiKey", "api_key", "token", "access"] {
            if let Some(s) = obj.get(field).and_then(|v| v.as_str()).map(str::trim).filter(|s| !s.is_empty()) {
                return Some(s.to_string());
            }
        }
    }
    None
}

fn read_opencode_cli_key() -> Option<String> {
    for path in opencode_auth_candidates() {
        let Ok(raw) = std::fs::read_to_string(&path) else {
            continue;
        };
        let Ok(val) = serde_json::from_str::<serde_json::Value>(&raw) else {
            continue;
        };
        if let Some(k) = extract_opencode_key_from_json(&val) {
            return Some(k);
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Status & client management commands
// ---------------------------------------------------------------------------

#[tauri::command]
pub fn oauth_cancel(state: tauri::State<'_, OAuthState>) -> Result<(), String> {
    state.cancel_active();
    Ok(())
}

#[tauri::command]
pub async fn oauth_status() -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(oauth_status_blocking)
        .await
        .map_err(|e| e.to_string())?
}

fn oauth_status_blocking() -> Result<serde_json::Value, String> {
    let clients = load_clients();
    let has_gh_oauth = clients
        .github_client_id
        .as_deref()
        .is_some_and(|s| !s.is_empty())
        && clients
            .github_client_secret
            .as_deref()
            .is_some_and(|s| !s.is_empty());
    let has_vercel_oauth = clients
        .vercel_client_id
        .as_deref()
        .is_some_and(|s| !s.is_empty());
    let gh_available = read_gh_cli_token().is_some();
    let gh_login = if gh_available { read_gh_cli_login() } else { None };
    Ok(serde_json::json!({
        "github": {
            "has_oauth": has_gh_oauth,
            "is_app": clients.github_is_app,
            "client_id": clients.github_client_id,
            "app_slug": clients.github_app_slug,
            "html_url": clients.github_html_url,
            "install_url": clients.github_install_url,
        },
        "vercel": {
            "has_oauth": has_vercel_oauth,
            "client_id": clients.vercel_client_id,
        },
        "cli": {
            "gh_available": gh_available,
            "gh_login": gh_login,
            "vercel_cli": read_vercel_cli_token().is_some(),
            "commandcode_cli": read_commandcode_cli_key().is_some(),
            "opencode_cli": read_opencode_cli_key().is_some(),
        }
    }))
}

#[tauri::command]
pub async fn oauth_save_client(
    provider: String,
    client_id: String,
    client_secret: Option<String>,
    is_app: Option<bool>,
) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let p = provider.trim().to_lowercase();
        let cid = client_id.trim().to_string();
        let csec = client_secret.map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
        if cid.is_empty() {
            return Err("client_id is required".into());
        }
        match p.as_str() {
            "github" => {
                let Some(secret) = csec else {
                    return Err("client_secret is required for GitHub OAuth".into());
                };
                mutate_clients(|c| {
                    c.github_client_id = Some(cid);
                    c.github_client_secret = Some(secret);
                    c.github_is_app = is_app.unwrap_or(false);
                })?;
                Ok(())
            }
            "vercel" => {
                mutate_clients(|c| {
                    c.vercel_client_id = Some(cid);
                    c.vercel_client_secret = csec;
                })?;
                Ok(())
            }
            _ => Err("provider must be 'github' or 'vercel'".into()),
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn oauth_clear_client(provider: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let p = provider.trim().to_lowercase();
        match p.as_str() {
            "github" => {
                mutate_clients(|c| {
                    c.github_client_id = None;
                    c.github_client_secret = None;
                    c.github_is_app = false;
                    c.github_app_slug = None;
                    c.github_html_url = None;
                    c.github_install_url = None;
                })?;
                Ok(())
            }
            "vercel" => {
                mutate_clients(|c| {
                    c.vercel_client_id = None;
                    c.vercel_client_secret = None;
                })?;
                Ok(())
            }
            _ => Err("provider must be 'github' or 'vercel'".into()),
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

// ---------------------------------------------------------------------------
// 1. OpenRouter 1-click PKCE OAuth (zero Client ID required)
// ---------------------------------------------------------------------------

#[tauri::command]
pub async fn openrouter_oauth_connect(
    state: tauri::State<'_, OAuthState>,
    settings_url: Option<String>,
    api_token: Option<String>,
) -> Result<serde_json::Value, String> {
    let cancelled = state.begin_flow();
    tauri::async_runtime::spawn_blocking(move || {
        openrouter_oauth_blocking(cancelled, settings_url, api_token)
    })
    .await
    .map_err(|e| e.to_string())?
}

fn openrouter_oauth_blocking(
    cancelled: Arc<AtomicBool>,
    settings_url: Option<String>,
    api_token: Option<String>,
) -> Result<serde_json::Value, String> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    let (verifier, challenge) = pkce_pair()?;
    let path_state = github_app::random_state()?;
    let callback_path = format!("/callback/{}", path_state);
    let callback_url = format!("http://127.0.0.1:{}{}", port, callback_path);
    let auth_url = build_openrouter_auth_url(&callback_url, &challenge);

    tauri_plugin_opener::open_url(&auth_url, None::<&str>)
        .map_err(|e| format!("could not open the browser: {}", e))?;

    let code = wait_for_oauth_code(
        &listener,
        &callback_path,
        None,
        &cancelled,
        "OpenRouter connected",
        "Your OpenRouter API key has been generated and sent to PR Agent Control.",
    )?;
    drop(listener);

    let res = crate::gh_client()?
        .post("https://openrouter.ai/api/v1/auth/keys")
        .header("Content-Type", "application/json")
        .json(&serde_json::json!({
            "code": code,
            "code_verifier": verifier,
            "code_challenge_method": "S256"
        }))
        .send()
        .map_err(|e| format!("OpenRouter key exchange failed: {}", e))?;
    let status = res.status();
    let body: serde_json::Value = res.json().map_err(|e| e.to_string())?;
    if !status.is_success() {
        let msg = body
            .get("error")
            .and_then(|e| e.get("message"))
            .and_then(|m| m.as_str())
            .unwrap_or("key exchange rejected");
        return Err(format!("OpenRouter rejected OAuth code (HTTP {}): {}", status, msg));
    }
    let key = body
        .get("key")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or("OpenRouter did not return an API key")?
        .to_string();
    let user_id = body.get("user_id").and_then(|v| v.as_str()).map(str::to_string);

    let saved = persist_settings(
        settings_url.as_deref(),
        api_token.as_deref(),
        &[("OPENROUTER_API_KEY", &key)],
    );
    Ok(serde_json::json!({
        "key": key,
        "user_id": user_id,
        "cloud_saved": saved.cloud_saved,
        "cloud_error": saved.cloud_error,
        "env_path": saved.env_path,
        "local_error": saved.local_error,
    }))
}

// ---------------------------------------------------------------------------
// 2. GitHub 1-click OAuth (existing App/OAuth OR chained App-Manifest -> Install+OAuth)
// ---------------------------------------------------------------------------

fn exchange_github_code(
    client_id: &str,
    client_secret: &str,
    code: &str,
    redirect_uri: Option<&str>,
) -> Result<String, String> {
    let mut body = serde_json::json!({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
    });
    if let Some(uri) = redirect_uri {
        if let Some(map) = body.as_object_mut() {
            map.insert("redirect_uri".into(), serde_json::Value::String(uri.into()));
        }
    }
    let res = crate::gh_client()?
        .post("https://github.com/login/oauth/access_token")
        .header("Accept", "application/json")
        .json(&body)
        .send()
        .map_err(|e| format!("GitHub token exchange failed: {}", e))?;
    let status = res.status();
    let data: serde_json::Value = res.json().map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("GitHub OAuth token exchange failed (HTTP {})", status));
    }
    if let Some(err) = data.get("error").and_then(|v| v.as_str()) {
        let desc = data.get("error_description").and_then(|v| v.as_str()).unwrap_or(err);
        return Err(format!("GitHub OAuth error ({}): {}", err, desc));
    }
    data.get("access_token")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .ok_or_else(|| "GitHub did not return an access_token".into())
}

fn fetch_github_user(token: &str) -> Result<(String, Option<String>), String> {
    let client = crate::gh_client()?;
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
    let avatar_url = body
        .get("avatar_url")
        .and_then(|v| v.as_str())
        .map(str::to_string);
    Ok((login, avatar_url))
}

fn bind_github_oauth_listener() -> Result<(TcpListener, u16), String> {
    for &p in github_app::GITHUB_OAUTH_PORTS {
        if let Ok(listener) = TcpListener::bind(("127.0.0.1", p)) {
            return Ok((listener, p));
        }
    }
    Err("Ports 17492-17494 are busy; close other GitHub sign-in flows and retry.".into())
}

#[tauri::command]
#[allow(clippy::too_many_arguments)]
pub async fn github_oauth_connect(
    state: tauri::State<'_, OAuthState>,
    backend_url: Option<String>,
    settings_url: Option<String>,
    api_token: Option<String>,
    save_to_backend: Option<bool>,
    force_new_app: Option<bool>,
    app_name: Option<String>,
    org: Option<String>,
) -> Result<serde_json::Value, String> {
    let cancelled = state.begin_flow();
    tauri::async_runtime::spawn_blocking(move || {
        github_oauth_blocking(
            cancelled,
            backend_url,
            settings_url,
            api_token,
            save_to_backend.unwrap_or(false),
            force_new_app.unwrap_or(false),
            app_name,
            org,
        )
    })
    .await
    .map_err(|e| e.to_string())?
}

#[allow(clippy::too_many_arguments)]
fn github_oauth_blocking(
    cancelled: Arc<AtomicBool>,
    backend_url: Option<String>,
    settings_url: Option<String>,
    api_token: Option<String>,
    save_to_backend: bool,
    force_new_app: bool,
    app_name: Option<String>,
    org: Option<String>,
) -> Result<serde_json::Value, String> {
    let clients = load_clients();
    let has_saved = clients.github_client_id.as_deref().is_some_and(|s| !s.is_empty())
        && clients.github_client_secret.as_deref().is_some_and(|s| !s.is_empty());

    let (listener, port) = bind_github_oauth_listener()?;
    let oauth_redirect = format!("http://127.0.0.1:{}/oauth/callback", port);

    if has_saved && !force_new_app {
        let cid = clients.github_client_id.as_deref().unwrap_or("");
        let csec = clients.github_client_secret.as_deref().unwrap_or("");
        let oauth_state = github_app::random_state()?;
        let auth_url = build_github_authorize_url(cid, &oauth_redirect, &oauth_state, clients.github_is_app);
        tauri_plugin_opener::open_url(&auth_url, None::<&str>)
            .map_err(|e| format!("could not open the browser: {}", e))?;

        let code = wait_for_oauth_code(
            &listener,
            "/oauth/callback",
            Some(&oauth_state),
            &cancelled,
            "GitHub connected",
            "You are signed in to PR Agent Control.",
        )?;
        drop(listener);

        let access_token = exchange_github_code(cid, csec, &code, Some(&oauth_redirect))?;
        let (login, avatar_url) = fetch_github_user(&access_token)?;
        let saved = if save_to_backend {
            persist_settings(
                settings_url.as_deref(),
                api_token.as_deref(),
                &[("GITHUB_TOKEN", &access_token)],
            )
        } else {
            PersistResult::default()
        };
        return Ok(serde_json::json!({
            "access_token": access_token,
            "login": login,
            "avatar_url": avatar_url,
            "mode": "oauth",
            "app_slug": clients.github_app_slug,
            "html_url": clients.github_html_url,
            "install_url": clients.github_install_url,
            "cloud_saved": saved.cloud_saved,
            "cloud_error": saved.cloud_error,
            "env_path": saved.env_path,
            "local_error": saved.local_error,
        }));
    }

    // Path B: First-time 1-click GitHub App manifest -> Install & Authorize -> OAuth token
    let raw_name = app_name.unwrap_or_default().trim().to_string();
    let resolved_name = if raw_name.is_empty() || raw_name == "PR Agent Control" {
        let suffix = &github_app::random_state()?[..4];
        format!("PR Agent Control {}", suffix)
    } else {
        if !github_app::valid_app_name(&raw_name) {
            return Err("App name: 1-34 characters (letters, digits, space, - _ .)".into());
        }
        raw_name
    };
    let org = org.map(|o| o.trim().to_string()).filter(|o| !o.is_empty());
    if let Some(o) = &org {
        if !github_app::valid_org(o) {
            return Err("invalid organization name".into());
        }
    }
    let webhook_base = backend_url
        .as_deref()
        .map(str::trim)
        .filter(|u| !u.is_empty())
        .and_then(|u| github_app::public_webhook_base(u).ok());

    let manifest_state = github_app::random_state()?;
    let oauth_state = github_app::random_state()?;
    let manifest_redirect = format!("http://127.0.0.1:{}/manifest/callback", port);
    let manifest = github_app::build_manifest(&resolved_name, webhook_base.as_deref(), &manifest_redirect);
    let action = match &org {
        Some(o) => format!("https://github.com/organizations/{}/settings/apps/new?state={}", o, manifest_state),
        None => format!("https://github.com/settings/apps/new?state={}", manifest_state),
    };
    let start_html = github_app::start_page(&action, &manifest);

    tauri_plugin_opener::open_url(format!("http://127.0.0.1:{}/start", port), None::<&str>)
        .map_err(|e| format!("could not open the browser: {}", e))?;

    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let deadline = Instant::now() + MANIFEST_OAUTH_TIMEOUT;

    struct ConvertedApp {
        id: i64,
        slug: String,
        html_url: String,
        install_url: String,
        client_id: String,
        client_secret: String,
        cloud_saved: bool,
        cloud_error: Option<String>,
        env_path: Option<String>,
        local_error: Option<String>,
    }
    let mut converted: Option<ConvertedApp> = None;
    let mut oauth_code: Option<String> = None;
    let mut installation_id: Option<String> = None;

    while Instant::now() < deadline {
        if cancelled.load(Ordering::Relaxed) {
            return Err("GitHub sign-in cancelled.".into());
        }
        let mut stream = match listener.accept() {
            Ok((s, _)) => s,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(150));
                continue;
            }
            Err(e) => return Err(e.to_string()),
        };
        let _ = stream.set_nonblocking(false);
        let line = github_app::read_request_line(&mut stream);
        let Some((path, params)) = github_app::parse_request_line(&line) else {
            github_app::respond(&mut stream, "405 Method Not Allowed", "");
            continue;
        };
        match path.as_str() {
            "/start" => github_app::respond(&mut stream, "200 OK", &start_html),
            "/manifest/callback" => {
                if converted.is_some() {
                    github_app::respond(
                        &mut stream,
                        "200 OK",
                        &github_app::result_page(true, "App already created", "Continue installing in GitHub."),
                    );
                    continue;
                }
                let got_state = param(&params, "state").unwrap_or("");
                if !constant_time_eq(got_state, &manifest_state) {
                    github_app::respond(
                        &mut stream,
                        "400 Bad Request",
                        &github_app::done_page(false, "State mismatch; start again from PR Agent Control."),
                    );
                    continue;
                }
                let Some(manifest_code) = param(&params, "code").filter(|c| !c.is_empty() && c.len() <= 200) else {
                    github_app::respond(
                        &mut stream,
                        "400 Bad Request",
                        &github_app::done_page(false, "GitHub did not return a manifest code."),
                    );
                    return Err("GitHub did not return a manifest code".into());
                };
                let app = match github_app::convert_code(manifest_code) {
                    Ok(a) => a,
                    Err(e) => {
                        github_app::respond(&mut stream, "502 Bad Gateway", &github_app::done_page(false, &e));
                        return Err(e);
                    }
                };
                let id = app.get("id").and_then(|v| v.as_i64()).ok_or("GitHub response had no App id")?;
                let slug = app.get("slug").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let pem = app.get("pem").and_then(|v| v.as_str()).ok_or("GitHub response had no private key")?;
                let html_url = app.get("html_url").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let client_id = app.get("client_id").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
                let client_secret = app.get("client_secret").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
                if client_id.is_empty() || client_secret.is_empty() {
                    let err = "GitHub did not return OAuth client credentials for the App";
                    github_app::respond(&mut stream, "502 Bad Gateway", &github_app::done_page(false, err));
                    return Err(err.into());
                }
                let webhook_secret = app
                    .get("webhook_secret")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                    .map(str::to_string);
                let install_url = if html_url.is_empty() {
                    format!("https://github.com/apps/{}/installations/new", slug)
                } else {
                    format!("{}/installations/new", html_url.trim_end_matches('/'))
                };

                let _ = save_github_oauth_client(
                    &client_id,
                    &client_secret,
                    true,
                    Some(&slug),
                    Some(&html_url),
                    Some(&install_url),
                );

                // Best-effort save to local backend/.env + cloud backend.
                let local = (|| -> Result<String, String> {
                    let mut p = crate::save_backend_env_var_blocking("GITHUB_APP_ID".into(), id.to_string())?;
                    if !slug.is_empty() {
                        crate::save_backend_env_var_blocking("GITHUB_APP_SLUG".into(), slug.clone())?;
                    }
                    crate::save_backend_env_var_blocking("GITHUB_APP_PRIVATE_KEY".into(), github_app::pem_to_env(pem))?;
                    if let Some(secret) = &webhook_secret {
                        p = crate::save_backend_env_var_blocking("GITHUB_APP_WEBHOOK_SECRET".into(), secret.clone())?;
                    }
                    Ok(p)
                })();
                let cloud = match settings_url.as_deref().map(str::trim).filter(|u| !u.is_empty()) {
                    Some(url) => Some(github_app::push_to_cloud(
                        url,
                        api_token.as_deref().map(str::trim).filter(|t| !t.is_empty()),
                        &github_app::cloud_settings_body(id, &slug, pem, webhook_secret.as_deref()),
                    )),
                    None => None,
                };
                let (env_path, local_error) = match local {
                    Ok(p) => (Some(p), None),
                    Err(e) => (None, Some(e)),
                };
                converted = Some(ConvertedApp {
                    id,
                    slug,
                    html_url,
                    install_url: install_url.clone(),
                    client_id,
                    client_secret,
                    cloud_saved: matches!(cloud, Some(Ok(()))),
                    cloud_error: match &cloud { Some(Err(e)) => Some(e.clone()), _ => None },
                    env_path,
                    local_error,
                });

                let next = format!("{}?state={}", install_url, url_encode(&oauth_state));
                github_app::respond_redirect(&mut stream, &next);
            }
            "/oauth/callback" => {
                let Some(ref conv) = converted else {
                    github_app::respond(
                        &mut stream,
                        "400 Bad Request",
                        &github_app::result_page(false, "App creation incomplete", "Complete the GitHub App creation step first."),
                    );
                    continue;
                };
                let _ = conv;
                if let Some(err) = param(&params, "error").filter(|e| !e.is_empty()) {
                    let desc = param(&params, "error_description").unwrap_or(err);
                    github_app::respond(
                        &mut stream,
                        "400 Bad Request",
                        &github_app::result_page(false, "GitHub authorization declined", desc),
                    );
                    return Err(format!("GitHub authorization declined: {}", desc));
                }
                let setup_action = param(&params, "setup_action");
                if let Some(got_state) = param(&params, "state").filter(|s| !s.is_empty()) {
                    if !constant_time_eq(got_state, &oauth_state) {
                        github_app::respond(
                            &mut stream,
                            "400 Bad Request",
                            &github_app::result_page(false, "State mismatch", "Please retry from PR Agent Control."),
                        );
                        continue;
                    }
                } else if setup_action != Some("install") {
                    github_app::respond(
                        &mut stream,
                        "400 Bad Request",
                        &github_app::result_page(false, "Missing state", "Please retry from PR Agent Control."),
                    );
                    continue;
                }
                if let Some(code) = param(&params, "code").filter(|c| !c.is_empty() && c.len() <= 512) {
                    installation_id = param(&params, "installation_id").map(str::to_string);
                    oauth_code = Some(code.to_string());
                    github_app::respond(
                        &mut stream,
                        "200 OK",
                        &github_app::result_page(
                            true,
                            "GitHub connected & configured",
                            "Your GitHub App is installed and PR Agent Control is signed in.",
                        ),
                    );
                    break;
                } else {
                    github_app::respond(
                        &mut stream,
                        "400 Bad Request",
                        &github_app::result_page(
                            false,
                            "Authorization code missing",
                            "The App was created, but GitHub did not return an OAuth code. Click Connect with GitHub to finish signing in.",
                        ),
                    );
                    return Err("GitHub did not return an OAuth code after App install.".into());
                }
            }
            _ => github_app::respond(&mut stream, "404 Not Found", ""),
        }
    }
    drop(listener);

    let conv = converted.ok_or("Timed out waiting for GitHub App creation.")?;
    let code = oauth_code.ok_or("Timed out waiting for GitHub App installation & OAuth.")?;
    let access_token = exchange_github_code(&conv.client_id, &conv.client_secret, &code, None)?;
    let (login, avatar_url) = fetch_github_user(&access_token)?;

    let mut cloud_saved = conv.cloud_saved;
    let mut cloud_error = conv.cloud_error;
    let mut env_path = conv.env_path;
    let mut local_error = conv.local_error;
    if save_to_backend {
        let tok_save = persist_settings(
            settings_url.as_deref(),
            api_token.as_deref(),
            &[("GITHUB_TOKEN", &access_token)],
        );
        cloud_saved = cloud_saved || tok_save.cloud_saved;
        if cloud_error.is_none() {
            cloud_error = tok_save.cloud_error;
        }
        if env_path.is_none() {
            env_path = tok_save.env_path;
        }
        if local_error.is_none() {
            local_error = tok_save.local_error;
        }
    }

    Ok(serde_json::json!({
        "access_token": access_token,
        "login": login,
        "avatar_url": avatar_url,
        "mode": "app_manifest",
        "app_id": conv.id,
        "app_slug": conv.slug,
        "html_url": conv.html_url,
        "install_url": conv.install_url,
        "webhook_active": webhook_base.is_some(),
        "installation_id": installation_id,
        "cloud_saved": cloud_saved,
        "cloud_error": cloud_error,
        "env_path": env_path,
        "local_error": local_error,
    }))
}

#[tauri::command]
pub async fn github_import_cli(
    settings_url: Option<String>,
    api_token: Option<String>,
    save_to_backend: Option<bool>,
) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let access_token = read_gh_cli_token()
            .ok_or("No active GitHub CLI session found. Run `gh auth login` or use Connect with GitHub.")?;
        let (login, avatar_url) = fetch_github_user(&access_token)?;
        let saved = if save_to_backend.unwrap_or(false) {
            persist_settings(
                settings_url.as_deref(),
                api_token.as_deref(),
                &[("GITHUB_TOKEN", &access_token)],
            )
        } else {
            PersistResult::default()
        };
        Ok(serde_json::json!({
            "access_token": access_token,
            "login": login,
            "avatar_url": avatar_url,
            "cloud_saved": saved.cloud_saved,
            "cloud_error": saved.cloud_error,
            "env_path": saved.env_path,
            "local_error": saved.local_error,
        }))
    })
    .await
    .map_err(|e| e.to_string())?
}

// ---------------------------------------------------------------------------
// 3. Vercel OAuth / CLI import + team discovery
// ---------------------------------------------------------------------------

fn vercel_user_and_teams(token: &str) -> Result<(String, Vec<serde_json::Value>), String> {
    let client = crate::gh_client()?;
    let user_res = client
        .get("https://api.vercel.com/v2/user")
        .header("Authorization", format!("Bearer {}", token))
        .send()
        .map_err(|e| format!("Vercel unreachable: {}", e))?;
    let ustatus = user_res.status();
    let ubody: serde_json::Value = user_res.json().map_err(|e| e.to_string())?;
    if !ustatus.is_success() {
        return Err(format!("Vercel rejected the token (HTTP {})", ustatus));
    }
    let username = ubody
        .get("user")
        .and_then(|u| u.get("username"))
        .or_else(|| ubody.get("username"))
        .and_then(|v| v.as_str())
        .unwrap_or("vercel-user")
        .to_string();

    let mut teams = Vec::new();
    if let Ok(tres) = client
        .get("https://api.vercel.com/v2/teams")
        .header("Authorization", format!("Bearer {}", token))
        .send()
    {
        if tres.status().is_success() {
            if let Ok(tbody) = tres.json::<serde_json::Value>() {
                if let Some(arr) = tbody.get("teams").and_then(|v| v.as_array()) {
                    for t in arr {
                        if let Some(id) = t.get("id").and_then(|v| v.as_str()) {
                            teams.push(serde_json::json!({
                                "id": id,
                                "slug": t.get("slug").and_then(|v| v.as_str()).unwrap_or(""),
                                "name": t.get("name").and_then(|v| v.as_str()).unwrap_or(id),
                            }));
                        }
                    }
                }
            }
        }
    }
    Ok((username, teams))
}

fn finish_vercel_connect(
    token: &str,
    settings_url: Option<&str>,
    api_token: Option<&str>,
) -> Result<serde_json::Value, String> {
    let (user, teams) = vercel_user_and_teams(token)?;
    let team_id = if teams.len() == 1 {
        teams[0].get("id").and_then(|v| v.as_str()).map(str::to_string)
    } else {
        None
    };
    let mut pairs: Vec<(&str, &str)> = vec![("VERCEL_TOKEN", token)];
    if let Some(tid) = team_id.as_deref() {
        pairs.push(("VERCEL_TEAM_ID", tid));
    }
    let saved = persist_settings(settings_url, api_token, &pairs);
    Ok(serde_json::json!({
        "user": user,
        "teams": teams,
        "team_id": team_id,
        "cloud_saved": saved.cloud_saved,
        "cloud_error": saved.cloud_error,
        "env_path": saved.env_path,
        "local_error": saved.local_error,
    }))
}

#[tauri::command]
pub async fn vercel_fetch_teams(token: String) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let t = token.trim();
        if t.is_empty() {
            return Err("Vercel token is required".into());
        }
        let (user, teams) = vercel_user_and_teams(t)?;
        Ok(serde_json::json!({ "user": user, "teams": teams }))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn vercel_import_cli(
    settings_url: Option<String>,
    api_token: Option<String>,
) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let token = read_vercel_cli_token()
            .ok_or("No Vercel CLI login found. Run `vercel login` or paste a Vercel token.")?;
        finish_vercel_connect(&token, settings_url.as_deref(), api_token.as_deref())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn vercel_oauth_connect(
    state: tauri::State<'_, OAuthState>,
    settings_url: Option<String>,
    api_token: Option<String>,
) -> Result<serde_json::Value, String> {
    let cancelled = state.begin_flow();
    tauri::async_runtime::spawn_blocking(move || {
        let clients = load_clients();
        let client_id = clients
            .vercel_client_id
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or("Configure a Vercel OAuth Client ID first, or use Import from Vercel CLI.")?
            .to_string();
        let client_secret = clients.vercel_client_secret.clone();

        let listener = TcpListener::bind(("127.0.0.1", VERCEL_OAUTH_PORT))
            .or_else(|_| TcpListener::bind("127.0.0.1:0"))
            .map_err(|e| e.to_string())?;
        let port = listener.local_addr().map_err(|e| e.to_string())?.port();
        let redirect_uri = format!("http://127.0.0.1:{}/vercel/callback", port);
        let (verifier, challenge) = pkce_pair()?;
        let oauth_state = github_app::random_state()?;
        let auth_url = format!(
            "https://vercel.com/oauth/authorize?client_id={}&redirect_uri={}&response_type=code&state={}&code_challenge={}&code_challenge_method=S256",
            url_encode(&client_id),
            url_encode(&redirect_uri),
            url_encode(&oauth_state),
            url_encode(&challenge),
        );
        tauri_plugin_opener::open_url(&auth_url, None::<&str>)
            .map_err(|e| format!("could not open the browser: {}", e))?;

        let code = wait_for_oauth_code(
            &listener,
            "/vercel/callback",
            Some(&oauth_state),
            &cancelled,
            "Vercel connected",
            "Your Vercel account has been connected to PR Agent Control.",
        )?;
        drop(listener);

        let mut form: Vec<(&str, &str)> = vec![
            ("client_id", &client_id),
            ("code", &code),
            ("redirect_uri", &redirect_uri),
            ("code_verifier", &verifier),
        ];
        if let Some(sec) = client_secret.as_deref() {
            form.push(("client_secret", sec));
        }
        let res = crate::gh_client()?
            .post("https://api.vercel.com/v2/oauth/access_token")
            .form(&form)
            .send()
            .map_err(|e| format!("Vercel OAuth exchange failed: {}", e))?;
        let status = res.status();
        let body: serde_json::Value = res.json().map_err(|e| e.to_string())?;
        if !status.is_success() {
            return Err(format!("Vercel OAuth failed (HTTP {}): {}", status, body));
        }
        let token = body
            .get("access_token")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or("Vercel did not return an access_token")?;
        finish_vercel_connect(token, settings_url.as_deref(), api_token.as_deref())
    })
    .await
    .map_err(|e| e.to_string())?
}

// ---------------------------------------------------------------------------
// 4. Command Code & OpenCode Go CLI imports
// ---------------------------------------------------------------------------

fn validate_openai_compatible_key(models_url: &str, key: &str, label: &str) -> Result<(), String> {
    let res = crate::gh_client()?
        .get(models_url)
        .header("Authorization", format!("Bearer {}", key))
        .send()
        .map_err(|e| format!("{} unreachable: {}", label, e))?;
    if res.status().is_success() {
        Ok(())
    } else {
        Err(format!("{} rejected the CLI key (HTTP {})", label, res.status()))
    }
}

#[tauri::command]
pub async fn commandcode_import_cli(
    settings_url: Option<String>,
    api_token: Option<String>,
) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let key = read_commandcode_cli_key()
            .ok_or("No Command Code CLI login found (~/.commandcode/auth.json). Run `cmd login` or paste an API key.")?;
        validate_openai_compatible_key(
            "https://api.commandcode.ai/provider/v1/models",
            &key,
            "Command Code",
        )?;
        let saved = persist_settings(
            settings_url.as_deref(),
            api_token.as_deref(),
            &[("COMMANDCODE_API_KEY", &key)],
        );
        Ok(serde_json::json!({
            "key": key,
            "cloud_saved": saved.cloud_saved,
            "cloud_error": saved.cloud_error,
            "env_path": saved.env_path,
            "local_error": saved.local_error,
        }))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn opencode_import_cli(
    settings_url: Option<String>,
    api_token: Option<String>,
) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let key = read_opencode_cli_key()
            .ok_or("No OpenCode Go CLI key found in OpenCode auth.json. Generate a key at opencode.ai/go.")?;
        validate_openai_compatible_key(
            "https://opencode.ai/inference/go/v1/models",
            &key,
            "OpenCode Go",
        )?;
        let saved = persist_settings(
            settings_url.as_deref(),
            api_token.as_deref(),
            &[("OPENCODE_GO_API_KEY", &key)],
        );
        Ok(serde_json::json!({
            "key": key,
            "cloud_saved": saved.cloud_saved,
            "cloud_error": saved.cloud_error,
            "env_path": saved.env_path,
            "local_error": saved.local_error,
        }))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pkce_s256_matches_rfc7636_test_vector() {
        // RFC 7636 Appendix B test vector
        let verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
        assert_eq!(
            pkce_s256_challenge(verifier),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        );
        let (v, c) = pkce_pair().unwrap();
        assert!(!v.is_empty() && !c.is_empty() && v != c);
        assert_eq!(pkce_s256_challenge(&v), c);
    }

    #[test]
    fn url_encoding_and_auth_urls() {
        assert_eq!(
            url_encode("http://127.0.0.1:54321/callback/a b~_.-"),
            "http%3A%2F%2F127.0.0.1%3A54321%2Fcallback%2Fa%20b~_.-"
        );
        let or_url = build_openrouter_auth_url("http://127.0.0.1:9000/callback/abc", "chal123");
        assert!(or_url.starts_with("https://openrouter.ai/auth?callback_url=http%3A%2F%2F127.0.0.1%3A9000%2Fcallback%2Fabc"));
        assert!(or_url.contains("&code_challenge=chal123&code_challenge_method=S256"));

        let gh_app_url = build_github_authorize_url("Iv1.abc", "http://127.0.0.1:17492/oauth/callback", "st", true);
        assert!(!gh_app_url.contains("scope="));
        let gh_oauth_url = build_github_authorize_url("Ov23abc", "http://127.0.0.1:17492/oauth/callback", "st", false);
        assert!(gh_oauth_url.contains("&scope=repo%20workflow"));
    }

    #[test]
    fn constant_time_state_checks() {
        assert!(constant_time_eq("abc123", "abc123"));
        assert!(!constant_time_eq("abc123", "abc124"));
        assert!(!constant_time_eq("abc123", "abc1234"));
    }

    #[test]
    fn clients_persist_in_scratch_dir() {
        let dir = std::env::temp_dir().join(format!("pr-agent-oauth-{}", std::process::id()));
        let path = dir.join("oauth_clients.json");
        let clients = StoredOAuthClients {
            github_client_id: Some("Iv23test".into()),
            github_client_secret: Some("sec_test".into()),
            github_is_app: true,
            github_app_slug: Some("my-app".into()),
            github_html_url: Some("https://github.com/apps/my-app".into()),
            github_install_url: Some("https://github.com/apps/my-app/installations/new".into()),
            vercel_client_id: Some("oac_123".into()),
            vercel_client_secret: None,
        };
        save_clients_to_path(&path, &clients).unwrap();
        let loaded = load_clients_from_path(&path);
        assert_eq!(loaded, clients);
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn extracts_opencode_keys() {
        let v = serde_json::json!({
            "opencode-go": { "key": "oc_sk_123" },
            "opencode": { "key": "oc_fallback" }
        });
        assert_eq!(extract_opencode_key_from_json(&v).as_deref(), Some("oc_sk_123"));
    }
}
