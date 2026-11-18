"""Review-only provider: zero-CPU GitHub diff review via OpenRouter.

No sandbox is provisioned. Fetches the PR diff from GitHub, asks the
LLM for a review, and posts it as an issue comment.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .safety import safe_label, safe_repo

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
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


class ReviewOnlyProvider:
    provider_name = "review_only"

    async def run(self, agent_config: dict, pr_context: dict) -> dict:
        logs: list[str] = []
        agent_name = agent_config.get("name", agent_config.get("id", "pr-agent"))
        repo = pr_context.get("repo", "")
        pr_number = pr_context.get("pr_number")
        system_prompt = agent_config.get("system_prompt", "You are a code reviewer.")
        title = pr_context.get("title", "")

        github_token = os.environ.get("GITHUB_TOKEN")
        llm_key = os.environ.get("OPENROUTER_API_KEY")
        llm_model = os.environ.get("LLM_MODEL", "thinkingmachines/inkling-2-small:free")

        if not github_token:
            logs.append("review_only: GITHUB_TOKEN missing — cannot fetch diff.")
            return _result(logs, "", "failed", False, agent_name)
        if not llm_key:
            logs.append("review_only: OPENROUTER_API_KEY missing — cannot call LLM.")
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
                if resp.status_code != 200:
                    logs.append(f"review_only: diff fetch failed HTTP {resp.status_code}.")
                    return _result(logs, "", "failed", False, agent_name)
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
            user_prompt = (
                f"Review pull request '{title}' in {repo} #{pr_number}.\n"
                f"sha={pr_context.get('sha', '')} action={pr_context.get('action', '')}\n\n"
                f"Diff:\n{truncated}\n\n"
                + (
                    "Note: parts of the diff were left out or truncated ("
                    + "; ".join(cut_notes[:10])
                    + "). Do not speculate about the omitted parts.\n\n"
                    if cut_notes else ""
                )
                + "Respond with a concise code review: summary, issues found "
                "(with file/line refs), and suggested fixes."
            )
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    OPENROUTER_URL,
                    headers={
                        "Authorization": f"Bearer {llm_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": llm_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
                if resp.status_code != 200:
                    logs.append(f"review_only: LLM call failed HTTP {resp.status_code}.")
                    return _result(logs, truncated, "failed", False, agent_name)
                data = resp.json()
                review = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
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
            body = f"{marker}\n##\U0001f916 {agent_name} (review_only, 0 CPU-sec)\n\n{review}"
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
