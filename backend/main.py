"""PR-Agent cloud backend — always-on event brain.

Deploy: `fastapi deploy` (exposes module-level `app`), or locally:
    uvicorn main:app --reload --port 8000

Responsibilities:
  * Receive GitHub webhooks, verify HMAC, debounce, match agent configs.
  * Queue runs and dispatch them in the background via sandboxes/router.py.
  * Serve agent CRUD, run history, SSE event stream, quota, health.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

try:
    # Works when cwd=backend (`uvicorn main:app`) or with PYTHONPATH=backend.
    import store
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import store as store  # type: ignore[no-redef]

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("pr-agent.backend")

# ---------------------------------------------------------------------------
# Config (env)
# ---------------------------------------------------------------------------

GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
LLM_MODEL = os.getenv("LLM_MODEL", "thinkingmachines/inkling-2-small:free")

# 5h of vCPU-seconds per provider per month.
QUOTA_LIMIT = 18_000

DEBOUNCE_TTL_S = 45

VALID_PR_ACTIONS = {"opened", "synchronize", "reopened", "labeled"}

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class AgentConfig(BaseModel):
    id: str = Field(default="", description="Unique id; auto-generated if empty")
    name: str = Field(default="Untitled agent")
    system_prompt: str = Field(default="")
    target_repos: list[str] = Field(default_factory=list)
    trigger: Literal["on_commit", "on_label", "manual"] = "on_commit"
    trigger_label: str | None = None
    runtime_mode: Literal["review_only", "interactive", "ephemeral"] = "review_only"


class PRContext(BaseModel):
    repo: str
    pr_number: int
    sha: str
    action: str
    label: str | None = None


class ApproveBody(BaseModel):
    approved_by: str | None = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="PR-Agent Cloud Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tauri desktop app has no fixed origin
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _verify_signature(raw: bytes, signature: str | None) -> bool:
    """Verify X-Hub-Signature-256. Dev mode (no secret) accepts with warning."""
    if not GITHUB_WEBHOOK_SECRET:
        log.warning("webhook: no GITHUB_WEBHOOK_SECRET set — accepting unsigned (dev mode)")
        return True
    if not signature:
        return False
    mac = hmac.new(GITHUB_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={mac}", signature)


def _agent_matches(agent: dict[str, Any], repo: str, action: str, label: str | None) -> bool:
    targets = agent.get("target_repos") or []
    if targets and repo not in targets and "*" not in targets:
        return False
    trigger = agent.get("trigger", "on_commit")
    if action == "labeled":
        # Only on_label agents whose label matches fire on label events.
        return trigger == "on_label" and (agent.get("trigger_label") or "") == (label or "")
    if action in ("opened", "synchronize", "reopened"):
        return trigger == "on_commit"
    return False


# ---------------------------------------------------------------------------
# Background dispatch (lazy router import to avoid startup cycles)
# ---------------------------------------------------------------------------


def dispatch_run(agent_cfg: dict[str, Any], pr: dict[str, Any], run_id: str) -> None:
    """Run the PR agent synchronously (called via BackgroundTasks)."""
    log.info("dispatch run=%s agent=%s repo=%s pr=%s", run_id, agent_cfg.get("id"), pr.get("repo"), pr.get("pr_number"))
    try:
        result: dict[str, Any] = {}
        try:
            # Prefer the unified SandboxRouter; tolerate both import layouts.
            router = None
            try:
                from sandboxes.router import SandboxRouter  # cwd=backend
            except ImportError:
                from backend.sandboxes.router import SandboxRouter  # type: ignore[no-redef]
            try:
                import store as _store
            except ImportError:
                from backend import store as _store  # type: ignore[no-redef]
            router = SandboxRouter(_store)
        except Exception as exc:
            log.warning("dispatch run=%s: SandboxRouter unavailable (%s); review_only fallback", run_id, exc)
            router = None

        if router is not None:
            maybe = router.run_pr_agent(agent_cfg, pr)
            if asyncio.iscoroutine(maybe):  # support async router impls
                maybe = asyncio.run(maybe)
            result = dict(maybe or {})
        else:
            result = {"status": "completed", "provider": "review_only", "logs": ["router missing; recorded as review_only"]}

        patch = {
            "status": result.get("status", "completed"),
            "provider": result.get("provider", agent_cfg.get("runtime_mode", "review_only")),
            "diff": result.get("diff"),
            "cpu_seconds": result.get("cpu_seconds", 0),
            "finished_at": _now_iso(),
            "logs": (store.rget_json(f"run:{run_id}") or {}).get("logs", []) + list(result.get("logs", [])),
        }
        cur = store.rget_json(f"run:{run_id}") or {}
        cur.update(patch)
        store.rset_json(f"run:{run_id}", cur)
        log.info("dispatch run=%s finished status=%s", run_id, patch["status"])
    except Exception as exc:  # never let background errors go silent
        log.exception("dispatch run=%s failed: %s", run_id, exc)
        try:
            cur = store.rget_json(f"run:{run_id}") or {}
            cur.update({"status": "failed", "finished_at": _now_iso()})
            logs = list(cur.get("logs", []))
            logs.append(f"ERROR: {exc}")
            cur["logs"] = logs
            store.rset_json(f"run:{run_id}", cur)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


@app.post("/webhooks/github", status_code=202)
async def github_webhook(request: Request, tasks: BackgroundTasks) -> JSONResponse:
    raw = await request.body()
    sig = request.headers.get("X-Hub-Signature-256")
    if not _verify_signature(raw, sig):
        raise HTTPException(status_code=401, detail="bad webhook signature")

    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON")

    sender = payload.get("sender") or {}
    if sender.get("type") == "Bot":
        return JSONResponse({"skipped": "bot"})

    pr = payload.get("pull_request") or {}
    action = payload.get("action", "")
    if not pr or action not in VALID_PR_ACTIONS:
        return JSONResponse({"skipped": action or "no-pull_request"})

    repo = ((payload.get("repository") or {}).get("full_name")) or ""
    pr_number = int(pr.get("number", 0))
    sha = (pr.get("head") or {}).get("sha", "")
    label = (payload.get("label") or {}).get("name") if action == "labeled" else None
    if not repo or not pr_number:
        raise HTTPException(status_code=400, detail="missing repo/pr_number")

    # Debounce: one trigger per PR per 45s.
    debounce_key = f"debounce:{repo}:{pr_number}"
    if not store.rset(debounce_key, "1", ex=DEBOUNCE_TTL_S, nx=True):
        return JSONResponse({"debounced": True}, status_code=202)

    # Load agent configs.
    agent_keys = store.rkeys("agent:*")
    agents: list[dict[str, Any]] = []
    for k in agent_keys:
        cfg = store.rget_json(k)
        if isinstance(cfg, dict):
            agents.append(cfg)
    matched = [a for a in agents if _agent_matches(a, repo, action, label)]
    log.info("webhook %s #%d action=%s: %d agents, %d matched", repo, pr_number, action, len(agents), len(matched))

    pr_ctx = PRContext(repo=repo, pr_number=pr_number, sha=sha, action=action, label=label)
    run_ids: list[str] = []
    for agent_cfg in matched:
        run_id = uuid.uuid4().hex[:12]
        record = {
            "run_id": run_id,
            "agent_id": agent_cfg.get("id"),
            "repo": repo,
            "pr_number": pr_number,
            "sha": sha,
            "action": action,
            "status": "queued",
            "created_at": _now_iso(),
            "logs": [],
        }
        store.rset_json(f"run:{run_id}", record)
        tasks.add_task(dispatch_run, agent_cfg, pr_ctx.model_dump(), run_id)
        run_ids.append(run_id)

    return JSONResponse({"accepted": True, "runs": run_ids}, status_code=202)


# ---------------------------------------------------------------------------
# Agent CRUD
# ---------------------------------------------------------------------------


@app.post("/agents")
def create_agent(agent: AgentConfig) -> dict[str, Any]:
    data = agent.model_dump()
    if not data["id"]:
        # Slug from name, else random.
        slug = "".join(c.lower() if c.isalnum() else "-" for c in data["name"]).strip("-") or "agent"
        data["id"] = f"{slug}-{uuid.uuid4().hex[:6]}"
    store.rset_json(f"agent:{data['id']}", data)
    return data


@app.get("/agents")
def list_agents() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k in store.rkeys("agent:*"):
        cfg = store.rget_json(k)
        if isinstance(cfg, dict):
            out.append(cfg)
    out.sort(key=lambda a: str(a.get("id")))
    return out


@app.get("/agents/{agent_id}")
def get_agent(agent_id: str) -> dict[str, Any]:
    cfg = store.rget_json(f"agent:{agent_id}")
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=404, detail="agent not found")
    return cfg


@app.put("/agents/{agent_id}")
def update_agent(agent_id: str, agent: AgentConfig) -> dict[str, Any]:
    if not isinstance(store.rget_json(f"agent:{agent_id}"), dict):
        raise HTTPException(status_code=404, detail="agent not found")
    data = agent.model_dump()
    data["id"] = agent_id  # URL id wins
    store.rset_json(f"agent:{agent_id}", data)
    return data


@app.delete("/agents/{agent_id}")
def delete_agent(agent_id: str) -> dict[str, Any]:
    if store.rdelete(f"agent:{agent_id}") == 0:
        # Memory fallback returns 1/0; SDK/REST path too. Missing -> 404.
        if not isinstance(store.rget_json(f"agent:{agent_id}"), dict):
            raise HTTPException(status_code=404, detail="agent not found")
    return {"deleted": agent_id}


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@app.get("/runs")
def list_runs(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k in store.rkeys("run:*"):
        rec = store.rget_json(k)
        if isinstance(rec, dict):
            out.append(rec)
    out.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
    return out[:limit]


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    rec = store.rget_json(f"run:{run_id}")
    if not isinstance(rec, dict):
        raise HTTPException(status_code=404, detail="run not found")
    return rec


@app.post("/runs/{run_id}/approve")
def approve_run(run_id: str, body: ApproveBody = Body(default_factory=ApproveBody)) -> dict[str, Any]:
    rec = store.rget_json(f"run:{run_id}")
    if not isinstance(rec, dict):
        raise HTTPException(status_code=404, detail="run not found")
    rec["status"] = "approved"
    rec["approved_by"] = body.approved_by
    rec["approved_at"] = _now_iso()
    store.rset_json(f"run:{run_id}", rec)
    return rec


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------


@app.get("/events/stream")
async def events_stream() -> StreamingResponse:
    async def gen():  # type: ignore[no-untyped-def]
        seen: set[str] = set()
        # Seed with existing run ids so we only push *new* ones.
        try:
            for k in await store.arkeys("run:*"):
                seen.add(k.split("run:", 1)[-1])
        except Exception:
            pass
        while True:
            try:
                keys = await store.arkeys("run:*")
                fresh: list[dict[str, Any]] = []
                for k in keys:
                    rid = k.split("run:", 1)[-1]
                    if rid in seen:
                        continue
                    seen.add(rid)
                    rec = await store.arget_json(k)
                    if isinstance(rec, dict):
                        fresh.append(rec)
                for rec in sorted(fresh, key=lambda r: str(r.get("created_at", ""))):
                    yield f"data: {json.dumps(rec, separators=(',', ':'))}\n\n"
                if not fresh:
                    yield ": ping\n\n"
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("sse poll failed: %s", exc)
                yield ": ping\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Quota / health
# ---------------------------------------------------------------------------


@app.get("/quota")
def quota() -> dict[str, Any]:
    ym = datetime.now(timezone.utc).strftime("%Y-%m")

    def _used(key: str) -> float:
        try:
            val = store.rget(key)
            return float(val) if val is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    ub = _used(f"quota:upstash_box:{ym}")
    vs = _used(f"quota:vercel_sandbox:{ym}")
    return {
        "upstash_box": {"used": ub, "limit": QUOTA_LIMIT, "remaining": max(0, QUOTA_LIMIT - ub)},
        "vercel_sandbox": {"used": vs, "limit": QUOTA_LIMIT, "remaining": max(0, QUOTA_LIMIT - vs)},
        "total_limit": QUOTA_LIMIT * 2,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "store": store.backend_mode(), "model": LLM_MODEL}


@app.get("/")
def root() -> dict[str, Any]:
    return {"ok": True, "service": "pr-agent-backend", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
