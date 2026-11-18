"""Decision flow: idempotent PR triage, desktop hand-off, cloud fallback, escalation.

How the pieces fit together
---------------------------
* Every commit-type PR event (``opened`` / ``reopened`` / ``synchronize``)
  registers an evaluation in ``store`` keyed on ``(repo, pr_number, head_sha)``.
  The first path to register wins; everyone else is told it was a duplicate.
* If the desktop daemon has a fresh heartbeat the event is *deferred*: the
  desktop evaluates it locally (Rust decider, optional local model) and submits
  the decision. If it does not do so within ``DECIDER_DEFER_GRACE_S`` the cloud
  sweeper (Cloudflare worker cron, or the backend's own reaper loop) decides with
  the Python policy instead. Either way exactly one decision is applied.
* The user's autonomy level turns a decision into an action or an escalation
  (``ask``) that waits in the inbox for a human to resolve.

Execution always happens here, through the existing quota-aware SandboxRouter,
so the desktop never needs provider credentials.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

try:  # cwd=backend (`uvicorn main:app`)
    import store
    from sandboxes import decider
    from sandboxes.safety import UnsafeSandboxInput, safe_repo
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import store  # type: ignore[no-redef]
    from backend.sandboxes import decider  # type: ignore[no-redef]
    from backend.sandboxes.safety import UnsafeSandboxInput, safe_repo  # type: ignore[no-redef]

log = logging.getLogger("pr-agent.decisions")

HEARTBEAT_TTL_S = 90
STUCK_AFTER_S = 5 * 60
EXTERNAL_LEASE_S = 30 * 60
COMMIT_ACTIONS = ("opened", "reopened", "synchronize")
_AUTONOMY_KEY = "cfg:autonomy"
_HEARTBEAT_KEY = "daemon:heartbeat"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

AuthFn = Callable[[Request], None]
DispatchFn = Callable[[list[dict[str, Any]], dict[str, Any], dict[str, Any]], Awaitable[list[str]]]
_require_auth: AuthFn | None = None
_dispatch_agents: DispatchFn | None = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def enabled() -> bool:
    return os.getenv("DECIDER_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no")


def defer_grace_s() -> int:
    try:
        return max(10, min(int(os.getenv("DECIDER_DEFER_GRACE_S", "120")), 3600))
    except ValueError:
        return 120


def get_autonomy() -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "level": "assist",
        "conf_auto": decider.DEFAULT_CONF_AUTO,
        "conf_floor": decider.DEFAULT_CONF_FLOOR,
    }
    raw = store.rget_json(_AUTONOMY_KEY)
    if isinstance(raw, dict):
        if raw.get("level") in decider.AUTONOMY_LEVELS:
            cfg["level"] = raw["level"]
        for key in ("conf_auto", "conf_floor"):
            try:
                cfg[key] = min(1.0, max(0.0, float(raw[key])))
            except (KeyError, TypeError, ValueError):
                pass
    cfg["conf_floor"] = min(cfg["conf_floor"], cfg["conf_auto"])
    return cfg


class AutonomyBody(BaseModel):
    level: Literal["observe", "assist", "auto"]
    conf_auto: float = Field(default=decider.DEFAULT_CONF_AUTO, ge=0.5, le=1.0)
    conf_floor: float = Field(default=decider.DEFAULT_CONF_FLOOR, ge=0.3, le=0.95)

    @model_validator(mode="after")
    def _floor_below_auto(self) -> "AutonomyBody":
        if self.conf_floor > self.conf_auto:
            raise ValueError("conf_floor must not exceed conf_auto")
        return self


class HeartbeatBody(BaseModel):
    version: str = Field(default="", max_length=32)
    interval_secs: int = Field(default=60, ge=5, le=3600)
    model: Literal["rules", "sidecar"] = "rules"


class SubmitBody(BaseModel):
    decision: dict[str, Any]
    decided_by: Literal["desktop"] = "desktop"


class ResolveBody(BaseModel):
    action: Literal["fast_review", "sandbox", "skip"]
    resolved_by: str | None = Field(default=None, max_length=80)


class ClaimBody(BaseModel):
    action: Literal["claim", "complete", "release"] = "claim"
    repo: str
    pr_number: int = Field(ge=1)
    head_sha: str
    owner: str = Field(default="external", max_length=40)
    claim_id: str | None = Field(default=None, max_length=64)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(stamp: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def desktop_status() -> dict[str, Any]:
    hb = store.rget_json(_HEARTBEAT_KEY)
    age = None
    if isinstance(hb, dict) and isinstance(hb.get("ts"), (int, float)):
        age = max(0.0, time.time() - float(hb["ts"]))
    online = age is not None and age <= HEARTBEAT_TTL_S
    return {
        "desktop_online": online,
        "last_seen_age_s": None if age is None else round(age, 1),
        "info": hb if isinstance(hb, dict) else None,
        "fallback_active": not online,
        "defer_grace_s": defer_grace_s(),
        "enabled": enabled(),
    }


def _brief(rec: dict[str, Any]) -> dict[str, Any]:
    """Record without the bulky facts (list views)."""
    out = {k: v for k, v in rec.items() if k != "facts"}
    files = (rec.get("facts") or {}).get("files") or []
    out["files"] = len(files)
    out["churn"] = sum(int(f.get("additions") or 0) + int(f.get("deletions") or 0) for f in files)
    return out


async def fetch_facts(repo: str, pr_number: int, title: str) -> dict[str, Any] | None:
    """PR facts from GitHub, or None when they cannot be fetched (caller fails open)."""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        return None
    files: list[dict[str, Any]] = []
    truncated = False
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            for page in range(1, 4):
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files",
                    params={"per_page": 100, "page": page},
                    headers=headers,
                )
                if resp.status_code != 200:
                    log.warning("decision: PR files HTTP %s for %s#%s", resp.status_code, repo, pr_number)
                    return None
                batch = resp.json()
                if not isinstance(batch, list):
                    return None
                files.extend(batch)
                if len(batch) < 100:
                    break
            else:
                truncated = True  # three full pages: there may be more
    except Exception as exc:
        log.warning("decision: PR files fetch failed (%s)", type(exc).__name__)
        return None
    return decider.extract_facts(title, files, truncated)


# ---------------------------------------------------------------------------
# State transitions (sync, run in a thread; the lock makes each one atomic)
# ---------------------------------------------------------------------------


_ACTIONS = ("fast_review", "sandbox", "skip")


def _bump_feedback(decision: dict[str, Any], action: str) -> None:
    """Calibration signal. For a concrete decision a human either agreed with it or
    overrode it. An ESCALATE_HUMAN decision made no claim to agree with, so what the
    human *chose* is recorded instead (that is the label for the gray zone)."""
    kind = decision.get("decision")
    if kind == decider.ESCALATE_HUMAN:
        verdict = f"chose_{action}"
    else:
        agree = (kind, action) in {
            (decider.SKIP, "skip"), (decider.FAST_REVIEW, "fast_review"), (decider.SANDBOX_TEST, "sandbox"),
        }
        verdict = "agree" if agree else "override"
    try:
        store.rincr(f"decisions:fb:{kind}:{verdict}")
    except Exception as exc:
        log.warning("feedback counter failed: %s", exc)


def apply_decision(eid: str, decision: dict[str, Any], decided_by: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    """Apply autonomy policy to ``decision`` under the evaluation lock."""
    with store.eval_lock(eid) as token:
        if token is None:
            return {"conflict": "busy"}
        rec = store.eval_get(eid)
        if rec is None:
            return {"conflict": "missing"}
        if rec.get("state") not in allowed:
            return {"conflict": "state", "state": rec.get("state")}
        auto = get_autonomy()
        patch: dict[str, Any] = {"decision": decision, "decided_by": decided_by, "autonomy": auto["level"]}
        if auto["level"] == "observe":
            # Shadow mode: keep the legacy behaviour, just record what we would do.
            patch.update(state="dispatching", legacy=True)
        else:
            disp = decider.disposition(decision, auto["level"], auto["conf_auto"], auto["conf_floor"])
            if disp == decider.AUTO and decision["decision"] == decider.SKIP:
                patch.update(state="skipped")
            elif disp == decider.AUTO:
                patch.update(state="dispatching", legacy=False)
            else:
                patch.update(state="escalated")
        store.eval_update(eid, patch)
        return {"state": patch["state"], "legacy": bool(patch.get("legacy"))}


def _resolve_plan(eid: str, action: str, who: str | None) -> dict[str, Any]:
    with store.eval_lock(eid) as token:
        if token is None:
            return {"conflict": "busy"}
        rec = store.eval_get(eid)
        if rec is None:
            return {"conflict": "missing"}
        if rec.get("state") not in ("escalated", "failed"):
            return {"conflict": "state", "state": rec.get("state")}
        patch: dict[str, Any] = {
            "resolution": {"action": action, "by": who or "user", "at": _iso(_utcnow())},
        }
        if isinstance(rec.get("decision"), dict):
            _bump_feedback(rec["decision"], action)
        patch["state"] = "dismissed" if action == "skip" else "dispatching"
        store.eval_update(eid, patch)
        return {"state": patch["state"], "action": action}


def _mark_failed(eid: str, error: str, only_if: tuple[str, ...] = ("pending", "dispatching"), stale_s: float = 0.0) -> None:
    """Mark an evaluation failed — but only if it is still in one of ``only_if`` (and,
    when ``stale_s`` is set, still untouched for that long), re-checked under the lock so
    a dispatch that just completed is never overwritten with ``failed``."""
    try:
        with store.eval_lock(eid) as token:
            if token is None:
                return
            rec = store.eval_get(eid)
            if rec is None or rec.get("state") not in only_if:
                return
            updated = _parse(rec.get("updated_at"))
            if stale_s and (updated is None or (_utcnow() - updated).total_seconds() <= stale_s):
                return
            store.eval_update(eid, {"state": "failed", "error": error[:300]})
    except Exception as exc:
        log.warning("could not mark evaluation %s failed: %s", eid, exc)


def _guard_desktop_decision(
    rec: dict[str, Any], decision: dict[str, Any], conf_floor: float
) -> tuple[dict[str, Any], str]:
    """The desktop is trusted for *scoring*, never for guardrails: re-check its decision
    against the stored facts and fall back to the cloud policy if it would skip or
    downgrade something the guardrails protect."""
    facts = rec.get("facts")
    if not isinstance(facts, dict):
        return decision, "desktop"
    server = decider.decide(facts, conf_floor)
    unsafe = server["reason_code"] == "SAFETY_FLAGGED" and decision["decision"] != decider.ESCALATE_HUMAN
    bad_skip = decision["decision"] == decider.SKIP and server["decision"] != decider.SKIP
    if unsafe or bad_skip:
        return server, "desktop-overridden"
    return decision, "desktop"


# ---------------------------------------------------------------------------
# Execution (async: creates runs through main.py's idempotent run machinery)
# ---------------------------------------------------------------------------


async def _dispatch(eid: str, mode_of: Callable[[dict[str, Any]], str | None], decision: dict[str, Any] | None) -> list[str]:
    if _dispatch_agents is None:
        raise RuntimeError("decision_flow.register() was not called")
    rec = await asyncio.to_thread(store.eval_get, eid)
    if rec is None:
        return []
    prepared: list[dict[str, Any]] = []
    for agent_id in rec.get("agent_ids") or []:
        agent = await asyncio.to_thread(store.rget_json, f"agent:{agent_id}")
        if not isinstance(agent, dict):
            continue
        mode = mode_of(agent)
        if mode is None:
            continue
        prepared.append({**agent, "runtime_mode": mode})
    try:
        run_ids = await _dispatch_agents(prepared, rec["pr"], {"eval_id": eid, "decision": decision}) if prepared else []
        # enqueue_dispatch marks a run failed (instead of raising) when the queue is full.
        records = await asyncio.to_thread(lambda: [store.rget_json(f"run:{r}") for r in run_ids])
        if any(isinstance(r, dict) and r.get("status") == "failed" for r in records):
            raise RuntimeError("run could not be queued (queue full?)")
    except Exception as exc:
        await asyncio.to_thread(_mark_failed, eid, f"dispatch failed: {type(exc).__name__}", ("dispatching",))
        raise
    await asyncio.to_thread(store.eval_update, eid, {"state": "dispatched", "run_ids": run_ids})
    return run_ids


def _agent_mode(agent: dict[str, Any]) -> str:
    return str(agent.get("runtime_mode") or "review_only")


async def _execute_plan(eid: str, plan: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    if "conflict" in plan:
        return {"deduped": True, "id": eid, **{k: v for k, v in plan.items() if k == "state"}}
    state = plan["state"]
    base = {"id": eid, "decision": decision}
    if state == "dispatching":
        mode_of = _agent_mode if plan.get("legacy") else (lambda a: decider.effective_mode(_agent_mode(a), decision))
        run_ids = await _dispatch(eid, mode_of, decision)
        return {"accepted": True, "runs": run_ids, **base}
    if state == "skipped":
        return {"skipped": decision["reason_code"], **base}
    return {"escalated": True, **base}


# ---------------------------------------------------------------------------
# Entry points used by main.py
# ---------------------------------------------------------------------------


async def gate_webhook(pr: dict[str, Any], agents: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Triage a webhook event. ``None`` means "carry on with the legacy path".

    ``labeled`` events bypass the gate: a human applied the label on purpose, and
    it shares a head SHA with the commit event that was already evaluated.
    """
    if not enabled() or pr.get("action") not in COMMIT_ACTIONS or not pr.get("sha"):
        return None
    repo, number, sha = str(pr["repo"]), int(pr["pr_number"]), str(pr["sha"])
    try:
        created, rec = await asyncio.to_thread(
            store.eval_register, repo, number, sha,
            {
                "state": "pending", "source": "cloud-webhook", "action": pr.get("action"),
                "title": pr.get("title", ""), "pr": pr,
                "agent_ids": [a.get("id") for a in agents if a.get("id")],
                "decision": None, "decided_by": None, "run_ids": [], "error": None, "defer_until": None,
            },
        )
    except Exception as exc:  # store trouble: fail open to the legacy path
        log.warning("decision gate unavailable (%s); legacy path", type(exc).__name__)
        return None
    eid = rec["id"]
    if not created:
        return {"deduped": True, "id": eid, "state": rec.get("state")}

    facts = await fetch_facts(repo, number, str(pr.get("title", "")))
    if facts is None:
        # Fail open: release the registration so the legacy path (and GitHub's own
        # redelivery, should that fail) is not blocked by a dedupe record.
        await asyncio.to_thread(store.eval_delete, eid)
        return None
    auto = await asyncio.to_thread(get_autonomy)
    status = await asyncio.to_thread(desktop_status)
    if auto["level"] != "observe" and status["desktop_online"]:
        until = _iso(_utcnow() + timedelta(seconds=defer_grace_s()))
        await asyncio.to_thread(store.eval_update, eid, {"state": "deferred", "facts": facts, "defer_until": until})
        return {"deferred_to_desktop": True, "id": eid, "defer_until": until}

    await asyncio.to_thread(store.eval_update, eid, {"facts": facts})
    decision = decider.decide(facts, auto["conf_floor"])
    plan = await asyncio.to_thread(apply_decision, eid, decision, "cloud", ("pending",))
    try:
        return await _execute_plan(eid, plan, decision)
    except Exception as exc:  # already recorded as `failed`; a human can retry from the inbox
        log.warning("dispatch for %s failed: %s", eid, type(exc).__name__)
        return {"failed": "dispatch", "id": eid, "decision": decision}


