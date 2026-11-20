"""GitHub event router: one handler per ``X-GitHub-Event``.

``pull_request`` commit events (opened / reopened / synchronize / labeled)
keep their existing path in ``main.github_webhook`` (triage, debounce, runs).
Everything else lands here. In Phase 0 the handlers *record* what happened
so later phases can act on it:

* a capped per-PR activity log (``pract:``) — reviews, threads, CI results,
  comments, merges — the raw signal for the merge-readiness score;
* a ``prdirty:`` marker that tells the scorer to recompute;
* GitHub App installation ids per repo (token minting);
* ``.pragent.yaml`` cache invalidation on pushes that touch it.

Handlers register with ``@on("event")``; ``route()`` dispatches and dedupes
redeliveries by ``X-GitHub-Delivery``.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

try:  # cwd=backend
    import datamodel as dm
    import github_app
    import store
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import datamodel as dm  # type: ignore[no-redef]
    from backend import github_app  # type: ignore[no-redef]
    from backend import store  # type: ignore[no-redef]

log = logging.getLogger("pr-agent.events")

CONFIG_FILES = (".pragent.yaml", ".pragent.yml")
# Bot senders are ignored for conversational events (no reply loops), but not
# for CI results: check suites and statuses are usually posted by bots.
_BOT_SENSITIVE = {"issue_comment", "pull_request_review_comment", "pull_request_review"}
_MENTION_RE_CACHE: dict[str, re.Pattern[str]] = {}


@dataclass
class EventContext:
    event: str
    action: str
    payload: dict[str, Any]
    delivery: str = ""
    repo: str = ""
    sender_is_bot: bool = False
    result: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[EventContext], Awaitable[dict[str, Any]]]
_HANDLERS: dict[str, list[Handler]] = {}


def on(event: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS.setdefault(event, []).append(fn)
        return fn

    return deco


def handled_events() -> list[str]:
    return sorted(_HANDLERS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _str(obj: Any, *path: str) -> str:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    return cur if isinstance(cur, str) else ""


def _int(obj: Any, *path: str) -> int:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return 0
        cur = cur.get(key)
    try:
        return int(cur) if not isinstance(cur, bool) else 0
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Shared recording helpers
# ---------------------------------------------------------------------------


def record_pr_activity(repo: str, pr_number: int, entry: dict[str, Any], *, dirty: bool = True) -> None:
    if not dm.valid_repo(repo) or pr_number <= 0:
        return
    entry = {"at": _now(), **entry}
    store.rlpush_json_capped(dm.activity_key(repo, pr_number), entry, dm.ACTIVITY_CAP, ex=dm.ACTIVITY_TTL_S)
    if dirty:
        store.rset(dm.dirty_key(repo, pr_number), entry["at"], ex=dm.DIRTY_TTL_S)


def pr_activity(repo: str, pr_number: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = store.rlrange_json(dm.activity_key(repo, pr_number), 0, max(0, limit - 1))
    return [r for r in rows if isinstance(r, dict)]


def is_dirty(repo: str, pr_number: int) -> bool:
    return store.rget(dm.dirty_key(repo, pr_number)) is not None


_MAX_PRS_PER_HEAD = 20


def remember_head(repo: str, pr_number: int, sha: str) -> None:
    """Map a head sha to its PR(s). Several open PRs can share one head (the same
    branch proposed into two bases), so the value is a small list, not one PR."""
    if not (sha and dm.valid_repo(repo) and pr_number > 0):
        return
    prs = prs_for_head(repo, sha)
    if pr_number not in prs:
        prs = (prs + [pr_number])[-_MAX_PRS_PER_HEAD:]
    store.rset_json(dm.prhead_key(repo, sha), prs, ex=dm.PRHEAD_TTL_S)


def prs_for_head(repo: str, sha: str) -> list[int]:
    if not sha or not dm.valid_repo(repo):
        return []
    raw = store.rget_json(dm.prhead_key(repo, sha))
    # Older entries hold a single PR number.
    items = raw if isinstance(raw, list) else [raw]
    out: list[int] = []
    for item in items:
        try:
            num = int(item)
        except (TypeError, ValueError):
            continue
        if num > 0 and num not in out:
            out.append(num)
    return out


def mentions_agent(body: str) -> bool:
    """True when a comment addresses the agent (``@pr-agent`` or the App's @slug)."""
    names = {"pr-agent"}
    slug = github_app.app_slug()
    if slug:
        names.update({slug, f"{slug}[bot]"})
    key = "|".join(sorted(names))
    pattern = _MENTION_RE_CACHE.get(key)
    if pattern is None:
        alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        pattern = re.compile(rf"(?<![\w@])@(?:{alts})(?![\w-])", re.IGNORECASE)
        _MENTION_RE_CACHE[key] = pattern
    return bool(pattern.search(body or ""))


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def note_installation(payload: dict[str, Any], repo: str) -> None:
    """Every App delivery carries ``installation.id``: cache it for the repo."""
    inst = _int(payload, "installation", "id")
    if inst and dm.valid_repo(repo):
        github_app.remember_installation(repo, inst)


def _claim_delivery(delivery: str) -> bool:
    key = dm.delivery_key(delivery)
    if key is None:
        return True
    return store.rset(key, "1", ex=dm.DELIVERY_TTL_S, nx=True)


async def route(event: str, payload: dict[str, Any], delivery: str = "") -> dict[str, Any]:
    action = payload.get("action") if isinstance(payload.get("action"), str) else ""
    repo = _str(payload, "repository", "full_name")
    ctx = EventContext(
        event=event,
        action=action,
        payload=payload,
        delivery=delivery,
        repo=repo if dm.valid_repo(repo) else "",
        sender_is_bot=_str(payload, "sender", "type") == "Bot",
    )
    base = {"event": event, "action": action}
    if ctx.repo:
        await asyncio.to_thread(note_installation, payload, ctx.repo)
    handlers = _HANDLERS.get(event)
    if not handlers:
        return {**base, "skipped": "unhandled-event"}
    if ctx.sender_is_bot and event in _BOT_SENSITIVE:
        return {**base, "skipped": "bot"}
    if not await asyncio.to_thread(_claim_delivery, delivery):
        return {**base, "duplicate": True}
    out: dict[str, Any] = dict(base)
    for handler in handlers:
        try:
            out.update(await handler(ctx) or {})
        except Exception as exc:  # one broken handler must not 500 the webhook
            log.exception("event %s/%s handler %s failed", event, action, getattr(handler, "__name__", "?"))
            out.setdefault("errors", []).append(type(exc).__name__)
    return out


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@on("installation")
async def _installation(ctx: EventContext) -> dict[str, Any]:
    inst = _int(ctx.payload, "installation", "id")
    repos = [r for r in (ctx.payload.get("repositories") or []) if isinstance(r, dict)]
    names = [n for n in (_str(r, "full_name") for r in repos) if dm.valid_repo(n)]
    for name in names:
        if ctx.action in ("deleted", "suspend"):
            await asyncio.to_thread(github_app.forget_installation, name)
        elif ctx.action in ("created", "unsuspend", "new_permissions_accepted"):
            await asyncio.to_thread(github_app.remember_installation, name, inst)
    return {"repos": len(names)}


@on("installation_repositories")
async def _installation_repositories(ctx: EventContext) -> dict[str, Any]:
    inst = _int(ctx.payload, "installation", "id")
    added = [_str(r, "full_name") for r in (ctx.payload.get("repositories_added") or []) if isinstance(r, dict)]
    removed = [_str(r, "full_name") for r in (ctx.payload.get("repositories_removed") or []) if isinstance(r, dict)]
    for name in filter(dm.valid_repo, added):
        await asyncio.to_thread(github_app.remember_installation, name, inst)
    for name in filter(dm.valid_repo, removed):
        await asyncio.to_thread(github_app.forget_installation, name)
    return {"added": len(added), "removed": len(removed)}


@on("pull_request")
async def _pull_request(ctx: EventContext) -> dict[str, Any]:
    pr = ctx.payload.get("pull_request") if isinstance(ctx.payload.get("pull_request"), dict) else {}
    number = _int(pr, "number")
    if not ctx.repo or number <= 0:
        return {"recorded": 0}
    sha = _str(pr, "head", "sha")
    entry: dict[str, Any] = {"kind": "pull_request", "action": ctx.action, "sha": sha}
    if ctx.action == "closed":
        entry["merged"] = bool(pr.get("merged"))
        entry["merge_sha"] = _str(pr, "merge_commit_sha")
    if ctx.action in ("labeled", "unlabeled"):
        entry["label"] = _str(ctx.payload, "label", "name")
    await asyncio.to_thread(remember_head, ctx.repo, number, sha)
    # A closed PR needs no readiness recompute; everything else may change it.
    await asyncio.to_thread(record_pr_activity, ctx.repo, number, entry, dirty=ctx.action != "closed")
    if ctx.action == "closed":
        await asyncio.to_thread(store.rdelete, dm.dirty_key(ctx.repo, number))
    return {"recorded": 1, "prs": [number]}


@on("issue_comment")
async def _issue_comment(ctx: EventContext) -> dict[str, Any]:
    issue = ctx.payload.get("issue") if isinstance(ctx.payload.get("issue"), dict) else {}
    # Present (as an object) only for comments on pull requests; match the edge
    # worker, which treats any object as a PR even when it is empty.
    if not isinstance(issue.get("pull_request"), dict):
        return {"skipped": "not-a-pull-request"}
    number = _int(issue, "number")
    if not ctx.repo or number <= 0:
        return {"recorded": 0}
    comment = ctx.payload.get("comment") if isinstance(ctx.payload.get("comment"), dict) else {}
    body = _str(comment, "body")
    entry = {
        "kind": "comment",
        "action": ctx.action,
        "comment_id": _int(comment, "id"),
        "author": _str(comment, "user", "login"),
        "association": _str(comment, "author_association"),
        "mentions_agent": mentions_agent(body),
    }
    await asyncio.to_thread(record_pr_activity, ctx.repo, number, entry, dirty=False)
    return {"recorded": 1, "prs": [number], "mentions_agent": entry["mentions_agent"]}


@on("pull_request_review")
async def _pull_request_review(ctx: EventContext) -> dict[str, Any]:
    pr = ctx.payload.get("pull_request") if isinstance(ctx.payload.get("pull_request"), dict) else {}
    review = ctx.payload.get("review") if isinstance(ctx.payload.get("review"), dict) else {}
    number = _int(pr, "number")
    if not ctx.repo or number <= 0:
        return {"recorded": 0}
    entry = {
        "kind": "review",
        "action": ctx.action,
        "review_id": _int(review, "id"),
        "state": _str(review, "state").lower(),
        "author": _str(review, "user", "login"),
        "association": _str(review, "author_association"),
        "commit_id": _str(review, "commit_id"),
    }
    await asyncio.to_thread(record_pr_activity, ctx.repo, number, entry)
    return {"recorded": 1, "prs": [number]}


@on("pull_request_review_comment")
async def _pull_request_review_comment(ctx: EventContext) -> dict[str, Any]:
    pr = ctx.payload.get("pull_request") if isinstance(ctx.payload.get("pull_request"), dict) else {}
    comment = ctx.payload.get("comment") if isinstance(ctx.payload.get("comment"), dict) else {}
    number = _int(pr, "number")
    if not ctx.repo or number <= 0:
        return {"recorded": 0}
    entry = {
        "kind": "review_comment",
        "action": ctx.action,
        "comment_id": _int(comment, "id"),
        "in_reply_to": _int(comment, "in_reply_to_id") or None,
        "path": _str(comment, "path"),
        "author": _str(comment, "user", "login"),
        "mentions_agent": mentions_agent(_str(comment, "body")),
    }
    await asyncio.to_thread(record_pr_activity, ctx.repo, number, entry, dirty=False)
    return {"recorded": 1, "prs": [number], "mentions_agent": entry["mentions_agent"]}


@on("pull_request_review_thread")
async def _pull_request_review_thread(ctx: EventContext) -> dict[str, Any]:
    pr = ctx.payload.get("pull_request") if isinstance(ctx.payload.get("pull_request"), dict) else {}
    thread = ctx.payload.get("thread") if isinstance(ctx.payload.get("thread"), dict) else {}
    number = _int(pr, "number")
    if not ctx.repo or number <= 0:
        return {"recorded": 0}
    comments = thread.get("comments") if isinstance(thread.get("comments"), list) else []
    first = comments[0] if comments and isinstance(comments[0], dict) else {}
    entry = {
        "kind": "thread",
        "action": ctx.action,  # resolved | unresolved
        "thread_node_id": _str(thread, "node_id"),
        "path": _str(first, "path"),
        "first_comment_id": _int(first, "id"),
        "by": _str(ctx.payload, "sender", "login"),
    }
    await asyncio.to_thread(record_pr_activity, ctx.repo, number, entry)
    return {"recorded": 1, "prs": [number]}


def _check_prs(obj: dict[str, Any]) -> list[int]:
    prs = obj.get("pull_requests") if isinstance(obj.get("pull_requests"), list) else []
    return sorted({_int(p, "number") for p in prs if isinstance(p, dict)} - {0})


def _is_own_check(obj: dict[str, Any]) -> bool:
    """Our own merge-readiness check must not mark the PR dirty again (loop)."""
    slug = github_app.app_slug()
    return bool(slug) and _str(obj, "app", "slug") == slug


@on("check_suite")
async def _check_suite(ctx: EventContext) -> dict[str, Any]:
    suite = ctx.payload.get("check_suite") if isinstance(ctx.payload.get("check_suite"), dict) else {}
    if ctx.action != "completed" or not ctx.repo or _is_own_check(suite):
        return {"recorded": 0}
    prs = _check_prs(suite)
    entry = {
        "kind": "check_suite",
        "app": _str(suite, "app", "slug"),
        "conclusion": _str(suite, "conclusion"),
        "sha": _str(suite, "head_sha"),
    }
    for number in prs:
        await asyncio.to_thread(record_pr_activity, ctx.repo, number, entry)
    return {"recorded": len(prs), "prs": prs}


@on("check_run")
async def _check_run(ctx: EventContext) -> dict[str, Any]:
    run = ctx.payload.get("check_run") if isinstance(ctx.payload.get("check_run"), dict) else {}
    if ctx.action != "completed" or not ctx.repo or _is_own_check(run):
        return {"recorded": 0}
    prs = _check_prs(run)
    entry = {
        "kind": "check_run",
        "name": _str(run, "name"),
        "app": _str(run, "app", "slug"),
        "conclusion": _str(run, "conclusion"),
        "sha": _str(run, "head_sha"),
        "url": _str(run, "html_url"),
    }
    for number in prs:
        await asyncio.to_thread(record_pr_activity, ctx.repo, number, entry)
    return {"recorded": len(prs), "prs": prs}


@on("status")
async def _status(ctx: EventContext) -> dict[str, Any]:
    sha = _str(ctx.payload, "sha")
    if not ctx.repo or not sha:
        return {"recorded": 0}
    prs = await asyncio.to_thread(prs_for_head, ctx.repo, sha)
    if not prs:
        return {"recorded": 0, "skipped": "unknown-head"}
    entry = {
        "kind": "status",
        "context": _str(ctx.payload, "context"),
        "state": _str(ctx.payload, "state"),
        "sha": sha,
        "url": _str(ctx.payload, "target_url"),
    }
    for number in prs:
        await asyncio.to_thread(record_pr_activity, ctx.repo, number, entry)
    return {"recorded": len(prs), "prs": prs}


@on("push")
async def _push(ctx: EventContext) -> dict[str, Any]:
    if not ctx.repo:
        return {"recorded": 0}
    ref = _str(ctx.payload, "ref")
    default = _str(ctx.payload, "repository", "default_branch")
    if not default or ref != f"refs/heads/{default}":
        return {"skipped": "not-default-branch"}
    touched: set[str] = set()
    for commit in ctx.payload.get("commits") or []:
        if isinstance(commit, dict):
            for bucket in ("added", "modified", "removed"):
                touched.update(p for p in (commit.get(bucket) or []) if isinstance(p, str))
    invalidated = bool(touched & set(CONFIG_FILES))
    if invalidated:
        # Config is read from the base branch only; drop the cache so the next
        # review picks up the new rules.
        await asyncio.to_thread(store.rdelete, dm.repo_config_key(ctx.repo))
    return {"config_invalidated": invalidated}
