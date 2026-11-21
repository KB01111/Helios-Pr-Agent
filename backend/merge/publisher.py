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

import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import httpx

try:  # cwd=backend
    import datamodel as dm
    import github_app
    import store
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import datamodel as dm  # type: ignore[no-redef]
    from backend import github_app  # type: ignore[no-redef]
    from backend import store  # type: ignore[no-redef]

from .score import compute_readiness
from .signals import collect_signals

log = logging.getLogger("pr-agent.merge.publisher")

GITHUB_API = "https://api.github.com"
CHECK_RUN_NAME = "PR Agent · Merge readiness"
STATUS_CONTEXT = "pr-agent/merge-readiness"
READINESS_MARKER = "<!-- pr-agent-control:readiness -->"

_MAX_COMMENT_PAGES = 10
_CHECK_CACHE_TTL_S = 7 * 24 * 3600
_COMMENT_CACHE_TTL_S = dm.MERGE_REPORT_TTL_S

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


def _comment_cache_key(repo: str, pr_number: int) -> str:
    return f"mergecomment:{repo.lower()}:{int(pr_number)}"


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


def _render_check_summary(report: dm.ReadinessReport, *, score_threshold: int, merge_gate: str) -> tuple[str, str, str]:
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
        report, score_threshold=score_threshold, merge_gate=merge_gate
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
) -> dict[str, Any]:
    if not report.head_sha:
        return {"action": "skipped", "reason": "missing_head_sha"}

    band_label = _BAND_LABELS.get(report.band, report.band)
    if passes_gate:
        state = "success"
        description = f"Merge readiness: {report.score}/100 ({band_label})"
    elif merge_gate == "required":
        state = "failure"
        description = f"Merge readiness: {report.score}/100 ({band_label}, needs {score_threshold}+)"
    else:
        state = "success"
        description = f"Advisory · Merge readiness: {report.score}/100 ({band_label})"

    payload: dict[str, Any] = {
        "state": state,
        "context": STATUS_CONTEXT,
        "description": description[:140],
    }
    safe_target = _safe_url(target_url)
    if safe_target:
        payload["target_url"] = safe_target

    q_sha = urllib.parse.quote(report.head_sha, safe="")
    try:
        resp = await client.post(
            f"{GITHUB_API}/repos/{repo}/statuses/{q_sha}",
            headers=headers,
            json=payload,
        )
        if resp.status_code in (200, 201):
            return {
                "action": "posted",
                "state": state,
                "context": STATUS_CONTEXT,
                "description": payload["description"],
                "status_code": resp.status_code,
            }
        log.warning("merge.publisher: commit status POST failed HTTP %s for %s#%s", resp.status_code, repo, pr_number)
        return {
            "action": "failed",
            "state": state,
            "context": STATUS_CONTEXT,
            "status_code": resp.status_code,
        }
    except Exception as exc:
        log.warning("merge.publisher: commit status error (%s) for %s#%s", type(exc).__name__, repo, pr_number)
        return {"action": "error", "state": state, "context": STATUS_CONTEXT, "error": type(exc).__name__}


