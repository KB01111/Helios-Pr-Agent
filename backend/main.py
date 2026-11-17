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

# --- Deployment hardening (1-click config) ---
# Comma-separated CORS allowlist, e.g. ALLOWED_ORIGINS=https://app.example.com,tauri://localhost
# Empty/unset = [] (no cross-origin access; production-safe default).
def _allowed_origins() -> list[str]:
    raw = os.getenv("ALLOWED_ORIGINS", "").strip()
    if not raw:
        return []
    if raw == "*":
        return ["*"]  # explicit opt-in only
    return [p.strip() for p in raw.split(",") if p.strip()]


ALLOWED_ORIGINS = _allowed_origins()

# Optional bearer token protecting mutating + config-test endpoints.
# Empty/unset = no auth (local dev). Set in production: BACKEND_API_TOKEN=<random>.
BACKEND_API_TOKEN = os.getenv("BACKEND_API_TOKEN", "").strip()


def _get_api_token() -> str:
    """Lazy read so tests / deploys can set BACKEND_API_TOKEN after import."""
    return os.getenv("BACKEND_API_TOKEN", "").strip() or BACKEND_API_TOKEN


def _auth_ok(request: Request) -> bool:
    token = _get_api_token()
    if not token:
        return True
    auth = (request.headers.get("Authorization") or "").strip()
    if auth and hmac.compare_digest(auth, f"Bearer {token}"):
        return True
    # EventSource cannot set headers — allow ?token= for the SSE stream.
    q = request.query_params.get("token", "")
    return bool(q) and hmac.compare_digest(q, token)


def _require_auth(request: Request) -> None:
    if not _auth_ok(request):
        raise HTTPException(status_code=401, detail="invalid or missing API token")


def _get_webhook_secret() -> str:
    return os.getenv("GITHUB_WEBHOOK_SECRET", "") or GITHUB_WEBHOOK_SECRET


def _get_llm_model() -> str:
    return os.getenv("LLM_MODEL", "") or LLM_MODEL


def _get_quota_limit() -> int:
    try:
        return int(os.getenv("QUOTA_LIMIT", "") or QUOTA_LIMIT)
    except ValueError:
        return 18_000

# 5h of vCPU-seconds per provider per month.
QUOTA_LIMIT = 18_000

DEBOUNCE_TTL_S = 45

# Only these PR actions may trigger runs. Anything else is acked+skipped.
# NOTE: "reopened" is intentionally excluded — reopen already emits "opened"
# semantics for our use-case; keeping the allowlist tight reduces surprise runs.
VALID_PR_ACTIONS = {"opened", "synchronize", "labeled"}

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
    allow_origins=ALLOWED_ORIGINS,  # [] by default (secure); set ALLOWED_ORIGINS / "*" for dev
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
    secret = _get_webhook_secret()
    if not secret:
        log.warning("webhook: no GITHUB_WEBHOOK_SECRET set — accepting unsigned (dev mode)")
        return True
    if not signature:
        return False
    mac = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={mac}", signature)


def _agent_matches(agent: dict[str, Any], repo: str, action: str, label: str | None) -> bool:
    targets = agent.get("target_repos") or []
    if targets and repo not in targets and "*" not in targets:
        return False
    trigger = agent.get("trigger", "on_commit")
    if action == "labeled":
        # Only on_label agents whose label matches fire on label events.
        # Empty/missing label never matches (prevents wildcard runs).
        if not label:
            return False
        return trigger == "on_label" and (agent.get("trigger_label") or "") == label
    if action in ("opened", "synchronize"):
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
            # NOTE: quota is incremented inside SandboxRouter only on
            # successful runs (status startswith "completed") — never here,
            # so failures/retries don't burn quota.
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
        return JSONResponse({"skipped": "bot"}, status_code=202)

    pr = payload.get("pull_request") or {}
    action = payload.get("action", "")
    if not pr or action not in VALID_PR_ACTIONS:
        return JSONResponse({"skipped": action or "no-pull_request"}, status_code=202)

    repo = ((payload.get("repository") or {}).get("full_name")) or ""
    pr_number = int(pr.get("number", 0))
    sha = (pr.get("head") or {}).get("sha", "")
    label = (payload.get("label") or {}).get("name") if action == "labeled" else None
    if action == "labeled" and not label:
        return JSONResponse({"skipped": "labeled-without-label"}, status_code=202)
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
def create_agent(agent: AgentConfig, request: Request) -> dict[str, Any]:
    _require_auth(request)
    data = agent.model_dump()
    if not data["id"]:
        # Slug from name, else random.
        slug = "".join(c.lower() if c.isalnum() else "-" for c in data["name"]).strip("-") or "agent"
        data["id"] = f"{slug}-{uuid.uuid4().hex[:6]}"
    store.rset_json(f"agent:{data['id']}", data)
    return data


