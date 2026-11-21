"""Merge-readiness publisher: Check Run / Commit Status, sticky checklist, and PR Inbox.

Responsibilities:
  * Persist every computed ``ReadinessReport`` in Redis (``merge:{repo}:{pr}``)
    and clear ``prdirty:{repo}:{pr}``.
  * Maintain the tracked PR Inbox index (``prinbox:{repo}:{pr}``) for the
    Desktop PR Inbox (``GET /prs/inbox``).
  * Render a crisp CodeRabbit/Macroscope-style sticky "Before you merge"
    Markdown checklist (``<!-- pr-agent-control:readiness -->``).
  * Publish either a GitHub App Check Run (``PR Agent · Merge readiness``) or a
    PAT commit status fallback (``pr-agent/merge-readiness``) respecting
    ``merge_gate`` (``off`` | ``advisory`` | ``required``).
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import httpx

try:  # cwd=backend
    import datamodel as dm
    import events
    import github_app
    import store
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import datamodel as dm  # type: ignore[no-redef]
    from backend import events  # type: ignore[no-redef]
    from backend import github_app  # type: ignore[no-redef]
    from backend import store  # type: ignore[no-redef]

from .score import compute_readiness, effective_score_threshold
from .signals import collect_signals

log = logging.getLogger("pr-agent.merge.publisher")

GITHUB_API = "https://api.github.com"
CHECK_RUN_NAME = "PR Agent · Merge readiness"
STATUS_CONTEXT = "pr-agent/merge-readiness"
READINESS_MARKER = "<!-- pr-agent-control:readiness -->"

_MAX_COMMENT_PAGES = 10
_CHECK_CACHE_TTL_S = 7 * 24 * 3600
_COMMENT_CACHE_TTL_S = dm.MERGE_REPORT_TTL_S
_PUBLISH_RETRY_TTL_S = 3600
_MAX_PUBLISH_RETRIES = 5
_PAT_LOGIN_TTL_S = 300.0
_NEXT_LINK_RE = re.compile(r'<[^>]+>\s*;\s*rel="?next"?', re.IGNORECASE)
_STATUS_STRICTNESS: dict[str, int] = {
    "success": 0,
    "pending": 1,
    "failure": 2,
    "error": 3,
}
_GATE_OK_ACTIONS = frozenset({"created", "updated", "posted", "unchanged"})
_UNSET: Any = object()

_pat_login_cache: dict[str, tuple[str, float]] = {}
_pat_login_lock = threading.Lock()

_BAND_LABELS: dict[str, str] = {
    "ready": "Ready",
    "ready_with_nits": "Ready with nits",
    "needs_work": "Needs work",
    "blocked": "Blocked",
}

_SECTION_META: tuple[tuple[str, str], ...] = (
    ("blocker", "Blockers"),
    ("should_fix", "Should fix"),
    ("nice_to_have", "Nice to have"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _safe_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    u = url.strip()
    if u.startswith(("https://", "http://")) and not any(c.isspace() for c in u):
        return u.replace(")", "%29")
    return None


def inbox_key(repo: str, pr_number: int) -> str:
    if not dm.valid_repo(repo):
        raise ValueError("invalid repository name")
    return f"prinbox:{repo.lower()}:{int(pr_number)}"


def _check_cache_key(repo: str, pr_number: int, head_sha: str) -> str:
    return f"mergecheck:{repo.lower()}:{int(pr_number)}:{head_sha.lower()}"


def _status_cache_key(repo: str, head_sha: str, context: str = STATUS_CONTEXT) -> str:
    return f"mergestatus:{repo.lower()}:{head_sha.lower()}:{context.lower()}"


def _status_pr_decision_key(repo: str, head_sha: str, pr_number: int) -> str:
    return f"mergestatuspr:{repo.lower()}:{head_sha.lower()}:{int(pr_number)}"


def _comment_cache_key(repo: str, pr_number: int) -> str:
    return f"mergecomment:{repo.lower()}:{int(pr_number)}"


def _pr_state_lock_key(repo: str, pr_number: int) -> str:
    return f"prstate:{repo.lower()}:{int(pr_number)}"


def _publish_retry_key(repo: str, pr_number: int) -> str:
    return f"mergretry:{repo.lower()}:{int(pr_number)}"


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_iso_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _has_next_link(resp: Any) -> bool | None:
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    link = headers.get("link") or headers.get("Link") or ""
    if not isinstance(link, str) or not link.strip():
        return None
    return bool(_NEXT_LINK_RE.search(link))


def get_stored_readiness(repo: str, pr_number: int) -> dict[str, Any] | None:
    """Read the last persisted ``ReadinessReport`` JSON dict for a PR, or None."""
    if not dm.valid_repo(repo) or int(pr_number) <= 0:
        return None
    raw = store.rget_json(dm.merge_report_key(repo, pr_number))
    return raw if isinstance(raw, dict) else None


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def render_readiness_comment(report: dm.ReadinessReport) -> str:
    """Render a sticky CodeRabbit/Macroscope-style 'Before you merge' checklist."""
    band_label = _BAND_LABELS.get(report.band, report.band.replace("_", " ").title())
    lines: list[str] = [
        READINESS_MARKER,
        f"## PR Agent Merge Readiness: {report.score}/100 · {band_label}",
        "",
    ]

    if not report.items:
        lines.append("All pre-merge checks passed — no open blockers or issues found.")
    else:
        for kind, heading in _SECTION_META:
            bucket = [item for item in report.items if item.kind == kind]
            if not bucket:
                continue
            lines.append(f"### {heading}")
            for item in bucket:
                mark = "x" if item.done else " "
                text = " ".join(item.text.splitlines()).strip()
                link = _safe_url(item.url)
                if link:
                    lines.append(f"- [{mark}] {text} ([view]({link}))")
                else:
                    lines.append(f"- [{mark}] {text}")
            lines.append("")

    short_sha = report.head_sha[:7] if report.head_sha else "unknown"
    footer_parts = [f"Commit `{short_sha}`", f"Policy `{report.policy_version}`"]
    if report.computed_at:
        footer_parts.append(f"Updated {report.computed_at}")
    lines.append("<sub>" + " · ".join(footer_parts) + "</sub>")
    return "\n".join(lines).rstrip() + "\n"


def _render_check_summary(
    report: dm.ReadinessReport,
    *,
    score_threshold: int,
    merge_gate: str,
    incomplete_signals: list[str] | None = None,
) -> tuple[str, str, str]:
    """Return ``(title, summary, text)`` for a GitHub Check Run output object."""
    band_label = _BAND_LABELS.get(report.band, report.band)
    open_blockers = [i for i in report.items if i.kind == "blocker" and not i.done]
    open_should = [i for i in report.items if i.kind == "should_fix" and not i.done]
    open_nits = [i for i in report.items if i.kind == "nice_to_have" and not i.done]

    title = f"{report.score}/100 · {band_label}"
    summary = (
        f"Score **{report.score}/100** (threshold {score_threshold}, gate `{merge_gate}`) — "
        f"{len(open_blockers)} blocker(s), {len(open_should)} should-fix, {len(open_nits)} nice-to-have."
    )
    if incomplete_signals:
        missing = ", ".join(incomplete_signals)
        if merge_gate == "required":
            summary += f" Incomplete signals ({missing}) — required gate withheld."
        else:
            summary += f" Partial data (incomplete signals: {missing})."
    body = render_readiness_comment(report)
    if body.startswith(READINESS_MARKER):
        body = body[len(READINESS_MARKER):].lstrip("\n")
    return title, summary, body


# ---------------------------------------------------------------------------
# GitHub publishing helpers
# ---------------------------------------------------------------------------


async def _publish_check_run(
    client: httpx.AsyncClient,
    repo: str,
    pr_number: int,
    report: dm.ReadinessReport,
    *,
    headers: dict[str, str],
    passes_gate: bool,
    merge_gate: str,
    score_threshold: int,
    incomplete_signals: list[str] | None = None,
) -> dict[str, Any]:
    if not report.head_sha:
        return {"action": "skipped", "reason": "missing_head_sha"}

    if passes_gate:
        conclusion = "success"
    elif merge_gate == "required":
        conclusion = "failure"
    else:
        conclusion = "neutral"

    title, summary, text = _render_check_summary(
        report,
        score_threshold=score_threshold,
        merge_gate=merge_gate,
        incomplete_signals=incomplete_signals,
    )
    completed_at = report.computed_at or _now_iso()
    payload: dict[str, Any] = {
        "name": CHECK_RUN_NAME,
        "head_sha": report.head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": completed_at,
        "output": {
            "title": title,
            "summary": summary,
            "text": text,
        },
    }

    cache_key = _check_cache_key(repo, pr_number, report.head_sha)
    raw_cached_id = store.rget(cache_key)
    cached_run_id: int | None = None
    if raw_cached_id is not None:
        if raw_cached_id.isascii() and raw_cached_id.isdigit() and int(raw_cached_id) > 0:
            cached_run_id = int(raw_cached_id)
        else:
            store.rdelete(cache_key)

    try:
        if cached_run_id is not None:
            patch_payload = {k: v for k, v in payload.items() if k not in ("name", "head_sha")}
            resp = await client.patch(
                f"{GITHUB_API}/repos/{repo}/check-runs/{cached_run_id}",
                headers=headers,
                json=patch_payload,
            )
            if resp.status_code in (200, 201):
                data = resp.json() if resp.content else {}
                run_id = data.get("id") or cached_run_id
                return {
                    "action": "updated",
                    "id": run_id,
                    "conclusion": conclusion,
                    "status_code": resp.status_code,
                }
            # Cached check run vanished or cannot be patched: fall through to create.
            store.rdelete(cache_key)

        resp = await client.post(
            f"{GITHUB_API}/repos/{repo}/check-runs",
            headers=headers,
            json=payload,
        )
        if resp.status_code in (200, 201):
            data = resp.json() if resp.content else {}
            run_id = data.get("id")
            if run_id:
                store.rset(cache_key, str(run_id), ex=_CHECK_CACHE_TTL_S)
            return {
                "action": "created",
                "id": run_id,
                "conclusion": conclusion,
                "status_code": resp.status_code,
            }
        log.warning("merge.publisher: check-run POST failed HTTP %s for %s#%s", resp.status_code, repo, pr_number)
        return {
            "action": "failed",
            "conclusion": conclusion,
            "status_code": resp.status_code,
        }
    except Exception as exc:
        log.warning("merge.publisher: check-run error (%s) for %s#%s", type(exc).__name__, repo, pr_number)
        return {"action": "error", "conclusion": conclusion, "error": type(exc).__name__}


def _aggregate_shared_head_status(
    repo: str,
    head_sha: str,
    pr_number: int,
    own_decision: dict[str, Any],
) -> tuple[str, str, str | None]:
    """Aggregate the strictest commit status across open PRs sharing ``head_sha``."""
    store.rset_json(
        _status_pr_decision_key(repo, head_sha, pr_number),
        own_decision,
        ex=_CHECK_CACHE_TTL_S,
    )

    candidate_prs = list(events.prs_for_head(repo, head_sha))
    if pr_number not in candidate_prs:
        candidate_prs.append(pr_number)

    active: list[dict[str, Any]] = []
    sha_low = head_sha.strip().lower()
    for sib_pr in candidate_prs:
        if sib_pr == pr_number:
            active.append(own_decision)
            continue
        sib_inbox = store.rget_json(inbox_key(repo, sib_pr))
        if isinstance(sib_inbox, dict):
            sib_state = str(sib_inbox.get("state") or "open").strip().lower()
            if sib_state in ("closed", "merged"):
                continue
            sib_sha = str(sib_inbox.get("head_sha") or "").strip().lower()
            if sib_sha and sib_sha != sha_low:
                continue

        dec = store.rget_json(_status_pr_decision_key(repo, head_sha, sib_pr))
        if isinstance(dec, dict) and dec.get("state"):
            active.append(dec)
        elif isinstance(sib_inbox, dict):
            sib_gate = str(sib_inbox.get("merge_gate") or "advisory").strip().lower()
            sib_score = _safe_int(sib_inbox.get("score"), 0)
            sib_thresh = effective_score_threshold(_safe_int(sib_inbox.get("score_threshold"), 85))
            sib_blockers = _safe_int(sib_inbox.get("blockers_count"), 0)
            sib_band = str(sib_inbox.get("band") or "blocked")
            sib_band_label = _BAND_LABELS.get(sib_band, sib_band)
            sib_passes = (sib_score >= sib_thresh) and sib_blockers == 0 and sib_band != "blocked"
            if sib_gate == "required" and not sib_passes:
                st = "failure"
                desc = f"Merge readiness: {sib_score}/100 ({sib_band_label}, needs {sib_thresh}+)"
            else:
                st = "success"
                desc = f"Merge readiness: {sib_score}/100 ({sib_band_label})"
            active.append({
                "pr_number": sib_pr,
                "state": st,
                "description": desc,
                "score": sib_score,
                "target_url": _safe_url(sib_inbox.get("url")),
            })

    if len(active) <= 1:
        return (
            str(own_decision["state"]),
            str(own_decision["description"])[:140],
            _safe_url(own_decision.get("target_url")),
        )

    # Pick strictest state first (error > failure > pending > success),
    # then lowest score, then lowest PR number for deterministic payloads.
    strictest = min(
        active,
        key=lambda d: (
            -_STATUS_STRICTNESS.get(str(d.get("state") or "success"), 0),
            _safe_int(d.get("score"), 100),
            _safe_int(d.get("pr_number"), 0),
        ),
    )
    winner_pr = _safe_int(strictest.get("pr_number"), pr_number)
    winner_state = str(strictest.get("state") or "success")
    winner_desc = str(strictest.get("description") or "")
    if winner_pr != pr_number or winner_state != "success":
        winner_desc = f"PR #{winner_pr}: {winner_desc}"
    return winner_state, winner_desc[:140], _safe_url(strictest.get("target_url"))


async def _publish_commit_status(
    client: httpx.AsyncClient,
    repo: str,
    pr_number: int,
    report: dm.ReadinessReport,
    *,
    headers: dict[str, str],
    passes_gate: bool,
    merge_gate: str,
    score_threshold: int,
    target_url: str | None = None,
    incomplete_signals: list[str] | None = None,
) -> dict[str, Any]:
    if not report.head_sha:
        return {"action": "skipped", "reason": "missing_head_sha"}

    band_label = _BAND_LABELS.get(report.band, report.band)
    if passes_gate:
        state = "success"
        description = f"Merge readiness: {report.score}/100 ({band_label})"
    elif merge_gate == "required":
        state = "failure"
        if incomplete_signals:
            missing = ", ".join(incomplete_signals)
            description = f"Merge readiness incomplete ({missing}); required gate withheld"
        else:
            description = f"Merge readiness: {report.score}/100 ({band_label}, needs {score_threshold}+)"
    else:
        state = "success"
        if incomplete_signals:
            missing = ", ".join(incomplete_signals)
            description = f"Advisory · Merge readiness: {report.score}/100 ({band_label}, partial: {missing})"
        else:
            description = f"Advisory · Merge readiness: {report.score}/100 ({band_label})"

    own_decision: dict[str, Any] = {
        "pr_number": int(pr_number),
        "state": state,
        "description": description[:140],
        "score": int(report.score),
        "target_url": _safe_url(target_url),
    }
    agg_state, agg_desc, agg_target = _aggregate_shared_head_status(
        repo, report.head_sha, int(pr_number), own_decision
    )

    payload: dict[str, Any] = {
        "state": agg_state,
        "context": STATUS_CONTEXT,
        "description": agg_desc[:140],
    }
    if agg_target:
        payload["target_url"] = agg_target

    confirmed_record = {
        "sha": report.head_sha.lower(),
        "context": STATUS_CONTEXT,
        "state": payload["state"],
        "description": payload["description"],
        "target_url": payload.get("target_url") or "",
    }
    status_cache_key = _status_cache_key(repo, report.head_sha, STATUS_CONTEXT)
    last_confirmed = store.rget_json(status_cache_key)
    if isinstance(last_confirmed, dict) and last_confirmed == confirmed_record:
        return {
            "action": "unchanged",
            "state": agg_state,
            "context": STATUS_CONTEXT,
            "description": payload["description"],
        }

    q_sha = urllib.parse.quote(report.head_sha, safe="")
    try:
        resp = await client.post(
            f"{GITHUB_API}/repos/{repo}/statuses/{q_sha}",
            headers=headers,
            json=payload,
        )
        if resp.status_code in (200, 201):
            store.rset_json(status_cache_key, confirmed_record, ex=_CHECK_CACHE_TTL_S)
            return {
                "action": "posted",
                "state": agg_state,
                "context": STATUS_CONTEXT,
                "description": payload["description"],
                "status_code": resp.status_code,
            }
        log.warning("merge.publisher: commit status POST failed HTTP %s for %s#%s", resp.status_code, repo, pr_number)
        return {
            "action": "failed",
            "state": agg_state,
            "context": STATUS_CONTEXT,
            "status_code": resp.status_code,
        }
    except Exception as exc:
        log.warning("merge.publisher: commit status error (%s) for %s#%s", type(exc).__name__, repo, pr_number)
        return {"action": "error", "state": agg_state, "context": STATUS_CONTEXT, "error": type(exc).__name__}


async def _authenticated_pat_login(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    token: str,
) -> str:
    """Resolve and briefly cache the GitHub login for a PAT via ``GET /user``."""
    if not token:
        return ""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    now = time.monotonic()
    with _pat_login_lock:
        cached = _pat_login_cache.get(digest)
        if cached and cached[1] > now:
            return cached[0]
    try:
        resp = await client.get(f"{GITHUB_API}/user", headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                login = str(data.get("login") or "").strip().lower()
                if login:
                    with _pat_login_lock:
                        _pat_login_cache[digest] = (login, now + _PAT_LOGIN_TTL_S)
                    return login
    except Exception as exc:
        log.debug("merge.publisher: GET /user login lookup failed (%s)", type(exc).__name__)
    return ""


def _is_own_readiness_comment(
    comment: dict[str, Any],
    *,
    auth_kind: str,
    own_app_slug: str,
    own_app_id: int,
    authenticated_login: str,
) -> bool:
    """Verify a readiness comment was authored by this publisher before updating it."""
    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    user_login = str(user.get("login") or "").strip().lower()
    user_type = str(user.get("type") or "").strip()
    via_app = (
        comment.get("performed_via_github_app")
        if isinstance(comment.get("performed_via_github_app"), dict)
        else {}
    )
    via_app_slug = str(via_app.get("slug") or "").strip().lower()
    via_app_id = _safe_int(via_app.get("id"), 0)

    if auth_kind == "app":
        if own_app_id > 0 and via_app_id > 0:
            return via_app_id == own_app_id
        if own_app_slug and via_app_slug:
            return via_app_slug == own_app_slug
        if own_app_slug:
            if user_login:
                return user_login == f"{own_app_slug}[bot]"
            return not user_type
        if user_login or user_type:
            return user_type == "Bot" or user_login.endswith("[bot]")
        return True

    if authenticated_login:
        return user_login == authenticated_login.lower()
    # Without a verified authenticated login, reject comments attributed to another user.
    return not user_login and not user_type


async def _find_readiness_comment(
    client: httpx.AsyncClient,
    comments_url: str,
    headers: dict[str, str],
    *,
    auth_kind: str = "",
    authenticated_login: str = "",
) -> tuple[int | None, bool]:
    """Scan PR comments for our sticky readiness comment.

    Returns ``(comment_id, lookup_complete)``. ``lookup_complete`` is False if
    any page fails or the page cap is hit before exhausting comments.
    """
    found: int | None = None
    own_app_slug = github_app.app_slug().strip().lower()
    own_app_id = _safe_int(github_app.app_id(), 0)
    try:
        for page in range(1, _MAX_COMMENT_PAGES + 1):
            resp = await client.get(
                comments_url,
                headers=headers,
                params={"per_page": 100, "page": page},
            )
            if resp.status_code != 200:
                log.warning(
                    "merge.publisher: comment lookup HTTP %s on page %d for %s",
                    resp.status_code,
                    page,
                    comments_url,
                )
                return found, False
            batch = resp.json()
            if not isinstance(batch, list):
                log.warning("merge.publisher: unexpected comment payload on page %d for %s", page, comments_url)
                return found, False
            for comment in batch:
                if (
                    isinstance(comment, dict)
                    and READINESS_MARKER in str(comment.get("body") or "")
                    and _is_own_readiness_comment(
                        comment,
                        auth_kind=auth_kind,
                        own_app_slug=own_app_slug,
                        own_app_id=own_app_id,
                        authenticated_login=authenticated_login,
                    )
                ):
                    with contextlib.suppress(KeyError, TypeError, ValueError):
                        cid = int(comment["id"])
                        if cid > 0:
                            found = cid
            has_next = _has_next_link(resp)
            if has_next is False:
                return found, True
            if has_next is None and len(batch) < 100:
                return found, True
            if page == _MAX_COMMENT_PAGES:
                log.warning(
                    "merge.publisher: comment lookup hit page cap (%d) for %s",
                    _MAX_COMMENT_PAGES,
                    comments_url,
                )
                return found, False
    except Exception as exc:
        log.warning("merge.publisher: comment lookup error (%s) for %s", type(exc).__name__, comments_url)
        return found, False
    return found, False


async def _upsert_readiness_comment(
    client: httpx.AsyncClient,
    repo: str,
    pr_number: int,
    report: dm.ReadinessReport,
    *,
    headers: dict[str, str],
    auth_kind: str = "",
    token: str = "",
) -> dict[str, Any]:
    body = render_readiness_comment(report)
    comments_url = f"{GITHUB_API}/repos/{repo}/issues/{int(pr_number)}/comments"
    cache_key = _comment_cache_key(repo, pr_number)

    try:
        existing_id: int | None = None
        cached = store.rget(cache_key)
        if cached:
            try:
                existing_id = int(cached)
            except ValueError:
                store.rdelete(cache_key)

        if existing_id is not None:
            resp = await client.patch(
                f"{GITHUB_API}/repos/{repo}/issues/comments/{existing_id}",
                headers=headers,
                json={"body": body},
            )
            if resp.status_code in (200, 201):
                return {"action": "updated", "id": existing_id, "status_code": resp.status_code}
            # Comment deleted or stale cache: fall back to scanning PR comments.
            store.rdelete(cache_key)

        pat_login = (
            await _authenticated_pat_login(client, headers, token)
            if auth_kind != "app" and token
            else ""
        )
        found_id, lookup_complete = await _find_readiness_comment(
            client,
            comments_url,
            headers,
            auth_kind=auth_kind,
            authenticated_login=pat_login,
        )
        if found_id is not None:
            resp = await client.patch(
                f"{GITHUB_API}/repos/{repo}/issues/comments/{found_id}",
                headers=headers,
                json={"body": body},
            )
            if resp.status_code in (200, 201):
                store.rset(cache_key, str(found_id), ex=_COMMENT_CACHE_TTL_S)
                return {"action": "updated", "id": found_id, "status_code": resp.status_code}
            return {"action": "failed", "id": found_id, "status_code": resp.status_code}

        if not lookup_complete:
            log.warning(
                "merge.publisher: skipping sticky comment creation for %s#%s after incomplete lookup",
                repo,
                pr_number,
            )
            return {"action": "skipped", "reason": "comment_lookup_incomplete"}

        resp = await client.post(comments_url, headers=headers, json={"body": body})
        if resp.status_code in (200, 201):
            data = resp.json() if resp.content else {}
            new_id = data.get("id")
            if new_id:
                store.rset(cache_key, str(new_id), ex=_COMMENT_CACHE_TTL_S)
            return {"action": "created", "id": new_id, "status_code": resp.status_code}
        log.warning("merge.publisher: sticky comment POST failed HTTP %s for %s#%s", resp.status_code, repo, pr_number)
        return {"action": "failed", "status_code": resp.status_code}
    except Exception as exc:
        log.warning("merge.publisher: sticky comment error (%s) for %s#%s", type(exc).__name__, repo, pr_number)
        return {"action": "error", "error": type(exc).__name__}


# ---------------------------------------------------------------------------
# Inbox persistence & queries
# ---------------------------------------------------------------------------


def _derive_findings_counts(
    report: dm.ReadinessReport,
    findings_summary: dict[str, Any] | None = None,
) -> dict[str, int]:
    if isinstance(findings_summary, dict):
        crit = int(findings_summary.get("critical") or 0)
        maj = int(findings_summary.get("major") or 0)
        minr = int(findings_summary.get("minor") or 0)
        nit = int(findings_summary.get("nit") or 0)
        total = int(findings_summary.get("total") if findings_summary.get("total") is not None else (crit + maj + minr + nit))
        return {"critical": crit, "major": maj, "minor": minr, "nit": nit, "total": total}

    counts = {"critical": 0, "major": 0, "minor": 0, "nit": 0}
    code_map = {
        "critical_finding": "critical",
        "major_finding": "major",
        "minor_finding": "minor",
        "nit_finding": "nit",
    }
    for item in report.items:
        if item.done:
            continue
        bucket = code_map.get(item.code)
        if bucket:
            counts[bucket] += 1
    counts["total"] = sum(counts.values())
    return counts


def _upsert_inbox_record(
    repo: str,
    pr_number: int,
    report: dm.ReadinessReport,
    *,
    merge_gate: str,
    score_threshold: int,
    pr_meta: dict[str, Any] | None = None,
    findings_summary: dict[str, Any] | None = None,
    snapshot_at: str = "",
) -> dict[str, Any]:
    key = inbox_key(repo, pr_number)
    existing = store.rget_json(key)
    prev = existing if isinstance(existing, dict) else {}
    meta = pr_meta if isinstance(pr_meta, dict) else {}

    open_blockers = [i for i in report.items if i.kind == "blocker" and not i.done]
    title = str(meta.get("title") or meta.get("pr_title") or prev.get("title") or "")
    url = str(
        meta.get("url")
        or meta.get("pr_url")
        or meta.get("html_url")
        or prev.get("url")
        or f"https://github.com/{repo}/pull/{int(pr_number)}"
    )
    author = str(meta.get("author") or meta.get("pr_author") or prev.get("author") or "")
    state = str(meta.get("state") or meta.get("pr_state") or prev.get("state") or "open")
    base_ref = str(meta.get("base_ref") or prev.get("base_ref") or "")
    head_ref = str(meta.get("head_ref") or prev.get("head_ref") or "")
    updated_at = report.computed_at or _now_iso()

    record: dict[str, Any] = {
        "repo": repo,
        "pr_number": int(pr_number),
        "head_sha": report.head_sha,
        "title": title,
        "url": url,
        "author": author,
        "state": state,
        "base_ref": base_ref,
        "head_ref": head_ref,
        "score": report.score,
        "band": report.band,
        "blockers_count": len(open_blockers),
        "top_blockers": [i.text for i in open_blockers[:3]],
        "findings_counts": _derive_findings_counts(report, findings_summary),
        "updated_at": updated_at,
        "snapshot_at": snapshot_at or updated_at,
        "merge_gate": merge_gate,
        "score_threshold": int(score_threshold),
    }
    if prev.get("closed_at") and state in ("closed", "merged"):
        record["closed_at"] = prev["closed_at"]
    if prev.get("merged") and state == "merged":
        record["merged"] = True
    store.rset_json(key, record, ex=dm.MERGE_REPORT_TTL_S)
    return record


def _is_stale_snapshot(
    existing_report: Any,
    existing_inbox: Any,
    *,
    head_sha: str,
    incoming_state: str,
    snapshot_at: str,
) -> bool:
    """True when an incoming refresh snapshot would clobber newer or closed PR state."""
    prev_report = existing_report if isinstance(existing_report, dict) else {}
    prev_inbox = existing_inbox if isinstance(existing_inbox, dict) else {}

    prev_sha = str(prev_report.get("head_sha") or prev_inbox.get("head_sha") or "").strip()
    cur_sha = str(head_sha or "").strip()
    if not cur_sha and prev_sha:
        return True

    snap_ts = _parse_iso_ts(snapshot_at)

    prev_state = str(prev_inbox.get("state") or "").strip().lower()
    inc_state = str(incoming_state or "").strip().lower()
    if prev_state == "merged" and inc_state == "open":
        return True
    if prev_state in ("closed", "merged") and inc_state == "open":
        closed_ts = _parse_iso_ts(prev_inbox.get("closed_at") or prev_inbox.get("updated_at"))
        if closed_ts is not None and (snap_ts is None or snap_ts <= closed_ts):
            return True

    prev_ts = _parse_iso_ts(
        prev_inbox.get("snapshot_at")
        or prev_report.get("computed_at")
        or prev_inbox.get("updated_at")
    )
    return prev_ts is not None and snap_ts is not None and snap_ts < prev_ts


def list_inbox_prs(
    *,
    repo: str | None = None,
    state: str | None = "open",
    band: str | None = None,
) -> list[dict[str, Any]]:
    """List tracked PR Inbox entries for the Desktop PR Inbox (``GET /prs/inbox``)."""
    if repo:
        if not dm.valid_repo(repo):
            raise ValueError("invalid repository name")
        pattern = f"prinbox:{repo.lower()}:*"
    else:
        pattern = "prinbox:*"

    keys = store.rkeys(pattern)
    if not keys:
        return []

    rows = store.rget_json_many(keys)
    wanted_state = (state or "").strip().lower()
    wanted_band = (band or "").strip().lower()

    out: list[dict[str, Any]] = []
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        rec_state = str(rec.get("state") or "open").strip().lower()
        if wanted_state and wanted_state != "all":
            if wanted_state == "closed":
                if rec_state not in ("closed", "merged"):
                    continue
            elif wanted_state == "merged":
                if rec_state != "merged" and not rec.get("merged"):
                    continue
            elif rec_state != wanted_state:
                continue
        if wanted_band and wanted_band != "all":
            if str(rec.get("band") or "").strip().lower() != wanted_band:
                continue
        out.append(rec)

    out.sort(
        key=lambda r: (str(r.get("updated_at") or ""), int(r.get("pr_number") or 0)),
        reverse=True,
    )
    return out


def mark_pr_closed(repo: str, pr_number: int, merged: bool = False) -> dict[str, Any] | None:
    """Mark a tracked PR closed/merged in the PR Inbox and clear its dirty flag."""
    if not dm.valid_repo(repo):
        raise ValueError("invalid repository name")
    pr_num = int(pr_number)
    if pr_num <= 0:
        raise ValueError("invalid pull request number")

    with store.key_lock(_pr_state_lock_key(repo, pr_num)):
        store.rdelete(dm.dirty_key(repo, pr_num))
        store.rdelete(_publish_retry_key(repo, pr_num))
        key = inbox_key(repo, pr_num)
        existing = store.rget_json(key)
        if not isinstance(existing, dict):
            return None
        now = _now_iso()
        updated = dict(existing)
        updated["state"] = "merged" if merged else "closed"
        updated["merged"] = bool(merged)
        updated["closed_at"] = now
        updated["updated_at"] = now
        updated["snapshot_at"] = now
        store.rset_json(key, updated, ex=dm.MERGE_REPORT_TTL_S)
        return updated


# ---------------------------------------------------------------------------
# Main publish & end-to-end orchestration
# ---------------------------------------------------------------------------


def _finalize_dirty_flag(
    repo: str,
    pr_num: int,
    captured_dirty_token: str | None,
    *,
    retry_needed: bool,
) -> None:
    """Clear ``prdirty`` atomically on clean completion, or keep it set with bounded retries."""
    dkey = dm.dirty_key(repo, pr_num)
    rkey = _publish_retry_key(repo, pr_num)
    if not retry_needed:
        store.rdelete(rkey)
        if captured_dirty_token is not None:
            store.rdelete_if_equal(dkey, captured_dirty_token)
        return

    attempts = store.rincr(rkey, 1, ex=_PUBLISH_RETRY_TTL_S)
    if attempts <= _MAX_PUBLISH_RETRIES:
        # Keep existing dirty marker intact, or mark dirty so the sweeper retries.
        store.rset(dkey, _now_iso(), ex=dm.DIRTY_TTL_S, nx=True)
        return

    log.warning(
        "merge.publisher: retry budget (%d) exhausted for %s#%d; clearing dirty flag",
        _MAX_PUBLISH_RETRIES,
        repo,
        pr_num,
    )
    if captured_dirty_token is not None:
        store.rdelete_if_equal(dkey, captured_dirty_token)


async def publish_readiness(
    repo: str,
    pr_number: int,
    report: dm.ReadinessReport,
    *,
    merge_gate: str = "advisory",
    score_threshold: int = 85,
    github_token: str | None = None,
    auth_kind: str | None = None,
    pr_meta: dict[str, Any] | None = None,
    findings_summary: dict[str, int] | None = None,
    client: httpx.AsyncClient | None = None,
    dirty_token: Any = _UNSET,
    snapshot_at: str | None = None,
    incomplete_signals: list[str] | None = None,
) -> dict[str, Any]:
    """Persist ``report`` + PR Inbox entry and optionally publish Check Run/Status + comment."""
    if not dm.valid_repo(repo):
        raise ValueError("invalid repository name")
    pr_num = int(pr_number)
    if pr_num <= 0:
        raise ValueError("invalid pull request number")

    gate = str(merge_gate or "advisory").strip().lower()
    if gate not in ("off", "advisory", "required"):
        gate = "advisory"
    threshold = effective_score_threshold(score_threshold)
    incomplete = [str(s).strip() for s in (incomplete_signals or []) if str(s).strip()]

    dkey = dm.dirty_key(repo, pr_num)
    captured_dirty_token: str | None = (
        store.rget(dkey) if dirty_token is _UNSET else (str(dirty_token) if dirty_token is not None else None)
    )
    effective_snapshot_at = str(snapshot_at or report.computed_at or _now_iso())
    meta = pr_meta if isinstance(pr_meta, dict) else {}
    incoming_state = str(meta.get("state") or meta.get("pr_state") or "")

    # 1. Guard report + PR inbox persistence against stale snapshots and close races.
    with store.key_lock(_pr_state_lock_key(repo, pr_num)):
        existing_report = store.rget_json(dm.merge_report_key(repo, pr_num))
        existing_inbox = store.rget_json(inbox_key(repo, pr_num))
        if _is_stale_snapshot(
            existing_report,
            existing_inbox,
            head_sha=report.head_sha,
            incoming_state=incoming_state,
            snapshot_at=effective_snapshot_at,
        ):
            log.info("merge.publisher: ignoring stale readiness snapshot for %s#%d", repo, pr_num)
            return {
                "report": existing_report if isinstance(existing_report, dict) else report.model_dump(),
                "inbox": existing_inbox if isinstance(existing_inbox, dict) else None,
                "published": False,
                "gate_published": False,
                "stale": True,
                "merge_gate": gate,
                "auth_kind": auth_kind or "none",
                "check_run": None,
                "commit_status": None,
                "comment": None,
            }

        store.rset_json(
            dm.merge_report_key(repo, pr_num),
            report.model_dump(),
            ex=dm.MERGE_REPORT_TTL_S,
        )
        inbox_record = _upsert_inbox_record(
            repo,
            pr_num,
            report,
            merge_gate=gate,
            score_threshold=threshold,
            pr_meta=pr_meta,
            findings_summary=findings_summary,
            snapshot_at=effective_snapshot_at,
        )
        if report.head_sha:
            events.remember_head(repo, pr_num, report.head_sha)

    check_result: dict[str, Any] | None = None
    status_result: dict[str, Any] | None = None
    comment_result: dict[str, Any] | None = None
    resolved_kind = auth_kind or "none"
    token = ""

    # 2. Publish to GitHub when enabled, head SHA is verified, and credentials exist.
    if gate != "off" and report.head_sha:
        if github_token is None:
            token, detected_kind = await github_app.token_for_repo(repo)
            resolved_kind = auth_kind or detected_kind
        else:
            token = github_token
            if auth_kind:
                resolved_kind = auth_kind
            elif token:
                resolved_kind = "app" if github_app.app_configured() else "pat"
            else:
                resolved_kind = "none"

        if token:
            headers = _gh_headers(token)
            has_open_blockers = any(i.kind == "blocker" and not i.done for i in report.items)
            passes_gate = (
                (report.score >= threshold)
                and not has_open_blockers
                and report.band != "blocked"
                and not incomplete
            )

            own_client = client is None
            http = client or httpx.AsyncClient(timeout=20.0)
            try:
                if resolved_kind == "app":
                    check_result = await _publish_check_run(
                        http,
                        repo,
                        pr_num,
                        report,
                        headers=headers,
                        passes_gate=passes_gate,
                        merge_gate=gate,
                        score_threshold=threshold,
                        incomplete_signals=incomplete or None,
                    )
                else:
                    status_result = await _publish_commit_status(
                        http,
                        repo,
                        pr_num,
                        report,
                        headers=headers,
                        passes_gate=passes_gate,
                        merge_gate=gate,
                        score_threshold=threshold,
                        target_url=inbox_record.get("url"),
                        incomplete_signals=incomplete or None,
                    )

                comment_result = await _upsert_readiness_comment(
                    http,
                    repo,
                    pr_num,
                    report,
                    headers=headers,
                    auth_kind=resolved_kind,
                    token=token,
                )
            finally:
                if own_client:
                    await http.aclose()

    gate_result = check_result if resolved_kind == "app" else status_result
    gate_published = isinstance(gate_result, dict) and gate_result.get("action") in _GATE_OK_ACTIONS
    comment_published = isinstance(comment_result, dict) and comment_result.get("action") in (
        "created",
        "updated",
    )

    if gate == "off":
        published = False
        retry_needed = False
    elif gate == "required":
        # A required gate is only considered published when the Check Run / Commit Status succeeded.
        published = gate_published
        retry_needed = bool(token and (not gate_published or bool(incomplete)))
    else:
        published = gate_published or comment_published
        retry_needed = bool(token and not published)

    # 3. Clear dirty marker ONLY after publishing finishes cleanly (or keep set for bounded retry).
    _finalize_dirty_flag(repo, pr_num, captured_dirty_token, retry_needed=retry_needed)

    return {
        "report": report.model_dump(),
        "inbox": inbox_record,
        "published": published,
        "gate_published": gate_published,
        "merge_gate": gate,
        "auth_kind": resolved_kind,
        "check_run": check_result,
        "commit_status": status_result,
        "comment": comment_result,
    }


async def recompute_pr_readiness(
    repo: str,
    pr_number: int,
    *,
    agent_config: dict[str, Any] | None = None,
    diff_files: list[dict[str, Any]] | None = None,
    publish: bool = True,
    github_token: str | None = None,
    auth_kind: str | None = None,
    pr_data: dict[str, Any] | None = None,
    weights: dict[str, int] | None = None,
    custom_check_failures: list[dict[str, Any]] | None = None,
    require_approval: bool | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """End-to-end helper: ``collect_signals`` -> ``compute_readiness`` -> ``publish_readiness``."""
    if not dm.valid_repo(repo):
        raise ValueError("invalid repository name")
    pr_num = int(pr_number)
    if pr_num <= 0:
        raise ValueError("invalid pull request number")

    cfg = agent_config if isinstance(agent_config, dict) else {}
    merge_gate = str(cfg.get("merge_gate") or "advisory").strip().lower()
    raw_threshold = cfg.get("score_threshold")
    score_threshold = 85 if raw_threshold is None else effective_score_threshold(raw_threshold)
    effective_weights = weights if weights is not None else cfg.get("merge_weights")
    effective_require_approval = (
        bool(cfg.get("require_approval", False))
        if require_approval is None
        else bool(require_approval)
    )

    # Capture dirty token & snapshot timestamp BEFORE collecting signals so concurrent
    # webhook deliveries arriving mid-refresh are never silently dropped.
    dirty_token = store.rget(dm.dirty_key(repo, pr_num))
    snapshot_at = _now_iso()

    if github_token is None:
        token, detected_kind = await github_app.token_for_repo(repo)
        resolved_kind = auth_kind or detected_kind
    else:
        token = github_token
        resolved_kind = auth_kind

    head_sha, signals = await collect_signals(
        repo,
        pr_num,
        github_token=token,
        pr_data=pr_data,
        diff_files=diff_files,
        agent_config=cfg,
        custom_check_failures=custom_check_failures,
        client=client,
    )

    report = compute_readiness(
        repo,
        pr_num,
        head_sha,
        signals,
        weights=effective_weights if isinstance(effective_weights, dict) else None,
        score_threshold=score_threshold,
        require_approval=effective_require_approval,
    )

    # Never overwrite a verified report or publish for an unverified / missing PR head SHA.
    if not signals.head_verified or not head_sha:
        log.warning("merge.publisher: unverified PR head for %s#%d; preserving existing state", repo, pr_num)
        existing_report = get_stored_readiness(repo, pr_num)
        raw_inbox = store.rget_json(inbox_key(repo, pr_num))
        existing_inbox = raw_inbox if isinstance(raw_inbox, dict) else None
        effective_gate = merge_gate if publish else "off"
        if effective_gate != "off":
            _finalize_dirty_flag(repo, pr_num, dirty_token, retry_needed=True)
        return {
            "report": existing_report if isinstance(existing_report, dict) else report.model_dump(),
            "inbox": existing_inbox,
            "published": False,
            "gate_published": False,
            "skipped": "unverified_head",
            "merge_gate": effective_gate,
            "auth_kind": resolved_kind or "none",
            "check_run": None,
            "commit_status": None,
            "comment": None,
            "signals": signals.model_dump(),
        }

    findings_summary: dict[str, int] = {"critical": 0, "major": 0, "minor": 0, "nit": 0}
    for finding in signals.open_findings:
        sev = str(finding.get("severity") or "minor").strip().lower()
        if sev in findings_summary:
            findings_summary[sev] += 1
        else:
            findings_summary["nit"] += 1
    findings_summary["total"] = sum(findings_summary.values())

    pr_meta = {
        "title": signals.pr_title,
        "url": signals.pr_url,
        "author": signals.pr_author,
        "state": signals.pr_state,
        "base_ref": signals.base_ref,
        "head_ref": signals.head_ref,
    }

    result = await publish_readiness(
        repo,
        pr_num,
        report,
        merge_gate=merge_gate if publish else "off",
        score_threshold=score_threshold,
        github_token=token,
        auth_kind=resolved_kind,
        pr_meta=pr_meta,
        findings_summary=findings_summary,
        client=client,
        dirty_token=dirty_token,
        snapshot_at=snapshot_at,
        incomplete_signals=signals.incomplete_signals,
    )
    result["signals"] = signals.model_dump()
    return result
