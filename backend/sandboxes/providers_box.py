"""Upstash Box provider: warm-box reuse for interactive agent runs.

Best-effort implementation: tries the ``upstash_box`` Python SDK if
installed, otherwise falls back to a REST attempt via httpx. If no
``UPSTASH_BOX_API_KEY`` is configured or the Box API fails, it degrades
gracefully to review-only logic (marked as provider ``upstash_box`` with
an estimated CPU cost) and never raises.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from .safety import safe_git_ref, safe_label, safe_repo, shell_quote

log = logging.getLogger(__name__)

DIFF_TRUNCATE = 24000


def _repo_box_name(repo: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9-]", "-", repo).strip("-")[:48]
    return f"pr-agent-{safe}" if safe else "pr-agent-default"


class UpstashBoxProvider:
    provider_name = "upstash_box"

    async def run(self, agent_config: dict, pr_context: dict) -> dict:
        logs: list[str] = []
        agent_name = agent_config.get("name", agent_config.get("id", "pr-agent"))
        repo = pr_context.get("repo", "")
        box_name = _repo_box_name(repo)
        api_key = os.environ.get("UPSTASH_BOX_API_KEY")

        logs.append(f"upstash_box: reusing warm box '{box_name}' (1h idle free).")

        if not api_key:
            logs.append("upstash_box: UPSTASH_BOX_API_KEY missing — review fallback.")
            return await self._review_fallback(agent_config, pr_context, logs)

        # Try SDK first (optional dependency).
        try:
            from upstash_box import Box  # type: ignore

            logs.append("upstash_box: SDK available, executing via SDK.")
            result = await self._run_via_sdk(Box, api_key, box_name, agent_config, pr_context, logs)
            return result
        except ImportError:
            logs.append("upstash_box: SDK not installed, trying REST.")
        except Exception as exc:
            logs.append(f"upstash_box: SDK error ({type(exc).__name__}) — review fallback.")
            return await self._review_fallback(agent_config, pr_context, logs)

        try:
            result = await self._run_via_rest(api_key, box_name, agent_config, pr_context, logs)
            return result
        except Exception as exc:
            logs.append(f"upstash_box: REST error ({type(exc).__name__}) — review fallback.")
            return await self._review_fallback(agent_config, pr_context, logs)

    async def _run_via_sdk(self, Box: Any, api_key: str, box_name: str,
                           agent_config: dict, pr_context: dict,
                           logs: list[str]) -> dict:
        """Execute agent steps inside the warm box via the Python SDK."""
        try:
            box = Box(api_key=api_key, name=box_name)  # type: ignore[call-arg]
            script = _build_script(agent_config, pr_context)
            # SDK surface may vary; call generically and capture output.
            if hasattr(box, "exec"):
                out = box.exec(script)  # type: ignore[attr-defined]
                if hasattr(out, "__await__"):
                    out = await out
                text = str(getattr(out, "output", out))
            elif hasattr(box, "run"):
                out = box.run(script)  # type: ignore[attr-defined]
                if hasattr(out, "__await__"):
                    out = await out
                text = str(getattr(out, "output", out))
            else:
                raise RuntimeError("upstash_box SDK has no exec/run method")
            logs.append(f"upstash_box: exec ok ({len(text)} chars output).")
            logs.extend(text.splitlines()[:50])
            return {
                "provider": "upstash_box",
                "status": "completed",
                "logs": logs,
                "diff": text[:DIFF_TRUNCATE],
                "cpu_seconds": 60.0,
                "pr_comment_posted": False,
            }
        except Exception as exc:
            logs.append(f"upstash_box: SDK exec failed ({type(exc).__name__}).")
            raise

    async def _run_via_rest(self, api_key: str, box_name: str,
                            agent_config: dict, pr_context: dict,
                            logs: list[str]) -> dict:
        """Best-effort generic Box REST exec attempt."""
        script = _build_script(agent_config, pr_context)
        endpoints = [
            "https://api.upstash.com/v2/box/exec",
            "https://api.upstash.com/box/exec",
        ]
        last_status: int | None = None
        async with httpx.AsyncClient(timeout=120.0) as client:
            for url in endpoints:
                try:
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={"name": box_name, "command": script},
                    )
                    last_status = resp.status_code
                    if resp.status_code == 200:
                        text = resp.text
                        logs.append("upstash_box: REST exec ok.")
                        logs.extend(text.splitlines()[:50])
                        return {
                            "provider": "upstash_box",
                            "status": "completed",
                            "logs": logs,
                            "diff": text[:DIFF_TRUNCATE],
                            "cpu_seconds": 60.0,
                            "pr_comment_posted": False,
                        }
                    logs.append(f"upstash_box: REST {url} -> HTTP {resp.status_code}.")
                except Exception as exc:
                    logs.append(f"upstash_box: REST {url} error ({type(exc).__name__}).")
        raise RuntimeError(f"Box REST exec failed (last HTTP {last_status})")

    async def _review_fallback(self, agent_config: dict, pr_context: dict,
                               logs: list[str]) -> dict:
        """Run review-only logic but attribute it to upstash_box."""
        from .providers_review import ReviewOnlyProvider

        review = ReviewOnlyProvider()
        result = await review.run(agent_config, pr_context)
        result["provider"] = "upstash_box"
        # No sandbox was provisioned: never bill quota for the fallback.
        result["cpu_seconds"] = 0.0
        result["logs"] = logs + result.get("logs", [])
        if result.get("status") == "completed":
            result["status"] = "completed_fallback"
        return result


def _build_script(agent_config: dict, pr_context: dict) -> str:
    repo = safe_repo(pr_context.get("repo", ""))
    ref = safe_git_ref(pr_context.get("head_ref") or pr_context.get("sha") or "")
    prompt = (agent_config.get("system_prompt", "") or "")[:2000]
    agent_id = safe_label(agent_config.get("id") or agent_config.get("name"))
    echo_line = f"agent={agent_id} prompt={prompt[:200]}"
    clone_url = f"https://github.com/{repo}.git"
    return (
        f"git clone --depth 50 {shell_quote(clone_url)} work 2>&1; "
        f"cd work && git fetch origin {shell_quote(ref)} 2>&1 | head -20; "
        f"echo {shell_quote(echo_line)} | head -5"
    )
