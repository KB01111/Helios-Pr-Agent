//! Always-on background daemon.
//!
//! While the app is running (the tray keeps it alive after the window closes) a
//! worker thread:
//!   1. heartbeats to the backend so the cloud knows the desktop is awake and
//!      defers new PR events to it instead of spending cloud CPU,
//!   2. pulls deferred PR facts (`GET /decisions/pending`),
//!   3. decides each one locally with `decider` — rules, plus an optional
//!      classifier sidecar bound to loopback — and submits the result.
//!
//! If the desktop sleeps or goes offline the heartbeat lapses and the backend's
//! cloud decider (reached via the Cloudflare worker's cron sweep) takes over.
//! Idempotency lives in the backend (`(repo, pr_number, head_sha)` claims), so a
//! machine waking up late can never trigger a duplicate review or sandbox.
//!
//! The backend token is held in this process's memory only.

use crate::decider::{self, Facts};
use serde::Serialize;
use serde_json::json;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{Manager, State};

const MAX_EVENTS: usize = 40;
const SIDECAR_WEIGHT: f64 = 0.5;
const MAX_BACKOFF_SECS: u64 = 300;

fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

#[derive(Serialize, Clone, Debug)]
pub struct EventLine {
    pub at: u64,
    pub msg: String,
}

#[derive(Serialize, Clone, Default, Debug)]
pub struct Status {
    pub running: bool,
    pub backend_url: String,
    pub interval_secs: u64,
    /// "rules" or "sidecar" (rules + local classifier)
    pub model: String,
    pub started_at: Option<u64>,
    pub last_tick_at: Option<u64>,
    pub last_ok_at: Option<u64>,
    pub last_error: Option<String>,
    pub polls: u64,
    pub decided: u64,
    /// Decisions the cloud fallback (or a duplicate) had already settled.
    pub lost_races: u64,
    pub failures: u64,
    pub recent: VecDeque<EventLine>,
}

#[derive(Clone)]
struct Config {
    backend_url: String,
    api_token: Option<String>,
    interval: Duration,
    sidecar: Option<Sidecar>,
}

#[derive(Clone)]
struct Sidecar {
    url: String,
    client: reqwest::blocking::Client,
}

#[derive(Default)]
struct Inner {
    generation: AtomicU64,
    tray_ok: AtomicBool,
    status: Mutex<Status>,
    /// Serialises start/stop so a concurrent pair can never leave `running` set for a
    /// generation whose worker has already exited.
    lifecycle: Mutex<()>,
    config: Mutex<Option<Config>>,
    wake_lock: Mutex<()>,
    wake: Condvar,
}

/// Managed Tauri state.
#[derive(Default, Clone)]
pub struct DaemonState(Arc<Inner>);

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

impl Inner {
    fn is_current(&self, generation: u64) -> bool {
        self.generation.load(Ordering::SeqCst) == generation && lock(&self.status).running
    }

    fn log(&self, msg: impl Into<String>) {
        let mut s = lock(&self.status);
        s.recent.push_back(EventLine { at: now_secs(), msg: msg.into() });
        while s.recent.len() > MAX_EVENTS {
            s.recent.pop_front();
        }
    }

    /// Sleep up to `dur`, waking early when the daemon is stopped or replaced.
    fn sleep(&self, generation: u64, dur: Duration) {
        let deadline = Instant::now() + dur;
        let mut guard = lock(&self.wake_lock);
        while self.is_current(generation) {
            let now = Instant::now();
            if now >= deadline {
                break;
            }
            guard = match self.wake.wait_timeout(guard, deadline - now) {
                Ok((g, _)) => g,
                Err(e) => e.into_inner().0,
            };
        }
    }

    fn snapshot(&self) -> Status {
        lock(&self.status).clone()
    }

    fn stop(&self) -> Status {
        let was_running = {
            let _life = lock(&self.lifecycle);
            self.generation.fetch_add(1, Ordering::SeqCst);
            let was = std::mem::replace(&mut lock(&self.status).running, false);
            self.wake.notify_all();
            was
        };
        if was_running {
            self.log("daemon stopped");
            // Tell the backend right away so it stops deferring events to us.
            if let Some(cfg) = lock(&self.config).clone() {
                if let Err(e) = call(&cfg, "POST", "/daemon/offline", None) {
                    self.log(format!("offline notice failed (heartbeat will lapse on its own): {}", short(&e)));
                }
            }
        }
        self.snapshot()
    }
}

