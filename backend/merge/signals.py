"""Deterministic signal collection for PR merge-readiness scoring (Phase 4).

Collects objective signals from GitHub REST APIs (PR metadata, check runs,
commit statuses, reviews, compare behind-by, changed files), Redis-stored
review findings and blocking repo memories, `decider.py` safety rules, and
the webhook PR activity log.

Self-check exclusion guarantees that our own Merge Readiness Check Run and
commit status never feed back into the score or trigger a self-blocking loop.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import httpx
from pydantic import BaseModel, Field

try:  # cwd=backend
    import datamodel as dm
    import events
    import github_app
    import store
    from sandboxes import decider
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import datamodel as dm  # type: ignore[no-redef]
    from backend import events  # type: ignore[no-redef]
    from backend import github_app  # type: ignore[no-redef]
    from backend import store  # type: ignore[no-redef]
    from backend.sandboxes import decider  # type: ignore[no-redef]

log = logging.getLogger("pr-agent.merge.signals")

GITHUB_API = "https://api.github.com"
OWN_CHECK_RUN_NAME = "PR Agent · Merge readiness"
OWN_STATUS_CONTEXT = "pr-agent/merge-readiness"

FAILING_CHECK_CONCLUSIONS = frozenset({
    "failure",
    "timed_out",
    "action_required",
    "startup_failure",
})
_CODE_EXTS = decider._SRC_EXTS | decider._UI_EXTS


class MergeSignals(BaseModel):
    """Deterministic inputs consumed by ``compute_readiness``."""

    has_conflicts: bool = False
    failing_checks: list[dict[str, Any]] = Field(default_factory=list)
    pending_checks: list[dict[str, Any]] = Field(default_factory=list)
    open_findings: list[dict[str, Any]] = Field(default_factory=list)
    resolved_findings: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_threads: int = Field(default=0, ge=0)
    resolved_threads: int = Field(default=0, ge=0)
    approvals: int = Field(default=0, ge=0)
    changes_requested: int = Field(default=0, ge=0)
    safety_flagged: bool = False
    safety_human_approved: bool = False
    source_changed_without_tests: bool = False
    behind_by: int = Field(default=0, ge=0)
    weak_description: bool = False
    blocking_memory_violations: list[dict[str, Any]] = Field(default_factory=list)
    custom_check_failures: list[dict[str, Any]] = Field(default_factory=list)
    pr_title: str = ""
    pr_url: str = ""
    pr_author: str = ""
    pr_state: str = "open"
    base_ref: str = ""
    head_ref: str = ""


# ---------------------------------------------------------------------------
# Self-check filters (prevent self-loop / self-block)
# ---------------------------------------------------------------------------


def is_own_check_run(run: dict[str, Any]) -> bool:
    """True when a GitHub Check Run belongs to this PR Agent."""
    if not isinstance(run, dict):
        return False
    name = str(run.get("name") or "").strip()
    if name.lower() == OWN_CHECK_RUN_NAME.lower():
        return True
    slug = github_app.app_slug().strip().lower()
    app = run.get("app") if isinstance(run.get("app"), dict) else {}
    run_slug = str(app.get("slug") or "").strip().lower()
    return bool(slug and run_slug and run_slug == slug)


def is_own_status(status: dict[str, Any]) -> bool:
    """True when a GitHub commit status is our own merge-readiness status."""
    if not isinstance(status, dict):
        return False
    context = str(status.get("context") or "").strip().lower()
    return context == OWN_STATUS_CONTEXT.lower()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _check_entry(name: str, url: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name}
    if isinstance(url, str) and url.strip():
        out["url"] = url.strip()
    return out


def _dedupe_check_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the newest check run per (name, app_slug) so re-runs replace stale runs."""
    latest: dict[tuple[str, str], tuple[int, int, dict[str, Any]]] = {}
    for idx, run in enumerate(runs):
        if not isinstance(run, dict) or is_own_check_run(run):
            continue
        name = str(run.get("name") or "check").strip()
        app = run.get("app") if isinstance(run.get("app"), dict) else {}
        app_slug = str(app.get("slug") or "").strip().lower()
        key = (name.lower(), app_slug)
        run_id = _safe_int(run.get("id"), -1)
        prev = latest.get(key)
        if prev is None:
            latest[key] = (run_id, idx, run)
        else:
            prev_id, prev_idx, _ = prev
            if (run_id > prev_id) or (run_id == prev_id and idx >= prev_idx):
                latest[key] = (run_id, idx, run)
    ordered = sorted(latest.values(), key=lambda item: item[1])
    return [item[2] for item in ordered]


