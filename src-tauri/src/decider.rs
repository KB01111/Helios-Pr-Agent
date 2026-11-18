//! Calibrated decision layer, policy v1 — Rust mirror of `backend/sandboxes/decider.py`.
//!
//! Both implementations are pinned to `backend/tests/decider_cases.json`, so the
//! desktop daemon and the cloud fallback reach the same decision for the same
//! facts. No ML runtime is linked in: an optional local classifier (any HTTP
//! server on 127.0.0.1) can nudge the *scoring* branch via
//! [`decide_blended`], but the guardrails below are applied first and are never
//! overridden by it:
//!   1. safety signals always escalate to a human,
//!   2. SKIP is only reachable through the docs-only / trusted-lockfile rules,
//!   3. oversized or truncated diffs escalate instead of being scored.

use serde::{Deserialize, Serialize};

pub const POLICY_VERSION: &str = "v1";
pub const DEFAULT_CONF_FLOOR: f64 = 0.55;

const CALIBRATION_TEMPERATURE: f64 = 1.5;
const MAX_FILES: usize = 100;
const MAX_CHURN: u64 = 2500;
const LOCKFILE_TRUSTED_MAX_CHURN: u64 = 400;

const FLAG_SECRET: &str = "secret_like";
const FLAG_SHELL_PIPE: &str = "shell_pipe";
const FLAG_UNTRUSTED_URL: &str = "untrusted_url";
const FLAG_NO_PATCH: &str = "no_patch";

#[derive(Serialize, Deserialize, Clone, Copy, PartialEq, Eq, Debug)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum DecisionKind {
    Skip,
    FastReview,
    SandboxTest,
    EscalateHuman,
}

#[derive(Serialize, Deserialize, Clone, Copy, PartialEq, Eq, Debug)]
#[serde(rename_all = "lowercase")]
pub enum Provider {
    None,
    Review,
    Vercel,
    Box,
}

#[derive(Serialize, Deserialize, Clone, Copy, PartialEq, Eq, Debug)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ReasonCode {
    DocsOnly,
    LockfileBump,
    StaticDiffOk,
    UiPreviewNeeded,
    RuntimeTestNeeded,
    LowConfidenceGrayZone,
    SafetyFlagged,
}

/// The wire schema shared with Python (`decision`, `confidence`, ...).
#[derive(Serialize, Deserialize, Clone, PartialEq, Debug)]
pub struct Decision {
    pub decision: DecisionKind,
    pub confidence: f64,
    pub provider: Provider,
    pub reason_code: ReasonCode,
    pub policy_version: String,
    pub latency_ms: u64,
}

#[derive(Serialize, Deserialize, Clone, Default, Debug)]
pub struct FactFile {
    pub path: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub additions: u64,
    #[serde(default)]
    pub deletions: u64,
    #[serde(default)]
    pub flags: Vec<String>,
}

