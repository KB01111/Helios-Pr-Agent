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
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

# Load local .env files before importing store: store snapshots the Redis env
# vars at import time. Real process env always wins over file values, and
# backend/.env (written by the desktop Integrations tab) wins over repo-root
# .env because it is loaded first.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is declared in requirements
    load_dotenv = None  # type: ignore[assignment]

if load_dotenv is not None:
    _BACKEND_DIR = Path(__file__).resolve().parent
    for _env_path in (_BACKEND_DIR / ".env", _BACKEND_DIR.parent / ".env"):
        if _env_path.is_file():
            load_dotenv(_env_path, override=False, interpolate=False)

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


def _constant_time_equal(left: str, right: str) -> bool:
    """Compare credentials without raising on non-ASCII input."""
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError, TypeError):
        return False


def _auth_ok(request: Request, *, allow_query_token: bool = False) -> bool:
    token = _get_api_token()
    if not token:
        return True
    auth = (request.headers.get("Authorization") or "").strip()
    if auth and _constant_time_equal(auth, f"Bearer {token}"):
        return True
    # EventSource cannot set headers, so only the SSE endpoint opts into a
    # short-lived, single-use ?ticket= (never the long-lived API token), which
    # keeps credentials out of URLs and proxy logs.
    if not allow_query_token:
        return False
    return _consume_stream_ticket(request.query_params.get("ticket", ""))


_STREAM_TICKET_TTL_S = 60
_stream_tickets: dict[str, float] = {}
_stream_ticket_lock = threading.Lock()


def _consume_stream_ticket(ticket: str) -> bool:
    if not ticket:
        return False
    now = time.monotonic()
    with _stream_ticket_lock:
        for k in [k for k, exp in _stream_tickets.items() if exp < now]:
            _stream_tickets.pop(k, None)
        exp = _stream_tickets.pop(ticket, None)
    return exp is not None and exp >= now


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
VALID_PR_ACTIONS = {"opened", "reopened", "synchronize", "labeled"}

# GitHub repository names are `owner/name`; validate before using them in
# downstream API URLs or sandbox shell commands.
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")

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

    @model_validator(mode="after")
    def _require_trigger_label(self):
        if self.trigger == "on_label" and not (self.trigger_label or "").strip():
            raise ValueError("trigger_label is required when trigger is on_label")
        return self


class PRContext(BaseModel):
    repo: str
    pr_number: int
    sha: str
    action: str
    label: str | None = None
    title: str = ""
    head_ref: str = ""


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
    return _constant_time_equal(f"sha256={mac}", signature)


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
    if action in ("opened", "reopened", "synchronize"):
        return trigger == "on_commit"
    return False


# ---------------------------------------------------------------------------
# Background dispatch (lazy router import to avoid startup cycles)
# ---------------------------------------------------------------------------


def _run_worker_count() -> int:
    try:
        return max(1, min(int(os.getenv("RUN_WORKERS", "4")), 32))
    except ValueError:
        return 4


# Dedicated pool: a slow provider must not exhaust Starlette's shared sync
# threadpool and stall /health, /agents, /runs, etc.
_RUN_EXECUTOR = ThreadPoolExecutor(
    max_workers=_run_worker_count(), thread_name_prefix="pr-agent-run"
)


_MAX_PENDING_RUNS = int(os.getenv("RUN_QUEUE_MAX", "200") or 200)
_pending_runs = 0
_pending_lock = threading.Lock()


def _fail_run(run_id: str, message: str, retries: int = 3) -> None:
    for attempt in range(retries):
        if _fail_run_once(run_id, message):
            return
        time.sleep(0.2 * (attempt + 1))
    # Last resort so an undispatched record can't linger as "queued".
    try:
        store.rdelete(f"run:{run_id}")
    except Exception as exc:
        log.warning("could not remove undispatched run=%s: %s (reaper will fail it)", run_id, exc)


def _fail_run_once(run_id: str, message: str) -> bool:
    try:
        current = store.rget_json(f"run:{run_id}")
        if not isinstance(current, dict):
            current = {}
        logs = current.get("logs")
        logs = [str(i) for i in logs] if isinstance(logs, list) else []
        logs.append(f"ERROR: {message}")
        current.update({"status": "failed", "finished_at": _now_iso(), "logs": logs})
        store.rset_json(f"run:{run_id}", current)
        return True
    except Exception as exc:
        log.warning("could not persist failure for run=%s: %s", run_id, exc)
        return False