async def sweep() -> dict[str, int]:
    """Cloud fallback: decide deferred events the desktop did not pick up in time,
    and fail evaluations stuck mid-flight so a human can retry them."""
    counts = {"decided": 0, "failed": 0}
    now = _utcnow()
    records = await asyncio.to_thread(store.eval_list_open, ("deferred", "pending", "dispatching"))
    for rec in records:
        eid, state = rec.get("id"), rec.get("state")
        if not eid:
            continue
        if state == "deferred":
            until = _parse(rec.get("defer_until"))
            if until is None or until > now or not isinstance(rec.get("facts"), dict):
                continue
            auto = await asyncio.to_thread(get_autonomy)
            decision = decider.decide(rec["facts"], auto["conf_floor"])
            plan = await asyncio.to_thread(apply_decision, eid, decision, "cloud-fallback", ("deferred",))
            if "conflict" not in plan:
                try:
                    await _execute_plan(eid, plan, decision)
                    counts["decided"] += 1
                except Exception as exc:
                    log.warning("sweep dispatch for %s failed: %s", eid, type(exc).__name__)
        elif state in ("pending", "dispatching"):
            updated = _parse(rec.get("updated_at"))
            if updated is not None and (now - updated).total_seconds() > STUCK_AFTER_S:
                await asyncio.to_thread(_mark_failed, eid, f"interrupted while {state}", (state,), STUCK_AFTER_S)
                counts["failed"] += 1
    return counts


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _auth(request: Request) -> None:
    if _require_auth is None:
        raise RuntimeError("decision_flow.register() was not called")
    _require_auth(request)


