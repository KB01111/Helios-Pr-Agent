"""Calibrated decision layer (policy v1) — pure stdlib, no ML dependencies.

Decides what a pull request deserves *before* any sandbox is spent:

    SKIP            nothing worth reviewing (docs-only, trusted lockfile bump)
    FAST_REVIEW     zero-CPU diff review via the ``review`` provider
    SANDBOX_TEST    run it: ``vercel`` for UI previews, ``box`` for runtime tests
    ESCALATE_HUMAN  a person should look (safety flags, gray zone, huge diff)

The wire schema is shared with ``src-tauri/src/decider.rs``; both sides are
pinned to ``backend/tests/decider_cases.json`` so they cannot drift apart.

Inputs are *facts* — a compact, secret-free summary of the PR produced by
``extract_facts`` from GitHub's ``/pulls/{n}/files`` payload. The decider never
sees raw patches, so the Rust desktop daemon can run the same policy offline.

Guardrails (never relaxed by the optional local model on the desktop):
  1. Safety signals (workflow/Dockerfile/secret-ish paths, secret-like or
     ``curl | sh`` added lines) always escalate to a human.
  2. SKIP is only reachable through the docs-only / trusted-lockfile rules,
     never through scoring.
  3. Oversized or truncated diffs escalate instead of being guessed at.
  4. A decision can only *reduce* spend relative to an agent's configured
     ``runtime_mode`` (see ``effective_mode``); it never upgrades it.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any, Iterable

POLICY_VERSION = "v1"

SKIP = "SKIP"
FAST_REVIEW = "FAST_REVIEW"
SANDBOX_TEST = "SANDBOX_TEST"
ESCALATE_HUMAN = "ESCALATE_HUMAN"
DECISIONS = (SKIP, FAST_REVIEW, SANDBOX_TEST, ESCALATE_HUMAN)

PROVIDERS = ("none", "review", "vercel", "box")

REASON_CODES = (
    "DOCS_ONLY",
    "LOCKFILE_BUMP",
    "STATIC_DIFF_OK",
    "UI_PREVIEW_NEEDED",
    "RUNTIME_TEST_NEEDED",
    "LOW_CONFIDENCE_GRAY_ZONE",
    "SAFETY_FLAGGED",
)

# Calibration: temperature-scaled softmax over the three code-scoring classes.
# T > 1 flattens over-eager logits. Re-fit from /decisions/stats feedback once
# real human-override data exists; bump POLICY_VERSION when you do.
CALIBRATION_TEMPERATURE = 1.5
DEFAULT_CONF_FLOOR = 0.55  # below this the best guess is a gray zone -> human
DEFAULT_CONF_AUTO = 0.75  # autonomy "assist" acts alone at/above this

MAX_FILES = 100
MAX_CHURN = 2500
LOCKFILE_TRUSTED_MAX_CHURN = 400

# Flags attached to files by extract_facts.
FLAG_SECRET = "secret_like"
FLAG_SHELL_PIPE = "shell_pipe"
FLAG_UNTRUSTED_URL = "untrusted_url"
FLAG_NO_PATCH = "no_patch"
SAFETY_FLAGS = (FLAG_SECRET, FLAG_SHELL_PIPE)

_LOCKFILES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "cargo.lock", "poetry.lock", "uv.lock", "pipfile.lock", "gemfile.lock",
    "go.sum", "composer.lock",
}
_DOC_EXTS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
_DOC_NAMES = {"license", "licence", "changelog", "authors", "notice", "contributing"}
_UI_EXTS = {".html", ".htm", ".css", ".scss", ".sass", ".less", ".jsx", ".tsx", ".vue", ".svelte"}
_SRC_EXTS = {
    ".py", ".rs", ".go", ".java", ".kt", ".js", ".mjs", ".cjs", ".ts", ".c", ".cc",
    ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".sh", ".sql",
}
_CONFIG_NAMES = {
    "package.json", "cargo.toml", "pyproject.toml", "requirements.txt", "setup.py",
    "setup.cfg", "tauri.conf.json", "makefile", "tsconfig.json",
}
_CONFIG_EXTS = {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg"}
_HARD_SENSITIVE_RE = re.compile(
    r"(^|/)(\.github/(workflows|actions)/|dockerfile[^/]*$|\.env(?!\.(example|sample|template))[^/]*$"
    r"|codeowners$|id_rsa[^/]*$)|\.(pem|key|p12|pfx)$",
    re.IGNORECASE,
)
_SOFT_SENSITIVE_RE = re.compile(r"(^|/)[^/]*(auth(?!or(?!i))|secret|credential|crypto|security)[^/]*", re.IGNORECASE)
_TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]*$|[._-](test|spec)\.[a-z]+$", re.IGNORECASE)

_SECRET_RES = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----"),
)
_SHELL_PIPE_RE = re.compile(r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b|base64\s+-d[^\n|]*\|\s*(ba)?sh\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+)")
_TRUSTED_HOSTS = {
    "registry.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org",
    "static.crates.io", "index.crates.io", "proxy.golang.org", "rubygems.org",
    "repo.packagist.org", "github.com", "api.github.com",
}


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _ext(path: str) -> str:
    name = _basename(path)
    return name[name.rfind("."):].lower() if "." in name else ""


def _is_lockfile(path: str) -> bool:
    return _basename(path).lower() in _LOCKFILES


def extract_facts(title: str, files: Iterable[dict[str, Any]], truncated: bool = False) -> dict[str, Any]:
    """Reduce GitHub's PR-files payload to the compact facts the policy needs."""
    out: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("filename") or item.get("path") or "")
        if not path:
            continue
        patch = item.get("patch")
        flags: list[str] = []
        added = ""
        if isinstance(patch, str):
            added = "\n".join(
                line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")
            )
        elif patch is None:
            flags.append(FLAG_NO_PATCH)
        if added:
            if any(rx.search(added) for rx in _SECRET_RES):
                flags.append(FLAG_SECRET)
            if _SHELL_PIPE_RE.search(added):
                flags.append(FLAG_SHELL_PIPE)
            if _is_lockfile(path) and any(
                m.group(1).lower() not in _TRUSTED_HOSTS for m in _URL_RE.finditer(added)
            ):
                flags.append(FLAG_UNTRUSTED_URL)
        out.append({
            "path": path,
            "status": str(item.get("status") or "modified"),
            "additions": _as_int(item.get("additions")),
            "deletions": _as_int(item.get("deletions")),
            "flags": flags,
        })
    return {"title": str(title or "")[:300], "files": out, "truncated": bool(truncated)}


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _kind(path: str) -> str:
    if _HARD_SENSITIVE_RE.search(path):
        return "sensitive"
    base = _basename(path).lower()
    if _ext(path) in _DOC_EXTS or path.lower().startswith("docs/") or base in _DOC_NAMES:
        return "docs"
    if _is_lockfile(path):
        return "lock"
    if _TEST_RE.search(path):
        return "test"
    if _SOFT_SENSITIVE_RE.search(path):
        return "sensitive"
    if _ext(path) in _UI_EXTS:
        return "ui"
    if base in _CONFIG_NAMES or base.startswith("requirements") or _ext(path) in _CONFIG_EXTS:
        return "config"
    if _ext(path) in _SRC_EXTS:
        return "source"
    return "other"