def _dispatch_tracked(agent_cfg: dict[str, Any], pr: dict[str, Any], run_id: str) -> None:
    global _pending_runs
    try:
        dispatch_run(agent_cfg, pr, run_id)
    finally:
        with _pending_lock:
            _pending_runs -= 1


def enqueue_dispatch(agent_cfg: dict[str, Any], pr: dict[str, Any], run_id: str) -> None:
    """Enqueue a run without blocking the request; the backlog is bounded."""
    global _pending_runs
    with _pending_lock:
        full = _pending_runs >= _MAX_PENDING_RUNS
        if not full:
            _pending_runs += 1
    if full:
        log.error("dispatch run=%s rejected: queue full (%d)", run_id, _MAX_PENDING_RUNS)
        _fail_run(run_id, "run queue is full; try again later")
        return
    try:
        _RUN_EXECUTOR.submit(_dispatch_tracked, agent_cfg, pr, run_id)
    except RuntimeError as exc:  # executor already shut down
        with _pending_lock:
            _pending_runs -= 1
        log.error("dispatch run=%s could not be queued: %s", run_id, exc)
        _fail_run(run_id, f"could not be queued: {exc}")


def dispatch_run(agent_cfg: dict[str, Any], pr: dict[str, Any], run_id: str) -> None:
    """Run the PR agent synchronously (called via BackgroundTasks)."""
    log.info("dispatch run=%s agent=%s repo=%s pr=%s", run_id, agent_cfg.get("id"), pr.get("repo"), pr.get("pr_number"))
    try:
        started = store.rget_json(f"run:{run_id}")
        if isinstance(started, dict):
            started["status"] = "running"
            started["started_at"] = _now_iso()
            store.rset_json(f"run:{run_id}", started)
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
            router = SandboxRouter(_store, limit=_get_quota_limit())
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

        result_logs = result.get("logs")
        if not isinstance(result_logs, list):
            result_logs = [str(result_logs)] if result_logs else []

        current = store.rget_json(f"run:{run_id}")
        if not isinstance(current, dict):
            current = {}
        current_logs = current.get("logs")
        if not isinstance(current_logs, list):
            current_logs = []

        patch = {
            # NOTE: quota is incremented inside SandboxRouter only for real
            # sandbox runs (status == "completed") — never here, so failures,
            # retries, and review fallbacks don't burn quota.
            "status": result.get("status", "completed"),
            "provider": result.get("provider", agent_cfg.get("runtime_mode", "review_only")),
            "diff": result.get("diff"),
            "cpu_seconds": result.get("cpu_seconds", 0),
            "finished_at": _now_iso(),
            "logs": [str(item) for item in current_logs + result_logs],
        }
        current.update(patch)
        store.rset_json(f"run:{run_id}", current)
        log.info("dispatch run=%s finished status=%s", run_id, patch["status"])
    except Exception as exc:  # never let background errors go silent
        log.exception("dispatch run=%s failed: %s", run_id, exc)
        try:
            current = store.rget_json(f"run:{run_id}")
            if not isinstance(current, dict):
                current = {}
            current.update({"status": "failed", "finished_at": _now_iso()})
            logs = current.get("logs")
            if not isinstance(logs, list):
                logs = []
            logs.append(f"ERROR: {exc}")
            current["logs"] = [str(item) for item in logs]
            store.rset_json(f"run:{run_id}", current)
        except Exception as persist_exc:
            log.warning("could not persist failure state for run=%s: %s", run_id, persist_exc)


# Queued runs are held only in this process. After a restart, records older
# than this deadline are marked failed instead of staying queued forever.
STALE_QUEUED_S = 15 * 60
STALE_RUNNING_S = 60 * 60
REAP_INTERVAL_S = 60


def _reap_stale_queued_runs() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_QUEUED_S)
    try:
        for k in store.rkeys("run:*"):
            rec = store.rget_json(k)
            if not isinstance(rec, dict) or rec.get("status") not in ("queued", "running"):
                continue
            is_running = rec.get("status") == "running"
            run_cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_RUNNING_S) if is_running else cutoff
            stamp = rec.get("started_at") if is_running and rec.get("started_at") else rec.get("created_at", "")
            try:
                created = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except ValueError:
                created = None
            if created is None:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= run_cutoff:
                continue
            rec["status"] = "failed"
            rec["finished_at"] = _now_iso()
            logs = rec.get("logs")
            if not isinstance(logs, list):
                logs = []
            logs = [str(item) for item in logs]
            logs.append("ERROR: run was interrupted by a backend restart before dispatch completed")
            rec["logs"] = logs
            store.rset_json(k, rec)
            log.warning("reaped stale queued run=%s", rec.get("run_id"))
    except Exception as exc:
        log.warning("stale-run reaper failed: %s", exc)


