"""Vercel Sandbox provider: ephemeral sandboxes for one-shot agent runs.

Best-effort implementation against the Vercel Sandbox API. Creates an
ephemeral sandbox, runs the agent steps, then tears it down. Requires
``VERCEL_TOKEN`` (``VERCEL_API_TOKEN`` also accepted). On missing token
or API error, degrades gracefully to review-only logic attributed to
``vercel_sandbox`` and never raises.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .safety import safe_git_ref, safe_label, safe_repo, shell_quote

log = logging.getLogger(__name__)

SANDBOX_API = "https://api.vercel.com/v1/sandboxes"
DIFF_TRUNCATE = 24000


class VercelSandboxProvider:
    provider_name = "vercel_sandbox"

    async def run(self, agent_config: dict, pr_context: dict) -> dict:
        logs: list[str] = []
        token = os.environ.get("VERCEL_TOKEN") or os.environ.get("VERCEL_API_TOKEN")
        team_id = os.environ.get("VERCEL_TEAM_ID")
        project_id = os.environ.get("VERCEL_PROJECT_ID")

        if not token:
            logs.append("vercel_sandbox: VERCEL_TOKEN missing — review fallback.")
            return await self._review_fallback(agent_config, pr_context, logs)

        import time
        started = time.monotonic()
        try:
            result = await self._run_ephemeral(
                token, team_id, project_id, agent_config, pr_context, logs
            )
            elapsed = time.monotonic() - started
            result["cpu_seconds"] = round(elapsed, 2)  # ~1 vCPU
            return result
        except Exception as exc:
            elapsed = time.monotonic() - started
            logs.append(f"vercel_sandbox: API error ({type(exc).__name__}) — review fallback.")
            return await self._review_fallback(agent_config, pr_context, logs)

    async def _run_ephemeral(self, token: str, team_id: str | None, project_id: str | None,
                             agent_config: dict, pr_context: dict,
                             logs: list[str]) -> dict:
        params: dict[str, str] = {}
        if team_id:
            params["teamId"] = team_id
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # Validate/build first so bad webhook input never leaves a sandbox behind.
        script = _build_command(agent_config, pr_context)

        sandbox_id = ""
        async with httpx.AsyncClient(timeout=60.0) as client:
            # 1. Create sandbox
            payload: dict[str, Any] = {"command": ["sleep", "600"]}
            if project_id:
                payload["projectId"] = project_id
            resp = await client.post(SANDBOX_API, headers=headers, params=params, json=payload)
            if resp.status_code not in (200, 201, 202):
                logs.append(f"vercel_sandbox: create failed HTTP {resp.status_code}.")
                raise RuntimeError(f"sandbox create HTTP {resp.status_code}")
            data = resp.json() if resp.content else {}
            sandbox_id = str(data.get("id") or data.get("sandboxId") or "")
            logs.append(f"vercel_sandbox: ephemeral sandbox created id={sandbox_id[:12] or '?'}.")

            # 2. Exec agent steps
            output = ""
            try:
                exec_resp = await client.post(
                    f"{SANDBOX_API}/{sandbox_id}/exec" if sandbox_id else f"{SANDBOX_API}/exec",
                    headers=headers,
                    params=params,
                    json={"command": ["sh", "-c", script]},
                )
                if exec_resp.status_code in (200, 201):
                    edata = exec_resp.json() if exec_resp.content else {}
                    output = str(edata.get("output") or edata.get("stdout") or edata)
                    logs.append("vercel_sandbox: exec ok.")
                else:
                    logs.append(f"vercel_sandbox: exec HTTP {exec_resp.status_code}.")
                    output = exec_resp.text
            except Exception as exc:
                logs.append(f"vercel_sandbox: exec error ({type(exc).__name__}).")

            # 3. Teardown (best-effort)
            if sandbox_id:
                try:
                    del_resp = await client.delete(
                        f"{SANDBOX_API}/{sandbox_id}", headers=headers, params=params
                    )
                    logs.append(f"vercel_sandbox: teardown HTTP {del_resp.status_code}.")
                except Exception as exc:
                    logs.append(f"vercel_sandbox: teardown error ({type(exc).__name__}).")

        logs.extend(output.splitlines()[:50])
        return {
            "provider": "vercel_sandbox",
            "status": "completed",
            "logs": logs,
            "diff": output[:DIFF_TRUNCATE],
            "cpu_seconds": 0.0,  # filled in by caller with elapsed
            "pr_comment_posted": False,
        }

    async def _review_fallback(self, agent_config: dict, pr_context: dict,
                               logs: list[str]) -> dict:
        from .providers_review import ReviewOnlyProvider

        review = ReviewOnlyProvider()
        result = await review.run(agent_config, pr_context)
        result["provider"] = "vercel_sandbox"
        # No sandbox was provisioned: never bill quota for the fallback.
        result["cpu_seconds"] = 0.0
        result["logs"] = logs + result.get("logs", [])
        if result.get("status") == "completed":
            result["status"] = "completed_fallback"
        return result


def _build_command(agent_config: dict, pr_context: dict) -> str:
    repo = safe_repo(pr_context.get("repo", ""))
    sha = safe_git_ref(pr_context.get("sha", ""))
    agent_id = safe_label(agent_config.get("id") or agent_config.get("name"))
    clone_url = f"https://github.com/{repo}.git"
    return (
        f"git clone --depth 50 {shell_quote(clone_url)} work 2>&1 | head -5; "
        f"cd work && git checkout {shell_quote(sha)} 2>&1 | head -5; "
        f"{{ npm run lint 2>&1 || npm test 2>&1; }} | head -40; "
        f"echo {shell_quote(f'agent={agent_id} done')}"
    )