async def _find_readiness_comment(
    client: httpx.AsyncClient,
    comments_url: str,
    headers: dict[str, str],
) -> int | None:
    found: int | None = None
    try:
        for page in range(1, _MAX_COMMENT_PAGES + 1):
            resp = await client.get(
                comments_url,
                headers=headers,
                params={"per_page": 100, "page": page},
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not isinstance(batch, list):
                break
            for comment in batch:
                if isinstance(comment, dict) and READINESS_MARKER in str(comment.get("body") or ""):
                    try:
                        found = int(comment["id"])
                    except (KeyError, TypeError, ValueError):
                        pass
            if len(batch) < 100:
                break
    except Exception:
        pass
    return found


async def _upsert_readiness_comment(
    client: httpx.AsyncClient,
    repo: str,
    pr_number: int,
    report: dm.ReadinessReport,
    *,
    headers: dict[str, str],
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

        found_id = await _find_readiness_comment(client, comments_url, headers)
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
        "updated_at": report.computed_at or _now_iso(),
        "merge_gate": merge_gate,
        "score_threshold": int(score_threshold),
    }
    store.rset_json(key, record, ex=dm.MERGE_REPORT_TTL_S)
    return record


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

    store.rdelete(dm.dirty_key(repo, pr_num))
    key = inbox_key(repo, pr_num)
    existing = store.rget_json(key)
    if not isinstance(existing, dict):
        return None
    updated = dict(existing)
    updated["state"] = "merged" if merged else "closed"
    updated["merged"] = bool(merged)
    updated["updated_at"] = _now_iso()
    store.rset_json(key, updated, ex=dm.MERGE_REPORT_TTL_S)
    return updated


# ---------------------------------------------------------------------------
# Main publish & end-to-end orchestration
# ---------------------------------------------------------------------------


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
    threshold = max(0, min(100, int(score_threshold)))

    # 1. Always persist report, clear dirty marker, and update PR inbox.
    store.rset_json(
        dm.merge_report_key(repo, pr_num),
        report.model_dump(),
        ex=dm.MERGE_REPORT_TTL_S,
    )
    store.rdelete(dm.dirty_key(repo, pr_num))
    inbox_record = _upsert_inbox_record(
        repo,
        pr_num,
        report,
        merge_gate=gate,
        score_threshold=threshold,
        pr_meta=pr_meta,
        findings_summary=findings_summary,
    )

    check_result: dict[str, Any] | None = None
    status_result: dict[str, Any] | None = None
    comment_result: dict[str, Any] | None = None
    resolved_kind = auth_kind or "none"

    # 2. Publish to GitHub when enabled and credentials exist.
    if gate != "off":
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
            passes_gate = (report.score >= threshold) and not has_open_blockers

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
                    )

                comment_result = await _upsert_readiness_comment(
                    http,
                    repo,
                    pr_num,
                    report,
                    headers=headers,
                )
            finally:
                if own_client:
                    await http.aclose()

    published = any(
        isinstance(res, dict) and res.get("action") in ("created", "updated", "posted")
        for res in (check_result, status_result, comment_result)
    )
    return {
        "report": report.model_dump(),
        "inbox": inbox_record,
        "published": published,
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
    cfg = agent_config if isinstance(agent_config, dict) else {}
    merge_gate = str(cfg.get("merge_gate") or "advisory").strip().lower()
    raw_threshold = cfg.get("score_threshold")
    score_threshold = 85 if raw_threshold is None else max(0, min(100, int(raw_threshold)))
    effective_weights = weights if weights is not None else cfg.get("merge_weights")
    effective_require_approval = (
        bool(cfg.get("require_approval", False))
        if require_approval is None
        else bool(require_approval)
    )

    if github_token is None:
        token, detected_kind = await github_app.token_for_repo(repo)
        resolved_kind = auth_kind or detected_kind
    else:
        token = github_token
        resolved_kind = auth_kind

    head_sha, signals = await collect_signals(
        repo,
        pr_number,
        github_token=token,
        pr_data=pr_data,
        diff_files=diff_files,
        agent_config=cfg,
        custom_check_failures=custom_check_failures,
        client=client,
    )

    report = compute_readiness(
        repo,
        pr_number,
        head_sha,
        signals,
        weights=effective_weights if isinstance(effective_weights, dict) else None,
        score_threshold=score_threshold,
        require_approval=effective_require_approval,
    )

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
        pr_number,
        report,
        merge_gate=merge_gate if publish else "off",
        score_threshold=score_threshold,
        github_token=token,
        auth_kind=resolved_kind,
        pr_meta=pr_meta,
        findings_summary=findings_summary,
        client=client,
    )
    result["signals"] = signals.model_dump()
    return result