async def _reap_loop() -> None:
    # Runs after startup too, so runs whose dispatch was lost in a restart are
    # failed once they pass the deadline without needing another restart.
    while True:
        await asyncio.sleep(REAP_INTERVAL_S)
        await asyncio.to_thread(_reap_stale_queued_runs)


@app.on_event("startup")
async def _startup_reap() -> None:
    # Import the router once up front: concurrent first-time imports from
    # worker threads can hit Python's import-lock deadlock detection.
    try:
        try:
            import sandboxes.router  # noqa: F401  (cwd=backend)
        except ImportError:
            import backend.sandboxes.router  # type: ignore[no-redef]  # noqa: F401
    except Exception as exc:
        log.warning("router preload failed: %s", exc)
    _reap_stale_queued_runs()
    app.state.reap_task = asyncio.create_task(_reap_loop())


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
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")

    sender = payload.get("sender")
    if isinstance(sender, dict) and sender.get("type") == "Bot":
        return JSONResponse({"skipped": "bot"}, status_code=202)

    pr = payload.get("pull_request")
    raw_action = payload.get("action", "")
    action = raw_action if isinstance(raw_action, str) else ""
    if not isinstance(pr, dict) or not pr or action not in VALID_PR_ACTIONS:
        return JSONResponse({"skipped": action or "no-pull_request"}, status_code=202)

    repository = payload.get("repository")
    repo = (repository.get("full_name") if isinstance(repository, dict) else "") or ""
    repo = repo if isinstance(repo, str) else ""
    try:
        pr_number = int(pr.get("number", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid pr_number")
    head = pr.get("head")
    sha = (head.get("sha") if isinstance(head, dict) else "") or ""
    sha = sha if isinstance(sha, str) else ""
    head_ref = (head.get("ref") if isinstance(head, dict) else "") or ""
    head_ref = head_ref if isinstance(head_ref, str) else ""
    title = pr.get("title") if isinstance(pr.get("title"), str) else ""
    label_obj = payload.get("label")
    label = (label_obj.get("name") if isinstance(label_obj, dict) else None) if action == "labeled" else None
    label = label if isinstance(label, str) and label else None
    if action == "labeled" and not label:
        return JSONResponse({"skipped": "labeled-without-label"}, status_code=202)
    if not repo or not pr_number:
        raise HTTPException(status_code=400, detail="missing repo/pr_number")
    if not _REPO_RE.fullmatch(repo):
        raise HTTPException(status_code=400, detail="invalid repository name")
    _owner, _name = repo.split("/", 1)
    if _owner in (".", "..") or _name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid repository name")

    # Load agent configs first: don't consume the debounce window for events
    # that match no agent (and scope it to the action/label).
    agent_keys = await store.arkeys("agent:*")
    agents: list[dict[str, Any]] = []
    for k in agent_keys:
        cfg = await store.arget_json(k)
        if isinstance(cfg, dict):
            agents.append(cfg)
    matched = [a for a in agents if _agent_matches(a, repo, action, label)]
    log.info("webhook %s #%d action=%s: %d agents, %d matched", repo, pr_number, action, len(agents), len(matched))
    if not matched:
        return JSONResponse({"accepted": True, "runs": [], "matched": 0}, status_code=202)

    # Debounce: one trigger per PR/action/label per 45s.
    # Keyed by head SHA so a newer push is never swallowed, while redeliveries
    # of the same event (same SHA) are still deduplicated.
    debounce_key = f"debounce:{repo}:{pr_number}:{action}:{label or ''}:{sha}"
    # Two-state claim: a short-lived "pending" lease while runs are being
    # created (lapses fast even if cleanup fails), promoted to "done" for the
    # full window only once every run record is stored.
    claim = f"pending:{uuid.uuid4().hex}"
    if not await store.arset(debounce_key, claim, ex=_CLAIM_LEASE_S, nx=True):
        state = await asyncio.to_thread(store.rget, debounce_key)
        if state == "done":
            return JSONResponse({"debounced": True}, status_code=202)
        # pending, or the lease lapsed between the two reads: ask the caller to retry
        return JSONResponse({"in_progress": True, "retry": True}, status_code=409)

    pr_ctx = PRContext(
        repo=repo,
        pr_number=pr_number,
        sha=sha,
        action=action,
        label=label,
        title=title,
        head_ref=head_ref,
    )
    run_ids: list[str] = []
    try:
        await _create_runs(matched, repo, pr_number, sha, action, pr_ctx, run_ids, tasks)
    except Exception:
        # Partial write: nothing was enqueued, so fail the records already
        # written (no stuck "queued" runs), then release the claim so GitHub's
        # retry can recreate every run.
        for rid in run_ids:
            try:
                await asyncio.to_thread(_fail_run, rid, "webhook processing aborted before dispatch")
            except Exception as exc:  # the stale-run reaper is the backstop
                log.warning("could not fail run=%s during cleanup: %s", rid, exc)
        try:
            # Only release a claim we still own (our lease may have lapsed).
            if await asyncio.to_thread(store.rget, debounce_key) == claim:
                await asyncio.to_thread(store.rdelete, debounce_key)
        except Exception as exc:
            log.warning("could not release debounce key: %s", exc)
        raise

    try:
        if await asyncio.to_thread(store.rget, debounce_key) == claim:
            await store.arset(debounce_key, "done", ex=DEBOUNCE_TTL_S)
    except Exception as exc:  # lease just lapses; worst case one duplicate set of runs
        log.warning("could not promote debounce claim: %s", exc)
    return JSONResponse({"accepted": True, "runs": run_ids}, status_code=202)


_CLAIM_LEASE_S = 60


async def _create_runs(matched, repo, pr_number, sha, action, pr_ctx, run_ids, tasks) -> None:  # type: ignore[no-untyped-def]
    pending: list[tuple[dict[str, Any], str]] = []
    for agent_cfg in matched:
        # Deterministic per (event, agent): if two handlers overlap (e.g. a
        # lease expired mid-processing) they target the same record, so the
        # nx write below keeps each agent's run unique.
        ident = f"{repo}:{pr_number}:{action}:{pr_ctx.label or ''}:{sha}:{agent_cfg.get('id')}"
        run_id = hashlib.sha256(ident.encode()).hexdigest()[:12]
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
        key = f"run:{run_id}"
        if not await store.arset_json(key, record, nx=True):
            existing = await store.arget_json(key)
            if not (isinstance(existing, dict) and existing.get("status") == "failed"):
                continue  # already created (and dispatched) by another delivery
            # A previously failed/aborted attempt: exactly one overlapping
            # handler wins the per-attempt nx claim and recreates the run.
            attempt = int(existing.get("attempt", 0) or 0) + 1
            if not await store.arset(f"runretry:{run_id}:{attempt}", "1", ex=60, nx=True):
                continue
            record["attempt"] = attempt
            try:
                await store.arset_json(key, record)
            except Exception:
                # Release the attempt claim so the next delivery can retry.
                try:
                    await asyncio.to_thread(store.rdelete, f"runretry:{run_id}:{attempt}")
                except Exception as exc:
                    log.warning("could not release retry claim for run=%s: %s", run_id, exc)
                raise
        run_ids.append(run_id)
        pending.append((agent_cfg, run_id))
    # Dispatch only once every record is persisted.
    for agent_cfg, run_id in pending:
        tasks.add_task(enqueue_dispatch, agent_cfg, pr_ctx.model_dump(), run_id)


# ---------------------------------------------------------------------------
# Agent CRUD
# ---------------------------------------------------------------------------


@app.post("/agents")
def create_agent(agent: AgentConfig, request: Request) -> dict[str, Any]:
    _require_auth(request)
    data = agent.model_dump()
    # Atomic create-if-absent (nx) so concurrent creates never overwrite.
    if data["id"]:
        if not store.rset_json(f"agent:{data['id']}", data, nx=True):
            raise HTTPException(status_code=409, detail="agent id already exists")
        return data
    slug = "".join(c.lower() if c.isalnum() else "-" for c in data["name"]).strip("-") or "agent"
    for _ in range(5):
        data["id"] = f"{slug}-{uuid.uuid4().hex[:6]}"
        if store.rset_json(f"agent:{data['id']}", data, nx=True):
            return data
    raise HTTPException(status_code=409, detail="could not allocate a unique agent id")


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
    if rec.get("status") != "queued":
        raise HTTPException(status_code=409, detail="only queued runs can be approved")
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
def config_status(request: Request) -> dict[str, Any]:
    """Which providers are configured. Returns booleans/mode only — never secret values."""
    _require_auth(request)
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

    secret_set = _configured(_get_webhook_secret())
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
    key = f"__config_test_ping__:{uuid.uuid4().hex}"
    try:
        probe = f"ping:{uuid.uuid4().hex[:8]}"
        if not await store.arset(key, probe, ex=60):
            raise RuntimeError("write rejected")
        if await store.arget(key) != probe:
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
    finally:
        try:
            await store.ardelete(key)
        except Exception as cleanup_exc:
            log.debug("could not remove Redis test key: %s", cleanup_exc)


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
    # Non-destructive probe. Only a 2xx proves the key/endpoint work; never
    # report healthy just because the host answered with 404/405.
    # Never sends the key anywhere except the Box API over TLS, never echoes it.
    last_status: int | None = None
    async with httpx.AsyncClient(timeout=10.0) as client:
        for url in ("https://api.upstash.com/v2/box", "https://api.upstash.com/v2/boxes"):
            resp = None
            try:
                resp = await client.get(url, headers={"Authorization": f"Bearer {key}"})
            except Exception as exc:
                log.debug("upstash box probe %s failed: %s", url, exc)
            if resp is None:
                continue  # try the next endpoint, else report offline
            last_status = resp.status_code
            if 200 <= resp.status_code < 300:
                return {
                    "provider": "upstash_box", "ok": True, "configured": True,
                    "details": f"Box API reachable ({url} HTTP {resp.status_code}), key accepted",
                }
            if resp.status_code in (401, 403):
                return {
                    "provider": "upstash_box", "ok": False, "configured": True,
                    "details": "Box API rejected the key (HTTP 401/403) — check/rotate it",
                }
    if last_status is not None:
        return {
            "provider": "upstash_box", "ok": False, "configured": True,
            "details": f"Box API answered HTTP {last_status} but no probe endpoint "
            "confirmed the key; check the Box API path/key",
            "reachable": True,
        }
    return {
        "provider": "upstash_box", "ok": False, "configured": True,
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


@app.post("/events/ticket")
def events_ticket(request: Request) -> dict[str, Any]:
    """Mint a 60s single-use ticket for EventSource (which cannot send headers)."""
    _require_auth(request)
    ticket = uuid.uuid4().hex + uuid.uuid4().hex
    with _stream_ticket_lock:
        if len(_stream_tickets) > 1000:
            _stream_tickets.clear()
        _stream_tickets[ticket] = time.monotonic() + _STREAM_TICKET_TTL_S
    return {"ticket": ticket, "expires_in": _STREAM_TICKET_TTL_S}


def _digest(rec: dict[str, Any]) -> str:
    """Fixed-size fingerprint so open streams don't retain whole run records."""
    return hashlib.sha1(json.dumps(rec, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


@app.get("/events/stream")
async def events_stream(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
) -> StreamingResponse:
    """SSE stream: replay recent runs on connect, then push new/updated runs live.

    Auth: if BACKEND_API_TOKEN is set, requires `Authorization: Bearer <token>`
    or a single-use `?ticket=<ticket>` from POST /events/ticket (EventSource
    cannot set headers).
    """
    if not _auth_ok(request, allow_query_token=True):
        raise HTTPException(status_code=401, detail="invalid or missing API token")

    async def gen():  # type: ignore[no-untyped-def]
        snapshots: dict[str, str] = {}
        # 1. Seed snapshots with every run so the first poll only emits real
        # changes, then replay just the most recent `limit` records.
        try:
            keys = await store.arkeys("run:*")
            recs: list[dict[str, Any]] = []
            for k in keys:
                rec = await store.arget_json(k)
                if isinstance(rec, dict):
                    recs.append(rec)
            recs.sort(key=lambda r: str(r.get("created_at", "")))
            for rec in recs:
                rid = str(rec.get("run_id", ""))
                if rid:
                    snapshots[rid] = _digest(rec)
            for rec in recs[-limit:]:
                blob = json.dumps(rec, separators=(",", ":"))
                yield f"data: {blob}\n\n"
        except Exception as exc:
            log.warning("sse replay failed: %s", exc)
        # 2. Live updates: push new runs AND status/log changes to known runs.
        while True:
            try:
                keys = await store.arkeys("run:*")
                changed: list[dict[str, Any]] = []
                live_ids: set[str] = set()
                for k in keys:
                    rec = await store.arget_json(k)
                    if not isinstance(rec, dict):
                        continue
                    rid = str(rec.get("run_id", "") or k.split("run:", 1)[-1])
                    live_ids.add(rid)
                    digest = _digest(rec)
                    if snapshots.get(rid) != digest:
                        snapshots[rid] = digest
                        changed.append(rec)
                # Keep the snapshot map bounded by the records that still exist.
                for stale in set(snapshots) - live_ids:
                    snapshots.pop(stale, None)
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

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


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