def _classify_checks(
    check_runs: list[dict[str, Any]],
    commit_statuses: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    failing: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    seen_fail: set[str] = set()
    seen_pend: set[str] = set()

    for run in _dedupe_check_runs(check_runs):
        name = str(run.get("name") or "check").strip()
        url = run.get("html_url") or run.get("details_url")
        status = str(run.get("status") or "").strip().lower()
        conclusion = str(run.get("conclusion") or "").strip().lower()
        low = name.lower()
        if status != "completed":
            if low not in seen_pend:
                seen_pend.add(low)
                pending.append(_check_entry(name, url))
        elif conclusion in FAILING_CHECK_CONCLUSIONS:
            if low not in seen_fail:
                seen_fail.add(low)
                failing.append(_check_entry(name, url))

    for st in commit_statuses:
        if not isinstance(st, dict) or is_own_status(st):
            continue
        name = str(st.get("context") or "status").strip()
        url = st.get("target_url")
        state = str(st.get("state") or "").strip().lower()
        low = name.lower()
        if state in ("failure", "error"):
            if low not in seen_fail:
                seen_fail.add(low)
                failing.append(_check_entry(name, url))
        elif state == "pending":
            if low not in seen_pend and low not in seen_fail:
                seen_pend.add(low)
                pending.append(_check_entry(name, url))

    return failing, pending


def _tally_reviews(reviews: list[dict[str, Any]]) -> tuple[int, int]:
    """Count effective human approvals and change requests (latest decisive state per reviewer)."""
    by_reviewer: dict[str, str] = {}
    for rev in reviews:
        if not isinstance(rev, dict):
            continue
        user = rev.get("user") if isinstance(rev.get("user"), dict) else {}
        login = str(user.get("login") or rev.get("author") or "").strip().lower()
        if not login:
            continue
        if str(user.get("type") or "") == "Bot" or login.endswith("[bot]"):
            continue
        state = str(rev.get("state") or "").strip().upper()
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            by_reviewer[login] = state
    approvals = sum(1 for st in by_reviewer.values() if st == "APPROVED")
    changes_requested = sum(1 for st in by_reviewer.values() if st == "CHANGES_REQUESTED")
    return approvals, changes_requested


def _threads_from_activity(activity: list[dict[str, Any]]) -> tuple[int, int]:
    """Derive ``(unresolved_threads, resolved_threads)`` from newest-first PR activity."""
    thread_states: dict[str, str] = {}
    for entry in reversed(activity):
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "")
        action = str(entry.get("action") or "").lower()
        if kind == "review_comment":
            cid = _safe_int(entry.get("comment_id"), 0)
            in_reply_to = entry.get("in_reply_to")
            if cid > 0 and not in_reply_to:
                key = f"c:{cid}"
                if action == "created":
                    thread_states[key] = "unresolved"
                elif action == "deleted":
                    thread_states.pop(key, None)
        elif kind == "thread":
            first_cid = _safe_int(entry.get("first_comment_id"), 0)
            node_id = str(entry.get("thread_node_id") or "").strip()
            key = f"c:{first_cid}" if first_cid > 0 else (f"t:{node_id}" if node_id else "")
            if not key:
                continue
            if action == "resolved":
                thread_states[key] = "resolved"
            elif action == "unresolved":
                thread_states[key] = "unresolved"
    unresolved = sum(1 for st in thread_states.values() if st == "unresolved")
    resolved = sum(1 for st in thread_states.values() if st == "resolved")
    return unresolved, resolved


