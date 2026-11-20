//! One-click GitHub App creation via the App-manifest flow.
//!
//! 1. Bind a loopback listener on a random port and generate a random `state`.
//! 2. Open `http://127.0.0.1:<port>/start` in the browser. That page auto-submits
//!    the App manifest (name, permissions, events, webhook URL) to GitHub.
//! 3. The user confirms on GitHub, which redirects to `/callback?code=..&state=..`.
//! 4. Exchange the one-time code (`POST /app-manifests/{code}/conversions`) for the
//!    App id, slug, private key and webhook secret, and write them to
//!    `backend/.env` through the same writer as the Integrations tab.
//!
//! The private key and secrets never reach the webview: the command only returns
//! the App's slug, id and URLs.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::{Duration, Instant};

const FLOW_TIMEOUT: Duration = Duration::from_secs(10 * 60);
const HOMEPAGE: &str = "https://github.com/KB01111/Helios-Pr-Agent";
pub const GITHUB_OAUTH_PORTS: &[u16] = &[17492, 17493, 17494];

pub fn github_callback_urls_for(preferred_port: Option<u16>) -> Vec<String> {
    let mut ports: Vec<u16> = Vec::with_capacity(GITHUB_OAUTH_PORTS.len() + 1);
    if let Some(p) = preferred_port {
        ports.push(p);
    }
    for &p in GITHUB_OAUTH_PORTS {
        if !ports.contains(&p) {
            ports.push(p);
        }
    }
    ports
        .into_iter()
        .map(|p| format!("http://127.0.0.1:{}/oauth/callback", p))
        .collect()
}

/// Events the agent consumes. `installation*` events are always sent to Apps.
pub const APP_EVENTS: &[&str] = &[
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_review_thread",
    "issue_comment",
    "check_suite",
    "check_run",
    "status",
    "push",
];

pub fn valid_app_name(name: &str) -> bool {
    let n = name.trim();
    // GitHub limits App names to 34 characters.
    !n.is_empty()
        && n.chars().count() <= 34
        && n.chars().all(|c| c.is_ascii_alphanumeric() || " -_.".contains(c))
}

pub fn valid_org(org: &str) -> bool {
    !org.is_empty()
        && org.len() <= 39
        && org.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
        && !org.starts_with('-')
}

/// True for addresses GitHub's servers can never reach: loopback, private,
/// link-local, unspecified, and IPv6 unique-local ranges.
fn non_public_ip(ip: std::net::IpAddr) -> bool {
    match ip {
        std::net::IpAddr::V4(v4) => {
            v4.is_loopback() || v4.is_private() || v4.is_link_local() || v4.is_unspecified()
                || v4.is_broadcast() || v4.octets()[0] == 0
                // 100.64.0.0/10 carrier-grade NAT
                || (v4.octets()[0] == 100 && (v4.octets()[1] & 0xc0) == 64)
        }
        std::net::IpAddr::V6(v6) => {
            if let Some(v4) = v6.to_ipv4_mapped() {
                return non_public_ip(std::net::IpAddr::V4(v4));
            }
            let seg0 = v6.segments()[0];
            v6.is_loopback()
                || v6.is_unspecified()
                || (seg0 & 0xfe00) == 0xfc00 // fc00::/7 unique local
                || (seg0 & 0xffc0) == 0xfe80 // fe80::/10 link local
        }
    }
}

/// `https://host[:port][/path]` that GitHub can reach. The webhook path is
/// appended to it, so a query, fragment or credentials are refused.
pub fn public_webhook_base(url: &str) -> Result<String, String> {
    let b = url.trim().trim_end_matches('/').to_string();
    let rest = b
        .strip_prefix("https://")
        .ok_or("the backend URL must be public https:// for GitHub to deliver webhooks")?;
    if rest.contains(['?', '#', '@']) || rest.chars().any(char::is_whitespace) {
        return Err("the backend URL must not contain a query, fragment, credentials or spaces".into());
    }
    let authority = rest.split('/').next().unwrap_or("");
    let host = if let Some(bracketed) = authority.strip_prefix('[') {
        // IPv6 literal: [addr] or [addr]:port
        bracketed.split(']').next().unwrap_or("").to_ascii_lowercase()
    } else {
        authority.split(':').next().unwrap_or("").to_ascii_lowercase()
    };
    let unreachable = "GitHub cannot deliver webhooks to localhost or a private address; use the deployed backend URL";
    if host.is_empty() || host == "localhost" || host.ends_with(".localhost") {
        return Err(unreachable.into());
    }
    if let Ok(ip) = host.parse::<std::net::IpAddr>() {
        if non_public_ip(ip) {
            return Err(unreachable.into());
        }
    }
    Ok(b)
}