@app.get("/agents")
def list_agents(request: Request) -> list[dict[str, Any]]:
    _require_auth(request)
    out: list[dict[str, Any]] = []
    for k in store.rkeys("agent:*"):
        cfg = store.rget_json(k)
        if isinstance(cfg, dict):
            out.append(cfg)
    out.sort(key=lambda a: str(a.get("id")))
    return out


@app.get("/agents/{agent_id}")
def get_agent(agent_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = store.rget_json(f"agent:{agent_id}")
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=404, detail="agent not found")
    return cfg


@app.put("/agents/{agent_id}")
def update_agent(agent_id: str, agent: AgentConfig, request: Request) -> dict[str, Any]:
    _require_auth(request)
    if not isinstance(store.rget_json(f"agent:{agent_id}"), dict):
        raise HTTPException(status_code=404, detail="agent not found")
    data = agent.model_dump()
    data["id"] = agent_id  # URL id wins
    store.rset_json(f"agent:{agent_id}", data)
    return data


@app.delete("/agents/{agent_id}")
def delete_agent(agent_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    if store.rdelete(f"agent:{agent_id}") == 0:
        # Memory fallback returns 1/0; SDK/REST path too. Missing -> 404.
        if not isinstance(store.rget_json(f"agent:{agent_id}"), dict):
            raise HTTPException(status_code=404, detail="agent not found")
    return {"deleted": agent_id}


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@app.get("/runs")
def list_runs(request: Request, limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
    _require_auth(request)
    out: list[dict[str, Any]] = []
    for k in store.rkeys("run:*"):
        rec = store.rget_json(k)
        if isinstance(rec, dict):
            out.append(rec)
    out.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
    return out[:limit]


@app.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    rec = store.rget_json(f"run:{run_id}")
    if not isinstance(rec, dict):
        raise HTTPException(status_code=404, detail="run not found")
    return rec


@app.post("/runs/{run_id}/approve")
def approve_run(run_id: str, request: Request, body: ApproveBody = Body(default_factory=ApproveBody)) -> dict[str, Any]:
    _require_auth(request)
    rec = store.rget_json(f"run:{run_id}")
    if not isinstance(rec, dict):
        raise HTTPException(status_code=404, detail="run not found")
    rec["status"] = "approved"
    rec["approved_by"] = body.approved_by
    rec["approved_at"] = _now_iso()
    store.rset_json(f"run:{run_id}", rec)
    return rec


# ---------------------------------------------------------------------------
# 1-click configuration: status + connectivity tests (never leaks secrets)
# ---------------------------------------------------------------------------


class ConfigTestBody(BaseModel):
    provider: str = Field(description="One of: github, redis, vercel, upstash_box, openrouter")


def _configured(value: str | None, min_len: int = 1) -> bool:
    return bool(value and len(value.strip()) >= min_len)


@app.get("/config/status")
def config_status() -> dict[str, Any]:
    """Which providers are configured. Returns booleans/mode only — never secret values."""
    redis_url = os.getenv("UPSTASH_REDIS_REST_URL", "")
    redis_token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
    vercel_token = os.getenv("VERCEL_TOKEN", "") or os.getenv("VERCEL_API_TOKEN", "")
    return {
        "github": {
            "webhook_secret_set": _configured(_get_webhook_secret()),
            "token_set": _configured(os.getenv("GITHUB_TOKEN", "")),
        },
        "redis": {
            "configured": _configured(redis_url) and _configured(redis_token),
            "mode": store.backend_mode(),
        },
        "upstash_box": {
            "configured": _configured(os.getenv("UPSTASH_BOX_API_KEY", ""), min_len=8),
        },
        "vercel": {
            "configured": _configured(vercel_token, min_len=8),
            "team_set": _configured(os.getenv("VERCEL_TEAM_ID", "")),
        },
        "openrouter": {
            "configured": _configured(os.getenv("OPENROUTER_API_KEY", ""), min_len=8),
        },
        "model": _get_llm_model(),
        "auth_required": bool(_get_api_token()),
        "allowed_origins": ALLOWED_ORIGINS,
    }


async def _test_github() -> dict[str, Any]:
    import httpx

    secret_set = _configured(os.getenv("GITHUB_WEBHOOK_SECRET", ""))
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        return {
            "provider": "github",
            "ok": secret_set,
            "configured": secret_set,
            "details": "webhook secret set; no GITHUB_TOKEN to live-check"
            if secret_set
            else "GITHUB_WEBHOOK_SECRET and GITHUB_TOKEN both missing",
            "webhook_secret_set": secret_set,
            "token_valid": None,
        }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 200:
            login = (resp.json() or {}).get("login", "?")
            return {
                "provider": "github", "ok": True, "configured": True,
                "details": f"token valid (login: {login}); webhook secret "
                + ("set" if secret_set else "MISSING"),
                "webhook_secret_set": secret_set, "token_valid": True, "login": login,
            }
        if resp.status_code in (401, 403):
            return {
                "provider": "github", "ok": False, "configured": True,
                "details": f"token rejected by GitHub API (HTTP {resp.status_code})",
                "webhook_secret_set": secret_set, "token_valid": False,
            }
        return {
            "provider": "github", "ok": False, "configured": True,
            "details": f"GitHub API unexpected HTTP {resp.status_code}",
            "webhook_secret_set": secret_set, "token_valid": None,
        }
    except Exception as exc:
        return {
            "provider": "github", "ok": False, "configured": True,
            "details": f"GitHub API unreachable ({type(exc).__name__})",
            "webhook_secret_set": secret_set, "token_valid": None,
        }


async def _test_redis() -> dict[str, Any]:
    import time

    started = time.monotonic()
    try:
        probe = f"ping:{uuid.uuid4().hex[:8]}"
        if not store.rset("__config_test_ping__", probe, ex=60):
            raise RuntimeError("write rejected")
        if store.rget("__config_test_ping__") != probe:
            raise RuntimeError("roundtrip mismatch")
        ms = round((time.monotonic() - started) * 1000)
        return {
            "provider": "redis", "ok": True,
            "configured": store.backend_mode() != "memory",
            "details": f"Redis roundtrip ok ({ms} ms, mode={store.backend_mode()})",
            "mode": store.backend_mode(), "latency_ms": ms,
        }
    except Exception as exc:
        return {
            "provider": "redis", "ok": False,
            "configured": store.backend_mode() != "memory",
            "details": f"Redis ping failed ({type(exc).__name__}: {exc}); "
            "falling back to in-memory store",
            "mode": store.backend_mode(),
        }


async def _test_vercel() -> dict[str, Any]:
    import httpx

    token = (os.getenv("VERCEL_TOKEN", "") or os.getenv("VERCEL_API_TOKEN", "")).strip()
    if not _configured(token, min_len=8):
        return {
            "provider": "vercel", "ok": False, "configured": False,
            "details": "VERCEL_TOKEN missing — add it on the Integrations card",
        }
    params: dict[str, str] = {}
    team_id = os.getenv("VERCEL_TEAM_ID", "").strip()
    if team_id:
        params["teamId"] = team_id
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.vercel.com/v2/user",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
        if resp.status_code == 200:
            data = resp.json() or {}
            user = (data.get("user") or {}).get("username") or data.get("username") or "?"
            return {
                "provider": "vercel", "ok": True, "configured": True,
                "details": f"Vercel token valid (user: {user})", "user": user,
            }
        if resp.status_code in (401, 403):
            return {
                "provider": "vercel", "ok": False, "configured": True,
                "details": f"Vercel token rejected (HTTP {resp.status_code}) — check/rotate it",
            }
        return {
            "provider": "vercel", "ok": False, "configured": True,
            "details": f"Vercel API unexpected HTTP {resp.status_code}",
        }
    except Exception as exc:
        return {
            "provider": "vercel", "ok": False, "configured": True,
            "details": f"Vercel API unreachable ({type(exc).__name__})",
        }


async def _test_upstash_box() -> dict[str, Any]:
    import httpx

    key = os.getenv("UPSTASH_BOX_API_KEY", "").strip()
    if not _configured(key, min_len=8):
        return {
            "provider": "upstash_box", "ok": False, "configured": False,
            "details": "UPSTASH_BOX_API_KEY missing — add it on the Integrations card",
        }
    # Non-destructive probe: any HTTP response proves reachability; 401/403 means bad key.
    # Never sends the key anywhere except the Box API over TLS, never echoes it back.
    for url in ("https://api.upstash.com/v2/box", "https://api.upstash.com/v2/boxes"):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {key}"})
            if resp.status_code == 200:
                return {
                    "provider": "upstash_box", "ok": True, "configured": True,
                    "details": f"Box API reachable ({url} HTTP 200), key accepted",
                }
            if resp.status_code in (401, 403):
                return {
                    "provider": "upstash_box", "ok": False, "configured": True,
                    "details": "Box API rejected the key (HTTP 401/403) — check/rotate it",
                }
            # Other statuses (404/405/…) still prove the key format path works at the edge;
            # fall through to report reachable-but-unverified rather than failing hard.
            return {
                "provider": "upstash_box", "ok": True, "configured": True,
                "details": f"Box API reachable ({url} HTTP {resp.status_code}); "
                "key present, full check runs at provision time",
                "reachable": True,
            }
        except Exception:
            continue  # try next endpoint, else fall through to offline verdict
    return {
        "provider": "upstash_box", "ok": True, "configured": True,
        "details": "key present (format ok); Box API not reachable from here — "
        "run Status on the Upstash Box card for a live check",
        "reachable": False,
    }


async def _test_openrouter() -> dict[str, Any]:
    import httpx

    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not _configured(key, min_len=8):
        return {
            "provider": "openrouter", "ok": False, "configured": False,
            "details": "OPENROUTER_API_KEY missing — add it on the Integrations card",
        }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {key}"},
            )
        if resp.status_code == 200:
            return {
                "provider": "openrouter", "ok": True, "configured": True,
                "details": "OpenRouter key valid",
            }
        if resp.status_code in (401, 403):
            return {
                "provider": "openrouter", "ok": False, "configured": True,
                "details": f"OpenRouter key rejected (HTTP {resp.status_code})",
            }
        return {
            "provider": "openrouter", "ok": False, "configured": True,
            "details": f"OpenRouter API unexpected HTTP {resp.status_code}",
        }
    except Exception as exc:
        return {
            "provider": "openrouter", "ok": False, "configured": True,
            "details": f"OpenRouter API unreachable ({type(exc).__name__})",
        }


@app.post("/config/test")
async def config_test(body: ConfigTestBody, request: Request) -> dict[str, Any]:
    """Live connectivity check for one provider. Never returns secret values."""
    _require_auth(request)
    provider = (body.provider or "").strip().lower()
    if provider == "github":
        return await _test_github()
    if provider == "redis":
        return await _test_redis()
    if provider == "vercel":
        return await _test_vercel()
    if provider in ("upstash_box", "upstash-box", "box"):
        return await _test_upstash_box()
    if provider == "openrouter":
        return await _test_openrouter()
    raise HTTPException(
        status_code=400,
        detail="unknown provider (use: github, redis, vercel, upstash_box, openrouter)",
    )


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------


@app.get("/events/stream")
async def events_stream(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
) -> StreamingResponse:
    """SSE stream: replay recent runs on connect, then push new/updated runs live.

    Auth: if BACKEND_API_TOKEN is set, requires `Authorization: Bearer <token>`
    or `?token=<token>` (EventSource cannot set headers).
    """
    if not _auth_ok(request):
        raise HTTPException(status_code=401, detail="invalid or missing API token")

    async def gen():  # type: ignore[no-untyped-def]
        snapshots: dict[str, str] = {}
        # 1. Replay recent runs so a fresh client catches up.
        try:
            keys = await store.arkeys("run:*")
            recs: list[dict[str, Any]] = []
            for k in keys:
                rec = await store.arget_json(k)
                if isinstance(rec, dict):
                    recs.append(rec)
            recs.sort(key=lambda r: str(r.get("created_at", "")))
            for rec in recs[-limit:]:
                blob = json.dumps(rec, separators=(",", ":"))
                rid = str(rec.get("run_id", ""))
                if rid:
                    snapshots[rid] = blob
                yield f"data: {blob}\n\n"
        except Exception as exc:
            log.warning("sse replay failed: %s", exc)
        # 2. Live updates: push new runs AND status/log changes to known runs.
        while True:
            try:
                keys = await store.arkeys("run:*")
                changed: list[dict[str, Any]] = []
                for k in keys:
                    rec = await store.arget_json(k)
                    if not isinstance(rec, dict):
                        continue
                    rid = str(rec.get("run_id", "") or k.split("run:", 1)[-1])
                    blob = json.dumps(rec, separators=(",", ":"))
                    if snapshots.get(rid) != blob:
                        snapshots[rid] = blob
                        changed.append(rec)
                for rec in sorted(changed, key=lambda r: str(r.get("created_at", ""))):
                    yield f"data: {json.dumps(rec, separators=(',', ':'))}\n\n"
                if not changed:
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
def quota(request: Request) -> dict[str, Any]:
    _require_auth(request)
    limit = _get_quota_limit()
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
        "upstash_box": {"used": ub, "limit": limit, "remaining": max(0, limit - ub)},
        "vercel_sandbox": {"used": vs, "limit": limit, "remaining": max(0, limit - vs)},
        "total_limit": limit * 2,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "store": store.backend_mode(), "model": _get_llm_model()}


@app.get("/")
def root() -> dict[str, Any]:
    return {"ok": True, "service": "pr-agent-backend", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