def _round2(x: float) -> float:
    return round(x + 0.0, 2)


def _decision(decision: str, confidence: float, provider: str, reason: str) -> dict[str, Any]:
    return {
        "decision": decision,
        "confidence": _round2(confidence),
        "provider": provider,
        "reason_code": reason,
        "policy_version": POLICY_VERSION,
        "latency_ms": 0,
    }


def class_probs(n_code: int, churn: int, n_ui: int, n_test: int, n_cfg: int, n_src: int) -> list[float]:
    """Calibrated probabilities for [static, ui, runtime] (code-scoring branch)."""
    static = 3.4 - churn / 100 - 0.3 * n_code - 1.6 * (n_ui > 0) - 1.4 * (n_cfg > 0) - 0.9 * (n_test > 0)
    ui = -2.0 + 5.0 * (n_ui / n_code)
    runtime = -0.2 + churn / 120 + 0.5 * n_src + 1.0 * (n_test > 0) + 1.2 * (n_cfg > 0)
    exps = [math.exp(v / CALIBRATION_TEMPERATURE) for v in (static, ui, runtime)]
    total = sum(exps)
    return [e / total for e in exps]


def decide(facts: dict[str, Any], conf_floor: float = DEFAULT_CONF_FLOOR) -> dict[str, Any]:
    """Apply policy v1 to ``facts`` and return the shared decision schema."""
    started = time.perf_counter()
    result = _decide(facts, conf_floor)
    result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def _decide(facts: dict[str, Any], conf_floor: float) -> dict[str, Any]:
    files = [f for f in (facts.get("files") or []) if isinstance(f, dict) and f.get("path")]
    if not files:
        return _decision(ESCALATE_HUMAN, 0.3, "none", "LOW_CONFIDENCE_GRAY_ZONE")

    kinds = [_kind(str(f["path"])) for f in files]
    flags = {fl for f in files for fl in (f.get("flags") or [])}
    if "sensitive" in kinds or any(fl in flags for fl in SAFETY_FLAGS):
        return _decision(ESCALATE_HUMAN, 0.95, "none", "SAFETY_FLAGGED")

    churn_of = lambda f: _as_int(f.get("additions")) + _as_int(f.get("deletions"))  # noqa: E731
    total_churn = sum(churn_of(f) for f in files)
    if facts.get("truncated") or len(files) > MAX_FILES or total_churn > MAX_CHURN:
        return _decision(ESCALATE_HUMAN, 0.4, "none", "LOW_CONFIDENCE_GRAY_ZONE")

    code = [(f, k) for f, k in zip(files, kinds) if k not in ("docs", "lock")]
    locks = [f for f, k in zip(files, kinds) if k == "lock"]
    if not code:
        if not locks:
            return _decision(SKIP, 0.97, "none", "DOCS_ONLY")
        trusted = (
            sum(churn_of(f) for f in locks) <= LOCKFILE_TRUSTED_MAX_CHURN
            and not any(
                fl in (f.get("flags") or []) for f in locks for fl in (FLAG_UNTRUSTED_URL, FLAG_NO_PATCH)
            )
        )
        if trusted:
            return _decision(SKIP, 0.92, "none", "LOCKFILE_BUMP")
        return _decision(FAST_REVIEW, 0.8, "review", "LOCKFILE_BUMP")

    n_code = len(code)
    churn = sum(churn_of(f) for f, _ in code)
    count = lambda kind: sum(1 for _, k in code if k == kind)  # noqa: E731
    probs = class_probs(n_code, churn, count("ui"), count("test"), count("config"), count("source"))
    return _decision_from_probs(probs, conf_floor)