pub fn build_manifest_with_port(
    app_name: &str,
    webhook_base: Option<&str>,
    redirect_url: &str,
    preferred_oauth_port: Option<u16>,
) -> serde_json::Value {
    let hook = match webhook_base {
        Some(base) => serde_json::json!({ "url": format!("{}/webhooks/github", base), "active": true }),
        // No public backend yet: create the App with an inactive hook; set it later.
        None => serde_json::json!({ "url": "https://example.com/webhooks/github", "active": false }),
    };
    serde_json::json!({
        "name": app_name.trim(),
        "url": HOMEPAGE,
        "hook_attributes": hook,
        "redirect_url": redirect_url,
        "callback_urls": github_callback_urls_for(preferred_oauth_port),
        "request_oauth_on_install": true,
        "public": false,
        "default_permissions": {
            "contents": "write",
            "workflows": "write",
            "secrets": "write",
            "metadata": "read",
            "pull_requests": "write",
            "issues": "write",
            "checks": "write",
            "statuses": "write"
        },
        "default_events": APP_EVENTS,
    })
}

pub fn build_manifest(app_name: &str, webhook_base: Option<&str>, redirect_url: &str) -> serde_json::Value {
    build_manifest_with_port(app_name, webhook_base, redirect_url, None)
}

pub(crate) fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