def _conflict(plan: dict[str, Any]) -> HTTPException:
    if plan.get("conflict") == "missing":
        return HTTPException(status_code=404, detail="evaluation not found")
    return HTTPException(status_code=409, detail=f"evaluation is {plan.get('state') or 'busy'}")


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/autonomy")
    def read_autonomy(request: Request) -> dict[str, Any]:
        _auth(request)
        return {**get_autonomy(), "levels": list(decider.AUTONOMY_LEVELS), "policy_version": decider.POLICY_VERSION}

    @router.put("/autonomy")
    def write_autonomy(body: AutonomyBody, request: Request) -> dict[str, Any]:
        _auth(request)
        store.rset_json(_AUTONOMY_KEY, body.model_dump())
        return get_autonomy()

    @router.post("/daemon/heartbeat")
    def heartbeat(body: HeartbeatBody, request: Request) -> dict[str, Any]:
        _auth(request)
        store.rset_json(_HEARTBEAT_KEY, {**body.model_dump(), "ts": time.time()}, ex=HEARTBEAT_TTL_S)
        return {"ok": True, "ttl_s": HEARTBEAT_TTL_S, "defer_grace_s": defer_grace_s()}

    @router.post("/daemon/offline")
    def offline(request: Request) -> dict[str, Any]:
        """The desktop is shutting down: stop deferring events to it right away."""
        _auth(request)
        store.rdelete(_HEARTBEAT_KEY)
        return {"ok": True}

    @router.get("/daemon/status")
    def daemon_status(request: Request) -> dict[str, Any]:
        _auth(request)
        return desktop_status()

    @router.get("/decisions")
    def list_decisions(
        request: Request,
        state: str | None = Query(default=None, max_length=24),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        _auth(request)
        if state in store.EVAL_OPEN_STATES:
            # Open states are read from the open index, so old unresolved items never
            # fall off a "newest N records" window.
            records = list(reversed(store.eval_list_open((state,))))
        else:
            records = store.eval_list(500)
            if state:
                records = [r for r in records if r.get("state") == state]
        return [_brief(r) for r in records[:limit]]

    @router.get("/decisions/pending")
    def pending(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        """Deferred events for the desktop daemon, oldest first, with facts."""
        _auth(request)
        items = [r for r in store.eval_list_open(("deferred",)) if isinstance(r.get("facts"), dict)]
        keep = ("id", "repo", "pr_number", "head_sha", "title", "facts", "defer_until", "created_at")
        return {"items": [{k: r.get(k) for k in keep} for r in items[:limit]]}

    @router.get("/decisions/stats")
    def stats(request: Request) -> dict[str, Any]:
        """Human agree/override counts per decision type — the calibration signal."""
        _auth(request)
        out: dict[str, Any] = {}
        def count(kind: str, verdict: str) -> int:
            try:
                return int(float(store.rget(f"decisions:fb:{kind}:{verdict}") or 0))
            except (TypeError, ValueError):
                return 0

        for kind in decider.DECISIONS:
            if kind == decider.ESCALATE_HUMAN:
                out[kind] = {f"chose_{a}": count(kind, f"chose_{a}") for a in _ACTIONS}
                continue
            counts = {verdict: count(kind, verdict) for verdict in ("agree", "override")}
            total = counts["agree"] + counts["override"]
            counts["override_rate"] = round(counts["override"] / total, 3) if total else None
            out[kind] = counts
        by_state: dict[str, int] = {}
        for rec in store.eval_list(500):
            by_state[str(rec.get("state"))] = by_state.get(str(rec.get("state")), 0) + 1
        return {"policy_version": decider.POLICY_VERSION, "feedback": out, "by_state": by_state}

    @router.post("/decisions/sweep")
    async def run_sweep(request: Request) -> dict[str, Any]:
        _auth(request)
        return await sweep()

    @router.post("/decisions/claim")
    async def claim(body: ClaimBody, request: Request) -> dict[str, Any]:
        """For executors outside the webhook path (e.g. the Box cron loop): claim a
        ``(repo, pr, sha)`` so nobody else reviews it, then ``complete`` or ``release``."""
        _auth(request)
        try:
            repo = safe_repo(body.repo)
        except UnsafeSandboxInput:
            raise HTTPException(status_code=400, detail="invalid repository name")
        if not _SHA_RE.fullmatch(body.head_sha):
            raise HTTPException(status_code=400, detail="invalid head_sha")
        eid = store.eval_id(repo, body.pr_number, body.head_sha)
        if body.action == "claim":
            claim_id = uuid.uuid4().hex
            created, rec = await asyncio.to_thread(
                store.eval_register, repo, body.pr_number, body.head_sha,
                {"state": "external_claimed", "source": body.owner, "claim_id": claim_id, "decision": None,
                 "run_ids": [], "agent_ids": [], "pr": {}, "error": None},
                EXTERNAL_LEASE_S,
            )
            if created:
                return {"claimed": True, "id": eid, "claim_id": claim_id}
            return {"claimed": False, "id": eid, "state": rec.get("state")}

        def _finish() -> dict[str, Any]:
            with store.eval_lock(eid) as token:
                rec = store.eval_get(eid)
                owned = (
                    token is not None and rec is not None and rec.get("state") == "external_claimed"
                    and body.claim_id and rec.get("claim_id") == body.claim_id
                )
                if not owned:
                    return {"ok": False}
                if body.action == "release":
                    store.eval_delete(eid)
                else:
                    store.eval_update(eid, {"state": "dispatched"})
                return {"ok": True}

        return await asyncio.to_thread(_finish)

    @router.get("/decisions/{eid}")
    def get_decision(eid: str, request: Request) -> dict[str, Any]:
        _auth(request)
        rec = store.eval_get(eid) if re.fullmatch(r"[0-9a-f]{16}", eid) else None
        if rec is None:
            raise HTTPException(status_code=404, detail="evaluation not found")
        return rec

    @router.post("/decisions/{eid}/submit")
    async def submit(eid: str, body: SubmitBody, request: Request) -> dict[str, Any]:
        """The desktop daemon submits its locally computed decision."""
        _auth(request)
        if not re.fullmatch(r"[0-9a-f]{16}", eid):
            raise HTTPException(status_code=404, detail="evaluation not found")
        try:
            decision = decider.validate_decision(body.decision)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        rec = await asyncio.to_thread(store.eval_get, eid)
        if rec is None:
            raise HTTPException(status_code=404, detail="evaluation not found")
        auto = await asyncio.to_thread(get_autonomy)
        decision, decided_by = _guard_desktop_decision(rec, decision, auto["conf_floor"])
        plan = await asyncio.to_thread(apply_decision, eid, decision, decided_by, ("deferred",))
        if "conflict" in plan:
            raise _conflict(plan)
        return await _execute_plan(eid, plan, decision)

    @router.post("/decisions/{eid}/resolve")
    async def resolve(eid: str, body: ResolveBody, request: Request) -> dict[str, Any]:
        """A human settles an escalation (or retries a failed evaluation)."""
        _auth(request)
        if not re.fullmatch(r"[0-9a-f]{16}", eid):
            raise HTTPException(status_code=404, detail="evaluation not found")
        plan = await asyncio.to_thread(_resolve_plan, eid, body.action, body.resolved_by)
        if "conflict" in plan:
            raise _conflict(plan)
        if plan["state"] == "dismissed":
            return {"id": eid, "dismissed": True}
        mode_of = (lambda a: "review_only") if body.action == "fast_review" else _agent_mode
        run_ids = await _dispatch(eid, mode_of, None)
        return {"id": eid, "accepted": True, "runs": run_ids}

    return router


def register(app: FastAPI, *, require_auth: AuthFn, dispatch_agents: DispatchFn) -> None:
    """Mount the routes. ``dispatch_agents(agents, pr, extra)`` must create run
    records idempotently and enqueue them (main.py's ``_dispatch_agents``)."""
    global _require_auth, _dispatch_agents
    _require_auth = require_auth
    _dispatch_agents = dispatch_agents
    app.include_router(build_router())