def _reviews_from_activity(activity: list[dict[str, Any]]) -> tuple[int, int]:
    """Fallback review tally from PR activity when GitHub reviews API is unavailable."""
    rows: list[dict[str, Any]] = []
    for entry in reversed(activity):
        if isinstance(entry, dict) and entry.get("kind") == "review":
            rows.append({
                "author": entry.get("author"),
                "state": entry.get("state"),
            })
    return _tally_reviews(rows)


def _latest_sha_from_activity(activity: list[dict[str, Any]]) -> str:
    for entry in activity:
        if not isinstance(entry, dict):
            continue
        sha = str(entry.get("sha") or entry.get("commit_id") or "").strip()
        if sha:
            return sha
    return ""


def _load_findings_and_blocking_memories(
    repo: str, pr_number: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Read stored findings from Redis and resolve any cited ``blocking=True`` memories."""
    prefix = f"finding:{repo.lower()}:{int(pr_number)}:"
    keys = sorted(store.rkeys(f"{prefix}*"))
    if not keys:
        return [], [], []

    raw_items = store.rget_json_many(keys)
    open_findings: list[dict[str, Any]] = []
    resolved_findings: list[dict[str, Any]] = []
    cited_mem_ids: list[str] = []
    seen_mem_ids: set[str] = set()

    for key, item in zip(keys, raw_items):
        if not isinstance(item, dict):
            continue
        fp = str(item.get("fingerprint") or key.rsplit(":", 1)[-1])
        state = str(item.get("state") or "open").strip().lower()
        learning_ids = [str(x).strip() for x in (item.get("learning_ids") or []) if str(x).strip()]
        url = item.get("url") or item.get("html_url") or item.get("comment_url")
        finding_row: dict[str, Any] = {
            "severity": str(item.get("severity") or "minor").strip().lower(),
            "category": str(item.get("category") or "maintainability").strip().lower(),
            "title": str(item.get("title") or "Untitled finding").strip(),
            "file": str(item.get("file") or "").strip(),
            "start_line": max(1, _safe_int(item.get("start_line"), 1)),
            "fingerprint": fp,
            "learning_ids": learning_ids,
        }
        if isinstance(url, str) and url.strip():
            finding_row["url"] = url.strip()

        if state == "open":
            open_findings.append(finding_row)
            for mid in learning_ids:
                if mid not in seen_mem_ids:
                    seen_mem_ids.add(mid)
                    cited_mem_ids.append(mid)
        elif state == "resolved":
            resolved_findings.append(finding_row)

    blocking_violations: list[dict[str, Any]] = []
    if cited_mem_ids:
        mem_keys = [dm.memory_key(repo, mid) for mid in cited_mem_ids]
        mem_rows = store.rget_json_many(mem_keys)
        for mid, mem in zip(cited_mem_ids, mem_rows):
            if not isinstance(mem, dict):
                continue
            status = str(mem.get("status") or "active").strip().lower()
            if mem.get("blocking") is True and status == "active":
                blocking_violations.append({
                    "id": str(mem.get("id") or mid),
                    "text": str(mem.get("text") or f"Blocking rule {mid}").strip(),
                })

    return open_findings, resolved_findings, blocking_violations


def _analyze_diff_files(
    pr_title: str,
    files: list[dict[str, Any]],
) -> tuple[bool, bool]:
    """Return ``(safety_flagged, source_changed_without_tests)`` from PR files."""
    if not files:
        return False, False
    facts = decider.extract_facts(pr_title, files)
    decision = decider.decide(facts)
    safety_flagged = decision.get("reason_code") == "SAFETY_FLAGGED"

    has_source = False
    has_test = False
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("filename") or item.get("path") or "").strip()
        if not path:
            continue
        kind = decider._kind(path)
        if kind == "test":
            has_test = True
        elif kind in ("source", "ui") or (
            kind == "sensitive" and decider._ext(path) in _CODE_EXTS
        ):
            has_source = True
    return safety_flagged, (has_source and not has_test)


async def _gh_get_json(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    *,
    params: dict[str, Any] | None = None,
) -> Any | None:
    """Best-effort GET returning decoded JSON on HTTP 200, else None."""
    try:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            log.debug("merge.signals: GET %s returned HTTP %s", url, resp.status_code)
            return None
        return resp.json()
    except Exception as exc:
        log.warning("merge.signals: GET %s failed (%s)", url, type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# Public collector
# ---------------------------------------------------------------------------


async def collect_signals(
    repo: str,
    pr_number: int,
    *,
    github_token: str | None = None,
    pr_data: dict[str, Any] | None = None,
    diff_files: list[dict[str, Any]] | None = None,
    agent_config: dict[str, Any] | None = None,
    custom_check_failures: list[dict[str, Any]] | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, MergeSignals]:
    """Collect deterministic merge-readiness signals for ``repo`` #``pr_number``.

    Returns ``(head_sha, MergeSignals)``. Optional GitHub endpoints degrade
    gracefully on HTTP/network errors so partial signals still score cleanly.
    """
    if not dm.valid_repo(repo):
        raise ValueError("invalid repository name")
    pr_num = int(pr_number)
    if pr_num <= 0:
        raise ValueError("invalid pull request number")

    if github_token is None:
        token, _auth_kind = await github_app.token_for_repo(repo)
    else:
        token = github_token

    activity = events.pr_activity(repo, pr_num, limit=dm.ACTIVITY_CAP)
    pr: dict[str, Any] = dict(pr_data) if isinstance(pr_data, dict) else {}
    files: list[dict[str, Any]] | None = list(diff_files) if diff_files is not None else None

    check_runs: list[dict[str, Any]] = []
    commit_statuses: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] | None = None
    behind_by = 0

    own_client = client is None
    if token:
        headers = _gh_headers(token)
        http = client or httpx.AsyncClient(timeout=20.0)
        try:
            if not pr:
                fetched_pr = await _gh_get_json(
                    http, f"{GITHUB_API}/repos/{repo}/pulls/{pr_num}", headers
                )
                if isinstance(fetched_pr, dict):
                    pr = fetched_pr

            head_obj = pr.get("head") if isinstance(pr.get("head"), dict) else {}
            base_obj = pr.get("base") if isinstance(pr.get("base"), dict) else {}
            head_sha_tmp = str(head_obj.get("sha") or "")
            base_ref_tmp = str(base_obj.get("ref") or "")

            if head_sha_tmp:
                q_sha = urllib.parse.quote(head_sha_tmp, safe="")
                runs_data = await _gh_get_json(
                    http,
                    f"{GITHUB_API}/repos/{repo}/commits/{q_sha}/check-runs",
                    headers,
                    params={"per_page": 100},
                )
                if isinstance(runs_data, dict) and isinstance(runs_data.get("check_runs"), list):
                    check_runs = runs_data["check_runs"]

                status_data = await _gh_get_json(
                    http,
                    f"{GITHUB_API}/repos/{repo}/commits/{q_sha}/status",
                    headers,
                )
                if isinstance(status_data, dict) and isinstance(status_data.get("statuses"), list):
                    commit_statuses = status_data["statuses"]

                if base_ref_tmp:
                    q_base = urllib.parse.quote(base_ref_tmp, safe="")
                    compare_data = await _gh_get_json(
                        http,
                        f"{GITHUB_API}/repos/{repo}/compare/{q_base}...{q_sha}",
                        headers,
                    )
                    if isinstance(compare_data, dict):
                        behind_by = max(0, _safe_int(compare_data.get("behind_by"), 0))

            reviews_data = await _gh_get_json(
                http,
                f"{GITHUB_API}/repos/{repo}/pulls/{pr_num}/reviews",
                headers,
                params={"per_page": 100},
            )
            if isinstance(reviews_data, list):
                reviews = reviews_data

            if files is None:
                files_data = await _gh_get_json(
                    http,
                    f"{GITHUB_API}/repos/{repo}/pulls/{pr_num}/files",
                    headers,
                    params={"per_page": 100},
                )
                if isinstance(files_data, list):
                    files = files_data
        finally:
            if own_client:
                await http.aclose()

    head_obj = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base_obj = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    user_obj = pr.get("user") if isinstance(pr.get("user"), dict) else {}

    head_sha = str(head_obj.get("sha") or "") or _latest_sha_from_activity(activity)
    base_ref = str(base_obj.get("ref") or "")
    head_ref = str(head_obj.get("ref") or "")
    pr_title = str(pr.get("title") or "")
    pr_url = str(pr.get("html_url") or f"https://github.com/{repo}/pull/{pr_num}")
    pr_author = str(user_obj.get("login") or "")
    pr_state = ("merged" if pr.get("merged") else str(pr.get("state") or "")) if pr else ""

    mergeable_state = str(pr.get("mergeable_state") or "").strip().lower()
    has_conflicts = (pr.get("mergeable") is False) or (mergeable_state == "dirty")
    if behind_by == 0 and mergeable_state == "behind":
        behind_by = 1

    if pr:
        body_text = str(pr.get("body") or "").strip()
        weak_description = len(body_text) < 20
    else:
        weak_description = False

    failing_checks, pending_checks = _classify_checks(check_runs, commit_statuses)

    if reviews is not None:
        approvals, changes_requested = _tally_reviews(reviews)
    else:
        approvals, changes_requested = _reviews_from_activity(activity)

    unresolved_threads, resolved_threads = _threads_from_activity(activity)

    open_findings, resolved_findings, blocking_memory_violations = (
        _load_findings_and_blocking_memories(repo, pr_num)
    )

    safety_flagged, source_changed_without_tests = _analyze_diff_files(
        pr_title, files or []
    )
    # Fallback to stored triage evaluation if files weren't available.
    if not safety_flagged and not files and head_sha:
        eval_rec = store.eval_get(store.eval_id(repo, pr_num, head_sha))
        if isinstance(eval_rec, dict):
            dec = eval_rec.get("decision") if isinstance(eval_rec.get("decision"), dict) else {}
            if dec.get("reason_code") == "SAFETY_FLAGGED":
                safety_flagged = True

    safety_human_approved = bool(safety_flagged and approvals > 0 and changes_requested == 0)

    custom_failures: list[dict[str, Any]] = []
    if custom_check_failures:
        for row in custom_check_failures:
            if isinstance(row, dict):
                custom_failures.append({
                    "name": str(row.get("name") or "Custom check"),
                    "mode": "error" if str(row.get("mode") or "").lower() == "error" else "warning",
                    "detail": str(row.get("detail") or ""),
                })
    # Keep agent_config accepted cleanly for forward compatibility with pre-merge check configs.
    _ = agent_config

    signals = MergeSignals(
        has_conflicts=has_conflicts,
        failing_checks=failing_checks,
        pending_checks=pending_checks,
        open_findings=open_findings,
        resolved_findings=resolved_findings,
        unresolved_threads=unresolved_threads,
        resolved_threads=resolved_threads,
        approvals=approvals,
        changes_requested=changes_requested,
        safety_flagged=safety_flagged,
        safety_human_approved=safety_human_approved,
        source_changed_without_tests=source_changed_without_tests,
        behind_by=behind_by,
        weak_description=weak_description,
        blocking_memory_violations=blocking_memory_violations,
        custom_check_failures=custom_failures,
        pr_title=pr_title,
        pr_url=pr_url,
        pr_author=pr_author,
        pr_state=pr_state,
        base_ref=base_ref,
        head_ref=head_ref,
    )
    return head_sha, signals
