"""SandboxRouter: quota-aware routing across sandbox providers."""
from __future__ import annotations

import datetime
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

LIMIT = 18000  # 5h CPU-seconds per provider per calendar month
FAILOVER_THRESHOLD = 0.9
FAILOVER_CUTOFF = LIMIT * FAILOVER_THRESHOLD  # 16200

MODE_TO_PROVIDER = {
    "interactive": "upstash_box",
    "ephemeral": "vercel_sandbox",
    "review_only": "review_only",
}


def _month_key(provider: str) -> str:
    ym = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
    return f"quota:{provider}:{ym}"


class SandboxRouter:
    """Route PR agent runs to the cheapest available sandbox.

    ``store`` may be:
    - an object with ``incr_quota(provider, seconds)`` / ``get_quota(provider)``,
    - a raw Redis client with ``incrbyfloat``/``get``,
    - or ``None`` (in-memory counters).
    """

    def __init__(self, store: Any | None = None, limit: int | None = None) -> None:
        self.store = store
        self._mem: dict[str, float] = {}
        self.limit = max(1, int(limit if limit is not None else LIMIT))
        self.failover_cutoff = self.limit * FAILOVER_THRESHOLD

    # -- quota helpers -------------------------------------------------
    def _get_usage(self, provider: str) -> float:
        if self.store is None:
            return self._mem.get(_month_key(provider), 0.0)
        if hasattr(self.store, "get_quota"):
            try:
                return float(self.store.get_quota(provider) or 0)
            except Exception:
                return 0.0
        try:  # raw Redis-like
            val = self.store.get(_month_key(provider))
            return float(val or 0)
        except Exception:
            return 0.0

    def _add_usage(self, provider: str, seconds: float) -> None:
        if self.store is None:
            key = _month_key(provider)
            self._mem[key] = self._mem.get(key, 0.0) + seconds
            return
        if hasattr(self.store, "incr_quota"):
            try:
                self.store.incr_quota(provider, seconds)
                return
            except Exception as exc:
                log.warning("quota incr_quota failed: %s", exc)
                return
        try:  # raw Redis-like
            if hasattr(self.store, "incrbyfloat"):
                self.store.incrbyfloat(_month_key(provider), seconds)
            elif hasattr(self.store, "incrby"):
                self.store.incrby(_month_key(provider), int(seconds))
        except Exception as exc:
            log.warning("quota redis incr failed: %s", exc)

    # -- main entry ----------------------------------------------------
    async def run_pr_agent(self, agent_config: dict, pr_context: dict) -> dict:
        from .providers_box import UpstashBoxProvider
        from .providers_review import ReviewOnlyProvider
        from .providers_vercel import VercelSandboxProvider

        logs: list[str] = []
        mode = (agent_config.get("runtime_mode") or "review_only").lower()
        requested = MODE_TO_PROVIDER.get(mode, "review_only")

        box_usage = self._get_usage("upstash_box")
        vercel_usage = self._get_usage("vercel_sandbox")

        # Failover: 90%+ usage reroutes to the other provider; both full -> review_only.
        provider = requested
        if requested == "upstash_box" and box_usage >= self.failover_cutoff:
            if vercel_usage < self.failover_cutoff:
                provider = "vercel_sandbox"
                logs.append(
                    f"router: upstash_box quota {box_usage:.0f}/{self.limit} >= 90% "
                    "-> failover to vercel_sandbox."
                )
            else:
                provider = "review_only"
                logs.append("router: both providers >= 90% -> fallback review_only (0 CPU).")
        elif requested == "vercel_sandbox" and vercel_usage >= self.failover_cutoff:
            if box_usage < self.failover_cutoff:
                provider = "upstash_box"
                logs.append(
                    f"router: vercel_sandbox quota {vercel_usage:.0f}/{self.limit} >= 90% "
                    "-> failover to upstash_box."
                )
            else:
                provider = "review_only"
                logs.append("router: both providers >= 90% -> fallback review_only (0 CPU).")
        else:
            logs.append(
                f"router: mode={mode} -> {provider} "
                f"(box {box_usage:.0f}/{self.limit}, vercel {vercel_usage:.0f}/{self.limit})."
            )

        instances = {
            "upstash_box": UpstashBoxProvider(),
            "vercel_sandbox": VercelSandboxProvider(),
            "review_only": ReviewOnlyProvider(),
        }

        # Dispatch with exception fallback to review_only.
        started = time.monotonic()
        try:
            result = await instances[provider].run(agent_config, pr_context)
            elapsed = time.monotonic() - started
            # Only bill real sandbox execution. Review-only fallbacks report
            # status "completed_fallback" and must stay at 0 CPU-seconds.
            if (
                not result.get("cpu_seconds")
                and provider != "review_only"
                and str(result.get("status", "")) == "completed"
            ):
                result["cpu_seconds"] = round(elapsed, 2)
        except Exception as exc:
            elapsed = time.monotonic() - started
            logs.append(f"router: {provider} raised {type(exc).__name__} — fallback review_only.")
            if provider == "review_only":
                return {
                    "provider": "review_only",
                    "status": "failed",
                    "logs": logs + [f"router: review_only failed: {exc}"],
                    "diff": "",
                    "cpu_seconds": 0.0,
                    "pr_comment_posted": False,
                }
            try:
                result = await instances["review_only"].run(agent_config, pr_context)
                result["provider"] = "review_only"
                result["cpu_seconds"] = 0.0
            except Exception as exc2:
                return {
                    "provider": "review_only",
                    "status": "failed",
                    "logs": logs + [f"router: fallback failed: {exc2}"],
                    "diff": "",
                    "cpu_seconds": 0.0,
                    "pr_comment_posted": False,
                }

        # Merge router logs, track quota (successful sandbox runs only).
        result["logs"] = logs + result.get("logs", [])
        cpu = float(result.get("cpu_seconds") or 0.0)
        _status = str(result.get("status", ""))
        if (
            result.get("provider") in ("upstash_box", "vercel_sandbox")
            and cpu > 0
            and _status == "completed"
        ):
            self._add_usage(result["provider"], cpu)

        result.setdefault("provider", provider)
        result.setdefault("status", "completed")
        result.setdefault("logs", logs)
        result.setdefault("diff", "")
        result.setdefault("cpu_seconds", 0.0)
        result.setdefault("pr_comment_posted", False)
        return result


async def run_pr_agent(
    agent_config: dict,
    pr_context: dict,
    store: Any | None = None,
    limit: int | None = None,
) -> dict:
    """Module-level convenience wrapper used by backend/main.py dispatch.

    Lazily binds to the shared ``store`` module when no explicit store is
    passed, so quota counters land in Redis (or the memory fallback).
    """
    if store is None:
        try:
            import store as _store  # cwd=backend
        except ImportError:
            try:
                from backend import store as _store  # repo-root layout
            except ImportError:
                _store = None
        store = _store
    return await SandboxRouter(store, limit=limit).run_pr_agent(agent_config, pr_context)