pub(crate) fn start_page(action: &str, manifest: &serde_json::Value) -> String {
    format!(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Create GitHub App — PR Agent Control</title>\
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;font-family:system-ui,-apple-system,sans-serif;\
background:#0d1117;color:#f0f6fc}}main{{max-width:440px;padding:28px;border:1px solid #30363d;border-radius:12px;\
background:#161b22;text-align:center}}button{{background:#2f81f7;color:#fff;border:0;padding:10px 18px;border-radius:8px;\
font-weight:600;cursor:pointer}}@media(prefers-color-scheme:light){{body{{background:#f6f8fa;color:#1f2328}}\
main{{background:#fff;border-color:#d0d7de}}}}</style></head>\
<body><main><h2 style=\"margin:0 0 8px\">PR Agent Control</h2><p style=\"opacity:.8;margin:0 0 18px\">Redirecting to GitHub to approve App creation…</p>\
<form id=\"f\" method=\"post\" action=\"{}\"><input type=\"hidden\" name=\"manifest\" value=\"{}\">\
<noscript><button type=\"submit\">Continue to GitHub</button></noscript></form>\
<script>document.getElementById('f').submit()</script></main></body></html>",
        html_escape(action),
        html_escape(&manifest.to_string())
    )
}

pub(crate) fn result_page(ok: bool, title: &str, msg: &str) -> String {
    let icon = if ok { "&#10003;" } else { "&#10005;" };
    let color = if ok { "#3fb950" } else { "#f85149" };
    format!(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>{0} — PR Agent Control</title>\
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;font-family:system-ui,-apple-system,sans-serif;\
background:#0d1117;color:#f0f6fc}}main{{max-width:460px;padding:32px;border:1px solid #30363d;border-radius:14px;\
background:#161b22;text-align:center}}.badge{{display:inline-flex;align-items:center;justify-content:center;width:48px;\
height:48px;border-radius:999px;font-size:24px;font-weight:700;margin-bottom:14px;background:{1}22;color:{1};border:1px solid {1}55}}\
p{{opacity:.82;line-height:1.5;margin:8px 0 0}}@media(prefers-color-scheme:light){{body{{background:#f6f8fa;color:#1f2328}}\
main{{background:#fff;border-color:#d0d7de}}}}</style></head>\
<body><main><div class=\"badge\">{2}</div><h2 style=\"margin:0 0 8px\">{0}</h2><p>{3}</p>\
<p style=\"font-size:13px;opacity:.65;margin-top:16px\">You can close this tab and return to PR Agent Control.</p></main></body></html>",
        html_escape(title),
        color,
        icon,
        html_escape(msg)
    )
}

pub(crate) fn done_page(ok: bool, msg: &str) -> String {
    result_page(
        ok,
        if ok { "GitHub App created" } else { "GitHub App was not created" },
        msg,
    )
}

pub(crate) fn respond(stream: &mut TcpStream, status: &str, body: &str) {
    let resp = format!(
        "HTTP/1.1 {}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\n\
Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\n{}",
        status,
        body.len(),
        body
    );
    let _ = stream.write_all(resp.as_bytes());
    let _ = stream.flush();
}

pub(crate) fn respond_redirect(stream: &mut TcpStream, location: &str) {
    let safe_loc = location.replace(['\r', '\n'], "");
    let body = format!(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Redirecting…</title></head>\
<body style=\"font-family:system-ui;padding:2rem\"><p>Redirecting to <a href=\"{0}\">GitHub</a>…</p></body></html>",
        html_escape(&safe_loc)
    );
    let resp = format!(
        "HTTP/1.1 302 Found\r\nLocation: {}\r\nContent-Type: text/html; charset=utf-8\r\n\
Content-Length: {}\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\n{}",
        safe_loc,
        body.len(),
        body
    );
    let _ = stream.write_all(resp.as_bytes());
    let _ = stream.flush();
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len()
                && bytes[i + 1].is_ascii_hexdigit()
                && bytes[i + 2].is_ascii_hexdigit() =>
            {
                let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("0");
                out.push(u8::from_str_radix(hex, 16).unwrap_or(0));
                i += 3;
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// `(path, query params)` of an HTTP request line such as `GET /callback?code=1 HTTP/1.1`.
pub fn parse_request_line(line: &str) -> Option<(String, Vec<(String, String)>)> {
    let mut parts = line.split_whitespace();
    if parts.next()? != "GET" {
        return None;
    }
    let target = parts.next()?;
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let params = query
        .split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            (percent_decode(k), percent_decode(v))
        })
        .collect();
    Some((path.to_string(), params))
}

pub(crate) fn random_state() -> Result<String, String> {
    sodiumoxide::init().map_err(|_| "failed to initialize libsodium".to_string())?;
    Ok(sodiumoxide::randombytes::randombytes(16)
        .iter()
        .map(|b| format!("{:02x}", b))
        .collect())
}

pub(crate) fn read_request_line(stream: &mut TcpStream) -> String {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let mut buf = [0u8; 4096];
    let n = stream.read(&mut buf).unwrap_or(0);
    let text = String::from_utf8_lossy(&buf[..n]);
    text.lines().next().unwrap_or("").to_string()
}

/// Serve `/start` until GitHub redirects to `/callback`; returns the one-time code.
fn wait_for_code(listener: &TcpListener, state: &str, start_html: &str) -> Result<String, String> {
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let deadline = Instant::now() + FLOW_TIMEOUT;
    while Instant::now() < deadline {
        let mut stream = match listener.accept() {
            Ok((s, _)) => s,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(200));
                continue;
            }
            Err(e) => return Err(e.to_string()),
        };
        let _ = stream.set_nonblocking(false);
        let line = read_request_line(&mut stream);
        let Some((path, params)) = parse_request_line(&line) else {
            respond(&mut stream, "405 Method Not Allowed", "");
            continue;
        };
        match path.as_str() {
            "/start" => respond(&mut stream, "200 OK", start_html),
            "/callback" => {
                let get = |k: &str| params.iter().find(|(pk, _)| pk == k).map(|(_, v)| v.clone());
                if get("state").as_deref() != Some(state) {
                    respond(&mut stream, "400 Bad Request", &done_page(false, "State mismatch; start again from the app."));
                    continue; // a stray/forged request must not end the flow
                }
                match get("code").filter(|c| !c.is_empty() && c.len() <= 200) {
                    Some(code) => {
                        respond(&mut stream, "200 OK", &done_page(true, "Finishing setup in PR Agent Control…"));
                        return Ok(code);
                    }
                    None => {
                        respond(&mut stream, "400 Bad Request", &done_page(false, "GitHub did not return a code."));
                        return Err("GitHub did not return a manifest code".into());
                    }
                }
            }
            _ => respond(&mut stream, "404 Not Found", ""),
        }
    }
    Err("timed out waiting for GitHub (10 minutes)".into())
}

/// PEM on one line with literal `\n`, as the backend's GITHUB_APP_PRIVATE_KEY expects.
pub fn pem_to_env(pem: &str) -> String {
    pem.trim().replace("\r\n", "\n").replace('\n', "\\n")
}

pub(crate) fn convert_code(code: &str) -> Result<serde_json::Value, String> {
    if !code.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-') {
        return Err("unexpected manifest code format".into());
    }
    let res = crate::gh_client()?
        .post(format!("https://api.github.com/app-manifests/{}/conversions", code))
        .header("Accept", "application/vnd.github+json")
        .send()
        .map_err(|e| e.to_string())?;
    let status = res.status();
    let body: serde_json::Value = res.json().map_err(|e| e.to_string())?;
    if !status.is_success() {
        let msg = body.get("message").and_then(|v| v.as_str()).unwrap_or("");
        return Err(format!("GitHub refused the App conversion (HTTP {}): {}", status, msg));
    }
    Ok(body)
}

/// Credentials the backend needs, as the `PUT /config/settings` body.
pub fn cloud_settings_body(id: i64, slug: &str, pem: &str, webhook_secret: Option<&str>) -> serde_json::Value {
    let mut values = serde_json::Map::new();
    values.insert("GITHUB_APP_ID".into(), id.to_string().into());
    if !slug.is_empty() {
        values.insert("GITHUB_APP_SLUG".into(), slug.into());
    }
    values.insert("GITHUB_APP_PRIVATE_KEY".into(), pem.trim().into());
    if let Some(secret) = webhook_secret.filter(|s| !s.is_empty()) {
        values.insert("GITHUB_APP_WEBHOOK_SECRET".into(), secret.into());
    }
    serde_json::json!({ "values": values })
}

/// Save the App credentials on the deployed backend (encrypted there). The secrets
/// go straight from this process to the backend; they never reach the webview.
pub(crate) fn push_to_cloud(settings_url: &str, api_token: Option<&str>, body: &serde_json::Value) -> Result<(), String> {
    // The body carries the private key, so refuse remote plaintext HTTP even when
    // no bearer token is sent (https and loopback http are fine).
    crate::require_secure_for_token(settings_url.trim(), true)?;
    crate::backend_mutate_blocking(
        "PUT".into(),
        settings_url.to_string(),
        "/config/settings".into(),
        Some(body.to_string()),
        api_token.map(str::to_string),
    )
    .map(|_| ())
}

fn create_blocking(
    backend_url: String,
    app_name: String,
    org: Option<String>,
    settings_url: Option<String>,
    api_token: Option<String>,
) -> Result<serde_json::Value, String> {
    if !valid_app_name(&app_name) {
        return Err("App name: 1-34 characters (letters, digits, space, - _ .)".into());
    }
    let org = org.map(|o| o.trim().to_string()).filter(|o| !o.is_empty());
    if let Some(o) = &org {
        if !valid_org(o) {
            return Err("invalid organization name".into());
        }
    }
    let webhook_base = if backend_url.trim().is_empty() {
        None
    } else {
        Some(public_webhook_base(&backend_url)?)
    };

    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    let state = random_state()?;
    let redirect = format!("http://127.0.0.1:{}/callback", port);
    let manifest = build_manifest(&app_name, webhook_base.as_deref(), &redirect);
    let action = match &org {
        Some(o) => format!("https://github.com/organizations/{}/settings/apps/new?state={}", o, state),
        None => format!("https://github.com/settings/apps/new?state={}", state),
    };
    let start_html = start_page(&action, &manifest);
    tauri_plugin_opener::open_url(format!("http://127.0.0.1:{}/start", port), None::<&str>)
        .map_err(|e| format!("could not open the browser: {}", e))?;

    let code = wait_for_code(&listener, &state, &start_html)?;
    drop(listener);
    let app = convert_code(&code)?;

    let id = app.get("id").and_then(|v| v.as_i64()).ok_or("GitHub response had no App id")?;
    let slug = app.get("slug").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let pem = app.get("pem").and_then(|v| v.as_str()).ok_or("GitHub response had no private key")?;
    let html_url = app.get("html_url").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let client_id = app.get("client_id").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    let client_secret = app.get("client_secret").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();

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

    // The App now exists and GitHub will not show the key again, so every sink is
    // best effort: a failing one must not lose the credentials for the others.
    let local = (|| -> Result<String, String> {
        let mut env_path = crate::save_backend_env_var_blocking("GITHUB_APP_ID".into(), id.to_string())?;
        if !slug.is_empty() {
            crate::save_backend_env_var_blocking("GITHUB_APP_SLUG".into(), slug.clone())?;
        }
        crate::save_backend_env_var_blocking("GITHUB_APP_PRIVATE_KEY".into(), pem_to_env(pem))?;
        if let Some(secret) = &webhook_secret {
            // Separate from GITHUB_WEBHOOK_SECRET so existing repo webhooks keep working.
            env_path = crate::save_backend_env_var_blocking("GITHUB_APP_WEBHOOK_SECRET".into(), secret.clone())?;
        }
        Ok(env_path)
    })();
    let cloud = match settings_url.as_deref().map(str::trim).filter(|u| !u.is_empty()) {
        Some(url) => Some(push_to_cloud(
            url,
            api_token.as_deref().map(str::trim).filter(|t| !t.is_empty()),
            &cloud_settings_body(id, &slug, pem, webhook_secret.as_deref()),
        )),
        None => None,
    };
    if local.is_err() && !matches!(cloud, Some(Ok(()))) {
        return Err(format!(
            "the App was created on GitHub (id {}) but its credentials could not be saved (local: {}; cloud: {}). \
Delete the App at {} and try again.",
            id,
            local.as_ref().err().cloned().unwrap_or_default(),
            match &cloud {
                Some(Err(e)) => e.clone(),
                _ => "not attempted".to_string(),
            },
            if html_url.is_empty() { "https://github.com/settings/apps".to_string() } else { html_url.clone() },
        ));
    }
    if !client_id.is_empty() && !client_secret.is_empty() {
        let _ = crate::oauth::save_github_oauth_client(
            &client_id,
            &client_secret,
            crate::oauth::GitHubOAuthClientMeta {
                is_app: true,
                slug: Some(&slug),
                html_url: Some(&html_url),
                install_url: Some(&install_url),
            },
        );
    }
    let (env_path, local_error) = match local {
        Ok(p) => (Some(p), None),
        Err(e) => (None, Some(e)),
    };
    Ok(serde_json::json!({
        "app_id": id,
        "slug": slug,
        "html_url": html_url,
        "install_url": install_url,
        "webhook_active": webhook_base.is_some(),
        "env_path": env_path,
        "local_error": local_error,
        "cloud_saved": matches!(cloud, Some(Ok(()))),
        "cloud_error": match &cloud { Some(Err(e)) => Some(e.clone()), _ => None },
    }))
}

/// Create the PR Agent GitHub App from a manifest and store its credentials on the
/// cloud backend (`settings_url`, encrypted there) and in the local `backend/.env`.
/// Returns only non-secret details (slug, id, URLs, where it was saved).
#[tauri::command]
pub async fn github_app_create(
    backend_url: String,
    app_name: String,
    org: Option<String>,
    settings_url: Option<String>,
    api_token: Option<String>,
) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || create_blocking(backend_url, app_name, org, settings_url, api_token))
        .await
        .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_has_permissions_events_and_hook() {
        let m = build_manifest("My Agent", Some("https://api.example.com"), "http://127.0.0.1:5/callback");
        assert_eq!(m["hook_attributes"]["url"], "https://api.example.com/webhooks/github");
        assert_eq!(m["hook_attributes"]["active"], true);
        assert_eq!(m["default_permissions"]["checks"], "write");
        assert_eq!(m["default_permissions"]["contents"], "write");
        assert_eq!(m["default_permissions"]["workflows"], "write");
        assert_eq!(m["default_permissions"]["secrets"], "write");
        assert_eq!(m["request_oauth_on_install"], true);
        assert_eq!(m["callback_urls"].as_array().unwrap().len(), GITHUB_OAUTH_PORTS.len());
        assert_eq!(m["public"], false);
        let events: Vec<&str> = m["default_events"].as_array().unwrap().iter().map(|v| v.as_str().unwrap()).collect();
        assert!(events.contains(&"pull_request_review_thread") && events.contains(&"check_suite"));
        let inactive = build_manifest("x", None, "http://127.0.0.1:5/callback");
        assert_eq!(inactive["hook_attributes"]["active"], false);
    }

    #[test]
    fn webhook_base_must_be_public_https() {
        assert!(public_webhook_base("https://api.example.com/").is_ok());
        assert!(public_webhook_base("http://api.example.com").is_err());
        assert!(public_webhook_base("https://localhost:8000").is_err());
        assert!(public_webhook_base("https://127.0.0.1").is_err());
        assert!(public_webhook_base("https://[::1]:8443").is_err());
        assert!(public_webhook_base("https://[fd00::1]").is_err());
        assert!(public_webhook_base("https://[::ffff:10.0.0.1]").is_err());
        for private in ["10.0.0.1", "172.16.4.2", "192.168.1.10", "169.254.1.1", "0.0.0.0", "100.64.0.1"] {
            assert!(public_webhook_base(&format!("https://{}", private)).is_err(), "{}", private);
        }
        assert!(public_webhook_base("https://203.0.113.9:8443/api").is_ok());
        assert!(public_webhook_base("https://[2001:db8::1]").is_ok());
        assert!(public_webhook_base("https://api.example.com?x=1").is_err());
        assert!(public_webhook_base("https://api.example.com/base#frag").is_err());
        assert!(public_webhook_base("https://user:pw@api.example.com").is_err());
        assert_eq!(public_webhook_base("https://api.example.com/base/").unwrap(), "https://api.example.com/base");
    }

    #[test]
    fn names_are_validated() {
        assert!(valid_app_name("Helios PR Agent"));
        assert!(!valid_app_name(""));
        assert!(!valid_app_name("a<script>"));
        assert!(!valid_app_name(&"x".repeat(35)));
        assert!(valid_org("my-org") && !valid_org("-bad") && !valid_org("a/b"));
    }

    #[test]
    fn request_line_parsing_and_escaping() {
        let (path, params) = parse_request_line("GET /callback?code=ab_1&state=f%30 HTTP/1.1").unwrap();
        assert_eq!(path, "/callback");
        assert_eq!(params, vec![("code".into(), "ab_1".into()), ("state".into(), "f0".into())]);
        assert!(parse_request_line("POST /callback HTTP/1.1").is_none());
        let (_, odd) = parse_request_line("GET /x?a=%zz&b=%4 HTTP/1.1").unwrap();
        assert_eq!(odd[0].1, "%zz");
        assert_eq!(odd[1].1, "%4");
        let page = start_page("https://github.com/settings/apps/new?state=s", &serde_json::json!({"name": "a\"<b>"}));
        assert!(page.contains("&quot;a\\&quot;&lt;b&gt;&quot;"));
        assert!(!page.contains("<b>"));
    }

    #[test]
    fn cloud_body_carries_credentials_for_the_backend() {
        // Placeholder text assembled at runtime so secret scanners do not mistake it for a key.
        let kind = "PRIVATE";
        let fake_pem = format!("-----BEGIN {kind} KEY-----\nplaceholder\n-----END {kind} KEY-----\n");
        let b = cloud_settings_body(42, "my-app", &fake_pem, Some("whsec"));
        let v = &b["values"];
        assert_eq!(v["GITHUB_APP_ID"], "42");
        assert_eq!(v["GITHUB_APP_SLUG"], "my-app");
        assert_eq!(v["GITHUB_APP_WEBHOOK_SECRET"], "whsec");
        assert!(v["GITHUB_APP_PRIVATE_KEY"].as_str().unwrap().ends_with("END PRIVATE KEY-----"));
        let bare = cloud_settings_body(1, "", "k", None);
        assert!(bare["values"].get("GITHUB_APP_SLUG").is_none() && bare["values"].get("GITHUB_APP_WEBHOOK_SECRET").is_none());
    }

    #[test]
    fn pem_is_stored_on_one_line() {
        assert_eq!(pem_to_env("-----BEGIN-----\r\nabc\ndef\n-----END-----\n"), "-----BEGIN-----\\nabc\\ndef\\n-----END-----");
    }
}