/// Compact, secret-free PR summary produced by the backend (`extract_facts`).
#[derive(Serialize, Deserialize, Clone, Default, Debug)]
pub struct Facts {
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub files: Vec<FactFile>,
    #[serde(default)]
    pub truncated: bool,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Kind {
    Sensitive,
    Docs,
    Lock,
    Test,
    Ui,
    Config,
    Source,
    Other,
}

const LOCKFILES: &[&str] = &[
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "cargo.lock",
    "poetry.lock", "uv.lock", "pipfile.lock", "gemfile.lock", "go.sum", "composer.lock",
];
const DOC_EXTS: &[&str] = &[".md", ".mdx", ".rst", ".txt", ".adoc"];
const DOC_NAMES: &[&str] = &["license", "licence", "changelog", "authors", "notice", "contributing"];
const UI_EXTS: &[&str] = &[".html", ".htm", ".css", ".scss", ".sass", ".less", ".jsx", ".tsx", ".vue", ".svelte"];
const SRC_EXTS: &[&str] = &[
    ".py", ".rs", ".go", ".java", ".kt", ".js", ".mjs", ".cjs", ".ts", ".c", ".cc", ".cpp", ".h",
    ".hpp", ".cs", ".rb", ".php", ".swift", ".sh", ".sql",
];
const CONFIG_NAMES: &[&str] = &[
    "package.json", "cargo.toml", "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg",
    "tauri.conf.json", "makefile", "tsconfig.json",
];
const CONFIG_EXTS: &[&str] = &[".toml", ".yaml", ".yml", ".json", ".ini", ".cfg"];
const SOFT_SENSITIVE: &[&str] = &["secret", "credential", "crypto", "security"];

fn basename(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

/// Extension including the dot, taken from the basename (".env" counts as one).
fn ext_of(lower: &str) -> &str {
    let name = basename(lower);
    name.rfind('.').map(|i| &name[i..]).unwrap_or("")
}

fn is_lockfile(lower: &str) -> bool {
    LOCKFILES.contains(&basename(lower))
}

fn hard_sensitive(lower: &str) -> bool {
    let base = basename(lower);
    let in_dir = |dir: &str| lower.starts_with(dir) || lower.contains(&format!("/{}", dir));
    in_dir(".github/workflows/")
        || in_dir(".github/actions/")
        || base.starts_with("dockerfile")
        || (base.starts_with(".env")
            && !(base.starts_with(".env.example")
                || base.starts_with(".env.sample")
                || base.starts_with(".env.template")))
        || base == "codeowners"
        || base.starts_with("id_rsa")
        || [".pem", ".key", ".p12", ".pfx"].iter().any(|e| lower.ends_with(e))
}

fn is_test(lower: &str) -> bool {
    if lower.split('/').any(|seg| matches!(seg, "test" | "tests" | "__tests__" | "spec")) {
        return true;
    }
    if basename(lower).starts_with("test_") {
        return true;
    }
    // `[._-](test|spec)\.[a-z]+$`
    if let Some(dot) = lower.rfind('.') {
        let (stem, ext) = (&lower[..dot], &lower[dot + 1..]);
        if !ext.is_empty() && ext.bytes().all(|b| b.is_ascii_lowercase()) {
            return ["test", "spec"].iter().any(|word| {
                stem.strip_suffix(word)
                    .and_then(|rest| rest.chars().last())
                    .is_some_and(|c| matches!(c, '.' | '_' | '-'))
            });
        }
    }
    false
}

/// `auth` counts unless it is the start of "author…" (but "authoriz…" still counts).
fn mentions_auth(seg: &str) -> bool {
    seg.match_indices("auth").any(|(i, _)| {
        let rest = &seg[i + 4..];
        !(rest.starts_with("or") && !rest[2..].starts_with('i'))
    })
}

fn soft_sensitive(lower: &str) -> bool {
    lower
        .split('/')
        .any(|seg| mentions_auth(seg) || SOFT_SENSITIVE.iter().any(|w| seg.contains(w)))
}

fn kind_of(path: &str) -> Kind {
    let lower = path.to_lowercase();
    let lower = lower.as_str();
    if hard_sensitive(lower) {
        return Kind::Sensitive;
    }
    let ext = ext_of(lower);
    let base = basename(lower);
    if DOC_EXTS.contains(&ext) || lower.starts_with("docs/") || DOC_NAMES.contains(&base) {
        return Kind::Docs;
    }
    if is_lockfile(lower) {
        return Kind::Lock;
    }
    if is_test(lower) {
        return Kind::Test;
    }
    if soft_sensitive(lower) {
        return Kind::Sensitive;
    }
    if UI_EXTS.contains(&ext) {
        return Kind::Ui;
    }
    if CONFIG_NAMES.contains(&base) || base.starts_with("requirements") || CONFIG_EXTS.contains(&ext) {
        return Kind::Config;
    }
    if SRC_EXTS.contains(&ext) {
        return Kind::Source;
    }
    Kind::Other
}

fn round2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

fn make(decision: DecisionKind, confidence: f64, provider: Provider, reason: ReasonCode) -> Decision {
    Decision {
        decision,
        confidence: round2(confidence),
        provider,
        reason_code: reason,
        policy_version: POLICY_VERSION.to_string(),
        latency_ms: 0,
    }
}

/// Calibrated probabilities for `[static, ui, runtime]`.
fn class_probs(n_code: usize, churn: u64, n_ui: usize, n_test: usize, n_cfg: usize, n_src: usize) -> [f64; 3] {
    let flag = |b: bool| if b { 1.0 } else { 0.0 };
    let churn = churn as f64;
    let static_ = 3.4 - churn / 100.0 - 0.3 * n_code as f64 - 1.6 * flag(n_ui > 0) - 1.4 * flag(n_cfg > 0) - 0.9 * flag(n_test > 0);
    let ui = -2.0 + 5.0 * (n_ui as f64 / n_code as f64);
    let runtime = -0.2 + churn / 120.0 + 0.5 * n_src as f64 + 1.0 * flag(n_test > 0) + 1.2 * flag(n_cfg > 0);
    let exps = [static_, ui, runtime].map(|v| (v / CALIBRATION_TEMPERATURE).exp());
    let total: f64 = exps.iter().sum();
    exps.map(|e| e / total)
}

/// Apply policy v1 with rules only. `latency_ms` is measured by the caller.
/// (The daemon always goes through [`decide_blended`]; this is the plain entry point.)
#[allow(dead_code)]
pub fn decide(facts: &Facts, conf_floor: f64) -> Decision {
    decide_blended(facts, conf_floor, None, 0.0)
}

/// Like [`decide`], but the scoring branch may blend in `model` probabilities
/// (`[static, ui, runtime]`, already normalised) with weight `model_weight`.
/// Guardrail branches never consult the model.
pub fn decide_blended(facts: &Facts, conf_floor: f64, model: Option<[f64; 3]>, model_weight: f64) -> Decision {
    if facts.files.is_empty() {
        return make(DecisionKind::EscalateHuman, 0.3, Provider::None, ReasonCode::LowConfidenceGrayZone);
    }
    let kinds: Vec<Kind> = facts.files.iter().map(|f| kind_of(&f.path)).collect();
    let has_flag = |f: &FactFile, flag: &str| f.flags.iter().any(|x| x == flag);
    if kinds.contains(&Kind::Sensitive)
        || facts.files.iter().any(|f| has_flag(f, FLAG_SECRET) || has_flag(f, FLAG_SHELL_PIPE))
    {
        return make(DecisionKind::EscalateHuman, 0.95, Provider::None, ReasonCode::SafetyFlagged);
    }
    let churn_of = |f: &FactFile| f.additions + f.deletions;
    let total_churn: u64 = facts.files.iter().map(churn_of).sum();
    if facts.truncated || facts.files.len() > MAX_FILES || total_churn > MAX_CHURN {
        return make(DecisionKind::EscalateHuman, 0.4, Provider::None, ReasonCode::LowConfidenceGrayZone);
    }

    let code: Vec<(&FactFile, Kind)> = facts
        .files
        .iter()
        .zip(kinds.iter().copied())
        .filter(|(_, k)| !matches!(k, Kind::Docs | Kind::Lock))
        .collect();
    let locks: Vec<&FactFile> = facts
        .files
        .iter()
        .zip(kinds.iter())
        .filter(|(_, k)| **k == Kind::Lock)
        .map(|(f, _)| f)
        .collect();
    if code.is_empty() {
        if locks.is_empty() {
            return make(DecisionKind::Skip, 0.97, Provider::None, ReasonCode::DocsOnly);
        }
        let trusted = locks.iter().map(|f| churn_of(f)).sum::<u64>() <= LOCKFILE_TRUSTED_MAX_CHURN
            && !locks.iter().any(|f| has_flag(f, FLAG_UNTRUSTED_URL) || has_flag(f, FLAG_NO_PATCH));
        return if trusted {
            make(DecisionKind::Skip, 0.92, Provider::None, ReasonCode::LockfileBump)
        } else {
            make(DecisionKind::FastReview, 0.8, Provider::Review, ReasonCode::LockfileBump)
        };
    }

    let count = |kind: Kind| code.iter().filter(|(_, k)| *k == kind).count();
    let churn: u64 = code.iter().map(|(f, _)| churn_of(f)).sum();
    let mut probs = class_probs(code.len(), churn, count(Kind::Ui), count(Kind::Test), count(Kind::Config), count(Kind::Source));
    if let Some(m) = model {
        let w = model_weight.clamp(0.0, 1.0);
        for i in 0..3 {
            probs[i] = (1.0 - w) * probs[i] + w * m[i];
        }
    }
    let best = (0..3).fold(0, |best, i| if probs[i] > probs[best] { i } else { best });
    let conf = probs[best];
    if conf < conf_floor {
        return make(DecisionKind::EscalateHuman, conf, Provider::None, ReasonCode::LowConfidenceGrayZone);
    }
    match best {
        0 => make(DecisionKind::FastReview, conf, Provider::Review, ReasonCode::StaticDiffOk),
        1 => make(DecisionKind::SandboxTest, conf, Provider::Vercel, ReasonCode::UiPreviewNeeded),
        _ => make(DecisionKind::SandboxTest, conf, Provider::Box, ReasonCode::RuntimeTestNeeded),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Fixture expectations leave out `latency_ms` (it is a measurement, not policy).
    #[derive(Deserialize)]
    struct Expect {
        decision: DecisionKind,
        confidence: f64,
        provider: Provider,
        reason_code: ReasonCode,
        policy_version: String,
    }
    #[derive(Deserialize)]
    struct Case {
        name: String,
        facts: Facts,
        conf_floor: f64,
        expect: Expect,
    }
    #[derive(Deserialize)]
    struct Cases {
        policy_version: String,
        cases: Vec<Case>,
    }

    fn cases() -> Cases {
        serde_json::from_str(include_str!("../../backend/tests/decider_cases.json")).expect("fixtures parse")
    }

    fn file(path: &str, additions: u64) -> FactFile {
        FactFile { path: path.into(), additions, ..Default::default() }
    }

    #[test]
    fn matches_the_python_policy_on_every_shared_case() {
        let all = cases();
        assert_eq!(all.policy_version, POLICY_VERSION);
        assert!(all.cases.len() >= 20);
        for case in all.cases {
            let got = decide(&case.facts, case.conf_floor);
            assert_eq!(got.decision, case.expect.decision, "{}: decision", case.name);
            assert_eq!(got.reason_code, case.expect.reason_code, "{}: reason", case.name);
            assert_eq!(got.provider, case.expect.provider, "{}: provider", case.name);
            assert_eq!(got.policy_version, case.expect.policy_version, "{}: version", case.name);
            assert!(
                (got.confidence - case.expect.confidence).abs() < 0.011,
                "{}: confidence {} vs {}",
                case.name,
                got.confidence,
                case.expect.confidence
            );
        }
    }

    #[test]
    fn serialises_to_the_shared_wire_schema() {
        let d = decide(&Facts { files: vec![file("src/main.js", 3)], ..Default::default() }, DEFAULT_CONF_FLOOR);
        let v = serde_json::to_value(&d).unwrap();
        assert_eq!(v["decision"], "FAST_REVIEW");
        assert_eq!(v["provider"], "review");
        assert_eq!(v["reason_code"], "STATIC_DIFF_OK");
        assert_eq!(v["policy_version"], "v1");
        let mut keys: Vec<_> = v.as_object().unwrap().keys().cloned().collect();
        keys.sort();
        assert_eq!(keys, ["confidence", "decision", "latency_ms", "policy_version", "provider", "reason_code"]);
        let sandbox = decide(
            &Facts { files: vec![file("a.py", 80), file("tests/test_a.py", 60)], ..Default::default() },
            DEFAULT_CONF_FLOOR,
        );
        assert_eq!(serde_json::to_value(&sandbox).unwrap()["provider"], "box");
    }

    #[test]
    fn the_model_can_never_override_a_guardrail() {
        let favourite_static = Some([0.98, 0.01, 0.01]);
        for path in [".github/workflows/ci.yml", "Dockerfile", "backend/auth.py"] {
            let facts = Facts { files: vec![file(path, 1)], ..Default::default() };
            let d = decide_blended(&facts, DEFAULT_CONF_FLOOR, favourite_static, 1.0);
            assert_eq!(d.reason_code, ReasonCode::SafetyFlagged, "{path}");
        }
        let flagged = Facts {
            files: vec![FactFile { flags: vec!["secret_like".into()], ..file("a.py", 1) }],
            ..Default::default()
        };
        assert_eq!(decide_blended(&flagged, 0.55, favourite_static, 1.0).decision, DecisionKind::EscalateHuman);
        // scoring code can never be turned into SKIP by the model
        let code = Facts { files: vec![file("a.py", 5)], ..Default::default() };
        let d = decide_blended(&code, 0.55, Some([1.0, 0.0, 0.0]), 1.0);
        assert_ne!(d.decision, DecisionKind::Skip);
        let huge = Facts { files: vec![file("a.py", 5000)], ..Default::default() };
        assert_eq!(decide_blended(&huge, 0.55, favourite_static, 1.0).reason_code, ReasonCode::LowConfidenceGrayZone);
    }

    #[test]
    fn a_confident_model_moves_the_scoring_branch_and_a_split_model_hits_the_floor() {
        let facts = Facts { files: vec![file("src/main.js", 3)], ..Default::default() };
        let rules = decide(&facts, 0.55);
        assert_eq!(rules.decision, DecisionKind::FastReview);
        let runtime = decide_blended(&facts, 0.55, Some([0.02, 0.03, 0.95]), 1.0);
        assert_eq!((runtime.decision, runtime.provider), (DecisionKind::SandboxTest, Provider::Box));
        let split = decide_blended(&facts, 0.55, Some([0.34, 0.33, 0.33]), 1.0);
        assert_eq!(split.reason_code, ReasonCode::LowConfidenceGrayZone);
    }

    #[test]
    fn path_classification_edge_cases() {
        assert_eq!(kind_of("src/author_list.py"), Kind::Source);
        assert_eq!(kind_of("src/authorization.py"), Kind::Sensitive);
        assert_eq!(kind_of("src/Auth/login.py"), Kind::Sensitive);
        assert_eq!(kind_of(".env.example"), Kind::Other);
        assert_eq!(kind_of(".env"), Kind::Sensitive);
        assert_eq!(kind_of("web/app.test.js"), Kind::Test);
        assert_eq!(kind_of("web/contest.js"), Kind::Source);
        assert_eq!(kind_of("docs/auth.md"), Kind::Docs);
        assert_eq!(kind_of("a/.github/workflows/x.yml"), Kind::Sensitive);
        assert_eq!(kind_of("keys/server.PEM"), Kind::Sensitive);
    }
}