def _decision_from_probs(probs: list[float], conf_floor: float) -> dict[str, Any]:
    best = max(range(3), key=lambda i: (probs[i], -i))  # ties keep the earlier class
    conf = probs[best]
    if conf < conf_floor:
        return _decision(ESCALATE_HUMAN, conf, "none", "LOW_CONFIDENCE_GRAY_ZONE")
    if best == 0:
        return _decision(FAST_REVIEW, conf, "review", "STATIC_DIFF_OK")
    if best == 1:
        return _decision(SANDBOX_TEST, conf, "vercel", "UI_PREVIEW_NEEDED")
    return _decision(SANDBOX_TEST, conf, "box", "RUNTIME_TEST_NEEDED")


# ---------------------------------------------------------------------------
# Autonomy + spend guardrails
# ---------------------------------------------------------------------------

AUTONOMY_LEVELS = ("observe", "assist", "auto")
AUTO = "auto"
ASK = "ask"


def disposition(decision: dict[str, Any], level: str, conf_auto: float, conf_floor: float) -> str:
    """What to do with a decision under the user's autonomy level.

    ``observe`` is handled by callers (record only, legacy behaviour runs).
    Returns ``"auto"`` (act now) or ``"ask"`` (park in the escalation inbox).
    """
    kind = decision.get("decision")
    conf = float(decision.get("confidence") or 0.0)
    if kind == ESCALATE_HUMAN:
        return ASK
    if level == "assist":
        return AUTO if kind in (SKIP, FAST_REVIEW) and conf >= conf_auto else ASK
    if level == "auto":
        return AUTO if conf >= conf_floor else ASK
    return ASK


_PROVIDER_MODE = {"review": "review_only", "vercel": "ephemeral", "box": "interactive"}


def effective_mode(agent_mode: str, decision: dict[str, Any]) -> str | None:
    """The ``runtime_mode`` to run an agent with, or None to run nothing.

    Never exceeds the agent's configured mode: a ``review_only`` agent stays
    review-only even when the decider asks for a sandbox.
    """
    kind = decision.get("decision")
    if kind in (SKIP, ESCALATE_HUMAN):
        return None
    if kind == FAST_REVIEW or agent_mode == "review_only":
        return "review_only"
    return _PROVIDER_MODE.get(str(decision.get("provider")), agent_mode)


def validate_decision(obj: Any) -> dict[str, Any]:
    """Strictly validate a decision dict from an untrusted client (the desktop)."""
    if not isinstance(obj, dict):
        raise ValueError("decision must be an object")
    expected = {"decision", "confidence", "provider", "reason_code", "policy_version", "latency_ms"}
    if set(obj) != expected:
        raise ValueError("decision must have exactly: " + ", ".join(sorted(expected)))
    if obj["decision"] not in DECISIONS:
        raise ValueError("unknown decision")
    if obj["provider"] not in PROVIDERS:
        raise ValueError("unknown provider")
    if obj["reason_code"] not in REASON_CODES:
        raise ValueError("unknown reason_code")
    conf = obj["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= float(conf) <= 1.0:
        raise ValueError("confidence must be a number in [0, 1]")
    if not isinstance(obj["policy_version"], str) or not obj["policy_version"] or len(obj["policy_version"]) > 16:
        raise ValueError("invalid policy_version")
    lat = obj["latency_ms"]
    if isinstance(lat, bool) or not isinstance(lat, int) or lat < 0:
        raise ValueError("latency_ms must be a non-negative integer")
    expected_provider = {
        SKIP: "none", ESCALATE_HUMAN: "none", FAST_REVIEW: "review",
    }.get(obj["decision"])
    if expected_provider and obj["provider"] != expected_provider:
        raise ValueError("provider does not match decision")
    if obj["decision"] == SANDBOX_TEST and obj["provider"] not in ("vercel", "box"):
        raise ValueError("SANDBOX_TEST needs provider vercel or box")
    return {**obj, "confidence": _round2(float(conf))}