fn short(s: &str) -> String {
    s.chars().take(160).collect()
}

fn call(cfg: &Config, method: &str, path: &str, body: Option<serde_json::Value>) -> Result<serde_json::Value, String> {
    let raw = crate::backend_mutate_blocking(
        method.to_string(),
        cfg.backend_url.clone(),
        path.to_string(),
        body.map(|b| b.to_string()),
        cfg.api_token.clone(),
    )?;
    serde_json::from_str(&raw).map_err(|e| format!("backend returned non-JSON: {}", e))
}

/// Only a loopback classifier may ever see PR facts (e.g. a llama.cpp wrapper on 127.0.0.1:8080).
fn loopback_sidecar(url: &str) -> Result<String, String> {
    let u = url.trim().trim_end_matches('/');
    let rest = u.strip_prefix("http://").ok_or("sidecar URL must start with http://")?;
    if rest.contains('/') || rest.contains('@') {
        return Err("sidecar URL must be just http://127.0.0.1:PORT".into());
    }
    let host = if rest.starts_with('[') {
        rest.split(']').next().map(|h| format!("{}]", h)).unwrap_or_default()
    } else {
        rest.split(':').next().unwrap_or("").to_string()
    };
    if matches!(host.to_ascii_lowercase().as_str(), "127.0.0.1" | "localhost" | "[::1]") {
        Ok(u.to_string())
    } else {
        Err("sidecar must run on loopback (127.0.0.1 / localhost); PR facts are never sent elsewhere".into())
    }
}

/// `POST {sidecar}/classify` -> `{"probs": {"static": p, "ui": p, "runtime": p}}`.
/// Any problem returns `None` and the rules decide alone.
fn query_sidecar(sc: &Sidecar, facts: &Facts) -> Option<[f64; 3]> {
    let resp = sc
        .client
        .post(format!("{}/classify", sc.url))
        .json(&json!({ "facts": facts, "classes": ["static", "ui", "runtime"] }))
        .send()
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let v: serde_json::Value = resp.json().ok()?;
    let p = v.get("probs")?;
    let probs = [p.get("static")?.as_f64()?, p.get("ui")?.as_f64()?, p.get("runtime")?.as_f64()?];
    let sum: f64 = probs.iter().sum();
    if probs.iter().any(|x| !x.is_finite() || *x < 0.0) || sum <= 0.0 {
        return None;
    }
    Some(probs.map(|x| x / sum))
}

