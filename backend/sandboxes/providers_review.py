"""Review-only provider: zero-CPU GitHub diff review via OpenRouter.

No sandbox is provisioned. Fetches the PR diff from GitHub, asks the
LLM for a review, and posts it as an issue comment.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DIFF_TRUNCATE = 24000


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

        truncated = diff[:DIFF_TRUNCATE]
        if len(diff) > DIFF_TRUNCATE:
            logs.append(f"review_only: diff truncated to {DIFF_TRUNCATE} chars.")

        # 2. LLM review
        review = ""
        try:
            user_prompt = (
                f"Review pull request '{title}' in {repo} #{pr_number}.\n"
                f"sha={pr_context.get('sha', '')} action={pr_context.get('action', '')}\n\n"
                f"Diff:\n{truncated}\n\n"
                "Respond with a concise code review: summary, issues found "
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

        # 3. Post comment
        posted = False
        try:
            body = f"## \U0001f916 {agent_name} (review_only, 0 CPU-sec)\n\n{review}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
                    headers={
                        "Authorization": f"Bearer {github_token}",
                        "Accept": "application/vnd.github+json",
                    },
                    json={"body": body},
                )
                posted = resp.status_code in (200, 201)
                logs.append(
                    f"review_only: comment {'posted' if posted else f'failed HTTP {resp.status_code}'}."
                )
        except Exception as exc:
            logs.append(f"review_only: comment post error: {type(exc).__name__}.")

        status = "completed" if posted else "completed_no_comment"
        return _result(logs, truncated, status, posted, agent_name)


def _result(logs: list[str], diff: str, status: str, posted: bool, agent: str) -> dict[str, Any]:
    return {
        "provider": "review_only",
        "status": status,
        "logs": logs,
        "diff": diff,
        "cpu_seconds": 0.0,
        "pr_comment_posted": posted,
    }
