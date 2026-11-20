"""Review-only provider: zero-CPU GitHub diff review via the configured LLM provider.

No sandbox is provisioned. Fetches the PR diff from GitHub, asks the
LLM for a review, and posts it as an issue comment.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

import httpx

from .safety import safe_label, safe_repo

try:  # cwd=backend
    import github_app
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import github_app  # type: ignore[no-redef]

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_URL = f"{OPENROUTER_API_BASE}/chat/completions"
DEFAULT_LLM_PROVIDER = "openrouter"
DEFAULT_LLM_MODEL = "thinkingmachines/inkling-2-small:free"
OPENROUTER_FALLBACK_MODEL = "openrouter/free"
DEFAULT_SYSTEM_PROMPT = "You are a senior code reviewer. Give concrete, accurate feedback grounded in the diff."
OPENCODE_SESSION_HEADER = "x-opencode-session"
_OPENROUTER_RETRY_STATUSES = frozenset({404, 429, 500, 502, 503, 504})
_LARGE_DIFF_MAX_PAGES = 3

# Command Code and OpenCode Go speak OpenAI Chat Completions; they are called
# directly (no LiteLLM), so the leading "openai/" route prefix is stripped.
LLM_PROVIDERS: dict[str, dict[str, Any]] = {
    "openrouter": {
        "label": "OpenRouter",
        "key_envs": ("OPENROUTER_API_KEY",),
        "api_base": OPENROUTER_API_BASE,
        "default_model": DEFAULT_LLM_MODEL,
    },
    "commandcode": {
        "label": "Command Code",
        "key_envs": ("COMMANDCODE_API_KEY", "CMD_API_KEY"),
        "api_base": "https://api.commandcode.ai/provider/v1",
        "default_model": "openai/deepseek/deepseek-v4-flash",
    },
    "opencode_go": {
        "label": "OpenCode Go",
        "key_envs": ("OPENCODE_GO_API_KEY", "OPENCODE_API_KEY"),
        "api_base": "https://opencode.ai/inference/go/openai/v1",
        "default_model": "openai/kimi-k3",
    },
}

DEFAULT_DIFF_BUDGET = 60000
# Hidden marker so a new push updates the previous review instead of stacking
# another comment on the PR.
# The marker is per agent so two agents on one repo keep separate reviews.
REVIEW_MARKER_PREFIX = "<!-- pr-agent-control:review:"


def review_marker(agent_key: object) -> str:
    return f"{REVIEW_MARKER_PREFIX}{safe_label(agent_key, 'pr-agent')} -->"

_NOISY_SUFFIXES = (
    ".lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
    "poetry.lock", "go.sum", ".min.js", ".min.css", ".map", ".svg", ".snap",
)


_TRUNC_MARK = "\n... [file truncated]\n"
_MAX_COMMENT_PAGES = 20


def diff_budget() -> int:
    try:
        return max(2000, int(os.environ.get("REVIEW_DIFF_MAX_CHARS", DEFAULT_DIFF_BUDGET)))
    except ValueError:
        return DEFAULT_DIFF_BUDGET


def _split_files(diff: str) -> list[tuple[str, str]]:
    """Split a unified diff into (path, chunk) pairs."""
    parts: list[tuple[str, str]] = []
    cur: list[str] = []
    path = ""
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if cur:
                parts.append((path, "".join(cur)))
            cur = [line]
            path = line.rsplit(" b/", 1)[-1].strip()
        else:
            cur.append(line)
    if cur:
        parts.append((path, "".join(cur)))
    return parts


def prepare_diff(diff: str, budget: int | None = None) -> tuple[str, list[str]]:
    """Fit a diff into ``budget`` characters without letting one huge or
    generated file starve the rest. Returns (text, notes about what was cut)."""
    budget = budget or diff_budget()
    notes: list[str] = []
    files = _split_files(diff)
    if not files:
        return diff, notes
    # Always drop generated/binary files, even from small diffs. If that would
    # leave nothing (a lockfile-only PR) review the original instead.
    kept: list[tuple[str, str]] = []
    for path, chunk in files:
        if path.endswith(_NOISY_SUFFIXES) or "Binary files" in chunk:
            notes.append(f"omitted generated/binary file {path}")
        else:
            kept.append((path, chunk))
    if not kept:
        return diff[:budget], [f"diff contains only generated/binary files; truncated to {budget} chars"]
    files = list(kept)  # original order; ``kept`` is re-sorted below
    if sum(len(chunk) for _, chunk in kept) <= budget:
        return "".join(chunk for _, chunk in kept), notes
    # Smallest files first: each gets its full text if it fits, and the
    # remaining budget is shared fairly between the larger ones.
    kept.sort(key=lambda item: len(item[1]))
    out: dict[str, str] = {}
    remaining = budget
    for i, (path, chunk) in enumerate(kept):
        share = remaining // (len(kept) - i)
        if len(chunk) <= share:
            out[path] = chunk
            remaining -= len(chunk)
        else:
            # The marker counts against this file's share so the total stays in budget.
            cut = max(0, share - len(_TRUNC_MARK))
            out[path] = chunk[:cut].rstrip() + _TRUNC_MARK
            remaining -= share
            notes.append(f"truncated {path} ({len(chunk)} -> {share} chars)")
    ordered = [out[path] for path, _ in files if path in out]
    return "".join(ordered), notes


def resolve_llm_provider(raw: str | None) -> str:
    """Known provider id from LLM_PROVIDER; anything else falls back to OpenRouter."""
    value = (raw or "").strip().lower()
    if not value:
        return DEFAULT_LLM_PROVIDER
    if value in LLM_PROVIDERS:
        return value
    log.warning("LLM_PROVIDER is not a supported provider; using %s", DEFAULT_LLM_PROVIDER)
    return DEFAULT_LLM_PROVIDER


def llm_key_for(provider: str) -> tuple[str, str]:
    """(key, env var it came from). The key is "" when unset; only the env name may be logged."""
    envs = LLM_PROVIDERS[provider]["key_envs"]
    for env in envs:
        value = os.environ.get(env, "").strip()
        if value:
            return value, env
    return "", envs[0]


def opencode_session_id(repo: str, pr_number: int) -> str:
    """Stable per-PR session id for OpenCode Go (ses_ + 24 hex chars)."""
    digest = hashlib.sha256(f"{repo}#{pr_number}".encode()).hexdigest()
    return f"ses_{digest[:24]}"


def normalize_openrouter_model(model: str) -> str:
    """Strip the LiteLLM ``openrouter/`` prefix when a vendor/model follows.

    OpenRouter router IDs such as ``openrouter/free`` and ``openrouter/auto``
    (single slash) keep their prefix.
    """
    m = (model or "").strip()
    if not m.startswith("openrouter/"):
        return m
    rest = m[len("openrouter/"):]
    return rest if "/" in rest else m


def _glob_matches(path: str, pattern: str) -> bool:
    """Match POSIX repo path against a glob pattern supporting ``**``."""
    p = path.strip().lstrip("./").lstrip("/")
    pat = pattern.strip().lstrip("./").lstrip("/")
    if not p or not pat:
        return False
    parts: list[str] = ["^"]
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pat[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pat[i]))
            i += 1
    parts.append("$")
    return bool(re.match("".join(parts), p))


def _sanitize_error_snippet(resp: Any, secret: str = "", max_chars: int = 180) -> str:
    """Extract a short, secret-free error snippet from an HTTP response."""
    raw = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                raw = str(err.get("message") or data.get("message") or "")
            else:
                raw = str(data.get("message") or err or "")
    except Exception:
        pass
    if not raw:
        raw = str(getattr(resp, "text", "") or "")
    flat = " ".join(raw.split())
    if secret and len(secret) >= 4:
        flat = flat.replace(secret, "[redacted]")
    flat = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", flat, flags=re.IGNORECASE)
    return flat[:max_chars]


def llm_chat_request(provider: str, model: str, repo: str, pr_number: int) -> tuple[str, dict[str, str], str]:
    """(chat completions URL, extra headers, model id to send) for one review call."""
    base = LLM_PROVIDERS[provider]["api_base"]
    if provider == "openrouter":
        return f"{base}/chat/completions", {}, normalize_openrouter_model(model)
    headers: dict[str, str] = {}
    if provider == "opencode_go":
        headers[OPENCODE_SESSION_HEADER] = opencode_session_id(repo, pr_number)
    send_model = model[len("openai/"):] if model.startswith("openai/") else model
    return f"{base}/chat/completions", headers, send_model


async def _fetch_pr_files_diff(client: Any, repo: str, pr_number: int, github_token: str) -> str | None:
    """Fallback when GitHub returns 406 for diffs larger than the raw diff limit."""
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
    }
    chunks: list[str] = []
    for page in range(1, _LARGE_DIFF_MAX_PAGES + 1):
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files",
            headers=headers,
            params={"per_page": 100, "page": page},
        )
        if resp.status_code != 200:
            return None
        batch = resp.json()
        if not isinstance(batch, list):
            return None
        for item in batch:
            if not isinstance(item, dict):
                continue
            fn = str(item.get("filename") or "").strip()
            if not fn:
                continue
            prev = str(item.get("previous_filename") or fn).strip()
            patch = item.get("patch")
            if isinstance(patch, str) and patch:
                chunks.append(f"diff --git a/{prev} b/{fn}\n--- a/{prev}\n+++ b/{fn}\n{patch.rstrip()}\n")
            else:
                status = str(item.get("status") or "modified")
                adds = int(item.get("additions") or 0)
                dels = int(item.get("deletions") or 0)
                chunks.append(
                    f"diff --git a/{prev} b/{fn}\n"
                    f"Binary files or large patch omitted ({status}, +{adds}/-{dels})\n"
                )
        if len(batch) < 100:
            break
    return "".join(chunks)


def _build_guidance(agent_config: dict, diff: str) -> str:
    """Build profile + matching path_instructions addendum for the prompt."""
    sections: list[str] = []
    profile = str(agent_config.get("profile") or "").strip().lower()
    if profile == "assertive":
        sections.append("Review profile (assertive): flag bugs, security issues, maintainability risks, and missing tests.")
    elif profile == "chill":
        sections.append("Review profile (chill): focus only on high-confidence bugs, security risks, and major issues; skip minor style nits.")

    raw_rules = agent_config.get("path_instructions") or []
    if isinstance(raw_rules, list) and raw_rules:
        changed_paths = [p for p, _ in _split_files(diff) if p]
        matched_rules: list[str] = []
        for rule in raw_rules:
            if not isinstance(rule, dict):
                continue
            pat = str(rule.get("path") or "").strip()
            instr = str(rule.get("instructions") or "").strip()
            if not pat or not instr:
                continue
            if not changed_paths or any(_glob_matches(p, pat) for p in changed_paths):
                matched_rules.append(f"- `{pat}`: {instr}")
        if matched_rules:
            sections.append("Path-specific repository instructions:\n" + "\n".join(matched_rules))
    return "\n\n".join(sections)


def _extract_choice_content(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices") or [{}]
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    msg = first.get("message") if isinstance(first.get("message"), dict) else {}
    return str(msg.get("content") or "").strip()


class ReviewOnlyProvider:
    provider_name = "review_only"

    async def run(self, agent_config: dict, pr_context: dict) -> dict:
        logs: list[str] = []
        agent_name = agent_config.get("name", agent_config.get("id", "pr-agent"))
        repo = pr_context.get("repo", "")
        pr_number = pr_context.get("pr_number")
        system_prompt = (agent_config.get("system_prompt") or "").strip() or DEFAULT_SYSTEM_PROMPT
        title = pr_context.get("title", "")

        llm_provider = resolve_llm_provider(os.environ.get("LLM_PROVIDER"))
        llm_key, llm_key_env = llm_key_for(llm_provider)
        configured_model = os.environ.get("LLM_MODEL", "").strip()
        if not configured_model or (
            llm_provider != "openrouter"
            and (configured_model == DEFAULT_LLM_MODEL or configured_model.startswith("openrouter/"))
        ):
            configured_model = LLM_PROVIDERS[llm_provider]["default_model"]
        llm_model = configured_model

        if github_app.auth_mode() == "none":
            logs.append("review_only: GITHUB_TOKEN (or a GitHub App) missing — cannot fetch diff.")
            return _result(logs, "", "failed", False, agent_name)
        if not llm_key:
            logs.append(f"review_only: {llm_key_env} missing — cannot call LLM.")
            return _result(logs, "", "failed", False, agent_name)
        if not repo or pr_number is None:
            logs.append("review_only: pr_context missing repo/pr_number.")
            return _result(logs, "", "failed", False, agent_name)
        try:
            repo = safe_repo(repo)
            pr_number = int(pr_number)
            if pr_number <= 0:
                raise ValueError
        except (ValueError, TypeError):
            logs.append("review_only: pr_context has an invalid repo/pr_number.")
            return _result(logs, "", "failed", False, agent_name)
        github_token, auth_kind = await github_app.token_for_repo(repo)
        if not github_token:
            logs.append("review_only: no GitHub credentials for this repo — cannot fetch diff.")
            return _result(logs, "", "failed", False, agent_name)
        logs.append(f"review_only: GitHub auth via {auth_kind}.")

        # 1. Fetch diff
        diff = ""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
                    headers={
                        "Accept": "application/vnd.github.diff",
                        "Authorization": f"Bearer {github_token}",
                    },
                )
                if resp.status_code == 406:
                    logs.append("review_only: diff exceeded GitHub 406 limit; falling back to PR files patches.")
                    fallback_diff = await _fetch_pr_files_diff(client, repo, pr_number, github_token)
                    if fallback_diff is None:
                        logs.append("review_only: PR files fallback failed.")
                        return _result(logs, "", "failed", False, agent_name)
                    diff = fallback_diff
                elif resp.status_code != 200:
                    logs.append(f"review_only: diff fetch failed HTTP {resp.status_code}.")
                    return _result(logs, "", "failed", False, agent_name)
                else:
                    diff = resp.text
            logs.append(f"review_only: fetched diff ({len(diff)} chars).")
        except Exception as exc:
            logs.append(f"review_only: diff fetch error: {type(exc).__name__}.")
            return _result(logs, "", "failed", False, agent_name)

        truncated, cut_notes = prepare_diff(diff)
        for note in cut_notes[:10]:
            logs.append(f"review_only: {note}.")

        # 2. LLM review
        review = ""
        try:
            guidance = _build_guidance(agent_config, truncated)
            user_prompt = (
                f"Review pull request '{title}' in {repo} #{pr_number}.\n"
                f"sha={pr_context.get('sha', '')} action={pr_context.get('action', '')}\n\n"
                + (f"{guidance}\n\n" if guidance else "")
                + f"Diff:\n{truncated}\n\n"
                + (
                    "Note: parts of the diff were left out or truncated ("
                    + "; ".join(cut_notes[:10])
                    + "). Do not speculate about the omitted parts.\n\n"
                    if cut_notes else ""
                )
                + "Respond with a concise code review: summary, issues found "
                "(with file/line refs), and suggested fixes."
            )
            llm_url, extra_headers, send_model = llm_chat_request(llm_provider, llm_model, repo, pr_number)
            req_headers = {
                "Authorization": f"Bearer {llm_key}",
                "Content-Type": "application/json",
                **extra_headers,
            }
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    llm_url,
                    headers=req_headers,
                    json={"model": send_model, "messages": messages},
                )
                if resp.status_code == 200:
                    review = _extract_choice_content(resp.json())
                if (
                    llm_provider == "openrouter"
                    and send_model != OPENROUTER_FALLBACK_MODEL
                    and (resp.status_code in _OPENROUTER_RETRY_STATUSES or (resp.status_code == 200 and not review))
                ):
                    reason = f"HTTP {resp.status_code}" if resp.status_code != 200 else "empty response"
                    logs.append(f"review_only: primary model '{send_model}' returned {reason}; retrying with {OPENROUTER_FALLBACK_MODEL}.")
                    resp = await client.post(
                        llm_url,
                        headers=req_headers,
                        json={"model": OPENROUTER_FALLBACK_MODEL, "messages": messages},
                    )
                    if resp.status_code == 200:
                        review = _extract_choice_content(resp.json())
                if resp.status_code != 200:
                    snippet = _sanitize_error_snippet(resp, llm_key)
                    logs.append(f"review_only: LLM call failed HTTP {resp.status_code}" + (f" ({snippet})" if snippet else "") + ".")
                    return _result(logs, truncated, "failed", False, agent_name)
            logs.append("review_only: LLM review generated.")
        except Exception as exc:
            logs.append(f"review_only: LLM error: {type(exc).__name__}.")
            return _result(logs, truncated, "failed", False, agent_name)

        if not review:
            logs.append("review_only: empty LLM response.")
            return _result(logs, truncated, "failed", False, agent_name)

        # 3. Post comment (update this agent's previous review when there is one)
        posted = False
        if await _head_moved(repo, pr_number, pr_context.get("sha"), github_token):
            logs.append("review_only: PR head moved on while reviewing; skipping the stale comment.")
            return _result(logs, truncated, "completed_no_comment", False, agent_name)
        try:
            marker = review_marker(agent_config.get("id") or agent_name)
            body = f"{marker}\n## \U0001f916 {agent_name} (review_only, 0 CPU-sec)\n\n{review}"
            headers = {
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
            }
            comments_url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
            async with httpx.AsyncClient(timeout=30.0) as client:
                existing_id = await _find_previous_review(client, comments_url, headers, marker)
                if existing_id:
                    resp = await client.patch(
                        f"{GITHUB_API}/repos/{repo}/issues/comments/{existing_id}",
                        headers=headers, json={"body": body},
                    )
                    verb = "updated"
                else:
                    resp = await client.post(comments_url, headers=headers, json={"body": body})
                    verb = "posted"
                posted = resp.status_code in (200, 201)
                logs.append(
                    f"review_only: comment {verb if posted else f'failed HTTP {resp.status_code}'}."
                )
        except Exception as exc:
            logs.append(f"review_only: comment post error: {type(exc).__name__}.")

        status = "completed" if posted else "completed_no_comment"
        return _result(logs, truncated, status, posted, agent_name)


async def _find_previous_review(client: Any, comments_url: str, headers: dict, marker: str) -> int | None:
    """Id of the newest comment carrying this agent's marker, or None (best effort)."""
    found: int | None = None
    try:
        # Comments come oldest first; walk every page (bounded) so a busy PR
        # whose previous review is past the first 100 comments is still found.
        for page in range(1, _MAX_COMMENT_PAGES + 1):
            resp = await client.get(comments_url, headers=headers, params={"per_page": 100, "page": page})
            if resp.status_code != 200:
                break
            batch = resp.json() or []
            for comment in batch:
                if isinstance(comment, dict) and marker in str(comment.get("body", "")):
                    found = int(comment["id"])
            if len(batch) < 100:
                break
    except Exception:
        pass
    return found


async def _head_moved(repo: str, pr_number: int, sha: Any, token: str) -> bool:
    """True when the PR head is no longer ``sha``: a newer push has its own
    review, so this older one must not overwrite it. Best effort."""
    if not sha:
        return False
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            )
        if resp.status_code != 200:
            return False
        head = (resp.json().get("head") or {}).get("sha")
        return bool(head) and str(head) != str(sha)
    except Exception:
        return False


def _result(logs: list[str], diff: str, status: str, posted: bool, agent: str) -> dict[str, Any]:
    return {
        "provider": "review_only",
        "status": status,
        "logs": logs,
        "diff": diff,
        "cpu_seconds": 0.0,
        "pr_comment_posted": posted,
    }