fn valid_id(id: &str) -> bool {
    id.len() == 16 && id.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

/// `generation == None` (tests) means "always current".
fn alive(inner: &Inner, generation: Option<u64>) -> bool {
    generation.map_or(true, |g| inner.is_current(g))
}

fn handle_item(
    inner: &Inner,
    cfg: &Config,
    generation: Option<u64>,
    floor: f64,
    item: &serde_json::Value,
) -> Result<(), String> {
    let id = item.get("id").and_then(|v| v.as_str()).unwrap_or("");
    if !valid_id(id) {
        return Err("pending item without a valid id".into());
    }
    let facts: Facts = serde_json::from_value(item.get("facts").cloned().unwrap_or_default())
        .map_err(|e| format!("bad facts for {}: {}", id, e))?;
    let started = Instant::now();
    let model = cfg.sidecar.as_ref().and_then(|sc| query_sidecar(sc, &facts));
    let mut decision = decider::decide_blended(&facts, floor, model, SIDECAR_WEIGHT);
    decision.latency_ms = started.elapsed().as_millis() as u64;
    // The (possibly slow) sidecar call may have outlived a stop or restart: don't submit then.
    if !alive(inner, generation) {
        return Ok(());
    }

    let repo = item.get("repo").and_then(|v| v.as_str()).unwrap_or("?");
    let pr = item.get("pr_number").and_then(|v| v.as_u64()).unwrap_or(0);
    let label = format!("{}#{} -> {:?}/{:?} ({:.2})", repo, pr, decision.decision, decision.reason_code, decision.confidence);
    match call(
        cfg,
        "POST",
        &format!("/decisions/{}/submit", id),
        Some(json!({ "decision": decision, "decided_by": "desktop" })),
    ) {
        Ok(_) => {
            lock(&inner.status).decided += 1;
            inner.log(format!("{} submitted in {} ms{}", label, decision.latency_ms, if model.is_some() { " [model]" } else { "" }));
            Ok(())
        }
        Err(e) if e.contains("HTTP 409") => {
            lock(&inner.status).lost_races += 1;
            inner.log(format!("{} already settled elsewhere (no duplicate work)", label));
            Ok(())
        }
        Err(e) => Err(format!("submit {}: {}", id, short(&e))),
    }
}

fn tick(inner: &Inner, cfg: &Config, generation: Option<u64>) -> Result<(), String> {
    call(
        cfg,
        "POST",
        "/daemon/heartbeat",
        Some(json!({
            "version": env!("CARGO_PKG_VERSION"),
            "interval_secs": cfg.interval.as_secs(),
            "model": if cfg.sidecar.is_some() { "sidecar" } else { "rules" },
        })),
    )?;
    let floor = call(cfg, "GET", "/autonomy", None)
        .ok()
        .and_then(|v| v.get("conf_floor").and_then(|f| f.as_f64()))
        .unwrap_or(decider::DEFAULT_CONF_FLOOR);
    let pending = call(cfg, "GET", "/decisions/pending?limit=20", None)?;
    let items = pending.get("items").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let mut first_error: Option<String> = None;
    for item in &items {
        if !alive(inner, generation) {
            break;
        }
        if let Err(e) = handle_item(inner, cfg, generation, floor, item) {
            lock(&inner.status).failures += 1;
            inner.log(format!("error: {}", e));
            first_error.get_or_insert(e);
        }
    }
    match first_error {
        Some(e) => Err(e),
        None => Ok(()),
    }
}

fn run_loop(inner: Arc<Inner>, generation: u64, cfg: Config) {
    let mut consecutive_errors: u32 = 0;
    while inner.is_current(generation) {
        let result = tick(&inner, &cfg, Some(generation));
        if !inner.is_current(generation) {
            break;
        }
        let wait = {
            let mut s = lock(&inner.status);
            s.polls += 1;
            s.last_tick_at = Some(now_secs());
            match &result {
                Ok(()) => {
                    consecutive_errors = 0;
                    s.last_ok_at = s.last_tick_at;
                    s.last_error = None;
                    cfg.interval
                }
                Err(e) => {
                    consecutive_errors = (consecutive_errors + 1).min(6);
                    s.last_error = Some(short(e));
                    Duration::from_secs((cfg.interval.as_secs() << consecutive_errors).min(MAX_BACKOFF_SECS))
                }
            }
        };
        if let Err(e) = &result {
            if consecutive_errors == 1 {
                inner.log(format!("tick failed: {} (backing off)", short(e)));
            }
        }
        inner.sleep(generation, wait);
    }
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[tauri::command]
pub async fn daemon_start(
    state: State<'_, DaemonState>,
    backend_url: String,
    api_token: Option<String>,
    interval_secs: Option<u64>,
    sidecar_url: Option<String>,
) -> Result<Status, String> {
    let inner = state.0.clone();
    tauri::async_runtime::spawn_blocking(move || start(inner, backend_url, api_token, interval_secs, sidecar_url))
        .await
        .map_err(|e| e.to_string())?
}

fn start(
    inner: Arc<Inner>,
    backend_url: String,
    api_token: Option<String>,
    interval_secs: Option<u64>,
    sidecar_url: Option<String>,
) -> Result<Status, String> {
    let backend_url = crate::backend_base(&backend_url)?;
    let token = api_token.map(|t| t.trim().to_string()).filter(|t| !t.is_empty());
    crate::require_secure_for_token(&backend_url, token.is_some())?;
    // Heartbeat TTL on the backend is 90 s, so never poll slower than every 60 s.
    let interval = Duration::from_secs(interval_secs.unwrap_or(30).clamp(5, 60));
    let sidecar = match sidecar_url.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        Some(u) => Some(Sidecar {
            url: loopback_sidecar(u)?,
            client: reqwest::blocking::Client::builder()
                .timeout(Duration::from_secs(3))
                // A redirect could carry the PR facts off loopback.
                .redirect(reqwest::redirect::Policy::none())
                .build()
                .map_err(|e| e.to_string())?,
        }),
        None => None,
    };
    let cfg = Config { backend_url: backend_url.clone(), api_token: token, interval, sidecar };

    // Held until the worker is spawned (or spawning failed) so stop() cannot interleave.
    let _life = lock(&inner.lifecycle);
    // A new generation supersedes any previous loop (restart with new settings).
    let generation = inner.generation.fetch_add(1, Ordering::SeqCst) + 1;
    let model = if cfg.sidecar.is_some() { "sidecar" } else { "rules" };
    *lock(&inner.config) = Some(cfg.clone());
    *lock(&inner.status) = Status {
        running: true,
        backend_url,
        interval_secs: interval.as_secs(),
        model: model.into(),
        started_at: Some(now_secs()),
        ..Status::default()
    };
    inner.wake.notify_all();
    inner.log(format!("daemon started (every {} s, {})", interval.as_secs(), model));

    let worker = inner.clone();
    std::thread::Builder::new()
        .name("pr-agent-daemon".into())
        .spawn(move || run_loop(worker, generation, cfg))
        .map_err(|e| {
            // No worker exists: do not leave the status claiming the daemon is running.
            lock(&inner.status).running = false;
            format!("could not start daemon thread: {}", e)
        })?;
    Ok(inner.snapshot())
}

#[tauri::command]
pub async fn daemon_stop(state: State<'_, DaemonState>) -> Result<Status, String> {
    let inner = state.0.clone();
    tauri::async_runtime::spawn_blocking(move || inner.stop()).await.map_err(|e| e.to_string())
}

#[tauri::command]
pub fn daemon_status(state: State<'_, DaemonState>) -> Status {
    state.0.snapshot()
}

// ---------------------------------------------------------------------------
// Tray + keep-alive-on-close
// ---------------------------------------------------------------------------

fn show_main(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// Tray icon so the daemon keeps running after the window is closed. Failure is
/// non-fatal (e.g. Linux without an app-indicator): the window then closes normally.
pub fn setup_tray(app: &tauri::App) -> tauri::Result<()> {
    use tauri::menu::{Menu, MenuItem};
    use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};

    let show = MenuItem::with_id(app, "show", "Show PR Agent Control", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Stop daemon and quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;
    let mut builder = TrayIconBuilder::with_id("main")
        .tooltip("PR Agent Control")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main(app),
            "quit" => {
                let inner = app.state::<DaemonState>().0.clone();
                let handle = app.clone();
                // Stop (and tell the backend) off the UI thread, then exit.
                std::thread::spawn(move || {
                    inner.stop();
                    handle.exit(0);
                });
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    app.state::<DaemonState>().0.tray_ok.store(true, Ordering::SeqCst);
    Ok(())
}

/// Hide instead of closing while the daemon is running and the tray can bring the window back.
pub fn on_window_event(window: &tauri::Window, event: &tauri::WindowEvent) {
    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
        let inner = &window.state::<DaemonState>().0;
        if inner.tray_ok.load(Ordering::SeqCst) && lock(&inner.status).running {
            api.prevent_close();
            let _ = window.hide();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    type Seen = Arc<Mutex<Vec<(String, String, String)>>>;

    /// Minimal one-shot-per-connection HTTP server: `handler(method, path) -> (status, json)`.
    fn mock_backend(
        requests: usize,
        handler: impl Fn(&str, &str) -> (u16, String) + Send + 'static,
    ) -> (String, Seen, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let seen: Seen = Arc::default();
        let log = seen.clone();
        let handle = std::thread::spawn(move || {
            for stream in listener.incoming().take(requests) {
                let mut stream = stream.unwrap();
                let mut buf = Vec::new();
                let mut chunk = [0u8; 4096];
                let (head_end, content_len) = loop {
                    let n = stream.read(&mut chunk).unwrap();
                    buf.extend_from_slice(&chunk[..n]);
                    if let Some(pos) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                        let head = String::from_utf8_lossy(&buf[..pos]).to_lowercase();
                        let len = head
                            .lines()
                            .find_map(|l| l.strip_prefix("content-length:").map(|v| v.trim().parse::<usize>().unwrap()))
                            .unwrap_or(0);
                        break (pos + 4, len);
                    }
                    assert!(n > 0, "connection closed early");
                };
                while buf.len() < head_end + content_len {
                    let n = stream.read(&mut chunk).unwrap();
                    buf.extend_from_slice(&chunk[..n]);
                }
                let head = String::from_utf8_lossy(&buf[..head_end]).to_string();
                let mut parts = head.lines().next().unwrap().split_whitespace();
                let (method, path) = (parts.next().unwrap().to_string(), parts.next().unwrap().to_string());
                let body = String::from_utf8_lossy(&buf[head_end..head_end + content_len]).to_string();
                let (status, reply) = handler(&method, &path);
                log.lock().unwrap().push((method, path, body));
                let resp = format!(
                    "HTTP/1.1 {} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    status,
                    reply.len(),
                    reply
                );
                stream.write_all(resp.as_bytes()).unwrap();
            }
        });
        (url, seen, handle)
    }

    const ID: &str = "0123456789abcdef";

    fn pending_json() -> String {
        json!({"items": [{
            "id": ID, "repo": "o/r", "pr_number": 7, "head_sha": "a".repeat(40),
            "facts": {"title": "t", "truncated": false, "files": [
                {"path": "src/main.js", "status": "modified", "additions": 2, "deletions": 1, "flags": []}
            ]}
        }]})
        .to_string()
    }

    fn cfg(url: &str) -> Config {
        Config { backend_url: url.into(), api_token: Some("tok".into()), interval: Duration::from_secs(30), sidecar: None }
    }

    fn router(submit_status: u16) -> impl Fn(&str, &str) -> (u16, String) + Send + 'static {
        move |method, path| match (method, path) {
            ("POST", "/daemon/heartbeat") => (200, r#"{"ok":true}"#.into()),
            ("GET", "/autonomy") => (200, r#"{"conf_floor":0.55,"conf_auto":0.75,"level":"assist"}"#.into()),
            ("GET", "/decisions/pending?limit=20") => (200, pending_json()),
            ("POST", p) if p == format!("/decisions/{}/submit", ID) => (submit_status, r#"{"accepted":true}"#.into()),
            _ => (404, r#"{"detail":"nope"}"#.into()),
        }
    }

    #[test]
    fn a_tick_heartbeats_decides_locally_and_submits_the_shared_schema() {
        let (url, seen, server) = mock_backend(4, router(200));
        let inner = Inner::default();
        tick(&inner, &cfg(&url), None).expect("tick succeeds");
        server.join().unwrap();

        let seen = seen.lock().unwrap();
        let paths: Vec<_> = seen.iter().map(|(m, p, _)| format!("{} {}", m, p)).collect();
        assert_eq!(
            paths,
            [
                "POST /daemon/heartbeat",
                "GET /autonomy",
                "GET /decisions/pending?limit=20",
                &format!("POST /decisions/{}/submit", ID),
            ]
        );
        let submitted: serde_json::Value = serde_json::from_str(&seen[3].2).unwrap();
        assert_eq!(submitted["decided_by"], "desktop");
        let d = &submitted["decision"];
        assert_eq!(d["decision"], "FAST_REVIEW");
        assert_eq!(d["provider"], "review");
        assert_eq!(d["reason_code"], "STATIC_DIFF_OK");
        assert_eq!(d["policy_version"], "v1");
        assert!(d["latency_ms"].as_u64().is_some());
        let s = inner.snapshot();
        assert_eq!((s.decided, s.lost_races, s.failures), (1, 0, 0));
    }

    #[test]
    fn losing_the_race_to_the_cloud_fallback_is_not_an_error() {
        let (url, _seen, server) = mock_backend(4, router(409));
        let inner = Inner::default();
        tick(&inner, &cfg(&url), None).expect("a 409 means someone else already acted");
        server.join().unwrap();
        let s = inner.snapshot();
        assert_eq!((s.decided, s.lost_races, s.failures), (0, 1, 0));
    }

    #[test]
    fn a_failed_submit_is_reported_and_counted() {
        let (url, _seen, server) = mock_backend(4, router(500));
        let inner = Inner::default();
        assert!(tick(&inner, &cfg(&url), None).is_err());
        server.join().unwrap();
        assert_eq!(inner.snapshot().failures, 1);
    }

    #[test]
    fn sidecar_must_be_loopback() {
        assert_eq!(loopback_sidecar("http://127.0.0.1:8080/").unwrap(), "http://127.0.0.1:8080");
        assert!(loopback_sidecar("http://localhost:8080").is_ok());
        assert!(loopback_sidecar("http://[::1]:8080").is_ok());
        for bad in [
            "https://127.0.0.1:8080",
            "http://example.com:8080",
            "http://127.0.0.1.evil.com:8080",
            "http://127.0.0.1:8080/x",
            "http://user@127.0.0.1:8080",
            "ftp://127.0.0.1",
        ] {
            assert!(loopback_sidecar(bad).is_err(), "{}", bad);
        }
    }

    #[test]
    fn the_sidecar_blends_in_and_garbage_falls_back_to_rules() {
        let sidecar = |body: &'static str| {
            let (url, _seen, server) = mock_backend(1, move |_, _| (200, body.to_string()));
            (Sidecar { url, client: reqwest::blocking::Client::new() }, server)
        };
        let facts = Facts {
            files: vec![decider::FactFile { path: "src/main.js".into(), additions: 3, ..Default::default() }],
            ..Default::default()
        };
        let (sc, server) = sidecar(r#"{"probs":{"static":1,"ui":1,"runtime":2}}"#);
        let probs = query_sidecar(&sc, &facts).expect("valid probs");
        server.join().unwrap();
        assert!((probs.iter().sum::<f64>() - 1.0).abs() < 1e-9 && (probs[2] - 0.5).abs() < 1e-9);
        for garbage in [r#"{"probs":{"static":-1,"ui":1,"runtime":1}}"#, r#"{"nope":1}"#, r#"not json"#] {
            let (sc, server) = sidecar(garbage);
            assert!(query_sidecar(&sc, &facts).is_none(), "{}", garbage);
            server.join().unwrap();
        }
    }

    #[test]
    fn stop_ends_the_loop_promptly_and_restart_supersedes_it() {
        let inner = Arc::new(Inner::default());
        // heartbeat + autonomy + pending (first tick), then the offline notice from stop()
        let (url, _seen, server) = mock_backend(4, move |m, p| match (m, p) {
            ("POST", "/daemon/offline") => (200, r#"{"ok":true}"#.into()),
            ("GET", "/decisions/pending?limit=20") => (200, r#"{"items":[]}"#.into()),
            _ => (200, r#"{"conf_floor":0.55}"#.into()),
        });
        let started = start(inner.clone(), url, Some("tok".into()), Some(60), None).expect("start");
        assert!(started.running && started.interval_secs == 60);
        // wait for the first tick (heartbeat, autonomy, pending)
        let deadline = Instant::now() + Duration::from_secs(10);
        while inner.snapshot().polls == 0 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert_eq!(inner.snapshot().polls, 1);
        let t = Instant::now();
        let stopped = inner.stop(); // sends /daemon/offline
        assert!(!stopped.running && t.elapsed() < Duration::from_secs(5));
        server.join().unwrap();
        assert!(!inner.is_current(inner.generation.load(Ordering::SeqCst)));
    }

    #[test]
    fn insecure_token_transport_is_refused() {
        let inner = Arc::new(Inner::default());
        let err = start(inner, "http://example.com".into(), Some("tok".into()), None, None).unwrap_err();
        assert!(err.contains("plain http"), "{}", err);
    }
}
